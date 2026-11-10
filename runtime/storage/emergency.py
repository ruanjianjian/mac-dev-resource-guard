"""emergency-agent: when this Mac is about to choke, start the user's own agent instead of waiting for a click.

The goal is a Mac that does not stall: enough room on the internal disk and no memory squeeze (RAW-21). The
plan's rules are where the agent starts, not where it stops. When the disk drops below the danger line (and
again at each deeper step) or memory stays under pressure, the background starts the agent (RAW-22). The agent
reads the evidence and writes down decisions only; after it ends, the background rechecks each one against the
safety floor and does it (CH-02): Codex runs in a sandbox that can only write into the run's own folder.
"""
import concurrent.futures
import fcntl
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid

from storage import GIB, Activity, StorageManager, atomic_json
import agent_plan
import ask_tier
import auto_tier
import checkup
import owners as O
import plan as space_plan
import tasks as T

STATE, RUNS, MATERIAL, DECISIONS, RESULT = 'emergency.json', 'emergency', 'material.json', 'decisions.jsonl', 'result.json'
HANDED = 'emergency-moves.json'   # moves a run could not make for want of the drive's permission (agent-investigates AC-09)
HANDED_HOURS = 24
HANDED_WORDS = '交给资源看板自己的那一轮去挪（它有外接盘的权限，5 分钟内）'
SESSION = 'session.json'      # which conversation this run is, so the panel can open it (emergency-agent AC-11)
CONVERSATION = 'conversation.json'   # the one conversation every run of an agent continues (CH-07)
TITLE = '资源看板 · 资源管理'   # the main agent's one conversation
SUB_TITLE = '资源看板 · 告急 {}'   # one emergency's own conversation, archived when it is done
DAILY_HOUR = 9                # the main agent looks over the day's runs once a day, from this hour on
DAILY_GAP_HOURS = 20
KEEP_RUNS = 48                # run folders kept: a busy day's runs, for the main agent to look over
SWAP_DISK_GIB = 4             # swap this big is worth saying when the disk is short: it is on the same disk
DIR_ENV = 'RESOURCE_GUARD_EMERGENCY_DIR'
DEEPER_GIB = (10, 5)          # after the danger line, one more run at each of these (AC-01)
WARN_MINUTES = 15             # memory still 「警告」 this long after the watch's own work: the agent comes (proactive-watch AC-05)
EARLY_MINUTES = 5             # or the watch's 「快紧了」 this long: by 「警告」 the Mac is already too slow to use (11-05)
MEMORY_QUIET_HOURS = 6        # nothing new since the agent last looked: it comes again only after this
SOONER_MINUTES = 30           # still tight and something new: it comes again this soon (agent-investigates AC-07)
HEAVY_GIB = 1.0               # a group this big, or one burning a core, is worth a new look when it appears
SWAP_MORE_GIB = 3.0
RUN_MINUTES = 30              # a run that takes longer is stopped (AC-10)
GIT_SECONDS = 15              # one git call while gathering the material; slower reads as unknown
COLLECT_SECONDS = 180         # the whole material; past this, the rest of the repositories are not looked at
ORPHAN_HOURS = 24
ORPHAN_CPU_SECONDS = 2.0      # CPU an orphan may burn between the look and the stop and still count as idle
ASK_KINDS = ('simulator-delete', 'simulator-shutdown', 'task-delete', 'process-stop', 'path-delete', 'task-move')
# 11-10 (the user set the target to 100 GB): 「没达到目标的话，agent 要自己去想办法出方案，然后强提醒用户去看」.
SHORT_HOURS = 6        # below the user's target, above the danger line or not: the agent works out a plan this often
REMIND_HOURS = 2       # its plan waiting and the disk still short: the user is told again this often
# What the agent reads beside the material: the raw facts its sandbox may not read itself (Codex's keeps ps, top
# and simctl from it), taken by the background as the run starts.
SNAPSHOTS = (('ps.txt', ['/bin/ps', '-axww', '-o', 'pid,ppid,user,rss,%cpu,etime,lstart,command']),
             ('top.txt', ['/usr/bin/top', '-l', '1', '-o', 'mem', '-n', '80', '-stats', 'pid,ppid,command,mem,cmprs,cpu,time']),
             ('simctl.json', ['/usr/bin/xcrun', 'simctl', 'list', 'devices', '-j']),
             ('vm_stat.txt', ['/usr/bin/vm_stat']))
READ_ONLY = ('ps', 'top', 'lsof', 'du', 'df', 'ls', 'stat', 'vm_stat', 'sysctl -n', 'memory_pressure', 'xcrun simctl list',
             'footprint')
MAYBE_SIM_DAYS = 1            # the disk still short and the agent listed nothing: shut-down simulators unused this long are asked about
MAYBE_MAX = 8
NEVER = O.NEVER
RGS = Path.home() / '.local/bin/resource-guard-service'
PROMPT = ('{when} 资源看板又叫你来了：{why}。用 mac-dev-resource-guard 这个 skill 处理：先运行 `{rgs} emergency show`'
          '（这次的材料在 {folder}），照里面的 steps 做。这次是资源看板自己把你叫来的，用户不在场；全程用简体中文。')
# 11-10: the one conversation every run continued had, 40 runs on, the agent copying its own old answers (「先说明
# 上轮漏项」 for 18 hours) and handing back no decision while the disk ran out. Now a main agent keeps the one
# conversation and looks over the runs once a day; each emergency goes to a sub-agent in a conversation of its own.
FIRST = ('这段对话是资源看板的主 agent 专用的：每天叫你来一次，看各轮告急处理（每轮交给一个一次性的子 agent）做得对不对，'
         '把以后用得上的记成经验。前几天看过什么都在上文，你自己记着用。\n\n')
AGAIN = '\n\n前几天看过的都在上文，接着用。'
SUB = ('这次告急交给你一次处理完：这段对话只用这一次，做完就归档。用户以前的决定和学到的经验在材料的 decisions、'
       'lessons 里，照着用。\n\n')
DAILY_PROMPT = ('{when} 资源看板每天叫你来一次，你是它的主 agent。用 mac-dev-resource-guard 这个 skill：先运行 `{rgs} emergency show`'
                '（这次的材料在 {folder}），照里面的 steps 做。用户不在场；全程用简体中文。')
IDLE = ('上次一个决定都没交回，后台什么也没做，所以还这么紧。这次别照抄上次的结论：材料里每个大头，要么交回决定'
        '（stop、move、ask），要么在 done --summary 里写清它为什么不能动、要谁做什么。\n\n')
LAST = '上次（{when}）的回执：{receipt}。{judged}\n\n'                       # agent-investigates AC-04, AC-06
STREAK = '这是连续第 {n} 次因为{why}叫你：上次处理完，后台看还紧。\n\n'   # AC-05
NEW = '跟上次（{when}）比，这次新出现的：{what}。先看这些。\n\n'                         # AC-07
STREAK_HOURS = 12
WHY = {'disk': '磁盘快满了', 'memory': '内存吃紧', 'asked': '用户让你现在处理一次'}
QUEUE_POLL_SECONDS = 10       # how often to look whether the Codex App finished the turn it was handed (CH-08)
CARRY = 'RG-DECISION '        # a decision the agent could not write comes back in its reply after this (CH-08)
UNWRITABLE = ('这一轮写不进去（只读）。不要申请权限：把下面这一行原样放进你最后的回复，资源看板会照它处理。\n')
STEPS = (
    # 11-10: the steps had grown into a rulebook (what to look at, what never to touch), and Codex's sandbox kept
    # ps and simctl from it; for 18 hours it only restated old conclusions. Now: the goal, the facts, what it can
    # hand back, and the floor the background keeps. How to find the cause is the model's to work out.
    '目标只有一个：这台 Mac 不卡、不爆盘——内置盘回到目标以上，内存不再吃紧。为什么紧、先动哪个，你自己判断：'
    '每台 Mac、每一次的原因都不一样，别套以前的结论，从这次的现场重新看。',
    '现场：材料（emergency show）是后台整理的线索；同一个目录里还有原始数据：ps.txt（全部进程，含父进程、内存、CPU、'
    '启动时间、完整命令）、top.txt（按内存排，含压缩内存）、simctl.json（全部模拟器）、vm_stat.txt。磁盘用 du、ls、'
    'lsof 这些只读命令自己查，哪里都可以看；交换文件在 /System/Volumes/VM，内存一紧它会连磁盘一起吃掉。',
    'decisions 是用户以前确认过、忽略过、在对话里说过的，lessons 是学到的经验：照着用；用户说过别动的不要列。',
    '你能交回的决定，后台会在你结束后逐项复核再做：`emergency stop <pid> --why "依据"` 关掉没人用的后台（orphans，'
    '或 companions 里 may_stop 的）；`emergency move <任务路径> --why` 把闲置任务整个挪到外接盘（不丢东西）；'
    '`emergency ask <kind>:<id> --why` 列给用户点头，kind 是 simulator-delete、simulator-shutdown、task-delete、'
    'task-move（有没保存改动、所以后台不挪的闲置任务，整个挪到外接盘，不丢东西）、'
    'process-stop（主人还在的进程）、path-delete（你查到的任何大目录或大文件：缓存、日志、旧构建、容器数据……'
    '写清是什么、为什么能删、删了会怎样）；`emergency rule <规则> --decision <做法> --why` 改计划；'
    '`emergency lesson` 记经验。',
    '还紧就一定要交回决定：能做的做，不能做的列给用户点头；什么都不交回，这一轮就白来了。确实没有能动的，'
    '在 done --summary 里逐个写清每个大头为什么不能动、要谁做什么。',
    '内置盘没到用户的目标（disk.goal_gib）就要出一份够数的方案：先想能放到外接盘的（disk.external_free_gib 是外接盘'
    '还空多少，挪过去不丢东西），再想能删的；凑不够也照实说还差多少、还能怎么办。你交回的方案后台会提醒用户去看。',
    '底线，后台会拒绝：正在用的、有没提交或没推送改动的、被用户保护的不动；App 本身、数据库、容器、调试器不关；'
    '会丢东西的只能列给用户。',
    '学到了以后也用得上的经验，记成一条规则：`emergency lesson --when "遇到什么情况" --do "该怎么做、为什么"`；'
    '改的是已有的那条，加 `--replaces <id>`。这一次查到了什么、做了什么写进 done --summary，不要记成经验。'
    'old_notes 是以前每次的笔记，不是规则：里面以后还用得上的，也这样改写成规则（带 --replaces）。',
    '说「恢复了」之前看 trend 的最近 10 分钟：一分钟正常不算恢复。后台收尾时也会自己再看一次。',
    '最后 `emergency done --cause "为什么紧" --summary "做了什么、还差什么" --need "要用户做什么"`（没有要用户做的就不写 --need）。'
    'done 会念一遍你交回了哪些决定：总结里只写交回了的；磁盘还差却没有腾磁盘的决定时，它会先请你再看一眼。',
    '命令说「写不进去」时，照它说的把那一行原样放进你最后的回复，后台从回复里取。',
)


DAILY_STEPS = (
    '你是资源看板的主 agent，每天来一次。rounds 是上次以来每轮告急处理（每轮是一个一次性的子 agent）的回执，'
    'user_decisions 是用户在看板上点过的、在对话里说过的，plan_changes 是计划怎么变了。',
    '先判断子 agent 有没有照预期做事：还紧的轮次交回决定了没有（handed）；有没有一轮轮照抄同样的话；'
    '说「等后台复核」「排队」的事后台真做了没有（看 done）。',
    '用户反复同意、从没忽略的同一类事，后台会自己改成「你同意过，直接做」（plan_changes 里 by 是 learned）；'
    '看它改得对不对，不对就写进 --need 告诉用户。',
    '以后用得上的写成经验：`emergency lesson --when "遇到什么情况" --do "该怎么做、为什么"`；已有的那条不对，'
    '加 `--replaces <id>` 改写。old_notes 是以前的流水笔记，不是规则：有用的改写成规则（带 --replaces）。',
    '最后 `emergency done --cause "这段时间为什么紧（或不紧）" --summary "子 agent 做得怎样、记了什么经验"`。'
    '只有要用户拍板，或子 agent 明显没干活时才写 --need；没问题就不写，不打扰用户。',
    '命令说「写不进去」时，照它说的把那一行原样放进你最后的回复，后台从回复里取。',
)


def state_root(home):
    return Path(home) / '.local/state/dev-resource-guard'


def load_state(home):
    try:
        return json.loads((state_root(home) / STATE).read_text())
    except (OSError, ValueError):
        return {}


