"""Memory alerts arrive early, survive a slow monitor, and are retried until delivered."""
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts import dev_memory as memory
from scripts import memory_external as ext


def sample(pressure, boot_id="test-boot"):
    return {"pressure": pressure, "boot_id": boot_id, "swap_used_bytes": 0, "compressed_bytes": 0, "swapins": 0, "swapouts": 0, "page_size": 16384}


class AlertTimingTests(unittest.TestCase):
    def run_samples(self, readings, state=None):
        state = dict(state or {})
        for pressure, when in readings:
            state, _ = memory.pressure_transition(state, sample(pressure), when)
        return state

    def test_warning_alerts_after_two_minutes_not_five(self):
        state = self.run_samples([(2, 1000), (2, 1060)])
        self.assertFalse(memory.alert_due(state, 2, 1060))
        state = self.run_samples([(2, 1120)], state)
        self.assertTrue(memory.alert_due(state, 2, 1120))

    def test_critical_alerts_at_once(self):
        state = self.run_samples([(4, 1000)])
        self.assertTrue(memory.alert_due(state, 4, 1000))

    def test_a_monitor_timeout_does_not_restart_the_alert_clock(self):
        # 1060 → 1300 is a four-minute gap (two timed-out samples): the cleanup streak restarts,
        # the incident does not, so the alert still comes two minutes after pressure began.
        state = self.run_samples([(2, 1000), (2, 1060), (2, 1300)])
        self.assertEqual(state["incident_started"], 1000)
        self.assertEqual(state["warn_count"], 1)
        self.assertTrue(memory.alert_due(state, 2, 1300))

    def test_reboot_or_long_sleep_starts_over(self):
        state = self.run_samples([(2, 1000), (2, 1060)])
        self.assertEqual(self.run_samples([(2, 2000)], state)["incident_started"], 2000)
        rebooted, _ = memory.pressure_transition(state, sample(2, "new-boot"), 1120)
        self.assertEqual(rebooted["incident_started"], 1120)

    def test_reminder_and_escalation_after_a_delivered_alert(self):
        state = {"incident_started": 1000, "notification_sent": True, "notification_level": 2, "notification_at": 1120}
        self.assertFalse(memory.alert_due(state, 2, 1500))
        self.assertTrue(memory.alert_due(state, 4, 1500), "warning became critical")
        self.assertTrue(memory.alert_due(state, 2, 1120 + 1800), "still under pressure half an hour later")
        self.assertFalse(memory.alert_due(state, 1, 9000))


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        patcher = mock.patch.object(memory, "STATE", self.dir)
        patcher.start(); self.addCleanup(patcher.stop)

    def test_the_app_delivers_and_acknowledges(self):
        def app_acks(*_):
            alert = json.loads((self.dir / "alert.json").read_text())
            (self.dir / "alert-ack.json").write_text(json.dumps({"id": alert["id"]}))
        with mock.patch.object(memory.time, "sleep", side_effect=app_acks), \
             mock.patch.object(memory.subprocess, "run", side_effect=AssertionError("no fallback needed")):
            self.assertTrue(memory.notify("主要占用：…", "内存紧张，已持续 2 分钟"))
        alert = json.loads((self.dir / "alert.json").read_text())
        self.assertEqual(alert["title"], "内存紧张，已持续 2 分钟")

    def test_without_the_app_it_falls_back_and_reports_failure_honestly(self):
        with mock.patch.object(memory.time, "sleep"), \
             mock.patch.object(memory.subprocess, "run", return_value=subprocess.CompletedProcess([], 1)) as run:
            self.assertFalse(memory.notify("x", "内存紧张", wait=0.05))
        self.assertEqual(run.call_args[0][0][0], "/usr/bin/osascript")

    def test_a_failed_delivery_is_not_recorded_and_is_tried_again(self):
        state = {"incident_started": 1000, "last_ts": 1060, "boot_id": "test-boot", "warn_count": 1}
        saved = {}
        stack = [mock.patch.object(memory, "locked", mock.MagicMock()),
                 mock.patch.object(memory, "read_json", side_effect=lambda p, d=None: dict(state) if p.name == "state.json" else d),
                 mock.patch.object(memory, "atomic_json", side_effect=lambda p, v: saved.__setitem__(p.name, v)),
                 mock.patch.object(memory, "log_event"),
                 mock.patch.object(memory, "sample_system", return_value=dict(sample(2), at=1180)),
                 mock.patch.object(memory, "protection_knowledge", return_value=[]),
                 mock.patch.object(memory, "process_table", return_value={}),
                 mock.patch.object(memory, "top_memory", return_value=[]),
                 mock.patch.object(memory, "inspect_resources", return_value=([], [])),
                 mock.patch.object(memory, "reconcile_external"),
                 mock.patch.object(memory, "inspect_external_resources", return_value=([], {})),
                 mock.patch.object(memory.time, "time", return_value=1180)]
        for p in stack:
            p.start(); self.addCleanup(p.stop)
        with mock.patch.object(memory, "notify", return_value=False) as failed:
            memory.run_cycle("tick")
        self.assertEqual(failed.call_count, 1)
        self.assertFalse(saved["state.json"].get("notification_sent"))
        state = saved["state.json"]
        with mock.patch.object(memory, "notify", return_value=True) as again, \
             mock.patch.object(memory.time, "time", return_value=1240):
            memory.run_cycle("tick")
        self.assertEqual(again.call_count, 1)
        self.assertTrue(saved["state.json"]["notification_sent"])


