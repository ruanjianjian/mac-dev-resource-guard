import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'runtime/storage'))
import common
from storage import Activity


class CommonCleanupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name).resolve()
        self.root = self.home / 'Library/Developer/Xcode/DerivedData'
        self.cache = self.root / 'Example-abc/Build/Intermediates.noindex'
        self.cache.mkdir(parents=True)
        (self.cache / 'output.o').write_bytes(b'x' * 8192)
        old = time.time() - 14 * 86400
        for path in [self.cache / 'output.o', self.cache]:
            os.utime(path, (old, old))
        self.products = self.root / 'Example-abc/Build/Products'
        self.products.mkdir()
        (self.products / 'App').write_text('keep')

    def preview(self):
        with patch.object(common, 'activity', return_value=common.CommonActivity()):
            return common.preview(self.home)

    def test_clean_install_discovers_existing_cache_and_reclaims_above_goal(self):
        plan = self.preview()
        self.assertEqual([r['path'] for r in plan['candidates']], [str(self.cache)])
        self.assertEqual(plan['candidates'][0]['decision'], 'eligible')
        with patch.object(common, 'activity', return_value=common.CommonActivity()):
            result = common.apply(self.home, plan)
        self.assertEqual(len(result['deleted']), 1)
        self.assertFalse(self.cache.exists())
        self.assertTrue((self.products / 'App').is_file())
        self.assertFalse((self.home / '.local/bin/devlease').exists())
        self.assertFalse((self.home / 'Library/LaunchAgents').exists())

    def test_new_build_or_open_file_after_preview_is_preserved(self):
        plan = self.preview()
        for active in (common.CommonActivity(commands=[(90, '/usr/bin/xcodebuild -project New.xcodeproj')]),
                       common.CommonActivity(open_paths=[(90, str(self.cache / 'output.o'))]),
                       common.CommonActivity(errors=['inventory incomplete'])):
            with self.subTest(active=active), patch.object(common, 'activity', return_value=active):
                result = common.apply(self.home, plan)
            self.assertFalse(result['deleted'])
            self.assertTrue(result['kept'])
            self.assertTrue(self.cache.exists())

    def test_pin_and_changed_cache_remain_protected(self):
        plan = self.preview()
        storage = common.manager(self.home)
        storage.pin(self.root / 'Example-abc', 'pending acceptance', 'other-owner')
        with patch.object(common, 'activity', return_value=common.CommonActivity()):
            result = common.apply(self.home, plan)
        self.assertFalse(result['deleted'])
        self.assertIn('pinned', result['kept'][0]['reason'])
        with self.assertRaises(ValueError):
            storage.unpin(self.root / 'Example-abc', 'not-owner')

    def test_newly_discovered_cache_is_not_added_to_confirmed_plan(self):
        plan = self.preview()
        other = self.root / 'New/Index.noindex'
        other.mkdir(parents=True)
        old = time.time() - 14 * 86400
        os.utime(other, (old, old))
        with patch.object(common, 'activity', return_value=common.CommonActivity()):
            common.apply(self.home, plan)
        self.assertTrue(other.exists())

    def test_standard_root_symlink_is_rejected(self):
        self.root.rename(self.root.with_name('Moved'))
        self.root.symlink_to(self.root.with_name('Moved'))
        with self.assertRaises(ValueError):
            self.preview()

    def test_scheduler_settings_do_not_discover_standard_cache(self):
        from portable_config import configure
        from storage import StorageManager
        self.assertEqual(StorageManager(configure(self.home)).discover(), [])


if __name__ == '__main__':
    unittest.main()
