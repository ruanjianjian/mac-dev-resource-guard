import json
import os
from pathlib import Path
import subprocess
import sys
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_tiers import DAY, TierFixture, age, git
import ask_tier
from storage import GIB, Activity, StorageManager


class FakeSimctl:
    def __init__(self, devices, runtimes=None, fail=()):
        self.devices, self.runtimes, self.fail, self.calls = devices, runtimes or {}, set(fail), []

    def __call__(self, args):
        self.calls.append(args)
        if args[:2] == ['list', 'devices']:
            return json.dumps({'devices': self.devices})
        if args[:2] == ['runtime', 'list']:
            return json.dumps(self.runtimes)
        if args[-1] in self.fail:
            raise RuntimeError('device is busy')
        return ''


class AskTierTests(TierFixture):
    IOS = 'com.apple.CoreSimulator.SimRuntime.iOS-27-0'

    def device(self, udid, state, idle_days, name='Phone'):
        data = self.home / 'sims' / udid / 'data'; data.mkdir(parents=True)
        (data.parent / 'device.plist').write_text('x')
        age(data.parent, idle_days)
        return {'udid': udid, 'name': name, 'state': state, 'isAvailable': True, 'dataPath': str(data), 'dataPathSize': 1000}

    def sims(self, booted_attached=False):
        devices = {self.IOS: [self.device('OLD', 'Shutdown', 10), self.device('NEW', 'Shutdown', 1),
                              self.device('ON', 'Booted', 0)]}
        runtimes = {'R1': {'identifier': 'R1', 'runtimeIdentifier': 'com.apple.CoreSimulator.SimRuntime.iOS-26-0', 'version': '26.0',
                           'deletable': True, 'state': 'Ready', 'sizeBytes': 7 * GIB},
                    'R2': {'identifier': 'R2', 'runtimeIdentifier': self.IOS, 'version': '27.0', 'deletable': True, 'state': 'Ready'}}
        return FakeSimctl(devices, runtimes)

    def worktree_task(self, name, idle_days, unpushed=False):
        main = self.home / 'main'
        if not main.exists():
            main.mkdir(); git(main, 'init', '-q', '-b', 'main'); (main / 'a.txt').write_text('a')
            git(main, 'add', '.'); git(main, 'commit', '-qm', 'init')
            git(main, 'remote', 'add', 'origin', str(self.remote)); git(main, 'push', '-q', 'origin', 'main')
        task = self.root / name; task.mkdir()
        git(main, 'worktree', 'add', '-q', '-b', name, str(task / 'main'))
        if not unpushed:
            git(task / 'main', 'push', '-q', '-u', 'origin', name)
        else:
            (task / 'main/b.txt').write_text('b'); git(task / 'main', 'add', '.'); git(task / 'main', 'commit', '-qm', 'local')
        age(task, idle_days)
        return task

    def refresh(self, simctl, activity=None, now=None, **cfg):
        cfg.setdefault('task_roots', [{'path': str(self.root)}])
        return ask_tier.refresh(self.home, self.cfg(**cfg), self.settings, simctl, activity or Activity(), now)

    def ids(self, pending, state='pending'):
        return sorted(i['id'] for i in pending['items'] if i['state'] == state)

    def test_four_kinds_listed_with_what_is_lost(self):
        task = self.worktree_task('done-task', 10)
        simctl = self.sims()
        now = time.time()
        self.refresh(simctl, now=now - 5 * 3600)            # first sight of the unattached booted device
        pending = self.refresh(simctl, now=now)
        self.assertEqual(self.ids(pending), sorted(['simulator-delete:OLD', 'simulator-shutdown:ON', 'runtime-delete:R1',
                                                    'task-delete:' + str(task)]))
        for item in pending['items']:
            self.assertTrue(item['loses'] and item['reason'] and item['title'])
        self.assertEqual(ask_tier.maybe_notify(self.home, pending, 50 * GIB, 10 * GIB, now, launch=lambda: None), 'daily')
        self.assertEqual(json.loads((ask_tier.state_root(self.home) / ask_tier.REQUEST).read_text())['count'], 4)

    def test_never_tier_objects_are_not_proposed(self):
        self.worktree_task('unpushed', 10, unpushed=True)
        loose = self.worktree_task('with-report', 10); (loose / 'report.md').write_text('r'); age(loose, 10)
        pinned = self.worktree_task('pinned', 10); StorageManager(self.settings).pin(pinned, 'keep', 'me')
        simctl = self.sims()
        attached = Activity(commands=[(9, 'xcodebuild -destination id=ON test'), (8, 'flutter run -d OLD')])
        now = time.time()
        self.refresh(simctl, attached, now - 5 * 3600)
        pending = self.refresh(simctl, attached, now)
        self.assertEqual(self.ids(pending), ['runtime-delete:R1'])

    def test_only_confirmed_items_run_after_recheck(self):
        simctl = self.sims(); now = time.time()
        self.refresh(simctl, now=now - 5 * 3600); self.refresh(simctl, now=now)
        simctl.devices[self.IOS][0]['state'] = 'Booted'          # OLD was started meanwhile
        results = ask_tier.execute(self.home, self.cfg(task_roots=[]), self.settings,
                                   ['simulator-delete:OLD', 'runtime-delete:R1'], simctl, Activity(), now)
        outcome = {r['id']: r['outcome'] for r in results}
        self.assertEqual(outcome, {'simulator-delete:OLD': '已跳过', 'runtime-delete:R1': '已完成'})
        self.assertIn(['runtime', 'delete', 'R1'], simctl.calls)
        self.assertFalse(any(c[:1] == ['delete'] for c in simctl.calls))
        self.assertNotIn(['shutdown', 'ON'], simctl.calls)

    def test_repeat_confirm_runs_once_and_failure_stays_listed(self):
        simctl = self.sims(); simctl.fail = {'R1'}
        cfg = self.cfg(task_roots=[])
        self.refresh(simctl, task_roots=[])
        first = ask_tier.execute(self.home, cfg, self.settings, ['runtime-delete:R1', 'simulator-delete:OLD'], simctl, Activity())
        self.assertEqual({r['id']: r['outcome'] for r in first}, {'runtime-delete:R1': '失败', 'simulator-delete:OLD': '已完成'})
        pending = ask_tier.load(self.home, ask_tier.PENDING, None)
        failed = [i for i in pending['items'] if i['id'] == 'runtime-delete:R1'][0]
        self.assertEqual(failed['state'], 'failed'); self.assertIn('busy', failed['result'])
        simctl.devices[self.IOS] = simctl.devices[self.IOS][1:]   # OLD is really gone now
        second = ask_tier.execute(self.home, cfg, self.settings, ['simulator-delete:OLD'], simctl, Activity())
        self.assertEqual(second[0]['outcome'], '已跳过')
        self.assertEqual(sum(c == ['delete', 'OLD'] for c in simctl.calls), 1)
        simctl.fail = set()
        self.assertEqual(ask_tier.execute(self.home, cfg, self.settings, ['runtime-delete:R1'], simctl, Activity())[0]['outcome'], '已完成')

    def test_ignore_until_object_changes(self):
        simctl = self.sims()
        pending = self.refresh(simctl, task_roots=[])
        ask_tier.ignore(self.home, {'simulator-delete:OLD'})
        pending = self.refresh(simctl, task_roots=[])
        self.assertIn('simulator-delete:OLD', self.ids(pending, 'ignored'))
        self.assertNotIn('simulator-delete:OLD', self.ids(pending))
        plist = Path(simctl.devices[self.IOS][0]['dataPath']).parent / 'device.plist'
        old = time.time() - 9 * DAY; os.utime(plist, (old, old))    # booted and shut down again since
        self.assertIn('simulator-delete:OLD', self.ids(self.refresh(simctl, task_roots=[])))

    def test_notice_throttle_daily_and_critical(self):
        pending = self.refresh(self.sims(), task_roots=[]); now = time.time()
        quiet = lambda: None
        self.assertEqual(ask_tier.maybe_notify(self.home, pending, 50 * GIB, 10 * GIB, now, quiet), 'daily')
        self.assertIsNone(ask_tier.maybe_notify(self.home, pending, 50 * GIB, 10 * GIB, now + 60, quiet))
        self.assertEqual(ask_tier.maybe_notify(self.home, pending, 5 * GIB, 10 * GIB, now + 120, quiet), 'critical')
        self.assertIsNone(ask_tier.maybe_notify(self.home, pending, 4 * GIB, 10 * GIB, now + 180, quiet))
        self.assertIsNone(ask_tier.maybe_notify(self.home, pending, 50 * GIB, 10 * GIB, now + 240, quiet))
        self.assertEqual(ask_tier.maybe_notify(self.home, pending, 5 * GIB, 10 * GIB, now + 300, quiet), 'critical')

    def test_task_delete_unregisters_worktree(self):
        task = self.worktree_task('done-task', 10)
        self.refresh(FakeSimctl({}))
        results = ask_tier.execute(self.home, self.cfg(task_roots=[{'path': str(self.root)}]), self.settings,
                                   ['task-delete:' + str(task)], FakeSimctl({}), Activity())
        self.assertEqual(results[0]['outcome'], '已完成', results)
        self.assertFalse(task.exists())
        listed = git(self.home / 'main', 'worktree', 'list', '--porcelain')
        self.assertNotIn('done-task', listed); self.assertNotIn('prunable', listed)

    def test_moved_task_on_unplugged_drive_is_listed_but_not_run(self):
        task = self.worktree_task('moved', 10)
        self.refresh(FakeSimctl({}))
        elsewhere = self.home / 'Gone/moved'; elsewhere.parent.mkdir()
        os.rename(task, elsewhere); os.symlink(elsewhere, task)
        self.refresh(FakeSimctl({}))
        os.rename(elsewhere, self.home / 'unplugged')                 # the drive goes away
        pending = self.refresh(FakeSimctl({}))
        item = [i for i in pending['items'] if i['kind'] == 'task-delete'][0]
        self.assertTrue(item['unreachable']); self.assertEqual(item['reason'], '外接盘未连接')
        result = ask_tier.execute(self.home, self.cfg(task_roots=[{'path': str(self.root)}]), self.settings,
                                  [item['id']], FakeSimctl({}), Activity())
        self.assertEqual((result[0]['outcome'], result[0]['reason']), ('已跳过', '外接盘未连接'))
        self.assertTrue(task.is_symlink())


if __name__ == '__main__':
    unittest.main()
