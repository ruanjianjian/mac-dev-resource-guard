import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_tiers import TierFixture
import emergency as E
import watch as W
from storage import GIB

NOW = 1_800_000_000.0
DAY = 86400


VM_PAGES = 24 * 2 ** 30 // 16384
VM_STAT = ('Mach Virtual Memory Statistics: (page size of 16384 bytes)\nPages free:                               13548.\n'
           'Pages wired down:                    {wired}.\nPages purgeable:                       {purge}.\n'
           '"Translation faults":           46957115221.\nAnonymous pages:                       {anon}.\n'
           'Pages occupied by compressor:          {comp}.\n')


class Readings:
    """sysctl answers for one minute: pressure and swap in GB."""
    def __init__(self, pressure=1, swap_used=1.0, swap_total=16.0, folders=None, share=None):
        self.pressure, self.used, self.total, self.share = pressure, swap_used, swap_total, share
        self.folders, self.writes = folders or {}, set()
        self.sockets = ''   # what `lsof -iTCP` says: who waits for connections, who is connected

    def __call__(self, args, **kwargs):
        if args == ['/usr/bin/vm_stat'] and self.share is not None:   # 24 GB in 16 KB pages, `share` of it held
            held = int(self.share * VM_PAGES)
            return subprocess.CompletedProcess(args, 0, VM_STAT.format(anon=held - 2000 - 1000, purge=0, wired=2000, comp=1000), '')
        if 'hw.memsize' in args and self.share is not None:
            return subprocess.CompletedProcess(args, 0, '{}\n'.format(VM_PAGES * 16384), '')
        if 'kern.memorystatus_vm_pressure_level' in args:
            return subprocess.CompletedProcess(args, 0, '{}\n'.format(self.pressure), '')
        if 'vm.swapusage' in args:
            return subprocess.CompletedProcess(args, 0, 'total = {:.2f}M  used = {:.2f}M  free = 1.00M'.format(
                self.total * 1024, self.used * 1024), '')
        if args[:2] == ['/bin/launchctl', 'list']:
            return subprocess.CompletedProcess(args, 0, 'PID\tStatus\tLabel\n987\t0\tcn.example.service\n', '')
        if args[0] == '/usr/sbin/lsof' and '-iTCP' in args:
            return subprocess.CompletedProcess(args, 0, self.sockets, '')
        if args[0] == '/usr/sbin/lsof':   # every process works in its folder, output to nowhere
            pids = args[args.index('-p') + 1].split(',')
            return subprocess.CompletedProcess(args, 0, ''.join('p{0}\nfcwd\ntDIR\nn{1}\nf1\nt{2}\nn{3}\n'.format(
                p, self.folders.get(int(p), '/'), *(('REG', '/tmp/rec.log') if int(p) in self.writes else ('CHR', '/dev/null')))
                for p in pids), '')
        return subprocess.CompletedProcess(args, 0, '', '')


def row(pid, command, ppid=1, hours=30, cpu=10.0, gib=0.1, lstart='Thu Oct 29 13:22:55 2026', pcpu=0.0):
    return dict(pid=pid, ppid=ppid, age=hours * 3600, cpu=cpu, pcpu=pcpu, rss=int(gib * GIB), lstart=lstart, command=command)


class WatchFixture(TierFixture):
    def setUp(self):
        super().setUp()
        self.cfg()
        self.kills, self.notices = [], []
        self.kill = lambda pid, sig: self.kills.append((pid, sig))
        self.notice = lambda home, title, body, tab='cover': self.notices.append((title, body, tab))

    def tick(self, now, readings, table, **kwargs):
        kwargs.setdefault('managed', {987})
        return W.tick(self.home, now, run=readings, table=table, kill=self.kill, sleep=lambda s: None,
                      notice=self.notice, **kwargs)


