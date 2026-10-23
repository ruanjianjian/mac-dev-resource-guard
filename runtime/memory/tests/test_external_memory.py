"""Protocol aggregation safety, without any real devices or daemons."""
import unittest
from unittest.mock import patch
from scripts import memory_external as ext


class ExternalPolicy(unittest.TestCase):
    def setUp(self):
        self.row = {'id': 'test-device', 'device_id': 'test-device', 'lease_signature': 'lease-v1',
                    'fingerprint': 'lease-v1', 'eligible': True, 'reason': 'owner_exited', 'evidence': {'owner_dead': True}}

    def collect(self, state, now, rows=None):
        with patch.object(ext, 'device_inspect', return_value=rows if rows is not None else [self.row]), \
             patch.object(ext, 'adapter', side_effect=ImportError):
            return ext.collect(state, now)

    def test_two_consistent_idle_observations_required(self):
        state = {}
        a, _ = self.collect(state, 1000)
        self.assertFalse(a[0]['eligible'])
        b, _ = self.collect(state, 1030)
        self.assertTrue(b[0]['eligible'])

    def test_changed_lease_or_sleep_gap_resets_idle_evidence(self):
        for changed, at in [(True, 1060), (False, 1300)]:
            with self.subTest(changed=changed, at=at):
                state = {}
                self.collect(state, 1000)
                row = dict(self.row, fingerprint='lease-v2') if changed else self.row
                b, _ = self.collect(state, at, [row])
                self.assertFalse(b[0]['eligible'])

    def test_active_or_unknown_never_becomes_eligible(self):
        for value in [False, None, 'true', 1]:
            state = {}
            for at in [1000, 1060]:
                rows, _ = self.collect(state, at, [dict(self.row, eligible=value)])
                self.assertFalse(rows[0]['eligible'])

    def test_unknown_write_outcome_blocks_repeat(self):
        state = {'external_pending': {'simulator:test-device': {'status': 'unknown'}}}
        for at in [1000, 1060, 1120]:
            rows, _ = self.collect(state, at)
            self.assertFalse(rows[0]['eligible'])
            self.assertEqual(rows[0]['reason'], 'pending_readonly_no_retry')

    def test_one_failed_adapter_does_not_disable_independent_safety_checks(self):
        state = {}
        self.collect(state, 1000)
        rows, _ = self.collect(state, 1060)
        self.assertTrue(rows[0]['eligible'])
        self.assertEqual(rows[1]['reason'], 'inspection_unavailable_preserved')

    def test_no_credential_fields_are_persisted(self):
        row = dict(self.row, evidence={'owner_dead': True, 'auth_token': 'SYNTHETIC', 'nested': {'password': 'SYNTHETIC'}})
        rows, _ = self.collect({}, 1000, [row])
        self.assertNotIn('auth_token', rows[0]['evidence'])
        self.assertNotIn('password', rows[0]['evidence']['nested'])
