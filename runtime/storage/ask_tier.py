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
import tasks as T

PENDING, REQUEST, ASK_STATE = 'pending.json', 'notify-request.json', 'ask-state.json'
LOSES = {
    'simulator-delete': '上面装的 App 和登录状态',
    'simulator-shutdown': '当前画面和 App 运行状态',
    'task-delete': '本地的构建产物和被忽略的文件；要用时得重新拉代码',
    'runtime-delete': '以后要建这个版本的模拟器得重新下载',
}
# The simulator's own processes and the viewer app mention the UDID but are not tools using it.
NOT_A_TOOL = re.compile(r'Simulator\.app/Contents/MacOS/Simulator|launchd_sim|CoreSimulator/Devices/')
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
    """lastBootedAt is often empty; device.plist changes on every boot and shutdown."""
    times = [parse_time(device.get('lastBootedAt'))]
    data = device.get('dataPath')
    if data:
        for path in (Path(data), Path(data).parent / 'device.plist'):
            try:
                times.append(path.stat().st_mtime)
            except OSError:
                pass
    times = [t for t in times if t]
    return max(times) if times else None


def attached(udid, activity):
    return (any(udid in command and not NOT_A_TOOL.search(command) for _, command in activity.commands) or
            any(udid in reference for reference in activity.lease_refs))


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


def candidates(home, cfg, activity, pins, simctl, now, memory):
    found = {}

    def add(kind, key, title, bytes_, reason, fingerprint, **extra):
        found[kind + ':' + key] = dict(id=kind + ':' + key, kind=kind, title=title, bytes=bytes_ or 0, reason=reason,
                                       loses=LOSES[kind], fingerprint=fingerprint, **extra)

    listing = json.loads(simctl(['list', 'devices', '--json']))
    used_runtimes, booted = set(), set()
    unattached = memory.setdefault('unattached_since', {})
    for runtime, devices in listing.get('devices', {}).items():
        if devices:
            used_runtimes.add(runtime)
        for device in devices:
            if device.get('isAvailable') is False:
                continue   # the auto tier removes unavailable ones
            udid = device['udid']
            title = '{}（{}）'.format(device.get('name', udid), runtime_label(runtime))
            data = device.get('dataPath')
            protected = attached(udid, activity) or (data and T.pin_reason(pins, T.Task(Path(data), Path(data), Path(data))))
            if device.get('state') == 'Booted':
                booted.add(udid)
                if protected:
                    unattached.pop(udid, None)
                    continue
                since = unattached.setdefault(udid, now)
                hours = (now - since) / 3600
                if hours >= cfg['ask']['simulator_unattached_hours']:
                    add('simulator-shutdown', udid, title, 0, '开着 {:.0f} 小时没有开发工具连着'.format(hours), 'since:{:.0f}'.format(since))
            elif device.get('state') == 'Shutdown' and not protected:
                last = last_used(device)
                if last is not None and now - last >= cfg['ask']['simulator_unused_days'] * T.DAY:
                    add('simulator-delete', udid, title, device.get('dataPathSize') or (T.size(data) if data else 0),
                        '已关机 {:.0f} 天没开过'.format((now - last) / T.DAY), 'last:{:.0f}'.format(last))
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
    for task in T.discover(cfg):
        if not task.reachable:
            continue
        blocker, latest = task_blocker(task, cfg, activity, pins, now)
        if blocker is None:
            where = '（在外接盘上）' if task.linked else ''
            add('task-delete', str(task.path), task.path.name + where, T.size(task.real),
                '闲置 {:.0f} 天，改动都已提交并推送'.format((now - latest) / T.DAY), 'latest:{:.0f}'.format(latest))
    return found


def refresh(home, cfg, settings, simctl, activity=None, now=None):
    """Rebuild the pending list (C4); ignored items return only when their object changes."""
    now = time.time() if now is None else now
    activity = activity if activity is not None else Activity.collect(settings.lease_root)
    pins, _ = StorageManager(settings)._pins(now)
    memory = load(home, ASK_STATE, {})
    pending = load(home, PENDING, empty_pending())
    items = {i['id']: i for i in pending['items']}
    fresh = {} if activity.errors else candidates(home, cfg, activity, pins, simctl, now, memory)
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


def app_running():
    return subprocess.run(['/usr/bin/pgrep', '-f', 'ResourceDashboard'], capture_output=True).returncode == 0


def launch_app():
    if APP.is_dir() and not app_running():
        subprocess.run(['/usr/bin/open', '-g', '-j', str(APP)], capture_output=True)


def maybe_notify(home, pending, free_bytes, critical_bytes, now=None, launch=launch_app):
    """At most one daily notice; one more as soon as space drops below the critical line (C6)."""
    now = time.time() if now is None else now
    count = sum(1 for i in pending['items'] if i['state'] == 'pending' and not i.get('unreachable'))
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


def execute(home, cfg, settings, ids, simctl, activity=None, now=None):
    """Run only the confirmed ids, re-checking each against a fresh scan (ask AC-02/06/07)."""
    now = time.time() if now is None else now
    pending = refresh(home, cfg, settings, simctl, activity, now)
    items = {i['id']: i for i in pending['items']}
    results = []
    for key in ids:
        item = items.get(key)
        if item is None:
            results.append(dict(id=key, outcome='已跳过', reason='状态已变，不再满足条件或已处理'))
            continue
        if item.get('unreachable'):
            results.append(dict(id=key, title=item['title'], outcome='已跳过', reason='外接盘未连接'))
            continue
        if item['state'] not in ('pending', 'failed', 'ignored'):
            results.append(dict(id=key, title=item['title'], outcome='已跳过', reason='已在处理'))
            continue
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
            else:
                remove_task(target, cfg)
            item.update(state='done', result=None, updated=time.time())
            results.append(dict(id=key, title=item['title'], outcome='已完成', reason=item['reason'], bytes=item['bytes']))
        except (RuntimeError, OSError, subprocess.SubprocessError) as error:
            item.update(state='failed', result=str(error), updated=time.time())
            results.append(dict(id=key, title=item['title'], outcome='失败', reason=str(error)))
        save(home, PENDING, pending)
    pending['items'] = [i for i in pending['items'] if i['state'] != 'done']
    save(home, PENDING, pending)
    return results


def ignore(home, ids):
    pending = load(home, PENDING, empty_pending())
    for item in pending['items']:
        if item['id'] in ids and item['state'] in ('pending', 'failed'):
            item['state'] = 'ignored'
    save(home, PENDING, pending)
    return pending
