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
import auto_tier
import owners as O
import shared_caches
import spaces
import tasks as T

PENDING, REQUEST, ASK_STATE = 'pending.json', 'notify-request.json', 'ask-state.json'
AGENT_ASK_DAYS = 3          # emergency-agent: an ask of the agent's nobody answered leaves the list after this
CLOSED = 'closed-tasks'     # on the drive: what was left beside the code of a task closed at the agent's ask
LOSES = {
    'simulator-delete': '上面装的 App 和登录状态',
    'simulator-shutdown': '当前画面和 App 运行状态',
    'task-delete': '本地的构建产物和被忽略的文件；要用时得重新拉代码',
    'runtime-delete': '以后要建这个版本的模拟器得重新下载',
    'installer-delete': '安装包文件，要用时得重新下载',
    'trash-empty': '废纸篓里的全部文件，清空后不能恢复',
    'device-support-delete': '下次连这台设备调试时，Xcode 要花几分钟重新准备',
    'archive-delete': '这个版本的安装包和查崩溃用的符号文件',
    'process-stop': '它这次运行的状态，要用时得重新启动它',
    'path-delete': '里面的全部内容（缓存、日志、构建产物这类下次用到会重新生成，别的就没了）',
    'task-move': '不丢东西：整个挪到外接盘，原路径留链接；外接盘拔掉时用不了，没推送的改动只在外接盘上',
    'pin-release': '不删东西，只去掉保护标记；之后它按别的规则处理（闲置的任务会列出来问你，或按你同意过的直接收）',
    'task-shipped': '整个任务目录：代码、笔记、截图、构建产物和它的私有缓存；代码都在 GitHub 上，要用时重新拉',
    'shared-caches': '这些版本的下载缓存；以后哪个项目又用到，会重新下载',
}
SHIPPED = ('merged', 'closed')
PIN_SIZE_DAYS = 1   # how long the size of what an old pin covers is reused before du looks again
# The simulator's own processes and the viewer app mention the UDID but are not tools using it.
NOT_A_TOOL = re.compile(r'Simulator\.app/Contents/MacOS/Simulator|launchd_sim|CoreSimulator/Devices/')
# Watching, not using: an agent's screen stream or a log stream it left running (10-30: five simulators held
# for up to a day this way). Only how long such a stream has run says whether anyone still looks.
# The program itself must be the stream (a word of its own, or the last part of a path), not a path that happens
# to hold these words, like a project in /repos/baguette/stream.
PASSIVE = re.compile(r'(?i)(?:^|[\s;&|(/])baguette\s+stream(?:\s|$)|(?:^|[\s;&|(/])simctl\s+spawn\s+\S+\s+log\s+stream(?:\s|$)'
                     r'|(?:^|[\s;&|(/])[\w.-]+\s+stream\s+(?:\S+\s+)*?--udid(?:[\s=]|$)')   # any tool's screen stream of a device
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


def task_blocker(task, cfg, activity, pins, now, memory=None):
    """(why a task directory cannot even be proposed for deletion or None, its latest write, its repositories' PR
    states). Commits only a merged or closed PR still holds count as saved (tasks.work_left)."""
    reasons, latest = T.idle_check(task, activity, pins, cfg['ask']['task_idle_days'], cfg, now)
    if reasons:
        return '；'.join(reasons), latest, []
    skipped = T.skip_names(cfg)
    loose = T.loose_files(task, skipped)
    if loose:
        return '有不在代码仓库里的文件：' + '、'.join(loose[:3]), latest, []
    repos = T.repositories(task.real, skipped)
    if not repos:
        return '没有代码仓库', latest, []
    states = []
    for repo in repos:
        change, pr = T.work_left(repo, memory, now)
        if change:
            return '{}：{}'.format(repo.name, change), latest, []
        states.append(pr)
        if (repo / '.git').is_dir():
            listed = T.git(repo, 'worktree', 'list', '--porcelain')
            if listed.returncode or listed.stdout.count('worktree ') > 1:
                return '{} 还有别的工作区依赖它'.format(repo.name), latest, []
    return None, latest, states


