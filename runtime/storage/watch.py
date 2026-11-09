"""proactive-watch: every minute, see the squeeze coming and act on what is plainly safe, before anyone asks.

On 10-31 four 4 GB type checks ran at once and the Mac froze. Nothing warned, the per-minute memory guard gave up
on slow process lookups, the agent came late and broke, and by the time the user said 「暂停排队」 the checks had
finished (RAW-35～37). So, each minute and with cheap reads only:
- the squeeze is judged from the trend (pressure that stays up, swap that climbs), not only from 「严重」;
- companions whose owner is gone or still are stopped (know-who AC-03), and heavy jobs that crowd each other are
  queued (paused, then let go one at a time) — nothing is deleted and every pause is let go again;
- one notice per squeeze says what was done and whose it is that holds the memory (know-who AC-01, AC-02);
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
import owners as O
import tiers_config

STATE = 'watch.json'
KEEP_SECONDS = 3600
WARN_MINUTES = 3              # 「警告」 this many minutes in a row is a squeeze (AC-01)
SWAP_SHARE, SWAP_CLIMB_GIB, SWAP_WINDOW = 0.6, 2.0, 600
# 「快紧了」 comes before any of that (11-05: by the kernel's 「警告」 the Mac was already too slow to use): apps,
# the kernel and the compressor holding this share of the memory, swap up this much in 10 minutes, or one minute of 「警告」.
USED_SHARE, SWAP_EARLY_GIB = 0.8, 1.0
NORMAL_TO_RELEASE = 2         # minutes of normal pressure before every queued job goes again (AC-03)
CALM_TO_END = 10              # minutes of no squeeze before a squeeze counts as over (pressure flickers: 4 notices in 6 minutes on 10-31)
NOTICE_GAP = 30 * 60          # at most one notice in this long
PAUSE_LIMIT = 20 * 60         # nobody waits longer than this
HEAVY_BYTES = GIB // 2        # what is still in memory; a job the Mac already swapped out shows less
SWAPPED_GIB = 2.0             # this much in swap and ps no longer tells what each one holds (know-who AC-07)
HELPER_IDLE_HOURS = O.AGENT_QUIET_HOURS   # a server this long without CPU of its own is abandoned
SERVER_IDLE_HOURS = 12        # a dev server nobody connected to, with no CPU of its own, this long after its starter left
BIG_SESSION_BYTES = 300 * 2 ** 20
SESSION_HOURS = 3
# Build, type-check, bundle and test tools: heavy for a while, then done. Never an App, a database or a debugger.
HEAVY = re.compile(r'node_modules/\.bin/(?:tsc|vue-tsc|vitest|jest|webpack|next|vite|esbuild|eslint|tsx)\b|^node \((?:vitest|jest)\b'
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


def soon(samples, now):
    """Why memory is getting tight, before the Mac slows down, or None. Everything squeeze() counts counts here too."""
    if not samples:
        return None
    last = samples[-1]
    if last['pressure'] >= 2:
        return '内存压力「{}」'.format('严重' if last['pressure'] >= 4 else '警告')
    if (last.get('used_share') or 0) >= USED_SHARE:
        return '内存已经用了 {:.0f}%'.format(last['used_share'] * 100)
    before = [s for s in samples if now - s['at'] >= SWAP_WINDOW - 60]
    if before and last['swap_used'] - before[-1]['swap_used'] >= SWAP_EARLY_GIB:
        return 'swap 10 分钟里涨了 {:.1f} GB'.format(last['swap_used'] - before[-1]['swap_used'])
    return squeeze(samples, now)


def used_share(run=subprocess.run):
    """What apps, the kernel and the compressor hold, as a share of the memory (Activity Monitor's 「已使用内存」)."""
    try:
        out = run(['/usr/bin/vm_stat'], capture_output=True, text=True, timeout=10).stdout
        total = int(run(['/usr/sbin/sysctl', '-n', 'hw.memsize'], capture_output=True, text=True, timeout=10).stdout)
        page = int(re.search(r'page size of (\d+) bytes', out).group(1))
        pages = {k: int(v) for k, v in re.findall(r'^"?([^":\n]+)"?:\s+(\d+)\.', out, re.M)}
        held = (pages['Anonymous pages'] - pages.get('Pages purgeable', 0) + pages['Pages wired down']
                + pages['Pages occupied by compressor'])
        return round(held * page / total, 3)
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, AttributeError, ZeroDivisionError):
        return None


