"""know-who: whose is each busy process — the tool, the project, who started it, and whether they are still around.

On 11-03 the board showed a row of `node` and 「在用 · 不动」, and two screen streams burned a core each for days
while the agent sessions that started them sat open (RAW-40). Nothing here is about one Mac (RAW-41): owners are
told apart by the programs Mac developers use, projects by git, and no other process's environment is read — it
often holds secrets.
"""
from pathlib import Path
import os
import plistlib
import re
import subprocess

AGENT_QUIET_HOURS = 6         # an agent this long without moving has left what it started (know-who AC-03)
AGENT_MOVED_SECONDS = 1.0     # its own CPU in a minute when it does anything; idle Claude Code uses up to 0.5 (11-03)
ORPHAN_COMPANION_HOURS = 6
ORPHAN_ANYWHERE_HOURS = 24  # while an agent that may work in any folder moves (the Codex App's server runs from /)
BUSY_SHARE = 0.8              # of a core: 「占满」 (AC-04)
BUSY_MINUTES = 30
COMPANION_SHARE = 0.5         # a companion this busy is worth stopping while memory is fine (AC-03)
TOP = 12
MINE = os.getuid()
# Never stopped, whatever its owner: this tool itself, and the user's databases, containers and debuggers
# (SKILL.md 内存: no killing of those).
NEVER = ('resource-guard', 'ResourceDashboard', 'dev-memory', 'postgres', 'mongod', 'redis-server', 'mysqld',
         'docker', 'com.docker', 'qemu', 'lldb', 'debugserver', 'launchd_sim', 'Simulator.app')

AGENTS = {'claude': 'Claude Code', 'codex': 'Codex', 'gemini': 'Gemini CLI', 'aider': 'Aider', 'opencode': 'opencode',
          'cursor-agent': 'Cursor Agent', 'amp': 'Amp', 'goose': 'Goose', 'qwen': 'Qwen Code', 'crush': 'Crush'}
AGENT_PACKAGES = (('@anthropic-ai/claude-code', 'Claude Code'), ('@openai/codex', 'Codex'), ('@google/gemini-cli', 'Gemini CLI'),
                  ('opencode-ai', 'opencode'), ('@sourcegraph/amp', 'Amp'), ('aider-chat', 'Aider'))
INTERPRETERS = re.compile(r'^(?:node|bun|deno|ruby|[Pp]ython(?:3(?:\.\d+)?)?)$')
TERMINAL_APPS = {'Terminal': 'Terminal', 'iTerm': 'iTerm2', 'iTerm2': 'iTerm2', 'Warp': 'Warp', 'Ghostty': 'Ghostty',
                 'kitty': 'kitty', 'Alacritty': 'Alacritty', 'WezTerm': 'WezTerm', 'Hyper': 'Hyper', 'Tabby': 'Tabby'}
MULTIPLEXERS = {'tmux': 'tmux', 'screen': 'screen', 'zellij': 'zellij'}
EDITOR_APPS = {'Visual Studio Code': 'VS Code', 'Code': 'VS Code', 'Visual Studio Code - Insiders': 'VS Code', 'Cursor': 'Cursor',
               'Windsurf': 'Windsurf', 'Zed': 'Zed', 'Xcode': 'Xcode', 'Xcode-beta': 'Xcode', 'Sublime Text': 'Sublime Text',
               'Nova': 'Nova', 'Android Studio': 'Android Studio', 'Fleet': 'Fleet'}
