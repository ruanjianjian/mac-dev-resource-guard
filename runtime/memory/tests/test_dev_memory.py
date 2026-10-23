"""Deterministic safety checks; never inspect or signal workstation processes."""

import copy
import os
import unittest

from scripts import dev_memory as memory


def sample(pressure, boot_id="test-boot"):
    return {
        "pressure": pressure,
        "boot_id": boot_id,
        "swap_used_bytes": 0,
        "swapins": 0,
        "swapouts": 0,
        "page_size": 16384,
    }


class PressureTransitionTests(unittest.TestCase):
    def advance(self, state, pressure, when, boot_id="test-boot"):
        return memory.pressure_transition(state, sample(pressure, boot_id), when)

    def test_warning_requires_three_consecutive_samples(self):
        state = {}
        for when in (1000, 1060):
            state, event = self.advance(state, 2, when)
            self.assertIsNone(event)
        state, event = self.advance(state, 2, 1120)
        self.assertEqual(event, "candidate")

    def test_critical_requires_confirmation_at_least_fifteen_seconds_later(self):
        state, event = self.advance({}, 4, 1000)
        self.assertIsNone(event)
        state, event = self.advance(state, 4, 1014)
        self.assertIsNone(event)
        state, event = self.advance(state, 4, 1015)
        self.assertEqual(event, "candidate")

    def test_unknown_pressure_never_triggers_cleanup(self):
        state = {"warn_count": 99, "last_ts": 1000, "boot_id": "test-boot"}
        state, event = self.advance(state, None, 1060)
        self.assertIsNone(event)

    def test_swap_volume_alone_does_not_trigger_cleanup(self):
        state = {}
        reading = dict(sample(1), swap_used_bytes=100 * 1024 ** 3, swapins=1000000, swapouts=1000000)
        for when in (1000, 1060, 1120):
            state, event = memory.pressure_transition(state, reading, when)
            self.assertIsNone(event)

    def test_unknown_sample_interrupts_warning_streak(self):
        state = {}
        for pressure, when in ((2, 1000), (2, 1060), (None, 1120), (2, 1180), (2, 1240)):
            state, event = self.advance(state, pressure, when)
            self.assertIsNone(event)
        state, event = self.advance(state, 2, 1300)
        self.assertEqual(event, "candidate")

    def test_unknown_sample_does_not_confirm_critical(self):
        state, _ = self.advance({}, 4, 1000)
        state, event = self.advance(state, None, 1015)
        self.assertIsNone(event)
        state, event = self.advance(state, 4, 1030)
        self.assertIsNone(event)

    def test_normal_sample_interrupts_warning_streak(self):
        state = {}
        for pressure, when in ((2, 1000), (2, 1060), (1, 1120), (2, 1180), (2, 1240)):
            state, event = self.advance(state, pressure, when)
            self.assertIsNone(event)

    def test_sleep_gap_resets_consecutive_pressure(self):
        state = {}
        for when in (1000, 1060, 1301, 1361):
            state, event = self.advance(state, 2, when)
            self.assertIsNone(event)
        state, event = self.advance(state, 2, 1421)
        self.assertEqual(event, "candidate")

    def test_boot_change_resets_consecutive_pressure(self):
        state, _ = self.advance({}, 2, 1000)
        state, _ = self.advance(state, 2, 1060)
        state, event = self.advance(state, 2, 1120, boot_id="new-test-boot")
        self.assertIsNone(event)

    def test_backward_clock_resets_consecutive_pressure(self):
        state, _ = self.advance({}, 2, 1000)
        state, _ = self.advance(state, 2, 1060)
        state, event = self.advance(state, 2, 900)
        self.assertIsNone(event)

    def test_cleanup_cooldown_blocks_warning_candidates(self):
        state = {"last_cleanup": 1000}
        for when in (1300, 1360, 1420):
            state, event = self.advance(state, 2, when)
            self.assertIsNone(event)

    def test_cleanup_cooldown_blocks_critical_candidates(self):
        state, _ = self.advance({"last_cleanup": 1000}, 4, 1300)
        state, event = self.advance(state, 4, 1315)
        self.assertIsNone(event)

    def test_candidate_allowed_at_ten_minute_cooldown_boundary(self):
        state = {"last_cleanup": 1000}
        for when in (1480, 1540):
            state, event = self.advance(state, 2, when)
            self.assertIsNone(event)
        state, event = self.advance(state, 2, 1600)
        self.assertEqual(event, "candidate")

    def test_five_normal_samples_end_existing_incident(self):
        state = {"incident_started": 900, "notification_sent": True}
        for when in (1000, 1060, 1120, 1180):
            state, event = self.advance(state, 1, when)
            self.assertIsNone(event)
            self.assertEqual(state.get("incident_started"), 900)
        state, event = self.advance(state, 1, 1240)
        self.assertIsNone(event)
        self.assertIsNone(state.get("incident_started"))
        self.assertFalse(state.get("notification_sent"))