def shipped_blocker(task, cfg, activity, pins, now, memory, hours):
    """task-shipped (11-18, the user: 「功能都上线了就把开发内容都可以删了。什么都可以删了」): (why it cannot go or None,
    latest write, PR words). Every repository's work is in the default branch or a merged or closed PR, nothing is
    left uncommitted, no other worktree hangs on it, nobody uses it, and it is untouched for `hours`. Notes and shots
    beside the code go with it."""
    reasons, latest = T.idle_check(task, activity, pins, hours / 24.0, cfg, now)
    if reasons:
        return '；'.join(reasons), latest, ''
    repos = T.repositories(task.real, T.skip_names(cfg))
    if not repos:
        return '没有代码仓库', latest, ''
    states = []
    for repo in repos:
        change, pr = T.work_left(repo, memory, now)
        if change:
            return '{}：{}'.format(repo.name, change), latest, ''
        if not (pr and pr[0] in SHIPPED):
            return '{} 的改动还没合入'.format(repo.name), latest, ''
        states.append(pr)
        if (repo / '.git').is_dir():
            listed = T.git(repo, 'worktree', 'list', '--porcelain')
            if listed.returncode or listed.stdout.count('worktree ') > 1:
                return '{} 还有别的工作区依赖它'.format(repo.name), latest, ''
    return None, latest, T.pr_words(states)


def candidates(home, cfg, activity, pins, simctl, now, memory, only=None):
    """Everything worth asking about; with `only` (ids), just those, so a confirmation rechecks in seconds."""
    found = {}
    kinds = None if only is None else {key.split(':', 1)[0] for key in only}

    def add(kind, key, title, bytes_, reason, fingerprint, **extra):
        found[kind + ':' + key] = dict(id=kind + ':' + key, kind=kind, title=title, bytes=bytes_ or 0, reason=reason,
                                       loses=LOSES[kind], fingerprint=fingerprint, **extra)

    other_candidates(home, activity, now, add, kinds=kinds)
    rules = agent_plan.load(home)['rules']
    opts = dict(pin_days=rules['pin-release']['params']['min_days'], shipped_on=rules['task-shipped']['action'] != 'off',
                shipped_auto=rules['task-shipped']['action'] == 'auto', shipped_hours=rules['task-shipped']['params']['idle_hours'])
    if (kinds is None or 'shared-caches' in kinds) and rules['shared-caches']['action'] != 'off':
        shared_caches.candidates(home, cfg, activity, now, memory, add, only)
    if kinds is not None and not kinds & {'simulator-shutdown', 'simulator-delete', 'runtime-delete'}:
        return _tasks(cfg, activity, pins, now, add, only, found, memory, opts)
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
    return _tasks(cfg, activity, pins, now, add, only, found, memory, opts)


def _tasks(cfg, activity, pins, now, add, only, found, memory=None, opts=None):
    opts = dict(dict(pin_days=7, shipped_on=False, shipped_auto=False, shipped_hours=24), **(opts or {}))
    if only is None or any(key.startswith('pin-release:') for key in only):
        _old_pins(pins, now, add, only, memory, opts['pin_days'])
    for task in T.discover(cfg):
        wanted = only is None or any(k in only for k in ('task-delete:' + str(task.path), 'task-delete:',
                                                           'task-shipped:' + str(task.path), 'task-shipped:'))
        if not task.reachable or not wanted:
            continue
        where = '（在外接盘上）' if task.linked else ''
        shipped = None
        if opts['shipped_on']:
            blocker, latest, prs = shipped_blocker(task, cfg, activity, pins, now, memory, opts['shipped_hours'])
            if blocker is None:
                shipped = (task.path.name + where, '{}，闲置 {:.0f} 小时；笔记、截图这些一起删'.format(
                    prs or '已合入', (now - latest) / 3600), 'latest:{:.0f}'.format(latest))
        # Set to 「直接收」, a shipped task goes as one; otherwise it is asked about as before (so a learned 「直接做」 on
        # idle tasks keeps working), and listed as shipped only when the old rule would not list it.
        if shipped and opts['shipped_auto']:
            add('task-shipped', str(task.path), shipped[0], T.size(task.real), shipped[1], shipped[2])
            continue
        blocker, latest, states = task_blocker(task, cfg, activity, pins, now, memory)
        if blocker is not None and shipped:
            add('task-shipped', str(task.path), shipped[0], T.size(task.real), shipped[1], shipped[2])
            continue
        if blocker is None:
            # done: every repository's work is in the default branch or in a merged or closed PR (learned 「auto」 only
            # takes these, 11-16).
            done = bool(states) and all(s and s[0] in ('merged', 'closed') for s in states)
            prs = T.pr_words([s for s in states if s])
            add('task-delete', str(task.path), task.path.name + where, T.size(task.real),
                '闲置 {:.0f} 天，改动都已提交并推送{}'.format((now - latest) / T.DAY, '，' + prs if prs else ''),
                'latest:{:.0f}'.format(latest), done=done)
    # In `only`, 「kind:」 stands for every one of that kind (learned-yes looks at all of one kind).
    return found if only is None else {key: value for key, value in found.items()
                                       if key in only or key.split(':', 1)[0] + ':' in only}


