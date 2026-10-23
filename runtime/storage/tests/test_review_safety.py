"""Regression cases from the destructive-operation review; all data is synthetic."""

import json
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import storage
from storage import Activity, Settings, StorageManager


class ReviewSafetyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="dev-storage-review-")
        self.base = Path(self.tmp.name).resolve()
        self.now = time.time()
        self.settings = Settings(
            cache_roots=[self.base / "cache"],
            work_roots=[self.base / "work"],
            state_root=self.base / "state",
            lease_root=self.base / "leases",
            volume=self.base,
        )
        for path in (
            self.settings.cache_roots[0],
            self.settings.work_roots[0],
            self.settings.lease_root,
        ):
            path.mkdir(parents=True)
        self.manager = StorageManager(self.settings)

    def tearDown(self):
        self.tmp.cleanup()

    def old_cache(self):
        path = (
            self.settings.cache_roots[0]
            / "tasks"
            / "review-task"
            / "xcode"
            / "Build"
            / "Intermediates.noindex"
        )
        path.mkdir(parents=True)
        object_file = path / "object.o"
        object_file.write_text("synthetic rebuildable object")
        old = self.now - 10 * 86400
        for entry in (object_file, path):
            os.utime(entry, (old, old))
        return path

    def plan(self):
        return self.manager.plan(activity=Activity(), free_bytes=0, now=self.now)

    def test_pin_cannot_replace_another_owners_acceptance_hold(self):
        path = self.old_cache().parent.parent
        self.manager.pin(path, "A is awaiting acceptance", owner="session-a")

        with self.assertRaises(ValueError):
            self.manager.pin(path, "B wants a shorter hold", owner="session-b", days=1)

        with self.assertRaises(ValueError):
            self.manager.unpin(path, owner="session-b")
        pins = json.loads(self.manager.pins_path.read_text())
        self.assertEqual(pins[str(path)]["owner"], "session-a")

    def test_pin_completed_during_apply_scan_prevents_deletion(self):
        path = self.old_cache()
        plan = self.plan()
        original_scan = storage.scan_tree
        pin_completed = []

        def scan_then_pin(candidate):
            stats = original_scan(candidate)
            self.manager.pin(
                path.parent.parent,
                "acceptance started while the cache was scanned",
                owner="session-a",
            )
            pin_completed.append(True)
            return stats

        with patch.object(self.manager, "free_bytes", return_value=0), patch(
            "storage.scan_tree", side_effect=scan_then_pin
        ):
            result = self.manager.apply(plan, activity_factory=Activity, now=self.now)

        self.assertTrue(pin_completed, "the hold must complete before the delete decision")
        self.assertEqual(result["deleted"], [])
        self.assertTrue(path.is_dir())
        self.assertEqual((path / "object.o").read_text(), "synthetic rebuildable object")

    def test_ancestor_symlink_swap_at_rename_preserves_outside_sentinel(self):
        path = self.old_cache()
        xcode = path.parent.parent
        original_xcode = xcode.with_name("xcode-original")
        outside = self.base / "outside-allowlist"
        outside_leaf = outside / "Build" / "Intermediates.noindex"
        outside_leaf.mkdir(parents=True)
        sentinel = outside_leaf / "precious"
        sentinel.write_text("outside data must survive")
        plan = self.plan()
        original_rename = os.rename
        swaps = []

        def swap_before_rename(source, destination, *args, **kwargs):
            # Accept absolute paths or fd-relative basenames. The race is at
            # the public rename boundary, independent of deletion helpers.
            if (
                not swaps
                and Path(source).name == "Intermediates.noindex"
                and Path(destination).name.startswith(".dev-storage-purge-")
            ):
                original_rename(xcode, original_xcode)
                xcode.symlink_to(outside, target_is_directory=True)
                swaps.append(True)
            return original_rename(source, destination, *args, **kwargs)

        with patch.object(self.manager, "free_bytes", return_value=0), patch(
            "storage.os.rename", side_effect=swap_before_rename
        ):
            self.manager.apply(plan, activity_factory=Activity, now=self.now)

        self.assertTrue(swaps, "the ancestor must change immediately before rename")
        self.assertTrue(sentinel.exists(), "cleanup must never delete through the new ancestor symlink")
        self.assertEqual(sentinel.read_text(), "outside data must survive")

    def test_old_plan_obeys_longer_retention_after_disk_pressure_resolves(self):
        path = self.old_cache()
        old = self.now - 3 * 86400
        for entry in (path / "object.o", path):
            os.utime(entry, (old, old))
        critical_plan = self.manager.plan(
            activity=Activity(), free_bytes=20 * storage.GIB, now=self.now
        )
        recovered_free = (self.settings.warning_free_gib + 10) * storage.GIB
        current_plan = self.manager.plan(activity=Activity(), free_bytes=recovered_free, now=self.now)
        self.assertEqual(critical_plan["candidates"][0]["decision"], "eligible")
        self.assertEqual(current_plan["candidates"][0]["decision"], "keep")
        self.assertGreater(current_plan["retention_days"], critical_plan["retention_days"])

        with patch.object(self.manager, "free_bytes", return_value=recovered_free):
            result = self.manager.apply(
                critical_plan, activity_factory=Activity, now=self.now
            )

        self.assertEqual(result["deleted"], [])
        self.assertTrue(path.is_dir())

    def test_lsof_success_with_incomplete_warning_disables_cleanup(self):
        path = self.old_cache()

        def inventory(command, **kwargs):
            if Path(command[0]).name == "ps":
                return subprocess.CompletedProcess(command, 0, "", "")
            self.assertEqual(Path(command[0]).name, "lsof")
            return subprocess.CompletedProcess(
                command,
                0,
                "",
                "lsof: WARNING: cannot stat() a mounted filesystem\n"
                "Output information may be incomplete.\n",
            )

        with patch("storage.subprocess.run", side_effect=inventory):
            activity = Activity.collect(self.settings.lease_root)
        plan = self.manager.plan(activity=activity, free_bytes=0, now=self.now)

        self.assertTrue(activity.errors, "an explicitly incomplete inventory is unsafe")
        self.assertEqual(plan["candidates"][0]["decision"], "keep")
        self.assertTrue(path.is_dir())


if __name__ == "__main__":
    unittest.main()
