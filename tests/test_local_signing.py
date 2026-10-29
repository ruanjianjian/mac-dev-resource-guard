import os
from pathlib import Path
import plistlib
import pwd
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'widget'))
import local_signing  # noqa: E402

BUNDLE = 'io.github.ruanjianjian.mac-dev-resource-guard.widget'


@unittest.skipUnless(shutil.which('codesign') and shutil.which('security'), 'needs macOS codesign and security')
class LocalSigningTest(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.home, True)
        # verify.py runs tests with a throwaway HOME, but up to macOS 26 security and codesign read the keychain
        # search list from the real one. The keychain file stays in the temp folder; signing restores the list.
        real = patch.dict(os.environ, HOME=pwd.getpwuid(os.getuid()).pw_dir)
        real.start(); self.addCleanup(real.stop)
        keychain = local_signing.folder(self.home) / 'signing.keychain-db'
        self.addCleanup(lambda: keychain.exists() and subprocess.run(['security', 'delete-keychain', str(keychain)], capture_output=True))

    def build(self, name, executable):
        """Two builds of the same app: same bundle id, different binary."""
        app = self.home / 'builds' / name / '资源看板.app'
        (app / 'Contents/MacOS').mkdir(parents=True)
        shutil.copyfile(executable, app / 'Contents/MacOS/ResourceDashboard')   # not copy2: system binaries carry a restricted flag
        os.chmod(app / 'Contents/MacOS/ResourceDashboard', 0o755)
        (app / 'Contents/Info.plist').write_bytes(plistlib.dumps({'CFBundleIdentifier': BUNDLE, 'CFBundleExecutable': 'ResourceDashboard'}))
        return app

    def test_ad_hoc_signatures_change_with_every_build(self):
        # Why macOS asked again after every reinstall: its grants follow this requirement.
        first, second = self.build('a', '/bin/ls'), self.build('b', '/bin/cat')
        for app in (first, second):
            subprocess.run(['codesign', '--force', '--sign', '-', str(app)], check=True, capture_output=True)
        self.assertIn('cdhash', local_signing.requirement(first))
        self.assertNotEqual(local_signing.requirement(first), local_signing.requirement(second))

    def test_every_build_gets_the_same_identity_made_once(self):
        first, second = self.build('a', '/bin/ls'), self.build('b', '/bin/cat')
        self.assertEqual(local_signing.sign(first, self.home), 'local')
        made = (local_signing.folder(self.home) / 'cert.pem').read_text()
        self.assertEqual(local_signing.sign(second, self.home), 'local')
        self.assertEqual((local_signing.folder(self.home) / 'cert.pem').read_text(), made, 'made once, then reused')
        requirement = local_signing.requirement(first)
        self.assertEqual(requirement, local_signing.requirement(second))
        self.assertIn('identifier "{}"'.format(BUNDLE), requirement)
        self.assertIn('certificate leaf = H"', requirement)
        base = local_signing.folder(self.home)
        self.assertEqual(os.stat(base).st_mode & 0o777, 0o700)
        self.assertEqual(os.stat(base / 'password').st_mode & 0o777, 0o600)
        listed = subprocess.run(['security', 'list-keychains', '-d', 'user'], capture_output=True, text=True).stdout
        self.assertNotIn(str(base), listed, "the user's keychain list stays as it was")

    def test_a_keychain_left_listed_by_an_interrupted_run_is_taken_off(self):
        local_signing.sign(self.build('a', '/bin/ls'), self.home)
        before = local_signing._search_list()
        self.addCleanup(lambda: subprocess.run(['security', 'list-keychains', '-d', 'user', '-s', *before], capture_output=True))
        keychain = local_signing.folder(self.home) / 'signing.keychain-db'
        subprocess.run(['security', 'list-keychains', '-d', 'user', '-s', str(keychain), *before], check=True)
        self.assertEqual(local_signing.sign(self.build('b', '/bin/cat'), self.home), 'local')
        self.assertEqual(local_signing._search_list(), before)

    def test_a_half_made_identity_is_made_again(self):
        app = self.build('a', '/bin/ls')
        local_signing.sign(app, self.home)
        (local_signing.folder(self.home) / 'password').unlink()   # e.g. interrupted before the last step
        self.assertEqual(local_signing.sign(self.build('b', '/bin/cat'), self.home), 'local')

    def test_falls_back_to_ad_hoc_when_no_identity_can_be_made(self):
        (self.home / '.local/share').mkdir(parents=True)
        (self.home / '.local/share/dev-resource-guard').write_text('not a folder')
        app = self.build('a', '/bin/ls')
        self.assertEqual(local_signing.sign(app, self.home), 'ad-hoc')
        self.assertIn('cdhash', local_signing.requirement(app))
        subprocess.run(['codesign', '--verify', '--strict', str(app)], check=True)


if __name__ == '__main__':
    unittest.main()
