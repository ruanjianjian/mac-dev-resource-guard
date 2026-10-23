import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('dashboard_backend', ROOT / 'widget/backend.py')
backend = importlib.util.module_from_spec(spec)
spec.loader.exec_module(backend)


def memory(**overrides):
    result = dict(paused=False, managed_resources=[], external_resources=[], actions=[], pending_requests=[], top_memory=[])
    result.update(overrides)
    return result


def plan():
    return dict(schema_version=1, candidates=[
        dict(path='/cache/Build/Intermediates.noindex', decision='eligible', reasons=[], stats=dict(allocated_bytes=4096)),
        dict(path='/cache/Index.noindex', decision='keep', reasons=['explicitly pinned for acceptance/use'], stats=dict(allocated_bytes=2048)),
        dict(path='/cache/Products', decision='eligible', reasons=[], stats=dict(allocated_bytes=100000))])


class DashboardTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='dashboard-test-')
        self.addCleanup(temp.cleanup)
        self.home = Path(temp.name)
        self.adapter = backend.Adapter(self.home)

    def storage_preview(self):
        def command(name, args):
            self.assertEqual(name, 'dev-storage')
            self.assertEqual(args[:2], ['plan', '--output'])
            backend.atomic_json(Path(args[2]), plan())
            return [dict(phase='planning'), dict(phase='plan_complete')]
        with patch.object(self.adapter, 'command', side_effect=command):
            return self.adapter.run('storage-preview')

    def test_classifies_cache_and_keeps_unknown_and_pin(self):
        value = self.storage_preview()
        self.assertEqual(value['eligible'], 1)
        self.assertEqual(value['eligibleBytes'], 4096)
        self.assertFalse(value['rows'][2]['eligible'])
        self.assertIn('验收', value['rows'][1]['reason'])

    def test_busy_scan_never_reuses_old_plan(self):
        self.storage_preview()
        with patch.object(self.adapter, 'command', return_value=[dict(status='busy')]):
            value = self.adapter.run('storage-preview')
        self.assertEqual(value['status'], 'busy')
        self.assertNotIn('token', value)

    def test_incomplete_scan_never_reuses_old_plan(self):
        self.storage_preview()
        with patch.object(self.adapter, 'command', return_value=[dict(phase='planning')]):
            with self.assertRaises(ValueError): self.adapter.run('storage-preview')

    def test_clean_consumes_preview_and_uses_existing_backend(self):
        preview = self.storage_preview()
        output = dict(status='ok', deleted=[{}], kept=[{}], errors=[], observed_free_delta_bytes=1024**3)
        with patch.object(self.adapter, 'command', return_value=[output]) as run:
            value = self.adapter.run('storage-clean', preview['token'])
            run.assert_called_once_with('dev-storage', ['maintain', '--apply', '--scan'])
            self.assertEqual(value['status'], 'ok')
            self.assertIn('+1.00 GiB', value['message'])
            with self.assertRaises(ValueError): self.adapter.run('storage-clean', preview['token'])
            self.assertEqual(run.call_count, 1)

    def test_expired_or_wrong_token_does_not_call_backend(self):
        preview = self.storage_preview()
        with patch.object(self.adapter, 'command') as run:
            with self.assertRaises(ValueError): self.adapter.run('storage-clean', '../wrong')
            with patch.object(backend.time, 'time', return_value=preview['created'] + 301):
                with self.assertRaises(ValueError): self.adapter.run('storage-clean', preview['token'])
            run.assert_not_called()

    def test_unknown_action_outcome_blocks_replay_even_after_new_preview(self):
        preview = self.storage_preview()
        with patch.object(self.adapter, 'command', side_effect=ValueError('lost connection')) as run:
            value = self.adapter.run('storage-clean', preview['token'])
            self.assertEqual(value['status'], 'unknown')
            next_preview = self.adapter.run('storage-preview')
            self.assertEqual(next_preview['status'], 'unknown')
            self.assertNotIn('token', next_preview)
            self.assertEqual(run.call_count, 1)

    def test_paused_memory_disables_reclaim_without_resuming(self):
        body = memory(paused=True, external_resources=[dict(id='gradle:1', kind='gradle', eligible=True)])
        with patch.object(self.adapter, 'command', return_value=[body]) as run:
            value = self.adapter.run('memory-preview')
            self.assertEqual(value['eligible'], 0)
            with self.assertRaises(ValueError): self.adapter.run('memory-clean', value['token'])
            run.assert_called_once_with('dev-memory', ['inspect', '--json'])

    def test_memory_preserves_apps_devices_and_pending(self):
        rows = backend.rows_memory(memory(external_resources=[dict(id='gradle:1', kind='gradle', eligible=False, reason='pending_readonly_no_retry')],
            top_memory=[dict(pid=123, name='Xcode', mem='1G', argv='SECRET', policy='保留')],
            protected_devices=2, pending_requests=[dict(secret='SECRET')]))
        self.assertTrue(all(not r['eligible'] for r in rows))
        self.assertNotIn('SECRET', json.dumps(rows))

    def test_accepted_is_not_reported_as_stopped(self):
        body = memory(external_resources=[dict(id='mcp:1', kind='mcp', eligible=True)])
        with patch.object(self.adapter, 'command', return_value=[body]):
            value = self.adapter.run('memory-preview')
        body.update(actions=[dict(result=dict(accepted=True, stopped=False))], pending_requests=[{}])
        with patch.object(self.adapter, 'command', return_value=[body]) as run:
            result = self.adapter.run('memory-clean', value['token'])
        run.assert_called_once_with('dev-memory', ['clean', '--json'])
        self.assertIn('正常退出 0 项，待核实 1 项', result['message'])

    def test_schema_failures_keep_cleaning_disabled(self):
        for body in ({}, {'paused':False, 'actions':[]}):
            with self.assertRaises(ValueError): backend.rows_memory(body)
        with self.assertRaises(ValueError): backend.rows_storage({'candidates':[]})
        with self.assertRaises(ValueError): backend.json_stream('{"status":"ok"}garbage')

    def test_missing_command_has_no_path_fallback(self):
        with self.assertRaisesRegex(ValueError, '未安装'):
            self.adapter.run('memory-preview')

    def test_multiple_window_operation_lock(self):
        import fcntl
        with open(self.adapter.state/'operation.lock', 'a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with patch.object(self.adapter, 'command') as run:
                self.assertEqual(self.adapter.run('storage-preview')['status'], 'busy')
                run.assert_not_called()

    def test_subprocess_cli_end_to_end_without_shell(self):
        tool = self.home/'.local/bin/dev-memory'
        tool.parent.mkdir(parents=True)
        tool.write_text('#!'+sys.executable+'\nimport json\nprint('+repr(json.dumps(memory()))+')\n')
        tool.chmod(0o700)
        result = subprocess.run([sys.executable, str(ROOT/'widget/backend.py'), 'memory-preview', '--home', str(self.home)],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['eligible'], 0)
        self.assertEqual((self.adapter.state/'memory-preview.json').stat().st_mode & 0o777, 0o600)


if __name__ == '__main__': unittest.main()
