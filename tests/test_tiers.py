import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'runtime/storage'))
import auto_tier
import tasks
import tiers_config
from portable_config import configure
from storage import GIB, Activity, StorageManager

DAY = 86400


def git(repo, *args):
    return subprocess.run(['git', '-C', str(repo), '-c', 'user.name=t', '-c', 'user.email=t@example.com', *args],
                          check=True, capture_output=True, text=True).stdout


def age(path, days):
    when = time.time() - days * DAY
    for folder, dirs, files in os.walk(path):
        for name in dirs + files:
            try:
                os.utime(os.path.join(folder, name), (when, when), follow_symlinks=False)
            except FileNotFoundError:
                pass   # git may drop a lock or temp file from a background job while we walk
    os.utime(path, (when, when))


class TierFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name).resolve()
        self.root = self.home / 'work/worktrees'; self.root.mkdir(parents=True)
        self.external = self.home / 'ext/worktrees'
        self.remote = self.home / 'remote.git'
        subprocess.run(['git', 'init', '-q', '--bare', str(self.remote)], check=True)
        self.settings = configure(self.home)
        self.settings.target_free_gib, self.settings.warning_free_gib = 10, 5

    def task(self, name, idle_days, dirty=False, unpushed=False, notes=False):
        task = self.root / name; app = task / 'app'; app.mkdir(parents=True)
        git(app, 'init', '-q', '-b', 'main')
        (app / '.gitignore').write_text('build/\nnode_modules/\n')
        (app / 'main.swift').write_text('print(1)\n')
        (app / 'build').mkdir(); (app / 'build/out.o').write_bytes(b'x' * 4096)
        (app / 'node_modules/pkg').mkdir(parents=True); (app / 'node_modules/pkg/i.js').write_text('1')
        (app / 'Pods').mkdir(); (app / 'Pods/tracked.txt').write_text('tracked on purpose')
        git(app, 'add', '.gitignore', 'main.swift', 'Pods'); git(app, 'commit', '-qm', 'init')
        git(app, 'remote', 'add', 'origin', str(self.remote)); git(app, 'push', '-q', '-u', 'origin', 'main:' + name)
        if unpushed:
            (app / 'more.swift').write_text('x'); git(app, 'add', '.'); git(app, 'commit', '-qm', 'local only')
        if dirty:
            (app / 'main.swift').write_text('changed\n')
        if notes:
            (task / 'report.md').write_text('acceptance report')
        age(task, idle_days)
        return task

    def cfg(self, **changes):
        body = {'schema': 1, 'auto_enabled': True,
                'task_roots': [{'path': str(self.root), 'external': str(self.external)}], **changes}
        path = tiers_config.settings_path(self.home); path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(body))
        return tiers_config.load(self.home, owned=[self.settings.cache_roots[0], self.settings.state_root])

    def free_by_links(self, base=1, each=20):
        return lambda: int((base + each * sum(p.is_symlink() for p in self.root.iterdir())) * GIB)

    def run_tier(self, cfg, activity=None, free=None, simctl=None, tool_run=None):
        return auto_tier.run(self.home, cfg, self.settings, activity_factory=lambda: activity or Activity(),
                             simctl=simctl or (lambda a: json.dumps({'devices': {}})), free_fn=free or self.free_by_links(),
                             tool_run=tool_run or (lambda *a, **k: subprocess.CompletedProcess(a, 1, '', '')))


class ConfigTests(TierFixture):
    def test_defaults_keep_auto_off_and_ask_on(self):
        cfg = tiers_config.load(self.home)
        self.assertFalse(cfg['auto_enabled']); self.assertTrue(cfg['ask_enabled'])
        self.assertEqual(cfg['auto_idle_days'], 3); self.assertEqual(cfg['ask']['task_idle_days'], 7)

    def test_wrong_values_name_the_field(self):
        for body, field in [({'auto_idle_days': 0}, 'auto_idle_days'), ({'colour': 1}, 'colour'),
                            ({'task_roots': [{'path': 'relative'}]}, 'task_roots[0].path'),
                            ({'task_roots': [{'path': str(self.root), 'external': '/'}]}, 'task_roots[0].external'),
                            ({'task_roots': [{'path': str(self.home)}]}, 'task_roots[0].path'),
                            ({'ask': {'task_idle_days': True}}, 'ask.task_idle_days')]:
            with self.subTest(field=field):
                path = tiers_config.settings_path(self.home); path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(body))
                with self.assertRaisesRegex(tiers_config.ConfigError, field.replace('[', r'\[').replace(']', r'\]')):
                    tiers_config.load(self.home)

    def test_switch_keeps_other_settings_and_old_config_untouched(self):
        self.cfg(auto_idle_days=5)
        old = self.home / '.config/dev-resource-guard/config.json'; old.write_text('{"target_free_gib": 50, "warning_free_gib": 40, "critical_free_gib": 30}')
        tiers_config.set_flag(self.home, 'auto_enabled', False)
        cfg = tiers_config.load(self.home)
        self.assertFalse(cfg['auto_enabled']); self.assertEqual(cfg['auto_idle_days'], 5)
        self.assertEqual(json.loads(old.read_text())['target_free_gib'], 50)
        configure(self.home)  # the 1.3 reader still accepts its own file