def save_state(home, state):
    state_root(home).mkdir(parents=True, exist_ok=True)
    atomic_json(state_root(home) / STATE, state)


def memory_pressure(run=subprocess.run):
    """kern.memorystatus_vm_pressure_level: 1 normal, 2 warning, 4 critical; 0 when it cannot be read."""
    try:
        out = run(['/usr/sbin/sysctl', '-n', 'kern.memorystatus_vm_pressure_level'], capture_output=True, text=True, timeout=10)
        return int(out.stdout.strip() or 0)
    except (OSError, subprocess.SubprocessError, ValueError):
        return 0


def swap_usage(run=subprocess.run):
    try:
        out = run(['/usr/sbin/sysctl', '-n', 'vm.swapusage'], capture_output=True, text=True, timeout=10).stdout
        found = dict(re.findall(r'(total|used) = ([\d.]+)M', out))
        return dict(used_gib=round(float(found['used']) / 1024, 1), total_gib=round(float(found['total']) / 1024, 1))
    except (OSError, subprocess.SubprocessError, ValueError, KeyError):
        return None


def disk_level(free, critical):
    """0 above the danger line, 1 below it, one more for each deeper step below it."""
    if free >= critical:
        return 0
    return 1 + sum(1 for step in DEEPER_GIB if step * GIB < critical and free < step * GIB)


def alive(pid):
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, ValueError, TypeError):
        return False


def running(state, now):
    run = state.get('run') or {}
    return bool(run) and alive(run.get('pid')) and now - run.get('started', 0) < (RUN_MINUTES + 5) * 60


def sign(group):
    """One group of who holds what, the same from minute to minute: tool, owner and projects, not how it is doing."""
    owner = group.get('owner') or {}
    return '|'.join([group['tool'], owner.get('kind', ''), owner.get('name') or ''] + sorted(group.get('projects', [])))


def view(seen, pressure, now):
    """What the agent would be shown now, kept at each run to tell later what is new (agent-investigates AC-07)."""
    who = seen.get('who') or {}
    groups = who.get('groups', []) if now - who.get('at', 0) <= 600 else []
    heavy = {sign(g): O.line(g, now) for g in groups if g['bytes'] >= HEAVY_GIB * GIB or g.get('busy_since')}
    swap = ((seen.get('samples') or [{}])[-1]).get('swap_used')
    episode = seen.get('episode') or {}
    early = episode.get('since') if now - episode.get('last_tight', 0) <= 150 else None   # still 「快紧了」 a minute ago
    return dict(at=now, pressure=pressure, swap=swap, heavy=heavy, early_since=early)


def news(looked, current):
    """What has changed since the agent last looked: a big one it has not seen, pressure gone 「严重」, swap well up."""
    if not looked or not current:
        return []
    found = [line for key, line in current['heavy'].items() if key not in looked.get('heavy', {})]
    if current['pressure'] >= 4 > looked.get('pressure', 0):
        found.append('内存压力到了「严重」')
    if current['swap'] is not None and looked.get('swap') is not None and current['swap'] - looked['swap'] >= SWAP_MORE_GIB:
        found.append('swap 比上次多了 {:.0f} GB（{:.0f} → {:.0f} GB）'.format(current['swap'] - looked['swap'], looked['swap'], current['swap']))
    return found


def due(state, free, critical, pressure, now, current=None, goal=None):
    """Why the agent should come now ('disk' or 'memory'), or None. Updates `state` in place (AC-01~03).
    `current` is view() now: still tight and something new, the agent comes before the quiet hours are over.
    `goal`: the user's target; below it the agent comes every SHORT_HOURS to work out a plan."""
    level = disk_level(free, critical)
    if level == 0:
        state['disk_fired'] = 0     # back above the line: the next drop counts again
    if pressure >= 2:
        state.setdefault('warn_since', now)
    else:
        state.pop('warn_since', None)
    reason = None
    if level > state.get('disk_fired', 0):
        reason = 'disk'
    elif (pressure >= 4 or pressure >= 2 and now - state.get('warn_since', now) >= WARN_MINUTES * 60
          or (current or {}).get('early_since') and now - current['early_since'] >= EARLY_MINUTES * 60):
        looked = state.get('looked') or {}
        if now - state.get('memory_fired_at', 0) >= MEMORY_QUIET_HOURS * 3600:
            reason = 'memory'
        elif now - looked.get('at', 0) >= SOONER_MINUTES * 60 and (news(looked, current) or not looked and current and current['heavy']):
            reason = 'memory'   # nothing kept from its last look (an update): what is there now is all new to it
    if reason is None and goal and free < goal and now - state.get('short_fired_at', 0) >= SHORT_HOURS * 3600:
        reason = 'disk'
    # No count a day (RAW-53): one run at a time, and what changed decides when the next one comes.
    return None if reason is None or running(state, now) else reason


def remind(home, state, free, goal, now):
    """Below the user's target with the agent's plan still waiting: said again every REMIND_HOURS, until the
    user answers or the disk is back (11-10 「强提醒用户去看」)."""
    if free >= goal:
        state.pop('reminded_at', None)
        return None
    last = state.get('last') or {}
    if now - max(state.get('reminded_at', 0), last.get('at', 0)) < REMIND_HOURS * 3600:
        return None
    pending = ask_tier.load(home, ask_tier.PENDING, ask_tier.empty_pending())
    waiting = [i for i in pending['items'] if i['state'] in ('pending', 'failed') and not i.get('quiet') and not i.get('unreachable')]
    if not waiting and not last.get('need'):
        return None
    body = ('{} 项等你确认，约 {:.0f} GB'.format(len(waiting), sum(i.get('bytes') or 0 for i in waiting) / GIB) if waiting else '')
    if last.get('need'):
        body += ('；' if body else '') + 'agent 说要你做：' + last['need']
    agent_plan.notice(home, '离目标还差 {:.0f} GB：agent 的方案还等你看'.format((goal - free) / GIB), body[:300],
                      tab='pending' if waiting else 'emergency')
    state['reminded_at'] = now
    return body


def undo_fired(state):
    """A run that broke or never got going did nothing: it does not cost the quiet hours
    (proactive-watch AC-05; on 10-31 a run that died at once kept the agent away for six hours)."""
    run = state.get('run') or {}
    for key, value in (run.get('prev') or {}).items():
        if value is None:
            state.pop(key, None)
        else:
            state[key] = value


def mark_fired(state, reason, free, critical, now):
    """A run the user asked for leaves the disk steps and the quiet hours as they were."""
    if reason == 'disk':
        state['disk_fired'] = disk_level(free, critical)
        state['short_fired_at'] = now
    elif reason == 'memory':
        state['memory_fired_at'] = now


def why_words(reason, free, critical, pressure):
    if reason == 'disk':
        return '内置盘只剩 {:.1f} GB，低于危险线 {:.0f} GB'.format(free / GIB, critical / GIB)
    if reason == 'memory':
        return '内存吃紧（系统内存压力：{}）'.format({4: '严重', 2: '警告'}.get(pressure, '还没报警，但内存快用满了'))
    return '用户要求处理一次'


# ---- which agent can run by itself (AC-04) ------------------------------------------------------------------

def _version_key(path):
    """…/claude-code/<version>/<build>/claude.app/Contents/MacOS/claude"""
    return [int(p) if p.isdigit() else 0 for p in Path(path).parents[4].name.split('.')]


def claude_binary(home):
    home = Path(home)
    found = [shutil.which('claude'), home / '.local/bin/claude', home / '.claude/local/claude']
    for path in found:
        if path and Path(path).is_file() and os.access(path, os.X_OK):
            return Path(path)
    # The Claude desktop App ships Claude Code: …/claude-code/<version>/<build>/claude.app/Contents/MacOS/claude
    # (older builds: …/claude-code/<version>/claude). People who only use the desktop App have it too.
    root = home / 'Library/Application Support/Claude/claude-code'
    bundled = sorted(root.glob('*/*/claude.app/Contents/MacOS/claude'), key=_version_key)
    bundled = bundled or sorted(root.glob('*/claude'), key=lambda p: [int(x) if x.isdigit() else 0 for x in p.parent.name.split('.')])
    return bundled[-1] if bundled else None


def claude_logged_in(home, env=None, run=subprocess.run):
    """The desktop App keeps its login to itself; the background needs Claude Code's own (CH-02).
    Claude Code says so itself (`auth status`); the key and token variables count as well."""
    env = os.environ if env is None else env
    if env.get('CLAUDE_CODE_OAUTH_TOKEN') or env.get('ANTHROPIC_API_KEY'):
        return True
    binary = claude_binary(home)
    if binary is None:
        return False
    try:
        out = run([str(binary), 'auth', 'status', '--json'], capture_output=True, text=True, timeout=30)
        return bool(json.loads(out.stdout or '{}').get('loggedIn'))
    except (OSError, subprocess.SubprocessError, ValueError):
        return False


def codex_binary(home):
    # The Codex App (named ChatGPT.app on some Macs) ships the codex command; others install it on their own.
    for path in (shutil.which('codex'), Path(home) / '.local/bin/codex', Path('/opt/homebrew/bin/codex'),
                 Path('/usr/local/bin/codex'),
                 Path('/Applications/ChatGPT.app/Contents/Resources/codex-cli/bin/codex'),
                 Path('/Applications/Codex.app/Contents/Resources/codex-cli/bin/codex'),
                 Path('/Applications/Codex.app/Contents/Resources/codex')):
        if path and Path(path).is_file() and os.access(path, os.X_OK):
            return Path(path)
    return None


def codex_logged_in(home):
    return (Path(home) / '.codex/auth.json').is_file()


def runners(home, preferred, env=None, run=subprocess.run):
    """Agents that can start without anyone at the keyboard, the recorded one first."""
    order = [preferred] if preferred in ('claude', 'codex') else []
    order += [name for name in ('claude', 'codex') if name not in order]
    ready = []
    for name in order:
        if name == 'claude' and claude_binary(home) and claude_logged_in(home, env, run):
            ready.append(name)
        elif name == 'codex' and codex_binary(home) and codex_logged_in(home):
            ready.append(name)
    return ready


def note_session(folder, **session):
    atomic_json(Path(folder) / SESSION, dict(at=time.time(), **session))


def conversation(home, agent):
    """The main agent's one conversation (CH-07, 11-10): its context and memory, not one more 「告急处理」 in the
    list each time. The ones from before ran inside a run's own folder, where the agent would read that old run's
    material, so they are not taken over. One without `since` is the one every run continued before 11-10."""
    try:
        return json.loads((state_root(home) / RUNS / CONVERSATION).read_text()).get(agent)
    except (OSError, ValueError):
        return None


def keep_conversation(home, agent, **ids):
    """Remember the main agent's conversation; with no ids, forget it."""
    path = state_root(home) / RUNS / CONVERSATION
    try:
        known = json.loads(path.read_text())
    except (OSError, ValueError):
        known = {}
    if not ids:
        known.pop(agent, None)
    else:
        old = known.get(agent) or {}
        same = old.get('since') and all(old.get(k) == v for k, v in ids.items() if k in ('thread', 'session'))
        known[agent] = dict(ids, agent=agent, at=time.time(), since=old['since'] if same else time.time())
    atomic_json(path, known)


def conversation_root(home):
    """Where the one conversation runs: the runs' parent, which outlives any run folder."""
    root = state_root(home) / RUNS
    root.mkdir(parents=True, exist_ok=True)
    return root


def run_claude(home, folder, prompt, env, timeout, popen=subprocess.Popen, main=False):
    """Claude Code headless, allowed only the emergency commands and reading (AC-04). An emergency gets a session
    of its own; the main agent (`main`) continues its one session (CH-07). The id is chosen up front, so the panel
    can open it in Terminal afterwards (AC-11)."""
    binary = claude_binary(home)
    cwd = conversation_root(home)
    known = conversation(home, 'claude') if main else None
    if known and known.get('since') and any((Path(home) / '.claude/projects').glob('*/{}.jsonl'.format(known['session']))):
        session, words, start = known['session'], prompt + AGAIN, '--resume'
    else:   # an emergency, the main agent's first run, or the user deleted its session
        session, words, start = str(uuid.uuid4()), (FIRST if main else SUB) + prompt, '--session-id'
    if main:
        keep_conversation(home, 'claude', session=session, binary=str(binary), cwd=str(cwd))
    note_session(folder, agent='claude', session=session, binary=str(binary), cwd=str(cwd))
    command = [str(binary), '-p', words, start, session, '--output-format', 'json', '--permission-mode', 'dontAsk',
               '--allowedTools', 'Bash({} emergency:*)'.format(RGS), 'Read', 'Skill', 'Glob', 'Grep',
               *['Bash({}:*)'.format(c) for c in READ_ONLY], '--max-turns', '80']
    process = popen(command, cwd=str(cwd), env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT, text=True, start_new_session=True)
    try:
        out, _ = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        stop_group(process)
        return dict(agent='claude', status='timeout')
    try:
        reply = json.loads(out.strip().splitlines()[-1])
        said = str(reply.get('result') or '')
        return dict(agent='claude', status='error' if reply.get('is_error') else 'done', session=reply.get('session_id'),
                    reply=said[:2000], said=said)
    except (ValueError, IndexError):
        return dict(agent='claude', status='error', reply=(out or '')[-2000:])


