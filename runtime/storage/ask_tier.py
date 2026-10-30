"""Ask tier (design C4/C6): cleanups that lose something are proposed, never done unasked."""
from datetime import datetime
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import time

from storage import GIB, Activity, StorageManager, atomic_json
import agent_plan
import spaces
import tasks as T

PENDING, REQUEST, ASK_STATE = 'pending.json', 'notify-request.json', 'ask-state.json'
LOSES = {
    'simulator-delete': '上面装的 App 和登录状态',
    'simulator-shutdown': '当前画面和 App 运行状态',
    'task-delete': '本地的构建产物和被忽略的文件；要用时得重新拉代码',
    'runtime-delete': '以后要建这个版本的模拟器得重新下载',
    'installer-delete': '安装包文件，要用时得重新下载',
    'trash-empty': '废纸篓里的全部文件，清空后不能恢复',
    'device-support-delete': '下次连这台设备调试时，Xcode 要花几分钟重新准备',
    'archive-delete': '这个版本的安装包和查崩溃用的符号文件',
}
# The simulator's own processes and the viewer app mention the UDID but are not tools using it.
NOT_A_TOOL = re.compile(r'Simulator\.app/Contents/MacOS/Simulator|launchd_sim|CoreSimulator/Devices/')
# Watching, not using: an agent's screen stream or a log stream it left running (10-30: five simulators held
# for up to a day this way). Only how long such a stream has run says whether anyone still looks.
# The program itself must be the stream (a word of its own, or the last part of a path), not a path that happens
# to hold these words, like a project in /repos/baguette/stream.
PASSIVE = re.compile(r'(?i)(?:^|[\s;&|(/])baguette\s+stream(?:\s|$)|(?:^|[\s;&|(/])simctl\s+spawn\s+\S+\s+log\s+stream(?:\s|$)')
APP = Path.home() / 'Applications/资源看板.app'


def state_root(home):
    return Path(home) / '.local/state/dev-resource-guard'


def load(home, name, default):
    path = state_root(home) / name
    try:
        return json.loads(path.read_text()) if path.exists() else default
    except (OSError, ValueError):
        return default


def save(home, name, value):
    state_root(home).mkdir(parents=True, exist_ok=True)
    atomic_json(state_root(home) / name, value)


def empty_pending():
    return dict(schema=1, updated=None, items=[], notify=dict(last_day=None, last_critical_at=None))


def parse_time(value):
    try:
        return datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp() if value else None
    except ValueError:
        return None


def last_used(device):
    """lastBootedAt is often empty; device.plist changes on every boot and shutdown.

    The data folder is not used: CoreSimulator touches every device's data folder
    at once (seen after a service update), which would make all of them look new.
    """
    times = [parse_time(device.get('lastBootedAt'))]
    data = device.get('dataPath')
    if data:
        try:
            times.append((Path(data).parent / 'device.plist').stat().st_mtime)
        except OSError:
            pass
    times = [t for t in times if t]
    return max(times) if times else None


def attached(udid, activity, stream_hours=None, ages=None):
    """Something uses this simulator. With `stream_hours`, a screen or log stream running longer than that
    no longer counts (rule simulator-shutdown); `ages` maps pid to seconds running."""
    for pid, command in activity.commands:
        if udid not in command or NOT_A_TOOL.search(command):
            continue
        if stream_hours and PASSIVE.search(command) and (ages or {}).get(pid, 0) >= stream_hours * 3600:
            continue
        return True
    return any(udid in reference for reference in activity.lease_refs)


def process_ages(run=subprocess.run):
    """Seconds each process has been running ({pid: seconds}); empty when ps cannot say."""
    ages = {}
    try:
        out = run(['/bin/ps', '-axo', 'pid=,etime='], capture_output=True, text=True, timeout=20).stdout
    except (OSError, subprocess.SubprocessError):
        return ages
    for line in out.splitlines():
        fields = line.split()
        if len(fields) != 2 or not fields[0].isdigit():
            continue
        days, _, clock = fields[1].rpartition('-')
        parts = [int(p) for p in clock.split(':') if p.isdigit()]
        seconds = 0
        for part in parts:
            seconds = seconds * 60 + part
        ages[int(fields[0])] = seconds + (int(days) * 86400 if days.isdigit() else 0)
    return ages


