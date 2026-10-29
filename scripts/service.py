#!/usr/bin/env python3
"""Versioned, self-contained maintenance. Existing tools and leases stay intact."""
import argparse
import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import plistlib
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'runtime/storage'))
from storage import GIB, StorageManager, atomic_json
from portable_config import configure
import ask_tier
import auto_tier
import common
import tiers_config
import toolcaches
import checkup
import mole
import plan as space_plan
from storage import Activity

LABELS = {'memory': 'local.dev-resource-guard.memory', 'storage': 'local.dev-resource-guard.storage'}
# launchd wakes the storage job often; each part keeps its own pace inside tick_storage.
STORAGE_WAKE_SECONDS = 300
CACHE_INTERVAL = 12 * 3600
# launchd jobs may not touch removable volumes; run through the app so macOS asks the user once for 资源看板.
APP_EXECUTABLE = 'Applications/资源看板.app/Contents/MacOS/ResourceDashboard'


def schedulers(home, kind):
    """Find both current and pre-existing jobs; never infer absence from our label."""
    result = []
    for directory in (home / 'Library/LaunchAgents', Path('/Library/LaunchAgents')):
        for path in directory.glob('*.plist'):
            try:
                body = plistlib.loads(path.read_bytes())
                args = body.get('ProgramArguments', [])
                text = ' '.join(args) + ' ' + body.get('Program', '')
                label = body.get('Label', '')
                matches = ('dev-' + kind in text or
                           kind == 'memory' and 'dev-idle-cleanup' in label or
                           label == LABELS[kind])
                if not matches:
                    continue
                loaded = subprocess.run(['launchctl', 'print', f'gui/{os.getuid()}/{label}'],
                                        capture_output=True).returncode == 0 if sys.platform == 'darwin' else False
                result.append({'label': label, 'path': str(path), 'loaded': loaded,
                               'interval': body.get('StartInterval'), 'ours': label == LABELS[kind],
                               'supported': kind == 'memory' and args[-1:] == ['tick'] and
                               any(Path(a).name in ('dev_memory.py', 'dev-memory', 'dev-memory.py') for a in args)})
            except (OSError, ValueError, TypeError):
                continue
    return result


def state_root(home):
    return home / '.local/state/dev-resource-guard'


class Busy(ValueError):
    """Another run holds the lock; callers that wake on a timer skip quietly."""


@contextlib.contextmanager
def operation_lock(home, name):
    path = state_root(home) / (name + '.lock')
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a') as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Busy('维护或安装正在进行，请稍后重试')
        yield


def adopt_config(home):
    """Reuse an unambiguous, validated existing slot root, pins and locks."""
    path = home / '.config/dev-resource-guard/config.json'
    if path.exists():
        configure(home)  # Reject invalid config, never silently replace it.
        return
    roots = set()
    for manifest in (home / '.local/state/dev-storage/slots').glob('*.json'):
        body = json.loads(manifest.read_text())
        cache = Path(body['cache_path'])
        key = body['key']
        if (cache.name != 'DerivedData' or cache.parent.name != key or
                len(key) != 64 or any(c not in '0123456789abcdef' for c in key) or
                cache.parents[1].name != 'xcode' or cache.parents[2].name != 'shared' or
                cache.resolve() != cache or not cache.is_relative_to(home)):
            raise ValueError('旧缓存槽路径无法验证，保留配置并停止接入')
        roots.add(str(cache.parents[3]))
    if len(roots) > 1:
        raise ValueError('存在多个缓存根，请明确配置 cache_root；未创建第二套缓存')
    cfg = {'cache_root': next(iter(roots))} if roots else {}
    old = home / '.local/bin/dev-storage'
    if roots and old.is_file():
        result = subprocess.run([str(old), 'status'], capture_output=True, text=True, timeout=30)
        if result.returncode:
            raise ValueError('旧存储策略读取失败；未改写配置')
        status = json.loads(result.stdout)
        for key in ('target_free_gib', 'warning_free_gib', 'critical_free_gib', 'intermediate_budget_gib'):
            if key in status:
                cfg[key] = status[key]
    if not cfg:
        capacity = shutil.disk_usage(home).total / 2**30
        cfg = dict(target_free_gib=round(capacity*.2, 1), warning_free_gib=round(capacity*.15, 1),
                   critical_free_gib=round(capacity*.1, 1), intermediate_budget_gib=round(capacity*.1, 1))
    atomic_json(path, cfg)
    configure(home)