class TaskTests(TierFixture):
    def test_git_state_and_ignored_outputs_only(self):
        clean = self.task('clean', 5); dirty = self.task('dirty', 5, dirty=True); ahead = self.task('ahead', 5, unpushed=True)
        cfg = self.cfg()
        self.assertIsNone(tasks.git_changes(clean / 'app'))
        self.assertIn('未提交', tasks.git_changes(dirty / 'app'))
        self.assertIn('未推送', tasks.git_changes(ahead / 'app'))
        found = sorted(p.name for p in tasks.rebuildable(tasks.Task(self.root, clean, clean), cfg))
        self.assertEqual(found, ['build', 'node_modules'])   # tracked Pods stays

    def test_idle_needs_no_use_no_recent_write_no_pin(self):
        task = self.task('t', 5); cfg = self.cfg(); item = tasks.discover(cfg)[0]
        self.assertEqual(tasks.idle_check(item, Activity(), {}, 3, cfg)[0], [])
        busy = Activity(open_paths=[(42, str(task / 'app/main.swift'))])
        self.assertTrue(tasks.idle_check(item, busy, {}, 3, cfg)[0])
        self.assertTrue(tasks.idle_check(item, Activity(), {str(task): {'reason': 'keep'}}, 3, cfg)[0])
        (task / 'app/new.swift').write_text('fresh')
        self.assertTrue(tasks.idle_check(item, Activity(), {}, 3, cfg)[0])


class LatestWriteTests(TierFixture):
    def test_stops_at_first_recent_write(self):
        task = self.task('t', 5); (task / 'app/new.swift').write_text('fresh')
        cutoff = time.time() - 3 * DAY
        self.assertGreater(tasks.latest_write(task, tasks.skip_names({}), stop_after=cutoff), cutoff)
        self.assertLess(tasks.latest_write(self.task('old', 5), tasks.skip_names({}), stop_after=cutoff), cutoff)


class UnreadableTests(TierFixture):
    def test_unreadable_folder_is_never_idle(self):
        task = self.task('t', 9); locked = task / 'app/private'; locked.mkdir(); age(task, 9); locked.chmod(0)
        self.addCleanup(locked.chmod, 0o755)
        reasons, _ = tasks.idle_check(tasks.Task(self.root, task, task), Activity(), {}, 3, self.cfg())
        self.assertTrue(any('无法完整读取' in r for r in reasons), reasons)


