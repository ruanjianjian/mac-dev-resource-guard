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
import plan as space_plan
import tasks as T

STATE, RUNS, MATERIAL, DECISIONS, RESULT = 'emergency.json', 'emergency', 'material.json', 'decisions.jsonl', 'result.json'
SESSION = 'session.json'      # which conversation this run is, so the panel can open it (emergency-agent AC-11)
DIR_ENV = 'RESOURCE_GUARD_EMERGENCY_DIR'
DEEPER_GIB = (10, 5)          # after the danger line, one more run at each of these (AC-01)
WARN_MINUTES = 30             # memory pressure 「警告」 this long counts as a squeeze (AC-02)
MEMORY_QUIET_HOURS = 6
DAY_LIMIT = 4                 # runs in any 24 hours (AC-03)
RUN_MINUTES = 30              # a run that takes longer is stopped (AC-10)
GIT_SECONDS = 15              # one git call while gathering the material; slower reads as unknown
COLLECT_SECONDS = 180         # the whole material; past this, the rest of the repositories are not looked at
ORPHAN_HOURS = 24
ORPHAN_CPU_SECONDS = 2.0      # CPU an orphan may burn between the look and the stop and still count as idle
ASK_KINDS = ('simulator-delete', 'simulator-shutdown', 'task-delete')
# Background helpers agents leave behind: app-server brokers, MCP servers, REPLs, log tails and searches.
AGENT_HINTS = ('app-server-broker', 'codex app-server', 'mcp-server', 'mcp_server', 'node_repl',
               'tail -n +1 -F', 'ugrep ', 'baguette stream', 'log stream')
# Never an orphan to stop, whatever its parent: this tool itself, and the user's databases, containers and
# debuggers (SKILL.md 内存: no killing of those).
NEVER = ('resource-guard', 'ResourceDashboard', 'dev-memory', 'postgres', 'mongod', 'redis-server', 'mysqld',
         'docker', 'com.docker', 'qemu', 'lldb', 'debugserver', 'launchd_sim', 'Simulator.app')
RGS = Path.home() / '.local/bin/resource-guard-service'
PROMPT = ('用 mac-dev-resource-guard 这个 skill 处理一次告急：先运行 `{rgs} emergency show`，照里面的 steps 做。'
          '这次是资源看板自己把你叫来的，用户不在场；全程用简体中文。')
