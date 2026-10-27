"""Action-boundary tests with synthetic records and mocked child/socket objects."""

import contextlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from scripts import dev_memory as memory


def reading(pressure=2):
    return {
        "pressure": pressure, "boot_id": "synthetic-boot", "swap_used_bytes": 0,
        "swapins": 0, "swapouts": 0, "page_size": 16384, "compressed_bytes": 0,
    }


class RunCycleSafetyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="dev-memory-unit-")
        self.addCleanup(self.temp.cleanup)
        self.state_dir = Path(self.temp.name)
        self.record = {"id": "a" * 16, "pid": 424242, "start": "synthetic:1"}
        self.candidate = {**self.record, "eligible": True}
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(mock.patch.object(memory, "STATE", self.state_dir))
        self.stack.enter_context(mock.patch.object(memory, "locked", contextlib.nullcontext))
        self.stack.enter_context(mock.patch.object(memory.time, "time", return_value=1000))
        self.stack.enter_context(mock.patch.object(memory, "read_json", return_value={}))
        self.stack.enter_context(mock.patch.object(memory, "atomic_json"))
        self.stack.enter_context(mock.patch.object(memory, "log_event"))
        self.stack.enter_context(mock.patch.object(memory, "top_memory", return_value=[]))
        self.stack.enter_context(mock.patch.object(memory, "inspect_external_resources", return_value=([], {})))
        self.stack.enter_context(mock.patch.object(memory, "reconcile_external"))
        self.notify = self.stack.enter_context(mock.patch.object(memory, "notify", return_value=True))
        self.leases = self.stack.enter_context(mock.patch.object(memory, "protection_knowledge", return_value=[]))
        self.stack.enter_context(mock.patch.object(memory, "process_table", return_value={}))
        self.inspect = self.stack.enter_context(mock.patch.object(
            memory, "inspect_resources", return_value=([self.candidate], [self.record])))
        self.sample = self.stack.enter_context(mock.patch.object(memory, "sample_system", return_value=reading()))
        self.request = self.stack.enter_context(mock.patch.object(
            memory, "resource_request", return_value={"stopped": True}))
        self.kill = self.stack.enter_context(mock.patch.object(
            memory.os, "kill", side_effect=AssertionError("scanner must not signal discovered PIDs")))

    def test_pause_prevents_action_even_for_eligible_resource(self):
        (self.state_dir / "paused").touch()
        result = memory.run_cycle("clean")
        self.assertTrue(result["paused"])
        self.assertEqual(result["actions"], [])
        self.request.assert_not_called()
        self.kill.assert_not_called()

    def test_manual_cleanup_works_while_paused_without_resuming(self):
        (self.state_dir / 'paused').touch()
        result = memory.run_cycle('clean-once', [self.record['id']])
        self.request.assert_called_once_with(self.record, 'stop_if_orphan_once')
        self.assertTrue(result['actions'][0]['result']['stopped'])
        self.assertTrue((self.state_dir / 'paused').exists())

    def test_manual_cleanup_never_adds_unselected_or_new_resources(self):
        memory.run_cycle('clean-once', ['not-this-resource'])
        self.request.assert_not_called()

    def test_isolated_owned_only_never_calls_external_adapters(self):
        with mock.patch.object(memory, 'inspect_external_resources') as external, \
             mock.patch.object(memory, 'reconcile_external') as reconcile:
            result = memory.run_cycle('inspect', owned_only=True)
        external.assert_not_called()
        reconcile.assert_not_called()
        self.assertEqual(result['external_resources'], [])

    def test_manual_cleanup_keeps_active_unknown_and_cooldown(self):
        with mock.patch.object(memory, 'read_json', return_value={'last_cleanup': 999}):
            memory.run_cycle('clean-once', [self.record['id']])
        self.request.assert_not_called()
        self.inspect.return_value = ([dict(self.candidate, eligible=False)], [self.record])
        memory.run_cycle('clean-once', [self.record['id']])
        self.request.assert_not_called()
        with self.assertRaises(ValueError):
            memory.run_cycle('clean-once', [])

    def test_unknown_system_pressure_prevents_action(self):
        self.sample.return_value = reading(None)
        result = memory.run_cycle("clean")
        self.assertEqual(result["actions"], [])
        self.request.assert_not_called()

    def test_device_knowledge_read_failure_prevents_action(self):
        self.leases.side_effect = ValueError("synthetic corrupt lease")
        with self.assertRaises(ValueError):
            memory.run_cycle("clean")
        self.request.assert_not_called()

    def test_state_read_failure_prevents_action(self):
        with mock.patch.object(memory, "read_json", side_effect=ValueError("synthetic corrupt state")):
            with self.assertRaises(ValueError):
                memory.run_cycle("clean")
        self.request.assert_not_called()

    def test_status_never_performs_cleanup(self):
        result = memory.run_cycle("status")
        self.assertEqual(result["actions"], [])
        self.request.assert_not_called()

    def test_clean_requests_supervisor_protocol_and_never_signals_scanned_pid(self):
        result = memory.run_cycle("clean")
        self.request.assert_called_once_with(self.record, "stop_if_orphan")
        self.assertEqual(len(result["actions"]), 1)
        self.kill.assert_not_called()

    def test_external_write_requires_durable_intent_before_protocol_request(self):
        row = {'id': 'gradle:fixture', 'kind': 'gradle', 'pid': 424244, 'eligible': True, 'reason': 'idle'}
        raw = {'id': 'fixture', 'pid': 424244, 'start': 'birth:1'}
        stored = []
        api = SimpleNamespace(private_metadata=lambda value: value)
        def send(kind, value, state_dir):
            self.assertIn(row['id'], stored[-1]['external_pending'])
            return {'stopped': False, 'unknown': True}
        api.reclaim = mock.Mock(side_effect=send)
        import copy
        with mock.patch.object(memory, 'inspect_external_resources', return_value=([row], {row['id']: raw})), \
             mock.patch.object(memory, 'external_module', return_value=api), \
             mock.patch.object(memory, 'atomic_json', side_effect=lambda path, value: stored.append(copy.deepcopy(value))):
            result = memory.run_cycle('clean')
        api.reclaim.assert_called_once()
        self.assertIn(row['id'], result['pending_requests'])
        self.kill.assert_not_called()

    def test_failed_intent_write_never_starts_external_shutdown(self):
        row = {'id': 'gradle:fixture', 'kind': 'gradle', 'pid': 424244, 'eligible': True}
        api = SimpleNamespace(private_metadata=lambda value: value, reclaim=mock.Mock())
        with mock.patch.object(memory, 'inspect_external_resources', return_value=([row], {row['id']: {'id': 'fixture'}})), \
             mock.patch.object(memory, 'external_module', return_value=api), \
             mock.patch.object(memory, 'atomic_json', side_effect=OSError('synthetic write failure')):
            with self.assertRaises(OSError):
                memory.run_cycle('clean')
        api.reclaim.assert_not_called()

    def test_shared_three_action_limit_includes_all_resource_classes(self):
        rows = [{'id': 'gradle:' + str(n), 'kind': 'gradle', 'pid': 424244+n, 'eligible': True} for n in range(4)]
        records = {r['id']: {'id': r['id']} for r in rows}
        api = SimpleNamespace(private_metadata=lambda value: value, reclaim=mock.Mock(return_value={'stopped': True}))
        with mock.patch.object(memory, 'inspect_external_resources', return_value=(rows, records)), \
             mock.patch.object(memory, 'external_module', return_value=api):
            result = memory.run_cycle('clean')
        self.assertEqual(len(result['actions']), 3)
        self.assertEqual(api.reclaim.call_count, 3)
        self.request.assert_not_called()

    def test_pause_also_blocks_external_protocol_requests(self):
        (self.state_dir / 'paused').touch()
        row = {'id': 'mcp:fixture', 'kind': 'mcp', 'pid': 424245, 'eligible': True}
        api = SimpleNamespace(reclaim=mock.Mock())
        with mock.patch.object(memory, 'inspect_external_resources', return_value=([row], {row['id']: {}})), \
             mock.patch.object(memory, 'external_module', return_value=api):
            result = memory.run_cycle('clean')
        api.reclaim.assert_not_called()
        self.assertFalse(result['actions'])

    def test_normal_housekeeping_can_finish_its_shared_three_resource_batch(self):
        self.sample.return_value = reading(1)
        rows = [{'id': 'gradle:' + str(n), 'kind': 'gradle', 'pid': 424244+n, 'eligible': True} for n in range(4)]
        api = SimpleNamespace(private_metadata=lambda value: value, reclaim=mock.Mock(return_value={'stopped': True}))
        with mock.patch.object(memory, 'inspect_external_resources', return_value=(rows, {r['id']: r for r in rows})), \
             mock.patch.object(memory, 'external_module', return_value=api):
            result = memory.run_cycle('tick')
        self.assertEqual(len(result['actions']), 3)

    def test_normal_housekeeping_does_not_repeat_within_thirty_minutes(self):
        self.sample.return_value = reading(1)
        with mock.patch.object(memory, 'read_json', return_value={'external_last_sweep': 980}):
            result = memory.run_cycle('tick')
        self.assertFalse(result['actions'])
        self.request.assert_not_called()

    def test_normal_housekeeping_waits_for_observation_without_delaying_next_check(self):
        import copy
        self.sample.return_value = reading(1)
        self.inspect.return_value = ([dict(self.candidate, eligible=False)], [self.record])
        def persist(path, value):
            if path.name == 'state.json':
                memory.read_json.return_value = copy.deepcopy(value)
        with mock.patch.object(memory, 'atomic_json', side_effect=persist):
            first = memory.run_cycle('tick')
            self.assertFalse(first['actions'])
            self.assertIsNone(memory.read_json.return_value.get('external_last_sweep'))
            memory.time.time.return_value = 1060
            self.inspect.return_value = ([self.candidate], [self.record])
            second = memory.run_cycle('tick')
        self.request.assert_called_once_with(self.record, 'stop_if_orphan')
        self.assertEqual(len(second['actions']), 1)
        self.assertEqual(memory.read_json.return_value['external_last_sweep'], 1060)

    def test_unsafe_candidates_are_not_sent_to_supervisor(self):
        self.inspect.return_value = ([dict(self.candidate, eligible=False)], [self.record])
        memory.run_cycle("clean")
        self.request.assert_not_called()

    def test_pressure_becoming_unknown_stops_the_batch(self):
        second = dict(self.record, id="b" * 16, pid=424243)
        self.inspect.return_value = ([self.candidate, dict(second, eligible=True)], [self.record, second])
        self.sample.side_effect = [reading(2), reading(2), reading(None)]
        result = memory.run_cycle("clean")
        self.request.assert_called_once_with(self.record, "stop_if_orphan")
        self.assertEqual(len(result["actions"]), 1)

    def test_pressure_recovered_before_request_stops_without_action(self):
        self.sample.side_effect = [reading(2), reading(1), reading(1)]
        result = memory.run_cycle("clean")
        self.request.assert_not_called()
        self.assertFalse(result["actions"])

    def test_pressure_recovered_before_next_request_stops_the_batch(self):
        second = dict(self.record, id="b" * 16, pid=424243)
        self.inspect.return_value = ([self.candidate, dict(second, eligible=True)], [self.record, second])
        self.sample.side_effect = [reading(2), reading(2), reading(2), reading(1), reading(1)]
        result = memory.run_cycle("clean")
        self.request.assert_called_once_with(self.record, "stop_if_orphan")
        self.assertEqual(len(result["actions"]), 1)

    def test_tail_intent_is_durable_before_request(self):
        import copy
        stored = []
        def send(record, action):
            self.assertIn('external_pending', stored[-1])
            intent = stored[-1]['external_pending'][record['id']]
            self.assertEqual(intent['kind'], 'tail')
            self.assertEqual(intent['resource']['start'], record['start'])
            self.assertEqual(stored[-1]['last_cleanup'], 1000)
            return {'stopped': False, 'unknown': True}
        self.request.side_effect = send
        with mock.patch.object(memory, 'atomic_json', side_effect=lambda path, value: stored.append(copy.deepcopy(value))):
            result = memory.run_cycle('clean')
        self.assertIn(self.record['id'], result['pending_requests'])

    def test_tail_unknown_outcome_never_replays_after_cooldown(self):
        self.request.side_effect = OSError('synthetic lost response')
        first = memory.run_cycle('clean')
        memory.time.time.return_value = 1700
        second = memory.run_cycle('clean')
        self.request.assert_called_once_with(self.record, 'stop_if_orphan')
        self.assertIn(self.record['id'], first['pending_requests'])
        self.assertIn(self.record['id'], second['pending_requests'])
        self.assertFalse(second['actions'])

    def test_tail_accepted_timeout_never_replays_after_cooldown(self):
        self.request.return_value = {'stopped': False, 'accepted': True, 'reason': 'term_timeout_preserved_no_sigkill'}
        first = memory.run_cycle('clean')
        memory.time.time.return_value = 1700
        second = memory.run_cycle('clean')
        self.request.assert_called_once_with(self.record, 'stop_if_orphan')
        self.assertIn(self.record['id'], first['pending_requests'])
        self.assertIn(self.record['id'], second['pending_requests'])

    def test_tail_incomplete_json_reply_is_unknown_and_never_replayed(self):
        for reply in ({}, {'stopped': False}, {'stopped': False, 'reason': 'legacy_timeout'}):
            with self.subTest(reply=reply):
                memory.read_json.return_value = {}
                memory.time.time.return_value = 1000
                self.request.reset_mock()
                self.request.return_value = reply
                first = memory.run_cycle('clean')
                memory.time.time.return_value = 1700
                second = memory.run_cycle('clean')
                self.request.assert_called_once_with(self.record, 'stop_if_orphan')
                self.assertIn(self.record['id'], first['pending_requests'])
                self.assertIn(self.record['id'], second['pending_requests'])

    def test_tail_explicit_decline_allows_fresh_observation_after_cooldown(self):
        self.request.return_value = {'stopped': False, 'accepted': False, 'reason': 'active_unknown_or_protected'}
        first = memory.run_cycle('clean')
        memory.time.time.return_value = 1700
        second = memory.run_cycle('clean')
        self.assertEqual(self.request.call_count, 2)
        self.assertNotIn(self.record['id'], first['pending_requests'])
        self.assertNotIn(self.record['id'], second['pending_requests'])

    def test_recovery_after_action_does_not_send_stale_pressure_notification(self):
        state = {'last_ts': 940, 'boot_id': 'synthetic-boot', 'warn_count': 2,
                 'incident_started': 500, 'notification_sent': False}
        self.sample.side_effect = [reading(2), reading(2), reading(1)]
        with mock.patch.object(memory, 'read_json', return_value=state):
            result = memory.run_cycle('tick')
        self.assertEqual(len(result['actions']), 1)
        self.assertEqual(result['actions'][0]['before']['pressure'], 2)
        self.assertEqual(result['actions'][0]['after']['pressure'], 1)
        self.notify.assert_not_called()

    def test_recovery_or_unknown_does_not_notify_from_stale_incident(self):
        self.inspect.return_value = ([], [])
        for pressure in (1, None):
            with self.subTest(pressure=pressure):
                state = {
                    "incident_started": 500, "last_ts": 940, "boot_id": "synthetic-boot",
                    "last_sweep": 1000, "notification_sent": False,
                }
                self.sample.return_value = reading(pressure)
                with mock.patch.object(memory, "read_json", return_value=state):
                    memory.run_cycle("tick")
                self.notify.assert_not_called()

    def test_first_critical_confirmation_uses_old_sample_timestamp_for_swap_rate(self):
        self.inspect.return_value = ([], [])
        state = {
            "last_ts": 940, "boot_id": "synthetic-boot",
            "sample": dict(reading(), at=900, swapins=1280, swapouts=2560),
        }
        self.sample.side_effect = [
            dict(reading(4), at=1000, swapins=7680, swapouts=15360),
            dict(reading(4), at=1015, swapins=8640, swapouts=17280),
        ]
        with mock.patch.object(memory, "read_json", return_value=state), \
                mock.patch.object(memory.time, "time", side_effect=[1000, 1015]), \
                mock.patch.object(memory.time, "sleep") as sleep:
            result = memory.run_cycle("tick")
        sleep.assert_called_once_with(15)
        self.assertEqual(result["sample"]["swapins_mib_s"], 1.0)
        self.assertEqual(result["sample"]["swapouts_mib_s"], 2.0)


