"""proactive-watch: every minute, see the squeeze coming and act on what is plainly safe, before anyone asks.

On 10-31 four 4 GB type checks ran at once and the Mac froze. Nothing warned, the per-minute memory guard gave up
on slow process lookups, the agent came late and broke, and by the time the user said 「暂停排队」 the checks had
finished (RAW-35～37). So, each minute and with cheap reads only:
- the squeeze is judged from the trend (pressure that stays up, swap that climbs), not only from 「严重」;
- leftovers nobody runs any more are stopped, and heavy jobs that crowd each other are queued (paused, then let
  go one at a time) — nothing is deleted and every pause is let go again;
- one notice per squeeze says what was done and who holds the memory;
- whatever still needs judgement goes to the agent (emergency.py).
"""
import fcntl
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import time

from storage import GIB, atomic_json
import agent_plan
import emergency as E
import tiers_config

STATE = 'watch.json'
KEEP_SECONDS = 3600
WARN_MINUTES = 3              # 「警告」 this many minutes in a row is a squeeze (AC-01)
SWAP_SHARE, SWAP_CLIMB_GIB, SWAP_WINDOW = 0.6, 2.0, 600
NORMAL_TO_RELEASE = 2         # minutes of normal pressure before every queued job goes again (AC-03)
PAUSE_LIMIT = 20 * 60         # nobody waits longer than this
HEAVY_BYTES = GIB // 2        # what is still in memory; a job the Mac already swapped out shows less
HELPER_IDLE_HOURS = 6         # an agent helper this long without CPU is abandoned; waiting on a model uses next to none
                              # for minutes, so one quiet minute would not be enough
BIG_SESSION_BYTES = 300 * 2 ** 20
SESSION_HOURS = 3
STREAMS = ('baguette stream', ' log stream')
# Build, type-check, bundle and test tools: heavy for a while, then done. Never an App, a database or a debugger.
HEAVY = re.compile(r'node_modules/\.bin/(?:tsc|vue-tsc|vitest|jest|webpack|next|vite|esbuild|eslint|tsx)\b'
                   r'|(?:^|/)(?:swift-frontend|swiftc|swift-build|xcodebuild|clang|clang\+\+|cargo|rustc|kotlinc|javac)(?:\s|$)'
                   r'|GradleWorkerMain|KotlinCompileDaemon|(?:^|/)go (?:build|test|vet)\b')


def state_root(home):
    return Path(home) / '.local/state/dev-resource-guard'


def load(home):
    try:
        return json.loads((state_root(home) / STATE).read_text())
    except (OSError, ValueError):
        return {}


def save(home, state):
    state_root(home).mkdir(parents=True, exist_ok=True)
    atomic_json(state_root(home) / STATE, state)


def squeeze(samples, now):
    """Why memory counts as tight now, or None (AC-01). `samples` hold one reading a minute, oldest first."""
    if not samples:
        return None
    last = samples[-1]
    if last['pressure'] >= 4:
        return '内存压力「严重」'
    recent = [s for s in samples if now - s['at'] <= WARN_MINUTES * 60 + 30]
    if len(recent) >= WARN_MINUTES and all(s['pressure'] >= 2 for s in recent[-WARN_MINUTES:]):
        return '内存压力「警告」已经 {} 分钟'.format(WARN_MINUTES)
    total = last.get('swap_total') or 0
    if total and last['swap_used'] >= SWAP_SHARE * total:
        before = [s for s in samples if now - s['at'] >= SWAP_WINDOW - 60]
        if before and last['swap_used'] - before[-1]['swap_used'] >= SWAP_CLIMB_GIB:
            return 'swap 用了 {:.0f} GB，10 分钟里涨了 {:.0f} GB'.format(last['swap_used'], last['swap_used'] - before[-1]['swap_used'])
    return None