class SqueezeTest(unittest.TestCase):
    def test_tight_is_judged_from_the_trend_not_only_from_critical(self):
        at = lambda minutes, pressure, swap=1.0: dict(at=NOW + minutes * 60, pressure=pressure, swap_used=swap, swap_total=16.0)
        self.assertEqual(W.squeeze([at(0, 4)], NOW), '内存压力「严重」')
        warn = [at(-2, 2), at(-1, 2), at(0, 2)]
        self.assertIn('警告', W.squeeze(warn, NOW))
        self.assertIsNone(W.squeeze([at(-2, 1), at(-1, 2), at(0, 2)], NOW))   # two minutes is not yet a squeeze
        climbing = [at(-11, 1, 7.0), at(-5, 1, 8.0), at(0, 1, 10.0)]
        self.assertIn('swap', W.squeeze(climbing, NOW))                       # 10 of 16 GB and up 3 GB in 10 minutes
        self.assertIsNone(W.squeeze([at(-11, 1, 9.5), at(0, 1, 10.0)], NOW))   # high but steady


    def test_getting_tight_comes_before_the_kernel_says_warning(self):
        at = lambda minutes, pressure=1, swap=1.0, share=None: dict(at=NOW + minutes * 60, pressure=pressure, swap_used=swap,
                                                                   swap_total=16.0, used_share=share)
        self.assertEqual(W.soon([at(0, share=0.83)], NOW), '内存已经用了 83%')
        self.assertIsNone(W.soon([at(0, share=0.7)], NOW))
        self.assertEqual(W.soon([at(0, 2)], NOW), '内存压力「警告」')                 # one minute is enough here
        self.assertIn('swap 10 分钟里涨了 1.5 GB', W.soon([at(-11, swap=3.0), at(0, swap=4.5)], NOW))   # far from 60% of swap
        self.assertIsNone(W.soon([at(-11, swap=3.0), at(0, swap=3.5)], NOW))

    def test_the_share_is_what_apps_the_kernel_and_the_compressor_hold(self):
        out = VM_STAT.format(anon=428216, purge=302, wired=360550, comp=524698)
        run = lambda args, **kw: subprocess.CompletedProcess(args, 0, out if args == ['/usr/bin/vm_stat'] else '25769803776\n', '')
        self.assertEqual(W.used_share(run), 0.835)                                   # 11-05 on a 24 GB Mac: 21.5 GB held
        self.assertIsNone(W.used_share(lambda args, **kw: subprocess.CompletedProcess(args, 0, '', '')))


class EarlyTest(WatchFixture):
    def test_the_watch_warns_and_acts_once_memory_is_getting_tight(self):
        jobs = {pid: row(pid, 'node /w{}/node_modules/.bin/vitest run'.format(pid), ppid=pid + 10, hours=0.2, cpu=100.0 * pid, gib=2.0)
                for pid in (1, 2)}
        jobs.update({pid + 10: row(pid + 10, 'npm test', ppid=99, hours=0.2) for pid in (1, 2)})
        result = self.tick(NOW, Readings(pressure=1, share=0.85), jobs)
        self.assertIsNone(result['tight'])
        self.assertEqual(result['early'], '内存已经用了 85%')
        self.assertEqual([p['pid'] for p in result['paused']], [1])                  # two heavy jobs: one waits its turn
        [(title, body, tab)] = self.notices
        self.assertEqual((title, tab), ('内存快紧了：内存已经用了 85%', 'memory'))
        self.assertIn('2 个重活同时在跑', body)
        self.assertEqual(W.status(self.home, NOW)['early'], '内存已经用了 85%')
        result = self.tick(NOW + 60, Readings(pressure=1, share=0.6), jobs)
        self.assertIsNone(result['early'])
        self.assertEqual(len(self.notices), 1)

    def test_a_setting_that_cannot_be_read_never_stops_the_watch(self):
        # 11-11: one bad task root made every minute's tiers_config.load fail; the watch stopped for 17 hours.
        bad = W.tiers_config.settings_path(self.home); bad.write_text('{broken')
        result = self.tick(NOW, Readings(pressure=1, share=0.6), {})
        self.assertTrue(result['on'])
        self.assertEqual(len(W.load(self.home)['samples']), 1)

    def test_the_agent_comes_after_five_minutes_of_it(self):
        for minute in range(6):
            self.tick(NOW + minute * 60, Readings(pressure=1, share=0.85), {})
        seen = E.watch_state(self.home)
        state = {}
        self.assertIsNone(E.due(state, 100 * GIB, 25 * GIB, 1, NOW + 4 * 60, E.view(seen, 1, NOW + 4 * 60)))
        self.assertEqual(E.due(state, 100 * GIB, 25 * GIB, 1, NOW + 5 * 60, E.view(seen, 1, NOW + 5 * 60)), 'memory')
        self.assertIsNone(E.due({}, 100 * GIB, 25 * GIB, 1, NOW + 20 * 60, E.view(seen, 1, NOW + 20 * 60)))   # eased since
        self.assertEqual(E.why_words('memory', 100 * GIB, 25 * GIB, 1), '内存吃紧（系统内存压力：还没报警，但内存快用满了）')


