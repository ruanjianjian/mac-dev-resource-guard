"""装好就省心: discovery, checkup, shared caches, Xcode compile cache, wider cleanup, one-line install."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import time
import unittest
import unittest.mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_tiers import DAY, ROOT, TierFixture, age, git
sys.path.insert(0, str(ROOT / 'scripts'))
import ask_tier
import checkup
import discover
import spaces
import tiers_config
import toolcaches
from storage import Activity

GIB = 1 << 30


class FakeLaunchctl:
    def __init__(self, env=None):
        self.env = dict(env or {})

    def __call__(self, *args):
        out = ''
        if args[0] == 'getenv':
            out = self.env.get(args[1], '')
        elif args[0] == 'setenv':
            self.env[args[1]] = args[2]
        elif args[0] == 'unsetenv':
            self.env.pop(args[1], None)
        return subprocess.CompletedProcess(args, 0, out + '\n' if out else '', '')


def idle():
    return Activity()


def drive_info(details):
    return lambda mount: details.get(Path(mount).name, {})


USB = {'Internal': False, 'Ejectable': True, 'FilesystemType': 'apfs', 'WritableVolume': True}


class DiscoverTest(TierFixture):
    def worktree(self, main, path, branch):
        path.parent.mkdir(parents=True, exist_ok=True)
        git(main, 'worktree', 'add', '-q', str(path), '-b', branch)

    def test_finds_the_folder_that_holds_tasks_and_ignores_home(self):
        main = self.home / 'code/app'; main.mkdir(parents=True)
        git(main, 'init', '-q', '-b', 'main'); git(main, 'commit', '-q', '--allow-empty', '-m', 'i')
        # Multi-repo tasks: worktrees/<task>/<repo>, and Claude's <repo>/.claude/worktrees/<name>.
        self.worktree(main, self.home / 'code/worktrees/20261001-a/app', 'a')
        self.worktree(main, self.home / 'code/worktrees/20261001-a/app-2', 'a2')
        self.worktree(main, self.home / 'code/worktrees/20261002-b/app', 'b')
        self.worktree(main, main / '.claude/worktrees/fix', 'c')
        protected = self.home / 'Documents/old-repo'; protected.mkdir(parents=True)
        git(protected, 'init', '-q')   # never walked: macOS would ask the user about Documents
        found = discover.task_roots(discover.worktrees(discover.main_repositories(self.home)), self.home)
        self.assertNotIn(protected / '.git', discover.main_repositories(self.home))
        self.assertEqual(found, sorted([self.home / 'code/worktrees', main / '.claude/worktrees']))
        self.assertEqual(discover.task_count(self.home / 'code/worktrees'), 2)

    def test_recommends_the_roomiest_usable_external_drive(self):
        volumes = self.home / 'Volumes'
        for name in ('Small', 'Big', 'Backup', 'ExFat', 'Locked', 'Inside'):
            (volumes / name).mkdir(parents=True)
        (volumes / 'Backup/Backups.backupdb').mkdir()
        details = {'Small': USB, 'Big': USB, 'Backup': USB, 'ExFat': dict(USB, FilesystemType='exfat'),
                   'Locked': dict(USB, WritableVolume=False), 'Inside': dict(USB, Internal=True, Ejectable=False)}
        free = {'Small': 10 * GIB, 'Big': 900 * GIB}
        with unittest.mock.patch('shutil.disk_usage', lambda p: type('Usage', (), {'free': free.get(Path(p).name, 1)})()):
            drives = discover.external_drives(volumes, drive_info(details), is_mount=lambda m: True)
        self.assertEqual(discover.recommended_drive(drives)['name'], 'Big')
        reasons = {d['name']: d['reason'] for d in drives}
        self.assertEqual(reasons['Backup'], '是时间机器备份盘')
        self.assertEqual(reasons['ExFat'], '格式不是 APFS')
        self.assertEqual(reasons['Locked'], '只读')
        self.assertEqual(reasons['Inside'], '不是外接盘')


class CacheFixture(TierFixture):
    def setUp(self):
        super().setUp()
        self.launchctl = FakeLaunchctl()
        self.drive = self.home / 'drive'; self.drive.mkdir()
        self.managed = self.drive / 'dev-resource-guard'

    def cache(self, relative, size=4096):
        path = self.home / relative; path.mkdir(parents=True, exist_ok=True)
        (path / 'blob').write_bytes(b'x' * size)
        return path


class CacheTest(CacheFixture):
    def test_local_caches_move_to_the_drive_and_tools_keep_their_paths(self):
        npm = self.cache('.npm'); gomod = self.cache('go/pkg/mod/golang.org/x')
        os.chmod(gomod, 0o555)   # Go keeps its module cache read-only
        self.cache('Library/pnpm/store')
        actions = toolcaches.take_over(self.home, self.managed, idle())
        self.assertEqual(sorted(a['result'] for a in actions), ['done', 'done'])
        self.assertTrue(npm.is_symlink() and (npm / 'blob').read_bytes() == b'x' * 4096)
        self.assertEqual(os.readlink(npm), str(self.managed / 'caches/npm/npm'))
        self.assertTrue((self.home / 'go/pkg/mod').is_symlink())
        self.assertFalse((self.home / 'Library/pnpm/store').is_symlink(), 'pnpm must stay beside the projects')
        rows = {r['key']: r for r in toolcaches.inspect(self.home, self.managed)}
        self.assertEqual(rows['npm:.npm']['status'], 'moved')
        self.assertEqual(rows['pnpm:Library/pnpm/store']['note'], '要和项目在同一块盘上才能共享文件，不搬')

    def test_a_running_tool_is_skipped_until_next_round(self):
        self.cache('.npm')
        busy = Activity(); busy.commands.append((42, '/usr/local/bin/npm install'))
        actions = toolcaches.take_over(self.home, self.managed, busy)
        self.assertEqual(actions[0]['result'], 'skipped')
        self.assertIn('正在运行', actions[0]['reason'])
        self.assertFalse((self.home / '.npm').is_symlink())

    def test_user_links_and_custom_locations_are_adopted_not_touched(self):
        mine = self.drive / 'my-npm'; mine.mkdir()
        os.symlink(mine, self.home / '.npm')
        env = self.home / 'Library/Application Support/go/env'; env.parent.mkdir(parents=True)
        env.write_text('GOCACHE=/Volumes/T9/go-build\n')
        rows = {r['key']: r for r in toolcaches.inspect(self.home, self.managed, sizes=False)}
        self.assertEqual(rows['npm:.npm']['status'], 'linked')
        self.assertEqual(rows['go:Library/Caches/go-build']['status'], 'custom')
        self.assertEqual(toolcaches.take_over(self.home, self.managed, idle()), [])
        toolcaches.restore(self.home)
        self.assertEqual(os.readlink(self.home / '.npm'), str(mine))

    def test_background_inspection_never_reads_the_external_drive(self):
        drive = self.home / 'Volumes/T9/dev-cache/npm'; drive.mkdir(parents=True)
        os.symlink(drive, self.home / '.npm')
        self.cache('Library/Caches/pip')
        measured = []
        with unittest.mock.patch.object(toolcaches.T, 'size', lambda p: measured.append(str(p)) or 1):
            rows = {r['key']: r for r in toolcaches.inspect(self.home, touch_external=False)}
        self.assertEqual(rows['npm:.npm']['dest'], str(drive))
        self.assertIsNone(rows['npm:.npm']['bytes'])
        self.assertEqual(measured, [str(self.home / 'Library/Caches/pip')])

    def test_restore_brings_tools_back_and_removes_the_copies(self):
        self.cache('.npm')
        toolcaches.take_over(self.home, self.managed, idle())
        toolcaches.restore(self.home)
        npm = self.home / '.npm'
        self.assertTrue(npm.is_dir() and not npm.is_symlink())
        self.assertFalse((self.managed / 'caches/npm/npm').exists())
        self.assertEqual(toolcaches.load_state(self.home)['entries'], {})

    def test_unplugged_or_full_drive_moves_nothing(self):
        self.cache('.npm')
        gone = toolcaches.take_over(self.home, Path('/Volumes/NoSuchGuardDrive/dev-resource-guard'), idle())
        self.assertEqual(gone[0]['reason'], '外接盘未连接')
        self.assertFalse(Path('/Volumes/NoSuchGuardDrive').exists())
        full = toolcaches.take_over(self.home, self.managed, idle(), free_bytes=lambda p: 10)
        self.assertEqual(full[0]['reason'], '外接盘空间不够')
        self.assertFalse((self.home / '.npm').is_symlink())


class XcodeCacheTest(CacheFixture):
    def test_enable_reaches_new_shells_and_apps_and_disable_undoes_it(self):
        rc = self.home / '.zshenv'; rc.write_text('export PATH=$HOME/bin:$PATH\n')
        toolcaches.enable_xcode_cache(self.home, self.launchctl)
        path = toolcaches.xcconfig_path(self.home)
        self.assertIn('COMPILATION_CACHE_ENABLE_CACHING = YES', path.read_text())
        self.assertEqual(self.launchctl.env['XCODE_XCCONFIG_FILE'], str(path))
        out = subprocess.run(['zsh', '-c', 'echo $XCODE_XCCONFIG_FILE'], env=dict(HOME=str(self.home), ZDOTDIR=str(self.home)),
                             capture_output=True, text=True).stdout.strip()
        self.assertEqual(out, str(path))
        toolcaches.enable_xcode_cache(self.home, self.launchctl)   # twice: still one block
        self.assertEqual(rc.read_text().count(toolcaches.BEGIN), 1)
        toolcaches.disable_xcode_cache(self.home, self.launchctl)
        self.assertEqual(rc.read_text(), 'export PATH=$HOME/bin:$PATH\n')
        self.assertNotIn('XCODE_XCCONFIG_FILE', self.launchctl.env)
        self.assertTrue(path.exists(), 'shells opened earlier still point at the file')
        self.assertNotIn('ENABLE_CACHING = YES', path.read_text())

    def test_someone_elses_global_settings_are_not_overwritten(self):
        launchctl = FakeLaunchctl({'XCODE_XCCONFIG_FILE': '/Users/me/team.xcconfig'})
        with self.assertRaises(toolcaches.Conflict):
            toolcaches.enable_xcode_cache(self.home, launchctl)
        (self.home / '.zshrc').write_text('export XCODE_XCCONFIG_FILE=~/mine.xcconfig\n')
        with self.assertRaises(toolcaches.Conflict):
            toolcaches.enable_xcode_cache(self.home, self.launchctl)
        self.assertFalse(toolcaches.xcconfig_path(self.home).exists())

    def test_periodic_job_puts_the_setting_back_after_logout(self):
        toolcaches.enable_xcode_cache(self.home, self.launchctl)
        self.launchctl.env.clear()
        toolcaches.ensure_env(self.home, self.launchctl)
        self.assertEqual(self.launchctl.env['XCODE_XCCONFIG_FILE'], str(toolcaches.xcconfig_path(self.home)))


class CheckupTest(CacheFixture):
    def scan(self):
        (self.home / 'Volumes/T9').mkdir(parents=True, exist_ok=True)
        if not tiers_config.read_raw(self.home).get('task_roots'):
            tiers_config.update(self.home, dict(task_roots=[dict(path=str(self.root))]))
        cfg = tiers_config.load(self.home)
        return checkup.scan(self.home, cfg, simulators_bytes=0, volumes=self.home / 'Volumes',
                            info=drive_info({'T9': USB}), is_mount=lambda m: True)

    def test_scan_lists_space_and_apply_turns_everything_on_once(self):
        self.task('20261001-old', idle_days=5); self.task('20261020-new', idle_days=0)
        self.cache('.npm', 1 << 20)
        report = self.scan()
        titles = {c['id']: c for c in report['categories']}
        self.assertGreater(titles['tasks']['bytes'], 0)
        self.assertEqual(report['recommended']['name'], 'T9')
        self.assertEqual([r['path'] for r in report['roots']], [str(self.root)])
        self.assertEqual(sum(t['idle'] for t in report['tasks']), 1)
        cfg = tiers_config.load(self.home)
        self.assertGreater(checkup.estimate(report, cfg), 1 << 20)
        cfg, notes = checkup.apply(self.home, ['caches', 'clean', 'external'], launchctl=self.launchctl, probe=lambda p: None)
        self.assertEqual(notes, [])
        self.assertTrue(cfg['auto_enabled'] and cfg['ask_enabled'] and cfg['caches_enabled'] and cfg['xcode_compile_cache'])
        self.assertEqual(cfg['external_root'], self.home / 'Volumes/T9/dev-resource-guard')
        self.assertEqual(cfg['task_roots'][0]['external'], self.home / 'Volumes/T9/dev-resource-guard/tasks/worktrees')

    def test_hand_written_settings_are_kept_and_not_duplicated(self):
        self.task('20261001-old', idle_days=5)
        mine = self.home / 'mine-external'
        tiers_config.update(self.home, dict(task_roots=[dict(path=str(self.root), external=str(mine))], auto_idle_days=4))
        self.scan()
        cfg, _ = checkup.apply(self.home, ['clean', 'external'], launchctl=self.launchctl, probe=lambda p: None)
        self.assertEqual(len(cfg['task_roots']), 1)
        self.assertEqual(cfg['task_roots'][0]['external'], mine)
        self.assertEqual(cfg['auto_idle_days'], 4)

    def test_refused_drive_access_leaves_the_other_two_on(self):
        self.task('20261001-old', idle_days=5)
        self.scan()
        cfg, notes = checkup.apply(self.home, ['caches', 'clean', 'external'], launchctl=self.launchctl,
                                   probe=lambda p: '后台任务没有访问外接盘的权限')
        self.assertIsNone(cfg['external_root'])
        self.assertTrue(cfg['auto_enabled'] and cfg['caches_enabled'])
        self.assertIn('没有访问外接盘的权限', notes[0])

    def test_unplugged_drive_is_reported_with_what_it_takes_away(self):
        self.task('20261001-old', idle_days=5)
        gone = Path('/Volumes/NoSuchGuardDrive')
        tiers_config.update(self.home, dict(external_root=str(gone / 'dev-resource-guard'),
                                            task_roots=[dict(path=str(self.root), external=str(gone / 'tasks'))]))
        os.symlink(gone / 'tasks/20261009-moved', self.root / '20261009-moved')
        os.symlink(gone / 'dev-resource-guard/caches/npm/npm', self.home / '.npm')
        toolcaches.save_state(self.home, dict(schema=1, leftovers=[], entries={'npm:.npm': dict(
            state='moved', path=str(self.home / '.npm'), dest=str(gone / 'dev-resource-guard/caches/npm/npm'))}))
        [drive] = checkup.disconnected(self.home, tiers_config.load(self.home))
        self.assertEqual((drive['name'], drive['caches'], drive['tasks']), ('NoSuchGuardDrive', ['npm'], 1))

    def test_caches_without_a_drive_stay_put_but_xcode_cache_turns_on(self):
        import service
        npm = self.cache('.npm'); self.scan()
        cfg, notes = checkup.apply(self.home, ['caches'], launchctl=self.launchctl, probe=lambda p: None)
        self.assertTrue(cfg['caches_enabled'] and cfg['xcode_compile_cache'])
        self.assertIsNone(cfg['external_root'])
        self.assertIsNone(service.tick_caches(self.home, time.time(), launchctl=self.launchctl))
        self.assertFalse(npm.is_symlink())

    def test_turning_caches_off_restores_and_switches_xcode_off(self):
        self.cache('.npm'); self.scan()
        checkup.apply(self.home, ['caches', 'external'], launchctl=self.launchctl, probe=lambda p: None)
        toolcaches.take_over(self.home, tiers_config.load(self.home)['external_root'], idle())
        self.assertTrue((self.home / '.npm').is_symlink())
        checkup.turn_off(self.home, 'caches', launchctl=self.launchctl)
        cfg = tiers_config.load(self.home)
        self.assertFalse(cfg['caches_enabled'] or cfg['xcode_compile_cache'])
        self.assertFalse((self.home / '.npm').is_symlink())
        self.assertNotIn('XCODE_XCCONFIG_FILE', self.launchctl.env)


class WiderCleanupTest(TierFixture):
    def file(self, relative, size, days):
        path = self.home / relative; path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, 'wb') as f:
            f.truncate(size)
        when = time.time() - days * DAY
        os.utime(path, (when, when))
        return path

    def collect(self, mounted=()):
        found = {}
        def add(kind, key, title, bytes_, reason, fingerprint, **extra):
            found[kind + ':' + key] = dict(kind=kind, title=title, bytes=bytes_, reason=reason)
        ask_tier.other_candidates(self.home, idle(), time.time(), add, mounted=set(mounted))
        return found

    def grant_downloads(self):
        (self.home / 'Downloads').mkdir(exist_ok=True)
        spaces.installers(self.home, foreground=True)

    def test_downloads_is_untouched_in_the_background_until_read_from_the_panel(self):
        old = self.file('Downloads/Xcode_27.xip', 300 << 20, 10)
        self.assertEqual(self.collect(), {})
        self.assertEqual(spaces.installers(self.home, foreground=True)[0]['path'], str(old))
        self.assertIn('installer-delete:' + str(old), self.collect())

    def test_container_disk_images_are_only_reported(self):
        orb = self.home / '.orbstack/data'; orb.mkdir(parents=True); (orb / 'disk.img').write_bytes(b'x' * 8192)
        [orbstack] = [c for c in spaces.containers(self.home) if c['name'] == 'OrbStack']
        self.assertGreater(orbstack['bytes'], 0)
        self.assertIn('请在 OrbStack 里清理', orbstack['note'])
        self.assertEqual(self.collect(), {}, 'never proposed for deletion')

    def test_old_big_installers_are_proposed_new_small_or_mounted_are_not(self):
        self.grant_downloads()
        old = self.file('Downloads/Xcode_27.xip', 300 << 20, 10)
        self.file('Downloads/new.dmg', 300 << 20, 1)
        self.file('Downloads/small.pkg', 10 << 20, 30)
        mounted = self.file('Downloads/tool.dmg', 300 << 20, 30)
        self.file('Downloads/movie.mov', 900 << 20, 30)
        found = self.collect(mounted=[str(mounted)])
        self.assertEqual(list(found), ['installer-delete:' + str(old)])
        self.assertEqual(ask_tier.LOSES['installer-delete'], '安装包文件，要用时得重新下载')

    def test_device_support_and_archives_by_age(self):
        stale = self.home / 'Library/Developer/Xcode/iOS DeviceSupport/26.0 (23A1)'; stale.mkdir(parents=True)
        (stale / 'Symbols').mkdir(); age(stale, 40)
        fresh = self.home / 'Library/Developer/Xcode/iOS DeviceSupport/27.0 (24A1)'; fresh.mkdir(parents=True)
        archive = self.home / 'Library/Developer/Xcode/Archives/2026-01-01/App 1-1-26.xcarchive'; archive.mkdir(parents=True)
        age(archive, 120)
        found = self.collect()
        self.assertEqual(sorted(k.split(':')[0] for k in found), ['archive-delete', 'device-support-delete'])

    def trash(self):
        path = self.home / '.Trash/big.bin'; path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'x' * 65536)

    @unittest.mock.patch.object(spaces, 'TRASH_MIN_BYTES', 32768)
    def test_trash_only_when_readable_and_big(self):
        self.trash()
        self.assertIn('trash-empty:' + str(self.home / '.Trash'), self.collect())
        os.chmod(self.home / '.Trash', 0)
        try:
            self.assertIsNone(spaces.trash_size(self.home))
            self.assertEqual(self.collect(), {})
        finally:
            os.chmod(self.home / '.Trash', 0o755)

    @unittest.mock.patch.object(spaces, 'TRASH_MIN_BYTES', 32768)
    def test_confirmed_installer_is_deleted_and_trash_goes_through_finder(self):
        self.grant_downloads()
        old = self.file('Downloads/Old.dmg', 300 << 20, 10)
        self.trash()
        cfg = tiers_config.load(self.home)
        emptied = []
        simctl = lambda args: json.dumps({'devices': {}}) if args[0] == 'list' else '{}'
        results = ask_tier.execute(self.home, cfg, self.settings, ['installer-delete:' + str(old), 'trash-empty:' + str(self.home / '.Trash')],
                                   simctl, activity=idle(), trash_emptier=lambda: emptied.append(True))
        self.assertEqual([r['outcome'] for r in results], ['已完成', '已完成'])
        self.assertFalse(old.exists())
        self.assertEqual(emptied, [True])


class TickTest(CacheFixture):
    def test_tick_moves_caches_once_turned_on_and_logs_them(self):
        import service
        self.cache('.npm')
        tiers_config.update(self.home, dict(caches_enabled=True, external_root=str(self.managed)))
        with unittest.mock.patch.object(service.Activity, 'collect', classmethod(lambda cls, root=None: Activity())):
            actions = service.tick_caches(self.home, time.time(), launchctl=self.launchctl)
        self.assertEqual([a['result'] for a in actions], ['done'])
        log = (self.home / '.local/state/dev-resource-guard/auto-tier-log.jsonl').read_text().splitlines()
        self.assertEqual(json.loads(log[-1])['actions'][0]['kind'], 'cache-move')
        self.assertIsNone(service.tick_caches(self.home, time.time(), launchctl=self.launchctl), 'nothing left to move')


class InstallScriptTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.home = self.base / 'home'; self.home.mkdir()

    def tarball(self, name, install_body):
        tree = self.base / ('src-' + name) / 'mac-dev-resource-guard-main'
        (tree / 'scripts').mkdir(parents=True)
        (tree / 'scripts/install.py').write_text(install_body)
        out = self.base / (name + '.tar.gz')
        with tarfile.open(out, 'w:gz') as tar:
            tar.add(tree, arcname=tree.name)
        return out

    def run_script(self, url):
        env = dict(os.environ, HOME=str(self.home), RESOURCE_GUARD_TARBALL=url)
        return subprocess.run(['bash', str(ROOT / 'install.sh')], env=env, capture_output=True, text=True)

    def test_downloads_swaps_source_and_runs_the_installer(self):
        good = self.tarball('good', 'print("installer ran", __file__)\n')
        result = self.run_script('file://' + str(good))
        self.assertEqual(result.returncode, 0, result.stderr)
        source = self.home / '.local/share/dev-resource-guard/source'
        self.assertIn('installer ran ' + str(source / 'scripts/install.py'), result.stdout)
        again = self.run_script('file://' + str(self.tarball('newer', 'print("v2")\n')))
        self.assertIn('v2', again.stdout)
        self.assertEqual(sorted(p.name for p in source.parent.iterdir()), ['source'])

    def test_broken_download_keeps_what_was_installed(self):
        self.run_script('file://' + str(self.tarball('good', 'print("v1")\n')))
        broken = self.base / 'broken.tar.gz'; broken.write_bytes(b'not a tarball')
        result = self.run_script('file://' + str(broken))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('下载失败或源码包不完整', result.stderr)
        source = self.home / '.local/share/dev-resource-guard/source'
        self.assertEqual((source / 'scripts/install.py').read_text(), 'print("v1")\n')
        self.assertEqual(sorted(p.name for p in source.parent.iterdir()), ['source'])


if __name__ == '__main__':
    unittest.main()