def runtime_label(identifier, version=None):
    name = identifier.rsplit('.', 1)[-1]
    platform = name.split('-', 1)[0]
    return '{} {}'.format(platform, version or name.split('-', 1)[-1].replace('-', '.'))


def task_blocker(task, cfg, activity, pins, now):
    """Why a task directory cannot even be proposed for deletion, or None."""
    reasons, latest = T.idle_check(task, activity, pins, cfg['ask']['task_idle_days'], cfg, now)
    if reasons:
        return '；'.join(reasons), latest
    skipped = T.skip_names(cfg)
    loose = T.loose_files(task, skipped)
    if loose:
        return '有不在代码仓库里的文件：' + '、'.join(loose[:3]), latest
    repos = T.repositories(task.real, skipped)
    if not repos:
        return '没有代码仓库', latest
    for repo in repos:
        change = T.git_changes(repo)
        if change:
            return '{}：{}'.format(repo.name, change), latest
        if (repo / '.git').is_dir():
            listed = T.git(repo, 'worktree', 'list', '--porcelain')
            if listed.returncode or listed.stdout.count('worktree ') > 1:
                return '{} 还有别的工作区依赖它'.format(repo.name), latest
    return None, latest


def candidates(home, cfg, activity, pins, simctl, now, memory, only=None):
    """Everything worth asking about; with `only` (ids), just those, so a confirmation rechecks in seconds."""
    found = {}
    kinds = None if only is None else {key.split(':', 1)[0] for key in only}

    def add(kind, key, title, bytes_, reason, fingerprint, **extra):
        found[kind + ':' + key] = dict(id=kind + ':' + key, kind=kind, title=title, bytes=bytes_ or 0, reason=reason,
                                       loses=LOSES[kind], fingerprint=fingerprint, **extra)

    other_candidates(home, activity, now, add, kinds=kinds)
    if kinds is not None and not kinds & {'simulator-shutdown', 'simulator-delete', 'runtime-delete'}:
        return _tasks(cfg, activity, pins, now, add, only, found)
    listing = json.loads(simctl(['list', 'devices', '--json']))
    stream_hours = agent_plan.load(home)['rules']['simulator-shutdown']['params'].get('stream_hours')
    ages = process_ages() if stream_hours else {}
    used_runtimes, booted = set(), set()
    unattached = memory.setdefault('unattached_since', {})
    for runtime, devices in listing.get('devices', {}).items():
        if devices:
            used_runtimes.add(runtime)
        for device in devices:
            udid = device['udid']
            title = '{}（{}）'.format(device.get('name', udid), runtime_label(runtime))
            if device.get('isAvailable') is False:
                # It cannot boot (its system version is gone); still the user's data, so asked, never auto-deleted.
                add('simulator-delete', udid, title, device.get('dataPathSize') or 0, '系统标为不可用，开不了机', 'unavailable',
                    path=device.get('dataPath'))
                continue
            data = device.get('dataPath')
            pinned = data and T.pin_reason(pins, T.Task(Path(data), Path(data), Path(data)))
            protected = attached(udid, activity) or pinned
            if device.get('state') == 'Booted':
                booted.add(udid)
                if attached(udid, activity, stream_hours, ages) or pinned:
                    unattached.pop(udid, None)
                    continue
                since = unattached.setdefault(udid, now)
                hours = (now - since) / 3600
                if hours >= cfg['ask']['simulator_unattached_hours']:
                    add('simulator-shutdown', udid, title, 0, '开着 {:.0f} 小时没有开发工具连着'.format(hours), 'since:{:.0f}'.format(since),
                        path=data)
            elif device.get('state') == 'Shutdown' and not protected:
                last = last_used(device)
                if last is not None and now - last >= cfg['ask']['simulator_unused_days'] * T.DAY:
                    add('simulator-delete', udid, title, device.get('dataPathSize') or (T.size(data) if data else 0),
                        '已关机 {:.0f} 天没开过'.format((now - last) / T.DAY), 'last:{:.0f}'.format(last), path=data)
    for udid in list(unattached):
        if udid not in booted:
            unattached.pop(udid)
    runtimes = json.loads(simctl(['runtime', 'list', '-j']))
    for key, runtime in runtimes.items():
        identifier = runtime.get('runtimeIdentifier', '')
        if runtime.get('deletable') and runtime.get('state') == 'Ready' and identifier and identifier not in used_runtimes:
            add('runtime-delete', runtime.get('identifier', key), 'iOS 运行时 ' + runtime_label(identifier, runtime.get('version'))
                if '.iOS-' in identifier else '运行时 ' + runtime_label(identifier, runtime.get('version')),
                runtime.get('sizeBytes'), '没有任何模拟器在用', 'runtime:' + identifier)
    return _tasks(cfg, activity, pins, now, add, only, found)


