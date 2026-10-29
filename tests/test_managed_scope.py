"""A narrow automatic scope must never enter task/device/tool-wide mutation paths."""
import json
import sys
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).parent))
from test_tiers import TierFixture, auto_tier, tiers_config, GIB
import plan
import service


class ManagedScopeTests(TierFixture):
    def test_new_and_legacy_settings_use_safe_scope(self):
        for changes in ({}, {'auto_scope': 'standard'}, {'auto_scope': 'managed-caches'}):
            with self.subTest(changes=changes):
                cfg = self.cfg(**changes)
                self.assertEqual(cfg['auto_scope'], 'managed-caches')

    def test_default_auto_never_deletes_unavailable_device_or_tool_cache(self):
        calls = []
        def simctl(args):
            calls.append(args)
            return json.dumps({'devices': {'old': [{'udid': 'U1', 'isAvailable': False}]}})
        result = self.run_tier(self.cfg(), free=lambda: GIB, simctl=simctl)
        self.assertEqual(result['actions'], [])
        self.assertEqual(calls, [])

    def test_idle_pushed_task_moves_whole_and_nothing_is_ever_deleted(self):
        task = self.task('idle', 20)
        result = self.run_tier(self.cfg(), free=lambda: GIB)
        self.assertEqual([(a['kind'], a['result']) for a in result['actions']], [('move', 'done')])
        self.assertTrue(task.is_symlink())
        self.assertTrue((task / 'app/build/out.o').is_file(), 'outputs travel with the task, never pruned')
        self.assertTrue((task / 'app/node_modules/pkg/i.js').is_file())

    def test_without_the_drive_switch_tasks_stay_and_nothing_is_pruned(self):
        task = self.task('idle', 20)
        result = self.run_tier(self.cfg(task_roots=[{'path': str(self.root)}]), free=lambda: GIB)
        self.assertEqual(result['actions'], [])
        self.assertFalse(task.is_symlink())
        self.assertTrue((task / 'app/build/out.o').is_file())

    def test_unsaved_work_never_leaves_the_internal_disk(self):
        dirty, unpushed = self.task('dirty', 20, dirty=True), self.task('unpushed', 20, unpushed=True)
        result = self.run_tier(self.cfg(), free=lambda: GIB)
        self.assertEqual(result['actions'], [])
        self.assertFalse(dirty.is_symlink() or unpushed.is_symlink())
        self.assertEqual(sorted(k['reason'].split('，')[0] for k in result['kept']), sorted(['app：有未提交改动', 'app：有未推送的提交']))

    def test_an_interrupted_move_is_finished_or_undone_first(self):
        task = self.task('old', 20); self.external.mkdir(parents=True)
        tmp = self.external / '.guard-moving-old'; tmp.mkdir()
        marker = auto_tier.state_root(self.home) / auto_tier.MOVE_STATE; marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(json.dumps(dict(src=str(task), dest=str(self.external / 'old'), tmp=str(tmp), phase='copying')))
        result = self.run_tier(self.cfg(), free=lambda: 50 * GIB)
        self.assertIn('原目录保留', result['recovered'])
        self.assertFalse(marker.exists() or tmp.exists())
        self.assertTrue((task / 'app/main.swift').is_file())

    def test_unavailable_simulator_is_asked_never_auto_deleted(self):
        import ask_tier
        from storage import Activity
        calls = []
        def simctl(args):
            calls.append(args)
            return json.dumps({'devices': {'com.apple.CoreSimulator.SimRuntime.iOS-17-0': [
                {'udid': 'U1', 'name': 'Old Phone', 'isAvailable': False, 'state': 'Shutdown'}]}}) if args[0] == 'list' else '{}'
        self.run_tier(self.cfg(), free=lambda: GIB, simctl=simctl)
        self.assertFalse(any(c[0] == 'delete' for c in calls))
        pending = ask_tier.refresh(self.home, self.cfg(task_roots=[]), self.settings, simctl, Activity())
        self.assertEqual([(i['id'], i['reason']) for i in pending['items']], [('simulator-delete:U1', '系统标为不可用，开不了机')])

    def test_plan_does_not_offer_automatic_task_deletion(self):
        cfg = self.cfg(auto_scope='managed-caches')
        report = dict(tasks=[dict(path=str(self.root/'idle'), idle=True, bytes=GIB)], categories=[], caches=[])
        with patch.object(plan.T, 'rebuildable', side_effect=AssertionError('must not inspect task outputs')):
            result = plan.build(self.home, cfg, self.settings, report, {'items': []}, GIB, False)
            moving = plan.build(self.home, cfg, self.settings, report, {'items': []}, GIB, True)
        self.assertEqual(result['sections'][0]['items'], [])
        self.assertEqual([i['kind'] for i in moving['sections'][0]['items']], ['move'], 'a move, never a deletion')

    def test_invalid_scope_fails_closed(self):
        with self.assertRaisesRegex(tiers_config.ConfigError, 'auto_scope'):
            self.cfg(auto_scope='anything')

    def test_scheduled_cache_maintenance_preserves_seven_days_in_both_families(self):
        self.cfg(auto_scope='managed-caches')
        seen = []
        class Manager:
            def __init__(self, settings): self.settings = settings
            def plan(self, activity):
                seen.append((self.settings.normal_days, self.settings.pressure_days, self.settings.critical_days))
                return {'eligible_accounting_bytes': 0}
            def apply(self, *a, **k): return {'status': 'complete', 'errors': [], 'deleted': []}
        with patch.object(service, 'StorageManager', Manager), \
             patch.object(service.common, 'manager', return_value=Manager(self.settings)), \
             patch.object(service.common, 'activity', return_value=None):
            result = service.clean_caches(self.home)
        self.assertEqual(result['status'], 'complete')
        self.assertEqual(seen, [(7,7,7), (7,7,7)])

    def test_upgrade_preserves_switches_paths_and_target(self):
        self.cfg(auto_scope='standard', auto_enabled=False, caches_enabled=True)
        target = self.home/'.config/dev-resource-guard/config.json'
        target.write_text('{"target_free_gib": 600, "warning_free_gib": 550, "critical_free_gib": 500}')
        before = target.read_bytes()
        cfg = tiers_config.migrate_scope(self.home)
        self.assertEqual(cfg['auto_scope'], 'managed-caches')
        self.assertFalse(cfg['auto_enabled'])
        self.assertTrue(cfg['caches_enabled'])
        self.assertEqual(cfg['task_roots'][0]['path'], self.root)
        self.assertEqual(target.read_bytes(), before)
        self.assertEqual(tiers_config.read_raw(self.home)['auto_scope'], 'managed-caches')

    def test_pause_blocks_real_expired_cache_then_enable_resumes(self):
        cfg = self.cfg(auto_enabled=False)
        cache = self.home/'Library/Developer/Xcode/DerivedData/Old/Build/Intermediates.noindex'
        cache.mkdir(parents=True);(cache/'file.o').write_bytes(b'x'*4096)
        from test_tiers import age
        from storage import Activity
        age(cache, 20)
        with patch.object(service.common, 'activity', return_value=Activity()):
            paused = service.tick_storage(self.home)
            self.assertEqual(paused['status'], 'disabled')
            self.assertTrue(cache.exists())
            tiers_config.set_flag(self.home, 'auto_enabled', True)
            resumed = service.tick_storage(self.home)
        self.assertEqual(resumed['status'], 'complete')
        self.assertFalse(cache.exists())

    def test_disable_during_scan_prevents_delete(self):
        from test_tiers import age
        from storage import Activity
        self.cfg()
        cache=self.home/'Library/Developer/Xcode/DerivedData/Old/Build/Intermediates.noindex'
        cache.mkdir(parents=True);(cache/'object.o').write_bytes(b'x'*4096);age(cache,20)
        original=service.common.manager
        def manager(home):
            value=original(home);old_plan=value.plan
            def scan(**kwargs):
                result=old_plan(**kwargs)
                tiers_config.set_flag(home,'auto_enabled',False)
                return result
            value.plan=scan
            return value
        with patch.object(service.common,'manager',side_effect=manager), patch.object(service.common,'activity',return_value=Activity()):
            service.clean_caches(self.home)
        self.assertTrue((cache/'object.o').exists())

    def test_invalid_config_replaces_stale_success_receipt_without_deletion(self):
        for raw in ('{', '{"auto_scope":"unsafe"}'):
            with self.subTest(raw=raw):
                path=tiers_config.settings_path(self.home);path.parent.mkdir(parents=True,exist_ok=True);path.write_text(raw)
                receipt=service.state_root(self.home)/'automatic-storage.json';receipt.parent.mkdir(parents=True,exist_ok=True);receipt.write_text('{"status":"complete"}')
                result=service.tick_storage(self.home)
                self.assertEqual(result['status'],'error')
                self.assertEqual(json.loads(receipt.read_text())['status'],'error')
                self.assertEqual(result['results'],[])

    def test_new_cache_move_keeps_old_recovery_marker(self):
        import toolcaches
        from storage import Activity
        source=self.home/'.npm';source.mkdir();(source/'keep').write_text('data')
        state=service.state_root(self.home);state.mkdir(parents=True)
        marker=state/auto_tier.MOVE_STATE;old='{"phase":"swapping","src":"old-task"}';marker.write_text(old)
        cfg=self.cfg(caches_enabled=True, external_root=str(self.external))
        with patch.object(service,'clean_caches',return_value={'status':'complete','results':[]}),              patch.object(service,'tick_tiers'), patch.object(service.Activity,'collect',return_value=Activity()):
            service.tick_storage(self.home)
        self.assertEqual(marker.read_text(),old)
        self.assertFalse(source.is_symlink())
        self.assertEqual((source/'keep').read_text(),'data')

    def test_activate_keeps_existing_pause_without_resuming_memory(self):
        state=service.state_root(self.home);state.mkdir(parents=True)
        (state/'installation.json').write_text('{"status":"active"}')
        paused=state/'storage-paused';paused.touch()
        with patch.object(service.Path,'home',return_value=self.home), patch.object(service.sys,'platform','darwin'),              patch.object(service,'install_runtime',return_value=self.home/'runtime'),              patch.object(service,'enable_job',return_value={'loaded':True}), patch.object(service.subprocess,'run') as run:
            service.activate(self.home)
        self.assertTrue(paused.exists())
        self.assertFalse(run.called)
        tiers_config.set_flag(self.home,'auto_enabled',True)
        self.assertFalse(paused.exists())

    def test_installer_does_not_replace_app_while_maintenance_runs(self):
        import plistlib
        import runpy
        from test_tiers import ROOT
        app=self.home/'Applications/资源看板.app';(app/'Contents').mkdir(parents=True)
        (app/'Contents/Info.plist').write_bytes(plistlib.dumps({'CFBundleIdentifier':'io.github.ruanjianjian.mac-dev-resource-guard.widget'}))
        (app/'keep').write_text('old app')
        calls=[]
        def run(args, **kwargs):
            calls.append(args)
            return __import__('subprocess').CompletedProcess(args,0)
        with service.operation_lock(self.home,'periodic-storage'), patch.object(service.Path,'home',return_value=self.home), \
             patch.object(service.subprocess,'run',side_effect=run), patch.object(service,'activate') as activate:
            with self.assertRaises(service.Busy):
                runpy.run_path(str(ROOT/'scripts/install.py'))
        self.assertEqual((app/'keep').read_text(),'old app')
        self.assertFalse(activate.called)
        self.assertFalse(any(args[0] in ('pkill','open') for args in calls))

    def test_installer_rejects_invalid_settings_before_any_subprocess(self):
        import runpy
        from test_tiers import ROOT
        p=tiers_config.settings_path(self.home);p.parent.mkdir(parents=True);p.write_text('{"task_roots":[{"path":"/missing/guard-task-root"}]}')
        with patch.object(service.Path,'home',return_value=self.home), patch.object(service.subprocess,'run') as run:
            with self.assertRaises(tiers_config.ConfigError):
                runpy.run_path(str(ROOT/'scripts/install.py'))
        self.assertFalse(run.called)