class SimulatorTests(unittest.TestCase):
    def fake(self, args, **kwargs):
        if args[:2] == ["/usr/bin/xcrun", "simctl"]:
            out = json.dumps({"devices": {"iOS-27": [{"udid": "AAA", "name": "Busy Phone"}, {"udid": "BBB", "name": "Idle Phone"}]}})
        else:
            out = "\n".join([
                " 10 1 100 /sbin/launchd_sim /Users/me/Library/Developer/CoreSimulator/Devices/AAA/data/var/run/launchd_bootstrap.plist",
                " 11 10 2000000 /Applications/Busy.app/Busy",
                " 20 1 100 /sbin/launchd_sim /Users/me/Library/Developer/CoreSimulator/Devices/BBB/data/var/run/launchd_bootstrap.plist",
                " 21 20 1000000 /Applications/Idle.app/Idle",
                " 30 1 50000 /usr/bin/xcodebuild test -destination id=AAA",
                " 31 1 900 /Library/Developer/PrivateFrameworks/CoreSimulator.framework/CoreSimulatorService AAA BBB",
            ])
        return subprocess.CompletedProcess(args, 0, out, "")

    def test_memory_per_simulator_and_who_is_connected(self):
        rows = {r["name"]: r for r in ext.simulator_inspect(run=self.fake, now=1)}
        self.assertEqual(rows["Busy Phone"]["rss_kib"], 2000100)
        self.assertEqual((rows["Busy Phone"]["reason"], rows["Busy Phone"]["attached"]), ("simulator_in_use", ["xcodebuild"]))
        self.assertEqual((rows["Idle Phone"]["reason"], rows["Idle Phone"]["attached"]), ("simulator_not_attached", []))
        self.assertFalse(any(r["eligible"] for r in rows.values()), "nothing is shut down without a decision")

    def test_without_devlease_the_own_check_is_used(self):
        with mock.patch.object(ext, "DEVLEASE", Path("/nonexistent/devlease")), \
             mock.patch.object(ext, "simulator_inspect", return_value=[{"id": "BBB"}]) as own:
            self.assertEqual(ext.device_inspect(), [{"id": "BBB"}])
        own.assert_called_once()

    def test_the_alert_says_how_much_the_simulators_take(self):
        sims = [{"kind": "simulator", "rss_kib": 4 << 20, "reason": "simulator_not_attached"},
                {"kind": "simulator", "rss_kib": 5 << 20, "reason": "simulator_in_use"}]
        message = memory.notification_message([{"name": "Xcode"}], [], {}, sims)
        self.assertIn("2 台模拟器占约 9 GB（1 台没被开发工具连着）", message)