def _old_pins(pins, now, add, only, memory, days):
    """pin-release: pins made before they had a date, older than the rule's days, or on a path that is gone (11-16);
    and (11-18) any pin, however new, whose code is all in the default branch or in merged or closed PRs — the
    feature is out, so the work it held is done. The user takes them off in one go. What each covers is measured
    at most once a day."""
    sizes = memory.setdefault('pin_sizes', {}) if memory is not None else {}
    for path, value in pins.items():
        if only is not None and 'pin-release:' + path not in only and 'pin-release:' not in only:
            continue
        if not isinstance(value, dict):
            continue
        created = value.get('created') if isinstance(value.get('created'), (int, float)) else 0
        exists = os.path.lexists(path)
        old = value.get('expires') is None and (not exists or now - created >= days * T.DAY)
        shipped = '' if old or not exists else guards_shipped(path, memory, now)
        if not (old or shipped):
            continue
        hit = sizes.get(path)
        if exists and not (isinstance(hit, list) and len(hit) == 2 and now - hit[1] < PIN_SIZE_DAYS * T.DAY):
            hit = sizes[path] = [T.size(path) or 0, now]
        if shipped:
            why = '它护着的代码已经上线（{}）'.format(shipped)
        elif not exists:
            why = '标记的目录已经不在了'
        else:
            why = '标记了 {:.0f} 天、没写到期日'.format((now - created) / T.DAY) if created else '没写到期日'
        add('pin-release', path, Path(path).name, hit[0] if exists else 0,
            '{}：{}'.format(why, str(value.get('reason', ''))[:80]),
            'created:{:.0f}'.format(created), owner=str(value.get('owner', ''))[:40])


def guards_shipped(path, memory, now):
    """「PR #12 已合入、…」 when every repository at or under `path` (or the one it sits in) has its work in the default
    branch or a merged or closed PR; '' otherwise or when that cannot be told."""
    path = Path(path)
    repos = T.repositories(path, set(T.PRUNE_NAMES) | {'.git'}) if path.is_dir() else []
    if not repos:
        repos = [parent for parent in [path, *path.parents][:8] if (parent / '.git').exists()][:1]
    states = [T.pr_state(repo, memory, now) for repo in repos]
    if not states or not all(state and state[0] in SHIPPED for state in states):
        return ''
    return T.pr_words(states) or '已合入'


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
    listed = None
    for key in list(items):
        if key in fresh or items[key]['state'] == 'running':
            continue
        item = items[key]
        if item.get('by') == 'agent' and now - item.get('at', now) < AGENT_ASK_DAYS * 86400:
            # emergency-agent AC-07: the agent's own asks stay until answered, gone, or a few days old.
            if listed is None:
                listed = device_ids(simctl)
            if item['kind'].startswith('simulator') and listed is not None and key.split(':', 1)[1] not in listed:
                del items[key]
            elif item['kind'] == 'task-delete' and not Path(key.split(':', 1)[1]).exists():
                del items[key]
            elif item['kind'] == 'process-stop' and same_process(item) is None:
                del items[key]
            elif item['kind'] == 'path-delete' and not Path(key.split(':', 1)[1]).exists():
                del items[key]
            elif item['kind'] == 'pin-release' and key.split(':', 1)[1] not in pins:
                del items[key]
            continue
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


def device_ids(simctl):
    try:
        listing = json.loads(simctl(['list', 'devices', '--json']))
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
        return None
    return {d['udid'] for devices in listing.get('devices', {}).values() for d in devices}