JETBRAINS = ('IntelliJ IDEA', 'PyCharm', 'WebStorm', 'GoLand', 'CLion', 'RustRover', 'Rider', 'PhpStorm', 'RubyMine', 'DataGrip')
# Helpers that are only any use while someone watches them (AC-03). Dev servers are not among them: one may be
# open in a browser right now.
COMPANIONS = (
    ('日志流', re.compile(r'(?:^|[\s/])log stream\b|(?:^|[\s/])tail\s+(?:\S+\s+)*?-[A-Za-z]*[fF]\b|(?:^|[\s/])adb\b.*\blogcat\b'
                       r'|\bidevicesyslog\b')),
    ('画面流', re.compile(r'\bsimctl\s+io\s+\S+\s+recordVideo\b|\sstream\s+(?:\S+\s+)*?--udid\b|\bscrcpy\b.*--no-(?:window|display|playback)\b')),
    ('盯着改动的编译测试', re.compile(r'\b(?:tsc|vue-tsc|webpack|rollup|esbuild|jest|vitest|babel)\b[^|;]*\s(?:--watch|-w|--watchAll)(?:\s|=|$)'
                               r'|\bvitest\s+watch\b|(?:^|[\s/])(?:nodemon|watchexec|fswatch|chokidar)\b')),
    ('语言服务', re.compile(r'tsserver|typescript-language-server|\bgopls\b|rust-analyzer|sourcekit-lsp|pyright-langserver'
                        r'|basedpyright|\bjdtls\b|\bclangd\b|lua-language-server|vscode-[\w-]+-language-server')),
    ('agent 帮手', re.compile(r'app-server-broker|\bcodex app-server\b|mcp-server|mcp_server|@modelcontextprotocol/|\bnode_repl\b'
                            r'|(?:^|/)(?:ugrep|rg)\s')),
)
# A shell Claude Code's Bash tool started: one left to launchd is a background task of a session (11-05: a vitest
# run of one, writing to its log, read 「没人管了」).
CLAUDE_SHELL = re.compile(r'/\.claude/shell-snapshots/snapshot-')
# The Mac's own services, and anything another user (root) runs.
SYSTEM = re.compile(r'^/(?:System|usr/libexec|usr/sbin|sbin)/|^/Library/(?:Apple|SystemExtensions|Developer/PrivateFrameworks)/'
                    r'|\.xpc/Contents/MacOS/')
SERVERS = ('语言服务', 'agent 帮手')   # they answer clients: only after hours without any CPU of their own
# A simulator's own processes sit under its launchd_sim, or run from its folder; both name the device (AC-06).
DEVICE = re.compile(r'/CoreSimulator/Devices/([0-9A-F]{8}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{12})/')
OWNER_ORDER = ('agent', 'editor', 'terminal', 'app')   # who speaks for a simulator several tools are connected to
FRIENDLY = {'node': 'node', 'Python': 'python', 'python3': 'python'}


def bundle(command):
    """The App a process is part of: its program sits in the App's Contents/MacOS, or in a helper App inside it
    ('/Applications/Cursor.app/Contents/Frameworks/Cursor Helper.app/Contents/MacOS/…' is Cursor). A tool that only
    ships inside an App, like Xcode's make or git, is not the App."""
    match = re.search(r'/([^/]+)\.app/Contents/(?:(?:Frameworks|Library|PlugIns|SharedSupport)/.+?\.app/Contents/)?MacOS/', command)
    return match.group(1) if match else None


def program(command):
    """The executable's own name, spaces in App paths allowed."""
    match = re.search(r'\.app/Contents/MacOS/(.+?)(?=\s+-|\s*$)', command)
    if match:
        first = match.group(1).split(' ')[0]
        return first if first in AGENTS else match.group(1)   # `…/MacOS/codex app-server`: the program is codex
    first = command.split()[0] if command.split() else ''
    return Path(first).name


VALUE_FLAGS = ('-r', '--require', '--import', '--loader', '--experimental-loader', '--env-file', '--conditions', '-C')


def script(command):
    """What an interpreter runs: its first argument that is neither a flag nor a flag's value."""
    parts = command.split()[1:]
    skip = False
    for index, part in enumerate(parts):
        if skip:
            skip = False
        elif part in VALUE_FLAGS:
            skip = True
        elif part == '-m' and index + 1 < len(parts):
            return parts[index + 1]   # python -m module
        elif not part.startswith('-'):
            return part
    return ''


def agent_name(command):
    name = program(command)
    if name in AGENTS:
        return AGENTS[name]
    if INTERPRETERS.match(name):
        for package, label in AGENT_PACKAGES:
            if '/' + package + '/' in command:
                return label
        base = Path(script(command)).name.split('.')[0]
        if base in AGENTS:
            return AGENTS[base]
        if ' -m aider' in command:
            return 'Aider'
    return None