def install_runtime(home):
    """Immutable copy: running builds retain their original executable."""
    digest = hashlib.sha256()
    files = [p for p in sorted(ROOT.rglob('*')) if p.is_file() and
             not {'.git', '__pycache__'}.intersection(p.relative_to(ROOT).parts)]
    for path in files:
        digest.update(str(path.relative_to(ROOT)).encode()); digest.update(path.read_bytes())
    version = digest.hexdigest()[:20]
    target = home / '.local/share/dev-resource-guard/versions' / version
    if not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        staging = target.with_name(version + '.staging-' + str(os.getpid()))
        try:
            staging.mkdir()
            for path in files:
                dest = staging / path.relative_to(ROOT)
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, dest)
            os.rename(staging, target)
        finally:
            if staging.exists(): shutil.rmtree(staging)
    for name, script in [('storage', 'runtime/storage/cli.py'), ('memory', 'runtime/memory/scripts/dev_memory.py'), ('service', 'scripts/service.py')]:
        wrapper = home / '.local/bin' / ('resource-guard-' + name)
        header = '#!/bin/sh\n# Managed by mac-dev-resource-guard\n'
        if wrapper.is_symlink() or wrapper.exists() and not wrapper.read_text().startswith(header):
            raise ValueError('同名命令属于其他工具，保留：' + str(wrapper))
        wrapper.parent.mkdir(parents=True, exist_ok=True)
        temp = wrapper.with_suffix('.new')
        temp.write_text(header + 'exec ' + shlex.quote(sys.executable) + ' ' + shlex.quote(str(target/script)) + ' "$@"\n')
        temp.chmod(0o755); os.replace(temp, wrapper)
        alias = home / '.local/bin' / ('dev-' + name)
        if name != 'service' and not alias.exists() and not alias.is_symlink():
            alias.symlink_to(wrapper)
    return target


def stop_idle_job(domain, label):
    # Disable future dispatch first; disable does not terminate a running job.
    subprocess.run(['launchctl','disable',domain+'/'+label],check=True,capture_output=True)
    probe = subprocess.run(['launchctl','print',domain+'/'+label],capture_output=True,text=True)
    if probe.returncode or re.search(r'^\s*pid = \d+',probe.stdout,re.M):
        subprocess.run(['launchctl','enable',domain+'/'+label],check=True,capture_output=True)
        raise ValueError('旧维护本轮仍在运行，保留并恢复调度；结束后重新完成安装即可迁移')
    result = subprocess.run(['launchctl','bootout',domain+'/'+label],capture_output=True)
    if result.returncode:
        subprocess.run(['launchctl','enable',domain+'/'+label],check=True,capture_output=True)
        raise ValueError('旧调度退出未确认，原配置保留')


