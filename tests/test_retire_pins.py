"""11-16, the other Mac (2 TB, 28 GiB left, no drive): tasks done with stayed, build slots outlived their worktrees
and pins without a date held finished work. Retire done tasks, collect slots, list old pins for the user."""
import json
from pathlib import Path
import subprocess
import sys
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from test_ask_tier import AskFixture, FakeSimctl
from test_tiers import DAY, age, git
import agent_plan
import ask_tier
import service
import tasks as T
from storage import GIB, Activity, StorageManager


class RetireFixture(AskFixture):
    def setUp(self):
        super().setUp()
        self.simctl = FakeSimctl({})

    def done_and_open(self):
        """One task whose branch holds nothing beyond main (its work is in the default branch), one whose pushed
        work is not in main and that no PR is known for."""
        done = self.worktree_task('done-task', 10)
        git(self.home / 'main', 'remote', 'set-head', 'origin', 'main')
        busy = self.worktree_task('open-task', 10)
        (busy / 'main/c.txt').write_text('c')
        git(busy / 'main', 'add', '.'); git(busy / 'main', 'commit', '-qm', 'work')
        git(busy / 'main', 'push', '-q', 'origin', 'open-task')
        age(busy, 10)
        return done, busy

    def say_yes(self, days, note='确认处理「x」：闲置 10 天，改动都已提交并推送'):
        """The user's yes in 「等你确认」 to retiring a task, once a day on `days` different days."""
        for back in range(days):
            agent_plan._append(self.home, agent_plan.MEMORY, dict(
                id='m{}'.format(back + 1), at=time.time() - back * DAY, kind='decision', source='confirm',
                about='task-delete', decision='confirmed', path='X', words='', note=note))


class RetireTests(RetireFixture):
    def test_listed_with_where_its_work_is(self):
        done, busy = self.done_and_open()
        pending = self.refresh(self.simctl)
        items = {i['id']: i for i in pending['items']}
        self.assertIn('已在主干', items['task-delete:' + str(done)]['reason'])
        self.assertTrue(items['task-delete:' + str(done)]['done'])
        self.assertFalse(items['task-delete:' + str(busy)]['done'], 'pushed but not merged: asked, never auto')

    def test_three_days_of_yes_learn_it_and_only_done_tasks_go_unasked(self):
        done, busy = self.done_and_open()
        self.say_yes(3)
        self.assertEqual(agent_plan.learn_yes(self.home), ['task-delete'])
        rule = agent_plan.load(self.home)['rules']['task-delete']
        self.assertEqual((rule['action'], rule['params']['min_days']), ('auto', 10))
        cfg = self.cfg(task_roots=[{'path': str(self.root)}])
        made = ask_tier.auto_run(self.home, cfg, self.settings, self.simctl, 'task-delete', short_bytes=100 * GIB,
                                 activity=Activity())
        self.assertEqual([r['id'] for r in made], ['task-delete:' + str(done)])
        self.assertFalse(done.exists())
        self.assertTrue(busy.exists())

    def test_not_short_of_the_target_nothing_goes(self):
        done, _ = self.done_and_open()
        self.say_yes(3)
        agent_plan.learn_yes(self.home)
        cfg = self.cfg(task_roots=[{'path': str(self.root)}])
        self.assertEqual(ask_tier.auto_run(self.home, cfg, self.settings, self.simctl, 'task-delete', short_bytes=0,
                                           activity=Activity()), [])
        self.assertTrue(done.exists())

    def test_the_agent_cannot_make_it_auto(self):
        with self.assertRaises(ValueError):
            agent_plan.set_rule(self.home, 'task-delete', 'auto', by='agent', why='盘不够')

    def test_squash_merged_commits_count_as_saved(self):
        task = self.worktree_task('squashed', 10, unpushed=True)
        repo = task / 'main'
        with patch.object(T, 'pr_state', return_value=None):
            self.assertEqual(T.work_left(repo)[0], '有未推送的提交')
        with patch.object(T, 'pr_state', return_value=('merged', 12)):
            self.assertEqual(T.work_left(repo), (None, ('merged', 12)))
            pending = self.refresh(self.simctl)
        item = next(i for i in pending['items'] if i['id'] == 'task-delete:' + str(task))
        self.assertIn('PR #12 已合入', item['reason'])
        self.assertTrue(item['done'])
        (repo / 'dirty.txt').write_text('unsaved')
        with patch.object(T, 'pr_state', return_value=('merged', 12)):
            self.assertEqual(T.work_left(repo)[0], '有未提交改动', 'a merged PR never covers unsaved files')

    def test_github_is_asked_once_per_commit(self):
        task = self.worktree_task('on-github', 10, unpushed=True)
        repo = task / 'main'
        git(repo, 'remote', 'set-url', 'origin', 'https://github.com/example/app.git')
        calls = []

        def run(args, **kwargs):
            calls.append(args)
            return subprocess.CompletedProcess(args, 0, json.dumps([{'number': 7, 'state': 'MERGED'}]), '')
        memory = {}
        with patch.object(T.shutil, 'which', return_value='/fake/gh'):
            self.assertEqual(T.pr_state(repo, memory, run=run), ('merged', 7))
            self.assertEqual(T.pr_state(repo, memory, run=run), ('merged', 7))
        self.assertEqual(len(calls), 1, 'merged is final: remembered')
        git(repo, 'remote', 'set-url', 'origin', str(self.remote))
        with patch.object(T.shutil, 'which', return_value='/fake/gh'):
            self.assertIsNone(T.pr_state(repo, {}, run=run), 'not on GitHub: nobody to ask')
        self.assertEqual(len(calls), 1)