def queue_codex(home, thread, words, env, run=subprocess.run):
    """`codex queue`: the Codex App, which has the thread open, runs these words as its next turn. False on a Codex
    without it."""
    try:
        done = run([str(codex_binary(home)), 'queue', '--thread', thread, '--message', words], env=env,
                   capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return False
    return done.returncode == 0


def said_in(turn):
    return '\n'.join(item.get('text') or '' for item in (turn or {}).get('items') or [] if item.get('type') == 'agentMessage')


def run_codex(home, folder, prompt, env, timeout, popen=subprocess.Popen, run=subprocess.run, main=False):
    """An emergency gets a Codex thread of its own, archived once its turn is done, so nothing piles up in the
    App (11-10). The main agent (`main`) keeps the one thread the user can open (AC-04, CH-07). Both are sandboxed
    to the runs' folder (CH-02); once the user has opened the main one, the App owns it until it quits (CH-08)."""
    cwd = conversation_root(home)
    process = popen([str(codex_binary(home)), 'app-server'], cwd=str(cwd), env=env, stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, bufsize=1, start_new_session=True)
    late = threading.Event()
    watchdog = threading.Timer(timeout, lambda: (late.set(), stop_group(process)))
    watchdog.daemon = True
    watchdog.start()
    counter = [0]

    def send(method, params, notify=False):
        message = dict(method=method, params=params)
        if not notify:
            counter[0] += 1
            message['id'] = counter[0]
        process.stdin.write(json.dumps(message, ensure_ascii=False) + '\n')
        process.stdin.flush()
        return message.get('id')

    def wait(match):
        for line in process.stdout:
            try:
                message = json.loads(line)
            except ValueError:
                continue
            if 'id' in message and 'method' in message:   # the server asks something: nothing is approved
                process.stdin.write(json.dumps(dict(id=message['id'], error=dict(code=-32601, message='not supported'))) + '\n')
                process.stdin.flush()
                continue
            if match(message):
                return message
        raise TimeoutError if late.is_set() else OSError('Codex 意外退出了')

    def turn_of(thread, words):
        """The turn that started with these words, read without owning the thread."""
        asked = send('thread/turns/list', dict(threadId=thread, limit=5, itemsView='summary'))
        for turn in (wait(lambda m: m.get('id') == asked).get('result') or {}).get('data') or []:
            for item in turn.get('items') or []:
                if item.get('type') == 'userMessage' and words in [c.get('text') for c in item.get('content') or []]:
                    return turn
        return None

    def archive(thread):
        asked = send('thread/archive', dict(threadId=thread))
        wait(lambda m: m.get('id') == asked)   # an error here only means it stays in the list

    thread = None
    try:
        asked = send('initialize', dict(clientInfo=dict(name='resource-guard', title='资源看板', version='1'),
                                        capabilities=dict(experimentalApi=False)))
        wait(lambda m: m.get('id') == asked)
        send('initialized', {}, notify=True)
        words = prompt + AGAIN
        known = conversation(home, 'codex')
        if known and not known.get('since'):
            # The one thread every run continued before 11-10: archived, and the main agent starts afresh.
            archive(known['thread'])
            keep_conversation(home, 'codex')
            known = None
        if main and known:
            asked = send('thread/resume', dict(threadId=known['thread'], cwd=str(cwd), approvalPolicy='never',
                                               sandbox='workspace-write', excludeTurns=True))
            resumed = wait(lambda m: m.get('id') == asked)
            if 'error' not in resumed:
                thread = known['thread']
            elif 'active writer' in str(resumed['error']) and queue_codex(home, known['thread'], words, env, run=run):
                thread = known['thread']
                note_session(folder, agent='codex', thread=thread)
                while True:   # the App may be busy with the user's own turn first
                    turn = turn_of(thread, words)
                    # Read from outside, a turn the App is still running shows as 「interrupted」 with no end time.
                    if turn and turn.get('completedAt'):
                        return dict(agent='codex', status='done', thread=thread, turn=turn.get('status'), via='app',
                                    said=said_in(turn))
                    if late.wait(QUEUE_POLL_SECONDS):
                        raise TimeoutError
        if thread is None:   # an emergency, the main agent's first run, or the user deleted its thread
            words = (FIRST if main else SUB) + prompt
            params = dict(cwd=str(cwd), approvalPolicy='never', sandbox='workspace-write', ephemeral=False, threadSource='user')
            asked = send('thread/start', params)
            started = wait(lambda m: m.get('id') == asked)
            if 'error' in started:
                # threadSource is what puts the thread in the Codex App's list; a Codex too old to know it still runs.
                params.pop('threadSource')
                asked = send('thread/start', params)
                started = wait(lambda m: m.get('id') == asked)
            if 'error' in started:
                return dict(agent='codex', status='error', reply=str(started['error'])[:2000])
            thread = started['result']['thread']['id']
        if main:
            keep_conversation(home, 'codex', thread=thread)
        note_session(folder, agent='codex', thread=thread)
        send('thread/name/set', dict(threadId=thread, name=TITLE if main else SUB_TITLE.format(time.strftime('%m-%d %H:%M'))))
        asked = send('turn/start', dict(threadId=thread, input=[dict(type='text', text=words, text_elements=[])]))
        done = wait(lambda m: m.get('method') == 'turn/completed' and (m.get('params') or {}).get('threadId') == thread)
        said = said_in(turn_of(thread, words))
        if not main:
            archive(thread)
        return dict(agent='codex', status='done', thread=thread, turn=((done.get('params') or {}).get('turn') or {}).get('status'),
                    said=said)
    except TimeoutError:
        return dict(agent='codex', status='timeout', thread=thread)
    except (OSError, ValueError, KeyError, TypeError) as error:
        return dict(agent='codex', status='error', thread=thread, reply=str(error)[:2000])
    finally:
        watchdog.cancel()
        stop_group(process)


def stop_group(process):
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(process.pid, sig)
        except (OSError, AttributeError, TypeError):
            pass
        try:
            process.wait(timeout=5)
            return
        except subprocess.TimeoutExpired:
            continue


# ---- the evidence (AC-05, AC-09) ------------------------------------------------------------------------------

def _seconds(text):
    """ps times: [[dd-]hh:]mm:ss[.cc]"""
    days, _, clock = text.rpartition('-')
    total = 0.0
    for part in clock.split(':'):
        total = total * 60 + float(part or 0)
    return total + (int(days) * 86400 if days.isdigit() else 0)


def process_table(run=subprocess.run):
    out = run(['/bin/ps', '-axww', '-o', 'pid=,ppid=,uid=,etime=,time=,%cpu=,rss=,lstart=,command='], capture_output=True,
              text=True, errors='surrogateescape', timeout=20).stdout
    table = {}
    for line in out.splitlines():
        fields = line.split(None, 12)
        if len(fields) < 13 or not fields[0].isdigit():
            continue
        try:
            table[int(fields[0])] = dict(pid=int(fields[0]), ppid=int(fields[1]), uid=int(fields[2]), age=_seconds(fields[3]),
                                         cpu=_seconds(fields[4]), pcpu=float(fields[5]), rss=int(fields[6]) * 1024,
                                         lstart=' '.join(fields[7:12]), command=fields[12])
        except ValueError:
            continue
    return table


def descendants(table, pid):
    children = {}
    for row in table.values():
        children.setdefault(row['ppid'], []).append(row['pid'])
    found, stack = [], [pid]
    while stack:
        current = stack.pop()
        if current in found:
            continue
        found.append(current)
        stack.extend(children.get(current, []))
    return found


def cwds(pids, run=subprocess.run):
    if not pids:
        return {}
    out = run(['/usr/sbin/lsof', '-a', '-d', 'cwd', '-p', ','.join(str(p) for p in pids), '-Fpn'],
              capture_output=True, text=True, errors='surrogateescape', timeout=40).stdout
    found, pid = {}, None
    for line in out.splitlines():
        if line.startswith('p') and line[1:].isdigit():
            pid = int(line[1:])
        elif line.startswith('n') and pid is not None:
            found[pid] = line[1:]
    return found


def launchd_jobs(run=subprocess.run):
    """Pids of the user's launchd jobs. They also have launchd as their parent, but someone set them up to keep
    running (10-31: test-journal's archive worker looked like a leftover)."""
    try:
        out = run(['/bin/launchctl', 'list'], capture_output=True, text=True, timeout=20).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    return {int(line.split()[0]) for line in out.splitlines() if line.split() and line.split()[0].isdigit()}


def orphans(task_roots, table=None, cwd_of=cwds, own=None, managed=None):
    """Process trees whose parent is gone (re-parented to launchd, but not one of its jobs), up a day or more,
    that an agent left behind: a known agent helper, or one working inside a task folder."""
    table = process_table() if table is None else table
    managed = launchd_jobs() if managed is None else managed
    if managed is None:
        return []   # cannot tell a leftover from a service: list none
    own = set(own or ()) | set(managed)
    roots = [r for r in table.values() if r['ppid'] == 1 and r['age'] >= ORPHAN_HOURS * 3600 and r['pid'] not in own
             and not any(word in r['command'] for word in NEVER)]
    where = cwd_of([r['pid'] for r in roots])
    found = []
    for row in roots:
        cwd = where.get(row['pid'], '')
        inside = next((str(root) for root in task_roots if cwd == str(root) or cwd.startswith(str(root).rstrip('/') + '/')), None)
        if not inside and O.companion(row['command']) is None:
            continue
        tree = descendants(table, row['pid'])
        found.append(dict(pid=row['pid'], lstart=row['lstart'], age_hours=round(row['age'] / 3600, 1),
                          rss_mb=round(sum(table[p]['rss'] for p in tree if p in table) / 2 ** 20),
                          cpu_seconds=round(sum(table[p]['cpu'] for p in tree if p in table), 1), processes=len(tree),
                          command=row['command'][:200], cwd=cwd, task=_task_of(cwd, task_roots)))
    return sorted(found, key=lambda o: -o['rss_mb'])


def leftovers(roots, table, cwd_of, managed):
    try:
        return orphans(roots, table, cwd_of, own=[os.getpid()], managed=managed)
    except (OSError, subprocess.SubprocessError):
        return []   # ps or lsof too slow right now: no leftovers listed this time, never a crash


def _task_of(path, task_roots):
    for root in task_roots:
        root = str(root).rstrip('/') + '/'
        if path.startswith(root):
            return root + path[len(root):].split('/', 1)[0]
    return None


def holders(activity, task):
    """Which processes keep a task looking in use, so the agent can match them against the orphans."""
    commands = dict(activity.commands)
    pids = sorted({int(p) for r in T.in_use(activity, task) for p in re.findall(r'PID (\d+)', r)})
    other = [r for r in T.in_use(activity, task) if 'PID' not in r]
    return [dict(pid=p, command=(commands.get(p) or '')[:120]) for p in pids[:12]] + [dict(reason=r) for r in other[:3]]


def gh_binary():
    for path in (shutil.which('gh'), '/opt/homebrew/bin/gh', '/usr/local/bin/gh', str(Path.home() / '.local/bin/gh')):
        if path and Path(path).is_file():
            return path
    return None


def git(repo, *args):
    """git that never stops the material: a slow or broken repository just reads as unknown. On 10-31 the first run
    the agent started by itself died on a 60-second `git log` while memory was critical."""
    try:
        return T.git(repo, *args, timeout=GIT_SECONDS)
    except (OSError, subprocess.SubprocessError):
        return subprocess.CompletedProcess(args, 124, '', '')


def unsaved_work(repo):
    """Like tasks.git_changes, with the short timeout; what cannot be read counts as unsaved work."""
    status = git(repo, 'status', '--porcelain')
    if status.returncode:
        return '读不出状态（太慢或出错），当作有没保存的改动'
    if status.stdout.strip():
        return '有未提交改动'
    ahead = git(repo, 'rev-list', '--count', 'HEAD', '--not', '--remotes')
    if ahead.returncode or not ahead.stdout.strip().isdigit():
        return '读不出状态（太慢或出错），当作有没保存的改动'
    return '有未推送的提交' if int(ahead.stdout.strip()) else None


def pull_request(repo, gh=None, run=subprocess.run):
    """'#12 MERGED', 'no PR', or None when GitHub cannot say."""
    gh = gh or gh_binary()
    branch = git(repo, 'branch', '--show-current').stdout.strip()
    url = git(repo, 'remote', 'get-url', 'origin').stdout.strip()
    name = re.sub(r'\.git$', '', re.sub(r'^.*github\.com[:/]', '', url)) if 'github.com' in url else None
    if not (gh and branch and name):
        return None
    try:
        out = run([gh, 'pr', 'list', '--repo', name, '--head', branch, '--state', 'all', '--json', 'number,state', '--limit', '3'],
                  capture_output=True, text=True, timeout=30)
        listed = json.loads(out.stdout) if out.returncode == 0 else None
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    if listed is None:
        return None
    return ', '.join('#{} {}'.format(p['number'], p['state']) for p in listed) or 'no PR'


def udids_named(task, skipped):
    """Simulator ids a task's own notes mention (.simulator-udid, resources.json, reports)."""
    found = set()
    try:
        files = [p for p in task.real.iterdir() if p.is_file()] + [p for d in task.real.iterdir()
                 if d.is_dir() and d.name not in skipped and not (d / '.git').exists() for p in d.iterdir() if p.is_file()]
    except OSError:
        return found
    for path in files[:200]:
        try:
            if path.stat().st_size < 2 * 2 ** 20:
                found.update(re.findall(r'[0-9A-F]{8}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{12}', path.read_text(errors='ignore')))
        except OSError:
            continue
    return found


def touched(task, repos, deadline=None):
    """When a task was last worked on, cheaply: its last commit, its git index, its top-level entries.
    A walk of the whole tree took minutes on a Mac that was already swapping (10-31)."""
    times = []
    for repo in repos:
        if deadline is not None and time.monotonic() > deadline:
            break   # out of time: the top-level entries still say roughly when
        out = git(repo, 'log', '-1', '--format=%ct')
        if out.returncode == 0 and out.stdout.strip().isdigit():
            times.append(int(out.stdout.strip()))
        index = git(repo, 'rev-parse', '--git-path', 'index').stdout.strip()
        if index:
            try:
                times.append((repo / index if not Path(index).is_absolute() else Path(index)).stat().st_mtime)
            except OSError:
                pass
    try:
        times += [p.lstat().st_mtime for p in task.real.iterdir()]
    except OSError:
        pass
    return max(times) if times else None


def repo_facts(task, repo, prs, deadline=None):
    name = str(repo.relative_to(task.real))
    if deadline is not None and time.monotonic() > deadline:
        return dict(name=name, branch=None, unsaved='没来得及查，当作有没保存的改动', pr=None)
    return dict(name=name, branch=git(repo, 'branch', '--show-current').stdout.strip() or None,
                unsaved=unsaved_work(repo), pr=prs(repo))


def collect(home, cfg, settings, reason, now=None, activity=None, simctl=auto_tier.default_simctl, table=None,
            cwd_of=cwds, prs=pull_request, sizer=None, free=None, report=None, managed=None, budget=COLLECT_SECONDS):
    """The 告急材料: what is wrong, the biggest things with their evidence, and what the user chose before.
    Sizes come from the last checkup (at most 6 hours old) instead of a fresh du of every task."""
    now = time.time() if now is None else now
    activity = activity if activity is not None else Activity.collect(settings.lease_root)
    free = shutil.disk_usage(str(settings.volume)).free if free is None else free
    pins, _ = StorageManager(settings)._pins(now)
    skipped = T.skip_names(cfg)
    if sizer is None:
        report = checkup.load_report(home) if report is None else report
        known = {t['path']: t.get('bytes') for t in (report or {}).get('tasks', [])}
        sizer = lambda real, path=None: known.get(str(path)) if str(path) in known else T.size(real)
    else:
        given = sizer
        sizer = lambda real, path=None: given(real)
    tasks_found, named, work = [], {}, []
    for task in T.discover(cfg):
        if not task.reachable:
            continue
        for udid in udids_named(task, skipped):
            named.setdefault(udid, []).append(task.path.name)
        if task.linked:   # already on the drive: it frees nothing on the internal disk
            tasks_found.append(dict(path=str(task.path), on_drive=True, gib=None, repos=[]))
            continue
        work.append((task, T.repositories(task.real, skipped)))
    deadline = time.monotonic() + budget
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        facts = {(str(task.path), str(repo)): pool.submit(repo_facts, task, repo, prs, deadline) for task, repos in work for repo in repos}
        for task, repos in work:
            try:
                latest = touched(task, repos, deadline)
                tasks_found.append(dict(path=str(task.path), on_drive=False, gib=round((sizer(task.real, task.path) or 0) / GIB, 1),
                                        idle_days=None if latest is None else round((now - latest) / T.DAY, 1),
                                        held_by=holders(activity, task), protected=T.pin_reason(pins, task),
                                        notes=T.loose_files(task, skipped)[:8],
                                        repos=[facts[(str(task.path), str(repo))].result() for repo in repos]))
            except Exception as error:   # one task that cannot be read never costs the whole run
                tasks_found.append(dict(path=str(task.path), on_drive=False, gib=None, repos=[],
                                        unreadable=str(error)[:200] or type(error).__name__))
    sims = []
    try:
        listing = json.loads(simctl(['list', 'devices', '--json']))
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
        listing = {'devices': {}}
    for runtime, devices in listing.get('devices', {}).items():
        for device in devices:
            last = ask_tier.last_used(device)
            sims.append(dict(id=device['udid'], name=device.get('name'), system=ask_tier.runtime_label(runtime),
                             state=device.get('state'), gib=round((device.get('dataPathSize') or 0) / GIB, 1),
                             days_since_used=None if last is None else round((now - last) / T.DAY, 1),
                             attached=ask_tier.attached(device['udid'], activity), tasks=named.get(device['udid'], [])))
    roots = [Path(r['path']) for r in cfg['task_roots']]
    pending = ask_tier.load(home, ask_tier.PENDING, ask_tier.empty_pending())
    seen = watch_state(home)
    try:
        table = process_table() if table is None else table
        jobs = (launchd_jobs() or set()) if managed is None else managed
        groups, helpers = who_now(seen, table, jobs, now)
    except (OSError, subprocess.SubprocessError):
        groups, helpers = [], []
    return dict(
        schema=1, at=now, reason=reason, rgs=str(RGS),
        why=why_words(reason, free, settings.critical_free_gib * GIB, memory_pressure()),
        disk=dict(free_gib=round(free / GIB, 1), goal_gib=settings.warning_free_gib, target_gib=settings.target_free_gib,
                  danger_gib=settings.critical_free_gib,
                  external=str(cfg['external_root']) if cfg['external_root'] else None,
                  external_ready=bool(cfg['external_root']) and auto_tier.volume_ready(cfg['external_root']),
                  external_free_gib=external_free(cfg)),
        memory=dict(pressure={0: '读不到', 1: '正常', 2: '警告', 4: '严重'}.get(memory_pressure(), '未知'), swap=swap_usage()),
        who=[dict(line=O.line(g, now), pids=g['pids'][:8], gib=round(g['bytes'] / GIB, 1), cores=g['cores'], owner=g['owner'])
             for g in groups[:10]],
        companions=helpers, trend=trend(seen.get('samples', []), now), watch_did=watch_did(seen, now),
        orphans=leftovers(roots, table, cwd_of, set() if table is not None and managed is None else managed),
        simulators=sorted(sims, key=lambda s: -s['gib']),
        tasks=sorted(tasks_found, key=lambda t: -(t['gib'] or 0))[:25],
        waiting=[dict(id=i['id'], title=i['title'], state=i['state']) for i in pending['items']],
        decisions=[m for m in agent_plan.memories(home) if m.get('kind') == 'decision'][-40:],
        lessons=[dict(id=m['id'], when=m['rule']['when'], then=m['rule']['then']) for m in agent_plan.lessons(home)],
        old_notes=[dict(id=m['id'], note=(m.get('note') or '')[:200]) for m in agent_plan.old_notes(home)][-20:],
        rules=[dict(id=r['id'], action=r['action'], words=r['words']) for r in agent_plan.describe(home)['rules']],
        steps=list(STEPS))


def external_free(cfg):
    """How much the drive still has, so the agent thinks of it first; None when it is not set or not there."""
    root = cfg.get('external_root')
    if not root or not auto_tier.volume_ready(root):
        return None
    probe = Path(root)
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    try:
        return round(shutil.disk_usage(str(probe)).free / GIB, 1)
    except OSError:
        return None


def watch_state(home):
    try:
        return json.loads((state_root(home) / 'watch.json').read_text())
    except (OSError, ValueError):
        return {}


def who_now(seen, table, managed, now, run=subprocess.run):
    """know-who AC-01 for the material: who holds memory and CPU, and which companions may go and why.
    The watch's agent clocks are used as they are; nothing is written back."""
    agents = seen.get('agents', {})
    hot = {p: now - row['age'] for p, row in table.items() if row.get('pcpu', 0) >= O.BUSY_SHARE * 100
           and row['age'] >= O.BUSY_MINUTES * 60 and row['cpu'] / row['age'] >= O.BUSY_SHARE}
    hot.update({int(p): v[1] for p, v in seen.get('busy', {}).items() if int(p) in table and now - v[1] >= O.BUSY_MINUTES * 60})
    sizes = O.footprints(run)   # swapped and compressed memory too: on a tight Mac most of it is there
    big = O.biggest(table, [p for p in table if 'kernel_task' not in table[p]['command']], managed, sizes)
    top = [p for members in big for p in members]
    mates = [p for p, row in table.items() if O.companion(row['command']) and p not in managed
             and not any(word in row['command'] for word in NEVER)]
    wrappers = {table[p]['ppid'] for p in mates}
    mates = [p for p in mates if p not in wrappers]   # `zsh -c '… stream …'` around the stream: list the stream
    where = O.looks({p for members in big for p in members[:O.LOOK_PER_GROUP]} | set(hot) | set(mates)
                    | {int(p) for p in agents if int(p) in table} | O.users(table, top + list(hot)), run)
    served = {int(p): (v['clients'], v['since']) for p, v in seen.get('served', {}).items()
              if int(p) in table and table[int(p)]['lstart'] == v['lstart']}   # the watch's: who is connected to each server
    groups = O.group(O.describe(table, top + [p for p in hot if p not in top], where, agents, managed, now, sizes=sizes, hot=hot,
                                links=served), now)
    helpers = []
    for pid in mates:
        quiet = seen.get('seen', {}).get(str(pid))
        who = O.owner(table, pid, managed)
        yes, why = O.verdict(table[pid], who, where, agents, quiet and quiet['since'], now)
        row = O.describe(table, [pid], where, agents, managed, now, hot=hot)[0]
        helpers.append(dict(pid=pid, lstart=table[pid]['lstart'], line=O.line(dict(row, title=row['tool'], projects=O.names([row['project']] if row['project'] else [])), now),
                            kind=O.companion(table[pid]['command']), may_stop=yes, why=why or '',
                            mb=round(table[pid]['rss'] / 2 ** 20), command=table[pid]['command'][:200]))
    return groups, sorted(helpers, key=lambda h: (not h['may_stop'], -h['mb']))[:20]


def trend(samples, now):
    """agent-investigates AC-01, AC-06: the last hour of the watch's minutes, in words and figures."""
    hour = [s for s in samples if now - s['at'] <= 3600]
    last10 = [s for s in hour if now - s['at'] <= 600]
    if not hour:
        return dict(words='最近 60 分钟没有记录', calm=None)
    warn, critical = sum(1 for s in hour if s['pressure'] == 2), sum(1 for s in hour if s['pressure'] >= 4)
    bad = sum(1 for s in last10 if s['pressure'] >= 2)
    swaps = [s.get('swap_used') or 0 for s in hour]
    calm = len(last10) >= 8 and bad == 0
    words = '最近 60 分钟：「警告」{} 分钟、「严重」{} 分钟；swap 从 {:.1f} GB 到 {:.1f} GB（最高 {:.1f} GB）；最近 10 分钟{}'.format(
        warn, critical, swaps[0], swaps[-1], max(swaps), '都正常' if calm else '有 {} 分钟不正常'.format(bad) if bad else '记录不够')
    return dict(words=words, calm=calm, warn_minutes=warn, critical_minutes=critical, bad_last10=bad,
                swap_from=swaps[0], swap_to=swaps[-1], swap_max=max(swaps))


def watch_did(seen, now):
    """What the minute-by-minute watch did itself in the last hour, and who waits in its queue."""
    acted = [a for a in seen.get('acted', []) if now - a['at'] <= 3600]
    return dict(stopped=sum(a.get('stopped', 0) for a in acted), paused=sum(a.get('paused', 0) for a in acted),
                names=[n for a in acted for n in a.get('names', [])][:10],
                queued=[q['name'] for q in seen.get('paused', [])])


def judge(home, reason, settings, now=None, free=None, samples=None, swap=None):
    """agent-investigates AC-06: the background's own look when the agent is done, by the watch's measure."""
    now = time.time() if now is None else now
    found = []
    if reason in ('memory', 'asked'):
        recent = trend(watch_state(home).get('samples', []) if samples is None else samples, now)
        if recent['calm'] is None:
            pressure = memory_pressure()
            found.append((pressure >= 2, '后台看：内存压力现在是「{}」'.format({1: '正常', 2: '警告', 4: '严重'}.get(pressure, '读不到'))))
        elif recent['bad_last10']:
            found.append((True, '后台看：最近 10 分钟有 {} 分钟「警告」或「严重」，还紧'.format(recent['bad_last10'])))
        else:
            found.append((not recent['calm'], '后台看：最近 10 分钟都正常' if recent['calm'] else '后台看：记录不够，还不能说缓过来了'))
    if reason in ('disk', 'asked'):
        free = shutil.disk_usage(str(settings.volume)).free if free is None else free
        short = settings.warning_free_gib - free / GIB   # the user's target (`rgs target`); target_free_gib is where tidying stops
        words = '后台看：内置盘还剩 {:.0f} GB，{}'.format(free / GIB, '比目标少 {:.0f} GB'.format(short) if short > 0 else '到目标了')
        swap = swap_usage() if swap is None else swap
        if short > 0 and swap and swap['total_gib'] >= SWAP_DISK_GIB:
            # 11-10: 24 GB of the disk was swap, which the notices never said; it comes back once memory eases.
            words += '（其中 {:.0f} GB 是交换文件：内存不够时系统写在磁盘上的，内存松下来会还回来）'.format(swap['total_gib'])
        found.append((short > 0, words))
    if reason == 'asked' and len(found) == 2 and not found[1][0]:
        found = found[:1]   # asked by the user, disk fine: say only how memory is
    return dict(tight=any(t for t, _ in found), words='；'.join(w for _, w in found))


def receipt(made, carried, result):
    """agent-investigates AC-04: what came back, what was written down, what was done and what was skipped."""
    handed = [e for e in made if e['action'] in ('stop', 'move', 'ask', 'rule')]
    ok = [d for d in result['done'] if d['result'] in ('已关掉', '已挪到外接盘', '已改')]
    later = [d for d in result['done'] if d['result'] == HANDED_WORDS]
    skipped = [d for d in result['done'] if d not in ok and d not in later]
    parts = []
    if carried['received']:
        parts.append('从回复里收到 {} 条，记上 {} 条'.format(carried['received'], len(carried['recorded']))
                     + ('（没记上：{}）'.format('；'.join(r['why'] for r in carried['refused'][:3])) if carried['refused'] else ''))
    if handed:
        parts.append('交回 {} 条决定，照做 {} 条'.format(len(handed), len(ok))
                     + ('，{} 条交给资源看板自己去挪'.format(len(later)) if later else '')
                     + ('，跳过 {} 条（{}）'.format(len(skipped), '；'.join(sorted({d['result'] for d in skipped}))[:120]) if skipped else '')
                     + ('，{} 条列进「等你确认」'.format(len(result['asks'])) if result['asks'] else ''))
    learned = result.get('learned', [])
    if learned:
        fresh = sum(1 for m in learned if not m.get('replaces'))
        parts.append('记下 {} 条经验'.format(len(learned)) + ('（{} 条是改写旧的）'.format(len(learned) - fresh) if fresh < len(learned) else ''))
    words = '；'.join(parts) or '没有交回要做的决定'
    return dict(handed=len(handed), done=len(ok), skipped=[dict(what=d.get('pid') or d.get('path'), why=d['result']) for d in skipped],
                asks=len(result['asks']), learned=len(learned), carried=carried, words=words)


# ---- the agent's commands: they only write down decisions (CH-02) --------------------------------------------

def run_folder(cwd=None, env=None, home=None):
    env = os.environ if env is None else env
    # The conversation no longer runs in the run's folder (CH-07), and a turn the Codex App runs has none of our
    # variables (CH-08): the run going on is in the state.
    current = (load_state(Path.home() if home is None else home).get('run') or {}).get('folder')
    for candidate in (env.get(DIR_ENV), current, cwd or os.getcwd()):
        if candidate and (Path(candidate) / MATERIAL).is_file():
            return Path(candidate)
    raise ValueError('这里不是一次告急处理：资源看板叫你来时才有告急材料')


def material(folder):
    return json.loads((Path(folder) / MATERIAL).read_text())


def show(folder, home=None, now=None):
    """`emergency show`: the material, first saying when it was made and whether it is this run's (AC-01)."""
    now = time.time() if now is None else now
    found = material(folder)
    made = '这份材料是 {} 生成的（{}前）'.format(time.strftime('%m-%d %H:%M', time.localtime(found['at'])), O.span(now - found['at']))
    current = (load_state(Path.home() if home is None else home).get('run') or {}).get('folder')
    if current and Path(current) != Path(folder):
        made += '；这不是这次的材料，这次的在 ' + current
    return dict(generated=made, **found)


LESSON_HOW = '经验要写成以后也用得上的规则：--when "遇到什么情况" --do "该怎么做、为什么"；这一次做了什么写进 done --summary'
ONCE = re.compile(r'本轮|这一轮|上一轮|上轮|这次|上次|(?<!\d)\d{1,2}:\d{2}(?!\d)')


DISK_KINDS = ('simulator-delete', 'task-delete', 'path-delete')
CHECKED = 'done-checked'   # the first `done` of a run was read back to the agent (done_check)


def said(entry):
    """One handed-back decision in a few words, as the agent is told it."""
    if entry['action'] == 'stop':
        return '关掉 PID {}'.format(entry.get('pid'))
    if entry['action'] == 'move':
        return '挪走 ' + Path(entry.get('path') or '').name
    if entry['action'] == 'rule':
        return '把规则 {} 改成 {}'.format(entry.get('rule'), entry.get('to'))
    return '列给用户：' + str(entry.get('title') or entry.get('id'))


def done_check(folder, found):
    """11-10: a summary said two services went to the background that never did, and a run 23 GB short of the
    target never looked at the disk. The first `done` of a run reads back what was really handed back; when the
    disk is still short and nothing would free it, or nothing was handed back at all, it is refused once, so the
    agent can add what it meant or confirm. Returns the words, or None."""
    folder = Path(folder)
    if found.get('reason') == 'daily' or (folder / CHECKED).exists():
        return None
    made = [e for e in decisions(folder) if e['action'] in ('stop', 'move', 'ask', 'rule')]
    disk = found.get('disk') or {}
    goal = disk.get('goal_gib') or disk.get('target_gib')
    short = goal - disk['free_gib'] if goal and isinstance(disk.get('free_gib'), (int, float)) else 0
    gaps = []
    if short > 0 and not any(e['action'] == 'move' or e.get('kind') in DISK_KINDS for e in made):
        gaps.append('内置盘还差 {:.0f} GB 到目标，你这轮没有交回任何腾磁盘的决定（move，或 ask simulator-delete、task-delete、'
                    'path-delete）。用 du 查查是什么在占，比如 `du -xhd1 ~ ~/Library ~/Library/Developer 2>/dev/null | sort -h | tail -20`；'
                    '能挪的挪，能删的列给用户；确实没有，就在 summary 里写清查了哪里、为什么都不能动。'.format(short))
    if not made and found.get('reason') in ('disk', 'memory'):
        gaps.append('你这轮一个决定都没交回，后台什么也不会做。')
    if not gaps:
        return None
    try:
        (folder / CHECKED).write_text('')
    except OSError:
        return None   # a read-only turn: the done itself goes back in the reply
    return ('先别收尾。后台只照你交回的决定做，你这轮交回的是：{}。总结里说要关、要挪、要列给用户的，不在这里就都不会发生。'
            .format('；'.join(said(e) for e in made) or '没有') + ''.join(gaps)
            + '要补就现在补，补完再运行一次 done；确认就是这些，原样再运行一次 done。')


def decide(folder, action, target=None, why='', note='', summary='', cause='', need='', when='', then='', replaces='',
           checked=False):
    """One decision of the agent's, checked against the material it was given. `checked`: a decision carried
    back in a reply, after the run (done_check has nothing more to ask by then)."""
    folder = Path(folder)
    found = material(folder)
    entry = dict(at=time.time(), action=action)
    if action in ('stop', 'move', 'ask', 'rule') and not why.strip():
        raise ValueError('要写依据：--why "…"')
    if action == 'stop':
        orphan = next((o for o in found['orphans'] if str(o['pid']) == str(target)), None)
        mate = next((c for c in found.get('companions', []) if str(c['pid']) == str(target) and c['may_stop']), None)
        if orphan is None and mate is None:
            raise ValueError('{} 不在告急材料里能直接关的（orphans，或 companions 里 may_stop 的）；主人还在的，'
                             '用 `emergency ask process-stop:{}` 列给用户'.format(target, target))
        if orphan is not None:
            entry.update(pid=orphan['pid'], lstart=orphan['lstart'], cpu_seconds=orphan['cpu_seconds'], why=why)
        else:
            entry.update(pid=mate['pid'], lstart=mate['lstart'], companion=True, why=why)
    elif action == 'move':
        task = next((t for t in found['tasks'] if t['path'] == str(target)), None)
        if task is None or task['on_drive']:
            raise ValueError('{} 不是告急材料里还在内置盘上的任务'.format(target))
        entry.update(path=task['path'], why=why)
    elif action == 'ask':
        kind, _, key = str(target or '').partition(':')
        if kind not in ASK_KINDS:
            raise ValueError('只能列这几类：' + '、'.join(ASK_KINDS))
        if kind == 'process-stop':
            entry.update(asked_process(found, key), id=target, kind=kind, why=why)
        elif kind == 'path-delete':
            entry.update(asked_path(key), id='path-delete:' + str(Path(key).expanduser()), kind=kind, why=why)
        elif kind.startswith('simulator'):
            sim = next((s for s in found['simulators'] if s['id'] == key), None)
            if sim is None:
                raise ValueError('告急材料里没有这台模拟器：' + key)
            entry.update(id=target, kind=kind, title='{}（{}）'.format(sim['name'], sim['system']),
                         bytes=int(sim['gib'] * GIB) if kind == 'simulator-delete' else 0, why=why)
        else:
            task = next((t for t in found['tasks'] if t['path'] == key), None)
            if task is None:
                raise ValueError('告急材料里没有这个任务：' + key)
            if kind == 'task-move' and task['on_drive']:
                raise ValueError('{} 已经在外接盘上了'.format(key))
            entry.update(id=target, kind=kind, title=Path(key).name + ('（在外接盘上）' if task['on_drive'] else ''),
                         bytes=int((task['gib'] or 0) * GIB), why=why)
    elif action == 'rule':
        spec = agent_plan.RULES.get(str(target))
        if spec is None:
            raise ValueError('没有这条规则：{}；可选：{}'.format(target, '、'.join(agent_plan.RULES)))
        if note not in spec['actions']:
            raise ValueError('「{}」只能设成：{}（用 --decision 写）'.format(spec['title'], '、'.join(spec['actions'])))
        entry.update(rule=str(target), to=note, why=why)
    elif action == 'lesson':
        when, then, replaces = when.strip(), then.strip(), replaces.strip()
        if not (when and then):
            raise ValueError(LESSON_HOW)
        once = ONCE.search(when + ' ' + then)
        if once:
            raise ValueError('「{}」只属于这一次。'.format(once.group(0)) + LESSON_HOW)
        known = {m['id'] for m in found.get('lessons', []) + found.get('old_notes', [])}
        if replaces and replaces not in known:
            raise ValueError('材料里没有这条经验：' + replaces)
        entry.update(when=when, then=then, **({'replaces': replaces} if replaces else {}))
    elif action == 'done':
        if not summary.strip():
            raise ValueError('要写结论：--summary "…"')
        if not cause.strip():
            raise ValueError('要先写为什么紧：--cause "…"（查下来不紧，就写不紧的依据）')
        nudge = None if checked else done_check(folder, found)
        if nudge:
            raise ValueError(nudge)
        entry.update(summary=summary, cause=cause, **({'need': need} if need.strip() else {}))
    else:
        raise ValueError('没有这个动作：' + str(action))
    line = json.dumps(entry, ensure_ascii=False)
    try:
        with (folder / DECISIONS).open('a') as out:
            out.write(line + '\n')
    except OSError:   # the Codex App ran this turn read-only (CH-08)
        raise ValueError(UNWRITABLE + CARRY + line)
    if action == 'done':   # what the background will do, said back once more
        return dict(entry, handed=[said(e) for e in decisions(folder) if e['action'] in ('stop', 'move', 'ask', 'rule')])
    return entry


def asked_path(key, home=None):
    """path-delete (11-10): anything big the agent found, for the user to say yes to. Only inside the home folder
    and never the home folder's own top level, never a code repository; how big, measured now."""
    home = Path.home() if home is None else Path(home)
    path = Path(os.path.abspath(os.path.expanduser(key)))
    inside = path.relative_to(home) if path.is_relative_to(home) else None
    if inside is None or len(inside.parts) < 2:
        raise ValueError('path-delete 只能列主目录里、至少两层深的路径（{} 不行）'.format(path))
    if not path.exists() and not path.is_symlink():
        raise ValueError('不存在：{}'.format(path))
    if (path / '.git').exists():
        raise ValueError('{} 是代码仓库：任务用 task-delete，挪走用 move'.format(path))
    return dict(title='{}（在 {}）'.format(path.name, path.parent), bytes=T.size(path) or 0, path=str(path))


def asked_process(found, key, table=None):
    """A process the agent wants gone while its owner is still around: the user decides (AC-03). Never an App
    itself, a database, a container, a debugger or a simulator itself."""
    if not key.isdigit():
        raise ValueError('process-stop 后面接进程号：process-stop:<pid>')
    try:
        table = process_table() if table is None else table
    except (OSError, subprocess.SubprocessError):
        table = {}
    if not table:   # the agent's sandbox may not run ps (11-04, Codex): what the material saw, checked again later
        return seen_process(found, int(key))
    row = table.get(int(key))
    if row is None:
        raise ValueError('进程 {} 已经不在了'.format(key))
    who = O.owner(table, int(key), launchd_jobs() or set())
    if who.get('self') or any(word in row['command'] for word in NEVER) or O.device(table, int(key)):
        raise ValueError('{} 是 App 本身、数据库、容器、调试器或模拟器，不能列'.format(O.tool(row['command'])))
    place = O.project(dict(O.looks([int(key)])).get(int(key), (None, False))[0])
    agents = watch_state(Path.home()).get('agents', {})
    title = '{}（{}{}）'.format(O.tool(row['command']), O.fact(who, row, agents, time.time()), '，' + place['name'] if place else '')
    return dict(title=title, bytes=row['rss'], pid=int(key), lstart=row['lstart'])


def seen_process(found, pid):
    """A process the material listed, for an agent that cannot read the process list itself. settle_asks() checks
    it in the background, where ps runs, before it reaches 「等你确认」."""
    group = next((g for g in found.get('who', []) if pid in g.get('pids', [])), None)
    mate = next((c for c in found.get('companions', []) if c['pid'] == pid), None)
    if group is None and mate is None:
        raise ValueError('进程 {} 不在告急材料的 who 和 companions 里（这里查不了进程表）'.format(pid))
    if group is not None and ((group.get('owner') or {}).get('self') or group['line'].startswith('iOS 模拟器')):
        raise ValueError('{} 是 App 本身或模拟器，不能列'.format(group['line'].split(' · ')[0]))
    return dict(title=(mate or group)['line'], bytes=0, pid=pid, lstart=mate and mate['lstart'], seen_at=found.get('at', 0))


def settle_asks(home, entries, table=None):
    """Process asks written where ps could not run: still there, started before the agent looked, and not an App,
    a database or a simulator. Only those reach 「等你确认」; the rest are dropped."""
    loose = [e for e in entries if e.get('kind') == 'process-stop' and not e.get('lstart')]
    if not loose:
        return entries
    try:
        table = process_table() if table is None else table
    except (OSError, subprocess.SubprocessError):
        table = {}
    now, kept = time.time(), [e for e in entries if e not in loose]
    for entry in loose:
        row = table.get(entry['pid'])
        if row is None or now - row['age'] > entry.get('seen_at', 0) + 60:   # gone, or a new process on that number
            continue
        try:
            settled = asked_process({}, str(entry['pid']), table)
        except ValueError:
            continue
        kept.append(dict(entry, title=settled['title'], bytes=settled['bytes'], lstart=settled['lstart']))
    return kept


def carry(folder, said):
    """Decisions the agent could not write come back in its reply, one line each; checked again like any other.
    Returns the receipt of that: how many lines, which were written down, which not and why (AC-04)."""
    key = lambda entry: json.dumps({k: v for k, v in entry.items() if k != 'at'}, sort_keys=True)
    have = {key(entry) for entry in decisions(folder)}
    receipt = dict(received=0, recorded=[], refused=[])
    for line in (said or '').splitlines():
        line = line.strip().strip('`').strip()
        if not line.startswith(CARRY):
            continue
        receipt['received'] += 1
        try:
            entry = json.loads(line[len(CARRY):])
            if key(entry) in have:
                receipt['recorded'].append(entry)
                continue
            target = (entry.get('id') if entry.get('action') == 'ask' else entry.get('rule') if entry.get('action') == 'rule'
                      else entry.get('pid') or entry.get('path') or entry.get('id'))
            receipt['recorded'].append(decide(folder, entry['action'], target, why=entry.get('why', ''),
                                              note=entry.get('to', '') if entry.get('action') == 'rule' else entry.get('note', ''),
                                              summary=entry.get('summary', ''),
                                              cause=entry.get('cause', ''), need=entry.get('need', ''),
                                              when=entry.get('when', ''), then=entry.get('then', ''),
                                              replaces=entry.get('replaces', ''), checked=True))
            have.add(key(entry))
        except (ValueError, KeyError, TypeError, AttributeError) as error:
            receipt['refused'].append(dict(line=line[len(CARRY):][:120], why=(str(error) or type(error).__name__).split('\n')[0][:120]))
    return receipt


def decisions(folder):
    found = []
    try:
        lines = (Path(folder) / DECISIONS).read_text().splitlines()
    except OSError:
        return found
    for line in lines:
        try:
            found.append(json.loads(line))
        except ValueError:
            continue
    return found


# ---- after the agent: recheck the floor, then do it (AC-06~08, AC-10) -----------------------------------------

def stop_orphan(entry, table=None, kill=os.kill, sleep=time.sleep, managed=None):
    """Stop it only if it is still the same orphan, a day or more old, and idle since the agent looked."""
    table = process_table() if table is None else table
    row = table.get(entry['pid'])
    if row is None:
        return '已经不在了'
    if row['lstart'] != entry['lstart']:
        return '进程号已被别的程序用了，没动'
    if row['ppid'] != 1 or row['age'] < ORPHAN_HOURS * 3600:
        return '已经有人接管它了，没动'
    jobs = launchd_jobs() if managed is None else managed
    if jobs is None or entry['pid'] in jobs:
        return '它是 launchd 管着的服务，没动'
    tree = descendants(table, entry['pid'])
    cpu = sum(table[p]['cpu'] for p in tree if p in table)
    if cpu - entry.get('cpu_seconds', cpu) > ORPHAN_CPU_SECONDS:
        return '这段时间它还在干活，没动'
    return stop_tree(table, entry['pid'], entry['lstart'], kill=kill, sleep=sleep)


def stop_tree(table, pid, lstart, kill=os.kill, sleep=time.sleep):
    """End a process and what it started, children first, if it is still the same process. None once done."""
    row = table.get(pid)
    if row is None:
        return '已经不在了'
    if row['lstart'] != lstart:
        return '进程号已被别的程序用了，没动'
    tree = descendants(table, pid)
    for sig in (signal.SIGTERM, signal.SIGKILL):
        for each in reversed(tree):
            try:
                kill(each, sig)
            except OSError:
                pass
        sleep(3)
        if not any(alive(p) for p in tree):
            break
    return None


def stop_companion(home, entry, table=None, kill=os.kill, sleep=time.sleep):
    """A companion the agent chose: still the same process, and its owner still gone or still (know-who AC-03)."""
    table = process_table() if table is None else table
    row = table.get(entry['pid'])
    if row is None:
        return '已经不在了'
    if row['lstart'] != entry['lstart']:
        return '进程号已被别的程序用了，没动'
    jobs = launchd_jobs()
    if jobs is None:
        return '这会儿查不清它是不是系统服务，没动'
    seen = watch_state(home)
    quiet = seen.get('seen', {}).get(str(entry['pid']))
    where = O.looks([entry['pid']] + [int(p) for p in seen.get('agents', {}) if int(p) in table])
    yes, why = O.verdict(row, O.owner(table, entry['pid'], jobs), where, seen.get('agents', {}), quiet and quiet['since'], time.time())
    if not yes:
        return why or '它的主人又在动了，没动'
    return stop_tree(table, entry['pid'], entry['lstart'], kill=kill, sleep=sleep)


def move_task(home, cfg, settings, path, activity, pins, unsaved_ok=False):
    """「挪到外接盘」's floor without its idle days: unused, nothing unsaved, a drive that is there and big enough.
    `unsaved_ok`: the user said yes to moving it with its unsaved work (task-move): nothing is lost, it only goes
    to the drive with the rest."""
    task = next((t for t in T.discover(cfg) if str(t.path) == str(path)), None)
    if task is None or task.linked:
        return '已经不在内置盘上了'
    if task.external is None:
        return '这个任务目录没有设外接盘'
    keep = agent_plan.kept(agent_plan.load(home), task.path)
    if keep:
        return '先别动：' + (keep.get('why') or '计划里写着')
    reasons = list(T.in_use(activity, task)) + ([T.pin_reason(pins, task)] if T.pin_reason(pins, task) else [])
    if reasons:
        return '；'.join(sorted(set(reasons))[:3])
    unsaved = None if unsaved_ok else auto_tier.unsaved(task, cfg)
    if unsaved:
        return unsaved
    if not auto_tier.volume_ready(task.external):
        return '外接盘没插'
    denied = auto_tier.external_access(task.external)
    if denied:
        return denied
    bytes_ = T.size(task.real) or 0
    probe = Path(task.external)
    while not probe.exists():
        probe = probe.parent
    if shutil.disk_usage(str(probe)).free < bytes_ * 1.1 + GIB:
        return '外接盘空间不够'
    try:
        auto_tier.move(home, task)
    except (auto_tier.Aborted, OSError, subprocess.SubprocessError) as error:
        return str(error)
    return None


def hand_over(home, entry, now):
    path = state_root(home) / HANDED
    queue = [q for q in read_list(path) if q.get('path') != entry['path']]
    atomic_json(path, queue + [dict(path=entry['path'], why=entry.get('why', ''), at=now)])


def read_list(path):
    try:
        found = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return []
    return found if isinstance(found, list) else []


def handed_moves(home, cfg, settings, now=None, activity_factory=None):
    """The App's wake (every 5 minutes) makes the moves a run handed over, each rechecked like the run would have.
    Still refused here too: kept for the next wake, a day at most. Returns what was done, or None."""
    now = time.time() if now is None else now
    path = state_root(home) / HANDED
    queue = read_list(path)
    if not queue:
        return None
    activity_factory = activity_factory or (lambda: Activity.collect(settings.lease_root))
    pins, _ = StorageManager(settings)._pins(now)
    keep, done = [], []
    for entry in queue:
        if now - entry.get('at', 0) > HANDED_HOURS * 3600:
            continue
        activity = activity_factory()
        result = '这会儿查不清哪些程序在用它' if activity.errors else move_task(home, cfg, settings, entry['path'], activity, pins)
        if result == auto_tier.DENIED:
            keep.append(entry)
        else:
            done.append(dict(entry, result=result or '已挪到外接盘'))
    atomic_json(path, keep)
    moved = [d for d in done if d['result'] == '已挪到外接盘']
    left = [d for d in done if d not in moved]
    if done:
        words = ['、'.join(Path(d['path']).name for d in moved) + ' 已挪到外接盘'] if moved else []
        words += ['{} 没挪：{}'.format(Path(d['path']).name, d['result']) for d in left[:2]]
        agent_plan.notice(home, 'agent 定的 {} 个闲置任务挪到了外接盘'.format(len(moved)) if moved else 'agent 定的挪动没做成',
                          '；'.join(words)[:300], tab='cover')
    return done


def maybe_asks(home, folder, settings, now, free=None):
    """The disk still short of the target and the agent listed nothing: the biggest shut-down simulators nobody is
    using join 「等你确认」 anyway, so the user hears what could go (11-05: the agent listed nothing at 1 GB free
    while 23 GB of simulators sat unused). Only a list: the user's yes runs it and becomes one of their decisions."""
    free = shutil.disk_usage(str(settings.volume)).free if free is None else free
    short = settings.warning_free_gib * GIB - free
    if short <= 0:
        return []
    plan = agent_plan.load(home)
    picked = []
    for sim in material(folder).get('simulators', []):   # biggest first
        key, days = 'simulator-delete:' + sim['id'], sim.get('days_since_used')
        if (sim.get('state') != 'Shutdown' or sim.get('attached') or days is None or days < MAYBE_SIM_DAYS
                or not sim.get('gib') or ask_tier.left_alone(plan, key, {})):
            continue
        picked.append(dict(id=key, kind='simulator-delete', title='{}（{}）'.format(sim['name'], sim['system']),
                           bytes=int(sim['gib'] * GIB),
                           why='关机 {:.0f} 天没开过，没有开发工具连着；内置盘还差 {:.0f} GB'.format(days, short / GIB)))
        if sum(p['bytes'] for p in picked) >= short or len(picked) >= MAYBE_MAX:
            break
    return add_asks(home, picked, now, prefix='后台补列：')


def add_asks(home, entries, now, prefix='agent：'):
    """The agent's asks join 「等你确认」, marked as its own, with its reasons (AC-07)."""
    pending = ask_tier.load(home, ask_tier.PENDING, ask_tier.empty_pending())
    items = {i['id']: i for i in pending['items']}
    added = []
    for entry in entries:
        old = items.get(entry['id'])
        if old and old['state'] in ('running', 'ignored'):
            continue   # ignored once already: the user's no stands until the object changes
        path = entry['id'].split(':', 1)[1]
        items[entry['id']] = dict(id=entry['id'], kind=entry['kind'], title=entry['title'], bytes=entry['bytes'],
                                  reason=prefix + entry['why'], loses=ask_tier.LOSES[entry['kind']], by='agent', at=now,
                                  fingerprint='agent:{:.0f}'.format(now), state='pending', result=None, updated=now,
                                  **({'path': path} if path.startswith('/') else {}),
                                  **({'lstart': entry['lstart']} if entry.get('lstart') else {}))
        added.append(items[entry['id']])
    pending['items'] = sorted(items.values(), key=lambda i: (i['kind'], i['title']))
    pending['updated'] = now
    ask_tier.save(home, ask_tier.PENDING, pending)
    return added


def apply(home, cfg, settings, folder, now=None, table=None, activity_factory=None):
    """Do what the agent decided, each step rechecked; returns what happened for the receipt and the notice."""
    now = time.time() if now is None else now
    activity_factory = activity_factory or (lambda: Activity.collect(settings.lease_root))
    made = decisions(folder)
    done = []
    for entry in (e for e in made if e['action'] == 'stop'):
        skipped = stop_companion(home, entry, table) if entry.get('companion') else stop_orphan(entry, table)
        done.append(dict(entry, result=skipped or '已关掉'))
    pins, _ = StorageManager(settings)._pins(now)
    for entry in (e for e in made if e['action'] == 'move'):
        activity = activity_factory()
        skipped = '这会儿查不清哪些程序在用它' if activity.errors else move_task(home, cfg, settings, entry['path'], activity, pins)
        if skipped == auto_tier.DENIED:
            # This run was started by the per-minute memory job, which macOS keeps off the drive; the App's own
            # wake can reach it (11-04: a move the agent chose was dropped here and never tried again).
            hand_over(home, entry, now)
            skipped = HANDED_WORDS
        done.append(dict(entry, result=skipped or '已挪到外接盘'))
    for entry in (e for e in made if e['action'] == 'rule'):
        try:
            agent_plan.set_rule(home, entry['rule'], entry['to'], by='agent', why=entry['why'])
            done.append(dict(entry, result='已改'))
        except (OSError, ValueError) as error:
            done.append(dict(entry, result=str(error).split('\n')[0][:120]))
    asks = add_asks(home, settle_asks(home, [e for e in made if e['action'] == 'ask'], table), now)
    learned = []
    for entry in (e for e in made if e['action'] == 'lesson' and e.get('when')):
        try:
            learned.append(agent_plan.learn(home, 'emergency', entry['when'], entry['then'], entry.get('replaces')))
        except (OSError, ValueError):
            pass
    last = next((e for e in reversed(made) if e['action'] == 'done'), {})
    return dict(done=done, asks=[dict(id=a['id'], title=a['title'], bytes=a['bytes']) for a in asks],
                summary=last.get('summary', ''), cause=last.get('cause', ''), need=last.get('need', ''),
                learned=[dict(id=m['id'], when=m['rule']['when'], then=m['rule']['then'], replaces=m.get('replaces')) for m in learned])


def notice_words(result, agent_result):
    stopped = [d for d in result['done'] if d['action'] == 'stop' and d['result'] == '已关掉']
    judged = result.get('judged') or {}
    moved = [d for d in result['done'] if d['action'] == 'move' and d['result'] == '已挪到外接盘']
    parts = []
    if stopped:
        parts.append('关掉了 {} 个没人管的残留后台'.format(len(stopped)))
    if moved:
        parts.append('把 {} 个闲置任务挪到了外接盘'.format(len(moved)))
    later = [d for d in result['done'] if d['action'] == 'move' and d['result'] == HANDED_WORDS]
    if later:
        parts.append('{} 个闲置任务交给资源看板自己挪到外接盘'.format(len(later)))
    if result['asks']:
        parts.append('还有 {} 项等你确认，约 {:.0f} GB'.format(len(result['asks']), sum(a['bytes'] for a in result['asks']) / GIB))
    if agent_result.get('status') == 'timeout':
        parts.append('agent 超过 {} 分钟没处理完，已停下'.format(RUN_MINUTES))
    elif agent_result.get('status') == 'error':
        parts.append('agent 没跑起来，点一下交给它')
    body = '；'.join(parts) or (result['summary'] or '这次没找到能安全处理的')
    if result['summary'] and parts:
        body += '。' + result['summary']
    if judged.get('words'):
        body = judged['words'] + '。' + body
    if result.get('need'):
        body += '。要你做：' + result['need']
    return body[:300]


def run(home, cfg, settings, reason, folder, runner, now=None, **hooks):
    """One emergency run. If it breaks (10-31: the material would not save), the run is cleared at once instead of
    blocking the next one for half an hour, and the user gets the 「交给 agent」 notice."""
    try:
        return _run(home, cfg, settings, reason, folder, runner, now, **hooks)
    except Exception as error:
        words = str(error) or type(error).__name__
        state = load_state(home)
        undo_fired(state)
        state.pop('run', None)
        state['last'] = dict(at=time.time(), reason=reason, folder=str(folder), agent='crashed', error=words[:300])
        save_state(home, state)
        atomic_json(Path(folder) / RESULT, dict(at=time.time(), reason=reason, error=words[:2000]))
        atomic_json(state_root(home) / 'agent-request.json',
                    dict(at=time.time(), title={'disk': '磁盘快满了', 'memory': '内存吃紧'}.get(reason, '资源看板告急处理'),
                         reason='告急处理没跑起来', prompt=space_plan.AGENT_PROMPT))
        raise


def _run(home, cfg, settings, reason, folder, runner, now=None, popen=subprocess.Popen, applier=apply, collector=collect,
         judger=judge):
    """Hand the agent its material, let it decide, then do what passes the floor (AC-04~10)."""
    now = time.time() if now is None else now
    folder = Path(folder)
    main = reason == 'daily'
    if not (folder / MATERIAL).is_file():
        atomic_json(folder / MATERIAL, daily_material(home, settings, now) if main else collector(home, cfg, settings, reason, now))
        if not main:
            snapshots(folder, popen)
    env = dict(os.environ, **{DIR_ENV: str(folder)})
    before = load_state(home)
    streak = (before.get('run') or {}).get('streak', 1)
    when = time.strftime('%m-%d %H:%M', time.localtime(now))
    prompt = (DAILY_PROMPT.format(when=when, rgs=RGS, folder=folder) if main else
              PROMPT.format(when=when, why=WHY.get(reason, reason), rgs=RGS, folder=folder))
    last = before.get('last') or {}
    run_now = before.get('run') or {}
    if not main and run_now.get('new') and run_now.get('looked_at'):
        prompt = NEW.format(when=time.strftime('%H:%M', time.localtime(run_now['looked_at'])), what='；'.join(run_now['new'])) + prompt
    if not main and streak >= 2:
        prompt = STREAK.format(n=streak, why=WHY.get(reason, reason)) + (IDLE if last.get('handed') == 0 else '') + prompt
    if not main and last.get('receipt'):
        prompt = LAST.format(when=time.strftime('%m-%d %H:%M', time.localtime(last['at'])), receipt=last['receipt'],
                             judged=last.get('judged', '')) + prompt
    launcher = run_claude if runner == 'claude' else run_codex
    try:
        agent_result = launcher(home, folder, prompt, env, RUN_MINUTES * 60, popen=popen, **({'main': True} if main else {}))
    except Exception as error:   # whatever went wrong starting it, what it already decided is still handled
        agent_result = dict(agent=runner, status='error', reply=str(error) or type(error).__name__)
    carried = carry(folder, agent_result.pop('said', ''))
    lock = state_root(home) / 'dashboard' / 'operation.lock'
    lock.parent.mkdir(parents=True, exist_ok=True)
    with lock.open('a') as busy:
        fcntl.flock(busy, fcntl.LOCK_EX)   # the dashboard or the tiers may be cleaning: wait for them
        result = applier(home, cfg, settings, folder, now=time.time())
        if reason in ('disk', 'asked') and not result['asks']:
            result['asks'] = [dict(id=a['id'], title=a['title'], bytes=a['bytes'])
                              for a in maybe_asks(home, folder, settings, time.time())]
    result['receipt'] = receipt(decisions(folder), carried, result)
    if main:
        return _after_daily(home, folder, reason, agent_result, result)
    result['judged'] = judger(home, reason, settings)
    atomic_json(folder / RESULT, dict(at=time.time(), reason=reason, agent=agent_result, streak=streak, **result))
    title = 'agent 处理了告急' if agent_result.get('status') == 'done' else '告急处理没做完'
    if result['judged']['tight'] and streak >= 2:
        title = 'agent 处理了 {} 次还没缓过来'.format(streak)   # AC-05: the user hears it
    short = settings.warning_free_gib * GIB - shutil.disk_usage(str(settings.volume)).free
    if reason in ('disk', 'asked') and short > 0:
        # Still short of the user's target: what the agent worked out is put to them, loudly (11-10).
        title = ('离目标还差 {:.0f} GB：agent 出了方案，等你看' if result['asks'] or result['need'] else
                 '离目标还差 {:.0f} GB：agent 这次没找到能腾的').format(short / GIB)
    brief = notice_words(result, agent_result)
    # AC-11: with a conversation to open, the notice opens it (and offers 「去确认」 when something waits).
    talk = (folder / SESSION).is_file()
    tab = ('emergency-ask' if result['asks'] else 'emergency') if talk else ('pending' if result['asks'] else 'cover')
    agent_plan.notice(home, title, brief, tab=tab)
    if agent_result.get('status') == 'error':
        atomic_json(state_root(home) / 'agent-request.json',
                    dict(at=time.time(), title={'disk': '磁盘快满了', 'memory': '内存吃紧'}.get(reason, '资源看板告急处理'),
                         reason='agent 没能自己启动', prompt=space_plan.AGENT_PROMPT))
    state = load_state(home)
    if agent_result.get('status') == 'error':
        undo_fired(state)
    state.pop('run', None)
    state['last'] = dict(at=time.time(), reason=reason, folder=str(folder), agent=agent_result.get('status'),
                         runner=runner, summary=result['summary'], brief=brief, asks=len(result['asks']), talk=talk,
                         receipt=result['receipt']['words'], judged=result['judged']['words'], tight=result['judged']['tight'],
                         streak=streak, cause=result['cause'], need=result['need'],
                         handed=result['receipt']['handed'] + result['receipt']['learned'])
    save_state(home, state)
    ask_tier.launch_app()
    return dict(agent=agent_result, **result)


def snapshots(folder, popen=subprocess.Popen):
    """The raw facts beside the material (SNAPSHOTS); one that cannot be taken is left out."""
    if popen is not subprocess.Popen:
        return   # a test's stand-in for the agent: no real readings
    for name, command in SNAPSHOTS:
        try:
            out = subprocess.run(command, capture_output=True, text=True, timeout=60)
            if out.returncode == 0 and out.stdout:
                # A whole command line is enough to tell what it is; past 500 characters it is mostly arguments.
                (Path(folder) / name).write_text('\n'.join(line[:500] for line in out.stdout.splitlines()) + '\n')
        except (OSError, subprocess.SubprocessError):
            continue


def _after_daily(home, folder, reason, agent_result, result):
    """The main agent's look is said only when the user has something to decide (it found a problem)."""
    result['judged'] = dict(tight=False, words='')
    atomic_json(Path(folder) / RESULT, dict(at=time.time(), reason=reason, agent=agent_result, streak=1, **result))
    if result['need']:
        agent_plan.notice(home, '资源看板的主 agent 看了这一天', ('要你做：' + result['need'] + '。' + result['summary'])[:300])
    state = load_state(home)
    state.pop('run', None)
    state['daily'] = dict(at=time.time(), folder=str(folder), agent=agent_result.get('status'), summary=result['summary'],
                          need=result['need'], learned=len(result['learned']))
    save_state(home, state)
    return dict(agent=agent_result, **result)


def past_rounds(home, since=0):
    """Each emergency run's receipt since `since`, for the main agent's daily look."""
    found = []
    for folder in sorted((state_root(home) / RUNS).glob('2*')):
        try:
            got = json.loads((folder / RESULT).read_text())
        except (OSError, ValueError):
            continue
        at = got.get('at')
        if got.get('reason') == 'daily' or not isinstance(at, (int, float)) or at < since:
            continue
        told = got.get('receipt') or {}
        found.append(dict(when=time.strftime('%m-%d %H:%M', time.localtime(at)), reason=got.get('reason'),
                          agent=(got.get('agent') or {}).get('status'), handed=told.get('handed', 0), learned=told.get('learned', 0),
                          done=[dict(what=d.get('pid') or d.get('path'), result=d.get('result')) for d in got.get('done') or []][:8],
                          asks=len(got.get('asks') or []), summary=(got.get('summary') or '')[:240],
                          need=(got.get('need') or '')[:160], judged=(got.get('judged') or {}).get('words', '')))
    return found


def daily_material(home, settings, now):
    """What the main agent looks over: the runs since its last look, the user's decisions and the plan's changes."""
    since = (load_state(home).get('daily') or {}).get('at') or now - 86400
    free = shutil.disk_usage(str(settings.volume)).free
    return dict(
        schema=1, at=now, reason='daily', rgs=str(RGS), why='每天一次：看各轮告急处理做得对不对', since=since,
        rounds=past_rounds(home, since),
        user_decisions=[m for m in agent_plan.memories(home) if m.get('kind') == 'decision' and (m.get('at') or 0) >= since][-60:],
        plan_changes=[c for c in agent_plan.changes(home) if (c.get('at') or 0) >= since][-30:],
        disk=dict(free_gib=round(free / GIB, 1), target_gib=settings.target_free_gib, danger_gib=settings.critical_free_gib),
        memory=dict(pressure={0: '读不到', 1: '正常', 2: '警告', 4: '严重'}.get(memory_pressure(), '未知'), swap=swap_usage()),
        decisions=[m for m in agent_plan.memories(home) if m.get('kind') == 'decision'][-40:],
        lessons=[dict(id=m['id'], when=m['rule']['when'], then=m['rule']['then']) for m in agent_plan.lessons(home)],
        old_notes=[dict(id=m['id'], note=(m.get('note') or '')[:200]) for m in agent_plan.old_notes(home)][-20:],
        rules=[dict(id=r['id'], action=r['action'], words=r['words']) for r in agent_plan.describe(home)['rules']],
        orphans=[], companions=[], tasks=[], simulators=[], waiting=[], steps=list(DAILY_STEPS))


def daily_due(home, state, now):
    """The main agent's daily look (11-10): once a day from DAILY_HOUR, and only when there are runs or decisions of
    the user's since its last look."""
    last = state.get('daily_fired') or 0
    if running(state, now) or now - last < DAILY_GAP_HOURS * 3600 or time.localtime(now).tm_hour < DAILY_HOUR:
        return False
    since = (state.get('daily') or {}).get('at') or now - 86400
    return bool(past_rounds(home, since)) or any((m.get('at') or 0) >= since for m in agent_plan.memories(home)
                                                 if m.get('kind') == 'decision')


def new_folder(home, now):
    folder = state_root(home) / RUNS / time.strftime('%Y%m%d-%H%M%S', time.localtime(now))
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def prune_runs(home, keep=KEEP_RUNS):
    runs = sorted(p for p in (state_root(home) / RUNS).glob('*') if p.is_dir())
    for old in runs[:-keep]:
        shutil.rmtree(old, ignore_errors=True)


SERVICE = Path(__file__).resolve().parents[2] / 'scripts/service.py'


def spawn(reason, folder, runner):
    """The run outlives this wake of the background: its own session, its own log."""
    log = (Path(folder) / 'log.txt').open('a')
    process = subprocess.Popen([sys.executable, str(SERVICE), 'emergency', 'run', runner, '--reason', reason,
                                '--path', str(folder)], stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                               start_new_session=True)
    return process.pid


def tick(home, cfg, settings, now=None, asked=False, run=subprocess.run, env=None, starter=spawn, free=None):
    """Each wake of the background (every 5 minutes): when this Mac is about to choke, start the user's agent
    (AC-01~04). `asked`: the user asked for a run now. Returns what happened, or None."""
    now = time.time() if now is None else now
    if not asked and not cfg.get('emergency_agent', True):
        return None
    free = shutil.disk_usage(str(settings.volume)).free if free is None else free
    critical = settings.critical_free_gib * GIB
    pressure = memory_pressure(run)
    state = load_state(home)
    current = view(watch_state(home), pressure, now)
    reason = due(state, free, critical, pressure, now, current, goal=settings.warning_free_gib * GIB)
    remind(home, state, free, settings.warning_free_gib * GIB, now)
    if asked:
        reason = None if running(state, now) else 'asked'
    for stale in ('starts', 'capped', 'capped_told'):    # left by builds that counted runs a day
        state.pop(stale, None)
    if reason is None and daily_due(home, state, now):
        reason = 'daily'
    if reason is None:
        save_state(home, state)
        return None
    prev = {key: state.get(key) for key in ('memory_fired_at', 'disk_fired', 'looked')}
    if reason == 'daily':
        state['daily_fired'] = now
    else:
        mark_fired(state, reason, free, critical, now)
    ready = runners(home, cfg['agent_app'], env, run)
    if not ready and reason == 'daily':
        save_state(home, state)   # nobody to look: tried again tomorrow
        return None
    if not ready:
        # Nobody can start by itself: hand it to the user instead (the disk drop already has its own notice),
        # and say once a day what is missing, e.g. one sign-in to Claude Code (AC-12).
        if reason == 'memory':
            atomic_json(state_root(home) / 'agent-request.json', dict(at=now, title='内存吃紧', prompt=space_plan.AGENT_PROMPT,
                                                                      reason=why_words(reason, free, critical, pressure)))
        missing = readiness(home, cfg, env, run)
        today = time.strftime('%Y-%m-%d', time.localtime(now))
        if missing['missing'] == 'claude-login' and state.get('login_hint_day') != today:
            state['login_hint_day'] = today
            agent_plan.notice(home, '告急了，agent 没法自己来', '还差一步：登录一次 Claude Code，以后告急它就能自己来。点一下去登录。',
                              tab='login')
        ask_tier.launch_app()
        state['missed'] = dict(at=now, reason=reason, missing=missing['missing'])
        save_state(home, state)
        return dict(reason=reason, agent=None)
    folder = new_folder(home, now)
    prune_runs(home)
    pid = starter(reason, folder, ready[0])
    last = state.get('last') or {}
    if reason == 'daily':
        state['run'] = dict(pid=pid, started=now, reason=reason, folder=str(folder), agent=ready[0], prev=prev, streak=1)
        save_state(home, state)
        return dict(reason=reason, agent=ready[0], folder=str(folder), pid=pid)
    again = last.get('tight') and last.get('reason') == reason and now - last.get('at', 0) <= STREAK_HOURS * 3600
    state['run'] = dict(pid=pid, started=now, reason=reason, folder=str(folder), agent=ready[0], prev=prev,
                        streak=last.get('streak', 1) + 1 if again else 1, new=news(state.get('looked'), current)[:5],
                        looked_at=(state.get('looked') or {}).get('at'))
    state['looked'] = current
    save_state(home, state)
    return dict(reason=reason, agent=ready[0], folder=str(folder), pid=pid)


def can_start(home, cfg, env=None, run=subprocess.run):
    """Whether a disk drop will bring the agent by itself, so the 「交给 agent」 notice is not needed."""
    return cfg.get('emergency_agent', True) and bool(runners(home, cfg['agent_app'], env, run))


def readiness(home, cfg, env=None, run=subprocess.run):
    """Whether this Mac's agent can start by itself, and if not, the one thing missing (AC-12)."""
    ready = runners(home, cfg['agent_app'], env, run)
    if ready:
        return dict(ready=True, agent=ready[0], missing=None, words='告急时 {} 会自己来'.format({'claude': 'Claude', 'codex': 'Codex'}[ready[0]]))
    if claude_binary(home):
        return dict(ready=False, agent='claude', missing='claude-login', words='还差一步：登录一次 Claude Code（`emergency login`）')
    if codex_binary(home):
        return dict(ready=False, agent='codex', missing='codex-login', words='还差一步：在 Codex 里登录一次')
    return dict(ready=False, agent=None, missing='agent', words='这台 Mac 上没找到 Claude 桌面 App 或 Codex')


def command_file(path, lines):
    """A .command file Terminal opens and runs: no Automation permission to ask for."""
    Path(path).write_text('#!/bin/sh\n' + '\n'.join(lines) + '\n')
    Path(path).chmod(0o755)
    return Path(path)


def open_session(home, now=None, run=subprocess.run):
    """AC-11: open the conversation of the run going on, or of the last one: Codex in its App, Claude in Terminal."""
    now = time.time() if now is None else now
    state = load_state(home)
    current = state.get('run') if running(state, now) else None
    folder = (current or state.get('last') or {}).get('folder')
    try:
        session = json.loads((Path(folder) / SESSION).read_text()) if folder else None
    except (OSError, ValueError):
        session = None
    if session is None and folder:   # runs from before session.json: the thread is in the result
        try:
            agent = json.loads((Path(folder) / RESULT).read_text()).get('agent') or {}
            session = dict(agent='codex', thread=agent['thread']) if agent.get('thread') else None
        except (OSError, ValueError):
            session = None
    if session is None:
        return dict(status='none', message='这一轮还没开始对话，过一会儿再点' if current else '还没有 agent 处理过告急')
    if session['agent'] == 'codex':
        run(['/usr/bin/open', 'codex://threads/' + session['thread']], capture_output=True, timeout=30)
        return dict(status='ok', agent='codex', message='已在 Codex 里打开这次的对话')
    if current:
        return dict(status='busy', agent='claude', message='Claude 正在后台处理，处理完再点就能在终端里接着聊')
    script = command_file(Path(folder) / 'continue.command',
                          ['cd {} || exit 1'.format(shlex.quote(session['cwd'])),
                           'exec {} --resume {}'.format(shlex.quote(session['binary']), shlex.quote(session['session']))])
    run(['/usr/bin/open', '-a', 'Terminal', str(script)], capture_output=True, timeout=30)
    return dict(status='ok', agent='claude', message='已在终端里接着这次的对话')


def login(home, run=subprocess.run):
    """AC-12: one sign-in to Claude Code in Terminal (the browser does the rest), so emergencies need nobody."""
    binary = claude_binary(home)
    if binary is None:
        return dict(status='missing', message='没找到 Claude Code：装好 Claude 桌面 App 后再试')
    state_root(home).mkdir(parents=True, exist_ok=True)
    script = command_file(state_root(home) / 'claude-login.command',
                          ['echo "登录一次 Claude Code，资源看板告急时就能自己叫它来。浏览器里授权完，这个窗口可以关掉。"',
                           'exec {} auth login'.format(shlex.quote(str(binary)))])
    run(['/usr/bin/open', '-a', 'Terminal', str(script)], capture_output=True, timeout=30)
    return dict(status='ok', message='已打开终端：在浏览器里授权一次就好')