def kind_of(command):
    """('agent' | 'editor' | 'terminal' | 'app', name) for a program that can own others, else None."""
    agent = agent_name(command)
    if agent:
        return 'agent', agent
    if program(command) in MULTIPLEXERS:
        return 'terminal', MULTIPLEXERS[program(command)]
    app = bundle(command)
    if app is None:
        return None
    if app in TERMINAL_APPS:
        return 'terminal', TERMINAL_APPS[app]
    if app in EDITOR_APPS:
        return 'editor', EDITOR_APPS[app]
    if app.startswith(JETBRAINS):
        return 'editor', app
    return 'app', app


def tool(command):
    """What people would call it: the App, the tool (tsc, vite…), or the interpreter with what it runs."""
    if 'SimMetalHost' in command:
        return 'iOS 模拟器画面'   # one per booted simulator, run by CoreSimulator, not under the device
    if 'CoreSimulator' in command or 'SimulatorRuntime' in command or 'launchd_sim' in command:
        return 'iOS 模拟器'
    agent = agent_name(command)
    if agent:
        return agent
    if bundle(command):
        return bundle(command)
    name = program(command)
    if not INTERPRETERS.match(name):
        return name
    what = script(command)
    if 'node_modules/' in what:
        return Path(what).name.split('.')[0]   # node …/node_modules/typescript/lib/tsserver.js is tsserver
    return FRIENDLY.get(name, name) + (' ' + Path(what).name if what else '')


def companion(command):
    return next((label for label, pattern in COMPANIONS if pattern.search(command)), None)


def device(table, pid):
    """The simulator a process runs inside (its UDID), or None: the process or one above it is that device's
    launchd_sim, or runs a program from the device's folder (an app being tested)."""
    current, seen = table.get(pid), set()
    while current is not None and current['pid'] not in seen:
        seen.add(current['pid'])
        found = DEVICE.search(current['command'])
        first = current['command'].split()[0] if current['command'].split() else ''
        if found and (Path(first).name == 'launchd_sim' or DEVICE.search(first)):
            return found.group(1)
        current = table.get(current['ppid'])
    return None


_NAMES = {}


def device_name(udid, home=None):
    """「iPhone 17 Pro」 from the device's own plist: no simctl, which hangs when the Mac is this tight."""
    if udid not in _NAMES:
        plist = (Path.home() if home is None else Path(home)) / 'Library/Developer/CoreSimulator/Devices' / udid / 'device.plist'
        try:
            _NAMES[udid] = plistlib.loads(plist.read_bytes()).get('name') or udid[:8]
        except (OSError, ValueError, plistlib.InvalidFileException):
            _NAMES[udid] = udid[:8]
    return _NAMES[udid]


def connected(table, udid):
    """Processes outside the simulator that name it: a screen or log stream, a test run, a build for it."""
    return [pid for pid, row in table.items() if udid in row['command'] and device(table, pid) != udid
            and 'CoreSimulator.framework' not in row['command'] and not any(w in row['command'] for w in NEVER[:3])]


def users(table, pids):
    """What callers should also look at (lsof) to tell a simulator's project: who is connected to the ones listed."""
    found = set()
    for udid in {device(table, p) for p in pids} - {None}:
        found.update(connected(table, udid))
    return found


def sim_owner(table, udid, managed=()):
    """Who uses a simulator (AC-06): the owner of what is connected to it, an agent first; the Simulator App when
    only it shows the device; nobody when nothing is connected, or only what nobody owns any more."""
    found, links = [], connected(table, udid)
    for pid in links:
        who = owner(table, pid, managed)
        mine = kind_of(table[pid]['command'])
        if mine and mine[0] in OWNER_ORDER:   # the tool itself can own (Xcode running the tests)
            who = dict(kind=mine[0], name=mine[1], pid=pid)
        if who['kind'] in OWNER_ORDER:
            found.append(dict(who, via='uses', user=pid))
    if found:
        return min(found, key=lambda w: OWNER_ORDER.index(w['kind']))
    if any(bundle(row['command']) == 'Simulator' for row in table.values()):
        return dict(kind='app', name='Simulator', via='shows')
    left = sorted({tool(table[p]['command']) for p in links if not table[p]['command'].startswith(('sh ', '/bin/sh ', 'zsh ', '/bin/zsh '))})
    return dict(kind='gone', via='sim', left=left, since=max((table[p]['age'] for p in links), default=None))