STEPS = (
    '目标是这台 Mac 不卡：内置盘回到目标以上，内存不再吃紧。计划里的规则是起点，不是边界：规则没碰到的大头，你来判断。',
    '先看 memories：用户以前确认过、忽略过、在对话里说过的，是这台 Mac 的偏好。照着它出单；用户说过别动的，不要列。',
    '没人管的残留后台（orphans：父进程已退出、放了一天以上）占着内存，还会让任务目录看起来像在用。'
    '看它开在哪个任务里、那个任务的 PR 合没合、改动推没推；判断没人用了，就 `emergency stop <pid> --why "依据"`。',
    '闲置任务挪到外接盘不丢东西：判断短期不会再用、改动都推了的，`emergency move <路径> --why "依据"`。',
    '会丢东西的（删模拟器、关模拟器、收尾任务）只能列给用户：`emergency ask <kind>:<id> --why "依据"`，'
    'kind 是 simulator-delete、simulator-shutdown 或 task-delete。依据写证据，比如「PR #12 已合入，改动都推了，3 天没开」。',
    '这一轮学到的新情况，`emergency lesson --note "…"` 记一条。',
    '最后 `emergency done --summary "三句话以内"`：做了什么、列了什么等用户点头、还差多少。你的命令只记下决定，'
    '后台会在你结束后逐项复核安全底线再做：正在用的、有没推送改动的、被保护的，一律不动。',
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


def due(state, free, critical, pressure, now):
    """Why the agent should come now ('disk' or 'memory'), or None. Updates `state` in place (AC-01~03)."""
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
    elif (pressure >= 4 or pressure >= 2 and now - state.get('warn_since', now) >= WARN_MINUTES * 60) \
            and now - state.get('memory_fired_at', 0) >= MEMORY_QUIET_HOURS * 3600:
        reason = 'memory'
    if reason is None or running(state, now):
        return None
    starts = [t for t in state.get('starts', []) if now - t < 86400]
    state['starts'] = starts
    if len(starts) >= DAY_LIMIT:
        return None
    return reason


def mark_fired(state, reason, free, critical, now):
    """Only runs the background started count toward the daily limit; a run the user asked for does not."""
    if reason == 'disk':
        state['disk_fired'] = disk_level(free, critical)
    elif reason == 'memory':
        state['memory_fired_at'] = now
    if reason in ('disk', 'memory'):
        state['starts'] = state.get('starts', []) + [now]


def why_words(reason, free, critical, pressure):
    if reason == 'disk':
        return '内置盘只剩 {:.1f} GB，低于危险线 {:.0f} GB'.format(free / GIB, critical / GIB)
    if reason == 'memory':
        return '内存吃紧（系统内存压力：{}）'.format('严重' if pressure >= 4 else '警告，已超过半小时')
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


def run_claude(home, folder, prompt, env, timeout, popen=subprocess.Popen):
    """Claude Code headless, allowed only the emergency commands and reading (AC-04). Its session id is chosen
    up front, so the panel can open the conversation in Terminal afterwards (AC-11)."""
    session = str(uuid.uuid4())
    binary = claude_binary(home)
    note_session(folder, agent='claude', session=session, binary=str(binary), cwd=str(folder))
    command = [str(binary), '-p', prompt, '--session-id', session, '--output-format', 'json', '--permission-mode', 'dontAsk',
               '--allowedTools', 'Bash({} emergency:*)'.format(RGS), 'Read', 'Skill', '--max-turns', '60']
    process = popen(command, cwd=str(folder), env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT, text=True, start_new_session=True)
    try:
        out, _ = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        stop_group(process)
        return dict(agent='claude', status='timeout')
    try:
        reply = json.loads(out.strip().splitlines()[-1])
        return dict(agent='claude', status='error' if reply.get('is_error') else 'done', session=reply.get('session_id'),
                    reply=str(reply.get('result') or '')[:2000])
    except (ValueError, IndexError):
        return dict(agent='claude', status='error', reply=(out or '')[-2000:])


def run_codex(home, folder, prompt, env, timeout, popen=subprocess.Popen):
    """A Codex thread the user can open in the Codex App, sandboxed to the run's folder (AC-04, CH-02)."""
    process = popen([str(codex_binary(home)), 'app-server'], cwd=str(folder), env=env, stdin=subprocess.PIPE,
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

    thread = None
    try:
        asked = send('initialize', dict(clientInfo=dict(name='resource-guard', title='资源看板', version='1'),
                                        capabilities=dict(experimentalApi=False)))
        wait(lambda m: m.get('id') == asked)
        send('initialized', {}, notify=True)
        params = dict(cwd=str(folder), approvalPolicy='never', sandbox='workspace-write', ephemeral=False, threadSource='user')
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
        note_session(folder, agent='codex', thread=thread)
        send('thread/name/set', dict(threadId=thread, name='资源看板告急处理'))
        asked = send('turn/start', dict(threadId=thread, input=[dict(type='text', text=prompt, text_elements=[])]))
        done = wait(lambda m: m.get('method') == 'turn/completed' and (m.get('params') or {}).get('threadId') == thread)
        return dict(agent='codex', status='done', thread=thread, turn=((done.get('params') or {}).get('turn') or {}).get('status'))
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
    out = run(['/bin/ps', '-axww', '-o', 'pid=,ppid=,etime=,time=,rss=,lstart=,command='], capture_output=True,
              text=True, errors='surrogateescape', timeout=20).stdout
    table = {}
    for line in out.splitlines():
        fields = line.split(None, 10)
        if len(fields) < 11 or not fields[0].isdigit():
            continue
        try:
            table[int(fields[0])] = dict(pid=int(fields[0]), ppid=int(fields[1]), age=_seconds(fields[2]),
                                         cpu=_seconds(fields[3]), rss=int(fields[4]) * 1024,
                                         lstart=' '.join(fields[5:10]), command=fields[10])
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
        if not inside and not any(word in row['command'] for word in AGENT_HINTS):
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
    return dict(
        schema=1, at=now, reason=reason, rgs=str(RGS),
        why=why_words(reason, free, settings.critical_free_gib * GIB, memory_pressure()),
        disk=dict(free_gib=round(free / GIB, 1), target_gib=settings.target_free_gib, danger_gib=settings.critical_free_gib,
                  external=str(cfg['external_root']) if cfg['external_root'] else None,
                  external_ready=bool(cfg['external_root']) and auto_tier.volume_ready(cfg['external_root'])),
        memory=dict(pressure={0: '读不到', 1: '正常', 2: '警告', 4: '严重'}.get(memory_pressure(), '未知'), swap=swap_usage()),
        orphans=leftovers(roots, table, cwd_of, set() if table is not None and managed is None else managed),
        simulators=sorted(sims, key=lambda s: -s['gib']),
        tasks=sorted(tasks_found, key=lambda t: -(t['gib'] or 0))[:25],
        waiting=[dict(id=i['id'], title=i['title'], state=i['state']) for i in pending['items']],
        memories=agent_plan.memories(home)[-60:],
        rules=[dict(id=r['id'], action=r['action'], words=r['words']) for r in agent_plan.describe(home)['rules']],
        steps=list(STEPS))


# ---- the agent's commands: they only write down decisions (CH-02) --------------------------------------------

def run_folder(cwd=None, env=None):
    env = os.environ if env is None else env
    for candidate in (env.get(DIR_ENV), cwd or os.getcwd()):
        if candidate and (Path(candidate) / MATERIAL).is_file():
            return Path(candidate)
    raise ValueError('这里不是一次告急处理：资源看板叫你来时才有告急材料')


def material(folder):
    return json.loads((Path(folder) / MATERIAL).read_text())


def decide(folder, action, target=None, why='', note='', summary=''):
    """One decision of the agent's, checked against the material it was given."""
    folder = Path(folder)
    found = material(folder)
    entry = dict(at=time.time(), action=action)
    if action in ('stop', 'move', 'ask') and not why.strip():
        raise ValueError('要写依据：--why "…"')
    if action == 'stop':
        orphan = next((o for o in found['orphans'] if str(o['pid']) == str(target)), None)
        if orphan is None:
            raise ValueError('{} 不在告急材料的残留后台（orphans）里'.format(target))
        entry.update(pid=orphan['pid'], lstart=orphan['lstart'], cpu_seconds=orphan['cpu_seconds'], why=why)
    elif action == 'move':
        task = next((t for t in found['tasks'] if t['path'] == str(target)), None)
        if task is None or task['on_drive']:
            raise ValueError('{} 不是告急材料里还在内置盘上的任务'.format(target))
        entry.update(path=task['path'], why=why)
    elif action == 'ask':
        kind, _, key = str(target or '').partition(':')
        if kind not in ASK_KINDS:
            raise ValueError('只能列这几类：' + '、'.join(ASK_KINDS))
        if kind.startswith('simulator'):
            sim = next((s for s in found['simulators'] if s['id'] == key), None)
            if sim is None:
                raise ValueError('告急材料里没有这台模拟器：' + key)
            entry.update(id=target, kind=kind, title='{}（{}）'.format(sim['name'], sim['system']),
                         bytes=int(sim['gib'] * GIB) if kind == 'simulator-delete' else 0, why=why)
        else:
            task = next((t for t in found['tasks'] if t['path'] == key), None)
            if task is None:
                raise ValueError('告急材料里没有这个任务：' + key)
            entry.update(id=target, kind=kind, title=Path(key).name + ('（在外接盘上）' if task['on_drive'] else ''),
                         bytes=int((task['gib'] or 0) * GIB), why=why)
    elif action == 'lesson':
        if not note.strip():
            raise ValueError('要写下学到了什么：--note "…"')
        entry.update(note=note)
    elif action == 'done':
        if not summary.strip():
            raise ValueError('要写结论：--summary "…"')
        entry.update(summary=summary)
    else:
        raise ValueError('没有这个动作：' + str(action))
    with (folder / DECISIONS).open('a') as out:
        out.write(json.dumps(entry, ensure_ascii=False) + '\n')
    return entry


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
    for sig in (signal.SIGTERM, signal.SIGKILL):
        for pid in reversed(tree):
            try:
                kill(pid, sig)
            except OSError:
                pass
        sleep(3)
        if not any(alive(p) for p in tree):
            break
    return None


def move_task(home, cfg, settings, path, activity, pins):
    """「挪到外接盘」's floor without its idle days: unused, nothing unsaved, a drive that is there and big enough."""
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
    unsaved = auto_tier.unsaved(task, cfg)
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


def add_asks(home, entries, now):
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
                                  reason='agent：' + entry['why'], loses=ask_tier.LOSES[entry['kind']], by='agent', at=now,
                                  fingerprint='agent:{:.0f}'.format(now), state='pending', result=None, updated=now,
                                  **({'path': path} if path.startswith('/') else {}))
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
        skipped = stop_orphan(entry, table)
        done.append(dict(entry, result=skipped or '已关掉'))
    pins, _ = StorageManager(settings)._pins(now)
    for entry in (e for e in made if e['action'] == 'move'):
        activity = activity_factory()
        skipped = '这会儿查不清哪些程序在用它' if activity.errors else move_task(home, cfg, settings, entry['path'], activity, pins)
        done.append(dict(entry, result=skipped or '已挪到外接盘'))
    asks = add_asks(home, [e for e in made if e['action'] == 'ask'], now)
    for entry in (e for e in made if e['action'] == 'lesson'):
        try:
            agent_plan.remember(home, 'lesson', 'emergency', note=entry['note'])
        except (OSError, ValueError):
            pass
    summary = next((e['summary'] for e in reversed(made) if e['action'] == 'done'), '')
    return dict(done=done, asks=[dict(id=a['id'], title=a['title'], bytes=a['bytes']) for a in asks], summary=summary)


def notice_words(result, agent_result):
    stopped = [d for d in result['done'] if d['action'] == 'stop' and d['result'] == '已关掉']
    moved = [d for d in result['done'] if d['action'] == 'move' and d['result'] == '已挪到外接盘']
    parts = []
    if stopped:
        parts.append('关掉了 {} 个没人管的残留后台'.format(len(stopped)))
    if moved:
        parts.append('把 {} 个闲置任务挪到了外接盘'.format(len(moved)))
    if result['asks']:
        parts.append('还有 {} 项等你确认，约 {:.0f} GB'.format(len(result['asks']), sum(a['bytes'] for a in result['asks']) / GIB))
    if agent_result.get('status') == 'timeout':
        parts.append('agent 超过 {} 分钟没处理完，已停下'.format(RUN_MINUTES))
    elif agent_result.get('status') == 'error':
        parts.append('agent 没跑起来，点一下交给它')
    body = '；'.join(parts) or (result['summary'] or '这次没找到能安全处理的')
    if result['summary'] and parts:
        body += '。' + result['summary']
    return body[:240]


def run(home, cfg, settings, reason, folder, runner, now=None, **hooks):
    """One emergency run. If it breaks (10-31: the material would not save), the run is cleared at once instead of
    blocking the next one for half an hour, and the user gets the 「交给 agent」 notice."""
    try:
        return _run(home, cfg, settings, reason, folder, runner, now, **hooks)
    except Exception as error:
        words = str(error) or type(error).__name__
        state = load_state(home)
        state.pop('run', None)
        state['last'] = dict(at=time.time(), reason=reason, folder=str(folder), agent='crashed', error=words[:300])
        save_state(home, state)
        atomic_json(Path(folder) / RESULT, dict(at=time.time(), reason=reason, error=words[:2000]))
        atomic_json(state_root(home) / 'agent-request.json',
                    dict(at=time.time(), title={'disk': '磁盘快满了', 'memory': '内存吃紧'}.get(reason, '资源看板告急处理'),
                         reason='告急处理没跑起来', prompt=space_plan.AGENT_PROMPT))
        raise


def _run(home, cfg, settings, reason, folder, runner, now=None, popen=subprocess.Popen, applier=apply, collector=collect):
    """Hand the agent its material, let it decide, then do what passes the floor (AC-04~10)."""
    now = time.time() if now is None else now
    folder = Path(folder)
    if not (folder / MATERIAL).is_file():
        atomic_json(folder / MATERIAL, collector(home, cfg, settings, reason, now))
    env = dict(os.environ, **{DIR_ENV: str(folder)})
    prompt = PROMPT.format(rgs=RGS)
    launcher = run_claude if runner == 'claude' else run_codex
    try:
        agent_result = launcher(home, folder, prompt, env, RUN_MINUTES * 60, popen=popen)
    except Exception as error:   # whatever went wrong starting it, what it already decided is still handled
        agent_result = dict(agent=runner, status='error', reply=str(error) or type(error).__name__)
    lock = state_root(home) / 'dashboard' / 'operation.lock'
    lock.parent.mkdir(parents=True, exist_ok=True)
    with lock.open('a') as busy:
        fcntl.flock(busy, fcntl.LOCK_EX)   # the dashboard or the tiers may be cleaning: wait for them
        result = applier(home, cfg, settings, folder, now=time.time())
    atomic_json(folder / RESULT, dict(at=time.time(), reason=reason, agent=agent_result, **result))
    title = 'agent 处理了告急' if agent_result.get('status') == 'done' else '告急处理没做完'
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
    state.pop('run', None)
    state['last'] = dict(at=time.time(), reason=reason, folder=str(folder), agent=agent_result.get('status'),
                         runner=runner, summary=result['summary'], brief=brief, asks=len(result['asks']), talk=talk)
    save_state(home, state)
    ask_tier.launch_app()
    return dict(agent=agent_result, **result)


def new_folder(home, now):
    folder = state_root(home) / RUNS / time.strftime('%Y%m%d-%H%M%S', time.localtime(now))
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def prune_runs(home, keep=20):
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
    reason = due(state, free, critical, pressure, now)
    if asked:
        reason = None if running(state, now) else 'asked'
    if reason is None:
        save_state(home, state)
        return None
    mark_fired(state, reason, free, critical, now)
    ready = runners(home, cfg['agent_app'], env, run)
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
    state['run'] = dict(pid=pid, started=now, reason=reason, folder=str(folder), agent=ready[0])
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

