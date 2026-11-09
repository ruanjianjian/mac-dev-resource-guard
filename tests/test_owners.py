from pathlib import Path
import plistlib
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_tiers   # puts runtime/storage on the path
import owners as O

NOW = 1_800_000_000.0
HOUR = 3600


def row(pid, command, ppid=1, hours=30, cpu=10.0, pcpu=0.0, mb=100, lstart='Thu Oct 29 13:22:55 2026'):
    return dict(pid=pid, ppid=ppid, age=hours * HOUR, cpu=cpu, pcpu=pcpu, rss=mb * 2 ** 20, lstart=lstart, command=command)


# What other Macs run: a terminal, editors, agents in the terminal and in their Apps (know-who AC-05).
TABLE = {
    10: row(10, '/System/Applications/Utilities/Terminal.app/Contents/MacOS/Terminal'),
    11: row(11, '/usr/bin/login -pf dev', ppid=10),
    12: row(12, '-zsh', ppid=11),
    13: row(13, '/opt/homebrew/bin/node /opt/homebrew/lib/node_modules/@anthropic-ai/claude-code/cli.js', ppid=12),
    14: row(14, '/bin/zsh -c tail -F /tmp/app.log', ppid=13),
    15: row(15, 'tail -F /tmp/app.log', ppid=14),
    20: row(20, '/Applications/Cursor.app/Contents/MacOS/Cursor'),
    21: row(21, '/Applications/Cursor.app/Contents/Frameworks/Cursor Helper (Plugin).app/Contents/MacOS/Cursor Helper (Plugin) --type=utility', ppid=20),
    22: row(22, '/usr/local/bin/node /x/node_modules/typescript/lib/tsserver.js', ppid=21),
    30: row(30, '/Applications/iTerm.app/Contents/MacOS/iTerm2'),
    31: row(31, '/bin/zsh', ppid=30),
    32: row(32, '/opt/homebrew/bin/tmux new -s work', ppid=31),
    33: row(33, '/Users/dev/.local/bin/codex', ppid=32),
    34: row(34, 'npx tsc --watch -p .', ppid=33),
    40: row(40, '/Applications/ChatGPT.app/Contents/MacOS/ChatGPT'),
    41: row(41, '/Applications/ChatGPT.app/Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex app-server', ppid=40),
    50: row(50, 'xcrun simctl spawn booted log stream --level debug', hours=8),
    51: row(51, 'python3 /Users/dev/.local/bin/aider --model x', ppid=31),
    60: row(60, '/Applications/PyCharm.app/Contents/MacOS/pycharm'),
    61: row(61, '/Users/dev/.local/share/baguette/Baguette stream --udid 4E5D --fps 1', ppid=99),
    70: row(70, '/usr/local/bin/my-daemon --serve'),
}