def owner(table, pid, managed=()):
    """Who started it: the nearest program up its parents that can own others. An App's own processes belong to
    the App; one that reaches launchd with no such program on the way is nobody's (`gone`), unless launchd runs it
    as a service."""
    row = table.get(pid)
    if row is None:
        return dict(kind='unknown')
    mine = kind_of(row['command'])
    if mine and mine[0] != 'agent':
        return dict(kind=mine[0], name=mine[1], pid=pid, self=True)
    current, seen = row, {pid}
    while True:
        parent_pid = current['ppid']
        if parent_pid <= 1:
            if current['pid'] in managed or SYSTEM.search(current['command']) or current.get('uid', MINE) != MINE:
                return dict(kind='service', name=tool(current['command']))
            if CLAUDE_SHELL.search(current['command']):
                return dict(kind='agent', name='Claude Code', pid=current['pid'], via='background')
            return dict(kind='gone')
        parent = table.get(parent_pid)
        if parent is None or parent_pid in seen:
            return dict(kind='unknown')
        seen.add(parent_pid)
        found = kind_of(parent['command'])
        if found and found != mine:   # an agent's own launcher is not its owner
            return dict(kind=found[0], name=found[1], pid=parent_pid)
        current = parent


def project(cwd, home=None):
    """The git repository a folder is in, or the folder itself; nothing for the home folder or the disk's root."""
    if not cwd or not str(cwd).startswith('/'):
        return None
    path, home = Path(cwd), Path.home() if home is None else Path(home)
    try:
        for folder in [path, *path.parents]:
            if folder in (home, Path('/')):
                break
            if (folder / '.git').exists():
                return dict(name=folder.name, path=str(folder))
    except OSError:
        pass
    return None if path in (home, Path('/')) else dict(name=path.name, path=str(path))


def looks(pids, run=subprocess.run):
    """{pid: (cwd, writes)} in one lsof: where each works, and whether its output goes to a file (a recording)."""
    if not pids:
        return {}
    try:
        out = run(['/usr/sbin/lsof', '-a', '-d', 'cwd,1,2', '-p', ','.join(str(p) for p in sorted(pids)), '-Fpftn'],
                  capture_output=True, text=True, errors='surrogateescape', timeout=40).stdout
    except (OSError, subprocess.SubprocessError):
        return {}
    found, pid, fd, kind = {}, None, None, None
    for line in out.splitlines():
        tag, value = line[:1], line[1:]
        if tag == 'p' and value.isdigit():
            pid, fd, kind = int(value), None, None
            found[pid] = [None, False]
        elif tag == 'f':
            fd, kind = value, None
        elif tag == 't':
            kind = value
        elif tag == 'n' and pid is not None:
            if fd == 'cwd':
                found[pid][0] = value
            elif kind == 'REG':
                found[pid][1] = True
    return {p: tuple(v) for p, v in found.items()}


def seen_where(state, table, pids, run=subprocess.run):
    """looks() kept per process in the watch state, so lsof only ever runs for processes it has not seen."""
    before = state.get('looks', {})
    known = {p: before[str(p)] for p in pids if str(p) in before and before[str(p)][0] == table[p]['lstart']}
    fresh = looks([p for p in pids if p not in known], run)
    for p, (cwd, writes) in fresh.items():
        known[p] = [table[p]['lstart'], cwd, writes]
    state['looks'] = {str(p): v for p, v in known.items() if p in table}
    return {p: (v[1], v[2]) for p, v in known.items()}