def enable_job(home, kind, runtime):
    existing = schedulers(home, kind)
    foreign = [j for j in existing if not j['ours']]
    if foreign:
        if not all(j['supported'] for j in foreign):
            raise ValueError('旧调度的自动清理范围无法确认，未报告安装完成：' + foreign[0]['label'])
        if not all(j['loaded'] for j in foreign):
            raise ValueError('发现未加载的旧调度，请核实后启用；未重复安装：' + foreign[0]['label'])
        return {'kind': kind, 'reused': foreign}
    label = LABELS[kind]
    path = home / 'Library/LaunchAgents' / (label + '.plist')
    domain = f'gui/{os.getuid()}'
    # launchd keeps a stable path; an atomic wrapper update affects only the next
    # invocation. In-flight work retains its immutable runtime and all locks.
    wrapper_tick = [str(home/'.local/bin/resource-guard-service'), 'tick-storage']
    app_tick = [str(home/APP_EXECUTABLE), '--run-tick']
    if kind == 'storage':
        command = app_tick if (home/APP_EXECUTABLE).is_file() else wrapper_tick
    else:
        command = [str(home/'.local/bin/resource-guard-memory'), 'tick']
    body = dict(Label=label, ProgramArguments=command, StartInterval=STORAGE_WAKE_SECONDS if kind == 'storage' else 60,
                RunAtLoad=True, EnvironmentVariables={'HOME': str(home), 'PATH': os.environ.get('PATH', '/usr/bin:/bin:/usr/sbin:/sbin')},
                StandardOutPath='/dev/null', StandardErrorPath=str(state_root(home)/(kind+'-scheduler.err')))
    # Do not boot out a running job during an upgrade. It finishes with the old code.
    migrated = False
    if existing and existing[0]['loaded']:
        previous = plistlib.loads(path.read_bytes())
        if kind == 'storage' and previous.get('ProgramArguments') != command and previous.get('ProgramArguments') in (wrapper_tick, app_tick):
            # Our own job moving between the wrapper and the app launcher: reload only while idle.
            stop_idle_job(domain, label)
            migrated = True
        elif previous.get('ProgramArguments') != command:
            old_command = home/'.local/bin'/('dev-'+kind)
            old_args = [str(old_command), 'tick'] if kind=='memory' else [str(old_command),'maintain','--apply']
            relative = 'runtime/memory/scripts/dev_memory.py' if kind=='memory' else 'runtime/storage/cli.py'
            canonical = home/'.codex/skills/mac-dev-resource-guard'/relative
            lines = old_command.read_text().splitlines() if old_command.is_file() and not old_command.is_symlink() else []
            words = shlex.split(lines[1]) if len(lines)==2 and lines[0]=='#!/bin/sh' else []
            if (previous.get('ProgramArguments') != old_args or len(words)!=4 or words[0]!='exec' or
                    not Path(words[1]).name.startswith('python3') or words[2:]!=[str(canonical),'$@']):
                raise ValueError('同名调度不是可识别的公开旧版本，保留原维护')
            stop_idle_job(domain, label)
            migrated = True
        elif previous.get('StartInterval') != body['StartInterval']:
            # Same command, older pace (1.3 ran every 12 hours): reload only while idle.
            stop_idle_job(domain, label)
            migrated = True
        if not migrated:
            return {'kind': kind, 'reused': existing}
    def write_plist(value):
        # A failed write must not truncate the last working schedule.
        temp = path.with_name(path.name + '.new-' + str(os.getpid()))
        try:
            temp.write_bytes(plistlib.dumps(value)); temp.chmod(0o600)
            os.replace(temp, path)
        finally:
            temp.unlink(missing_ok=True)

    try:
        if migrated:
            subprocess.run(['launchctl','enable',domain+'/'+label],check=True,capture_output=True)
        path.parent.mkdir(parents=True, exist_ok=True)
        write_plist(body)
        result = subprocess.run(['launchctl', 'bootstrap', domain, str(path)], capture_output=True, text=True)
        if result.returncode:
            raise ValueError('定时维护加载失败：' + result.stderr.strip())
        if not any(j['loaded'] for j in schedulers(home, kind)):
            raise ValueError('无法确认定时维护已加载')
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        if migrated:
            try:
                subprocess.run(['launchctl','enable',domain+'/'+label],check=True,capture_output=True)
                if path.read_bytes() != plistlib.dumps(previous):
                    write_plist(previous)
                subprocess.run(['launchctl','bootstrap',domain,str(path)],capture_output=True)
                if not any(j['loaded'] for j in schedulers(home, kind)):
                    raise ValueError('旧调度尚未恢复加载')
            except (OSError, ValueError, subprocess.SubprocessError) as restore_error:
                raise ValueError(f'{error}；恢复失败：{restore_error}') from error
        raise
    return {'kind': kind, 'loaded': True}


def activate(home):
    if sys.platform != 'darwin' or home.resolve() != Path.home().resolve():
        raise ValueError('自动维护仅能为当前 macOS 用户开启')
    with operation_lock(home, 'installation'):
        upgrading = (state_root(home)/'installation.json').is_file()
        adopt_config(home)
        tiers_config.migrate_scope(home)
        runtime = install_runtime(home)
        receipt = dict(schema=1, runtime=str(runtime), started=time.time(), status='configuring', jobs=[])
        atomic_json(state_root(home)/'installation.json', receipt)
        try:
            # An upgrade preserves pauses, including the pre-1.6 storage marker.
            if not upgrading:
                subprocess.run([sys.executable, str(runtime/'runtime/memory/scripts/dev_memory.py'), 'resume'],
                               check=True, capture_output=True)
            for kind in ('memory', 'storage'):
                receipt['jobs'].append(enable_job(home, kind, runtime))
            receipt['status'] = 'active'
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            receipt.update(status='partial', error=str(error))
            raise
        finally:
            atomic_json(state_root(home)/'installation.json', receipt)
        return receipt


FAMILIES = {'managed': '固定构建缓存', 'existing-xcode': '已有 Xcode 缓存'}