def reading(home, run=subprocess.run):
    pressure = E.memory_pressure(run)
    swap = E.swap_usage(run) or {}
    try:
        free = shutil.disk_usage(str(home)).free / GIB
    except OSError:
        free = None
    return dict(pressure=pressure, swap_used=swap.get('used_gib', 0.0), swap_total=swap.get('total_gib', 0.0), free_gib=free)


def alive_same(table, entry):
    row = table.get(entry['pid'])
    return row if row and row['lstart'] == entry['lstart'] else None


def signal_one(kill, pid, sig):
    try:
        kill(pid, sig)
        return True
    except OSError:
        return False


def release(state, table, kill, now, everything=False):
    """Let queued jobs go again (AC-03): when told to, after the time limit, or when their parent is gone; and one
    more whenever nothing queued is running."""
    kept, let_go = [], []
    for entry in state.get('paused', []):
        row = alive_same(table, entry)
        if row is None:
            continue   # finished, or someone else ended it
        if everything or now - entry['since'] >= PAUSE_LIMIT or row['ppid'] != entry['ppid'] or entry['ppid'] not in table:
            signal_one(kill, entry['pid'], signal.SIGCONT)
            let_go.append(entry)
            # Let go for good: waiting its turn once is enough, it is never queued again.
            state.setdefault('freed', []).append(dict(pid=entry['pid'], lstart=entry['lstart']))
        else:
            kept.append(entry)
    leader = state.get('leader')
    if kept and (leader is None or alive_same(table, leader) is None):
        nxt = kept.pop(0)   # the one queued first goes next
        signal_one(kill, nxt['pid'], signal.SIGCONT)
        let_go.append(nxt)
        state['leader'] = dict(pid=nxt['pid'], lstart=nxt['lstart'])
    state['paused'] = kept
    if not kept and (leader is None or alive_same(table, leader) is None):
        state.pop('leader', None)
    state['freed'] = [f for f in state.get('freed', []) if alive_same(table, f)]
    return let_go


def heavy_jobs(table, own=()):
    rows = [r for r in table.values() if r['rss'] >= HEAVY_BYTES and HEAVY.search(r['command'])
            and r['pid'] not in own and not any(word in r['command'] for word in E.NEVER) and '.app/Contents/MacOS/' not in r['command']]
    return sorted(rows, key=lambda r: -r['cpu'])


def queue(state, table, kill, now, own=()):
    """Keep the job furthest along running, pause the rest (AC-03). Returns what was paused."""
    paused_pids = {p['pid'] for p in state.get('paused', [])}
    freed = {(f['pid'], f['lstart']) for f in state.get('freed', [])}
    running = [r for r in heavy_jobs(table, own) if r['pid'] not in paused_pids]
    if len(running) < 2:
        return []
    # The ones already let go come first: they keep running; only the others can wait.
    running = sorted(running, key=lambda r: ((r['pid'], r['lstart']) not in freed, -r['cpu']))
    leader = running[0]
    rest = [r for r in running[1:] if (r['pid'], r['lstart']) not in freed]
    if state.get('leader') and alive_same(table, state['leader']) and state['leader']['pid'] in {r['pid'] for r in running}:
        leader = table[state['leader']['pid']]
        rest = [r for r in running if r['pid'] != leader['pid'] and (r['pid'], r['lstart']) not in freed]
    state['leader'] = dict(pid=leader['pid'], lstart=leader['lstart'])
    done = []
    for row in rest:
        if signal_one(kill, row['pid'], signal.SIGSTOP):
            entry = dict(pid=row['pid'], lstart=row['lstart'], ppid=row['ppid'], since=now, gib=round(row['rss'] / GIB, 1),
                         name=short_name(row['command']))
            state.setdefault('paused', []).append(entry)
            done.append(entry)
    return done


