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
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'runtime/storage'))
from storage import StorageManager, atomic_json
from portable_config import configure
import common

LABELS = {'memory': 'local.dev-resource-guard.memory', 'storage': 'local.dev-resource-guard.storage'}


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


@contextlib.contextmanager
def operation_lock(home, name):
    path = state_root(home) / (name + '.lock')
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a') as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError('维护或安装正在进行，请稍后重试')
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
    command = [str(home/'.local/bin/resource-guard-service'), 'tick-storage'] if kind == 'storage' else [
        str(home/'.local/bin/resource-guard-memory'), 'tick']
    body = dict(Label=label, ProgramArguments=command, StartInterval=43200 if kind == 'storage' else 60,
                RunAtLoad=True, EnvironmentVariables={'HOME': str(home), 'PATH': os.environ.get('PATH', '/usr/bin:/bin:/usr/sbin:/sbin')},
                StandardOutPath='/dev/null', StandardErrorPath=str(state_root(home)/(kind+'-scheduler.err')))
    # Do not boot out a running job during an upgrade. It finishes with the old code.
    migrated = False
    if existing and existing[0]['loaded']:
        previous = plistlib.loads(path.read_bytes())
        if previous.get('ProgramArguments') != command:
            old_command = home/'.local/bin'/('dev-'+kind)
            old_args = [str(old_command), 'tick'] if kind=='memory' else [str(old_command),'maintain','--apply']
            relative = 'runtime/memory/scripts/dev_memory.py' if kind=='memory' else 'runtime/storage/cli.py'
            canonical = home/'.codex/skills/mac-dev-resource-guard'/relative
            lines = old_command.read_text().splitlines() if old_command.is_file() and not old_command.is_symlink() else []
            words = shlex.split(lines[1]) if len(lines)==2 and lines[0]=='#!/bin/sh' else []
            if (previous.get('ProgramArguments') != old_args or len(words)!=4 or words[0]!='exec' or
                    not Path(words[1]).name.startswith('python3') or words[2:]!=[str(canonical),'$@']):
                raise ValueError('同名调度不是可识别的公开旧版本，保留原维护')
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
            migrated = True
        else:
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
        adopt_config(home)
        runtime = install_runtime(home)
        receipt = dict(schema=1, runtime=str(runtime), started=time.time(), status='configuring', jobs=[])
        atomic_json(state_root(home)/'installation.json', receipt)
        try:
            # Resume uses the same state lock as tick and preserves all ownership data.
            subprocess.run([sys.executable, str(runtime/'runtime/memory/scripts/dev_memory.py'), 'resume'],
                           check=True, capture_output=True)
            (state_root(home)/'storage-paused').unlink(missing_ok=True)
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


def tick_storage(home):
    """Same guarded delete path for periodic and manual cleanup, even on roomy disks."""
    results = []
    with operation_lock(home, 'periodic-storage'):
        if (state_root(home)/'storage-paused').exists():
            return {'status': 'paused', 'results': []}
        for name, build in [('managed', lambda: StorageManager(configure(home))), ('existing-xcode', lambda: common.manager(home))]:
            if (state_root(home)/'storage-paused').exists(): break
            started = time.time()
            # Each family stands alone: a failure is recorded for the dashboard, never hides the other.
            try:
                manager = build()
                plan = manager.plan(activity=common.activity(manager.settings.lease_root))
                def protected_activity():
                    activity = common.activity(manager.settings.lease_root)
                    if (state_root(home)/'storage-paused').exists():
                        activity.errors.append('automatic maintenance paused')
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


def main():
    p=argparse.ArgumentParser(); p.add_argument('action', choices=['activate', 'tick-storage', 'integrate'])
    p.add_argument('--project', type=Path); a=p.parse_args(); home=Path.home()
    result = activate(home) if a.action=='activate' else tick_storage(home) if a.action=='tick-storage' else integrate(home,a.project)
    print(json.dumps(result,ensure_ascii=False))
    if a.action=='tick-storage' and result['status']=='error':
        print(result['error'],file=sys.stderr);sys.exit(2)

if __name__=='__main__':
    try: main()
    except (OSError,ValueError,subprocess.SubprocessError) as error:
        print(str(error),file=sys.stderr);sys.exit(2)
