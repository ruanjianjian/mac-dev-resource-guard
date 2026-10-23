"""Integration checks for build-slot protection; every path is temporary."""

import fcntl
import json
import os
from pathlib import Path
import plistlib
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import slots
from storage import Activity, Settings, StorageManager


class ManagedCacheIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="dev-storage-managed-gc-")
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name).resolve()
        self.now = time.time()
        self.old = self.now - 30 * 86400
        self.settings = Settings(
            cache_roots=[self.base / "DevCache"],
            work_roots=[self.base / "work"],
            state_root=self.base / "state",
            lease_root=self.base / "leases",
            volume=self.base,
        )
        for root in (*self.settings.cache_roots, *self.settings.work_roots, self.settings.lease_root):
            root.mkdir(parents=True)
        self.worktree = self.settings.work_roots[0] / "managed-worktree"
        self.worktree.mkdir()
        (self.worktree / ".git").write_text("gitdir: synthetic-worktree-metadata\n", encoding="utf-8")
        self.manager = StorageManager(self.settings)

    def old_intermediates(self, derived_data):
        leaf = derived_data / "Build" / "Intermediates.noindex"
        leaf.mkdir(parents=True)
        object_file = leaf / "synthetic-object.o"
        object_file.write_bytes(b"temporary rebuildable fixture" * 512)
        for path in (object_file, leaf):
            os.utime(path, (self.old, self.old))
        products = derived_data / "Build" / "Products"
        products.mkdir()
        (products / "accepted.app").write_text("preserve product", encoding="utf-8")
        return leaf

    def managed_cache(self):
        identity = slots.slot_identity(
            self.worktree,
            "iphonesimulator",
            "Debug",
            toolchain={"developer_dir": "/synthetic/Xcode", "xcodebuild_version": "synthetic version"},
        )
        paths = slots.slot_paths(
            identity,
            self.settings.cache_roots[0] / "shared" / "xcode",
            self.settings.state_root,
        )
        leaf = self.old_intermediates(Path(paths["cache_path"]))
        manifest = {
            **identity,
            "cache_path": paths["cache_path"],
            "last_used": self.old,
            "last_finished": self.old,
            "pid": None,
        }
        self.write_manifest(paths, manifest)
        return leaf, paths, manifest

    @staticmethod
    def write_manifest(paths, manifest):
        path = Path(paths["manifest_path"])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(manifest), encoding="utf-8")

    def plan(self, activity=None):
        return self.manager.plan(
            activity=Activity() if activity is None else activity,
            free_bytes=0,
            now=self.now,
        )

    def candidate(self, plan, path):
        matches = [candidate for candidate in plan["candidates"] if candidate["path"] == str(path)]
        self.assertEqual(len(matches), 1, "the intended temporary leaf must be discovered")
        return matches[0]

    def apply(self, plan, activity=None):
        # Make cleanup necessary regardless of the host's actual free space.
        with patch.object(self.manager, "free_bytes", return_value=0):
            return self.manager.apply(
                plan,
                activity_factory=lambda: Activity() if activity is None else activity,
                now=self.now,
            )

    def assert_kept(self, result, leaf, reason):
        self.assertEqual(result["errors"], [])
        self.assertEqual(result["deleted"], [])
        self.assertTrue(leaf.is_dir())
        self.assertTrue(
            any(entry["path"] == str(leaf) and reason in entry["reason"] for entry in result["kept"]),
            result,
        )

    def test_held_slot_lock_preserves_an_eligible_managed_cache(self):
        leaf, paths, manifest = self.managed_cache()
        plan = self.plan()
        candidate = self.candidate(plan, leaf)
        self.assertEqual(candidate["decision"], "eligible")
        self.assertEqual(candidate["slot_key"], manifest["key"])
        lock_path = Path(paths["lock_path"])
        lock_path.parent.mkdir(parents=True)

        with lock_path.open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = self.apply(plan)
            self.assert_kept(result, leaf, "build slot is locked")

    def test_recent_manifest_use_protects_old_files_in_plan_and_apply(self):
        leaf, paths, manifest = self.managed_cache()
        old_plan = self.plan()
        self.assertEqual(self.candidate(old_plan, leaf)["decision"], "eligible")
        manifest["last_used"] = self.now - 60
        self.write_manifest(paths, manifest)

        candidate = self.candidate(self.plan(), leaf)
        self.assertEqual(candidate["decision"], "keep")
        self.assertEqual(candidate["stats"]["slot_last_used"], manifest["last_used"])
        self.assertIn("used within retention window", candidate["reasons"])
        self.assertLess((leaf / "synthetic-object.o").stat().st_mtime, self.now - 20 * 86400)
        self.assert_kept(self.apply(old_plan), leaf, "retention window")

    def test_source_worktree_pin_protects_its_managed_cache(self):
        leaf, _, _ = self.managed_cache()
        old_plan = self.plan()
        self.assertEqual(self.candidate(old_plan, leaf)["decision"], "eligible")
        self.manager.pin(self.worktree, "waiting for fixture acceptance", owner="integration-test")

        candidate = self.candidate(self.plan(), leaf)
        self.assertEqual(candidate["worktree"], str(self.worktree))
        self.assertEqual(candidate["decision"], "keep")
        self.assertIn("explicitly pinned for acceptance/use", candidate["reasons"])
        self.assert_kept(self.apply(old_plan), leaf, "explicitly pinned for acceptance/use")

    def test_legacy_workspace_maps_git_worktree_and_its_open_source_protects_cache(self):
        source = self.settings.work_roots[0] / "legacy-task" / "SampleApp"
        workspace = source / "ios" / "Runner.xcworkspace"
        workspace.mkdir(parents=True)
        (source / ".git").write_text("gitdir: synthetic-legacy-gitdir\n", encoding="utf-8")
        source_file = source / "main.dart"
        source_file.write_text("synthetic source must survive", encoding="utf-8")
        derived_data = self.settings.cache_roots[0] / "tasks" / "legacy-task" / "xcode" / "DerivedData"
        leaf = self.old_intermediates(derived_data)
        (derived_data / "info.plist").write_bytes(plistlib.dumps({"WorkspacePath": str(workspace)}))
        inactive_plan = self.plan()
        candidate = self.candidate(inactive_plan, leaf)
        self.assertEqual(candidate["decision"], "eligible")
        self.assertEqual(candidate["worktree"], str(source))

        # A real isolated child holds only the temporary source file. Inject its
        # known open-path fact rather than enumerating unrelated host processes.
        holder = subprocess.Popen(
            [sys.executable, "-c", "import sys; f=open(sys.argv[1], 'rb'); print('ready', flush=True); sys.stdin.read()", str(source_file)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            self.assertEqual(holder.stdout.readline().strip(), "ready")
            activity = Activity(open_paths=[(holder.pid, str(source_file))])
            active_candidate = self.candidate(self.plan(activity), leaf)
            self.assertEqual(active_candidate["decision"], "keep")
            self.assertTrue(any("PID " + str(holder.pid) in reason for reason in active_candidate["reasons"]))
            self.assert_kept(self.apply(inactive_plan, activity), leaf, "open file/cwd held by PID " + str(holder.pid))
        finally:
            try:
                holder.communicate(input="", timeout=5)
            except subprocess.TimeoutExpired:
                holder.kill()
                holder.communicate(timeout=5)
        self.assertEqual(holder.returncode, 0)
        self.assertEqual(source_file.read_text(encoding="utf-8"), "synthetic source must survive")

    def test_unprotected_old_managed_cache_is_cleaned_without_products_or_metadata(self):
        leaf, paths, _ = self.managed_cache()
        plan = self.plan()
        self.assertEqual(self.candidate(plan, leaf)["decision"], "eligible")

        result = self.apply(plan)

        self.assertEqual(result["errors"], [])
        self.assertEqual([entry["path"] for entry in result["deleted"]], [str(leaf)])
        self.assertFalse(leaf.exists())
        self.assertTrue((leaf.parent / "Products" / "accepted.app").is_file())
        self.assertTrue(Path(paths["manifest_path"]).is_file())
        self.assertTrue(Path(paths["lock_path"]).is_file())
        self.assertTrue(self.worktree.is_dir())


if __name__ == "__main__":
    unittest.main()
