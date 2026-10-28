import fcntl
import json
from pathlib import Path
import sys
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'widget'))
from test_ask_tier import AskFixture, FakeSimctl
import ask_tier
import auto_tier
import backend
from storage import Activity


class TierActionTests(AskFixture):
    def setUp(self):
        super().setUp()
        (self.home / '.resource-dashboard-fixture').touch()
        self.simctl = self.sims()
        self.refresh(self.simctl, task_roots=[])
        self.adapter = backend.Adapter(self.home)
        patch.object(backend, 'fixture_simctl', return_value=self.simctl).start(); self.addCleanup(patch.stopall)
        patch.object(ask_tier.Activity, 'collect', return_value=Activity()).start()

    def test_preview_lists_what_is_lost_and_runs_only_ticked_ids(self):
        preview = self.adapter.run('pending-preview')
        rows = {r['id']: r for r in preview['rows']}
        self.assertEqual(set(rows), {'simulator-delete:OLD', 'runtime-delete:R1'})
        self.assertTrue(all(r['eligible'] and r['loses'] and r['outcome'] == '待确认' for r in rows.values()))
        result = self.adapter.run('pending-clean', preview['token'], ids='runtime-delete:R1')
        self.assertEqual([(r['id'], r['outcome']) for r in result['rows']], [('runtime-delete:R1', '已完成')])
        self.assertEqual([c for c in self.simctl.calls if c[0] in ('delete', 'runtime') and c[1] != 'list'], [['runtime', 'delete', 'R1']])

    def test_stale_token_or_unlisted_id_runs_nothing(self):
        preview = self.adapter.run('pending-preview')
        self.assertEqual(self.adapter.run('pending-clean', preview['token'], ids='task-delete:/elsewhere')['status'], 'rejected')
        self.assertEqual(self.adapter.run('pending-clean', preview['token'], ids='runtime-delete:R1')['status'], 'rejected')  # used up
        old = self.adapter.run('pending-preview')
        receipt = self.adapter.state / 'pending-preview.json'
        body = json.loads(receipt.read_text()); body['created'] -= 400; receipt.write_text(json.dumps(body))
        self.assertEqual(self.adapter.run('pending-clean', old['token'], ids='runtime-delete:R1')['status'], 'rejected')
        self.assertFalse(any(c[:2] == ['runtime', 'delete'] for c in self.simctl.calls))

    def test_busy_background_cleanup_blocks_confirmation(self):
        preview = self.adapter.run('pending-preview')
        lock = self.home / '.local/state/dev-resource-guard/periodic-storage.lock'
        with lock.open('a') as held:
            fcntl.flock(held, fcntl.LOCK_EX)
            self.assertEqual(self.adapter.run('pending-clean', preview['token'], ids='runtime-delete:R1')['status'], 'busy')
        self.assertFalse(any(c[:2] == ['runtime', 'delete'] for c in self.simctl.calls))

    def test_ignore_and_switches(self):
        rows = {r['id']: r for r in self.adapter.run('pending-ignore', ids='simulator-delete:OLD')['rows']}
        self.assertEqual(rows['simulator-delete:OLD']['outcome'], '已忽略')
        setup = {r['id']: r for r in self.adapter.run('setup-auto-enable')['rows']}
        self.assertEqual(setup['auto-tier']['outcome'], '已打开'); self.assertIn('询问档已打开', setup['devices']['reason'])
        self.assertIn('1 项待确认', setup['ask-tier']['reason'])
        setup = {r['id']: r for r in self.adapter.run('setup-ask-disable')['rows']}
        self.assertEqual(setup['ask-tier']['outcome'], '未打开'); self.assertNotIn('询问档已打开', setup['devices']['reason'])

    def test_auto_records_show_actions_kept_and_missing_drive(self):
        auto_tier.write_receipt(self.home, dict(schema=1, at=time.time(), status='complete', free_before=3.0, free_after=40.0,
                                                external_missing=['/Volumes/T9/work'], config_error=None,
                                                actions=[dict(kind='move', path='/w/old', dest='/Volumes/T9/work/old', bytes=5, result='done', reason='闲置 6 天')],
                                                kept=[dict(path='/w/busy', reason='open file/cwd held by PID 7')]))
        rows = {r['id'].split(':')[0]: r for r in self.adapter.run('auto-preview')['rows']}
        self.assertEqual(rows['auto-status']['outcome'], '已完成')
        self.assertIn('/Volumes/T9/work', rows['external']['reason'])
        self.assertEqual((rows['auto']['title'], rows['auto']['outcome']), ('搬到外接盘：old', '已完成'))
        self.assertEqual(rows['kept']['outcome'], '保留')

    def test_denied_drive_access_is_shown(self):
        auto_tier.write_receipt(self.home, dict(schema=1, at=time.time(), status='complete', free_before=3.0, free_after=3.0, config_error=None,
                                                external_missing=[], external_denied=[dict(path='/Volumes/T9/w', reason='后台任务没有访问外接盘的权限')],
                                                actions=[], kept=[]))
        rows = {r['id'].split(':')[0]: r for r in self.adapter.run('auto-preview')['rows']}
        self.assertEqual(rows['denied']['outcome'], '需要授权'); self.assertIn('改为删构建产物', rows['denied']['reason'])

    def test_wrong_settings_are_shown_not_hidden(self):
        (self.home / '.config/dev-resource-guard/auto-cleanup.json').write_text('{"auto_idle_days": -1}')
        setup = {r['id']: r for r in self.adapter.run('setup-preview')['rows']}
        self.assertEqual(setup['tier-config']['outcome'], '设置有误'); self.assertIn('auto_idle_days', setup['tier-config']['reason'])


if __name__ == '__main__':
    unittest.main()