class AutoCloseTests(unittest.TestCase):
    """Choice A: idle two hours and memory tight → closed automatically; otherwise one click."""
    def rows(self, reason="simulator_not_attached"):
        return [{"id": "BBB", "device_id": "BBB", "kind": "ios-sim", "name": "Idle Phone", "rss_kib": 1 << 21,
                 "attached": [], "eligible": False, "reason": reason, "_observed_at": 0}]

    def collect(self, state, now, reason="simulator_not_attached"):
        with mock.patch.object(ext, "device_inspect", return_value=self.rows(reason)), \
             mock.patch.object(ext, "adapter", side_effect=lambda name: mock.Mock(inspect=lambda now: [])):
            return ext.collect(state, now)

    def test_two_hours_without_a_dev_tool_makes_it_a_candidate(self):
        state = {}
        public, _ = self.collect(state, 1000)
        self.assertFalse(public[0]["eligible"])
        self.assertEqual(state["simulator_idle_since"], {"simulator:BBB": 1000})
        public, _ = self.collect(state, 1000 + 7200)
        self.assertFalse(public[0]["eligible"], "a second look 30 s later is still required")
        public, _ = self.collect(state, 1000 + 7260)
        self.assertTrue(public[0]["eligible"])
        self.assertEqual(public[0]["unattached_since"], 1000)

    def test_a_dev_tool_connecting_restarts_the_clock(self):
        state = {}
        self.collect(state, 1000)
        self.collect(state, 5000, reason="simulator_in_use")
        public, _ = self.collect(state, 9000)
        self.assertEqual(public[0]["unattached_since"], 9000)

    def test_closing_rechecks_and_spares_one_that_just_got_connected(self):
        def run(args, **kwargs):
            if args[:3] == ["/usr/bin/xcrun", "simctl", "shutdown"]:
                raise AssertionError("must not shut down a simulator in use")
            return SimulatorTests().fake(args)
        self.assertEqual(ext.shutdown_idle_simulator("AAA", run=run)["reason"], "simulator_in_use")
        calls = []
        def run_idle(args, **kwargs):
            calls.append(args)
            return subprocess.CompletedProcess(args, 0, "", "") if "shutdown" in args else SimulatorTests().fake(args)
        result = ext.shutdown_idle_simulator("BBB", run=run_idle)
        self.assertTrue(result["stopped"])
        self.assertIn(["/usr/bin/xcrun", "simctl", "shutdown", "BBB"], calls)

    def test_only_while_memory_is_tight(self):
        own = {"simulator:BBB": {"id": "BBB"}}
        resource = {"kind": "simulator", "id": "simulator:BBB", "eligible": True, "reason": "simulator_not_attached"}
        with mock.patch.object(memory, "inspect_external_resources", return_value=([dict(resource)], own)):
            stack = [mock.patch.object(memory, "locked", mock.MagicMock()),
                     mock.patch.object(memory, "read_json", side_effect=lambda p, d=None: {} if p.name == "state.json" else d),
                     mock.patch.object(memory, "atomic_json"), mock.patch.object(memory, "log_event"),
                     mock.patch.object(memory, "sample_system", return_value=dict(sample(1), at=1000)),
                     mock.patch.object(memory, "protection_knowledge", return_value=[]),
                     mock.patch.object(memory, "process_table", return_value={}),
                     mock.patch.object(memory, "top_memory", return_value=[]),
                     mock.patch.object(memory, "inspect_resources", return_value=([], [])),
                     mock.patch.object(memory, "reconcile_external"),
                     mock.patch.object(memory.time, "time", return_value=1000)]
            for p in stack:
                p.start(); self.addCleanup(p.stop)
            with mock.patch.object(memory, "external_module") as module:
                result = memory.run_cycle("tick")
        module.return_value.reclaim.assert_not_called()
        self.assertEqual([(r["eligible"], r["reason"]) for r in result["external_resources"]], [(False, "simulator_not_attached")])


if __name__ == "__main__":
    unittest.main()