def same_process(item, run=subprocess.run):
    """The process a process-stop ask names, if it is still that one: (command, ppid) or None."""
    pid = item['id'].split(':', 1)[1]
    try:
        out = run(['/bin/ps', '-ww', '-o', 'ppid=,lstart=,command=', '-p', pid], capture_output=True, text=True,
                  errors='surrogateescape', timeout=10).stdout.split(None, 6)
    except (OSError, subprocess.SubprocessError):
        return None
    if len(out) < 7 or ' '.join(out[1:6]) != item.get('lstart'):
        return None
    return out[6].strip(), int(out[0])


def agent_floor(home, cfg, item, activity, pins, simctl):
    """emergency-agent AC-08: an ask of the agent's is rechecked against the safety floor only, not the rules'
    waiting days. Returns (fresh fields, None) when it may go ahead, or (None, why not)."""
    kind, target = item['kind'], item['id'].split(':', 1)[1]
    if kind == 'process-stop':   # agent-investigates AC-03
        found = same_process(item)
        if found is None:
            return None, '它已经不在了，或进程号换了程序'
        command, _ = found
        if O.kind_of(command) and O.kind_of(command)[0] != 'agent' or any(word in command for word in O.NEVER):
            return None, '它是 App 本身、数据库、容器、调试器或模拟器'
        return dict(bytes=item['bytes'], reason=item['reason']), None
    if kind == 'task-move':   # 11-10: an idle task with unsaved work, to the drive with the user's yes
        task = next((t for t in T.discover(cfg) if str(t.path) == target), None)
        if task is None or not task.reachable or task.linked:
            return None, '任务目录不在内置盘上了'
        reasons = T.in_use(activity, task) + ([T.pin_reason(pins, task)] if T.pin_reason(pins, task) else [])
        if reasons:
            return None, '；'.join(sorted(set(reasons))[:3])
        return dict(bytes=T.size(task.real) or item['bytes'], reason=item['reason']), None
    if kind == 'path-delete':   # 11-10: anything else the agent found; the user said yes, the floor still holds
        path = Path(target)
        if not path.exists() and not path.is_symlink():
            return None, '已经不在了'
        if (path / '.git').exists():
            return None, '它是代码仓库'
        reasons = activity.reasons(path)
        if reasons:
            return None, '有程序正在用它：' + '；'.join(sorted(set(reasons))[:2])
        if T.pin_reason(pins, T.Task(path, path, path)):
            return None, '已被标记保护'
        if time.time() - path.lstat().st_mtime < 86400:
            return None, '最近一天还在改，先不删'
        return dict(bytes=T.size(path) or item['bytes'], reason=item['reason']), None
    if kind == 'pin-release':   # 11-16: the agent saw the work it guards is done (its PR merged, say)
        if target not in pins:
            return None, '这个保护标记已经不在了或已到期'
        return dict(bytes=item['bytes'], reason=item['reason']), None
    if kind.startswith('simulator'):
        try:
            listing = json.loads(simctl(['list', 'devices', '--json']))
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
            return None, '这会儿读不到模拟器列表'
        device = next((d for devices in listing.get('devices', {}).values() for d in devices if d['udid'] == target), None)
        if device is None:
            return None, '这台模拟器已经不在了'
        if attached(target, activity):
            return None, '有开发工具正连着它'
        data = device.get('dataPath')
        if data and T.pin_reason(pins, T.Task(Path(data), Path(data), Path(data))):
            return None, '已被标记保护'
        if kind == 'simulator-delete' and device.get('state') != 'Shutdown':
            return None, '它开着，先关机才能删'
        if kind == 'simulator-shutdown' and device.get('state') != 'Booted':
            return None, '它已经关机了'
        return dict(bytes=device.get('dataPathSize') or item['bytes'], reason=item['reason']), None
    task = next((t for t in T.discover(cfg) if str(t.path) == target), None)
    if task is None or not task.reachable:
        return None, '任务目录不在了，或外接盘没插'
    reasons = T.in_use(activity, task) + ([T.pin_reason(pins, task)] if T.pin_reason(pins, task) else [])
    if reasons:
        return None, '；'.join(sorted(set(reasons))[:3])
    skipped = T.skip_names(cfg)
    repos = T.repositories(task.real, skipped)
    if not repos:
        return None, '没有代码仓库'
    for repo in repos:
        change, _ = T.work_left(repo)
        if change:
            return None, '{}：{}'.format(repo.name, change)
        if (repo / '.git').is_dir():
            listed = T.git(repo, 'worktree', 'list', '--porcelain')
            if listed.returncode or listed.stdout.count('worktree ') > 1:
                return None, '{} 还有别的工作区依赖它'.format(repo.name)
    archive = None
    if T.loose_files(task, skipped):
        external = cfg.get('external_root')
        if not (external and auto_tier.volume_ready(external)):
            return None, '里面有笔记、截图这些不在代码仓库里的文件，外接盘没插，先不删'
        archive = Path(external) / CLOSED / task.path.name
        if archive.exists():
            return None, '外接盘的「收尾的任务」里已有同名目录'
    return dict(bytes=T.size(task.real) or item['bytes'], reason=item['reason'], archive=str(archive) if archive else None), None


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