def track(state, table, now):
    """Since when each agent has been still (AC-02, AC-03). One first seen counts as moving now: a guess on the safe
    side, so nothing it started goes in its first six hours of being watched."""
    before, fresh = state.get('agents', {}), {}
    for pid, row in table.items():
        if not agent_name(row['command']):
            continue
        old = before.get(str(pid))
        if old and old['lstart'] == row['lstart']:
            minutes = max(1.0, (now - old['at']) / 60)
            moved = now if row['cpu'] - old['cpu'] >= AGENT_MOVED_SECONDS * minutes else old['moved']
        else:
            moved = now
        seen = old.get('seen', old['moved']) if old and old['lstart'] == row['lstart'] else now
        fresh[str(pid)] = dict(lstart=row['lstart'], cpu=row['cpu'], at=now, moved=moved, seen=seen)
    state['agents'] = fresh
    return fresh


def busy(state, table, now):
    """{pid: since} for what has used most of a core for half an hour or more (AC-04). One first seen that has done
    so all its life counts from its start."""
    before, fresh = state.get('busy', {}), {}
    for pid, row in table.items():
        if row.get('pcpu', 0) < BUSY_SHARE * 100 or 'kernel_task' in row['command']:
            continue
        old = before.get(str(pid))
        if old and old[0] == row['lstart']:
            fresh[str(pid)] = old
        else:
            lifelong = row['age'] > 0 and row['cpu'] / row['age'] >= BUSY_SHARE
            fresh[str(pid)] = [row['lstart'], now - row['age'] if lifelong else now]
    state['busy'] = fresh
    return {int(p): v[1] for p, v in fresh.items() if now - v[1] >= BUSY_MINUTES * 60}


def span(seconds):
    if seconds >= 86400:
        return '{:.0f} 天'.format(seconds / 86400) if seconds >= 2 * 86400 else '{:.0f} 小时'.format(seconds / 3600)
    if seconds >= 3600:
        return '{:.0f} 小时'.format(seconds / 3600)
    return '{:.0f} 分钟'.format(max(1, seconds / 60))


def still_since(who, agents):
    entry = agents.get(str(who.get('pid')))
    return entry['moved'] if entry else None


def fact(who, row, agents, now):
    """What can be seen about its owner, instead of 「在用」 (AC-02)."""
    if who.get('self'):
        return 'App 本身'
    verb = '在用' if who.get('via') == 'uses' else '起的'
    if who.get('via') == 'background':
        return '{} 的后台任务 · {}'.format(who['name'], span(row['age']))
    if who['kind'] == 'agent':
        moved, entry = still_since(who, agents), agents.get(str(who.get('pid'))) or {}
        if moved is None or moved == entry.get('seen') and now - moved < AGENT_QUIET_HOURS * 3600:
            return '{} {}'.format(who['name'], verb)   # not seen moving yet: nothing to say about it
        quiet = now - moved
        if quiet < 120:
            return '{} {} · 刚刚还在动'.format(who['name'], verb)
        return '{} {} · {}{}'.format(who['name'], verb, span(quiet), '前还在动' if quiet < 3600 else '没动')
    if who['kind'] in ('editor', 'terminal', 'app'):
        return '{} {}'.format(who['name'], '在用' if who.get('via') == 'uses' else '开着')
    if who['kind'] == 'service':
        return '后台服务'
    if who['kind'] == 'gone':
        if who.get('via') == 'sim' and who.get('left'):   # only a stream nobody owns any more is connected
            return '只有没人管的 {} 连着 · {}'.format('、'.join(who['left']), span(who['since'] or row['age']))
        return ('没人连着 · {}' if who.get('via') == 'sim' else '没人管了 · {}').format(span(row['age']))
    return '查不到是谁的'


