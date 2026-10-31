import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_ask_tier import AskFixture, FakeSimctl
from test_tiers import DAY, git
import agent_plan
import ask_tier
import emergency as E
from storage import GIB, Activity

CRITICAL = 25 * GIB
NOW = 1_800_000_000.0


class TriggerTest(unittest.TestCase):
    def test_each_deeper_drop_starts_the_agent_once(self):
        state, fired = {}, []
        for free_gib in (40, 23, 22, 9, 8, 4, 3, 30, 20):
            reason = E.due(state, free_gib * GIB, CRITICAL, 1, NOW)
            if reason:
                E.mark_fired(state, reason, free_gib * GIB, CRITICAL, NOW)
                state['starts'] = []   # the daily cap has its own test
            fired.append((free_gib, reason))
        self.assertEqual(fired, [(40, None), (23, 'disk'), (22, None), (9, 'disk'), (8, None), (4, 'disk'), (3, None),
                                 (30, None), (20, 'disk')])

    def test_memory_pressure_starts_the_agent_after_half_an_hour_of_warning(self):
        state, free = {}, 100 * GIB
        self.assertIsNone(E.due(state, free, CRITICAL, 2, NOW))
        self.assertIsNone(E.due(state, free, CRITICAL, 2, NOW + 29 * 60))
        self.assertEqual(E.due(state, free, CRITICAL, 2, NOW + 31 * 60), 'memory')
        E.mark_fired(state, 'memory', free, CRITICAL, NOW + 31 * 60)
        self.assertIsNone(E.due(state, free, CRITICAL, 4, NOW + 2 * 3600))        # quiet for 6 hours
        self.assertIsNone(E.due(state, free, CRITICAL, 1, NOW + 3 * 3600))        # pressure gone: the half hour restarts
        self.assertNotIn('warn_since', state)
        self.assertEqual(E.due(state, free, CRITICAL, 4, NOW + 7 * 3600), 'memory')   # 「严重」 needs no half hour

    def test_no_second_run_while_one_is_going_and_four_a_day_at_most(self):
        state = {'run': {'pid': os.getpid(), 'started': NOW}}
        self.assertIsNone(E.due(state, 20 * GIB, CRITICAL, 1, NOW + 60))
        state = {'run': {'pid': os.getpid(), 'started': NOW - 40 * 60}}            # overstayed: no longer counts
        self.assertEqual(E.due(state, 20 * GIB, CRITICAL, 1, NOW), 'disk')
        state = {'starts': [NOW - h * 3600 for h in (1, 2, 3, 4)]}
        self.assertIsNone(E.due(state, 20 * GIB, CRITICAL, 1, NOW))
        state = {'starts': [NOW - h * 3600 for h in (1, 2, 3, 25)]}
        self.assertEqual(E.due(state, 20 * GIB, CRITICAL, 1, NOW), 'disk')