def reading(home, run=subprocess.run):
    pressure = E.memory_pressure(run)
    swap = E.swap_usage(run) or {}
    try:
        free = shutil.disk_usage(str(home)).free / GIB
    except OSError:
        free = None
    return dict(pressure=pressure, swap_used=swap.get('used_gib', 0.0), swap_total=swap.get('total_gib', 0.0), free_gib=free,
                used_share=used_share(run))


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
    current = state.get('leader')
    if not kept and (current is None or alive_same(table, current) is None):
        state.pop('leader', None)   # the job that was let go last stays the one running, so it is not paused again
    state['freed'] = [f for f in state.get('freed', []) if alive_same(table, f)]
    return let_go


footprints = O.footprints


def held(row, sizes):
    return max(row['rss'], (sizes or {}).get(row['pid'], 0))


def heavy_jobs(table, own=(), sizes=None):
    rows = [dict(r, rss=held(r, sizes)) for r in table.values() if held(r, sizes) >= HEAVY_BYTES and HEAVY.search(r['command'])
            and r['pid'] not in own and not any(word in r['command'] for word in E.NEVER) and '.app/Contents/MacOS/' not in r['command']]
    return sorted(rows, key=lambda r: -r['cpu'])


def queue(state, table, kill, now, own=(), sizes=None):
    """Keep the job furthest along running, pause the rest (AC-03). Returns what was paused."""
    paused_pids = {p['pid'] for p in state.get('paused', [])}
    freed = {(f['pid'], f['lstart']) for f in state.get('freed', [])}
    running = [r for r in heavy_jobs(table, own, sizes) if r['pid'] not in paused_pids]
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
    """Every minute, tight or not: since when each companion server has used no CPU (a little more resets the clock)."""
    fresh = {}
    for entry in found:
        key, before = str(entry['pid']), state.get('seen', {}).get(str(entry['pid']))
        if before and before['lstart'] == entry['lstart'] and entry['cpu_seconds'] - before['cpu'] <= E.ORPHAN_CPU_SECONDS:
            fresh[key] = before
        else:
            fresh[key] = dict(lstart=entry['lstart'], cpu=entry['cpu_seconds'], since=now)
    state['seen'] = fresh


def served(state, table, clients, now):
    """Every minute: since when each server has had nobody connected and used no CPU of its own (with what it
    started). Returns {pid: (clients now, quiet since)}."""
    fresh = {}
    for pid, count in clients.items():
        row = table.get(pid)
        if row is None:
            continue
        cpu = sum(table[c]['cpu'] for c in E.descendants(table, pid) if c in table)
        before = state.get('served', {}).get(str(pid))
        still = before and before['lstart'] == row['lstart'] and not count and cpu - before['cpu'] <= E.ORPHAN_CPU_SECONDS
        fresh[str(pid)] = dict(lstart=row['lstart'], cpu=cpu, clients=count, since=before['since'] if still else now)
    state['served'] = fresh
    return links(state, table)


def links(seen, table):
    return {int(p): (v['clients'], v['since']) for p, v in seen.get('served', {}).items()
            if int(p) in table and table[int(p)]['lstart'] == v['lstart']}


def idle_servers(state, table, kill, sleep, now, managed, own=()):
    """Dev servers whose starter is gone and that nobody has connected to, using no CPU of their own, for
    SERVER_IDLE_HOURS: stopped with the job that started them, while memory is getting tight (11-09: two vite
    servers had waited 3 and 9 days with nobody connected). Never a database or a service launchd runs, never one
    whose job still serves someone."""
    stopped, ended = [], set()
    served_ = state.get('served', {})
    idle = lambda p: str(p) in served_ and now - served_[str(p)]['since'] >= SERVER_IDLE_HOURS * 3600
    for key, entry in served_.items():
        pid, row = int(key), table.get(int(key))
        if (row is None or pid in own or pid in ended or row['lstart'] != entry['lstart'] or not idle(pid)
                or O.owner(table, pid, managed)['kind'] != 'gone'):
            continue
        root = pid
        while table[root]['ppid'] > 1 and table[root]['ppid'] in table:
            root = table[root]['ppid']
        tree = E.descendants(table, root)
        if any(any(word in table[p]['command'] for word in E.NEVER) for p in tree if p in table) \
                or any(str(p) in served_ and not idle(p) for p in tree):
            continue
        if E.stop_tree(table, root, table[root]['lstart'], kill=kill, sleep=sleep) is None:
            ended.update(tree)
            stopped.append(dict(pid=pid, name=O.tool(row['command']), mb=round(row['rss'] / 2 ** 20), server=True,
                                why='{}：没人管、{}没人连'.format(O.tool(row['command']), O.span(now - entry['since']))))
    return stopped


