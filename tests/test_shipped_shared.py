"""11-18, the user: 「功能都上线了就把开发内容都可以删了。什么都可以删了。只有影响下次做elys其他项目开发的时候的构建进度的可以
不删。也就是那些共享缓存。共享缓存也不是都不能删，也要经常根据项目的实际情况迭代下」."""
import json
import os
from pathlib import Path
import sys
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_ask_tier import AskFixture, FakeSimctl
from test_tiers import DAY, age, git
import agent_plan
import ask_tier
import shared_caches as S
import tasks as T
from storage import Activity, StorageManager

PUBSPEC_LOCK = '''packages:
  args:
    dependency: transitive
    description:
      name: args
      url: "https://pub.dev"
    source: hosted
    version: "2.5.0"
  local_pkg:
    dependency: "direct main"
    description:
      path: "../local_pkg"
    source: path
    version: "1.0.0"
sdks:
  dart: ">=3.0.0 <4.0.0"
'''
PODFILE_LOCK = '''PODS:
  - Alamofire (5.8.1)
  - Firebase/Core (10.1.0):
    - FirebaseCore (= 10.1.0)

DEPENDENCIES:
  - Alamofire
'''


class ShippedFixture(AskFixture):
    def setUp(self):
        super().setUp()
        self.simctl = FakeSimctl({})

    def shipped_task(self, name, hours_idle=48, notes=True):
        """A task whose branch holds nothing beyond main: its work is in the default branch."""
        task = self.worktree_task(name, 0)
        git(self.home / 'main', 'remote', 'set-head', 'origin', 'main')
        if notes:
            (task / 'notes.md').write_text('验收记录')
        age(task, hours_idle / 24)
        return task

    def cfg_(self):
        return self.cfg(task_roots=[{'path': str(self.root)}])

    def set_rule(self, rid, action):
        agent_plan.set_rule(self.home, rid, action, by='user', words='做进去')


class ShippedTests(ShippedFixture):
    def test_a_shipped_task_with_notes_is_listed_whole(self):
        task = self.shipped_task('shipped', hours_idle=48)
        pending = self.refresh(self.simctl)
        item = next(i for i in pending['items'] if i['id'] == 'task-shipped:' + str(task))
        self.assertIn('已在主干', item['reason'])
        self.assertIn('笔记', item['loses'])

    def test_unsaved_or_unmerged_work_is_never_shipped(self):
        dirty = self.shipped_task('dirty')
        (dirty / 'main/wip.txt').write_text('not committed')
        age(dirty, 2)
        unpushed = self.worktree_task('unpushed', 2, unpushed=True)
        with patch.object(T, 'pr_state', side_effect=lambda repo, *a, **k: None if 'unpushed' in str(repo) else ('merged', None)):
            ids = self.ids(self.refresh(self.simctl))
        self.assertFalse(any(i.endswith('/dirty') or i.endswith('/unpushed') for i in ids if i.startswith('task-shipped:')))

    def test_recently_touched_waits(self):
        self.shipped_task('fresh', hours_idle=2)
        self.assertFalse(any(i.startswith('task-shipped:') for i in self.ids(self.refresh(self.simctl))))

    def test_confirmed_it_goes_with_its_own_cache(self):
        task = self.shipped_task('shipped')
        cache = self.settings.cache_roots[0] / 'tasks' / 'shipped'
        cache.mkdir(parents=True)
        (cache / 'pods.zip').write_text('x')
        self.refresh(self.simctl)
        result = ask_tier.execute(self.home, self.cfg_(), self.settings, ['task-shipped:' + str(task)], self.simctl, Activity())
        self.assertEqual(result[0]['outcome'], '已完成')
        self.assertFalse(task.exists())
        self.assertFalse(cache.exists())

    def test_the_rule_set_to_auto_takes_them_each_round(self):
        task = self.shipped_task('shipped')
        self.assertEqual(ask_tier.run_policy(self.home, self.cfg_(), self.settings, self.simctl, 'task-shipped',
                                             activity=Activity()), [], 'asked first by default')
        self.assertTrue(task.exists())
        self.set_rule('task-shipped', 'auto')
        done = ask_tier.run_policy(self.home, self.cfg_(), self.settings, self.simctl, 'task-shipped', activity=Activity())
        self.assertEqual([r['id'] for r in done], ['task-shipped:' + str(task)])
        self.assertFalse(task.exists())

    def test_only_the_user_makes_it_auto(self):
        with self.assertRaises(ValueError):
            agent_plan.set_rule(self.home, 'task-shipped', 'auto', by='agent', why='盘不够')
        agent_plan.set_rule(self.home, 'shared-caches', 'auto', by='agent', why='这些版本没有项目在用')

    def test_a_pin_on_shipped_code_is_listed_however_new(self):
        task = self.shipped_task('pinned-shipped')
        open_task = self.worktree_task('pinned-open', 2)
        (open_task / 'main/c.txt').write_text('c'); git(open_task / 'main', 'add', '.'); git(open_task / 'main', 'commit', '-qm', 'work')
        git(open_task / 'main', 'push', '-q', 'origin', 'pinned-open')
        manager = StorageManager(self.settings)
        manager.pin(task, 'awaiting acceptance', 'codex', days=30)
        manager.pin(open_task, 'awaiting acceptance', 'codex', days=30)
        with patch.object(T, 'pr_state', side_effect=lambda repo, *a, **k: ('open', 3) if 'pinned-open' in str(repo) else ('merged', 12)):
            pending = self.refresh(self.simctl)
        items = {i['id']: i for i in pending['items']}
        self.assertIn('已经上线', items['pin-release:' + str(task.resolve())]['reason'])
        self.assertNotIn('pin-release:' + str(open_task.resolve()), items)