class ProcessPolicyTests(unittest.TestCase):
    def setUp(self):
        self.proc = memory.Process(
            pid=424242,
            ppid=1234,
            uid=os.getuid(),
            start="1700000000:123456",
            comm="tail",
            command="tail -f /synthetic/test.log",
            rss_kib=1024,
        )
        self.record = {
            "pid": self.proc.pid,
            "start": self.proc.start,
            "kind": "tail",
            "state": "idle",
            "disposable": True,
            "supervised": True,
            "owner_dead": True,
        }

    def classify(self, proc=None, record=None, protected=None, knowledge_ok=True):
        return memory.classify_process(
            proc or self.proc,
            self.record if record is None else record,
            set() if protected is None else protected,
            knowledge_ok,
        )

    def test_only_explicitly_registered_idle_resource_is_eligible(self):
        self.assertEqual(self.classify(), "managed_idle")
        record = dict(self.record, kind="mcp")
        self.assertEqual(self.classify(record=record), "managed_idle")

    def test_missing_protection_knowledge_preserves_resource(self):
        self.assertEqual(self.classify(knowledge_ok=False), "knowledge_unavailable")

    def test_active_protection_overrides_idle_registration(self):
        self.assertEqual(self.classify(protected={self.proc.pid}), "protected")

    def test_unregistered_process_is_preserved(self):
        self.assertEqual(memory.classify_process(self.proc, None, set(), True), "unregistered")

    def test_pid_reuse_is_not_treated_as_original_resource(self):
        self.assertEqual(self.classify(record=dict(self.record, start="1700000001:123456")), "identity_changed")

    def test_registration_for_different_pid_is_rejected(self):
        self.assertEqual(self.classify(record=dict(self.record, pid=424243)), "identity_changed")

    def test_live_owner_is_preserved_even_when_resource_is_idle(self):
        self.assertEqual(self.classify(record=dict(self.record, owner_dead=False)), "active_owner")

    def test_explicit_safety_evidence_cannot_be_missing_or_false(self):
        for field in ("disposable", "supervised", "owner_dead", "state"):
            with self.subTest(field=field, mode="missing"):
                record = copy.deepcopy(self.record)
                del record[field]
                self.assertNotEqual(self.classify(record=record), "managed_idle")
            with self.subTest(field=field, mode="false"):
                record = dict(self.record, **{field: False})
                self.assertNotEqual(self.classify(record=record), "managed_idle")

    def test_non_idle_registration_is_preserved(self):
        self.assertNotEqual(self.classify(record=dict(self.record, state="active")), "managed_idle")

    def test_safety_flags_must_be_actual_booleans(self):
        for field in ("disposable", "supervised", "owner_dead"):
            for value in ("true", "false", 1, [], {}):
                with self.subTest(field=field, value=value):
                    self.assertNotEqual(self.classify(record=dict(self.record, **{field: value})), "managed_idle")

    def test_unsupported_resource_kind_is_preserved(self):
        for kind in ("broker", "gradle", "kotlin", "build", "desktop", "simulator", None):
            with self.subTest(kind=kind):
                self.assertEqual(self.classify(record=dict(self.record, kind=kind)), "non_reclaimable")

    def test_other_user_process_is_preserved(self):
        proc = memory.Process(
            pid=self.proc.pid, ppid=self.proc.ppid, uid=self.proc.uid + 1,
            start=self.proc.start, comm=self.proc.comm, command=self.proc.command,
            rss_kib=self.proc.rss_kib,
        )
        self.assertNotEqual(self.classify(proc=proc), "managed_idle")

    def test_mislabelled_working_processes_are_preserved(self):
        commands = (
            ("codex", "codex mcp-server broker"),
            ("java", "java org.gradle.launcher.daemon.bootstrap.GradleDaemon"),
            ("java", "java org.jetbrains.kotlin.daemon.KotlinCompileDaemon"),
            ("xcodebuild", "xcodebuild -scheme SyntheticTest build"),
            ("dart", "dart flutter_tools.snapshot run -d synthetic-device"),
            ("Xcode", "/Applications/Xcode.app/Contents/MacOS/Xcode"),
            ("Simulator", "/Applications/Xcode.app/Contents/Developer/Applications/Simulator.app/Contents/MacOS/Simulator"),
        )
        for comm, command in commands:
            with self.subTest(command=command):
                proc = memory.Process(
                    pid=self.proc.pid, ppid=self.proc.ppid, uid=self.proc.uid,
                    start=self.proc.start, comm=comm, command=command,
                    rss_kib=self.proc.rss_kib,
                )
                self.assertNotEqual(self.classify(proc=proc), "managed_idle")