def sockets(*entries):
    """lsof -FpnT for (pid, [(name, state)])"""
    return ''.join('p{}\n'.format(pid) + ''.join('f2{}\nn{}\nTST={}\nTQR=0\n'.format(i, name, st) for i, (name, st) in enumerate(files))
                   for pid, files in entries)


class IdleServerTest(WatchFixture):
    """11-09: of four vite servers read 「没人管了」, two were open in a browser, two had nobody connected for days."""
    def servers(self, cpu=60.0):
        return {101: row(101, 'npm exec vite --host 127.0.0.1 --port 5253', hours=200),
                102: row(102, 'node /w/a/web/node_modules/.bin/vite --host 127.0.0.1 --port 5253', ppid=101, hours=200, cpu=cpu, gib=1.0),
                201: row(201, 'npm exec vite --host 127.0.0.1 --port 5233', hours=70),
                202: row(202, 'node /w/b/web/node_modules/.bin/vite --host 127.0.0.1 --port 5233', ppid=201, hours=70, gib=1.0),
                301: row(301, 'mongod --port 27017', hours=200, gib=0.5)}

    def readings(self):
        readings = Readings(pressure=1, share=0.85)                     # memory getting tight
        readings.sockets = sockets((102, [('127.0.0.1:5253', 'LISTEN')]),
                                   (202, [('127.0.0.1:5233', 'LISTEN'), ('127.0.0.1:5233->127.0.0.1:61234', 'ESTABLISHED')]),
                                   (301, [('127.0.0.1:27017', 'LISTEN')]))
        return readings

    def test_a_server_nobody_connects_to_goes_after_twelve_hours(self):
        readings = self.readings()
        first = self.tick(NOW, readings, self.servers())
        self.assertEqual(first['stopped'], [])                          # nobody has been watched that long yet
        lines = W.load(self.home)['who']['lines']
        self.assertTrue(any(line.startswith('vite') and '有人连着' in line for line in lines), lines)
        later = self.tick(NOW + 3 * 3600, readings, self.servers())
        self.assertEqual(later['stopped'], [])
        self.assertTrue(any(line.startswith('vite') and '3 小时没人连' in line for line in W.load(self.home)['who']['lines']))
        self.kills.clear()
        done = self.tick(NOW + 12 * 3600, readings, self.servers())
        self.assertEqual([s['pid'] for s in done['stopped']], [102])
        self.assertEqual(sorted({pid for pid, _ in self.kills}), [101, 102])   # with the npm that started it
        self.assertIn('关掉了 1 个12 小时以上没人连的开发服务（vite）', ''.join(n[1] for n in self.notices))

    def test_one_with_someone_connected_or_working_or_owned_stays(self):
        readings = self.readings()
        self.tick(NOW, readings, self.servers())
        self.tick(NOW + 6 * 3600, readings, self.servers(cpu=200.0))   # it did some work: the clock starts again
        done = self.tick(NOW + 13 * 3600, readings, self.servers(cpu=200.0))
        self.assertEqual(done['stopped'], [])
        calm = Readings(pressure=1, share=0.5)
        calm.sockets = readings.sockets
        done = self.tick(NOW + 19 * 3600, calm, self.servers(cpu=200.0))
        self.assertEqual(done['stopped'], [])                           # memory is fine: nothing needs the room
        owned = {**self.servers(cpu=200.0), 101: row(101, 'npm exec vite', ppid=13, hours=200),
                 13: row(13, '/opt/homebrew/bin/node /opt/homebrew/lib/node_modules/@anthropic-ai/claude-code/cli.js', hours=300)}
        done = self.tick(NOW + 20 * 3600, readings, owned)
        self.assertEqual(done['stopped'], [])                           # a Claude Code session still holds it
        self.assertNotIn(301, {pid for pid, _ in self.kills})           # a database never