class PinReleaseTests(RetireFixture):
    def legacy_pin(self, path, days_ago, owner='codex-thread-1'):
        manager = StorageManager(self.settings)
        manager.pin(path, 'awaiting acceptance', owner)
        pins = json.loads(manager.pins_path.read_text())
        key = str(Path(path).resolve())
        pins[key].update(created=time.time() - days_ago * DAY, expires=None)   # how pins were made before 11-16
        manager.pins_path.write_text(json.dumps(pins))
        return key

    def test_old_dateless_pins_are_listed_new_ones_are_not(self):
        old = self.home / 'old-task'; old.mkdir()
        fresh = self.home / 'fresh-task'; fresh.mkdir()
        dated = self.home / 'dated-task'; dated.mkdir()
        old_key = self.legacy_pin(old, 20)
        self.legacy_pin(fresh, 2)
        StorageManager(self.settings).pin(dated, 'acceptance', 'me')
        gone_key = self.legacy_pin(self.home / 'deleted-task', 1)
        pending = self.refresh(self.simctl)
        ids = self.ids(pending)
        self.assertIn('pin-release:' + old_key, ids)
        self.assertIn('pin-release:' + gone_key, ids, 'what it marked is gone: listed whatever its age')
        self.assertFalse(any(i.endswith(str(fresh)) or i.endswith(str(dated)) for i in ids))
        item = next(i for i in pending['items'] if i['id'] == 'pin-release:' + old_key)
        self.assertIn('没写到期日', item['reason'])
        self.assertIn('不删东西', item['loses'])

    def test_the_users_yes_takes_the_mark_off_and_deletes_nothing(self):
        old = self.home / 'old-task'; old.mkdir(); (old / 'keep.txt').write_text('k')
        key = self.legacy_pin(old, 20)
        self.refresh(self.simctl)
        cfg = self.cfg(task_roots=[{'path': str(self.root)}])
        result = ask_tier.execute(self.home, cfg, self.settings, ['pin-release:' + key], self.simctl, Activity())
        self.assertEqual(result[0]['outcome'], '已完成')
        self.assertEqual(json.loads(StorageManager(self.settings).pins_path.read_text()), {})
        self.assertTrue((old / 'keep.txt').exists())

    def test_the_agent_can_ask_for_a_pin_on_finished_work(self):
        import emergency
        task = self.home / 'merged-task'; task.mkdir()
        StorageManager(self.settings).pin(task, 'awaiting acceptance', 'codex')
        asked = emergency.asked_pin(str(task), self.settings)
        self.assertEqual(asked['id'], 'pin-release:' + str(task))
        with self.assertRaises(ValueError):
            emergency.asked_pin(str(self.home / 'nothing-here'), self.settings)