class SupervisorSafetyTests(unittest.TestCase):
    def run_supervisor(self, *, owner="dead", paused=False, current_start="synthetic:1", leaf=True, term_timeout=False):
        with tempfile.TemporaryDirectory(prefix="dev-memory-supervisor-unit-") as temp:
            state_dir = Path(temp)
            if paused:
                (state_dir / "paused").touch()
            owner_identity = {"pid": 575757, "uid": os.getuid(), "start": "owner:1"}
            born = {"pid": 424242, "uid": os.getuid(), "start": "synthetic:1"}
            current = None if current_start is None else dict(born, start=current_start)
            supervisor_identity = {"pid": os.getpid(), "uid": os.getuid(), "start": "supervisor:1"}
            child = mock.Mock(pid=born["pid"])
            child.poll.side_effect = [None, 0]
            child.wait.return_value = 0
            if term_timeout:
                child.wait.side_effect = [subprocess.TimeoutExpired("tail", 8), 0]
            conn = mock.MagicMock()
            conn.__enter__.return_value = conn
            conn.makefile.return_value.readline.return_value = json.dumps({
                "action": "stop_if_orphan", "id": "c" * 16, "start": born["start"],
            }).encode() + b"\n"
            server = mock.MagicMock()
            server.__enter__.return_value = server
            server.accept.return_value = (conn, None)
            proc = memory.Process(born["pid"], os.getpid(), os.getuid(), born["start"], "tail", "tail -f /synthetic/log")
            table = {proc.pid: proc}
            if not leaf:
                table[424243] = memory.Process(424243, proc.pid, os.getuid(), "descendant:1", "cat", "cat")
            with contextlib.ExitStack() as stack:
                stack.enter_context(mock.patch.object(memory, "STATE", state_dir))
                stack.enter_context(mock.patch.object(memory, "setup"))
                stack.enter_context(mock.patch.object(memory, "locked", contextlib.nullcontext))
                stack.enter_context(mock.patch.object(memory, "atomic_json"))
                stack.enter_context(mock.patch.object(memory, "log_event"))
                stack.enter_context(mock.patch.object(memory.os, "chmod"))
                stack.enter_context(mock.patch.object(memory.signal, "signal"))
                stack.enter_context(mock.patch.object(memory.uuid, "uuid4", return_value=SimpleNamespace(hex="c" * 32)))
                popen = stack.enter_context(mock.patch.object(memory.subprocess, "Popen", return_value=child))
                stack.enter_context(mock.patch.object(memory.socket, "socket", return_value=server))
                stack.enter_context(mock.patch.object(memory, "identity", side_effect=[owner_identity, born, supervisor_identity, current]))
                stack.enter_context(mock.patch.object(memory, "owner_state", return_value=owner))
                stack.enter_context(mock.patch.object(memory, "process_table", return_value=table))
                kill = stack.enter_context(mock.patch.object(
                    memory.os, "kill", side_effect=AssertionError("supervisor may only terminate its retained Popen child")))
                result = memory.supervise(SimpleNamespace(
                    command=["tail", "-f", "/synthetic/log"], kind="tail", owner_pid=owner_identity["pid"]))
                popen.assert_called_once_with(["/usr/bin/tail", "-f", "/synthetic/log"])
                kill.assert_not_called()
            payload = json.loads(conn.sendall.call_args.args[0])
            return child, payload, result

    def test_only_own_retained_popen_child_is_terminated_after_owner_exits(self):
        child, payload, result = self.run_supervisor()
        child.terminate.assert_called_once_with()
        child.kill.assert_not_called()
        self.assertTrue(payload["stopped"])
        self.assertEqual(result, 0)

    def test_live_or_unknown_owner_prevents_child_termination(self):
        for owner in ("alive", "unknown"):
            with self.subTest(owner=owner):
                child, payload, _ = self.run_supervisor(owner=owner)
                child.terminate.assert_not_called()
                self.assertFalse(payload["stopped"])

    def test_pause_at_supervisor_blocks_a_previously_authorized_request(self):
        child, payload, _ = self.run_supervisor(paused=True)
        child.terminate.assert_not_called()
        self.assertFalse(payload["stopped"])

    def test_changed_or_unknown_child_identity_prevents_termination(self):
        for start in ("reused:2", None):
            with self.subTest(start=start):
                child, payload, _ = self.run_supervisor(current_start=start)
                child.terminate.assert_not_called()
                self.assertFalse(payload["stopped"])

    def test_child_with_descendants_is_preserved(self):
        child, payload, _ = self.run_supervisor(leaf=False)
        child.terminate.assert_not_called()
        self.assertFalse(payload["stopped"])

    def test_termination_timeout_never_escalates_to_sigkill(self):
        child, payload, _ = self.run_supervisor(term_timeout=True)
        child.terminate.assert_called_once_with()
        child.kill.assert_not_called()
        self.assertFalse(payload["stopped"])
        self.assertTrue(payload.get("accepted"))
        self.assertEqual(payload["reason"], "term_timeout_preserved_no_sigkill")


