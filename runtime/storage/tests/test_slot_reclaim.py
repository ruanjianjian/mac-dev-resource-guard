import fcntl
import json
import os
import tempfile
import time
import unittest
from pathlib import Path

from unittest.mock import patch

import storage
from storage import GIB, Activity, Settings, StorageManager, open_directory_fd, remove_tree_at, worktree_gone

DAY = 86400


class SlotReclaimTests(unittest.TestCase):
    """build-slots (11-16): slots outlived their worktrees — 380 GiB of them on a 2 TB Mac with 28 GiB free."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.now = time.time()
        self.settings = Settings(cache_roots=[self.base / 'DevCache'], state_root=self.base / 'state',
                                 lease_root=self.base / 'leases', volume=self.base)
        self.manager = StorageManager(self.settings)
        self.work = self.base / 'work'
        self.work.mkdir()
        self.sizes = {}
        self.measured = []

    def sizer(self, folder):
        self.measured.append(folder.name)
        return self.sizes.get(folder.name, GIB)

    def slot(self, n, worktree, used_days_ago, gib=1):
        key = '{:064x}'.format(n)
        products = self.base / 'DevCache/shared/xcode' / key / 'DerivedData/Build/Products/Debug'
        products.mkdir(parents=True)
        (products / 'App.app').write_text('app')
        manifest = self.base / 'state/slots' / (key + '.json')
        manifest.parent.mkdir(parents=True, exist_ok=True)
        when = self.now - used_days_ago * DAY
        manifest.write_text(json.dumps(dict(key=key, worktree=str(worktree), cache_path=str(products.parents[2]),
                                            last_used=when, last_finished=when)))
        self.sizes[key] = gib * GIB
        return self.base / 'DevCache/shared/xcode' / key

    def tree(self, name):
        path = self.work / name
        path.mkdir()
        return path

    def reclaim(self, **kwargs):
        kwargs.setdefault('activity_factory', Activity)
        return self.manager.reclaim_slots(now=self.now, sizer=self.sizer, **kwargs)

    def test_slot_of_a_gone_worktree_goes_whole_with_its_manifest(self):
        gone = self.slot(1, self.work / 'retired-task/App', 0.1)
        kept = self.slot(2, self.tree('live'), 30)
        result = self.reclaim()
        self.assertFalse(gone.exists())
        self.assertFalse((self.base / 'state/slots' / (gone.name + '.json')).exists())
        self.assertTrue(kept.exists(), 'no cap: a slot whose worktree is there stays, however old')
        self.assertEqual([d['why'] for d in result['deleted']], ['它的工作区已经不在了'])
        self.assertEqual(result['deleted'][0]['directory_accounting_bytes'], GIB)
        receipt = json.loads((self.base / 'state/last-slots.json').read_text())
        self.assertEqual(receipt['count'], 1)

    def test_unplugged_drive_or_dangling_link_is_not_gone(self):
        self.assertFalse(worktree_gone('/Volumes/NoSuchDrive-guard-test/task/App'))
        link = self.work / 'moved'
        link.symlink_to(self.base / 'unplugged/moved')
        self.assertFalse(worktree_gone(str(link / 'App')), 'a task moved to a drive that is unplugged')
        self.assertTrue(worktree_gone(str(self.work / 'deleted/App')))
        slot = self.slot(3, link / 'App', 10)
        self.reclaim()
        self.assertTrue(slot.exists())

    def test_cap_takes_least_recently_used_first_and_spares_recent_ones(self):
        oldest = self.slot(1, self.tree('a'), 9, gib=10)
        older = self.slot(2, self.tree('b'), 5, gib=10)
        recent = self.slot(3, self.tree('c'), 0.2, gib=10)
        newest_old = self.slot(4, self.tree('d'), 2, gib=10)
        result = self.reclaim(cap_bytes=15 * GIB, min_age=DAY)
        self.assertFalse(oldest.exists())
        self.assertFalse(older.exists())
        self.assertFalse(newest_old.exists())
        self.assertTrue(recent.exists(), 'used within a day: never taken for the cap')
        self.assertEqual(result['total_bytes'], 10 * GIB)

    def test_cap_stops_once_under_it(self):
        old = self.slot(1, self.tree('a'), 9, gib=10)
        mid = self.slot(2, self.tree('b'), 5, gib=10)
        self.reclaim(cap_bytes=15 * GIB, min_age=DAY)
        self.assertFalse(old.exists())
        self.assertTrue(mid.exists())

    def test_pinned_locked_open_or_kept_slots_stay(self):
        pinned_tree = self.work / 'pinned-task/App'
        pinned = self.slot(1, pinned_tree, 9)
        self.manager.pin(self.work / 'pinned-task', 'awaiting acceptance', 'other-session')
        locked = self.slot(2, self.work / 'locked/App', 9)
        open_one = self.slot(3, self.work / 'open/App', 9)
        kept = self.slot(4, self.work / 'kept/App', 9)
        no_manifest = self.base / 'DevCache/shared/xcode' / ('{:064x}'.format(5))
        (no_manifest / 'DerivedData').mkdir(parents=True)
        lock_path = self.base / 'state/slot-locks' / (locked.name + '.lock')
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with open(lock_path, 'a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            result = self.reclaim(activity_factory=lambda: Activity(open_paths=[(42, str(open_one / 'DerivedData/x'))]),
                                  keep=lambda path: path == str(self.work / 'kept/App'))
        for slot in (pinned, locked, open_one, kept, no_manifest):
            self.assertTrue(slot.exists(), slot)
        self.assertEqual(result['deleted'], [])

    def test_unknown_activity_keeps_everything(self):
        slot = self.slot(1, self.work / 'gone/App', 9)
        self.reclaim(activity_factory=lambda: Activity(errors=['lsof failed']))
        self.assertTrue(slot.exists())

    def test_a_build_since_the_scan_keeps_the_slot(self):
        slot = self.slot(1, self.work / 'gone/App', 9)
        manifest = self.base / 'state/slots' / (slot.name + '.json')

        def activity():
            body = json.loads(manifest.read_text())
            body['last_used'] = self.now
            manifest.write_text(json.dumps(body))
            return Activity()
        self.reclaim(activity_factory=activity)
        self.assertTrue(slot.exists())

    def test_sizes_are_reused_until_the_slot_is_built_in_again(self):
        self.slot(1, self.tree('a'), 9)
        self.slot(2, self.tree('b'), 9)
        self.reclaim(cap_bytes=100 * GIB)
        self.assertEqual(len(self.measured), 2)
        self.reclaim(cap_bytes=100 * GIB)
        self.assertEqual(len(self.measured), 2, 'nothing built since: last sizes still hold')


    # 11-18: removing a slot stopped at 「Directory not empty: 'Headers'」 — something kept writing into it — and the
    # whole round stopped there, leaving the rest and a half-removed folder behind.
    def test_files_written_during_removal_are_removed_too(self):
        headers = self.base / 'victim/Build/Headers'
        headers.mkdir(parents=True)
        for i in range(50):
            (headers / 'h{}.h'.format(i)).write_text('x')
        real_unlink, wrote = os.unlink, []

        def unlink(name, *args, **kwargs):
            real_unlink(name, *args, **kwargs)
            if not wrote:   # a build service writes one more header right after the first goes
                wrote.append(True)
                (headers / 'late.h').write_text('late')
        fd = open_directory_fd(self.base)
        try:
            with patch.object(storage.os, 'unlink', side_effect=unlink):
                remove_tree_at(fd, 'victim')
        finally:
            os.close(fd)
        self.assertFalse((self.base / 'victim').exists())

    def test_one_slot_that_fails_does_not_stop_the_rest(self):
        first = self.slot(1, self.work / 'gone-a/App', 9)
        second = self.slot(2, self.work / 'gone-b/App', 9)
        real, calls = storage.remove_tree_at, []

        def flaky(parent_fd, name, expected=None):
            calls.append(name)
            if len(calls) == 1:
                raise OSError(66, 'Directory not empty', 'Headers')
            return real(parent_fd, name, expected)
        with patch.object(storage, 'remove_tree_at', side_effect=flaky):
            result = self.reclaim()
        self.assertEqual(len(result['errors']), 1)
        self.assertEqual(len(result['deleted']), 1)
        self.assertFalse(second.exists(), 'the second slot still went')

    def test_a_half_removed_slot_is_finished_by_the_next_round(self):
        xcode = self.base / 'DevCache/shared/xcode'
        old = xcode / ('.dev-storage-purge-' + 'a' * 64 + '-1')
        (old / 'DerivedData/Build/Headers').mkdir(parents=True)
        (old / 'DerivedData/Build/Headers/x.h').write_text('x')
        fresh = xcode / ('.dev-storage-purge-' + 'b' * 64 + '-2')
        fresh.mkdir(parents=True)
        long_ago = self.now - 3600
        for folder, _, files in os.walk(old):
            os.utime(folder, (long_ago, long_ago))
        result = self.reclaim()
        self.assertFalse(old.exists())
        self.assertTrue(fresh.exists(), 'one being removed right now is left to its own round')
        self.assertEqual([s['error'] for s in result['swept']], [None])


class PinTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name).resolve()
        self.manager = StorageManager(Settings(cache_roots=[base / 'c'], state_root=base / 's', volume=base))
        self.path = base / 'task'
        self.path.mkdir()

    def test_a_pin_lasts_seven_days_unless_told_otherwise(self):
        self.manager.pin(self.path, 'awaiting acceptance', 'me')
        value = json.loads(self.manager.pins_path.read_text())[str(self.path)]
        self.assertAlmostEqual(value['expires'] - value['created'], 7 * DAY, delta=5)
        self.manager.pin(self.path, 'longer', 'me', days=30)
        value = json.loads(self.manager.pins_path.read_text())[str(self.path)]
        self.assertAlmostEqual(value['expires'] - value['created'], 30 * DAY, delta=5)

    def test_the_users_yes_takes_off_anyones_pin(self):
        self.manager.pin(self.path, 'awaiting acceptance', 'another-session')
        with self.assertRaises(ValueError):
            self.manager.unpin(self.path, 'me')
        self.manager.release_pin(self.path)
        self.assertEqual(json.loads(self.manager.pins_path.read_text()), {})
        with self.assertRaises(ValueError):
            self.manager.release_pin(self.path)


if __name__ == '__main__':
    unittest.main()