class LeftoverTest(WatchFixture):
    def test_streams_go_at_once_helpers_after_hours_of_quiet_services_never(self):
        table = {
            100: row(100, 'baguette stream --udid X --fps 1', cpu=9000.0),
            200: row(200, 'node /x/app-server-broker.mjs serve', cpu=50.0),
            201: row(201, 'codex app-server', ppid=200, cpu=10.0),
            300: row(300, 'node /x/app-server-broker.mjs serve', hours=3),          # too young
            400: row(400, 'postgres -D data'),                                       # never
            987: row(987, 'python3 archive_queue.py --watch'),                       # a launchd job
            500: row(500, 'node src/server.ts'),                                     # a dev server: the agent's call
        }
        first = self.tick(NOW, Readings(pressure=4), table)
        self.assertEqual([s['pid'] for s in first['stopped']], [100])
        self.assertIn((100, signal.SIGTERM), self.kills)
        self.assertNotIn((200, signal.SIGTERM), self.kills)
        gone = {k: v for k, v in table.items() if k != 100}                         # the stream ended when stopped
        soon = self.tick(NOW + 3600, Readings(pressure=4), gone)                    # an hour quiet may still be a wait on a model
        self.assertEqual(soon['stopped'], [])
        later = NOW + W.HELPER_IDLE_HOURS * 3600 + 60
        second = self.tick(later, Readings(pressure=4), {**gone, 200: row(200, 'node /x/app-server-broker.mjs serve', cpu=50.5)})
        self.assertEqual([s['pid'] for s in second['stopped']], [200])
        self.assertEqual(self.kills[-2:], [(201, signal.SIGTERM), (200, signal.SIGTERM)])   # children first
        for pid in (300, 400, 987, 500):
            self.assertNotIn((pid, signal.SIGTERM), self.kills)

    def test_a_helper_that_kept_working_stays(self):
        table = {200: row(200, 'node /x/app-server-broker.mjs serve', cpu=50.0)}
        self.tick(NOW, Readings(pressure=1), table)                                 # watched while memory is fine too
        self.tick(NOW + 5 * 3600, Readings(pressure=1), {200: row(200, 'node /x/app-server-broker.mjs serve', cpu=90.0)})
        busy = self.tick(NOW + 7 * 3600, Readings(pressure=4), {200: row(200, 'node /x/app-server-broker.mjs serve', cpu=90.0)})
        self.assertEqual(busy['stopped'], [])
        self.assertEqual(self.kills, [])

    def test_nothing_is_stopped_while_memory_is_fine(self):
        result = self.tick(NOW, Readings(pressure=1), {100: row(100, 'baguette stream --udid X')})
        self.assertIsNone(result['tight'])
        self.assertEqual(self.kills, [])