def companions(table, own=(), managed=()):
    return [pid for pid, row in table.items() if O.companion(row['command']) and pid not in own and pid not in managed
            and not any(word in row['command'] for word in E.NEVER)]


def leftovers(state, table, kill, sleep, now, managed, where, tight, own=()):
    """Stop the companions whose owner is gone or has been still for six hours (know-who AC-03): while memory is
    tight, or when the companion itself burns half a core."""
    stopped, ended = [], set()
    for pid in sorted(companions(table, own, managed), key=lambda p: -table[p]['age']):   # parents before children
        row = table[pid]
        if pid in ended or not tight and row.get('pcpu', 0) < O.COMPANION_SHARE * 100:
            continue
        quiet = state.get('seen', {}).get(str(pid))
        yes, why = O.verdict(row, O.owner(table, pid, managed), where, state.get('agents', {}),
                             quiet and quiet['since'], now)
        if yes and E.stop_tree(table, pid, row['lstart'], kill=kill, sleep=sleep) is None:
            ended.update(E.descendants(table, pid))
            stopped.append(dict(pid=pid, name=O.tool(row['command']), mb=round(row['rss'] / 2 ** 20), why=why))
    return stopped


def short_name(command):
    """What people would call it: the App's name, or the tool's (tsc, swift-frontend…)."""
    return O.tool(command)


def who(state, table, now, managed, run=subprocess.run, sizes=None, own=(), served_now=None):
    """know-who AC-01: the biggest processes and the ones that have burned a core for half an hour, each with its
    tool, project and owner. Kept in the state for the memory page, the notice and the agent's material."""
    hot = O.busy(state, table, now)
    big = O.biggest(table, [p for p in table if p not in own and 'kernel_task' not in table[p]['command']], managed, sizes)
    top = [p for members in big for p in members]
    agents = O.track(state, table, now)
    pids = {p for members in big for p in members[:O.LOOK_PER_GROUP]} | set(hot) | set(companions(table, own, managed))
    pids |= {int(p) for p in agents} | O.users(table, top + list(hot))   # who is connected to a simulator tells its project (know-who AC-06)
    where = O.seen_where(state, table, [p for p in pids if p in table], run)
    rows = O.describe(table, top + [p for p in hot if p not in top], where, agents, managed, now, sizes=sizes, hot=hot,
                      links=served_now)
    groups = O.group(rows, now)
    state['who'] = dict(at=now, groups=groups, lines=[O.line(g, now) for g in groups])
    return groups, where


def top_users(groups, limit=3):
    """The notice's 「最占内存的」: by App or tool, with project and owner (know-who AC-02)."""
    found = []
    for g in groups[:limit]:
        if g['bytes'] < GIB / 4:
            continue
        kind = g['owner']['kind']
        extra = [O.where_words(g)] + ([g['fact'].split(' · ')[0]] if kind in ('agent', 'editor', 'terminal') and not g['app']
                                      else ['没人管了'] if kind == 'gone' else [])
        extra = [e for e in extra if e]
        found.append(dict(name=g['title'] + ('（{}）'.format('，'.join(extra)) if extra else ''), gib=round(g['bytes'] / GIB, 1)))
    return found


def hot_users(groups, now, limit=2):
    """know-who AC-04: what has burned most of a core for half an hour or more."""
    return [g['title'] + '（' + g['fact'] + '）' + O.busy_words(g, now) for g in groups if g.get('busy_since')][:limit]


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


def words(stopped, paused, users, sessions, hot=()):
    parts = []
    for kind, these in (('没人管的陪跑后台', [s for s in stopped if not s.get('server')]),
                        ('{} 小时以上没人连的开发服务'.format(SERVER_IDLE_HOURS), [s for s in stopped if s.get('server')])):
        if these:
            parts.append('关掉了 {} 个{}（{}）'.format(len(these), kind, '、'.join(sorted({s['name'] for s in these}))))
    if paused:
        parts.append('{} 个重活同时在跑，已让 {} 个排队（{}），一个跑完再放下一个'.format(
            len(paused) + 1, len(paused), '、'.join(sorted({p['name'] for p in paused}))))
    if users:
        parts.append('现在最占内存的是 ' + '、'.join('{} {:.1f} GB'.format(u['name'], u['gib']) for u in users))
    for line in hot:
        parts.append(line)
    for session in sessions:
        parts.append('{} 有个会话已经 {} MB，开个新会话会顺很多'.format(session['agent'], session['mb']))
    return '；'.join(parts) + '。' if parts else ''