class AutoTierTests(TierFixture):
    def test_moves_oldest_idle_first_and_stops_at_target(self):
        old, mid = self.task('old', 6), self.task('mid', 4)
        self.task('fresh', 1)
        head = git(old / 'app', 'rev-parse', 'HEAD')
        receipt = self.run_tier(self.cfg())
        self.assertEqual([a['kind'] for a in receipt['actions']], ['move'])
        self.assertTrue(old.is_symlink()); self.assertFalse(mid.is_symlink())
        self.assertEqual(old.resolve(), self.external / 'old')
        self.assertEqual(git(old / 'app', 'rev-parse', 'HEAD'), head)
        self.assertEqual(git(old / 'app', 'status', '--porcelain'), '')
        self.assertTrue(any('fresh' in k['path'] for k in receipt['kept']))
        self.assertEqual(self.run_tier(self.cfg())['actions'], [])   # already back above target

    def test_without_external_drive_prunes_ignored_outputs_only(self):
        task = self.task('old', 6)
        cfg = self.cfg(task_roots=[{'path': str(self.root), 'external': '/Volumes/NoSuchGuardDrive/worktrees'}])
        receipt = self.run_tier(cfg, free=lambda: GIB)
        self.assertEqual(sorted(Path(a['path']).name for a in receipt['actions'] if a['kind'] == 'prune'), ['build', 'node_modules'])
        self.assertFalse((task / 'app/build').exists()); self.assertTrue((task / 'app/Pods/tracked.txt').exists())
        self.assertTrue((task / 'app/main.swift').exists()); self.assertEqual(receipt['external_missing'], ['/Volumes/NoSuchGuardDrive/worktrees'])

    def test_denied_external_drive_falls_back_to_pruning_and_says_why(self):
        task = self.task('old', 6)
        with patch.object(auto_tier, 'external_access', return_value='后台任务没有访问外接盘的权限'):
            receipt = self.run_tier(self.cfg(), free=lambda: GIB)
        self.assertFalse(task.is_symlink()); self.assertFalse((task / 'app/build').exists())
        self.assertEqual(receipt['external_denied'], [{'path': str(self.external), 'reason': '后台任务没有访问外接盘的权限'}])
        self.assertEqual(receipt['external_missing'], [])

    def test_in_use_recent_or_pinned_untouched_but_dirty_idle_moves(self):
        busy, pinned = self.task('busy', 6), self.task('pinned', 6)
        dirty = self.task('dirty', 6, dirty=True)
        StorageManager(self.settings).pin(pinned, 'acceptance', 'owner')
        activity = Activity(open_paths=[(7, str(busy))])
        receipt = self.run_tier(self.cfg(), activity=activity, free=self.free_by_links(each=0))
        self.assertTrue(dirty.is_symlink()); self.assertFalse(busy.is_symlink()); self.assertFalse(pinned.is_symlink())
        reasons = {Path(k['path']).name: k['reason'] for k in receipt['kept']}
        self.assertIn('PID 7', reasons['busy']); self.assertIn('保护', reasons['pinned'])
        self.assertTrue((busy / 'app/build').exists())

    def test_change_during_copy_aborts_and_leaves_no_copy(self):
        task = self.task('old', 6)
        real = auto_tier.move
        def changing(home, item, hook=None):
            return real(home, item, hook=lambda: (task / 'app/late.txt').write_text('late'))
        with patch.object(auto_tier, 'move', changing):
            receipt = self.run_tier(self.cfg())
        self.assertEqual(receipt['actions'][0]['result'], 'aborted')
        self.assertFalse(task.is_symlink()); self.assertTrue((task / 'app/main.swift').exists())
        self.assertEqual([p.name for p in self.external.iterdir()], [])
        self.assertFalse((auto_tier.state_root(self.home) / auto_tier.MOVE_STATE).exists())

    def test_unavailable_simulators_and_tool_caches(self):
        calls = []
        def simctl(args):
            calls.append(args)
            if args[:2] == ['list', 'devices']:
                return json.dumps({'devices': {'old-runtime': [{'udid': 'U1', 'name': 'Gone', 'isAvailable': False, 'dataPathSize': 100}],
                                               'ios-26': [{'udid': 'U2', 'name': 'Fine', 'isAvailable': True}]}})
            return ''
        receipt = self.run_tier(self.cfg(), free=lambda: GIB, simctl=simctl)
        self.assertIn(['delete', 'U1'], calls); self.assertNotIn(['delete', 'U2'], calls)
        self.assertEqual([a['kind'] for a in receipt['actions']], ['simulator-delete'])

    def test_disabled_or_roomy_does_nothing(self):
        task = self.task('old', 6)
        self.assertEqual(self.run_tier(self.cfg(auto_enabled=False), free=lambda: GIB)['status'], 'disabled')
        self.assertEqual(self.run_tier(self.cfg(), free=lambda: 50 * GIB)['status'], 'healthy')
        self.assertFalse(task.is_symlink()); self.assertTrue((task / 'app/build').exists())

    def test_recovers_each_interrupted_phase(self):
        task = self.task('old', 6); self.external.mkdir(parents=True)
        marker = auto_tier.state_root(self.home) / auto_tier.MOVE_STATE; marker.parent.mkdir(parents=True, exist_ok=True)
        dest, tmp = self.external / 'old', self.external / '.guard-moving-old'
        def mark(phase):
            marker.write_text(json.dumps(dict(src=str(task), dest=str(dest), tmp=str(tmp), phase=phase)))
        subprocess.run(['/usr/bin/ditto', str(task), str(tmp)], check=True); mark('copying')
        auto_tier.recover(self.home)
        self.assertFalse(tmp.exists()); self.assertTrue((task / 'app/main.swift').exists())
        subprocess.run(['/usr/bin/ditto', str(task), str(dest)], check=True)
        old = task.with_name('.guard-old-old'); os.rename(task, old); mark('swapping')
        auto_tier.recover(self.home)
        self.assertTrue(task.is_symlink()); self.assertFalse(old.exists()); self.assertTrue((task / 'app/main.swift').exists())
        self.assertFalse(marker.exists())

    def test_receipt_and_log_written(self):
        self.task('old', 6)
        self.run_tier(self.cfg())
        state = auto_tier.state_root(self.home)
        self.assertEqual(json.loads((state / 'auto-tier.json').read_text())['actions'][0]['kind'], 'move')
        self.assertEqual(len((state / 'auto-tier-log.jsonl').read_text().splitlines()), 1)


if __name__ == '__main__':
    unittest.main()