def observe(state, found, now):
    """Every minute, tight or not: since when each leftover has used no CPU (a little more resets the clock)."""
    seen, fresh = state.get('seen', {}), {}
    for orphan in found:
        key, before = str(orphan['pid']), state.get('seen', {}).get(str(orphan['pid']))
        if before and before['lstart'] == orphan['lstart'] and orphan['cpu_seconds'] - before['cpu'] <= E.ORPHAN_CPU_SECONDS:
            fresh[key] = before
        else:
            fresh[key] = dict(lstart=orphan['lstart'], cpu=orphan['cpu_seconds'], since=now)
    state['seen'] = fresh


def leftovers(state, found, table, kill, sleep, now, managed):
    """Stop what nobody runs any more (AC-02): a day-old screen or log stream at once; an agent helper only after
    hours without any CPU."""
    stopped = []
    for orphan in found:
        command = orphan['command']
        stream = any(word in command for word in STREAMS)
        helper = any(word in command for word in E.AGENT_HINTS)
        quiet = state.get('seen', {}).get(str(orphan['pid']))
        if stream:
            entry = dict(pid=orphan['pid'], lstart=orphan['lstart'], cpu_seconds=float('inf'))   # streams always burn CPU
        elif helper and quiet and now - quiet['since'] >= HELPER_IDLE_HOURS * 3600:
            entry = dict(pid=orphan['pid'], lstart=orphan['lstart'], cpu_seconds=quiet['cpu'])
        else:
            continue   # a dev server in a task folder, or a helper that may still be waiting on a model
        if E.stop_orphan(entry, table, kill=kill, sleep=sleep, managed=managed) is None:
            stopped.append(dict(pid=orphan['pid'], name=short_name(command), mb=orphan['rss_mb']))
    return stopped


FRIENDLY = {'claude': 'Claude Code', 'codex': 'Codex 后台', 'node': 'Node 程序', 'Python': 'Python 程序'}


def short_name(command):
    """What people would call it: the App's name, or the tool's (tsc, swift-frontend…)."""
    if 'CoreSimulator' in command or 'SimulatorRuntime' in command or 'launchd_sim' in command:
        return 'iOS 模拟器'
    app = re.search(r'/([^/]+)\.app/Contents/', command)
    if app:
        return app.group(1)
    tool = re.search(r'node_modules/\.bin/([\w.-]+)', command)
    if tool:
        return tool.group(1)
    first = command.split()[0] if command.split() else command
    return FRIENDLY.get(Path(first).name, Path(first).name)


def top_users(table, limit=3):
    groups = {}
    for row in table.values():
        name = short_name(row['command'])
        groups[name] = groups.get(name, 0) + row['rss']
    return [dict(name=n, gib=round(b / GIB, 1)) for n, b in sorted(groups.items(), key=lambda kv: -kv[1])[:limit] if b >= GIB / 4]


def big_sessions(home, now):
    """Claude and Codex conversations being written to that grew very large (AC-04)."""
    home = Path(home)
    found = []
    for agent, pattern in (('Claude', home.glob('.claude/projects/*/*.jsonl')), ('Codex', home.glob('.codex/sessions/*/*/*/*.jsonl'))):
        for path in pattern:
            try:
                info = path.stat()
            except OSError:
                continue
            if info.st_size >= BIG_SESSION_BYTES and now - info.st_mtime <= SESSION_HOURS * 3600:
                found.append(dict(agent=agent, mb=round(info.st_size / 2 ** 20)))
    return sorted(found, key=lambda s: -s['mb'])[:2]


def words(stopped, paused, users, sessions):
    parts = []
    if stopped:
        parts.append('关掉了 {} 个没人管的残留后台（{}）'.format(len(stopped), '、'.join(sorted({s['name'] for s in stopped}))))
    if paused:
        parts.append('{} 个重活同时在跑，已让 {} 个排队（{}），一个跑完再放下一个'.format(
            len(paused) + 1, len(paused), '、'.join(sorted({p['name'] for p in paused}))))
    if users:
        parts.append('现在最占内存的是 ' + '、'.join('{} {:.1f} GB'.format(u['name'], u['gib']) for u in users))
    for session in sessions:
        parts.append('{} 有个会话已经 {} MB，开个新会话会顺很多'.format(session['agent'], session['mb']))
    return '；'.join(parts) + '。' if parts else ''