def verdict(row, who, where, agents, quiet_cpu, now, home=None):
    """AC-03: (stop?, why). Only companions, only when their owner is gone or has been still for six hours, never one
    that writes to a file. `where` is {pid: (cwd, writes)}; `quiet_cpu` since when a server used no CPU of its own."""
    label = companion(row['command'])
    if label is None:
        return False, None
    cwd, writes = where.get(row['pid'], (None, False))
    if writes:
        return False, '在往文件里写'
    hours = AGENT_QUIET_HOURS * 3600
    if who.get('via') == 'background':
        who = dict(kind='gone')   # whether its session still moves cannot be seen: judged like one nobody owns
    if who['kind'] == 'agent':
        if label in SERVERS:
            return False, None   # its own MCP or language servers: the session needs them when someone comes back
        moved = still_since(who, agents)
        if moved is not None and now - moved >= hours:
            return True, '{}（{}）：起它的 {} 已经 {}没动'.format(label, tool(row['command']), who['name'], span(now - moved))
        return False, None
    if who['kind'] != 'gone' or row['age'] < ORPHAN_COMPANION_HOURS * 3600:
        return False, None
    if label in SERVERS and (quiet_cpu is None or now - quiet_cpu < hours):
        return False, None
    if cwd is None:
        return False, '查不到它在哪个文件夹'
    mine, anywhere = project(cwd, home), False
    for pid, entry in agents.items():
        if now - entry['moved'] >= hours:
            continue
        theirs = project(where.get(int(pid), (None, False))[0], home)
        if theirs is None:
            anywhere = True   # an agent in the home folder or at the disk's root may work in any folder
        elif mine is not None and theirs['path'] == mine['path']:
            return False, '同一个项目里还有 agent 在动'
    if anywhere and row['age'] < ORPHAN_ANYWHERE_HOURS * 3600:
        return False, '有 agent 在动、查不到它在哪个项目：放满一天再关'
    return True, '{}（{}）：没人管了 {}，同一个项目里也没有 agent 在动'.format(label, tool(row['command']), span(row['age']))


def describe(table, pids, where, agents, managed, now, sizes=None, hot=None, home=None):
    """One line per process (AC-01): tool, project, owner, how long, what it holds, how busy."""
    hot = hot or {}
    rows, up = [], None   # up: how long each booted device has been up, from its launchd_sim
    for pid in pids:
        row = table.get(pid)
        if row is None:
            continue
        udid = device(table, pid)
        if udid:   # one of a simulator's own processes: the simulator is the tool, whoever is connected its owner
            who = sim_owner(table, udid, managed)
            name = 'iOS 模拟器 · ' + device_name(udid, home)
            place = project(where.get(who.get('user'), (None, False))[0], home)
            # How long it has been up is the device's, not this process's: a helper it started a minute ago said
            # 「没人连着 · 1 分钟」 for a simulator up for 16 hours (11-04).
            if up is None:
                up = {found.group(1): r['age'] for r in table.values() if r['command'].startswith('launchd_sim ')
                      for found in [DEVICE.search(r['command'])] if found}
            row = dict(row, age=up.get(udid, row['age']))
        else:
            who = owner(table, pid, managed)
            name = tool(row['command'])
            place = None if who.get('self') or who['kind'] == 'service' else project(where.get(pid, (None, False))[0], home)
        rows.append(dict(pid=pid, lstart=row['lstart'], tool=name, project=place and place['path'], sim=udid,
                         owner=who, fact=fact(who, row, agents, now), age=row['age'],
                         bytes=max(row['rss'], (sizes or {}).get(pid, 0)), cores=round(row.get('pcpu', 0) / 100, 1),
                         busy_since=hot.get(pid), companion=companion(row['command'])))
    return rows


LOOK_PER_GROUP = 8   # lsof for this many of a group's processes: enough to name its projects


def biggest(table, pids, managed=(), sizes=None, limit=TOP):
    """The `limit` biggest groups, each with all its processes, biggest first (know-who AC-07). Ranked process by
    process, 25 Claude Code sessions of 250 MB each and a simulator's dozen processes never made the top 12, and the
    agent chased an 8 GB decoder instead (11-04). Grouped as group() does: the tool and its owner, or the device."""
    held = lambda p: max(table[p]['rss'], (sizes or {}).get(p, 0))
    found = {}
    for pid in pids:
        udid = device(table, pid)
        if udid:
            key = ('sim', udid)
        else:
            who = owner(table, pid, managed)
            key = (tool(table[pid]['command']), who['kind'], who.get('name'))
        entry = found.setdefault(key, [0, []])
        entry[0] += held(pid)
        entry[1].append(pid)
    return [sorted(members, key=held, reverse=True) for _, members in sorted(found.values(), key=lambda g: -g[0])[:limit]]