class ProtectedTreeSafetyTests(unittest.TestCase):
    def setUp(self):
        # On this Mac /usr/bin/python3 resolves inside Xcode.app. Its executable
        # path must not erase an independently verified supervision boundary.
        self.app = memory.Process(
            100, 1, os.getuid(), "app:1", "Codex", "/Applications/Codex.app/Contents/MacOS/Codex")
        self.supervisor = memory.Process(
            101, 100, os.getuid(), "supervisor:1", "Python",
            "/Applications/Xcode.app/Contents/Developer/Library/Frameworks/Python3.framework/Versions/3.9/Resources/Python.app/Contents/MacOS/Python dev_memory.py supervise")
        self.tail = memory.Process(
            102, 101, os.getuid(), "tail:1", "tail", "/usr/bin/tail -f /synthetic/log")
        self.table = {p.pid: p for p in (self.app, self.supervisor, self.tail)}

    def test_verified_supervisor_is_a_boundary_even_when_its_python_is_inside_xcode(self):
        protected = memory.protected_tree(self.table, supervisor_ids={self.supervisor.pid})
        self.assertIn(self.app.pid, protected)
        self.assertNotIn(self.supervisor.pid, protected)
        self.assertNotIn(self.tail.pid, protected)

    def test_unregistered_supervisor_keeps_the_whole_app_process_tree_protected(self):
        protected = memory.protected_tree(self.table)
        self.assertEqual(protected, {self.app.pid, self.supervisor.pid, self.tail.pid})

    def test_build_remains_protected_inside_a_verified_supervisor_boundary(self):
        build = memory.Process(
            self.tail.pid, self.supervisor.pid, os.getuid(), "build:1", "xcodebuild", "xcodebuild -scheme Synthetic build")
        table = {**self.table, build.pid: build}
        protected = memory.protected_tree(table, supervisor_ids={self.supervisor.pid})
        self.assertIn(self.app.pid, protected)
        self.assertIn(build.pid, protected)
        self.assertNotIn(self.supervisor.pid, protected)


if __name__ == "__main__":
    unittest.main()
