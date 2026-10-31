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


class Readings:
    """sysctl answers for one minute: pressure and swap in GB."""
    def __init__(self, pressure=1, swap_used=1.0, swap_total=16.0):
        self.pressure, self.used, self.total = pressure, swap_used, swap_total

    def __call__(self, args, **kwargs):
        if 'kern.memorystatus_vm_pressure_level' in args:
            return subprocess.CompletedProcess(args, 0, '{}\n'.format(self.pressure), '')
        if 'vm.swapusage' in args:
            return subprocess.CompletedProcess(args, 0, 'total = {:.2f}M  used = {:.2f}M  free = 1.00M'.format(
                self.total * 1024, self.used * 1024), '')
        if args[:2] == ['/bin/launchctl', 'list']:
            return subprocess.CompletedProcess(args, 0, 'PID\tStatus\tLabel\n987\t0\tcn.example.service\n', '')
        return subprocess.CompletedProcess(args, 0, '', '')


def row(pid, command, ppid=1, hours=30, cpu=10.0, gib=0.1, lstart='Thu Oct 29 13:22:55 2026'):
    return dict(pid=pid, ppid=ppid, age=hours * 3600, cpu=cpu, rss=int(gib * GIB), lstart=lstart, command=command)


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
                      cwd_of=lambda pids: {p: '/' for p in pids}, notice=self.notice, **kwargs)


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
        self.assertIn('tsc 8.8 GB', self.notices[0][1])

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
        self.tick(NOW + 120, Readings(pressure=1), table)                                   # over
        self.tick(NOW + 180, Readings(pressure=4), table)                                   # a new squeeze: a new notice
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