def tick_storage(home, now=None):
    """Each wake: 1.3 cache cleanup when 12 hours are due, then the tiers at their own pace."""
    now = time.time() if now is None else now
    with contextlib.ExitStack() as stack:
        try:
            stack.enter_context(operation_lock(home, 'periodic-storage'))
        except Busy:
            return {'status': 'busy', 'results': []}   # the previous wake is still working (e.g. a long move)
        if (state_root(home)/'storage-paused').exists():
            return {'status': 'paused', 'results': []}
        previous = read_json(state_root(home)/'automatic-storage.json')
        if previous and previous.get('status') not in ('running', 'disabled') and now - previous.get('at', 0) < CACHE_INTERVAL - 60:
            receipt = {'status': 'not-due', 'results': [], 'cache_status': previous.get('status')}
        else:
            receipt = clean_caches(home)
        if not (state_root(home)/'storage-paused').exists():
            tick_caches(home, now)
            tick_tiers(home, now)
        return receipt


def read_json(path):
    try:
        return json.loads(path.read_text()) if path.is_file() else None
    except (OSError, ValueError):
        return None


def tick_tiers(home, now):
    """Auto and ask tiers (design C1-C6). Their failures land in their own receipts."""
    root = state_root(home)
    last = read_json(root/'tiers-last.json') or {}
    if now - last.get('at', 0) < last.get('interval_minutes', 30) * 60 - 30:
        return None
    empty = dict(schema=1, at=now, config_error=None, actions=[], kept=[], external_missing=[])
    try:
        settings = configure(home)
        cfg = tiers_config.load(home, owned=[settings.cache_roots[0], settings.state_root, root])
    except ValueError as error:
        atomic_json(root/'tiers-last.json', dict(at=now, interval_minutes=30))
        auto_tier.write_receipt(home, dict(empty, status='config_error', config_error=str(error)))
        return 'config_error'
    atomic_json(root/'tiers-last.json', dict(at=now, interval_minutes=cfg['check_interval_minutes']))
    dashboard = root/'dashboard'/'operation.lock'
    dashboard.parent.mkdir(parents=True, exist_ok=True)
    with dashboard.open('a') as busy:
        try:
            fcntl.flock(busy, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            auto_tier.write_receipt(home, dict(empty, status='busy'))   # someone is cleaning by hand
            return 'busy'
        try:
            auto_tier.run(home, cfg, settings, now=now)
        except Exception as error:
            auto_tier.write_receipt(home, dict(empty, status='error', error=str(error) or type(error).__name__))
        if cfg['ask_enabled']:
            try:
                pending = ask_tier.refresh(home, cfg, settings, auto_tier.default_simctl, now=now)
                free = shutil.disk_usage(str(settings.volume)).free
                ask_tier.maybe_notify(home, pending, free, settings.critical_free_gib * GIB, now)
                (root/'ask-error.json').unlink(missing_ok=True)
            except Exception as error:
                atomic_json(root/'ask-error.json', dict(at=now, error=str(error) or type(error).__name__))
        try:
            refresh_plan(home, cfg, settings, now)
            (root/'plan-error.json').unlink(missing_ok=True)
        except Exception as error:
            atomic_json(root/'plan-error.json', dict(at=now, error=str(error) or type(error).__name__))
    return 'ran'


AGENT_REQUEST = 'agent-request.json'


def current_plan(home, cfg, settings, report=None, now=None):
    report = report or checkup.load_report(home)
    if report is None:
        return None
    pending = ask_tier.load(home, ask_tier.PENDING, ask_tier.empty_pending())
    external = cfg['external_root']
    ready = external is not None and auto_tier.volume_ready(external)
    free = shutil.disk_usage(str(settings.volume)).free
    return space_plan.build(home, cfg, settings, report, pending, free, ready, now=now)


def refresh_plan(home, cfg, settings, now):
    """Keep the checkup fresh, save the plan, and wake the agent only for the unexpected (agent-handoff AC-05)."""
    report = checkup.load_report(home)
    if report is None or now - report.get('at', 0) > 6 * 3600:
        report = checkup.scan(home, cfg, now=now)
    plan = current_plan(home, cfg, settings, report, now)
    space_plan.save(home, plan)
    reason = space_plan.agent_needed(home, plan, space_plan.growth(home, report, now), now)
    if reason:
        atomic_json(state_root(home)/AGENT_REQUEST, dict(at=now, reason=reason, prompt=space_plan.AGENT_PROMPT))
        ask_tier.launch_app()
    return plan


def set_target(home, gib):
    """The user's one number (space-target-plan AC-01): warning = target, stop at 110%, critical at half."""
    value = float(gib)
    capacity = shutil.disk_usage(str(home)).total / GIB
    if not 5 <= value <= capacity * 0.9:
        raise ValueError('空间目标要在 5 GB 到 {:.0f} GB 之间'.format(capacity * 0.9))
    path = home / '.config/dev-resource-guard/config.json'
    body = json.loads(path.read_text()) if path.exists() else {}
    previous = dict(body)
    body.update(space_plan.thresholds(value))
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_json(path, body)
    try:
        configure(home)
    except ValueError:
        atomic_json(path, previous)
        raise
    return body


SKILL = 'mac-dev-resource-guard'
SKILL_MARKER = '.managed-by-resource-guard'
SKILL_SKIP = {'.git', '.github', 'tests', '__pycache__', '.DS_Store'}


def install_skill(home, source=ROOT):
    """agent-handoff AC-01: put the companion skill where Codex and Claude Code look, and keep it current.

    An older git clone with no local changes is backed up and replaced; one with changes is kept.
    """
    canonical = home / '.codex/skills' / SKILL
    notes = []
    if canonical.resolve() == Path(source).resolve():
        return notes
    if canonical.is_symlink() or canonical.exists() and not canonical.is_dir():
        return ['已有同名 skill 不是目录，保留未替换：' + str(canonical)]
    if canonical.is_dir() and not (canonical / SKILL_MARKER).exists():
        if not (canonical / '.git').exists():
            return ['已有同名 skill 不是本工具装的，保留未替换：' + str(canonical)]
        status = subprocess.run(['git', '-C', str(canonical), 'status', '--porcelain'], capture_output=True, text=True)
        if status.returncode or status.stdout.strip():
            return ['旧 skill 有本地改动，保留未替换：' + str(canonical)]
        backup = state_root(home) / 'skill-backups' / str(time.time_ns())
        backup.parent.mkdir(parents=True, exist_ok=True)
        os.rename(canonical, backup)
        notes.append('旧 skill 已备份到 ' + str(backup))
    canonical.parent.mkdir(parents=True, exist_ok=True)
    staged = canonical.with_name('.' + SKILL + '.new')
    if staged.exists():
        shutil.rmtree(staged)
    shutil.copytree(source, staged, symlinks=True, ignore=lambda folder, names: [n for n in names if n in SKILL_SKIP])
    (staged / SKILL_MARKER).write_text('installed {}\n'.format(time.strftime('%Y-%m-%d %H:%M')))
    old = canonical.with_name('.' + SKILL + '.old')
    if canonical.exists():
        os.rename(canonical, old)
    os.rename(staged, canonical)
    if old.exists():
        shutil.rmtree(old)
    claude = home / '.claude/skills' / SKILL
    if not claude.exists() and not claude.is_symlink():
        claude.parent.mkdir(parents=True, exist_ok=True)
        os.symlink(canonical, claude)
    elif not (claude.is_symlink() and claude.resolve() == canonical.resolve()):
        notes.append('Claude Code 那边已有同名 skill，保留：' + str(claude))
    return notes


def prune_output(home, path):
    """The one delete the agent may ask for directly (agent-handoff AC-03): a rebuildable build folder, not in use."""
    import tasks as T
    from storage import Activity
    target = Path(path).expanduser()
    if target.is_symlink() or not target.is_dir():
        raise ValueError('只能删真实的构建产物目录：' + str(target))
    target = target.resolve()
    if target.name not in T.PRUNE_NAMES and not space_plan.is_xcode_build(target.name):
        raise ValueError('不在可重建产物名单里（{}，或名字里带 DerivedData）：{}'.format('、'.join(T.PRUNE_NAMES), target))
    settings = configure(home)
    cfg = tiers_config.load(home, owned=[settings.cache_roots[0], settings.state_root, state_root(home)])
    roots = [Path(r['path']).resolve() for r in cfg['task_roots']] + [Path(r['external']) for r in cfg['task_roots'] if r['external']]
    if not any((p / '.git').exists() for p in target.parents) and not any(r in target.parents for r in roots):
        raise ValueError('既不在 git 工作区里，也不在任务目录里，保留：' + str(target))
    reasons = Activity.collect(settings.lease_root).reasons(target)
    if reasons:
        raise ValueError('正在使用，保留：' + '；'.join(reasons))
    size = T.size(target)
    T.remove_tree(target)
    auto_tier.append_log(home, dict(schema=1, at=time.time(), status='agent', actions=[
        dict(kind='prune', path=str(target), bytes=size or 0, result='done', reason='用户在对话里确认，由 agent 发起')]))
    return {'removed': str(target), 'bytes': size}


def current_target(home):
    """`target` with no number: what is set now, and what we would suggest for this disk."""
    settings = configure(home)
    return dict(target_gib=settings.warning_free_gib, stop_at_gib=settings.target_free_gib,
                critical_gib=settings.critical_free_gib,
                suggested_gib=space_plan.suggested_target(shutil.disk_usage(str(home)).total))


def guard_mole(home, remove=False):
    settings = configure(home)
    cfg = tiers_config.load(home, owned=[settings.cache_roots[0], settings.state_root, state_root(home)])
    return mole.guard(home, cfg, toolcaches.inspect(home, cfg['external_root'], sizes=False, touch_external=False), remove)


def tick_caches(home, now, launchctl=toolcaches.default_launchctl):
    """Shared build caches (toolchain-cache-takeover): keep the Xcode setting alive, move local caches to the drive."""
    try:
        settings = configure(home)
        cfg = tiers_config.load(home, owned=[settings.cache_roots[0], settings.state_root, state_root(home)])
    except ValueError:
        return None   # tick_tiers records the settings error
    try:
        if cfg['xcode_compile_cache']:
            toolcaches.ensure_env(home, launchctl)
        toolcaches.clear_leftovers(home)
        if not cfg['caches_enabled'] or cfg['external_root'] is None:
            return None
        activity = Activity.collect(settings.lease_root)
        actions = []
        if any(row['status'] == 'local' for row in toolcaches.inspect(home, cfg['external_root'], sizes=False)):
            actions += toolcaches.take_over(home, cfg['external_root'], activity)
        actions += toolcaches.take_over_builds(home, cfg['external_root'], cfg, activity)
    except Exception as error:
        actions = [dict(kind='cache-move', path=str(cfg['external_root']), bytes=0, result='failed', reason=str(error) or type(error).__name__)]
    if actions:
        auto_tier.append_log(home, dict(schema=1, at=now, status='caches', actions=actions))
    return actions or None


def clean_caches(home):
    """1.3 cleanup of the managed slots and the existing Xcode caches."""
    try:
        cfg = tiers_config.load(home)
    except ValueError as error:
        receipt = dict(at=time.time(), status='error', error=str(error), results=[])
        atomic_json(state_root(home)/'automatic-storage.json', receipt)
        return receipt
    if not cfg['auto_enabled']:
        receipt = dict(at=time.time(), status='disabled', results=[])
        atomic_json(state_root(home)/'automatic-storage.json', receipt)
        return receipt
    results = []
    for name, build in [('managed', lambda: StorageManager(configure(home))), ('existing-xcode', lambda: common.manager(home))]:
        if (state_root(home)/'storage-paused').exists(): break
        started = time.time()
        # Each family stands alone: a failure is recorded for the dashboard, never hides the other.
        try:
            manager = build()
            manager.settings.normal_days = manager.settings.pressure_days = manager.settings.critical_days = 7
            plan = manager.plan(activity=common.activity(manager.settings.lease_root))
            def protected_activity():
                activity = common.activity(manager.settings.lease_root)
                try:
                    enabled = tiers_config.load(home)['auto_enabled']
                except ValueError:
                    enabled = False
                if not enabled or (state_root(home)/'storage-paused').exists():
                    activity.errors.append('automatic maintenance paused or configuration invalid')
                return activity
            result = manager.apply(plan, activity_factory=protected_activity, manual=True)
            results.append(dict(family=name, started=started, eligible_bytes=plan['eligible_accounting_bytes'], result=result))
        except common.CacheUnavailable as reason:
            results.append(dict(family=name, started=started, skipped=str(reason)))
        except Exception as error:
            results.append(dict(family=name, started=started, error=str(error) or type(error).__name__))
        atomic_json(state_root(home)/'automatic-storage.json', dict(at=time.time(), status='running', results=results))
    errors = []
    for r in results:
        if r.get('error'):
            errors.append(FAMILIES[r['family']] + '：' + r['error'])
        elif r.get('result', {}).get('errors'):
            errors.append('{}：{} 项删除失败'.format(FAMILIES[r['family']], len(r['result']['errors'])))
    status = 'paused' if (state_root(home)/'storage-paused').exists() else 'error' if errors else 'busy' if any(r.get('result', {}).get('status')=='busy' for r in results) else 'complete'
    receipt = dict(at=time.time(), status=status, results=results)
    if errors:
        receipt['error'] = '；'.join(errors)
    atomic_json(state_root(home)/'automatic-storage.json', receipt)
    return receipt


def set_enabled(home, kind, enabled):
    info = state_root(home)/'installation.json'
    if not info.is_file(): raise ValueError('请先完成安装')
    runtime = Path(json.loads(info.read_text())['runtime'])
    if kind == 'memory':
        subprocess.run([sys.executable, str(runtime/'runtime/memory/scripts/dev_memory.py'), 'resume' if enabled else 'pause'],
                       check=True, capture_output=True)
    else:
        if any(not j['ours'] for j in schedulers(home, kind)):
            raise ValueError('现有磁盘调度属于原工具；请在原入口暂停，未覆盖或重复启动')
        paused = state_root(home)/'storage-paused'
        if enabled: paused.unlink(missing_ok=True)
        else: paused.touch()
    if enabled: enable_job(home, kind, runtime)


def integrate(home, project):
    project = project.expanduser().resolve(strict=True)
    if not project.is_dir(): raise ValueError('请选择项目目录')
    command = home / '.local/bin/resource-guard-storage'
    if not command.is_file(): raise ValueError('请先完成安装并开启维护')
    path = project / '.dev-resource-guard/xcode-build'
    header = '#!/bin/sh\n# Managed by mac-dev-resource-guard\n'
    content = header + 'exec ' + shlex.quote(str(command)) + ' xcode --worktree ' + shlex.quote(str(project)) + ' "$@"\n'
    if path.is_symlink() or path.parent.is_symlink() or path.exists() and path.read_text() != content:
        raise ValueError('项目已有不同构建入口，保留原文件')
    path.parent.mkdir(exist_ok=True)
    with path.open('w') as f: f.write(content)
    path.chmod(0o755)
    # Keep the local entry out of git status without editing the project's own .gitignore.
    ignore = path.parent / '.gitignore'
    if not ignore.exists() and not ignore.is_symlink():
        ignore.write_text('# Local build entry generated by mac-dev-resource-guard; not part of the project.\n*\n')
    atomic_json(state_root(home)/'project.json', dict(path=str(project), wrapper=str(path), at=time.time()))
    return {'path': str(project), 'wrapper': str(path)}


def menu_app_pids(executable, listing=None):
    """PIDs of the running menu app: exactly this executable, no arguments. The storage tick runs the same
    binary with --run-tick and is left alone. Matched here, not with pkill: one process whose command line
    holds half a character makes pkill's regex fail for the whole list, and the old app kept running."""
    if listing is None:
        listing = subprocess.run(['/bin/ps', '-axww', '-o', 'pid=,command='], capture_output=True, text=True,
                                 errors='surrogateescape', env=dict(os.environ, LC_ALL='en_US.UTF-8')).stdout or ''
    found = []
    for line in listing.splitlines():
        pid, _, command = line.strip().partition(' ')
        if pid.isdigit() and command.strip() == str(executable):
            found.append(int(pid))
    return found


def stop_menu_app(executable, wait=5.0):
    """Quit the running menu app and wait for it, so `open` starts the new build instead of reusing the old one."""
    def alive(pid):
        try:
            if os.waitpid(pid, os.WNOHANG)[0] == pid:
                return False   # our own child that has just exited
        except ChildProcessError:
            pass
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
    pids = menu_app_pids(executable)
    for sig in (signal.SIGTERM, signal.SIGKILL):
        for pid in pids:
            try:
                os.kill(pid, sig)
            except ProcessLookupError:
                pass
        deadline = time.time() + wait
        while time.time() < deadline and any(alive(p) for p in pids):
            time.sleep(0.1)
        pids = [p for p in pids if alive(p)]
        if not pids:
            break
    return pids


CONSENT = 'consent.json'
YES, NO = ('yes', 'y', '1', 'true'), ('no', 'n', '0', 'false')


def consent_question(drive=None):
    """The one question at the first install (RAW-36): what turning everything on changes, in plain words."""
    lines = ['默认打开这几项：',
             '  · 共享构建缓存：各任务共用一份工具缓存，并打开 Xcode 编译缓存（在 ~/.zshenv 加一段带标记的设置，可一键还原）'
             + ('；任务目录里的 Xcode 构建产物也放到外接盘' if drive else '')]
    if drive:
        lines.append('  · 外接盘「{}」：缓存和构建产物放过去；空间不够时，闲置且改动都已推送的任务也整个挪过去'.format(drive))
    lines.append('（安全自动清理默认就开着：只清 7 天没用过的编译中间缓存，任务目录和模拟器不动。）')
    return '\n'.join(lines) + '\n同意吗？[Y/n] '


def ask_consent(home, drive=None, env=None, tty='/dev/tty', interactive=None):
    """Asked once, at the first install: 'yes' / 'no', 'decided' on upgrades, 'unasked' when nobody could answer.
    RESOURCE_GUARD_ENABLE=yes|no answers for an agent that asked the user in its chat."""
    env = os.environ if env is None else env
    path = state_root(home) / CONSENT
    if path.exists():
        return 'decided'
    cfg = tiers_config.load(home)
    if cfg['caches_enabled']:
        atomic_json(path, dict(consent=True, at=time.time(), how='already on'))
        return 'decided'
    value = env.get('RESOURCE_GUARD_ENABLE', '').strip().lower()
    if value in YES or value in NO:
        answer = value in YES
    else:
        # Only a person at a terminal is asked; an agent's captured output never waits on a prompt.
        if not (sys.stdout.isatty() if interactive is None else interactive):
            return 'unasked'
        try:
            with open(tty) as reply, open(tty, 'a') as screen:
                screen.write(consent_question(drive)); screen.flush()
                answer = reply.readline().strip().lower() not in NO
        except OSError:
            return 'unasked'
    atomic_json(path, dict(consent=answer, at=time.time()))
    return 'yes' if answer else 'no'


def enable_recommended(home, launchctl=toolcaches.default_launchctl, probe=auto_tier.external_access, kick=True, **scan):
    """After the user agreed: one checkup, then shared caches, cleanup and the drive (if one is plugged in) on."""
    settings = configure(home)
    owned = [settings.cache_roots[0], settings.state_root, state_root(home)]
    checkup.scan(home, tiers_config.load(home, owned=owned), **scan)
    cfg, notes = checkup.apply(home, ['caches', 'clean', 'external'], owned, probe=probe, launchctl=launchctl)
    on = [name for name, done in (('共享构建缓存', cfg['caches_enabled']), ('自动清理', cfg['auto_enabled'] and cfg['ask_enabled']),
                                  ('外接盘「{}」'.format(Path(toolcaches.volume_of(str(cfg['external_root'])) or '').name), cfg['external_root']))
          if done]
    atomic_json(state_root(home) / CONSENT, dict(consent=True, at=time.time(), applied=on))
    if kick:
        subprocess.run(['/bin/launchctl', 'kickstart', 'gui/{}/local.dev-resource-guard.storage'.format(os.getuid())], capture_output=True)
    return (['已打开：' + '、'.join(on)] if on else []) + notes


def main():
    p=argparse.ArgumentParser(); p.add_argument('action', choices=['activate', 'tick-storage', 'integrate', 'plan', 'target', 'agent-app', 'mole-guard', 'prune', 'enable'])
    p.add_argument('value', nargs='?', help='target: GB to keep free; agent-app: claude or codex; mole-guard: remove')
    p.add_argument('--json', action='store_true', help='plan: print the plan as JSON (the default)')
    p.add_argument('--project', type=Path); a=p.parse_args(); home=Path.home()
    if a.action in ('plan', 'agent-app'):
        settings = configure(home)
        owned = [settings.cache_roots[0], settings.state_root, state_root(home)]
        if a.action == 'agent-app':
            result = {k: str(v) for k, v in tiers_config.update(home, dict(agent_app=a.value), owned).items() if k == 'agent_app'}
        else:
            result = current_plan(home, tiers_config.load(home, owned=owned), settings) or {'status': 'no-checkup', 'message': '还没体检过：打开资源看板，或等下一轮后台检查'}
    elif a.action == 'target' and a.value is None:
        result = current_target(home)
    elif a.action == 'target':
        result = set_target(home, a.value)
    elif a.action == 'mole-guard':
        result = {'protected': guard_mole(home, remove=a.value == 'remove')}
    elif a.action == 'prune':
        result = prune_output(home, a.value or '')
    elif a.action == 'enable':   # the user agreed in their agent's chat after an install that could not ask
        result = {'enabled': enable_recommended(home)}
    else:
        result = activate(home) if a.action=='activate' else tick_storage(home) if a.action=='tick-storage' else integrate(home,a.project)
    print(json.dumps(result,ensure_ascii=False))
    if a.action=='tick-storage' and result['status']=='error':
        print(result['error'],file=sys.stderr);sys.exit(2)

if __name__=='__main__':
    try: main()
    except (OSError,ValueError,subprocess.SubprocessError) as error:
        print(str(error),file=sys.stderr);sys.exit(2)
