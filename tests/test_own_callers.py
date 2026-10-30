"""10-30: 「关掉所选」 never closed a simulator: the check found its own command line, which names the simulator,
and took it for a dev tool. The same happens to an agent's shell running a confirmation. Callers do not count."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'runtime/storage'))
sys.path.insert(0, str(ROOT / 'runtime/memory/scripts'))
import memory_external
from storage import callers

UDID = 'D4FB45C3-534B-41C6-8464-0452C145C6E3'


class CallersTest(unittest.TestCase):
    def test_the_chain_up_to_launchd(self):
        self.assertEqual(callers(30, {30: 20, 20: 10, 10: 1, 99: 20}), {30, 20, 10})
        self.assertEqual(callers(30, {30: 30}), {30}, 'a loop ends')

    def test_the_shell_that_started_the_check_is_not_using_what_it_names(self):
        marker = '/tmp/guard-callers-' + str(os.getpid())
        check = ('import sys; sys.path.insert(0, {!r}); from storage import Activity; '
                 'print(any({!r} in c for _, c in Activity.collect().commands))').format(str(ROOT / 'runtime/storage'), marker)
        # The parent shell names the marker, like an agent's `zsh -c "… --ids …"`; a separate process naming it still counts.
        quiet = subprocess.run(['/bin/sh', '-c', '{} -c "$0"; : {}'.format(sys.executable, marker), check],
                               capture_output=True, text=True, env=dict(os.environ, HOME=tempfile.mkdtemp()))
        self.assertEqual(quiet.stdout.strip(), 'False', quiet.stderr)
        other = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)', marker])
        self.addCleanup(other.kill)
        import time; time.sleep(0.3)
        seen = subprocess.run([sys.executable, '-c', check], capture_output=True, text=True, env=dict(os.environ, HOME=tempfile.mkdtemp()))
        self.assertEqual(seen.stdout.strip(), 'True', seen.stderr)


class SimulatorCallersTest(unittest.TestCase):
    def inspect(self, extra=()):
        me, shell = os.getpid(), 424242
        ps = ['{} {} 100 python3 backend.py memory-close --ids sim:{}'.format(me, shell, UDID),
              '{} 1 50 /bin/zsh -c python3 backend.py memory-close --ids sim:{}'.format(shell, UDID),
              '500 1 900000 launchd_sim /Users/me/Library/Developer/CoreSimulator/Devices/{}/data/var/run/launchd_bootstrap.plist'.format(UDID)]
        ps += list(extra)
        def run(args, **kwargs):
            if args[:2] == ['/usr/bin/xcrun', 'simctl']:
                out = json.dumps({'devices': {'iOS': [{'udid': UDID, 'name': 'Membership Takeover iPhone', 'state': 'Booted'}]}})
            else:
                out = '\n'.join(ps)
            return subprocess.CompletedProcess(args, 0, out, '')
        [row] = memory_external.simulator_inspect(run=run)
        return row

    def test_the_check_and_its_shell_do_not_hold_the_simulator(self):
        row = self.inspect()
        self.assertEqual((row['reason'], row['attached']), ('simulator_not_attached', []))

    def test_a_real_tool_still_does_and_is_named(self):
        row = self.inspect(['777 1 300 xcodebuild -destination id={} test'.format(UDID)])
        self.assertEqual((row['reason'], row['attached']), ('simulator_in_use', ['xcodebuild']))


class CloseMessageTest(unittest.TestCase):
    def test_each_simulator_not_closed_is_named_with_its_reason(self):
        home = Path(tempfile.mkdtemp()); (home / '.resource-dashboard-fixture').touch()
        out = subprocess.run([sys.executable, str(ROOT / 'widget/backend.py'), 'memory-close', '--ids', 'sim:' + UDID, '--home', str(home)],
                             capture_output=True, text=True, env=dict(os.environ, HOME=str(home)))
        reply = json.loads(out.stdout)
        self.assertEqual(reply['message'], '已关掉 0 台；没关：{}（隔离测试不操作真实模拟器）。'.format(UDID))
        self.assertNotIn('原因见上', reply['message'])



class SizeWordsTest(unittest.TestCase):
    def test_sizes_read_like_the_panel_never_zero_gb(self):
        sys.path.insert(0, str(ROOT / 'widget'))
        import backend
        G = 1 << 30
        self.assertEqual([backend.gb(v) for v in (int(1.2 * G), 27 * G, 18 << 20, 1000, 2048 * G, None)],
                         ['1.2 GB', '27 GB', '18 MB', '不到 1 MB', '2.0 TB', '读不到'])

if __name__ == '__main__':
    unittest.main()