class TailPendingPolicyTests(unittest.TestCase):
    def test_pending_tail_is_reported_without_another_supervisor_request(self):
        with tempfile.TemporaryDirectory(prefix='dev-memory-pending-unit-') as temp:
            state_dir = Path(temp)
            (state_dir / 'resources').mkdir()
            rec = {'id': 'd' * 16, 'pid': 424246, 'start': 'child:1', 'kind': 'tail',
                   'supervisor_pid': 424247, 'supervisor_start': 'supervisor:1'}
            (state_dir / 'resources' / (rec['id'] + '.json')).write_text(json.dumps(rec))
            state = {'external_pending': {rec['id']: {'kind': 'tail', 'resource': rec}}}
            with mock.patch.object(memory, 'STATE', state_dir), \
                    mock.patch.object(memory, 'identity', return_value=None), \
                    mock.patch.object(memory, 'resource_request') as request:
                items, _ = memory.inspect_resources({}, state, 1000, True)
            request.assert_not_called()
            self.assertFalse(items[0]['eligible'])
            self.assertEqual(items[0]['reason'], 'pending_readonly_no_retry')

    def test_pending_tail_clears_only_after_original_identity_is_confirmed_dead(self):
        rec = {'pid': 424246, 'start': 'child:1'}
        for status in ('alive', 'unknown', 'dead'):
            with self.subTest(status=status):
                state = {'external_pending': {'d' * 16: {'kind': 'tail', 'resource': rec}}}
                with mock.patch.object(memory, 'owner_state', return_value=status) as inspect, \
                        mock.patch.object(memory, 'log_event'), \
                        mock.patch.object(memory, 'external_module') as external:
                    memory.reconcile_external(state)
                inspect.assert_called_once_with(rec)
                external.assert_not_called()
                self.assertEqual(bool(state['external_pending']), status != 'dead')


if __name__ == "__main__":
    unittest.main()