def _tasks(cfg, activity, pins, now, add, only, found):
    for task in T.discover(cfg):
        if not task.reachable or only is not None and 'task-delete:' + str(task.path) not in only:
            continue
        blocker, latest = task_blocker(task, cfg, activity, pins, now)
        if blocker is None:
            where = '（在外接盘上）' if task.linked else ''
            add('task-delete', str(task.path), task.path.name + where, T.size(task.real),
                '闲置 {:.0f} 天，改动都已提交并推送'.format((now - latest) / T.DAY), 'latest:{:.0f}'.format(latest))
    return found if only is None else {key: value for key, value in found.items() if key in only}


def other_candidates(home, activity, now, add, mounted=None, kinds=None):
    """Installers, the Trash and Xcode leftovers (space-watch AC-02~04); `kinds` limits which are looked at."""
    for item in (spaces.installers(home, now, mounted) if kinds is None or 'installer-delete' in kinds else []):
        if item['idle'] and not activity.reasons(Path(item['path'])):
            add('installer-delete', item['path'], '下载里的 ' + Path(item['path']).name, item['bytes'],
                '{:.0f} 天没动，没有挂载'.format(item['idle_days']), 'mtime:{:.0f}'.format(item['mtime']))
    trash = spaces.trash_size(home) if kinds is None or 'trash-empty' in kinds else None
    if trash is not None and trash >= spaces.TRASH_MIN_BYTES:
        add('trash-empty', str(Path(home) / '.Trash'), '废纸篓', trash, '废纸篓占了 {:.1f} GB'.format(trash / GIB),
            'bytes:{}'.format(trash // GIB))
    for item in (spaces.device_support(home, now) if kinds is None or 'device-support-delete' in kinds else []):
        if item['idle_days'] >= spaces.DEVICE_SUPPORT_IDLE_DAYS and not activity.reasons(Path(item['path'])):
            add('device-support-delete', item['path'], '{} {} 设备支持文件'.format(item['platform'], item['version']), item['bytes'],
                '{:.0f} 天没连过这个版本的设备'.format(item['idle_days']), 'mtime:{:.0f}'.format(item['mtime']))
    for item in (spaces.archives(home, now) if kinds is None or 'archive-delete' in kinds else []):
        if item['age_days'] >= spaces.ARCHIVE_AGE_DAYS and not activity.reasons(Path(item['path'])):
            add('archive-delete', item['path'], '归档 ' + item['name'], item['bytes'],
                '{:.0f} 天前打的包'.format(item['age_days']), 'mtime:{:.0f}'.format(item['mtime']))


def empty_trash():
    out = subprocess.run(['/usr/bin/osascript', '-e', 'tell application "Finder" to empty trash'],
                         capture_output=True, text=True, timeout=600)
    if out.returncode:
        raise RuntimeError((out.stderr or out.stdout).strip() or '访达没有清空废纸篓')


def left_alone(plan, key, item):
    """Why the plan says to leave this item alone (its kind is set to 不管, or it touches a 「先别动」 path), or None.
    Checked when the list is built and again right before anything runs, so a stale confirmation cannot win."""
    if agent_plan.action(plan, item.get('kind') or key.split(':', 1)[0]) == 'off':
        return '计划里这一类设成了不管'
    for path in (item.get('path'), key.split(':', 1)[1]):
        keep = agent_plan.kept(plan, path) if path and str(path).startswith('/') else None
        if keep:
            return '先别动：' + (keep.get('why') or '计划里写着')
    return None


def refresh(home, cfg, settings, simctl, activity=None, now=None):
    """Rebuild the pending list (C4); ignored items return only when their object changes."""
    now = time.time() if now is None else now
    activity = activity if activity is not None else Activity.collect(settings.lease_root)
    pins, _ = StorageManager(settings)._pins(now)
    memory = load(home, ASK_STATE, {})
    pending = load(home, PENDING, empty_pending())
    if activity.errors:
        return pending   # cannot tell what is in use: keep the list as it is instead of emptying it
    items = {i['id']: i for i in pending['items']}
    fresh = candidates(home, cfg, activity, pins, simctl, now, memory)
    # The plan decides which kinds are asked about, listed quietly or left alone, and what is kept for now.
    plan = agent_plan.load(home)
    fresh = {key: dict(found, quiet=agent_plan.action(plan, found['kind']) == 'quiet') for key, found in fresh.items()
             if not left_alone(plan, key, found)}
    for key, found in fresh.items():
        old = items.get(key)
        if old and (old['state'] == 'running' or old['state'] == 'ignored' and old['fingerprint'] == found['fingerprint']):
            continue
        state = old['state'] if old and old['state'] == 'failed' else 'pending'
        items[key] = dict(found, state=state, result=old.get('result') if old else None, updated=now)
    for key in list(items):
        if key in fresh or items[key]['state'] == 'running':
            continue
        item = items[key]
        path = Path(key.split(':', 1)[1])
        # A moved task whose drive is unplugged stays listed but cannot run (ask AC-09).
        if item['kind'] == 'task-delete' and path.is_symlink() and not path.resolve().is_dir():
            items[key] = dict(item, unreachable=True, reason='外接盘未连接')
        else:
            del items[key]
    pending['items'] = sorted(items.values(), key=lambda i: (i['kind'], i['title']))
    pending['updated'] = now
    save(home, ASK_STATE, memory)
    save(home, PENDING, pending)
    return pending


def dashboard_running(ps_output):
    """The menu-bar dashboard, not the short-lived instance that runs the scheduled tick."""
    return any('ResourceDashboard' in line and '--run-tick' not in line and '--render-tab' not in line
               for line in ps_output.splitlines())


def app_running():
    listing = subprocess.run(['/bin/ps', '-axo', 'command='], capture_output=True, text=True, errors='surrogateescape')
    return listing.returncode == 0 and dashboard_running(listing.stdout)


def launch_app():
    if APP.is_dir() and not app_running():
        subprocess.run(['/usr/bin/open', '-g', '-j', str(APP)], capture_output=True)


def maybe_notify(home, pending, free_bytes, critical_bytes, now=None, launch=launch_app):
    """At most one daily notice; one more as soon as space drops below the critical line (C6)."""
    now = time.time() if now is None else now
    count = sum(1 for i in pending['items'] if i['state'] == 'pending' and not i.get('unreachable') and not i.get('quiet'))
    notify = pending.setdefault('notify', {})
    today = datetime.fromtimestamp(now).strftime('%Y-%m-%d')
    reason = None
    if free_bytes < critical_bytes:
        if notify.get('last_critical_at') is None and count:
            reason = 'critical'
            notify['last_critical_at'] = now
    else:
        notify['last_critical_at'] = None
    if reason is None and count and notify.get('last_day') != today:
        reason = 'daily'
    if reason:
        notify['last_day'] = today
        save(home, REQUEST, dict(at=now, count=count, reason=reason))
        launch()
    save(home, PENDING, pending)
    return reason


def remove_task(path, cfg):
    """Unregister its git worktrees from their main repositories, then delete what is left."""
    path = Path(path)
    real = path.resolve()
    for repo in T.repositories(real, T.skip_names(cfg)):
        if (repo / '.git').is_file():
            listed = T.git(repo, 'worktree', 'list', '--porcelain').stdout
            entries = [line[9:] for line in listed.splitlines() if line.startswith('worktree ')]
            main = Path(entries[0])
            registered = next((e for e in entries[1:] if Path(e).resolve() == repo.resolve()), None)
            if registered is None:
                raise RuntimeError('找不到 {} 的工作区登记'.format(repo.name))
            result = T.git(main, 'worktree', 'remove', registered)
            if result.returncode:
                raise RuntimeError((result.stderr or result.stdout).strip())
    if path.is_symlink():
        os.unlink(path)
    if real.exists():
        T.remove_tree(real)


def execute(home, cfg, settings, ids, simctl, activity=None, now=None, trash_emptier=empty_trash):
    """Run only the confirmed ids, each re-checked just before (ask AC-02/06/07). Only those ids are looked
    at again, so confirming takes seconds instead of a full scan of every task and simulator."""
    now = time.time() if now is None else now
    activity = activity if activity is not None else Activity.collect(settings.lease_root)
    if activity.errors:
        # Without knowing what is in use nothing is deleted, and the list stays as it is.
        return [dict(id=key, outcome='已跳过', reason='这会儿查不清哪些程序在用它，为安全没有删；稍后再试') for key in ids]
    pins, _ = StorageManager(settings)._pins(now)
    fresh = candidates(home, cfg, activity, pins, simctl, now, dict(load(home, ASK_STATE, {})), only=set(ids))
    plan = agent_plan.load(home)
    pending = load(home, PENDING, empty_pending())
    items = {i['id']: i for i in pending['items']}
    gone = set()
    results = []
    for key in ids:
        item = items.get(key)
        if item is None and key in fresh:
            # Confirmed straight from a check (not yet on the saved list): it qualifies right now.
            item = items[key] = dict(fresh[key], state='pending', result=None, updated=now)
            pending['items'].append(item)
        if item is None:
            results.append(dict(id=key, outcome='已跳过', reason='状态已变，不再满足条件或已处理'))
            continue
        alone = left_alone(plan, key, dict(item, **{k: v for k, v in fresh.get(key, {}).items() if k == 'path'}))
        if alone:
            results.append(dict(id=key, title=item['title'], outcome='已跳过', reason=alone))
            continue
        if item.get('unreachable'):
            results.append(dict(id=key, title=item['title'], outcome='已跳过', reason='外接盘未连接'))
            continue
        if item['state'] not in ('pending', 'failed', 'ignored'):
            results.append(dict(id=key, title=item['title'], outcome='已跳过', reason='已在处理'))
            continue
        if key not in fresh:
            # Rechecked just now: in use again, changed, or already gone. It leaves the list.
            gone.add(key)
            results.append(dict(id=key, title=item['title'], outcome='已跳过', reason='状态已变，不再满足条件或已处理'))
            continue
        item.update(bytes=fresh[key]['bytes'], reason=fresh[key]['reason'])
        item['state'] = 'running'
        save(home, PENDING, pending)
        target = key.split(':', 1)[1]
        try:
            if item['kind'] == 'simulator-delete':
                simctl(['delete', target])
            elif item['kind'] == 'simulator-shutdown':
                simctl(['shutdown', target])
            elif item['kind'] == 'runtime-delete':
                simctl(['runtime', 'delete', target])
            elif item['kind'] == 'trash-empty':
                trash_emptier()
            elif item['kind'] in ('installer-delete', 'device-support-delete', 'archive-delete'):
                path = Path(target)
                if path.is_dir() and not path.is_symlink():
                    T.remove_tree(path)
                else:
                    path.unlink()
            else:
                remove_task(target, cfg)
            item.update(state='done', result=None, updated=time.time())
            results.append(dict(id=key, title=item['title'], outcome='已完成', reason=item['reason'], bytes=item['bytes']))
            remember_choice(home, item, 'confirmed')
        except (RuntimeError, OSError, subprocess.SubprocessError) as error:
            item.update(state='failed', result=str(error), updated=time.time())
            results.append(dict(id=key, title=item['title'], outcome='失败', reason=str(error)))
        save(home, PENDING, pending)
    pending['items'] = [i for i in pending['items'] if i['state'] != 'done' and i['id'] not in gone]
    save(home, PENDING, pending)
    return results


def ignore(home, ids):
    pending = load(home, PENDING, empty_pending())
    for item in pending['items']:
        if item['id'] in ids and item['state'] in ('pending', 'failed'):
            item['state'] = 'ignored'
            remember_choice(home, item, 'ignored')
    save(home, PENDING, pending)
    return pending


def remember_choice(home, item, decision):
    """Each yes or no in 「等你确认」 is one of the user's decisions the agent learns from (RAW-04)."""
    try:
        agent_plan.remember(home, 'decision', item['kind'], decision=decision, path=item['id'].split(':', 1)[1],
                            source='confirm', note='{}「{}」：{}'.format('确认处理' if decision == 'confirmed' else '忽略',
                                                                    item['title'], item['reason']))
    except (OSError, ValueError):
        pass   # memory is a record; it never stops the cleanup the user asked for