class SharedCacheTests(ShippedFixture):
    def flutter_repo(self):
        app = self.root / 'flutter-task' / 'app'
        app.mkdir(parents=True)
        git(app, 'init', '-q', '-b', 'main')
        (app / 'pubspec.lock').write_text(PUBSPEC_LOCK)
        (app / 'ios').mkdir(); (app / 'ios/Podfile.lock').write_text(PODFILE_LOCK)
        (app / '.fvmrc').write_text(json.dumps({'flutter': '3.41.9'}))
        (app / 'android/gradle/wrapper').mkdir(parents=True)
        (app / 'android/gradle/wrapper/gradle-wrapper.properties').write_text(
            'distributionUrl=https\\://services.gradle.org/distributions/gradle-8.14-bin.zip\n')
        git(app, 'add', '.'); git(app, 'commit', '-qm', 'init')
        return app

    def caches(self):
        made = {}
        for rel in ('fvm/versions/3.41.9', 'fvm/versions/3.24.0', '.pub-cache/hosted/pub.dev/args-2.5.0',
                    '.pub-cache/hosted/pub.dev/args-2.4.0', '.pub-cache/hosted/pub.dev/local_pkg-1.0.0',
                    'Library/Caches/CocoaPods/Pods/Release/Alamofire/5.8.1-abcde',
                    'Library/Caches/CocoaPods/Pods/Release/Alamofire/5.6.0-xyz12',
                    'Library/Caches/CocoaPods/Pods/Release/Firebase/10.1.0-aaaaa',
                    '.gradle/wrapper/dists/gradle-8.14-bin', '.gradle/wrapper/dists/gradle-7.6-all', '.gradle/caches'):
            path = self.home / rel
            path.mkdir(parents=True)
            (path / 'f').write_text('x')
            made[rel] = path
        return made

    def test_the_lock_files_say_what_is_used(self):
        app = self.flutter_repo()
        refs = S.uses(S.lock_files(app))
        self.assertEqual(refs['pub'], {('args', '2.5.0')})
        self.assertEqual(refs['cocoapods'], {('Alamofire', '5.8.1'), ('Firebase', '10.1.0')})
        self.assertEqual((refs['flutter'], refs['gradle'], refs['gradle_project']), ({'3.41.9'}, {'8.14'}, True))

    def test_versions_no_project_uses_are_listed_and_go(self):
        os.environ.pop('PUB_CACHE', None)
        self.flutter_repo()
        made = self.caches()
        pending = self.refresh(self.simctl)
        items = {i['id']: i for i in pending['items'] if i['kind'] == 'shared-caches'}
        self.assertEqual(sorted(items), ['shared-caches:cocoapods', 'shared-caches:flutter-sdk', 'shared-caches:gradle',
                                         'shared-caches:pub'])
        self.assertEqual([Path(p).name for p in items['shared-caches:pub']['paths']], ['args-2.4.0', 'local_pkg-1.0.0'])
        self.assertEqual([Path(p).name for p in items['shared-caches:gradle']['paths']], ['gradle-7.6-all'],
                         'a project builds with Gradle: its caches stay')
        result = ask_tier.execute(self.home, self.cfg_(), self.settings, sorted(items), self.simctl, Activity())
        self.assertTrue(all(r['outcome'] == '已完成' for r in result), result)
        for kept in ('fvm/versions/3.41.9', '.pub-cache/hosted/pub.dev/args-2.5.0',
                     'Library/Caches/CocoaPods/Pods/Release/Alamofire/5.8.1-abcde', '.gradle/wrapper/dists/gradle-8.14-bin',
                     '.gradle/caches'):
            self.assertTrue(made[kept].exists(), kept)
        for gone in ('fvm/versions/3.24.0', '.pub-cache/hosted/pub.dev/args-2.4.0',
                     'Library/Caches/CocoaPods/Pods/Release/Alamofire/5.6.0-xyz12', '.gradle/wrapper/dists/gradle-7.6-all'):
            self.assertFalse(made[gone].exists(), gone)

    def test_no_project_found_lists_nothing(self):
        self.caches()
        self.assertFalse(any(i.startswith('shared-caches:') for i in self.ids(self.refresh(self.simctl))))

    def test_a_version_in_use_stays(self):
        self.flutter_repo()
        made = self.caches()
        held = Activity(open_paths=[(5, str(made['.pub-cache/hosted/pub.dev/args-2.4.0'] / 'f'))])
        pending = self.refresh(self.simctl, activity=held)
        pub = next(i for i in pending['items'] if i['id'] == 'shared-caches:pub')
        self.assertNotIn(str(made['.pub-cache/hosted/pub.dev/args-2.4.0']), pub['paths'])


if __name__ == '__main__':
    unittest.main()