def remove_task(path, cfg, archive=None):
    """Unregister its git worktrees from their main repositories, then delete what is left. With `archive`
    (emergency-agent AC-08), what is left (notes, shots, logs) goes there instead of being deleted."""
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
            if result.returncode and 'submodule' in (result.stderr or '') and T.work_left(repo)[0] is None:
                # git will not remove a worktree that has submodules unless forced; this one is clean and pushed.
                result = T.git(main, 'worktree', 'remove', '--force', registered)
            if result.returncode:
                raise RuntimeError((result.stderr or result.stdout).strip())
    if path.is_symlink():
        os.unlink(path)
    if real.exists() and archive:
        Path(archive).parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(real), str(archive))
    elif real.exists():
        T.remove_tree(real)


def drop_task_cache(settings, name, activity):
    """A retired task's own cache beside the shared ones (<cache root>/tasks/<task name>) goes with it (11-18)."""
    for root in settings.cache_roots:
        folder = Path(root) / 'tasks' / name
        if name and folder.is_dir() and not folder.is_symlink() and not activity.reasons(folder):
            try:
                T.remove_tree(folder)
            except OSError:
                pass   # the task itself is gone; its cache waits for the next look


def execute(home, cfg, settings, ids, simctl, activity=None, now=None, trash_emptier=empty_trash, source='confirm'):
    """Run only the confirmed ids, each re-checked just before (ask AC-02/06/07). Only those ids are looked
    at again, so confirming takes seconds instead of a full scan of every task and simulator. `source` 'auto':
    run by a rule the user's yes made (learned-yes), remembered as that and not as one more yes of theirs."""
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
        if item.get('by') == 'agent' and key not in fresh:
            checked, why_not = agent_floor(home, cfg, item, activity, pins, simctl)
            if why_not:
                results.append(dict(id=key, title=item['title'], outcome='已跳过', reason=why_not))
                continue
            fresh[key] = dict(item, **checked)
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
            elif item['kind'] == 'pin-release':
                StorageManager(settings).release_pin(target)
            elif item['kind'] == 'shared-caches':
                skipped = shared_caches.remove(fresh[key]['paths'], activity)
                if skipped and len(skipped) == len(fresh[key]['paths']):
                    raise RuntimeError('都有程序在用，没有删：' + skipped[0])
            elif item['kind'] == 'task-move':
                import emergency   # here only: it imports this module
                skipped = emergency.move_task(home, cfg, settings, target, activity, pins, unsaved_ok=True)
                if skipped:
                    raise RuntimeError(skipped)
            elif item['kind'] == 'process-stop':
                import emergency   # here only: it imports this module
                skipped = emergency.stop_tree(emergency.process_table(), int(target), item['lstart'])
                if skipped:
                    raise RuntimeError(skipped)
            elif item['kind'] in ('installer-delete', 'device-support-delete', 'archive-delete', 'path-delete'):
                path = Path(target)
                if path.is_dir() and not path.is_symlink():
                    T.remove_tree(path)
                else:
                    path.unlink()
            else:
                remove_task(target, cfg, archive=fresh[key].get('archive'))
                drop_task_cache(settings, Path(target).name, activity)
            item.update(state='done', result=None, updated=time.time())
            results.append(dict(id=key, title=item['title'], outcome='已完成', reason=item['reason'], bytes=item['bytes']))
            remember_choice(home, item, 'confirmed', source)
        except (RuntimeError, OSError, subprocess.SubprocessError) as error:
            item.update(state='failed', result=str(error), updated=time.time())
            results.append(dict(id=key, title=item['title'], outcome='失败', reason=str(error)))
        save(home, PENDING, pending)
    pending['items'] = [i for i in pending['items'] if i['state'] != 'done' and i['id'] not in gone]
    save(home, PENDING, pending)
    if any(r['outcome'] == '已完成' and r['id'].startswith(('task-delete:', 'task-shipped:')) for r in results):
        try:   # build-slots: the retired tasks' build slots go now, not at the next cache round
            if agent_plan.action(plan, 'build-slots') == 'clean':
                StorageManager(settings).reclaim_slots(activity_factory=lambda: activity, now=now)
        except (OSError, ValueError, RuntimeError):
            pass
    return results


