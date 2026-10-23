"""Recovery contracts: stable boot identity, safe no-dispatch retry, honest alerts."""
import unittest
from unittest import mock
from types import SimpleNamespace
from scripts import dev_memory as memory

BOOT = 'DD229E57-E075-47EF-A10B-05BF03761C7F'

class BootIdentityTests(unittest.TestCase):
    def sample(self, micros):
        raw = f'''hw.memsize: 51539607552
vm.swapusage: total = 100M used = 20M free = 80M
kern.memorystatus_vm_pressure_level: 2
kern.boottime: {{ sec = 1791537163, usec = {micros} }}
kern.bootsessionuuid: {BOOT}
'''
        vm = 'Mach Virtual Memory Statistics: (page size of 16384 bytes)\nPages occupied by compressor: 10.\nPages wired down: 20.\nSwapins: 30.\nSwapouts: 40.\n'
        with mock.patch.object(memory, 'command', side_effect=[raw, vm]):
            return memory.sample_system()

    def test_clock_correction_does_not_reset_pressure_or_notify_again(self):
        first, second = self.sample(251921), self.sample(282176)
        self.assertEqual(first['boot_id'], second['boot_id'])
        state = {'last_ts': 1000, 'boot_id': first['boot_id'], 'warn_count': 4,
                 'incident_started': 500, 'notification_sent': True}
        state, event = memory.pressure_transition(state, second, 1060)
        self.assertTrue(state['notification_sent'])
        self.assertEqual(state['incident_started'], 500)
        self.assertEqual(state['warn_count'], 5)

    def test_boot_identity_is_session_uuid(self):
        self.assertEqual(self.sample(282176)['boot_id'], BOOT.lower())

class GradleRecoveryTests(unittest.TestCase):
    def state(self):
        return {'external_pending': {'gradle:test': {'kind': 'gradle',
            'resource': {'pid': 424242, 'start': 'birth:1'}}},
            'external_observations': {'gradle:test': {'at': 1000}}}

    def check(self, outcome):
        state = self.state()
        module = SimpleNamespace(reconcile=mock.Mock(return_value=outcome))
        api = SimpleNamespace(adapter=mock.Mock(return_value=module))
        with mock.patch.object(memory, 'owner_state', return_value='alive'), \
             mock.patch.object(memory, 'external_module', return_value=api), \
             mock.patch.object(memory, 'log_event') as log:
            memory.reconcile_external(state)
        module.reconcile.assert_called_once()
        return state, log

    def test_proven_unsent_attempt_clears_pending_and_requires_fresh_observation(self):
        state, log = self.check({'stopped': False, 'safe_to_retry': True, 'reason': 'worker_exited_before_dispatch'})
        self.assertFalse(state['external_pending'])
        self.assertNotIn('gradle:test', state['external_observations'])
        self.assertEqual(log.call_args.args[0]['type'], 'cleanup_not_dispatched')

    def test_unknown_or_legacy_attempt_is_not_replayed(self):
        for result in ({'stopped': False, 'safe_to_retry': False}, {}, {'safe_to_retry': 'true'}):
            with self.subTest(result=result):
                state, _ = self.check(result)
                self.assertIn('gradle:test', state['external_pending'])

    def test_reconciled_exit_is_counted_as_exit_not_a_new_stop(self):
        state, log = self.check({'stopped': True, 'safe_to_retry': False})
        self.assertFalse(state['external_pending'])
        self.assertEqual(log.call_args.args[0]['type'], 'cleanup_reconciled')

class NotificationTruthTests(unittest.TestCase):
    def test_no_safe_candidate_alert_does_not_imply_cleanup_is_fixing_pressure(self):
        message = memory.notification_message([], [], {})
        self.assertIn('没有可安全回收', message)

    def test_unknown_stop_is_called_pending_not_success(self):
        actions = [{'result': {'stopped': False, 'unknown': True}}]
        message = memory.notification_message([], actions, {'x': {'kind': 'gradle'}})
        self.assertIn('待核实', message)
        self.assertNotIn('已正常退出', message)

    def test_confirmed_exit_is_reported(self):
        message = memory.notification_message([], [{'result': {'stopped': True}}], {})
        self.assertIn('已正常退出 1 项', message)

if __name__ == '__main__':
    unittest.main()
