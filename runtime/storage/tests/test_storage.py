import fcntl
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from storage import Settings, Activity, StorageManager, scan_tree


class StorageSafetyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.now = time.time()
        self.settings = Settings(
            cache_roots=[self.base / 'DevCache'],
            work_roots=[self.base / 'work'],
            state_root=self.base / 'state',
            lease_root=self.base / 'leases',
            volume=self.base,
        )
        self.settings.cache_roots[0].mkdir()
        self.settings.work_roots[0].mkdir()
        self.settings.lease_root.mkdir()
        self.manager = StorageManager(self.settings)
        self.space = patch.object(self.manager, 'free_bytes', return_value=20 * 2**30)
        self.space.start()
        self.addCleanup(self.space.stop)

    def tearDown(self):
        self.tmp.cleanup()

    def cache(self, name='old-task', age_days=10):
        p = self.settings.cache_roots[0] / 'tasks' / name / 'xcode' / 'Build' / 'Intermediates.noindex'
        p.mkdir(parents=True)
        f = p / 'object.o'
        f.write_bytes(b'x' * 8192)
        old = self.now - age_days * 86400
        os.utime(f, (old, old))
        os.utime(p, (old, old))
        products = p.parent / 'Products'
        products.mkdir(exist_ok=True)
        (products / 'accepted.app').write_text('keep')
        return p

    def plan(self, activity=None, free_gib=20):
        return self.manager.plan(activity=activity or Activity(), free_bytes=int(free_gib * 2**30), now=self.now)

    def test_only_rebuildable_leaves_are_discovered(self):
        p = self.cache()
        arbitrary = self.settings.cache_roots[0] / 'tasks' / 'source' / 'Intermediates.noindex'
        arbitrary.mkdir(parents=True)
        found = {x['path'] for x in self.plan()['candidates']}
        self.assertIn(str(p), found)
        self.assertNotIn(str(arbitrary), found)
        self.assertFalse(any('Products' in x for x in found))

    def test_old_directory_with_recent_nested_file_is_kept(self):
        p = self.cache()
        (p / 'deep').mkdir()
        (p / 'deep' / 'recent.o').write_text('in use recently')
        os.utime(p, (self.now - 20 * 86400,) * 2)
        self.assertEqual(self.plan()['candidates'][0]['decision'], 'keep')

    def test_recent_file_access_is_kept_even_with_old_mtime(self):
        p = self.cache()
        os.utime(p / 'object.o', (self.now, self.now - 20 * 86400))
        self.assertEqual(self.plan()['candidates'][0]['decision'], 'keep')

    def test_process_open_file_protects_whole_task(self):
        p = self.cache()
        a = Activity(open_paths=[(123, str(p.parent / 'Products' / 'accepted.app'))])
        candidate = self.plan(a)['candidates'][0]
        self.assertEqual(candidate['decision'], 'keep')
        self.assertTrue(any('123' in x for x in candidate['reasons']))

    def test_process_argument_with_task_path_protects(self):
        p = self.cache()
        a = Activity(commands=[(321, 'xcodebuild -derivedDataPath ' + str(p.parent.parent))])
        self.assertEqual(self.plan(a)['candidates'][0]['decision'], 'keep')

    def test_unknown_activity_fails_closed(self):
        self.cache()
        self.assertEqual(self.plan(Activity(errors=['lsof timed out']))['candidates'][0]['decision'], 'keep')

    def test_pin_and_corrupt_pin_file_protect(self):
        p = self.cache()
        self.manager.pin(p.parent.parent, 'waiting for human acceptance', owner='test')
        self.assertEqual(self.plan()['candidates'][0]['decision'], 'keep')
        self.manager.pins_path.write_text('not json')
        self.assertEqual(self.plan()['candidates'][0]['decision'], 'keep')

    def test_symlink_escape_is_never_discovered(self):
        outside = self.base / 'outside'
        outside.mkdir()
        (outside / 'precious').write_text('keep')
        link = self.settings.cache_roots[0] / 'tasks'
        link.symlink_to(outside, target_is_directory=True)
        self.assertEqual(self.plan()['candidates'], [])

    def test_preview_does_not_delete(self):
        p = self.cache()
        self.assertEqual(self.plan()['candidates'][0]['decision'], 'eligible')
        self.assertTrue(p.is_dir())

    def test_nested_protected_namespace_preserves_entire_cache_leaf(self):
        cases = [
            'DerivedSources/TTNetworkManager.framework.dSYM/symbols',
            'nested/Products/accepted.app',
            'nested/test-results/summary.txt',
            'nested/Reports/summary.md',
            'nested/result.xcresult/Info.plist',
            'nested/acceptance.xctestrun',
            'nested/signing.p12',
            'nested/private.key',
            'nested/.env',
        ]
        for index, relative in enumerate(cases):
            with self.subTest(relative=relative):
                p = self.cache(name='protected-' + str(index))
                evidence = p / relative
                evidence.parent.mkdir(parents=True, exist_ok=True)
                evidence.write_bytes(b'preserve evidence or credential')
                old = self.now - 10 * 86400
                for entry in [*p.rglob('*'), p]:
                    os.utime(entry, (old, old))
                plan = self.plan()
                candidate = next(x for x in plan['candidates'] if x['path'] == str(p))
                self.assertEqual(candidate['decision'], 'keep')
                self.assertTrue(any('protected cache content' in reason for reason in candidate['reasons']))
                # An old/forged eligible decision still cannot bypass the live scan.
                candidate['decision'] = 'eligible'
                result = self.manager.apply(plan, activity_factory=Activity, now=self.now)
                self.assertEqual(result['deleted'], [])
                self.assertEqual(result['errors'], [])
                self.assertTrue(any(x['path'] == str(p) and 'protected cache content' in x['reason'] for x in result['kept']))
                self.assertEqual(evidence.read_bytes(), b'preserve evidence or credential')
                self.assertTrue((p / 'object.o').is_file())

    def test_special_filesystem_node_keeps_cache_leaf(self):
        p = self.cache()
        special = p / 'unknown-pipe'
        os.mkfifo(special)
        old = self.now - 10 * 86400
        os.utime(special, (old, old))
        os.utime(p, (old, old))
        candidate = self.plan()['candidates'][0]
        self.assertEqual(candidate['decision'], 'keep')
        self.assertTrue(any('special filesystem node' in reason for reason in candidate['reasons']))
        self.assertTrue(special.exists())

    def test_apply_rechecks_new_protected_name_before_any_mutation(self):
        p = self.cache()
        plan = self.plan()
        self.assertEqual(plan['candidates'][0]['decision'], 'eligible')
        # Same inode, blocks, entry count and old file times: scalar stats alone
        # cannot notice that an ordinary cache file became protected evidence.
        evidence = p / 'acceptance.xcresult'
        (p / 'object.o').rename(evidence)
        os.utime(p, (self.now - 10 * 86400,) * 2)
        with patch('storage.os.rename', wraps=os.rename) as rename:
            result = self.manager.apply(plan, activity_factory=Activity, now=self.now)
        self.assertEqual(result['deleted'], [])
        self.assertEqual(result['errors'], [])
        self.assertTrue(any('protected cache content' in x['reason'] for x in result['kept']))
        rename.assert_not_called()
        self.assertTrue(evidence.is_file())

    def test_apply_deletes_intermediates_and_preserves_products_and_symlink_target(self):
        p = self.cache()
        outside = self.base / 'outside'
        outside.mkdir()
        (outside / 'precious').write_text('keep')
        (p / 'external-link').symlink_to(outside)
        old = self.now - 10 * 86400
        os.utime(p, (old, old))
        # Symlink inode timestamps are current; they are not the cache's use evidence.
        plan = self.plan()
        result = self.manager.apply(plan, activity_factory=Activity, now=self.now)
        self.assertEqual(len(result['deleted']), 1)
        self.assertFalse(p.exists())
        self.assertTrue((p.parent / 'Products' / 'accepted.app').exists())
        self.assertEqual((outside / 'precious').read_text(), 'keep')

    def test_recent_write_between_plan_and_apply_is_kept(self):
        p = self.cache()
        plan = self.plan()
        (p / 'new.o').write_text('new build')
        result = self.manager.apply(plan, activity_factory=Activity, now=self.now)
        self.assertEqual(result['deleted'], [])
        self.assertTrue(p.exists())

    def test_active_process_appearing_after_plan_prevents_deletion(self):
        p = self.cache()
        plan = self.plan()
        result = self.manager.apply(plan, activity_factory=lambda: Activity(open_paths=[(999, str(p / 'object.o'))]), now=self.now)
        self.assertEqual(result['deleted'], [])
        self.assertTrue(p.exists())

    def test_cleanup_lock_prevents_second_maintainer(self):
        self.cache()
        plan = self.plan()
        self.manager.state_root.mkdir(parents=True, exist_ok=True)
        with open(self.manager.lock_path, 'a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = self.manager.apply(plan, activity_factory=Activity, now=self.now)
        self.assertEqual(result['deleted'], [])
        self.assertEqual(result['status'], 'busy')

    def test_forged_plan_cannot_delete_source_or_products(self):
        p = self.cache()
        products = p.parent / 'Products'
        plan = self.plan()
        plan['candidates'][0]['path'] = str(products)
        result = self.manager.apply(plan, activity_factory=Activity, now=self.now)
        self.assertEqual(result['deleted'], [])
        self.assertTrue(products.exists())

    def test_tree_scan_does_not_follow_symlinks(self):
        p = self.cache()
        outside = self.base / 'outside'
        outside.mkdir()
        (outside / 'file').write_bytes(b'x' * 1024 * 1024)
        (p / 'external').symlink_to(outside)
        result = scan_tree(p)
        self.assertLess(result['allocated_bytes'], 1024 * 1024)


if __name__ == '__main__':
    unittest.main()