class CompanionTest(WatchFixture):
    """know-who AC-03: companions go by their owner, not by whether their parent exited."""
    TERM = row(10, '/System/Applications/Utilities/Terminal.app/Contents/MacOS/Terminal', hours=80)
    CLAUDE = row(13, '/opt/homebrew/bin/claude --resume abc', ppid=10, hours=80, cpu=500.0)

    def table(self, claude_cpu=500.0, more=None):
        return {10: self.TERM, 13: dict(self.CLAUDE, cpu=claude_cpu),
                61: row(61, 'sim-mirror stream --udid 4E5D --fps 1', ppid=13, hours=72, cpu=72 * 3600 * 0.99, pcpu=99.0), **(more or {})}

    def test_a_stream_its_agent_left_goes_after_six_still_hours_even_while_memory_is_fine(self):
        self.assertEqual(self.tick(NOW, Readings(), self.table())['stopped'], [])          # the agent was just seen
        for hour in range(1, 7):
            self.tick(NOW + hour * 3600, Readings(), self.table(500.0 + hour * 0.2))     # idle: barely any CPU
        result = self.tick(NOW + 6 * 3600 + 120, Readings(), self.table(501.3))
        self.assertEqual([s['pid'] for s in result['stopped']], [61])
        self.assertIn('Claude Code 已经 6 小时没动', result['stopped'][0]['why'])
        self.assertNotIn((13, signal.SIGTERM), self.kills)                               # never the agent itself
        [(title, body, tab)] = self.notices
        self.assertEqual((title, tab), ('关掉了占着 CPU 的陪跑后台', 'memory'))
        self.assertIn('sim-mirror', body)

    def test_an_agent_that_keeps_working_keeps_its_stream(self):
        self.tick(NOW, Readings(), self.table())
        self.tick(NOW + 3 * 3600, Readings(), self.table(500.5))
        self.tick(NOW + 3 * 3600 + 60, Readings(), self.table(505.5))                    # a turn three hours in
        result = self.tick(NOW + 7 * 3600, Readings(pressure=4), self.table(505.7))
        self.assertEqual(result['stopped'], [])

    def test_editor_and_terminal_companions_and_recordings_stay(self):
        cursor = {20: row(20, '/Applications/Cursor.app/Contents/MacOS/Cursor', hours=80),
                  22: row(22, 'node /x/node_modules/typescript/lib/tsserver.js', ppid=20, hours=80, pcpu=90.0),
                  31: row(31, '/bin/zsh', ppid=10, hours=80),
                  32: row(32, 'tail -f /var/log/system.log', ppid=31, hours=80, pcpu=60.0),
                  50: row(50, 'xcrun simctl spawn booted log stream --level debug', hours=30, pcpu=70.0)}
        readings = Readings(pressure=4)
        readings.writes = {50}
        self.tick(NOW, readings, self.table(more=cursor))
        result = self.tick(NOW + 7 * 3600, readings, self.table(more=cursor))
        self.assertEqual([s['pid'] for s in result['stopped']], [61])
        for pid in (22, 32, 50):
            self.assertNotIn((pid, signal.SIGTERM), self.kills)

    def test_the_minute_keeps_who_holds_what_for_the_page_and_the_agent(self):
        readings = Readings(folders={101: '/code/shop', 102: '/code/blog'})
        table = self.table(more={101: row(101, 'node /code/shop/server.js', ppid=13, gib=1.5),
                                 102: row(102, 'node /code/blog/server.js', ppid=13, gib=1.0)})
        self.tick(NOW, readings, table)
        lines = W.status(self.home, NOW)['who']
        self.assertIn('node server.js ×2 · 2 个项目：shop、blog · Claude Code 起的', lines)   # not seen moving yet: no claim
        self.assertIn('sim-mirror · Claude Code 起的 · 占满 1 个核已经 3 天', lines)
        self.assertIn('Terminal · App 本身', lines)
        table[13] = dict(table[13], cpu=table[13]['cpu'] + 5)                                 # a turn
        self.tick(NOW + 60, readings, table)
        self.assertIn('node server.js ×2 · 2 个项目：shop、blog · Claude Code 起的 · 刚刚还在动', W.status(self.home, NOW + 60)['who'])
        # know-who AC-07: twenty small helpers of one owner outweigh any single big process, and are all counted.
        many = {200 + i: row(200 + i, 'node /code/shop/worker.js', ppid=13, gib=0.3) for i in range(20)}
        self.tick(NOW + 120, readings, self.table(more={**many, 101: table[101], 102: table[102]}))
        groups = W.load(self.home)['who']['groups']
        self.assertEqual((groups[0]['title'], groups[0]['count']), ('node worker.js ×20', 20))