def group(rows):
    """Same tool, same owner: one line with how many and in which projects. An App's processes are the App."""
    groups = {}
    for row in rows:
        who = row['owner']
        key = (row['tool'], who['kind'], who.get('name'))
        entry = groups.setdefault(key, dict(tool=row['tool'], owner=who, count=0, projects=[], bytes=0, cores=0.0,
                                            age=0, fact=row['fact'], busy_since=None, pids=[],
                                            app=bool(who.get('self') or row.get('sim'))))   # one simulator is one thing
        entry['count'] += 1
        entry['bytes'] += row['bytes']
        entry['cores'] = round(entry['cores'] + row['cores'], 1)
        entry['age'] = max(entry['age'], row['age'])
        entry['youngest'] = min(entry.get('youngest', row['age']), row['age'])
        entry['pids'].append(row['pid'])
        if row['project'] and row['project'] not in entry['projects']:
            entry['projects'].append(row['project'])   # paths here; names once all are in
        if row['busy_since'] and (entry['busy_since'] is None or row['busy_since'] < entry['busy_since']):
            entry['busy_since'] = row['busy_since']
        if '刚刚' in row['fact'] or '前还在动' in row['fact']:
            entry['fact'] = row['fact']   # the liveliest member speaks for the group
    for entry in groups.values():
        entry['title'] = entry['tool'] + ('' if entry['app'] or entry['count'] == 1 else ' ×{}'.format(entry['count']))
        young, old = span(entry.pop('youngest')), span(entry['age'])
        if entry['owner']['kind'] == 'gone' and entry['owner'].get('via') != 'sim' and young != old:
            entry['fact'] = '没人管了 · {}～{}'.format(young, old)   # 11-03: one of 12 minutes read as 「23 小时」
        entry['projects'] = names(entry['projects'])
    return sorted(groups.values(), key=lambda g: -g['bytes'])


def names(paths):
    """Folder names, with the folder above when two checkouts share one (tasks/a/shop and tasks/b/shop)."""
    leaf = [Path(p).name for p in paths]
    return [Path(p).parent.name + '/' + Path(p).name if leaf.count(Path(p).name) > 1 else Path(p).name for p in paths]


def where_words(entry):
    projects = entry['projects']
    if not projects:
        return ''
    if len(projects) == 1:
        return projects[0]
    return '{} 个项目：{}{}'.format(len(projects), '、'.join(projects[:3]), '等' if len(projects) > 3 else '')


def busy_words(entry, now):
    if not entry.get('busy_since'):
        return ''
    cores = max(entry['cores'], 1.0)
    return '占满 {} 个核已经 {}'.format('{:g}'.format(round(cores)), span(now - entry['busy_since']))


def line(entry, now):
    """「node ×4 · 4 个项目：a、b、c · Claude Code 起的 · 5 分钟前还在动」"""
    parts = [entry['title'], where_words(entry), entry['fact'], busy_words(entry, now)]
    return ' · '.join(p for p in parts if p)


def footprints(run=subprocess.run, limit=1000):
    """What each process really holds, swapped and compressed memory included ({pid: bytes}), as `top` counts it.
    `ps` only sees what is still in RAM: on 10-31 five 3 GB type checks each showed under 300 MB there."""
    try:
        out = run(['/usr/bin/top', '-l', '1', '-o', 'mem', '-n', str(limit), '-stats', 'pid,mem'],
                  capture_output=True, text=True, timeout=30).stdout
    except (OSError, subprocess.SubprocessError):
        return {}
    found, unit = {}, {'K': 2 ** 10, 'M': 2 ** 20, 'G': 2 ** 30, 'B': 1}
    for line in out.splitlines():
        match = re.match(r'\s*(\d+)\s+([\d.]+)([KMGB])[+-]?\s*$', line)
        if match:
            found[int(match.group(1))] = int(float(match.group(2)) * unit[match.group(3)])
    return found
