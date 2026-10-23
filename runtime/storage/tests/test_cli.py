import contextlib
import fcntl
import io
import json
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import cli
import slots
from storage import GIB, Settings, atomic_json


class CliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.settings = Settings(cache_roots=[root / 'cache'], work_roots=[root / 'work'],
                                 state_root=root / 'state', lease_root=root / 'leases', volume=root)
        self.settings.state_root.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def run_cli(self, argv, free=20):
        out = io.StringIO()
        with patch('cli.configure', return_value=self.settings), patch('storage.StorageManager.free_bytes', return_value=free * GIB), contextlib.redirect_stdout(out):
            status = cli.main(argv)
        return status, json.loads(out.getvalue()) if out.getvalue().strip() else None

    def test_physical_space_check_is_advisory_at_low_and_high_space(self):
        status, value = self.run_cli(['check'])
        self.assertEqual(status, 0)
        self.assertTrue(value['build_allowed'])
        self.assertTrue(value['advisory_only'])
        self.assertTrue(value['below_recommended_reserve'])
        status, value = self.run_cli(['check'], free=self.settings.critical_free_gib + 20)
        self.assertEqual(status, 0)
        self.assertTrue(value['build_allowed'])
        self.assertTrue(value['advisory_only'])
        self.assertFalse(value['below_recommended_reserve'])

    def test_xcode_starts_below_advisory_reserve_and_returns_child_status(self):
        with patch('cli.run_xcode', return_value=7) as run:
            status, value = self.run_cli(['xcode', '--worktree', str(self.tmp.name), '--platform', 'macos', '--', 'xcodebuild', 'build'])
        self.assertEqual(status, 7)
        self.assertIsNone(value)
        run.assert_called_once()

    def test_450_gib_does_not_block_small_builds_or_dependency_installation(self):
        commands = [
            ['check'],
            ['check', '--required-gib', '1'],
            ['xcode', '--worktree', self.tmp.name, '--platform', 'macos', '--', 'xcodebuild', 'build'],
            ['deps', '--family', 'pub', '--', 'synthetic-small-install'],
        ]
        for argv in commands:
            with self.subTest(command=argv[0], argv=argv):
                out = io.StringIO()
                with patch('cli.configure', return_value=self.settings), patch(
                    'storage.StorageManager.free_bytes', return_value=450 * GIB
                ), patch('cli.run_xcode', return_value=0) as xcode, patch(
                    'cli.subprocess.run', return_value=subprocess.CompletedProcess(['synthetic-small-install'], 0)
                ) as install, contextlib.redirect_stdout(out):
                    status = cli.main(argv)
                self.assertEqual(status, 0, 'storage targets must not block coding commands')
                if argv[0] == 'check':
                    value = json.loads(out.getvalue())
                    self.assertTrue(value['build_allowed'])
                    self.assertTrue(value['advisory_only'])
                    xcode.assert_not_called()
                    install.assert_not_called()
                elif argv[0] == 'xcode':
                    xcode.assert_called_once()
                    install.assert_not_called()
                else:
                    xcode.assert_not_called()
                    install.assert_called_once()

    def test_xcode_starts_after_real_slot_lock_even_if_free_space_drops(self):
        worktree = Path(self.tmp.name).resolve()
        identity = slots.slot_identity(worktree, 'macos', 'Profile', toolchain='synthetic-xcode')
        paths = slots.slot_paths(identity, self.settings.cache_roots[0], self.settings.state_root)
        lock_path = Path(paths['lock_path'])
        lock_path.parent.mkdir(parents=True)
        original_flock = fcntl.flock
        waiting = threading.Event()
        free = [600 * GIB]
        statuses, failures = [], []
        out = io.StringIO()

        def observe_flock(fd, operation):
            if operation == fcntl.LOCK_EX:
                waiting.set()
            return original_flock(fd, operation)

        def waiting_build():
            try:
                statuses.append(cli.main([
                    'xcode', '--worktree', str(worktree), '--platform', 'macos', '--', 'xcodebuild', 'build'
                ]))
            except BaseException as error:
                failures.append(error)

        with open(lock_path, 'a') as held_lock:
            original_flock(held_lock.fileno(), fcntl.LOCK_EX)
            with patch('cli.configure', return_value=self.settings), patch(
                'storage.StorageManager.free_bytes', side_effect=lambda: free[0]
            ), patch('slots.slot_identity', return_value=identity), patch(
                'slots.slot_paths', return_value=paths
            ), patch('slots.fcntl.flock', side_effect=observe_flock), patch(
                'slots.subprocess.Popen'
            ) as spawn, contextlib.redirect_stdout(out):
                spawn.return_value.pid = 12345
                spawn.return_value.wait.return_value = 0
                spawn.return_value.poll.return_value = 0
                worker = threading.Thread(target=waiting_build, daemon=True)
                worker.start()
                try:
                    self.assertTrue(waiting.wait(3), 'the build must actually reach the held slot lock')
                    self.assertTrue(worker.is_alive(), 'the held lock must delay the build')
                    free[0] = 450 * GIB
                finally:
                    original_flock(held_lock.fileno(), fcntl.LOCK_UN)
                    worker.join(3)
                self.assertFalse(worker.is_alive(), 'the build must finish and release its slot')
                self.assertEqual(failures, [])
                self.assertEqual(statuses, [0])
                spawn.assert_called_once()

        self.assertTrue(Path(paths['manifest_path']).exists())
        self.assertTrue(Path(paths['cache_path']).exists())
        self.assertIsNone(json.loads(Path(paths['manifest_path']).read_text())['pid'])
        self.assertEqual(out.getvalue(), '')

    def test_second_scan_returns_busy_without_traversing_cache(self):
        with open(self.settings.state_root / 'scan.lock', 'a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            with patch('storage.StorageManager.plan') as plan:
                status, value = self.run_cli(['plan'])
        self.assertEqual(value['status'], 'busy')
        plan.assert_not_called()

    def test_active_maintenance_does_not_start_another_scan(self):
        with open(self.settings.state_root / 'maintenance.lock', 'a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            with patch('storage.StorageManager.plan') as plan:
                status, value = self.run_cli(['maintain', '--apply'])
        self.assertEqual(value['status'], 'busy')
        plan.assert_not_called()

    def test_healthy_recent_run_is_a_fast_check(self):
        atomic_json(self.settings.state_root / 'last-plan.json', {'created': time.time()})
        with patch('storage.StorageManager.plan') as plan:
            status, value = self.run_cli(['maintain', '--apply'], free=self.settings.target_free_gib + 20)
        self.assertEqual(value['status'], 'healthy')
        plan.assert_not_called()


if __name__ == '__main__':
    unittest.main()