class OwnerTest(unittest.TestCase):
    def test_the_nearest_program_that_can_own_others_is_the_owner(self):
        who = lambda pid: O.owner(TABLE, pid, managed={70})
        self.assertEqual(who(15), dict(kind='agent', name='Claude Code', pid=13))      # a tail Claude Code started
        self.assertEqual(who(13), dict(kind='terminal', name='Terminal', pid=10))      # and Claude Code is the terminal's
        self.assertEqual(who(22), dict(kind='editor', name='Cursor', pid=21))          # Cursor's language server
        self.assertEqual(who(34), dict(kind='agent', name='Codex', pid=33))            # a watch Codex left in tmux
        self.assertEqual(who(33), dict(kind='terminal', name='tmux', pid=32))
        self.assertEqual((O.agent_name(TABLE[51]['command']), who(51)['name']), ('Aider', 'iTerm2'))   # Aider, run in iTerm2
        self.assertEqual(who(41), dict(kind='app', name='ChatGPT', pid=40))            # the Codex App's own server
        self.assertEqual(who(60), dict(kind='editor', name='PyCharm', pid=60, self=True))
        self.assertEqual(who(50), dict(kind='gone'))                                   # its parent is long gone
        self.assertEqual(who(70)['kind'], 'service')                                   # launchd runs it on purpose
        made = {514: row(514, '/Applications/Xcode.app/Contents/Developer/usr/bin/make local-bff'),
                517: row(517, 'bash ./scripts/dev-local.sh bff', ppid=514),
                649: row(649, 'node --require ./otel-register.cjs --import tsx src/server.ts', ppid=517)}
        self.assertEqual(O.owner(made, 649), dict(kind='gone'))                      # Xcode's make is not the Xcode App
        self.assertEqual((O.tool(made[514]['command']), O.tool(made[649]['command'])), ('make', 'node server.ts'))
        webkit = {5: row(5, '/System/Library/Frameworks/WebKit.framework/Versions/A/XPCServices/com.apple.WebKit.WebContent.xpc/Contents/MacOS/com.apple.WebKit.WebContent')}
        self.assertEqual(O.owner(webkit, 5)['kind'], 'service')                       # the Mac's own, not nobody's
        self.assertEqual(O.owner({6: dict(row(6, 'autofsd'), uid=0)}, 6)['kind'], 'service')   # root's, not the user's leftover
        self.assertEqual(who(61), dict(kind='unknown'))                                # its parent cannot be seen

    def test_tools_are_named_the_way_people_say_them(self):
        self.assertEqual(O.tool(TABLE[13]['command']), 'Claude Code')
        self.assertEqual(O.tool(TABLE[21]['command']), 'Cursor')
        self.assertEqual(O.tool(TABLE[22]['command']), 'tsserver')
        self.assertEqual(O.tool('/p/node_modules/@esbuild/darwin-arm64/bin/esbuild --service=0.25'), 'esbuild')
        self.assertEqual(O.tool('/p/node_modules/.bin/vite --port 3000'), 'vite')
        self.assertEqual(O.tool('node src/server.ts'), 'node server.ts')
        self.assertEqual(O.tool(TABLE[61]['command']), 'Baguette')

    def test_companions_are_the_helpers_only_useful_while_watched(self):
        kinds = {pid: O.companion(r['command']) for pid, r in TABLE.items()}
        self.assertEqual(kinds[15], '日志流')
        self.assertEqual(kinds[50], '日志流')
        self.assertEqual(kinds[61], '画面流')
        self.assertEqual(kinds[34], '盯着改动的编译测试')
        self.assertEqual(kinds[22], '语言服务')
        self.assertEqual(kinds[41], 'agent 帮手')
        for pid in (10, 13, 20, 33, 70):
            self.assertIsNone(kinds[pid])
        self.assertIsNone(O.companion('/p/node_modules/.bin/vite --port 3000'))   # a dev server may be open in a browser
        self.assertIsNone(O.companion('npx tsc -p .'))                             # a one-off check is a heavy job, not a companion
        self.assertEqual(O.companion('adb -s emulator-5554 logcat'), '日志流')
        self.assertEqual(O.companion('xcrun simctl io booted recordVideo out.mov'), '画面流')

    def test_a_project_is_the_git_repository_or_else_the_folder(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            repo = home / 'code/shop'
            (repo / 'web/src').mkdir(parents=True)
            (repo / '.git').mkdir()
            worktree = home / 'tasks/fix-cart/shop'
            worktree.mkdir(parents=True)
            (worktree / '.git').write_text('gitdir: ../../../code/shop/.git/worktrees/fix-cart')
            self.assertEqual(O.project(str(repo / 'web/src'), home)['name'], 'shop')
            self.assertEqual(O.project(str(worktree), home)['path'], str(worktree))
            self.assertEqual(O.project(str(home / 'Downloads'), home)['name'], 'Downloads')
            self.assertIsNone(O.project(str(home), home))
            self.assertIsNone(O.project('/', home))
            self.assertIsNone(O.project(None, home))

    def test_lsof_tells_where_each_works_and_whether_it_writes_a_file(self):
        out = 'p15\nfcwd\ntDIR\nn/code/shop\nf1\ntCHR\nn/dev/null\n' + 'p50\nfcwd\ntDIR\nn/\nf1\ntREG\nn/tmp/sim.log\n'
        run = lambda args, **kw: subprocess.CompletedProcess(args, 0, out, '')
        self.assertEqual(O.looks([15, 50], run), {15: ('/code/shop', False), 50: ('/', True)})
        self.assertEqual(O.looks([], run), {})


class ActivityTest(unittest.TestCase):
    def test_an_agent_moves_when_its_own_cpu_does_more_than_an_idle_one(self):
        state = {}
        claude = lambda cpu: {13: row(13, TABLE[13]['command'], cpu=cpu)}
        O.track(state, claude(100.0), NOW)
        self.assertEqual(state['agents']['13']['moved'], NOW)                    # first seen: counts as moving now
        for minute in range(1, 6):                                               # idle: a third of a second a minute
            O.track(state, claude(100.0 + minute * 0.3), NOW + minute * 60)
        self.assertEqual(state['agents']['13']['moved'], NOW)
        O.track(state, claude(104.0), NOW + 6 * 60)                              # a turn: seconds of CPU
        self.assertEqual(state['agents']['13']['moved'], NOW + 6 * 60)
        O.track(state, {13: row(13, TABLE[13]['command'], cpu=1.0, lstart='Fri Oct 30 09:00:00 2026')}, NOW + 420)
        self.assertEqual(state['agents']['13']['moved'], NOW + 420)              # another process with the same pid

    def test_busy_for_half_an_hour_or_all_its_life(self):
        state = {}
        stream = row(61, TABLE[61]['command'], hours=72, cpu=72 * HOUR * 0.99, pcpu=99.0)
        fresh = row(62, 'swift-frontend -c x.swift', hours=1, cpu=60.0, pcpu=95.0)
        hot = O.busy(state, {61: stream, 62: fresh}, NOW)
        self.assertEqual(list(hot), [61])                                        # three days at a full core
        self.assertEqual(hot[61], NOW - 72 * HOUR)
        later = O.busy(state, {61: stream, 62: fresh}, NOW + 31 * 60)
        self.assertEqual(set(later), {61, 62})                                   # half an hour at it now
        self.assertEqual(O.busy(state, {61: dict(stream, pcpu=3.0)}, NOW + 32 * 60), {})


    def test_a_claude_code_background_task_is_its_not_nobodys(self):
        # Claude Code leaves its background tasks to launchd: one is a session's, not 「没人管了」 (11-05).
        table = {39472: row(39472, '/bin/zsh -c source /Users/dev/.claude/shell-snapshots/snapshot-zsh-1-x.sh 2>/dev/null || true && '
                                   'eval \'npx vitest related --run\'', hours=0.3),
                 39476: row(39476, 'npm exec vitest related --run', ppid=39472, hours=0.3),
                 39613: row(39613, 'node (vitest)', ppid=39476, hours=0.3, mb=3000)}
        who = O.owner(table, 39613)
        self.assertEqual(who, dict(kind='agent', name='Claude Code', pid=39472, via='background'))
        self.assertEqual(O.fact(who, table[39613], {}, NOW), 'Claude Code 的后台任务 · 18 分钟')
        # Whether its session still moves cannot be seen: a companion in one is judged like one nobody owns.
        table[39480] = row(39480, 'tail -f /tmp/app.log', ppid=39472, hours=8)
        where = {p: ('/code/shop', False) for p in table}
        self.assertTrue(O.verdict(table[39480], O.owner(table, 39480), where, {}, None, NOW)[0])
        table[39480] = dict(table[39480], age=2 * HOUR)
        self.assertFalse(O.verdict(table[39480], O.owner(table, 39480), where, {}, None, NOW)[0])


    def test_a_server_counts_who_it_accepted_not_who_it_called(self):
        out = ('p102\nf20\nn127.0.0.1:5253\nTST=LISTEN\nTQR=0\n'
               'p202\nf20\nn[::1]:5233\nTST=LISTEN\nf21\nn[::1]:5233->[::1]:61234\nTST=ESTABLISHED\n'
               'f22\nn[::1]:5233->[::1]:61240\nTST=ESTABLISHED\nf23\nn127.0.0.1:61300->127.0.0.1:443\nTST=ESTABLISHED\n'
               'p77\nf5\nn127.0.0.1:61234->127.0.0.1:5223\nTST=ESTABLISHED\n')   # a client of someone else's server
        run = lambda args, **kw: subprocess.CompletedProcess(args, 0, out, '')
        self.assertEqual(O.serving(run), {102: 0, 202: 2})
        self.assertIsNone(O.serving(lambda args, **kw: (_ for _ in ()).throw(subprocess.TimeoutExpired(args, 20))))


class VerdictTest(unittest.TestCase):
    def verdict(self, pid, table=TABLE, where=None, agents=None, quiet=None, now=NOW):
        where = {p: ('/code/shop', False) for p in table} if where is None else where
        return O.verdict(table[pid], O.owner(table, pid, managed={70}), where, agents or {}, quiet, now)

    def test_a_companion_goes_once_the_agent_that_started_it_has_been_still_six_hours(self):
        agents = {'13': dict(lstart='x', cpu=1.0, at=NOW, moved=NOW - 7 * HOUR)}
        yes, why = self.verdict(15, agents=agents)
        self.assertTrue(yes)
        self.assertIn('Claude Code 已经 7 小时没动', why)
        agents['13']['moved'] = NOW - 5 * HOUR
        self.assertEqual(self.verdict(15, agents=agents), (False, None))

    def test_one_an_editor_or_terminal_keeps_open_stays(self):
        self.assertEqual(self.verdict(22), (False, None))   # Cursor's language server
        table = {**TABLE, 80: row(80, 'tail -f /var/log/system.log', ppid=31)}
        self.assertEqual(self.verdict(80, table=table), (False, None))   # someone may be watching iTerm for hours

    def test_one_writing_to_a_file_is_a_recording(self):
        agents = {'13': dict(lstart='x', cpu=1.0, at=NOW, moved=NOW - 9 * HOUR)}
        where = {15: ('/code/shop', True)}
        self.assertEqual(self.verdict(15, where=where, agents=agents), (False, '在往文件里写'))

    def test_nobodys_companion_waits_for_quiet_in_its_project(self):
        yes, _ = self.verdict(50)                                            # eight hours, nobody's, no agent about
        self.assertTrue(yes)
        young = {**TABLE, 50: dict(TABLE[50], age=2 * HOUR)}
        self.assertFalse(self.verdict(50, table=young)[0])
        busy = {'13': dict(lstart='x', cpu=1.0, at=NOW, moved=NOW - 600)}   # an agent working in the same repo
        self.assertEqual(self.verdict(50, agents=busy), (False, '同一个项目里还有 agent 在动'))
        elsewhere = {50: ('/code/shop', False), 13: ('/code/blog', False)}
        self.assertTrue(self.verdict(50, where=elsewhere, agents=busy)[0])  # a different project's agent does not count
        anywhere = {50: ('/code/shop', False), 13: ('/', False)}             # one that may work in any folder
        self.assertEqual(self.verdict(50, where=anywhere, agents=busy)[1], '有 agent 在动、查不到它在哪个项目：放满一天再关')
        old = {**TABLE, 50: dict(TABLE[50], age=30 * HOUR)}
        self.assertTrue(self.verdict(50, table=old, where=anywhere, agents=busy)[0])
        self.assertEqual(self.verdict(50, where={}), (False, '查不到它在哪个文件夹'))

    def test_servers_need_hours_without_cpu_and_an_agents_own_servers_stay(self):
        table = {**TABLE, 90: row(90, 'node /x/app-server-broker.mjs serve', hours=30)}
        self.assertFalse(self.verdict(90, table=table, quiet=NOW - HOUR)[0])
        self.assertTrue(self.verdict(90, table=table, quiet=NOW - 7 * HOUR)[0])
        table[91] = row(91, 'node /x/node_modules/@modelcontextprotocol/server-filesystem/dist/index.js', ppid=13)
        agents = {'13': dict(lstart='x', cpu=1.0, at=NOW, moved=NOW - 30 * HOUR)}
        self.assertEqual(self.verdict(91, table=table, agents=agents, quiet=NOW - 30 * HOUR), (False, None))


class WordsTest(unittest.TestCase):
    def test_one_line_per_tool_and_owner_with_projects_and_facts(self):
        table = {
            13: TABLE[13], 10: TABLE[10], 11: TABLE[11], 12: TABLE[12],
            101: row(101, 'node /code/a/server.js', ppid=13, mb=900), 102: row(102, 'node /code/b/server.js', ppid=13, mb=800),
            103: row(103, 'node /code/c/server.js', ppid=13, mb=700), 104: row(104, 'node /code/d/server.js', ppid=13, mb=600),
            200: row(200, '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome', mb=2000),
            201: row(201, '/Applications/Google Chrome.app/Contents/Frameworks/Google Chrome Helper (Renderer).app/Contents/MacOS/x', ppid=200, mb=1000),
            300: row(300, TABLE[61]['command'], hours=72, pcpu=99.0, mb=12),
        }
        where = {101: ('/code/a', False), 102: ('/code/b', False), 103: ('/code/c', False), 104: ('/code/d', False)}
        agents = {'13': dict(lstart='x', cpu=1.0, at=NOW, moved=NOW - 300)}
        rows = O.describe(table, [101, 102, 103, 104, 200, 201, 300], where, agents, set(), NOW, hot={300: NOW - 72 * HOUR})
        groups = {g['title']: g for g in O.group(rows)}
        self.assertEqual(set(groups), {'node server.js ×4', 'Google Chrome', 'Baguette'})
        self.assertEqual(O.line(groups['node server.js ×4'], NOW),
                         'node server.js ×4 · 4 个项目：a、b、c等 · Claude Code 起的 · 5 分钟前还在动')
        self.assertEqual(O.line(groups['Google Chrome'], NOW), 'Google Chrome · App 本身')
        self.assertEqual(O.line(groups['Baguette'], NOW), 'Baguette · 没人管了 · 3 天 · 占满 1 个核已经 3 天')
        self.assertEqual(groups['Google Chrome']['bytes'], 3000 * 2 ** 20)
        same = {1: row(1, 'node /t/a/shop/server.js', ppid=13), 2: row(2, 'node /t/b/shop/server.js', ppid=13), 13: TABLE[13]}
        where = {1: ('/t/a/shop', False), 2: ('/t/b/shop', False)}
        [checkouts] = O.group(O.describe(same, [1, 2], where, {}, set(), NOW))
        self.assertEqual(checkouts['projects'], ['a/shop', 'b/shop'])   # two checkouts of one repo are two projects
        left = {1: row(1, 'node /t/a/server.ts', hours=23), 2: row(2, 'node /t/b/server.ts', hours=0.2)}
        [both] = O.group(O.describe(left, [1, 2], {}, {}, set(), NOW))
        self.assertEqual(both['fact'], '没人管了 · 12 分钟～23 小时')   # not the oldest one's age for both



UDID, OTHER = '386C210B-5E0F-4000-833B-E798B0C5A858', '78022C0C-75B4-48E6-A69A-28D2BB11BC7B'
DEVICES = '/Users/dev/Library/Developer/CoreSimulator/Devices/'
RUNTIME = '/Library/Developer/CoreSimulator/Volumes/iOS_24A/Library/Developer/CoreSimulator/Profiles/Runtimes/iOS 27.0.simruntime/Contents/Resources/RuntimeRoot'
SIMS = {
    **{p: TABLE[p] for p in (10, 11, 12, 13, 30, 31, 32, 33)},
    80: row(80, 'launchd_sim ' + DEVICES + UDID + '/data/var/run/launchd_bootstrap.plist', hours=1),
    81: row(81, RUNTIME + '/usr/libexec/backboardd', ppid=80, hours=1, pcpu=90.0, mb=300),
    82: row(82, DEVICES + UDID + '/data/Containers/Bundle/Application/AB12/Runner.app/Runner', ppid=80, hours=1, mb=700),
    83: row(83, '/Users/dev/.local/bin/baguette stream --udid ' + UDID + ' --fps 5', ppid=13, hours=1),     # Claude Code's
    84: row(84, '/usr/bin/xcrun simctl spawn ' + UDID + ' log stream --style compact', ppid=33, hours=1),   # Codex's
    90: row(90, 'launchd_sim ' + DEVICES + OTHER + '/data/var/run/launchd_bootstrap.plist', hours=5),
    91: row(91, RUNTIME + '/System/Library/CoreServices/SpringBoard.app/SpringBoard', ppid=90, hours=5, mb=400),
    95: row(95, '/Library/Developer/PrivateFrameworks/CoreSimulator.framework/Versions/A/XPCServices/'
                'com.apple.CoreSimulator.CoreSimulatorService.xpc/Contents/MacOS/com.apple.CoreSimulator.CoreSimulatorService'),
}


class SimulatorTest(unittest.TestCase):
    """know-who AC-06: a simulator is the tool, and whoever is connected to it — not 「后台服务」 — its owner."""
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        for udid, name in ((UDID, 'iPhone 17 Pro'), (OTHER, 'iPad Air')):
            folder = self.home / 'Library/Developer/CoreSimulator/Devices' / udid
            folder.mkdir(parents=True)
            (folder / 'device.plist').write_bytes(plistlib.dumps({'name': name, 'UDID': udid}))
        O._NAMES.clear()
        self.addCleanup(O._NAMES.clear)

    def test_each_process_belongs_to_the_device_it_runs_in(self):
        self.assertEqual([O.device(SIMS, p) for p in (80, 81, 82, 91)], [UDID, UDID, UDID, OTHER])
        self.assertEqual([O.device(SIMS, p) for p in (83, 84, 95, 13)], [None] * 4)   # connected to it, or the Mac's own
        self.assertEqual(O.device_name(UDID, self.home), 'iPhone 17 Pro')
        self.assertEqual(O.users(SIMS, [81, 91]), {83, 84})

    def test_who_is_connected_owns_it_and_says_how_lively(self):
        agents = {'13': dict(lstart='x', cpu=1.0, at=NOW, moved=NOW - 60, seen=NOW - 2 * HOUR)}
        where = {83: ('/code/shop', False), 84: ('/code/shop', False)}
        rows = O.describe(SIMS, [81, 82, 91, 95], where, agents, set(), NOW, hot={81: NOW - 40 * 60}, home=self.home)
        lines = {g['title']: O.line(g, NOW) for g in O.group(rows)}
        self.assertEqual(lines, {
            'iOS 模拟器 · iPhone 17 Pro': 'iOS 模拟器 · iPhone 17 Pro · shop · Claude Code 在用 · 刚刚还在动 · 占满 1 个核已经 40 分钟',
            'iOS 模拟器 · iPad Air': 'iOS 模拟器 · iPad Air · 没人连着 · 5 小时',
            'iOS 模拟器': 'iOS 模拟器 · 后台服务'})   # the Mac's own simulator service is shared, nobody's in particular
        shown = {**SIMS, 99: row(99, '/Applications/Xcode.app/Contents/Developer/Applications/Simulator.app/Contents/MacOS/Simulator')}
        self.assertEqual(O.sim_owner(shown, OTHER), dict(kind='app', name='Simulator', via='shows'))
        self.assertEqual(O.fact(O.sim_owner(shown, OTHER), SIMS[91], {}, NOW), 'Simulator 开着')
        left = {**SIMS, 97: row(97, 'sh -c tail -f /dev/null | baguette stream --udid ' + OTHER, hours=3),
                98: row(98, '/Users/dev/.local/bin/baguette stream --udid ' + OTHER, ppid=97, hours=3)}   # nohup'd, its agent gone
        self.assertEqual(O.fact(O.sim_owner(left, OTHER), SIMS[91], {}, NOW), '只有没人管的 baguette 连着 · 3 小时')

    def test_the_biggest_owners_count_every_process_they_have(self):
        """know-who AC-07, 11-04: ranked one process at a time, 25 Claude Code sessions of 250 MB showed as 「×3」 and
        each simulator as its app alone, while one 1.5 GB decoder looked like the worst."""
        app = '/Applications/Claude.app/Contents/MacOS/Claude'
        sessions = {200 + i: row(200 + i, '/Users/dev/Library/Application Support/Claude/claude-code/2.1.309/x/claude.app/Contents/MacOS/claude',
                                 ppid=199, mb=250) for i in range(25)}
        table = {**SIMS, 199: row(199, app, mb=400), **sessions,
                 300: row(300, '/System/Library/Frameworks/VideoToolbox.framework/Versions/A/XPCServices/VTDecoderXPCService.xpc'
                               '/Contents/MacOS/VTDecoderXPCService', mb=1500),
                 301: row(301, '/Library/Developer/PrivateFrameworks/CoreSimulator.framework/Versions/A/Resources/SimRenderingServices'
                               '.simdeviceio/Contents/XPCServices/SimMetalHost.xpc/Contents/MacOS/SimMetalHost', mb=500)}
        sizes = {82: 900 * 2 ** 20}   # swapped out: ps sees 700 MB of it
        big = O.biggest(table, list(table), sizes=sizes, limit=3)
        self.assertEqual(big, [sorted(sessions), [300], [82, 81, 80]])   # 25 × 250 MB first; every process of the device
        rows = O.describe(table, [p for members in big for p in members], {}, {}, set(), NOW, sizes=sizes, home=self.home)
        lines = [(g['title'], round(g['bytes'] / 2 ** 30, 1)) for g in O.group(rows)]
        self.assertEqual(lines, [('Claude Code ×25', 6.1), ('VTDecoderXPCService', 1.5), ('iOS 模拟器 · iPhone 17 Pro', 1.3)])
        self.assertEqual(O.tool(table[301]['command']), 'iOS 模拟器画面')
        # A helper the device started a minute ago does not make it 「1 分钟」: the device has been up for 5 hours.
        fresh = {**SIMS, 92: row(92, RUNTIME + '/usr/libexec/assetsd', ppid=90, hours=0.02, mb=900)}
        [sim] = [g for g in O.group(O.describe(fresh, [92, 91], {}, {}, set(), NOW, home=self.home)) if 'iPad' in g['title']]
        self.assertEqual(O.line(sim, NOW), 'iOS 模拟器 · iPad Air · 没人连着 · 5 小时')

    def test_the_agent_cannot_ask_to_stop_what_runs_inside_a_simulator(self):
        import emergency as E
        for pid in (81, 82):
            with self.assertRaises(ValueError) as refused:
                E.asked_process({}, str(pid), SIMS)
            self.assertIn('模拟器', str(refused.exception))

if __name__ == '__main__':
    unittest.main()
