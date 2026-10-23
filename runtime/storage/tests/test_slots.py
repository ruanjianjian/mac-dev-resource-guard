import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import unittest
from unittest import mock

import slots


class SlotTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="dev-storage-slots-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.worktree = self.root / "worktree"
        self.worktree.mkdir()
        self.cache_root = self.root / "cache"
        self.state_root = self.root / "state"
        self.events = self.root / "events.jsonl"
        self.bin_dir = self.root / "bin"
        self.bin_dir.mkdir()
        fake = self.bin_dir / "xcodebuild"
        fake.write_text(textwrap.dedent("""\
            #!/usr/bin/env python3
            import json
            import os
            import sys
            import time

            arguments = sys.argv[1:]
            def argument(flag, default):
                return arguments[arguments.index(flag) + 1] if flag in arguments else default
            def event(kind):
                value = {
                    "event": kind,
                    "pid": os.getpid(),
                    "at": time.time(),
                    "cwd": os.getcwd(),
                    "derived_data": argument("-derivedDataPath", None),
                    "derived_data_count": arguments.count("-derivedDataPath"),
                }
                with open(argument("-events", "events.jsonl"), "a") as output:
                    output.write(json.dumps(value) + "\\n")
            event("start")
            time.sleep(float(argument("-delay", "0")))
            event("end")
            sys.exit(int(argument("-exit", "0")))
            """), encoding="utf-8")
        fake.chmod(0o755)
        xcrun = self.bin_dir / "xcrun"
        xcrun.write_text(
            "#!/usr/bin/env python3\nimport os, sys\nos.execvp(sys.argv[1], sys.argv[1:])\n",
            encoding="utf-8",
        )
        xcrun.chmod(0o755)
        self.environment = {**os.environ, "PATH": str(self.bin_dir) + os.pathsep + os.environ.get("PATH", "")}
        self.environment_patch = mock.patch.dict(os.environ, self.environment)
        self.environment_patch.start()
        self.addCleanup(self.environment_patch.stop)

    def identity(self, **overrides):
        values = {
            "worktree": self.worktree,
            "platform": "iphonesimulator",
            "configuration": "Debug",
            "toolchain": {"developer_dir": "/fake/Xcode", "xcodebuild_version": "fake 1"},
        }
        values.update(overrides)
        return slots.slot_identity(**values)

    def paths(self):
        return slots.slot_paths(self.identity(), self.cache_root, self.state_root)

    def command(self, *extra):
        return ["xcodebuild", "-events", str(self.events), *extra]

    def run_slot(self, command=None):
        return slots.run_xcode(
            self.worktree,
            "iphonesimulator",
            "Debug",
            self.command() if command is None else command,
            self.cache_root,
            self.state_root,
            toolchain=self.identity()["toolchain"],
        )

    def read_manifest(self):
        return json.loads(Path(self.paths()["manifest_path"]).read_text(encoding="utf-8"))

    def assert_lock_available(self):
        with open(self.paths()["lock_path"], "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(lock, fcntl.LOCK_UN)

    def test_identity_reuses_canonical_worktree_and_mapping_order(self):
        alias = self.root / "worktree-alias"
        alias.symlink_to(self.worktree, target_is_directory=True)
        identity = self.identity()
        equivalent = self.identity(
            worktree=alias,
            toolchain={"xcodebuild_version": "fake 1", "developer_dir": "/fake/Xcode"},
        )
        self.assertEqual(identity, equivalent)
        self.assertEqual(identity["worktree"], str(self.worktree.resolve()))
        self.assertRegex(identity["key"], r"^[0-9a-f]{64}$")

    def test_identity_isolates_each_build_dimension(self):
        other = self.root / "other-worktree"
        other.mkdir()
        identities = [
            self.identity(),
            self.identity(worktree=other),
            self.identity(platform="iphoneos"),
            self.identity(configuration="Release"),
            self.identity(toolchain="fake 2"),
        ]
        self.assertEqual(len({value["key"] for value in identities}), len(identities))

    def test_default_toolchain_uses_developer_dir_and_xcode_version(self):
        with mock.patch.dict(os.environ, {"DEVELOPER_DIR": "/fake/Xcode/Contents/Developer"}):
            with mock.patch.object(slots.subprocess, "run") as command:
                command.return_value = subprocess.CompletedProcess(
                    ["xcodebuild", "-version"], 0, "Xcode 99.1\nBuild version TEST\n", ""
                )
                identity = slots.slot_identity(self.worktree, "iphoneos", "Release")
        self.assertEqual(identity["toolchain"]["developer_dir"], "/fake/Xcode/Contents/Developer")
        self.assertEqual(identity["toolchain"]["xcodebuild_version"], "Xcode 99.1\nBuild version TEST")
        self.assertEqual(command.call_args.args[0], ["xcodebuild", "-version"])

    def test_paths_are_stable_external_metadata_and_query_creates_nothing(self):
        identity = self.identity()
        paths = self.paths()
        self.assertEqual(paths, self.paths())
        self.assertEqual(Path(paths["cache_path"]), self.cache_root / identity["key"] / "DerivedData")
        self.assertEqual(Path(paths["lock_path"]), self.state_root / "slot-locks" / (identity["key"] + ".lock"))
        self.assertEqual(Path(paths["manifest_path"]), self.state_root / "slots" / (identity["key"] + ".json"))
        self.assertFalse(self.cache_root.exists())
        self.assertFalse(self.state_root.exists())
        with self.assertRaises(ValueError):
            slots.slot_paths(identity, self.cache_root, self.cache_root / "state")

    def test_explicit_default_xcode_reuses_existing_implicit_slot(self):
        developer = self.root / "Xcode.app/Contents/Developer"
        developer.mkdir(parents=True)
        version = "Xcode 27.0\nBuild version TEST"

        def query(argv, **kwargs):
            if argv == ["xcode-select", "--print-path"]:
                self.assertNotIn("DEVELOPER_DIR", kwargs["env"])
                return subprocess.CompletedProcess(argv, 0, str(developer) + "\n", "")
            self.assertEqual(argv, ["xcodebuild", "-version"])
            return subprocess.CompletedProcess(argv, 0, version, "")

        with mock.patch.object(slots.subprocess, "run", side_effect=query):
            with mock.patch.dict(os.environ, {"DEVELOPER_DIR": ""}):
                implicit = slots.slot_identity(self.worktree, "iphoneos", "Release")
            for explicit in (developer, developer.parent.parent):
                with mock.patch.dict(os.environ, {"DEVELOPER_DIR": str(explicit)}):
                    current = slots.slot_identity(self.worktree, "iphoneos", "Release")
                self.assertEqual(current, implicit)
        legacy = self.identity(platform="iphoneos", configuration="Release", toolchain={
            "developer_dir": "", "xcodebuild_version": version,
        })
        self.assertEqual(implicit, legacy)

    def test_other_xcode_stays_isolated_and_selection_failure_does_not_block(self):
        explicit = str(self.root / "Other.app/Contents/Developer")
        for selection in ("/Applications/Xcode.app/Contents/Developer", None):
            def query(argv, **kwargs):
                if argv == ["xcode-select", "--print-path"]:
                    if selection is None:
                        raise subprocess.TimeoutExpired(argv, 5)
                    return subprocess.CompletedProcess(argv, 0, selection, "")
                return subprocess.CompletedProcess(argv, 0, "Xcode 99", "")
            with mock.patch.dict(os.environ, {"DEVELOPER_DIR": explicit}):
                with mock.patch.object(slots.subprocess, "run", side_effect=query):
                    current = slots.slot_identity(self.worktree, "iphoneos", "Release")
            self.assertEqual(current["toolchain"]["developer_dir"], explicit)
            default = self.identity(platform="iphoneos", configuration="Release", toolchain={
                "developer_dir": "", "xcodebuild_version": "Xcode 99",
            })
            self.assertNotEqual(current["key"], default["key"])

    def test_different_derived_data_path_is_rejected_before_execution(self):
        with self.assertRaises(ValueError):
            self.run_slot(self.command("-derivedDataPath", str(self.root / "foreign-cache")))
        with self.assertRaises(ValueError):
            self.run_slot(self.command("-derivedDataPath=" + str(self.root / "foreign-cache")))
        self.assertFalse(self.events.exists())

    def test_cache_slot_symlink_cannot_escape_cache_root(self):
        self.cache_root.mkdir()
        outside = self.root / "outside-cache"
        outside.mkdir()
        (self.cache_root / self.identity()["key"]).symlink_to(outside, target_is_directory=True)
        with self.assertRaises(ValueError):
            self.run_slot()
        self.assertFalse((outside / "DerivedData").exists())

    def test_manifest_symlink_cannot_put_metadata_inside_cache(self):
        self.cache_root.mkdir()
        self.state_root.mkdir()
        (self.state_root / "slots").symlink_to(self.cache_root, target_is_directory=True)
        with self.assertRaises(ValueError):
            self.paths()

    def test_existing_matching_derived_data_path_is_normalized_once(self):
        cache_path = self.paths()["cache_path"]
        self.assertEqual(self.run_slot(self.command("-derivedDataPath", cache_path)), 0)
        events = [json.loads(line) for line in self.events.read_text().splitlines()]
        self.assertEqual(events[0]["derived_data"], cache_path)
        self.assertEqual(events[0]["derived_data_count"], 1)
        self.assertEqual(events[0]["cwd"], str(self.worktree.resolve()))

    def test_rejects_arbitrary_commands(self):
        for command in ([sys.executable, "-c", "pass"], ["xcrun", "simctl", "list"], "xcodebuild build"):
            with self.subTest(command=command), self.assertRaises(ValueError):
                self.run_slot(command)
        self.assertFalse(self.events.exists())

    def test_xcrun_xcodebuild_is_supported_without_manifest_argv(self):
        secret = "synthetic-private-value-never-persist"
        command = ["xcrun", *self.command(), "TEST_SECRET=" + secret]
        self.assertEqual(self.run_slot(command), 0)
        manifest = self.read_manifest()
        self.assertEqual(manifest["schema_version"], 1)
        self.assertEqual(manifest["cache_path"], self.paths()["cache_path"])
        self.assertIsNone(manifest["pid"])
        self.assertIsInstance(manifest["last_used"], (int, float))
        self.assertGreaterEqual(manifest["last_finished"], manifest["last_used"])
        self.assertNotIn(secret, json.dumps(manifest))
        self.assertNotIn("command", manifest)
        self.assertNotIn("argv", manifest)

    def test_separate_processes_serialize_the_same_slot(self):
        project = str(Path(slots.__file__).resolve().parent)
        code = (
            "import sys; sys.path.insert(0, sys.argv[1]); import slots; "
            "raise SystemExit(slots.run_xcode(sys.argv[2], 'iphonesimulator', 'Debug', "
            "['xcodebuild', '-events', sys.argv[5], '-delay', '0.3'], sys.argv[3], sys.argv[4], "
            "toolchain={'developer_dir': '/fake/Xcode', 'xcodebuild_version': 'fake 1'}))"
        )
        arguments = [sys.executable, "-c", code, project, str(self.worktree), str(self.cache_root), str(self.state_root), str(self.events)]
        children = [subprocess.Popen(arguments, env=self.environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for _ in range(2)]
        for child in children:
            try:
                stdout, stderr = child.communicate(timeout=10)
            except BaseException:
                for running in children:
                    if running.poll() is None:
                        running.kill()
                        running.wait()
                raise
            self.assertEqual(child.returncode, 0, stdout + stderr)
        events = [json.loads(line) for line in self.events.read_text().splitlines()]
        self.assertEqual([event["event"] for event in events], ["start", "end", "start", "end"])
        self.assertNotEqual(events[0]["pid"], events[2]["pid"])
        self.assertGreaterEqual(events[2]["at"], events[1]["at"])
        self.assertEqual({event["derived_data"] for event in events}, {self.paths()["cache_path"]})
        self.assertIsNone(self.read_manifest()["pid"])
        self.assert_lock_available()

    def test_nonzero_exit_clears_pid_and_releases_lock(self):
        self.assertEqual(self.run_slot(self.command("-exit", "7")), 7)
        self.assertIsNone(self.read_manifest()["pid"])
        self.assertIsNotNone(self.read_manifest()["last_finished"])
        self.assert_lock_available()
        self.assertEqual(self.run_slot(), 0)

    def test_spawn_exception_clears_pid_and_releases_lock(self):
        with self.assertRaises(FileNotFoundError):
            self.run_slot([str(self.root / "missing" / "xcodebuild")])
        self.assertIsNone(self.read_manifest()["pid"])
        self.assertIsNotNone(self.read_manifest()["last_finished"])
        self.assert_lock_available()
        self.assertEqual(self.run_slot(), 0)

    def test_wait_exception_stops_child_and_clears_pid(self):
        actual_popen = subprocess.Popen
        children = []

        def interrupted_child(*arguments, **keywords):
            child = actual_popen(*arguments, **keywords)
            children.append(child)
            actual_wait = child.wait
            first_wait = True

            def wait(*wait_arguments, **wait_keywords):
                nonlocal first_wait
                if first_wait:
                    first_wait = False
                    raise RuntimeError("synthetic wait interruption")
                return actual_wait(*wait_arguments, **wait_keywords)

            child.wait = wait
            return child

        with mock.patch.object(slots.subprocess, "Popen", side_effect=interrupted_child):
            with self.assertRaisesRegex(RuntimeError, "synthetic wait interruption"):
                self.run_slot(self.command("-delay", "10"))
        self.assertIsNotNone(children[0].poll())
        self.assertIsNone(self.read_manifest()["pid"])
        self.assert_lock_available()


if __name__ == "__main__":
    unittest.main()