def tick(home, now=None, run=subprocess.run, table=None, kill=os.kill, sleep=time.sleep, cwd_of=E.cwds, managed=None,
         notice=agent_plan.notice, emergency_tick=None):
    """One minute of watching (AC-01～05). Never raises for a slow Mac: what cannot be read is skipped this minute."""
    now = time.time() if now is None else now
    home = Path(home)
    cfg = tiers_config.load(home)
    state = load(home)
    if table is None:
        try:
            table = E.process_table()
        except (OSError, subprocess.SubprocessError):
            table = None
    if not cfg.get('proactive_watch', True):
        if table is not None and state.get('paused'):
            release(state, table, kill, now, everything=True)
        state['paused'] = []
        save(home, state)
        return dict(on=False)
    sample = dict(at=now, **reading(home, run))
    state['samples'] = [s for s in state.get('samples', []) if now - s['at'] <= KEEP_SECONDS] + [sample]
    why = squeeze(state['samples'], now)
    result = dict(on=True, tight=why, stopped=[], paused=[], released=[])
    if table is not None:
        calm = [s for s in state['samples'] if now - s['at'] <= NORMAL_TO_RELEASE * 60 + 30]
        everything = why is None and len(calm) >= NORMAL_TO_RELEASE and all(s['pressure'] <= 1 for s in calm[-NORMAL_TO_RELEASE:])
        result['released'] = release(state, table, kill, now, everything=everything)
    found = []
    if table is not None:
        managed = E.launchd_jobs(run) if managed is None else managed
        if managed is not None:
            try:
                found = E.orphans([Path(r['path']) for r in cfg['task_roots']], table, cwd_of, own=[os.getpid()], managed=managed)
            except (OSError, subprocess.SubprocessError):
                found = []
            observe(state, found, now)
    if why and table is not None:
        result['stopped'] = leftovers(state, found, table, kill, sleep, now, managed) if found else []
        result['paused'] = queue(state, table, kill, now, own=[os.getpid()])
        if result['stopped'] or result['paused']:
            acted = dict(at=now, stopped=len(result['stopped']), paused=len(result['paused']))
            state['acted'] = (state.get('acted', []) + [acted])[-20:]
        if not state.get('episode'):
            state['episode'] = dict(since=now, why=why)
            body = words(result['stopped'], result['paused'], top_users(table), big_sessions(home, now))
            notice(home, '内存在变紧：' + why, body or '正在盯着，暂时没有能安全处理的。', tab='memory')
        elif result['stopped'] or result['paused']:
            notice(home, '内存还紧，又处理了一些', words(result['stopped'], result['paused'], [], []), tab='memory')
    elif why is None:
        state.pop('episode', None)
    save(home, state)
    if emergency_tick is not None:
        result['emergency'] = emergency_tick(home, now)
    return result


def emergency_after(home, now):
    """AC-05: whatever is still tight after the minute's own work goes to the agent, by the emergency rules."""
    import portable_config
    settings = portable_config.configure(home)
    cfg = tiers_config.load(home, owned=[settings.cache_roots[0], settings.state_root, state_root(home)])
    lock = state_root(home) / 'emergency.lock'
    with lock.open('a') as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return None
        return E.tick(home, cfg, settings, now)


def status(home, now=None):
    now = time.time() if now is None else now
    state = load(home)
    last = (state.get('samples') or [{}])[-1]
    return dict(on=tiers_config.load(home).get('proactive_watch', True), tight=squeeze(state.get('samples', []), now),
                pressure={1: '正常', 2: '警告', 4: '严重'}.get(last.get('pressure'), '未知'), swap_gib=last.get('swap_used'),
                queued=[dict(name=p['name'], gib=p['gib'], minutes=round((now - p['since']) / 60)) for p in state.get('paused', [])],
                acted=state.get('acted', [])[-5:])