class CleanRoundTests(unittest.TestCase):
    """The 12-hour cache round collects slots whose worktree is gone, unless the rule is off."""

    def setUp(self):
        import tempfile
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name).resolve()
        key = 'a' * 64
        self.folder = self.home / 'Library/Caches/dev-resource-guard/shared/xcode' / key
        (self.folder / 'DerivedData/Build/Products').mkdir(parents=True)
        manifest = self.home / '.local/state/dev-storage/slots' / (key + '.json')
        manifest.parent.mkdir(parents=True)
        manifest.write_text(json.dumps(dict(key=key, worktree=str(self.home / 'work/retired/App'),
                                            cache_path=str(self.folder / 'DerivedData'), last_used=time.time() - DAY)))
        (self.home / 'work').mkdir()

    def round(self):
        with patch.object(service.common, 'activity', return_value=Activity()):
            return service.clean_caches(self.home)

    def test_orphan_slot_goes_in_the_round(self):
        receipt = self.round()
        self.assertFalse(self.folder.exists())
        slots = next(r for r in receipt['results'] if r['family'] == 'build-slots')
        self.assertEqual(len(slots['result']['deleted']), 1)

    def test_a_compile_elsewhere_does_not_hold_up_slots(self):
        """11-17: an acceptance Mac always has some xcodebuild running; that kept every slot (0 of 74 freed)."""
        import common
        busy = common.CommonActivity(commands=[(7, 'xcodebuild -scheme Elys build')])
        self.assertTrue(busy.reasons(self.folder), 'the standard caches still wait for any compile')
        with patch.object(service.common, 'activity', return_value=busy), \
             patch.object(service.Activity, 'collect', return_value=Activity(commands=[(7, 'xcodebuild -scheme Elys build')])):
            receipt = service.clean_caches(self.home)
        self.assertFalse(self.folder.exists())
        self.assertEqual(len(next(r for r in receipt['results'] if r['family'] == 'build-slots')['result']['deleted']), 1)

    def test_a_compile_using_the_slot_keeps_it(self):
        using = Activity(commands=[(7, 'xcodebuild -derivedDataPath {} build'.format(self.folder / 'DerivedData'))])
        with patch.object(service.common, 'activity', return_value=using), \
             patch.object(service.Activity, 'collect', return_value=using):
            service.clean_caches(self.home)
        self.assertTrue(self.folder.exists())

    def project(self, name, workspace):
        import plistlib
        folder = self.home / 'Library/Developer/Xcode/DerivedData' / name
        (folder / 'Build/Intermediates.noindex').mkdir(parents=True)
        (folder / 'info.plist').write_bytes(plistlib.dumps({'WorkspacePath': str(workspace)}))
        return folder

    def test_xcode_folders_of_gone_projects_go_whole(self):
        """11-17: a Runner-… per Flutter checkout in Xcode's own DerivedData grew 48 GiB in four hours."""
        gone = self.project('Runner-gone', self.home / 'work/old-lane/ElysMobileApp/ios/Runner.xcworkspace')
        live_tree = self.home / 'work/live/ios/Runner.xcworkspace'; live_tree.mkdir(parents=True)
        live = self.project('Runner-live', live_tree)
        shared = self.home / 'Library/Developer/Xcode/DerivedData/CompilationCache.noindex'; shared.mkdir()
        unplugged = self.project('Runner-drive', '/Volumes/NoSuchDrive-guard-test/task/Runner.xcworkspace')
        with patch.object(service.common, 'activity', return_value=Activity()), \
             patch.object(service.Activity, 'collect', return_value=Activity(commands=[(7, 'xcodebuild -scheme Elys build')])):
            receipt = service.clean_caches(self.home)
        self.assertFalse(gone.exists())
        for kept in (live, shared, unplugged):
            self.assertTrue(kept.exists(), kept)
        projects = next(r for r in receipt['results'] if r['family'] == 'xcode-projects')
        self.assertEqual([d['path'] for d in projects['result']['deleted']], [str(gone)])

    def test_xcode_folder_of_a_gone_project_still_open_stays(self):
        gone = self.project('Runner-open', self.home / 'work/old/Runner.xcworkspace')
        held = Activity(open_paths=[(9, str(gone / 'Build/Intermediates.noindex/x.o'))])
        with patch.object(service.common, 'activity', return_value=held), patch.object(service.Activity, 'collect', return_value=held):
            service.clean_caches(self.home)
        self.assertTrue(gone.exists())

    def test_rule_off_leaves_slots_alone(self):
        agent_plan.set_rule(self.home, 'build-slots', 'off', by='user', words='构建槽别动')
        receipt = self.round()
        self.assertTrue(self.folder.exists())
        self.assertFalse(any(r['family'] == 'build-slots' for r in receipt['results']))


class SlotRuleTests(unittest.TestCase):
    def test_words_and_cap(self):
        rule = dict(action='clean', params={'max_gib': 0, 'min_hours': 24})
        self.assertIn('磁盘的十分之一', agent_plan.rule_words('build-slots', rule))
        self.assertIn('120 GB', agent_plan.rule_words('build-slots', dict(rule, params={'max_gib': 120, 'min_hours': 24})))
        settings = type('S', (), {'volume': Path('/')})()
        self.assertEqual(service.slot_cap(None, dict(rule, params={'max_gib': 120, 'min_hours': 24}), settings), 120 * GIB)
        total = service.shutil.disk_usage('/').total
        self.assertEqual(service.slot_cap(None, rule, settings), int(total * agent_plan.SLOT_SHARE))


if __name__ == '__main__':
    unittest.main()