def executable(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('#!/bin/sh\nexit 0\n')
    path.chmod(0o755)
    return path


class FakeRun:
    """sysctl and security, answered without touching the real Mac."""
    def __init__(self, pressure=1, keychain=False):
        self.pressure, self.keychain = pressure, keychain

    def __call__(self, args, **kwargs):
        if 'kern.memorystatus_vm_pressure_level' in args:
            return subprocess.CompletedProcess(args, 0, '{}\n'.format(self.pressure), '')
        if 'vm.swapusage' in args:
            return subprocess.CompletedProcess(args, 0, 'total = 2048.00M  used = 1024.00M  free = 1024.00M', '')
        return subprocess.CompletedProcess(args, 0 if self.keychain else 44, b'', b'')


class RunnerTest(AskFixture):
    def setUp(self):
        super().setUp()
        self.bundled = executable(self.home / 'Library/Application Support/Claude/claude-code/2.1.309/abc/claude.app/Contents/MacOS/claude')
        self.codex = executable(self.home / '.local/bin/codex')
        self.folder = self.home / 'run'; self.folder.mkdir()
        (self.folder / E.MATERIAL).write_text('{}')
        which = patch.object(E.shutil, 'which', return_value=None); which.start(); self.addCleanup(which.stop)

    def test_the_recorded_agent_first_then_the_other_then_the_notice(self):
        self.assertEqual(E.runners(self.home, None, env={}, run=FakeRun()), [])          # nobody logged in
        (self.home / '.codex').mkdir(); (self.home / '.codex/auth.json').write_text('{}')
        self.assertEqual(E.runners(self.home, None, env={}, run=FakeRun()), ['codex'])  # the desktop login is not enough
        self.assertEqual(E.runners(self.home, None, env={}, run=FakeRun(keychain=True)), ['claude', 'codex'])
        self.assertEqual(E.runners(self.home, 'codex', env={}, run=FakeRun(keychain=True)), ['codex', 'claude'])
        self.assertEqual(E.claude_binary(self.home), self.bundled)

    def test_claude_runs_with_only_the_emergency_commands(self):
        seen = {}

        class Process:
            pid = None
            def __init__(self, command, **kwargs):
                seen.update(command=command, **kwargs)
            def communicate(self, timeout=None):
                return json.dumps({'is_error': False, 'result': '关了 2 个残留后台', 'session_id': 's1'}) + '\n', None

        result = E.run_claude(self.home, self.folder, 'prompt', {E.DIR_ENV: str(self.folder)}, 60, popen=Process)
        self.assertEqual(result, dict(agent='claude', status='done', session='s1', reply='关了 2 个残留后台'))
        command = seen['command']
        self.assertEqual(command[command.index('--permission-mode') + 1], 'dontAsk')
        allowed = command[command.index('--allowedTools') + 1:command.index('--max-turns')]
        self.assertEqual(allowed, ['Bash({} emergency:*)'.format(E.RGS), 'Read', 'Skill'])
        self.assertEqual(seen['cwd'], str(self.folder))

    def test_codex_starts_a_visible_thread_in_the_run_folder(self):
        sent = []

        class Pipe:
            def __init__(self):
                self.queue = []
            def write(self, text):
                message = json.loads(text)
                sent.append(message)
                if message.get('method') == 'thread/start':
                    self.queue.append({'id': message['id'], 'result': {'thread': {'id': 't1'}}})
                elif message.get('method') == 'turn/start':
                    self.queue.append({'id': message['id'], 'result': {}})
                    self.queue.append({'method': 'item/agentMessage/delta', 'params': {}})
                    self.queue.append({'id': 99, 'method': 'item/commandExecution/requestApproval', 'params': {}})
                    self.queue.append({'method': 'turn/completed', 'params': {'threadId': 't1', 'turn': {'status': 'completed'}}})
                elif 'id' in message and 'method' in message:
                    self.queue.append({'id': message['id'], 'result': {}})
            def flush(self):
                pass

        class Process:
            pid = None
            def __init__(self, command, **kwargs):
                self.stdin = Pipe()
                self.stdout = self
                self.kwargs = kwargs
            def __iter__(self):
                return self
            def __next__(self):
                if not self.stdin.queue:
                    raise StopIteration
                return json.dumps(self.stdin.queue.pop(0)) + '\n'
            def wait(self, timeout=None):
                return 0

        result = E.run_codex(self.home, self.folder, 'emergency show 那句', {}, 60, popen=Process)
        self.assertEqual(result, dict(agent='codex', status='done', thread='t1', turn='completed'))
        start = next(m for m in sent if m.get('method') == 'thread/start')['params']
        self.assertEqual(start, dict(cwd=str(self.folder), approvalPolicy='never', sandbox='workspace-write',
                                     ephemeral=False, threadSource='user'))
        named = next(m for m in sent if m.get('method') == 'thread/name/set')['params']
        self.assertEqual(named['name'], '资源看板告急处理')
        turn = next(m for m in sent if m.get('method') == 'turn/start')['params']
        self.assertEqual(turn['input'][0]['text'], 'emergency show 那句')
        refused = next(m for m in sent if m.get('id') == 99)
        self.assertIn('error', refused)   # the server's question is never approved

    def test_a_run_that_overstays_is_stopped_and_its_decisions_still_apply(self):
        class Slow:
            pid = None
            def __init__(self, *a, **k):
                pass
            def communicate(self, timeout=None):
                raise subprocess.TimeoutExpired('claude', timeout)
            def wait(self, timeout=None):
                return 0

        self.assertEqual(E.run_claude(self.home, self.folder, 'p', {}, 1, popen=Slow), dict(agent='claude', status='timeout'))
        sims = self.sims()
        folder = E.new_folder(self.home, NOW)
        material = E.collect(self.home, self.cfg(), self.settings, 'disk', NOW, activity=Activity(), simctl=sims,
                             table={}, cwd_of=lambda p: {}, prs=lambda r: None, sizer=lambda p: GIB, free=20 * GIB)
        (folder / E.MATERIAL).write_text(json.dumps(material))
        E.decide(folder, 'ask', 'simulator-delete:OLD', why='关机 10 天了')
        with patch.object(E, 'run_claude', return_value=dict(agent='claude', status='timeout')), \
                patch.object(ask_tier, 'launch_app'):
            result = E.run(self.home, self.cfg(), self.settings, 'disk', folder, 'claude', now=NOW)
        self.assertEqual([a['id'] for a in result['asks']], ['simulator-delete:OLD'])
        [notice] = agent_plan.queued_notices(self.home)
        self.assertEqual(notice['title'], '告急处理没做完')
        self.assertIn('超过 30 分钟', notice['body'])
        self.assertEqual(notice['tab'], 'pending')
        self.assertNotIn('run', E.load_state(self.home))

    def test_a_run_that_breaks_clears_itself_and_hands_over(self):
        folder = E.new_folder(self.home, NOW)
        E.save_state(self.home, {'run': {'pid': os.getpid(), 'started': time.time()}})
        broken = lambda *a, **k: (_ for _ in ()).throw(TypeError('Object of type PosixPath is not JSON serializable'))
        with self.assertRaises(TypeError):
            E.run(self.home, self.cfg(), self.settings, 'memory', folder, 'codex', collector=broken)
        state = E.load_state(self.home)
        self.assertNotIn('run', state)
        self.assertEqual(state['last']['agent'], 'crashed')
        request = json.loads((E.state_root(self.home) / 'agent-request.json').read_text())
        self.assertEqual(request['title'], '内存吃紧')


class TickTest(AskFixture):
    def setUp(self):
        super().setUp()
        which = patch.object(E.shutil, 'which', return_value=None); which.start(); self.addCleanup(which.stop)
        launch = patch.object(ask_tier, 'launch_app'); launch.start(); self.addCleanup(launch.stop)
        self.started = []
        self.starter = lambda reason, folder, runner: self.started.append((reason, runner)) or 4242

    def test_a_drop_starts_the_agent_that_can_run_and_records_the_run(self):
        executable(self.home / '.local/bin/codex')
        (self.home / '.codex').mkdir(); (self.home / '.codex/auth.json').write_text('{}')
        cfg = self.cfg()
        got = E.tick(self.home, cfg, self.settings, NOW, run=FakeRun(), env={}, starter=self.starter, free=20 * GIB)
        self.assertEqual((got['reason'], got['agent']), ('disk', 'codex'))
        self.assertEqual(self.started, [('disk', 'codex')])
        self.assertEqual(E.load_state(self.home)['run']['pid'], 4242)
        self.assertTrue(E.can_start(self.home, cfg, env={}, run=FakeRun()))
        self.assertIsNone(E.tick(self.home, cfg, self.settings, NOW + 300, run=FakeRun(), env={}, starter=self.starter,
                                 free=19 * GIB))   # same drop, same step: no second start
        self.assertIsNone(E.tick(self.home, self.cfg(emergency_agent=False), self.settings, NOW, run=FakeRun(), env={},
                                 starter=self.starter, free=1 * GIB))

    def test_with_no_agent_that_can_run_memory_falls_back_to_the_handoff_notice(self):
        cfg = self.cfg()
        state = {'warn_since': NOW - 3600}
        E.save_state(self.home, state)
        got = E.tick(self.home, cfg, self.settings, NOW, run=FakeRun(pressure=2), env={}, starter=self.starter, free=80 * GIB)
        self.assertEqual(got, dict(reason='memory', agent=None))
        self.assertEqual(self.started, [])
        request = json.loads((E.state_root(self.home) / 'agent-request.json').read_text())
        self.assertEqual(request['title'], '内存吃紧')
        self.assertFalse(E.can_start(self.home, cfg, env={}, run=FakeRun()))


class MaterialTest(AskFixture):
    def test_the_material_holds_the_evidence_and_the_users_choices(self):
        task = self.worktree_task('done-task', 0)
        (task / 'resources.json').write_text(json.dumps({'device': 'A1B2C3D4-0000-4000-8000-000000000001'}))
        sim = self.device('A1B2C3D4-0000-4000-8000-000000000001', 'Shutdown', 5, name='Done Phone')
        simctl = FakeSimctl({self.IOS: [sim]})
        agent_plan.remember(self.home, 'decision', 'emergency', words='开着但 3 小时没人连的，不要关机。', note='告急时不关开着的模拟器')
        old = 30 * 3600
        table = {
            500: dict(pid=500, ppid=1, age=old, cpu=12.0, rss=200 * 2 ** 20, lstart='Thu Oct 29 13:22:55 2026',
                      command='node /x/app-server-broker.mjs serve'),
            501: dict(pid=501, ppid=500, age=old, cpu=3.0, rss=100 * 2 ** 20, lstart='Thu Oct 29 13:22:55 2026',
                      command='codex app-server'),
            600: dict(pid=600, ppid=1, age=old, cpu=1.0, rss=50 * 2 ** 20, lstart='x', command='/Applications/Notes.app/Contents/MacOS/Notes'),
            700: dict(pid=700, ppid=1, age=old, cpu=1.0, rss=50 * 2 ** 20, lstart='x', command='postgres -D data'),
            800: dict(pid=800, ppid=1, age=3600, cpu=1.0, rss=50 * 2 ** 20, lstart='x', command='codex app-server'),
            900: dict(pid=900, ppid=1, age=47 * 3600, cpu=2000.0, rss=1000 * 2 ** 20, lstart='y',
                      command='baguette stream --udid 101B3970 --fps 1 --scale 4'),
        }
        cwd = {500: str(task / 'main'), 600: '/', 700: str(task), 800: str(task), 900: '/'}
        activity = Activity(open_paths=[(500, str(task / 'main'))], commands=[(500, 'node /x/app-server-broker.mjs serve')])
        drive = self.home / 'drive'; drive.mkdir()
        material = E.collect(self.home, self.cfg(external_root=str(drive)), self.settings, 'disk', NOW, activity=activity,
                             simctl=simctl, table=table, cwd_of=lambda pids: {p: cwd[p] for p in pids if p in cwd},
                             prs=lambda repo: '#12 MERGED', report={'tasks': [{'path': str(task), 'bytes': 3 * GIB}]}, free=4 * GIB)
        json.dumps(material)   # 10-31: a drive path in the material broke the first real run
        self.assertEqual(material['disk']['external'], str(drive))
        self.assertIn('只剩 4.0 GB', material['why'])
        # not the Notes App, not the database, not the one an hour old; the day-old screen stream nobody watches is
        [stream, orphan] = material['orphans']
        self.assertEqual((stream['pid'], stream['task']), (900, None))
        self.assertEqual((orphan['pid'], orphan['processes'], orphan['rss_mb'], orphan['task']), (500, 2, 300, str(task)))
        [entry] = material['tasks']
        self.assertEqual(entry['repos'][0]['pr'], '#12 MERGED')
        self.assertIsNone(entry['repos'][0]['unsaved'])
        self.assertEqual(entry['held_by'], [dict(pid=500, command='node /x/app-server-broker.mjs serve')])
        self.assertEqual(entry['gib'], 3.0)
        [phone] = material['simulators']
        self.assertEqual((phone['tasks'], phone['days_since_used'] >= 5), (['done-task'], True))
        self.assertTrue(any(m['words'] == '开着但 3 小时没人连的，不要关机。' for m in material['memories']))
        self.assertTrue(material['steps'] and material['rules'])


class ApplyTest(AskFixture):
    def table(self, **changes):
        row = dict(pid=999991, ppid=1, age=30 * 3600, cpu=10.0, rss=2 ** 20, lstart='Thu Oct 29 13:22:55 2026', command='codex app-server')
        child = dict(row, pid=999992, ppid=999991, cpu=1.0)
        return {999991: dict(row, **changes), 999992: child}

    def test_an_orphan_is_stopped_only_if_it_is_still_the_same_idle_orphan(self):
        entry = dict(pid=999991, lstart='Thu Oct 29 13:22:55 2026', cpu_seconds=11.0)
        kills = []
        kill = lambda pid, sig: kills.append((pid, sig))
        self.assertEqual(E.stop_orphan(entry, {}, kill), '已经不在了')
        self.assertIn('别的程序', E.stop_orphan(entry, self.table(lstart='Fri Oct 30 09:00:00 2026'), kill))
        self.assertIn('有人接管', E.stop_orphan(entry, self.table(ppid=4321), kill))
        self.assertIn('还在干活', E.stop_orphan(entry, self.table(cpu=60.0), kill))
        self.assertEqual(kills, [])
        self.assertIsNone(E.stop_orphan(entry, self.table(), kill, sleep=lambda s: None))
        self.assertEqual(kills, [(999992, signal.SIGTERM), (999991, signal.SIGTERM)])   # children first

    def test_a_task_moves_without_the_idle_days_but_never_with_unsaved_work(self):
        fresh = self.task('fresh', 0)
        dirty = self.task('dirty', 0, dirty=True)
        busy = self.task('busy', 0)
        cfg = self.cfg()
        self.assertIsNone(E.move_task(self.home, cfg, self.settings, fresh, Activity(), {}))
        self.assertTrue(fresh.is_symlink())
        self.assertIn('有未提交改动', E.move_task(self.home, cfg, self.settings, dirty, Activity(), {}))
        held = Activity(open_paths=[(4242, str(busy / 'app'))])
        self.assertIn('PID 4242', E.move_task(self.home, cfg, self.settings, busy, held, {}))
        self.assertFalse(dirty.is_symlink() or busy.is_symlink())

    def asked_run(self, *asks, status='done', now=NOW):
        task = self.worktree_task('merged-task', 0)
        folder = E.new_folder(self.home, now)
        self.simctl = self.sims()
        material = E.collect(self.home, self.cfg(), self.settings, 'disk', now, activity=Activity(), simctl=self.simctl,
                             table={}, cwd_of=lambda p: {}, prs=lambda r: '#3 MERGED', sizer=lambda p: 2 * GIB, free=8 * GIB)
        (folder / E.MATERIAL).write_text(json.dumps(material))
        for target in asks:
            E.decide(folder, 'ask', target.format(task=task), why='PR #3 已合入，改动都推了')
        E.decide(folder, 'done', summary='列了两项等你确认。')
        with patch.object(E, 'run_codex', return_value=dict(agent='codex', status=status, thread='t1')), \
                patch.object(ask_tier, 'launch_app'):
            E.run(self.home, self.cfg(), self.settings, 'disk', folder, 'codex', now=now)
        return task

    def test_asks_land_in_the_pending_list_with_one_notice(self):
        task = self.asked_run('simulator-delete:NEW', 'task-delete:{task}')
        pending = ask_tier.load(self.home, ask_tier.PENDING, ask_tier.empty_pending())
        mine = {i['id']: i for i in pending['items']}
        self.assertEqual(set(mine), {'simulator-delete:NEW', 'task-delete:' + str(task)})
        self.assertTrue(all(i['by'] == 'agent' and i['reason'] == 'agent：PR #3 已合入，改动都推了' for i in mine.values()))
        [notice] = agent_plan.queued_notices(self.home)
        self.assertEqual((notice['title'], notice['tab']), ('agent 处理了告急', 'pending'))
        self.assertIn('还有 2 项等你确认', notice['body'])
        self.assertIn('列了两项等你确认。', notice['body'])

    def test_agent_asks_survive_the_next_refresh(self):
        now = time.time()
        self.asked_run('simulator-delete:NEW', now=now)
        simctl = self.simctl
        pending = self.refresh(simctl, now=now + 3600)
        self.assertIn('simulator-delete:NEW', self.ids(pending))      # the rules would not list a sim used yesterday
        with patch.object(ask_tier, 'last_used', return_value=now):   # still in use by the rules' measure
            pending = self.refresh(simctl, now=now + (ask_tier.AGENT_ASK_DAYS + 1) * DAY)
        self.assertNotIn('simulator-delete:NEW', self.ids(pending))   # nobody answered for days: it leaves


class ConfirmTest(AskFixture):
    def ask(self, item_id, kind, title='x'):
        E.add_asks(self.home, [dict(id=item_id, kind=kind, title=title, bytes=GIB, why='证据')], time.time())

    def test_a_confirmed_agent_ask_is_rechecked_against_the_floor_only(self):
        simctl = self.sims()
        self.ask('simulator-delete:NEW', 'simulator-delete')     # used a day ago: the rules wait for 7 days
        self.ask('simulator-delete:ON', 'simulator-delete')      # still on
        [done, on] = ask_tier.execute(self.home, self.cfg(), self.settings, ['simulator-delete:NEW', 'simulator-delete:ON'],
                                      simctl, activity=Activity())
        self.assertEqual(done['outcome'], '已完成')
        self.assertEqual((on['outcome'], on['reason']), ('已跳过', '它开着，先关机才能删'))
        self.assertIn(['delete', 'NEW'], simctl.calls)
        self.assertNotIn(['delete', 'ON'], simctl.calls)
        self.ask('simulator-delete:OLD', 'simulator-delete')
        [held] = ask_tier.execute(self.home, self.cfg(), self.settings, ['simulator-delete:OLD'], simctl,
                                  activity=Activity(commands=[(7, 'xcrun simctl launch OLD app')]))
        self.assertEqual(held['reason'], '有开发工具正连着它')

    def test_notes_beside_the_code_go_to_the_drive_not_the_bin(self):
        task = self.worktree_task('merged', 0)
        (task / 'report.md').write_text('acceptance report')
        drive = self.home / 'drive'
        key = 'task-delete:' + str(task)
        self.ask(key, 'task-delete', 'merged')
        cfg = self.cfg(task_roots=[{'path': str(self.root)}])
        simctl = self.sims()
        [skipped] = ask_tier.execute(self.home, cfg, self.settings, [key], simctl, activity=Activity())
        self.assertIn('外接盘没插', skipped['reason'])
        self.assertTrue((task / 'report.md').exists())
        cfg = self.cfg(task_roots=[{'path': str(self.root)}], external_root=str(drive))
        [done] = ask_tier.execute(self.home, cfg, self.settings, [key], simctl, activity=Activity())
        self.assertEqual(done['outcome'], '已完成')
        self.assertFalse(task.exists())
        self.assertEqual((drive / ask_tier.CLOSED / 'merged/report.md').read_text(), 'acceptance report')
        self.assertNotIn(str(task), git(self.home / 'main', 'worktree', 'list', '--porcelain'))
        [again] = ask_tier.execute(self.home, cfg, self.settings, [key], simctl, activity=Activity())
        self.assertEqual(again['outcome'], '已跳过')


class CommandTest(AskFixture):
    def test_the_agent_can_only_decide_about_what_it_was_shown(self):
        folder = E.new_folder(self.home, NOW)
        material = E.collect(self.home, self.cfg(), self.settings, 'memory', NOW, activity=Activity(), simctl=self.sims(),
                             table={}, cwd_of=lambda p: {}, prs=lambda r: None, sizer=lambda p: GIB, free=80 * GIB)
        (folder / E.MATERIAL).write_text(json.dumps(material))
        self.assertEqual(E.run_folder(cwd=str(folder), env={}), folder)
        with self.assertRaises(ValueError):
            E.run_folder(cwd=str(self.home), env={})
        for args in (('stop', '12345', '没人用'), ('move', '/nowhere', '闲置'), ('ask', 'trash-empty:/x', '满了'),
                     ('ask', 'simulator-delete:NOPE', '旧'), ('ask', 'simulator-delete:OLD', '')):
            with self.assertRaises(ValueError):
                E.decide(folder, *args[:2], why=args[2])
        with self.assertRaises(ValueError):
            E.decide(folder, 'done')
        E.decide(folder, 'lesson', note='Codex 的 app-server 会在任务目录里留一天以上')
        self.assertEqual([d['action'] for d in E.decisions(folder)], ['lesson'])


if __name__ == '__main__':
    unittest.main()
