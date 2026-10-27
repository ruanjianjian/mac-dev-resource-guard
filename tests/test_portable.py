import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'runtime/storage'))
from portable_config import configure
import slots


class PortableTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='resource-install-')
        self.addCleanup(self.temp.cleanup)
        self.home=Path(self.temp.name).resolve()
        self.env={**os.environ,'HOME':str(self.home),'PYTHONDONTWRITEBYTECODE':'1'}
        self.env.pop('DEV_RESOURCE_CONFIG',None)
        self.env.pop('DEV_MEMORY_STATE_DIR',None)

    def setup_run(self,*args):
        return subprocess.run([sys.executable,str(ROOT/'scripts/setup.py'),'--home',str(self.home),*args],env=self.env,capture_output=True,text=True)

    def test_install_is_paused_idempotent_and_shared_with_claude(self):
        for _ in range(2):
            r=self.setup_run('install');self.assertEqual(r.returncode,0,r.stderr)
        a=self.home/'.codex/skills/mac-dev-resource-guard'
        b=self.home/'.claude/skills/mac-dev-resource-guard'
        self.assertEqual(a.resolve(),b.resolve())
        self.assertTrue((self.home/'.local/state/dev-memory/paused').exists())
        self.assertFalse((self.home/'Library/LaunchAgents').exists())
        r=subprocess.run([str(self.home/'.local/bin/dev-storage'),'check','--required-gib','999999'],env=self.env,capture_output=True,text=True)
        self.assertEqual(r.returncode,0,r.stderr)
        body=json.loads(r.stdout);self.assertTrue(body['advisory_only']);self.assertTrue(body['build_allowed'])
        self.assertTrue(body['below_recommended_reserve'])
        r=subprocess.run([str(self.home/'.local/bin/dev-memory'),'--help'],env=self.env,capture_output=True,text=True)
        self.assertEqual(r.returncode,0,r.stderr)

    def test_existing_command_is_preserved_without_partial_install(self):
        p=self.home/'.local/bin/dev-storage';p.parent.mkdir(parents=True);p.write_text('existing tool')
        r=self.setup_run('install');self.assertNotEqual(r.returncode,0)
        self.assertEqual(p.read_text(),'existing tool')
        self.assertFalse((self.home/'.codex/skills/mac-dev-resource-guard').exists())

    def test_standalone_observation_can_be_followed_by_install_without_state_loss(self):
        result = subprocess.run([sys.executable, str(ROOT/'widget/backend.py'), 'memory-preview', '--home', str(self.home)],
                                env=self.env, capture_output=True, text=True)
        self.assertEqual(json.loads(result.stdout)['status'], 'ok', result.stdout)
        state = self.home / '.local/state/dev-memory/state.json'
        before = state.read_bytes()
        installed = self.setup_run('install')
        self.assertEqual(installed.returncode, 0, installed.stderr)
        self.assertEqual(state.read_bytes(), before)
        self.assertTrue((state.parent/'paused').exists())

    def test_existing_config_survives(self):
        p=self.home/'.config/dev-resource-guard/config.json';p.parent.mkdir(parents=True)
        p.write_text('{"target_free_gib":150}')
        self.assertEqual(self.setup_run('install').returncode,0)
        self.assertEqual(p.read_text(),'{"target_free_gib":150}')

    def test_upgrade_rolls_back_directory_swap_failure_and_keeps_state(self):
        self.assertEqual(self.setup_run('install').returncode, 0)
        spec = importlib.util.spec_from_file_location('upgrade_setup', ROOT/'scripts/setup.py')
        setup = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(setup)
        canonical = self.home/'.codex/skills/mac-dev-resource-guard'
        before = (canonical/'runtime/storage/cli.py').read_bytes()
        config = (self.home/'.config/dev-resource-guard/config.json').read_bytes()
        replace = os.replace
        def fail_staging(source, target):
            if '.upgrade-' in Path(source).name:
                raise OSError('synthetic full disk')
            replace(source, target)
        with patch.object(setup.subprocess, 'run', return_value=type('Result', (), {'returncode': 113})()), \
             patch.object(setup.os, 'replace', side_effect=fail_staging):
            with self.assertRaisesRegex(OSError, 'full disk'):
                setup.upgrade(self.home)
        self.assertEqual((canonical/'runtime/storage/cli.py').read_bytes(), before)
        self.assertEqual((self.home/'.config/dev-resource-guard/config.json').read_bytes(), config)
        self.assertTrue((self.home/'.local/state/dev-memory/paused').exists())

    def test_cache_config_is_reflected_in_slot_and_managed_roots(self):
        p=self.home/'config.json';p.write_text(json.dumps({'cache_root':str(self.home/'cache'),'state_root':str(self.home/'state'),'volume':str(self.home)}))
        with patch.dict(os.environ,{'DEV_RESOURCE_CONFIG':str(p)}):
            s=configure()
        self.assertEqual(s.cache_roots,[self.home/'cache'])
        self.assertEqual(s.work_roots,[])
        self.assertEqual(slots.DEFAULT_CACHE_ROOT,self.home/'cache/shared/xcode')
        self.assertFalse((self.home/'cache').exists())

    def test_unsafe_configuration_rejected_before_filesystem_mutation(self):
        p=self.home/'config.json'
        for cfg in ({'cache_root':'/'},{'cache_root':str(self.home/'x'),'state_root':str(self.home/'x/state')},{'critical_free_gib':300},{'target_free_gib':float('nan')},{'work_roots':['/']}):
            with self.subTest(cfg=cfg):
                p.write_text(json.dumps(cfg))
                with patch.dict(os.environ,{'DEV_RESOURCE_CONFIG':str(p)}),self.assertRaises(ValueError): configure()
        self.assertFalse((self.home/'x').exists())

    def test_missing_device_adapter_retains_devices(self):
        spec=importlib.util.spec_from_file_location('portable_external',ROOT/'runtime/memory/scripts/memory_external.py')
        m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
        state={}
        fake_adapter=type('Adapter',(),{'inspect':lambda self,**kw:[]})()
        with patch.object(m,'device_inspect',side_effect=FileNotFoundError),patch.object(m,'adapter',return_value=fake_adapter):
            public,records=m.collect(state,1000)
        self.assertEqual(records,{})
        self.assertFalse(public[0]['eligible'])
        self.assertEqual(public[0]['reason'],'inspection_unavailable_preserved')

if __name__=='__main__': unittest.main()