def ignore(home, ids):
    pending = load(home, PENDING, empty_pending())
    for item in pending['items']:
        if item['id'] in ids and item['state'] in ('pending', 'failed'):
            item['state'] = 'ignored'
            remember_choice(home, item, 'ignored')
    save(home, PENDING, pending)
    return pending


def remember_choice(home, item, decision, source='confirm'):
    """Each yes or no in 「等你确认」 is one of the user's decisions the agent learns from (RAW-04)."""
    what = '照你同意过的直接处理' if source == 'auto' else '确认处理' if decision == 'confirmed' else '忽略'
    try:
        agent_plan.remember(home, 'decision', item['kind'], decision=decision, path=item['id'].split(':', 1)[1],
                            source=source, note='{}「{}」：{}'.format(what, item['title'], item['reason']))
    except (OSError, ValueError):
        pass   # memory is a record; it never stops the cleanup the user asked for


AUTO_PARAM = {'simulator-delete': ('min_days', 'simulator_unused_days'), 'simulator-shutdown': ('min_hours', 'simulator_unattached_hours'),
              'task-delete': ('min_days', 'task_idle_days')}


def auto_run(home, cfg, settings, simctl, kind, now=None, short_bytes=None, activity=None):
    """learned-yes: what a 「你同意过，直接做」 rule covers is done without asking, each rechecked like a yes in
    「等你确认」: shut-down simulators unused at least the rule's days (biggest first, only as many as the disk is
    short of), or open ones nobody connected to for its hours. Said once afterwards. Returns what was done."""
    now = time.time() if now is None else now
    rule = agent_plan.load(home)['rules'].get(kind) or {}
    name, waits = AUTO_PARAM[kind]
    least = (rule.get('params') or {}).get(name) or 0
    if rule.get('action') != 'auto' or least <= 0 or short_bytes is not None and short_bytes <= 0:
        return []
    lowered = dict(cfg, ask=dict(cfg['ask'], **{waits: least}))
    activity = activity if activity is not None else Activity.collect(settings.lease_root)
    if activity.errors:
        return []
    pins, _ = StorageManager(settings)._pins(now)
    found = candidates(home, lowered, activity, pins, simctl, now, dict(load(home, ASK_STATE, {})), only={kind + ':'})
    plan = agent_plan.load(home)
    picked, total = [], 0
    # A task goes unasked only when its work is in the default branch or a merged or closed PR (11-16).
    found = {k: f for k, f in found.items() if f['kind'] == kind and not left_alone(plan, k, f)
             and (kind != 'task-delete' or f.get('done'))}
    for item in sorted(found.values(), key=lambda f: -f['bytes']):
        if short_bytes is not None and total >= short_bytes:
            break
        picked.append(item['id'])
        total += item['bytes']
    if not picked:
        return []
    done = [r for r in execute(home, lowered, settings, picked, simctl, activity, now, source='auto') if r['outcome'] == '已完成']
    if done:
        names = '、'.join(r['title'] for r in done[:4]) + (' 等' if len(done) > 4 else '')
        if kind == 'simulator-delete':
            agent_plan.notice(home, '按你同意过的，删了 {} 个模拟器'.format(len(done)),
                              '关机 {:g} 天以上没开过，腾出约 {:.0f} GB：{}'.format(least, sum(r.get('bytes') or 0 for r in done) / GIB, names))
        elif kind == 'task-delete':
            agent_plan.notice(home, '按你同意过的，收了 {} 个做完的任务'.format(len(done)),
                              '闲置 {:g} 天以上，改动都已推送，PR 已合入或关闭；腾出约 {:.0f} GB：{}。要用时重新拉代码。'.format(
                                  least, sum(r.get('bytes') or 0 for r in done) / GIB, names))
        else:
            agent_plan.notice(home, '内存紧，按你同意过的关了 {} 个模拟器'.format(len(done)),
                              '开着 {:g} 小时以上没有开发工具连着：{}。数据都在，要用再开。'.format(least, names), tab='memory')
    return done


