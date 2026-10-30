import io
import json
from pathlib import Path
import plistlib
import subprocess
import sys
import tempfile
import shutil
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'runtime/storage'))
sys.path.insert(0, str(ROOT / 'scripts'))
import checkup  # noqa: E402
import service  # noqa: E402
import tiers_config  # noqa: E402
import updater  # noqa: E402

NOW = 1_800_000_000.0


class Release:
    """A stand-in for GitHub's releases/latest answer."""
    def __init__(self, tag='v1.7.8', body='菜单栏数字更好认\n\n- 细节一', fail=None):
        self.tag, self.body, self.fail, self.calls = tag, body, fail, 0

    def __call__(self, request, timeout):
        self.calls += 1
        assert request.full_url == 'https://api.github.com/repos/ruanjianjian/mac-dev-resource-guard/releases/latest'
        if self.fail:
            raise self.fail
        return io.BytesIO(json.dumps(dict(tag_name=self.tag, name=self.tag, body=self.body)).encode())


class Spawn:
    def __init__(self):
        self.calls = []

    def __call__(self, args, **kwargs):
        self.calls.append((args, kwargs))
        return type('Process', (), {'pid': 999999})()


class InstalledHome(unittest.TestCase):
    """A throwaway home with 资源看板 1.7.7 installed."""
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.home, True)
        self.installed('1.7.7')

    def installed(self, version):
        info = self.home / updater.APP_INFO
        info.parent.mkdir(parents=True, exist_ok=True)
        info.write_bytes(plistlib.dumps({'CFBundleShortVersionString': version}))


class UpdaterTest(InstalledHome):
    def tick(self, now=NOW, enabled=True, release=None, spawn=None):
        return updater.tick(self.home, enabled, now=now, fetch=release or Release(), spawn=spawn or Spawn())

    def test_a_newer_release_is_installed_with_the_hand_command_pinned_to_its_tag(self):
        spawn = Spawn()
        state = self.tick(spawn=spawn)
        [(args, kwargs)] = spawn.calls
        self.assertEqual(args[:2], ['/bin/bash', '-c'])
        self.assertIn('raw.githubusercontent.com/ruanjianjian/mac-dev-resource-guard/v1.7.8/install.sh', args[2])
        self.assertIn('RESOURCE_GUARD_REF=refs/tags/v1.7.8 bash', args[2])
        self.assertTrue(kwargs['start_new_session'], 'outlives the storage tick')
        self.assertEqual(kwargs['stdin'], subprocess.DEVNULL, 'never waits for an answer')
        self.assertEqual((state['status'], state['from'], state['to'], state['summary']), ('installing', '1.7.7', '1.7.8', '菜单栏数字更好认'))
        self.assertEqual(updater.read(self.home)['status'], 'installing')

    def test_it_looks_at_most_once_a_day(self):
        self.tick(release=Release(tag='v1.7.7'))
        quiet = Release(fail=AssertionError('looked again within a day'))
        self.assertEqual(self.tick(now=NOW + 3600, release=quiet)['status'], 'current')
        later = Release()
        self.assertEqual(self.tick(now=NOW + updater.DAY + 1, release=later)['status'], 'installing')
        self.assertEqual(later.calls, 1)

    def test_the_same_or_an_older_release_changes_nothing(self):
        for tag in ('v1.7.7', 'v1.6.9'):
            spawn = Spawn()
            self.assertEqual(updater.tick(self.home, True, now=NOW, fetch=Release(tag=tag), spawn=spawn, force=True)['status'], 'current')
            self.assertEqual(spawn.calls, [])

    def test_turned_off_it_never_looks(self):
        self.assertEqual(self.tick(enabled=False, release=Release(fail=AssertionError('looked while off')))['status'], 'off')

    def test_it_waits_only_while_memory_is_critical_and_says_so(self):
        memory = self.home / '.local/state/dev-memory'; memory.mkdir(parents=True)
        (memory / 'latest.json').write_text(json.dumps(dict(sample=dict(pressure=4))))
        state = self.tick(release=Release(fail=AssertionError('looked while memory was critical')))
        self.assertEqual(state.get('waiting'), '内存很紧')
        self.assertEqual(updater.read(self.home).get('waiting'), '内存很紧', 'the reason is kept for later')
        # 「紧张」 (2) is where a Mac with several simulators sits for hours: that alone does not hold the update.
        (memory / 'latest.json').write_text(json.dumps(dict(sample=dict(pressure=2))))
        self.assertEqual(self.tick(now=NOW + 300)['status'], 'installing')

    def test_offline_is_tried_again_after_an_hour(self):
        self.assertEqual(self.tick(release=Release(fail=OSError('offline')))['status'], 'check-failed')
        self.assertEqual(self.tick(now=NOW + 1800, release=Release(fail=AssertionError('too soon')))['status'], 'check-failed')
        self.assertEqual(self.tick(now=NOW + updater.RETRY + 1)['status'], 'installing')

    def test_only_release_tags_ever_reach_the_shell(self):
        spawn = Spawn()
        self.assertEqual(self.tick(release=Release(tag='v1.7.9;touch /tmp/x'), spawn=spawn)['status'], 'current')
        self.assertEqual(spawn.calls, [])
        with self.assertRaises(ValueError):
            updater.command('main')

    def test_a_started_install_is_followed_to_its_end(self):
        self.tick()
        self.installed('1.7.8')
        self.assertEqual(self.tick(now=NOW + 300)['status'], 'installed')
        # A run that stopped short of the new version is a failure, retried after an hour, with its last lines kept.
        self.installed('1.7.7')
        state = updater.read(self.home); state.update(status='installing', checked_at=0); updater.save(self.home, state)
        (updater.state_path(self.home).parent / 'update.log').write_text('正在下载资源看板（refs/tags/v1.7.8）…\n安装未完成：下载失败\n')
        failed = self.tick(now=NOW + 600, release=Release(fail=AssertionError('retried at once')))
        self.assertEqual(failed['status'], 'failed')
        self.assertIn('下载失败', failed['error'])
        self.assertEqual(self.tick(now=NOW + 600 + updater.RETRY + 1)['status'], 'installing')

    def test_a_home_without_the_app_never_looks_or_installs(self):
        # 10-30: test runs in throwaway homes reached GitHub and installed real copies from there.
        (self.home / updater.APP_INFO).unlink()
        spawn = Spawn()
        state = self.tick(release=Release(fail=AssertionError('looked with nothing installed')), spawn=spawn)
        self.assertEqual((state['status'], spawn.calls), ('not-installed', []))

    def test_an_install_still_running_is_left_alone(self):
        spawn = Spawn()
        self.tick(spawn=spawn)
        with patch.object(updater, 'alive', return_value=True):
            self.assertEqual(self.tick(now=NOW + 300, spawn=spawn)['status'], 'installing')
        self.assertEqual(len(spawn.calls), 1)


