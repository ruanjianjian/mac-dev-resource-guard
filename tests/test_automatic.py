import json
import os
from pathlib import Path
import plistlib
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'scripts'))
import service
from storage import Activity


class AutomaticTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name).resolve()
        self.tier_patch = patch.object(service, 'tick_tiers', return_value=None)   # tier behaviour has its own tests
        self.tiers = self.tier_patch.start(); self.addCleanup(patch.stopall)

    def cache(self, suffix):
        path = self.home/'Library/Developer/Xcode/DerivedData'/suffix/'Build/Intermediates.noindex'
        path.mkdir(parents=True); (path/'object.o').write_bytes(b'x'*4096)
        old = time.time()-14*86400
        os.utime(path/'object.o',(old,old)); os.utime(path,(old,old))
        return path

    def test_periodic_cleans_existing_even_when_disk_healthy_but_keeps_pin(self):
        expired = self.cache('Old'); pinned = self.cache('Pinned')
        service.common.manager(self.home).pin(pinned,'acceptance','another-owner')
        with patch.object(service.common,'activity',return_value=Activity()):
            result=service.tick_storage(self.home)
        self.assertFalse(expired.exists()); self.assertTrue(pinned.exists())
        self.assertEqual(result['status'],'complete')
        self.assertEqual(len(result['results'][1]['result']['deleted']),1)

    def test_pause_during_scan_prevents_delete(self):
        expired = self.cache('Old')
        original = service.common.manager
        def manager(home):
            value=original(home); old_plan=value.plan
            def plan(**kwargs):
                result=old_plan(**kwargs)
                (service.state_root(home)/'storage-paused').touch()
                return result
            value.plan=plan; return value
        with patch.object(service.common,'manager',side_effect=manager), patch.object(service.common,'activity',return_value=Activity()):
            result=service.tick_storage(self.home)
        self.assertTrue(expired.exists()); self.assertEqual(result['status'],'paused')

    def test_unknown_activity_keeps_real_file(self):
        expired=self.cache('Old')
        with patch.object(service.common,'activity',return_value=Activity(errors=['lsof failed'])):
            service.tick_storage(self.home)
        self.assertTrue(expired.exists())

    def test_legacy_job_is_recognized_not_duplicated(self):
        p=self.home/'Library/LaunchAgents/com.ruanjianjian.dev-idle-cleanup.plist';p.parent.mkdir(parents=True)
        p.write_bytes(plistlib.dumps(dict(Label='com.ruanjianjian.dev-idle-cleanup',ProgramArguments=['python3','old/dev-memory.py','tick'],StartInterval=60)))
        with patch.object(service.subprocess,'run',return_value=subprocess.CompletedProcess([],0)):
            jobs=service.schedulers(self.home,'memory')
            result=service.enable_job(self.home,'memory',ROOT)
        self.assertTrue(jobs[0]['loaded']);self.assertEqual(result['reused'],jobs)
        self.assertEqual(len(list(p.parent.glob('*.plist'))),1)

    def test_version_updates_keep_stable_loaded_job(self):
        p=self.home/'Library/LaunchAgents'/f'{service.LABELS["storage"]}.plist';p.parent.mkdir(parents=True)
        command=[str(self.home/'.local/bin/resource-guard-service'),'tick-storage']
        p.write_bytes(plistlib.dumps(dict(Label=service.LABELS['storage'],ProgramArguments=command,StartInterval=service.STORAGE_WAKE_SECONDS)))
        with patch.object(service.subprocess,'run',return_value=subprocess.CompletedProcess([],0)) as run:
            self.assertIn('reused',service.enable_job(self.home,'storage',Path('/new/version')))
        self.assertTrue(all('bootstrap' not in call.args[0] and 'bootout' not in call.args[0] for call in run.call_args_list))

    def test_install_retains_foreign_commands_and_pins(self):
        command=self.home/'.local/bin/dev-storage';command.parent.mkdir(parents=True);command.write_text('private implementation')
        pins=self.home/'.local/state/dev-storage/pins.json';pins.parent.mkdir(parents=True);pins.write_text('{"protected":true}')
        tiny=self.home/'source';tiny.mkdir();(tiny/'scripts').mkdir();(tiny/'scripts/service.py').write_text('test')
        with patch.object(service,'ROOT',tiny):runtime=service.install_runtime(self.home)
        self.assertEqual(command.read_text(),'private implementation');self.assertEqual(pins.read_text(),'{"protected":true}')
        self.assertTrue(runtime.is_dir());self.assertTrue((self.home/'.local/bin/resource-guard-service').is_file())

    def test_public_12_job_migrates_only_when_idle(self):
        old=self.home/'.local/bin/dev-storage';old.parent.mkdir(parents=True)
        target=self.home/'.codex/skills/mac-dev-resource-guard/runtime/storage/cli.py'
        old.write_text('#!/bin/sh\nexec /usr/bin/python3 '+str(target)+' "$@"\n')
        p=self.home/'Library/LaunchAgents'/f'{service.LABELS["storage"]}.plist';p.parent.mkdir(parents=True)
        p.write_bytes(plistlib.dumps(dict(Label=service.LABELS['storage'],ProgramArguments=[str(old),'maintain','--apply'])))
        def running(args,**kwargs):return subprocess.CompletedProcess(args,0,'pid = 123\n','')
        with patch.object(service.subprocess,'run',side_effect=running) as calls:
            with self.assertRaisesRegex(ValueError,'仍在运行'):service.enable_job(self.home,'storage',ROOT)
            self.assertFalse(any('bootout' in c.args[0] for c in calls.call_args_list))
        original = p.read_bytes()
        real_write = Path.write_bytes
        def no_space(path, data):
            if '.new-' in path.name:
                raise OSError('No space left on device')
            return real_write(path, data)
        with patch.object(service.subprocess,'run',return_value=subprocess.CompletedProcess([],0,'state = not running\n','')) as calls, patch.object(Path,'write_bytes',no_space):
            with self.assertRaisesRegex(OSError,'No space'):service.enable_job(self.home,'storage',ROOT)
            self.assertEqual(p.read_bytes(),original)
            self.assertTrue(any('bootstrap' in c.args[0] for c in calls.call_args_list))
        with patch.object(service.subprocess,'run',return_value=subprocess.CompletedProcess([],0,'state = not running\n','')):
            service.enable_job(self.home,'storage',ROOT)
        self.assertEqual(plistlib.loads(p.read_bytes())['ProgramArguments'][0],str(self.home/'.local/bin/resource-guard-service'))

    def test_existing_invalid_config_is_not_replaced(self):
        p=self.home/'.config/dev-resource-guard/config.json';p.parent.mkdir(parents=True);p.write_text('{"cache_root":"/"}')
        with self.assertRaises(ValueError):service.adopt_config(self.home)
        self.assertEqual(p.read_text(),'{"cache_root":"/"}')

    def test_project_entry_is_opt_in_and_preserves_foreign_file(self):
        command=self.home/'.local/bin/resource-guard-storage';command.parent.mkdir(parents=True);command.touch()
        project=self.home/'Project with spaces';project.mkdir();(project/'source.c').write_text('source')
        result=service.integrate(self.home,project)
        self.assertIn('--worktree',Path(result['wrapper']).read_text())
        Path(result['wrapper']).write_text('my own build')
        with self.assertRaises(ValueError):service.integrate(self.home,project)
        self.assertEqual((project/'source.c').read_text(),'source')

    def link_standard_root(self, target=None):
        root=self.home/'Library/Developer/Xcode/DerivedData'; root.parent.mkdir(parents=True,exist_ok=True)
        moved=self.home/'External/Xcode-DerivedData'; moved.parent.mkdir(parents=True)
        if root.exists(): root.rename(moved)
        root.symlink_to(target or moved)
        return moved

    def receipt(self):
        return json.loads((service.state_root(self.home)/'automatic-storage.json').read_text())

    def test_linked_standard_cache_is_cleaned_by_periodic_tick(self):
        self.cache('Old'); moved=self.link_standard_root()
        with patch.object(service.common,'activity',return_value=Activity()):
            result=service.tick_storage(self.home)
        self.assertEqual(result['status'],'complete')
        self.assertFalse((moved/'Old/Build/Intermediates.noindex').exists())

    def test_family_failure_is_recorded_and_other_family_still_runs(self):
        self.cache('Old')
        with patch.object(service.common,'manager',side_effect=ValueError('boom')), patch.object(service.common,'activity',return_value=Activity()):
            result=service.tick_storage(self.home)
        self.assertEqual(result['status'],'error')
        families={r['family']:r for r in result['results']}
        self.assertIn('result',families['managed']); self.assertEqual(families['existing-xcode']['error'],'boom')
        self.assertEqual(self.receipt()['status'],'error'); self.assertIn('boom',self.receipt()['error'])

    def test_unplugged_link_target_is_skipped_not_failed(self):
        self.link_standard_root(self.home/'Unplugged/DerivedData')
        with patch.object(service.common,'activity',return_value=Activity()):
            result=service.tick_storage(self.home)
        self.assertEqual(result['status'],'complete')
        skipped=[r for r in result['results'] if r.get('skipped')]
        self.assertEqual([r['family'] for r in skipped],['existing-xcode']); self.assertIn('不存在',skipped[0]['skipped'])

    def test_invalid_config_still_writes_error_receipt(self):
        p=self.home/'.config/dev-resource-guard/config.json';p.parent.mkdir(parents=True);p.write_text('{"cache_root":"/"}')
        result=service.tick_storage(self.home)
        self.assertEqual(result['status'],'error'); self.assertEqual(self.receipt()['status'],'error')
        self.assertTrue(all(r.get('error') for r in result['results']))

    def setup_rows(self):
        sys.path.insert(0,str(ROOT/'widget')); import setup_status
        return {r['id']:r for r in setup_status.status(self.home)['rows']}

    def test_setup_page_shows_failure_skip_and_scheduler_error(self):
        state=service.state_root(self.home); state.mkdir(parents=True)
        def write(status, results, error=None):
            body=dict(at=time.time(),status=status,results=results)
            if error: body['error']=error
            (state/'automatic-storage.json').write_text(json.dumps(body))
        write('error',[dict(family='managed',result=dict(deleted=[],errors=[])),dict(family='existing-xcode',error='boom')],'已有 Xcode 缓存：boom')
        row=self.setup_rows()['automatic-last']; self.assertEqual(row['outcome'],'失败'); self.assertIn('boom',row['reason'])
        write('complete',[dict(family='managed',result=dict(deleted=[],errors=[])),dict(family='existing-xcode',skipped='指向的位置不存在')])
        row=self.setup_rows()['automatic-last']; self.assertEqual(row['outcome'],'部分跳过'); self.assertIn('已跳过',row['reason']); self.assertIn('不存在',row['reason'])
        (state/'automatic-storage.json').unlink()
        (state/'storage-scheduler.err').write_text('Traceback...\nValueError: 配置坏了\n')
        row=self.setup_rows()['automatic-last']; self.assertEqual(row['outcome'],'失败'); self.assertIn('配置坏了',row['reason'])

    def git(self, repo, *args):
        return subprocess.run(['git','-C',str(repo),*args],check=True,capture_output=True,text=True).stdout

    def test_project_entry_stays_out_of_git_status(self):
        command=self.home/'.local/bin/resource-guard-storage';command.parent.mkdir(parents=True);command.touch()
        repo=self.home/'repo'; sub=repo/'apps/mobile'; sub.mkdir(parents=True); (sub/'main.swift').write_text('source')
        subprocess.run(['git','init','-q',str(repo)],check=True); (repo/'.gitignore').write_text('build/\n')
        self.git(repo,'add','.'); self.git(repo,'-c','user.name=t','-c','user.email=t@example.com','commit','-qm','init')
        for project in (repo, sub):
            service.integrate(self.home,project)
            self.assertEqual(self.git(repo,'status','--porcelain'),'')
        self.assertEqual((repo/'.gitignore').read_text(),'build/\n')
        (repo/'.dev-resource-guard/.gitignore').unlink()  # an entry generated by 1.3
        self.assertNotEqual(self.git(repo,'status','--porcelain'),'')
        service.integrate(self.home,repo)
        self.assertEqual(self.git(repo,'status','--porcelain'),'')

    def test_13_twelve_hour_job_reloads_with_short_wake_only_when_idle(self):
        p=self.home/'Library/LaunchAgents'/f'{service.LABELS["storage"]}.plist';p.parent.mkdir(parents=True)
        command=[str(self.home/'.local/bin/resource-guard-service'),'tick-storage']
        p.write_bytes(plistlib.dumps(dict(Label=service.LABELS['storage'],ProgramArguments=command,StartInterval=43200)))
        with patch.object(service.subprocess,'run',return_value=subprocess.CompletedProcess([],0,'pid = 9\n','')) as run:
            with self.assertRaisesRegex(ValueError,'仍在运行'):service.enable_job(self.home,'storage',ROOT)
            self.assertFalse(any('bootout' in c.args[0] for c in run.call_args_list))
        self.assertEqual(plistlib.loads(p.read_bytes())['StartInterval'],43200)
        with patch.object(service.subprocess,'run',return_value=subprocess.CompletedProcess([],0,'state = not running\n','')) as run:
            service.enable_job(self.home,'storage',ROOT)
            self.assertTrue(any('bootout' in c.args[0] for c in run.call_args_list))
        self.assertEqual(plistlib.loads(p.read_bytes())['StartInterval'],service.STORAGE_WAKE_SECONDS)

    def test_cache_cleanup_keeps_twelve_hour_pace_while_tiers_run_each_wake(self):
        self.cache('Old')
        with patch.object(service.common,'activity',return_value=Activity()):
            first=service.tick_storage(self.home); second=service.tick_storage(self.home, now=time.time()+600)
            third=service.tick_storage(self.home, now=time.time()+13*3600)
        self.assertEqual(first['status'],'complete'); self.assertEqual(second['status'],'not-due'); self.assertEqual(third['status'],'complete')
        self.assertEqual(self.tiers.call_count,3)

    def test_tier_step_paces_reports_config_errors_and_yields_to_manual_cleanup(self):
        self.tier_patch.stop()
        calls=[]
        with patch.object(service.auto_tier,'run',side_effect=lambda *a,**k:calls.append('auto')), \
             patch.object(service.ask_tier,'refresh',return_value={'items':[]}), patch.object(service.ask_tier,'maybe_notify'):
            now=time.time()
            self.assertEqual(service.tick_tiers(self.home,now),'ran')
            self.assertIsNone(service.tick_tiers(self.home,now+60))                 # not due within 30 minutes
            lock=service.state_root(self.home)/'dashboard'/'operation.lock'
            with lock.open('a') as held:
                import fcntl; fcntl.flock(held,fcntl.LOCK_EX)
                self.assertEqual(service.tick_tiers(self.home,now+1900),'busy')
            settings=self.home/'.config/dev-resource-guard/auto-cleanup.json'; settings.parent.mkdir(parents=True,exist_ok=True); settings.write_text('{"auto_idle_days": 0}')
            self.assertEqual(service.tick_tiers(self.home,now+3800),'config_error')
        receipt=json.loads((service.state_root(self.home)/'auto-tier.json').read_text())
        self.assertEqual(receipt['status'],'config_error'); self.assertIn('auto_idle_days',receipt['config_error'])
        self.assertEqual(calls,['auto'])

if __name__=='__main__':unittest.main()