class QueueTest(WatchFixture):
    def checks(self, alive=(1, 2, 3, 4)):
        cpu = {1: 300.0, 2: 200.0, 3: 100.0, 4: 50.0}
        jobs = {pid: row(pid, 'node /w{}/node_modules/.bin/tsc --noEmit'.format(pid), ppid=pid + 10, hours=0.5, cpu=cpu[pid], gib=4.0)
                for pid in alive}
        parents = {pid + 10: row(pid + 10, 'npm run typecheck', ppid=99, hours=0.5) for pid in (1, 2, 3, 4)}
        return {**jobs, **parents}

    def test_crowding_heavy_jobs_queue_and_go_one_at_a_time(self):
        first = self.tick(NOW, Readings(pressure=4), self.checks())
        self.assertEqual(sorted(p['pid'] for p in first['paused']), [2, 3, 4])           # the furthest along keeps going
        self.assertEqual(sorted(pid for pid, sig in self.kills if sig == signal.SIGSTOP), [2, 3, 4])
        title, body, tab = self.notices[0]
        self.assertIn('4 个重活同时在跑，已让 3 个排队', body)
        self.assertIn('tsc', body)
        self.assertEqual(tab, 'memory')
        self.kills.clear()
        self.tick(NOW + 60, Readings(pressure=4), self.checks())                           # still running: nobody moves
        self.assertEqual(self.kills, [])
        self.tick(NOW + 120, Readings(pressure=2), self.checks(alive=(2, 3, 4)))           # the first is done
        self.assertEqual(self.kills, [(2, signal.SIGCONT)])
        self.assertEqual(W.load(self.home)['leader']['pid'], 2)                            # the one let go is now the runner
        self.kills.clear()
        for minute in (3, 4, 5):                                                           # calm for two minutes
            self.tick(NOW + minute * 60, Readings(pressure=1), self.checks(alive=(2, 3, 4)))
        self.assertEqual(sorted(pid for pid, sig in self.kills if sig == signal.SIGCONT), [3, 4])
        self.assertEqual(W.load(self.home)['paused'], [])

    def test_no_job_waits_longer_than_twenty_minutes_or_past_its_parent(self):
        self.tick(NOW, Readings(pressure=4), self.checks())
        self.kills.clear()
        table = self.checks()
        del table[13]                                                                      # job 3's npm is gone
        self.tick(NOW + 60, Readings(pressure=4), table)
        self.assertEqual(self.kills, [(3, signal.SIGCONT)])
        self.kills.clear()
        self.tick(NOW + 21 * 60, Readings(pressure=4), table)
        self.assertEqual(sorted(pid for pid, sig in self.kills if sig == signal.SIGCONT), [2, 4])

    def test_turned_off_everything_queued_goes_again(self):
        self.tick(NOW, Readings(pressure=4), self.checks())
        self.kills.clear()
        self.cfg(proactive_watch=False)
        result = self.tick(NOW + 60, Readings(pressure=4), self.checks())
        self.assertEqual(result, dict(on=False))
        self.assertEqual(sorted(pid for pid, sig in self.kills if sig == signal.SIGCONT), [2, 3, 4])

    def test_jobs_mostly_swapped_out_still_count_by_what_they_really_hold(self):
        table = {pid: row(pid, 'node /w{}/node_modules/.bin/tsc --noEmit'.format(pid), ppid=pid + 10, hours=0.1, cpu=100.0 - pid, gib=0.18)
                 for pid in (1, 2, 3)}
        table.update({pid + 10: row(pid + 10, 'npm run typecheck', ppid=99, hours=0.1) for pid in (1, 2, 3)})
        readings = Readings(pressure=4)

        def run(args, **kwargs):
            if args[0] == '/usr/bin/top':
                return subprocess.CompletedProcess(args, 0, 'PID    MEM\n1      3040M\n2      3023M+\n3      2.9G\n', '')
            return readings(args, **kwargs)
        result = self.tick(NOW, run, table)
        self.assertEqual(sorted(p['pid'] for p in result['paused']), [2, 3])     # ps shows 0.18 GB each; top 3 GB
        self.assertIn('tsc ×3 8.8 GB', self.notices[0][1])   # whose they are cannot be told here: nothing made up
        self.tick(NOW + 60, run, table)   # every tight minute, not only the one that sent the notice
        [tsc] = [g for g in W.load(self.home)['who']['groups'] if g['tool'] == 'tsc']
        self.assertGreater(tsc['bytes'], 8 * 2 ** 30)
        # Pressure back to normal but 10 GB still in swap: ps would say 0.5 GB, so top still counts (know-who AC-07).
        calm = Readings(pressure=1, swap_used=10.0, swap_total=40.0)   # a quarter of swap, steady: not a squeeze
        self.tick(NOW + 3600, lambda args, **kwargs: run(args, **kwargs) if args[0] == '/usr/bin/top' else calm(args, **kwargs), table)
        [tsc] = [g for g in W.load(self.home)['who']['groups'] if g['tool'] == 'tsc']
        self.assertGreater(tsc['bytes'], 8 * 2 ** 30)

    def test_apps_databases_and_light_jobs_are_never_queued(self):
        table = {1: row(1, '/Applications/Claude.app/Contents/MacOS/Claude', hours=1, cpu=900.0, gib=6.0),
                 2: row(2, 'mongod --dbpath x', hours=1, cpu=800.0, gib=3.0),
                 3: row(3, 'node /a/node_modules/.bin/tsc --noEmit', hours=1, cpu=100.0, gib=0.2),
                 4: row(4, 'node /b/node_modules/.bin/tsc --noEmit', hours=1, cpu=90.0, gib=3.0)}
        result = self.tick(NOW, Readings(pressure=4), table)
        self.assertEqual(result['paused'], [])
        self.assertEqual([k for k in self.kills if k[1] == signal.SIGSTOP], [])