class UpdateSwitchTest(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.home, True)

    def test_on_by_default_and_one_switch_turns_it_off(self):
        self.assertTrue(tiers_config.load(self.home)['auto_update'])
        self.assertIn('update', checkup.SUGGESTIONS)
        self.assertIn('不再自动更新', checkup.turn_off(self.home, 'update'))
        self.assertFalse(tiers_config.load(self.home)['auto_update'])
        tiers_config.set_flag(self.home, 'auto_update', True)
        self.assertTrue(tiers_config.load(self.home)['auto_update'])

    def test_the_storage_tick_looks_for_updates_after_releasing_its_lock(self):
        seen = []
        def look(home, enabled, **kwargs):
            # The installer it may start waits for this same lock, so it must be free by now.
            with service.operation_lock(home, 'periodic-storage'):
                seen.append(enabled)
            return {'status': 'current'}
        with patch.object(service, 'clean_caches', return_value={'status': 'complete', 'results': []}), \
             patch.object(service, 'tick_caches'), patch.object(service, 'tick_tiers'), patch.object(updater, 'tick', side_effect=look):
            receipt = service.tick_storage(self.home, now=NOW)
        self.assertEqual((seen, receipt['update']), ([True], 'current'))

    def test_a_broken_settings_file_or_network_never_fails_the_tick(self):
        path = tiers_config.settings_path(self.home); path.parent.mkdir(parents=True); path.write_text('{not json')
        with patch.object(updater, 'tick', side_effect=OSError('offline')):
            self.assertEqual(service.auto_update(self.home, NOW), 'error: offline')



class UpdateNowTest(InstalledHome):
    """`resource-guard-service update`, what an agent runs when the user says 「更新资源看板」."""

    def test_already_latest_installs_nothing(self):
        ran = []
        result = service.update_now(self.home, fetch=Release(tag='v1.7.7'), run=ran.append)
        self.assertEqual((result['status'], result['message'], ran), ('current', '已是最新：1.7.7', []))

    def test_a_newer_release_is_installed_here_and_checked(self):
        ran = []
        def run(args):
            ran.append(args)
            self.installed('1.7.8')
        result = service.update_now(self.home, fetch=Release(), run=run)
        self.assertEqual(ran, [updater.command('v1.7.8')])
        self.assertEqual((result['status'], result['message']), ('installed', '已更新到 1.7.8'))
        self.assertEqual(service.update_now(self.home, fetch=Release(), run=lambda args: None)['status'], 'current')

    def test_an_install_that_stops_short_says_so(self):
        result = service.update_now(self.home, fetch=Release(), run=lambda args: None)
        self.assertEqual(result['status'], 'failed')
        self.assertIn('仍是 1.7.7', result['message'])

    def test_offline_is_a_plain_answer_not_a_crash(self):
        result = service.update_now(self.home, fetch=Release(fail=OSError('nodename nor servname provided')), run=self.fail)
        self.assertEqual((result['status'], result['version']), ('check-failed', '1.7.7'))
        self.assertIn('没联网', result['message'])

if __name__ == '__main__':
    unittest.main()