def tick(home, now=None, run=subprocess.run, table=None, kill=os.kill, sleep=time.sleep, managed=None,
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
    # Everything below starts at 「快紧了」 (`why`); `tight` is the squeeze proper.
    tight = squeeze(state['samples'], now)
    why = soon(state['samples'], now)
    result = dict(on=True, tight=tight, early=why, stopped=[], paused=[], released=[])
    if table is not None:
        calm = [s for s in state['samples'] if now - s['at'] <= NORMAL_TO_RELEASE * 60 + 30]
        everything = why is None and len(calm) >= NORMAL_TO_RELEASE and all(s['pressure'] <= 1 for s in calm[-NORMAL_TO_RELEASE:])
        result['released'] = release(state, table, kill, now, everything=everything)
    groups = []
    # While tight, or with much in swap, most of what each one holds is swapped or compressed, which ps does not see
    # (11-03: the Claude App showed 0.5 GB of its 4 GB; 11-04: pressure back to normal with 16 GB in swap, 25 Claude
    # Code sessions showed 0.2 GB): who holds what is measured the way top counts it then.
    swapped = (sample.get('swap_used') or 0) >= SWAPPED_GIB
    sizes = footprints(run) if (why or swapped) and table is not None else None
    if table is not None:
        managed = E.launchd_jobs(run) if managed is None else managed
        if managed is not None:
            clients = O.serving(run)
            served_now = served(state, table, clients, now) if clients is not None else links(state, table)
            try:
                groups, where = who(state, table, now, managed, run, sizes=sizes, own=[os.getpid()], served_now=served_now)
            except (OSError, subprocess.SubprocessError):
                groups, where = [], {}
            observe(state, [dict(pid=p, lstart=table[p]['lstart'], cpu_seconds=sum(table[c]['cpu'] for c in E.descendants(table, p) if c in table))
                            for p in companions(table, [os.getpid()], managed) if O.companion(table[p]['command']) in O.SERVERS], now)
            result['stopped'] = leftovers(state, table, kill, sleep, now, managed, where, bool(why), own=[os.getpid()])
            if why and clients is not None:
                result['stopped'] += idle_servers(state, table, kill, sleep, now, managed, own=[os.getpid()])
    if why and table is not None:
        result['paused'] = queue(state, table, kill, now, own=[os.getpid()], sizes=sizes)
        quiet = now - state.get('noticed_at', 0) >= NOTICE_GAP
        if not state.get('episode'):
            state['episode'] = dict(since=now, why=why)
            if quiet:
                body = words(result['stopped'], result['paused'], top_users(groups), big_sessions(home, now), hot_users(groups, now))
                notice(home, ('内存在变紧：' if tight else '内存快紧了：') + why,
                       body or '正在盯着，暂时没有能安全处理的。', tab='memory')
                state['noticed_at'] = now
        elif (result['stopped'] or result['paused']) and quiet:
            notice(home, '内存还紧，又处理了一些', words(result['stopped'], result['paused'], [], []), tab='memory')
            state['noticed_at'] = now
        state['episode']['last_tight'] = now
    elif why is None and state.get('episode') and now - state['episode'].get('last_tight', state['episode']['since']) >= CALM_TO_END * 60:
        state.pop('episode', None)
    if why is None and result['stopped'] and now - state.get('noticed_at', 0) >= NOTICE_GAP:
        notice(home, '关掉了占着 CPU 的陪跑后台', words(result['stopped'], [], [], []), tab='memory')   # know-who AC-03
        state['noticed_at'] = now
    if result['stopped'] or result['paused']:
        names = [s['name'] for s in result['stopped']] + [p['name'] for p in result['paused']]
        acted = dict(at=now, stopped=len(result['stopped']), paused=len(result['paused']), names=names[:6])
        state['acted'] = (state.get('acted', []) + [acted])[-20:]
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
                early=soon(state.get('samples', []), now), used_share=last.get('used_share'),
                pressure={1: '正常', 2: '警告', 4: '严重'}.get(last.get('pressure'), '未知'), swap_gib=last.get('swap_used'),
                queued=[dict(name=p['name'], gib=p['gib'], minutes=round((now - p['since']) / 60)) for p in state.get('paused', [])],
                acted=state.get('acted', [])[-5:], who=(state.get('who') or {}).get('lines', [])[:8])