def cap_simulators(home, cfg, settings, simctl, now=None, activity=None):
    """simulator-cap (11-10, the user: 「都改」): more simulators open than the cap, the extra ones nobody has been
    connected to for its minutes go, the longest unused first; each rechecked like a yes in 「等你确认」. Their data
    stays: only what runs now stops. Returns what was shut."""
    now = time.time() if now is None else now
    rule = agent_plan.load(home)['rules'].get('simulator-cap') or {}
    if rule.get('action') != 'auto':
        return []
    most, minutes = rule['params']['max_booted'], rule['params']['quiet_minutes']
    try:
        listing = json.loads(simctl(['list', 'devices', '--json']))
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
        return []
    booted = [d for devices in listing.get('devices', {}).values() for d in devices if d.get('state') == 'Booted']
    if len(booted) <= most:
        return []
    activity = activity if activity is not None else Activity.collect(settings.lease_root)
    if activity.errors:
        return []
    stream_hours = agent_plan.load(home)['rules']['simulator-shutdown']['params'].get('stream_hours')
    ages = process_ages() if stream_hours else {}
    memory = load(home, ASK_STATE, {})
    since = memory.setdefault('unattached_since', {})
    quiet = []
    for device in booted:
        if attached(device['udid'], activity, stream_hours, ages):
            since.pop(device['udid'], None)
        elif now - since.setdefault(device['udid'], now) >= minutes * 60:
            quiet.append((since[device['udid']], device))
    save(home, ASK_STATE, memory)
    picked = ['simulator-shutdown:' + d['udid'] for _, d in sorted(quiet, key=lambda q: q[0])][:len(booted) - most]
    if not picked:
        return []
    lowered = dict(cfg, ask=dict(cfg['ask'], simulator_unattached_hours=minutes / 60))
    done = [r for r in execute(home, lowered, settings, picked, simctl, activity, now, source='auto') if r['outcome'] == '已完成']
    if done:
        agent_plan.notice(home, '同时开着 {} 台模拟器，关了 {} 台没人连的'.format(len(booted), len(done)),
                          '{}：没人连满 {:g} 分钟。数据都在，要用再开。'.format('、'.join(r['title'] for r in done), minutes), tab='memory')
    return done


POLICY_WORDS = {'task-shipped': ('按你定的，收了 {} 个已上线的任务', '代码都在 GitHub 上，要用时重新拉；腾出约 {:.0f} GB：{}'),
                'shared-caches': ('按你定的，清了共享缓存里 {} 类现在的项目都不用的版本', '以后哪个项目又用到会重新下载；腾出约 {:.0f} GB：{}')}


def run_policy(home, cfg, settings, simctl, kind, now=None, activity=None):
    """A kind the user set to 「直接做」 as a standing rule (11-18: task-shipped, shared-caches): all of it that
    qualifies goes each round, whatever the free space, each rechecked like a yes in 「等你确认」. Said once."""
    now = time.time() if now is None else now
    if (agent_plan.load(home)['rules'].get(kind) or {}).get('action') != 'auto':
        return []
    activity = activity if activity is not None else Activity.collect(settings.lease_root)
    if activity.errors:
        return []
    pins, _ = StorageManager(settings)._pins(now)
    found = candidates(home, cfg, activity, pins, simctl, now, dict(load(home, ASK_STATE, {})), only={kind + ':'})
    plan = agent_plan.load(home)
    picked = [k for k, f in found.items() if f['kind'] == kind and not left_alone(plan, k, f)]
    if not picked:
        return []
    done = [r for r in execute(home, cfg, settings, picked, simctl, activity, now, source='auto') if r['outcome'] == '已完成']
    if done:
        title, body = POLICY_WORDS[kind]
        names = '、'.join(r['title'] for r in done[:4]) + (' 等' if len(done) > 4 else '')
        agent_plan.notice(home, title.format(len(done)), body.format(sum(r.get('bytes') or 0 for r in done) / GIB, names))
    return done
