import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'runtime/storage'))
import slots


class ScriptSlotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.worktree = self.base / 'app'; self.worktree.mkdir()
        self.cache, self.state = self.base / 'cache', self.base / 'state'
        self.out = self.base / 'out.txt'

    def script(self, body):
        path = self.worktree / 'build.sh'
        path.write_text('#!/bin/sh\n' + body + '\n'); path.chmod(0o755)
        return [str(path)]

    def run_script(self, command, configuration='Debug', env_name='MY_DD'):
        return slots.run_script(self.worktree, 'ios-simulator', configuration, command, env_name,
                                cache_root=self.cache, state_root=self.state)

    def manifest(self, configuration='Debug'):
        identity = slots.slot_identity(self.worktree, 'ios-simulator', configuration)
        paths = slots.slot_paths(identity, self.cache, self.state)
        return paths, json.loads(Path(paths['manifest_path']).read_text())

    def test_script_reads_the_same_slot_and_successes_count(self):
        command = self.script('echo "$MY_DD" >> "{}"'.format(self.out))
        self.assertEqual(self.run_script(command), 0); self.assertEqual(self.run_script(command), 0)
        paths, manifest = self.manifest()
        self.assertEqual(self.out.read_text().split(), [paths['cache_path']] * 2)
        self.assertEqual(manifest['successful_builds'], 2)
        self.assertEqual(self.run_script(self.script('exit 3')), 3)
        self.assertEqual(self.manifest()[1]['successful_builds'], 2)

    def test_same_slot_queues_other_configuration_runs_alongside(self):
        command = self.script('echo start $(date +%s.%N 2>/dev/null || python3 -c "import time;print(time.time())") >> "{0}"; '
                              'sleep 1; echo end >> "{0}"'.format(self.out))
        times = {}
        def go(key, configuration):
            start = time.time(); self.run_script(command, configuration); times[key] = (start, time.time())
        same = [threading.Thread(target=go, args=(k, 'Debug')) for k in ('a', 'b')]
        for t in same: t.start()
        for t in same: t.join()
        self.assertGreaterEqual(max(v[1] for v in times.values()) - min(v[0] for v in times.values()), 1.9)
        times.clear()
        other = [threading.Thread(target=go, args=(k, c)) for k, c in (('a', 'Debug'), ('b', 'Release'))]
        for t in other: t.start()
        for t in other: t.join()
        self.assertLess(max(v[1] for v in times.values()) - min(v[0] for v in times.values()), 1.9)

    def test_exit_code_output_and_interrupt_pass_through_and_release_lock(self):
        result = subprocess.run([sys.executable, str(ROOT / 'runtime/storage/cli.py'), 'xcode', '--worktree', str(self.worktree),
                                 '--platform', 'ios-simulator', '--configuration', 'Debug', '--cache-env', 'MY_DD', '--',
                                 *self.script('echo visible; echo problem >&2; exit 7')],
                                capture_output=True, text=True, env=dict(os.environ, HOME=str(self.base)))
        self.assertEqual((result.returncode, result.stdout.strip(), result.stderr.strip()), (7, 'visible', 'problem'))
        runner = subprocess.Popen([sys.executable, '-c', 'import sys; sys.path.insert(0, sys.argv[1]); import slots; '
                                   'sys.exit(slots.run_script(sys.argv[2], "ios-simulator", "Debug", ["sleep", "30"], "MY_DD", sys.argv[3], sys.argv[4]))',
                                   str(ROOT / 'runtime/storage'), str(self.worktree), str(self.cache), str(self.state)],
                                  start_new_session=True)
        # Ctrl-C reaches the whole foreground group; a lone killed wrapper keeps the lock until the build really ends.
        time.sleep(1); os.killpg(runner.pid, signal.SIGINT); runner.wait(timeout=20)
        start = time.time()
        self.assertEqual(self.run_script(self.script('exit 0')), 0)
        self.assertLess(time.time() - start, 5)

    def test_unplugged_cache_drive_is_a_clear_error(self):
        with self.assertRaisesRegex(ValueError, '缓存所在的盘未连接：/Volumes/NoSuchGuardDrive'):
            slots.run_script(self.worktree, 'ios-simulator', 'Debug', ['true'], 'MY_DD',
                             cache_root=Path('/Volumes/NoSuchGuardDrive/cache'), state_root=self.state)
        self.assertFalse(Path('/Volumes/NoSuchGuardDrive').exists())

    def test_bad_variable_name_rejected_and_13_form_unchanged(self):
        with self.assertRaisesRegex(ValueError, '环境变量名'):
            self.run_script(['true'], env_name='1-bad')
        fake = self.worktree / 'xcodebuild'
        fake.write_text('#!/bin/sh\necho "$@" > "{}"\n'.format(self.out)); fake.chmod(0o755)
        code = slots.run_xcode(self.worktree, 'ios-simulator', 'Debug', [str(fake), 'build'], self.cache, self.state)
        self.assertEqual(code, 0); self.assertIn('-derivedDataPath', self.out.read_text())
        slots.run_xcode(self.worktree, 'ios-simulator', 'Debug', [str(fake), '-list'], self.cache, self.state)
        self.assertEqual(self.manifest()[1]['successful_builds'], 1)


if __name__ == '__main__':
    unittest.main()