class NoticeTest(WatchFixture):
    def test_one_notice_per_squeeze_naming_who_holds_the_memory(self):
        session = self.home / '.claude/projects/p/s.jsonl'
        session.parent.mkdir(parents=True)
        with session.open('wb') as out:
            out.truncate(W.BIG_SESSION_BYTES + 2 ** 20)
        os.utime(session, (NOW - 600, NOW - 600))
        table = {1: row(1, '/Applications/Claude.app/Contents/MacOS/Claude', hours=1, gib=5.0),
                 2: row(2, '/Applications/Claude.app/Contents/Frameworks/Claude Helper (Renderer).app/Contents/MacOS/x', ppid=1, hours=1, gib=2.0)}
        self.tick(NOW, Readings(pressure=4), table)
        self.tick(NOW + 60, Readings(pressure=4), table)
        self.assertEqual(len(self.notices), 1)
        title, body, _ = self.notices[0]
        self.assertIn('内存在变紧', title)
        self.assertIn('Claude 7.0 GB', body)
        self.assertIn('Claude 有个会话已经 301 MB', body)
        self.tick(NOW + 120, Readings(pressure=1), table)                                   # a flicker, not the end
        self.tick(NOW + 180, Readings(pressure=4), table)
        self.assertEqual(len(self.notices), 1)
        for minute in range(4, 16):                                                          # ten calm minutes: over
            self.tick(NOW + minute * 60, Readings(pressure=1), table)
        self.tick(NOW + 31 * 60, Readings(pressure=4), table)                                # a new squeeze, half an hour on
        self.assertEqual(len(self.notices), 2)


class CooldownTest(TierFixture):
    def test_a_run_that_broke_costs_neither_the_quiet_hours_nor_a_start(self):
        settings = self.settings
        state = {'memory_fired_at': NOW - 7 * 3600}
        E.save_state(self.home, state)
        with patch.object(E, 'memory_pressure', return_value=4), patch.object(E, 'runners', return_value=['codex']):
            got = E.tick(self.home, self.cfg(), settings, NOW, starter=lambda *a: 4242, free=80 * GIB)
        self.assertEqual(got['reason'], 'memory')
        folder = got['folder']
        broken = lambda *a, **k: (_ for _ in ()).throw(TypeError('broken'))
        with patch('ask_tier.launch_app'), self.assertRaises(TypeError):
            E.run(self.home, self.cfg(), settings, 'memory', folder, 'codex', collector=broken)
        after = E.load_state(self.home)
        self.assertEqual(after['memory_fired_at'], NOW - 7 * 3600)   # back to before the run
        self.assertEqual(after.get('starts', []), [])
        self.assertNotIn('run', after)


if __name__ == '__main__':
    unittest.main()
