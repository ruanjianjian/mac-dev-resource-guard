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
import auto_tier
import emergency as E
import owners as O
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
            fired.append((free_gib, reason))
        self.assertEqual(fired, [(40, None), (23, 'disk'), (22, None), (9, 'disk'), (8, None), (4, 'disk'), (3, None),
                                 (30, None), (20, 'disk')])

    def test_memory_pressure_starts_the_agent_after_a_quarter_hour_of_warning(self):
        state, free = {}, 100 * GIB
        self.assertIsNone(E.due(state, free, CRITICAL, 2, NOW))
        self.assertIsNone(E.due(state, free, CRITICAL, 2, NOW + 14 * 60))   # the watch's own work comes first
        self.assertEqual(E.due(state, free, CRITICAL, 2, NOW + 16 * 60), 'memory')
        E.mark_fired(state, 'memory', free, CRITICAL, NOW + 16 * 60)
        self.assertIsNone(E.due(state, free, CRITICAL, 4, NOW + 2 * 3600))        # quiet for 6 hours
        self.assertIsNone(E.due(state, free, CRITICAL, 1, NOW + 3 * 3600))        # pressure gone: the half hour restarts
        self.assertNotIn('warn_since', state)
        self.assertEqual(E.due(state, free, CRITICAL, 4, NOW + 7 * 3600), 'memory')   # 「严重」 needs no half hour

    def test_no_second_run_while_one_is_going_and_no_count_a_day(self):
        state = {'run': {'pid': os.getpid(), 'started': NOW}}
        self.assertIsNone(E.due(state, 20 * GIB, CRITICAL, 1, NOW + 60))
        state = {'run': {'pid': os.getpid(), 'started': NOW - 40 * 60}}            # overstayed: no longer counts
        self.assertEqual(E.due(state, 20 * GIB, CRITICAL, 1, NOW), 'disk')
        # However many times it came today, a new drop brings it again (RAW-53).
        state = {'starts': [NOW - h * 600 for h in range(1, 40)], 'disk_fired': 1}
        self.assertEqual(E.due(state, 9 * GIB, CRITICAL, 1, NOW), 'disk')

    def test_still_tight_and_something_new_brings_the_agent_back_within_the_quiet_hours(self):
        group = lambda tool, gib, owner=('agent', 'Codex'), projects=(): dict(
            tool=tool, title=tool, owner=dict(kind=owner[0], name=owner[1]), projects=list(projects), bytes=gib * GIB,
            fact=owner[1] + ' 在用', busy_since=None, count=1, cores=0.0)
        looked_at = lambda groups, swap, at, pressure=2: E.view({'who': {'at': at, 'groups': groups},
                                                                 'samples': [{'swap_used': swap}]}, pressure, at)
        state, free = {'warn_since': NOW - 3600}, 100 * GIB
        first = looked_at([group('tsc', 2), group('ugrep', 0.2)], 16.0, NOW)
        self.assertEqual(list(first['heavy']), ['tsc|agent|Codex'])   # only what is worth a look
        self.assertEqual(E.due(state, free, CRITICAL, 2, NOW, first), 'memory')
        E.mark_fired(state, 'memory', free, CRITICAL, NOW)
        state['looked'] = first
        hour = NOW + 3600
        self.assertIsNone(E.due(state, free, CRITICAL, 2, hour, looked_at([group('tsc', 2)], 16.5, hour)))   # the same again
        sims = [group('tsc', 2), group('iOS 模拟器 · iPhone 17 Pro', 1.9, ('agent', 'Claude Code'), ['shop'])]
        soon = NOW + 20 * 60
        self.assertIsNone(E.due(state, free, CRITICAL, 2, soon, looked_at(sims, 16.5, soon)))   # new, but too soon
        later = NOW + 31 * 60
        self.assertEqual(E.news(first, looked_at(sims, 16.5, later)), ['iOS 模拟器 · iPhone 17 Pro · shop · Claude Code 在用'])
        self.assertEqual(E.due(state, free, CRITICAL, 2, later, looked_at(sims, 16.5, later)), 'memory')
        self.assertEqual(E.news(first, looked_at([group('tsc', 2)], 19.0, later)), ['swap 比上次多了 3 GB（16 → 19 GB）'])
        self.assertEqual(E.news(first, looked_at([group('tsc', 2)], 16.0, later, pressure=4)), ['内存压力到了「严重」'])
        updated = {'warn_since': NOW - 3600, 'memory_fired_at': NOW}   # fired by a version that kept no look
        self.assertEqual(E.due(updated, free, CRITICAL, 2, later, looked_at([group('tsc', 2)], 16.0, later)), 'memory')


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
        if args[1:4] == ['auth', 'status', '--json']:   # Claude Code says whether it is signed in
            return subprocess.CompletedProcess(args, 0 if self.keychain else 1, json.dumps({'loggedIn': self.keychain}), '')
        return subprocess.CompletedProcess(args, 0, '', '')


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
        self.assertEqual(result, dict(agent='claude', status='done', session='s1', reply='关了 2 个残留后台', said='关了 2 个残留后台'))
        command = seen['command']
        self.assertEqual(command[command.index('--permission-mode') + 1], 'dontAsk')
        allowed = command[command.index('--allowedTools') + 1:command.index('--max-turns')]
        # 11-10: it may look at anything, read-only; it changes things only by handing back decisions.
        self.assertEqual(allowed, ['Bash({} emergency:*)'.format(E.RGS), 'Read', 'Skill', 'Glob', 'Grep',
                                   *['Bash({}:*)'.format(c) for c in E.READ_ONLY]])
        self.assertIn('Bash(ps:*)', allowed)
        commands = [a[5:].split()[0] for a in allowed if a.startswith('Bash(')]
        self.assertFalse(set(commands) & {'rm', 'kill', 'find', 'git', 'mv', 'simctl'})
        root = str(E.state_root(self.home) / E.RUNS)
        self.assertEqual(seen['cwd'], root)   # not the run's folder: those get pruned, the conversation stays
        session = json.loads((self.folder / E.SESSION).read_text())   # chosen up front: openable afterwards (AC-11)
        self.assertEqual(command[command.index('--session-id') + 1], session['session'])
        self.assertEqual((session['agent'], session['cwd']), ('claude', root))
        self.assertTrue(command[command.index('-p') + 1].startswith(E.SUB))   # an emergency: a sub-agent of its own
        self.assertIsNone(E.conversation(self.home, 'claude'))

    def test_every_claude_run_continues_the_one_session(self):
        commands = []

        class Process:
            pid = None
            def __init__(self, command, **kwargs):
                commands.append(command)
            def communicate(self, timeout=None):
                return json.dumps({'is_error': False, 'result': 'ok', 'session_id': 's'}) + '\n', None

        E.run_claude(self.home, self.folder, 'p1', {}, 60, popen=Process, main=True)
        first = commands[-1][commands[-1].index('--session-id') + 1]
        self.assertTrue(commands[-1][commands[-1].index('-p') + 1].startswith(E.FIRST))
        transcript = self.home / '.claude/projects/-runs' / (first + '.jsonl')
        transcript.parent.mkdir(parents=True); transcript.write_text('{}')
        E.run_claude(self.home, self.folder, 'e1', {}, 60, popen=Process)   # an emergency in between: its own session
        self.assertNotEqual(commands[-1][commands[-1].index('--session-id') + 1], first)
        self.assertNotIn('--resume', commands[-1])
        E.run_claude(self.home, self.folder, 'p2', {}, 60, popen=Process, main=True)
        again = commands[-1]
        self.assertEqual(again[again.index('--resume') + 1], first)
        self.assertNotIn('--session-id', again)
        self.assertEqual(again[again.index('-p') + 1], 'p2' + E.AGAIN)
        self.assertEqual(json.loads((self.folder / E.SESSION).read_text())['session'], first)
        transcript.unlink()   # the user deleted it: a new one, not a run that cannot start
        E.run_claude(self.home, self.folder, 'p3', {}, 60, popen=Process, main=True)
        self.assertNotEqual(commands[-1][commands[-1].index('--session-id') + 1], first)

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
        self.assertEqual(result, dict(agent='codex', status='done', thread='t1', turn='completed', said=''))
        self.assertIsNone(E.conversation(self.home, 'codex'))   # an emergency's thread is not the main agent's
        self.assertEqual(json.loads((self.folder / E.SESSION).read_text())['thread'], 't1')
        self.assertFalse(any(m.get('method') == 'thread/resume' for m in sent))
        start = next(m for m in sent if m.get('method') == 'thread/start')['params']
        self.assertEqual(start, dict(cwd=str(E.state_root(self.home) / E.RUNS), approvalPolicy='never',
                                     sandbox='workspace-write', ephemeral=False, threadSource='user'))
        named = next(m for m in sent if m.get('method') == 'thread/name/set')['params']
        self.assertTrue(named['name'].startswith('资源看板 · 告急 '))
        turn = next(m for m in sent if m.get('method') == 'turn/start')['params']
        self.assertEqual(turn['input'][0]['text'], E.SUB + 'emergency show 那句')
        # 11-10: done, it is archived, so the App's list does not fill with one thread per emergency.
        self.assertEqual([m['params'] for m in sent if m.get('method') == 'thread/archive'], [dict(threadId='t1')])
        self.assertGreater(sent.index(next(m for m in sent if m.get('method') == 'thread/archive')), sent.index(next(
            m for m in sent if m.get('method') == 'turn/start')))
        refused = next(m for m in sent if m.get('id') == 99)
        self.assertIn('error', refused)   # the server's question is never approved

    def test_every_codex_run_continues_the_one_thread(self):
        sent, owner, turns, queued, fresh = [], {}, {}, [], ['one', 'two', 'three']

        class Pipe:
            def __init__(self):
                self.queue = []
            def write(self, text):
                message = json.loads(text)
                sent.append(message)
                method, params = message.get('method'), message.get('params') or {}
                if method == 'thread/resume':
                    held = owner.get(params['threadId'])
                    self.queue.append({'id': message['id'], 'result': {}} if held is None else
                                      {'id': message['id'], 'error': {'message': held}})
                elif method == 'thread/start':
                    self.queue.append({'id': message['id'], 'result': {'thread': {'id': fresh.pop(0)}}})
                elif method == 'turn/start':
                    thread = params['threadId']
                    turns.setdefault(thread, []).append({'status': 'completed', 'completedAt': NOW, 'items': [
                        {'type': 'userMessage', 'content': params['input']},
                        {'type': 'agentMessage', 'text': '处理完了'}]})
                    self.queue.append({'method': 'turn/completed', 'params': {'threadId': thread, 'turn': {'status': 'completed'}}})
                elif method == 'thread/turns/list':
                    listed = turns.get(params['threadId'], [])
                    self.queue.append({'id': message['id'], 'result': {'data': list(reversed(listed))[:params['limit']]}})
                    for turn in listed:   # the App works through what it was handed, one look at a time;
                        # from outside, a turn it is still running reads 「interrupted」 with no end time (11-02 22:28)
                        if turn['status'] == 'interrupted':
                            turn.update(status='completed', completedAt=NOW)
                        elif turn['status'] == 'queued':
                            turn['status'] = 'interrupted'
                elif 'id' in message:
                    self.queue.append({'id': message['id'], 'result': {}})
            def flush(self):
                pass

        class Process:
            pid = None
            def __init__(self, command, **kwargs):
                self.stdin = Pipe(); self.stdout = self
            def __iter__(self):
                return self
            def __next__(self):
                if not self.stdin.queue:
                    raise StopIteration
                return json.dumps(self.stdin.queue.pop(0)) + '\n'
            def wait(self, timeout=None):
                return 0

        def run(command, **kwargs):   # `codex queue`: the App that owns the thread takes the words
            if 'queue' not in command or owner.get('no-queue'):
                return subprocess.CompletedProcess(command, 2)
            thread, words = command[command.index('--thread') + 1], command[command.index('--message') + 1]
            queued.append((thread, words, kwargs.get('env')))
            turns.setdefault(thread, []).append({'status': 'queued', 'items': [
                {'type': 'userMessage', 'content': [{'type': 'text', 'text': words}]},
                {'type': 'agentMessage', 'text': 'RG-DECISION {"action": "lesson", "when": "只读", "then": "放进回复"}'}]})
            return subprocess.CompletedProcess(command, 0)

        # The threads from before ran inside an old run's folder: not taken over, one new thread is started.
        E.note_session(E.new_folder(self.home, NOW - 86400), agent='codex', thread='old')
        # The one every run continued before 11-10 (no `since`): archived, not continued.
        path = E.state_root(self.home) / E.RUNS / E.CONVERSATION
        path.write_text(json.dumps({'codex': {'thread': 'every-run', 'agent': 'codex', 'at': NOW - 3600}}))
        go = lambda: E.run_codex(self.home, self.folder, 'p', {E.DIR_ENV: str(self.folder)}, 60, popen=Process, run=run, main=True)
        self.assertEqual(go()['thread'], 'one')
        self.assertFalse(any(m.get('method') == 'thread/resume' for m in sent))
        self.assertEqual([m['params']['threadId'] for m in sent if m.get('method') == 'thread/archive'], ['every-run'])
        self.assertEqual(next(m for m in sent if m.get('method') == 'turn/start')['params']['input'][0]['text'], E.FIRST + 'p')
        sent.clear()
        result = go()   # the next run continues it
        self.assertEqual((result['thread'], result['said']), ('one', '处理完了'))
        resume = next(m for m in sent if m.get('method') == 'thread/resume')['params']
        self.assertEqual(resume, dict(threadId='one', cwd=str(E.state_root(self.home) / E.RUNS), approvalPolicy='never',
                                      sandbox='workspace-write', excludeTurns=True))
        self.assertFalse(any(m.get('method') in ('thread/start', 'thread/archive') for m in sent))
        turn = next(m for m in sent if m.get('method') == 'turn/start')['params']
        self.assertEqual((turn['threadId'], turn['input'][0]['text']), ('one', 'p' + E.AGAIN))
        named = next(m for m in sent if m.get('method') == 'thread/name/set')['params']
        self.assertEqual((named['threadId'], named['name']), ('one', E.TITLE))

        # The user opened it in the Codex App, which now owns it: the App runs the turn, in the same thread.
        owner['one'] = 'thread one already has an active writer'
        sent.clear()
        with patch.object(E, 'QUEUE_POLL_SECONDS', 0.01):
            result = go()
        self.assertEqual((result['thread'], result['via'], result['turn']), ('one', 'app', 'completed'))
        self.assertEqual(queued[-1][:2], ('one', 'p' + E.AGAIN))
        self.assertFalse(any(m.get('method') in ('thread/start', 'turn/start') for m in sent))
        self.assertIn('RG-DECISION', result['said'])
        self.assertEqual(json.loads((self.folder / E.SESSION).read_text())['thread'], 'one')

        owner['no-queue'] = True   # a Codex without `codex queue`: a new thread rather than no run at all
        self.assertEqual(go()['thread'], 'two')
        self.assertEqual(E.conversation(self.home, 'codex')['thread'], 'two')
        owner['two'] = 'no rollout found for thread id two'   # the user deleted it: a new one is kept
        self.assertEqual(go()['thread'], 'three')
        self.assertEqual(E.conversation(self.home, 'codex')['thread'], 'three')

    def test_a_decision_the_sandbox_would_not_take_comes_back_in_the_reply(self):
        material = dict(orphans=[], tasks=[], simulators=[], waiting=[])
        (self.folder / E.MATERIAL).write_text(json.dumps(material))
        self.folder.chmod(0o500)   # the Codex App ran the turn read-only
        try:
            with self.assertRaises(ValueError) as refused:
                E.decide(self.folder, 'lesson', when='只读的对话里记决定', then='写不进去就放进回复，资源看板会取')
        finally:
            self.folder.chmod(0o700)
        line = str(refused.exception).splitlines()[-1]
        self.assertTrue(line.startswith(E.CARRY))
        said = '处理完了。\n\n```\n' + line + '\n' + line + '\n```\nRG-DECISION {"action": "stop", "pid": 1, "why": "x"}'
        carried = E.carry(self.folder, said)
        self.assertEqual(carried['received'], 3)
        self.assertEqual((carried['recorded'][0]['action'], carried['recorded'][0]['when']), ('lesson', '只读的对话里记决定'))
        [refused] = carried['refused']   # the stop is checked again: pid 1 was never in the material
        self.assertIn('不在告急材料里', refused['why'])
        again = E.carry(self.folder, said)
        self.assertEqual((again['received'], len(again['refused'])), (3, 1))
        self.assertEqual([d['action'] for d in E.decisions(self.folder)], ['lesson'])   # said twice, kept once
    def test_an_older_codex_without_thread_source_still_runs(self):
        starts = []

        class Pipe:
            def __init__(self):
                self.queue = []
            def write(self, text):
                message = json.loads(text)
                if message.get('method') == 'thread/start':
                    starts.append(message['params'])
                    reply = ({'id': message['id'], 'error': {'message': 'unknown field threadSource'}} if 'threadSource' in message['params']
                             else {'id': message['id'], 'result': {'thread': {'id': 't2'}}})
                    self.queue.append(reply)
                elif message.get('method') == 'turn/start':
                    self.queue.append({'method': 'turn/completed', 'params': {'threadId': 't2', 'turn': {'status': 'completed'}}})
                elif 'id' in message:
                    self.queue.append({'id': message['id'], 'result': {}})
            def flush(self):
                pass

        class Process:
            pid = None
            def __init__(self, command, **kwargs):
                self.stdin = Pipe(); self.stdout = self
            def __iter__(self):
                return self
            def __next__(self):
                if not self.stdin.queue:
                    raise StopIteration
                return json.dumps(self.stdin.queue.pop(0)) + '\n'
            def wait(self, timeout=None):
                return 0

        result = E.run_codex(self.home, self.folder, 'p', {}, 60, popen=Process)
        self.assertEqual((result['status'], result['thread']), ('done', 't2'))
        self.assertEqual(['threadSource' in p for p in starts], [True, False])

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
            987: dict(pid=987, ppid=1, age=139 * 3600, cpu=900.0, rss=2 ** 20, lstart='z',
                      command='python3 /Users/x/.claude/skills/test-journal/scripts/archive_queue.py --watch'),
        }
        cwd = {500: str(task / 'main'), 600: '/', 700: str(task), 800: str(task), 900: '/', 987: str(task)}
        activity = Activity(open_paths=[(500, str(task / 'main'))], commands=[(500, 'node /x/app-server-broker.mjs serve')])
        drive = self.home / 'drive'; drive.mkdir()
        material = E.collect(self.home, self.cfg(external_root=str(drive)), self.settings, 'disk', NOW, activity=activity,
                             simctl=simctl, table=table, cwd_of=lambda pids: {p: cwd[p] for p in pids if p in cwd},
                             prs=lambda repo: '#12 MERGED', report={'tasks': [{'path': str(task), 'bytes': 3 * GIB}]}, free=4 * GIB,
                             managed={987})   # a launchd job: a service someone set up, not a leftover
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
        self.assertTrue(any(m['words'] == '开着但 3 小时没人连的，不要关机。' for m in material['decisions']))
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
        self.assertIn('还在干活', E.stop_orphan(entry, self.table(cpu=60.0), kill, managed=set()))
        self.assertIn('launchd', E.stop_orphan(entry, self.table(), kill, managed={999991}))
        self.assertEqual(kills, [])
        self.assertIsNone(E.stop_orphan(entry, self.table(), kill, sleep=lambda s: None, managed=set()))
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

    def test_a_move_this_run_may_not_make_waits_for_the_wake_that_may(self):
        """agent-investigates AC-09, 11-04: the run the per-minute memory job started could not reach the drive, and
        the agent's move was dropped. It now waits for the App's own wake, rechecked there."""
        task = self.worktree_task('done-task', 0)
        folder = E.new_folder(self.home, NOW)
        (folder / E.MATERIAL).write_text(json.dumps(dict(at=NOW, orphans=[], companions=[], simulators=[], waiting=[], who=[],
                                                         tasks=[dict(path=str(task), on_drive=False, gib=1)])))
        E.decide(folder, 'move', str(task), why='PR 已合入，改动都推了')
        with patch.object(auto_tier, 'external_access', return_value=auto_tier.DENIED):
            result = E.apply(self.home, self.cfg(), self.settings, folder, now=NOW, table={}, activity_factory=Activity)
        [done] = result['done']
        self.assertEqual(done['result'], E.HANDED_WORDS)
        self.assertFalse(task.is_symlink())
        self.assertIn('1 条交给资源看板自己去挪', E.receipt(E.decisions(folder), dict(received=0, recorded=[], refused=[]), result)['words'])
        self.assertIn('1 个闲置任务交给资源看板自己挪到外接盘', E.notice_words(result, dict(status='done')))
        # The App's wake: refused there too, it stays; allowed, it moves and says so.
        with patch.object(auto_tier, 'external_access', return_value=auto_tier.DENIED):
            self.assertEqual(E.handed_moves(self.home, self.cfg(), self.settings, NOW + 300, activity_factory=Activity), [])
        before = len(agent_plan.queued_notices(self.home))
        [moved] = E.handed_moves(self.home, self.cfg(), self.settings, NOW + 600, activity_factory=Activity)
        self.assertEqual(moved['result'], '已挪到外接盘')
        self.assertTrue(task.is_symlink())
        [notice] = agent_plan.queued_notices(self.home)[before:]
        self.assertEqual(notice['title'], 'agent 定的 1 个闲置任务挪到了外接盘')
        self.assertIsNone(E.handed_moves(self.home, self.cfg(), self.settings, NOW + 900, activity_factory=Activity))
        # Nobody could make it for a day: it is dropped, not tried forever.
        E.hand_over(self.home, dict(path=str(self.worktree_task('late-task', 0)), why='x'), NOW)
        self.assertEqual(E.handed_moves(self.home, self.cfg(), self.settings, NOW + 25 * 3600, activity_factory=Activity), [])
        self.assertEqual(E.read_list(E.state_root(self.home) / E.HANDED), [])

    def asked_run(self, *asks, status='done', now=NOW):
        task = self.worktree_task('merged-task', 0)
        folder = E.new_folder(self.home, now)
        self.simctl = self.sims()
        material = E.collect(self.home, self.cfg(), self.settings, 'disk', now, activity=Activity(), simctl=self.simctl,
                             table={}, cwd_of=lambda p: {}, prs=lambda r: '#3 MERGED', sizer=lambda p: 2 * GIB, free=8 * GIB)
        (folder / E.MATERIAL).write_text(json.dumps(material))
        for target in asks:
            E.decide(folder, 'ask', target.format(task=task), why='PR #3 已合入，改动都推了')
        E.decide(folder, 'done', summary='列了两项等你确认。', cause='两台模拟器和一个闲置任务占着空间')
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

    def test_still_short_with_nothing_listed_the_unused_simulators_are_asked_about(self):
        simctl = self.sims()
        for device in simctl.devices[self.IOS]:
            device['dataPathSize'] = 3 * GIB
        self.settings.warning_free_gib = 10 ** 6   # far below the target, whatever this disk has
        with patch.object(self, 'sims', return_value=simctl):
            self.asked_run()
        pending = ask_tier.load(self.home, ask_tier.PENDING, ask_tier.empty_pending())
        mine = {i['id']: i for i in pending['items']}
        self.assertEqual(set(mine), {'simulator-delete:OLD', 'simulator-delete:NEW'})   # never the one that is on
        self.assertTrue(all(i['by'] == 'agent' and i['reason'].startswith('后台补列：关机') for i in mine.values()))
        [notice] = agent_plan.queued_notices(self.home)
        self.assertEqual(notice['tab'], 'pending')
        self.assertIn('还有 2 项等你确认，约 6 GB', notice['body'])
        # The user's yes runs it and is kept as one of their decisions.
        [done] = ask_tier.execute(self.home, self.cfg(), self.settings, ['simulator-delete:NEW'], simctl, activity=Activity())
        self.assertEqual(done['outcome'], '已完成')
        self.assertIn(['delete', 'NEW'], simctl.calls)
        [memory] = agent_plan.memories(self.home, about='simulator-delete')
        self.assertEqual((memory['decision'], memory['path']), ('confirmed', 'NEW'))

    def test_nothing_is_added_when_the_disk_is_fine_or_the_agent_listed_something(self):
        simctl = self.sims()
        for device in simctl.devices[self.IOS]:
            device['dataPathSize'] = 3 * GIB
        self.settings.warning_free_gib = 0
        with patch.object(self, 'sims', return_value=simctl):
            self.asked_run()
        self.assertEqual(ask_tier.load(self.home, ask_tier.PENDING, ask_tier.empty_pending())['items'], [])
        self.settings.warning_free_gib = 10 ** 6
        with patch.object(self, 'sims', return_value=simctl), patch.object(self, 'worktree_task', return_value=self.root / 'merged-task'):
            self.asked_run('simulator-delete:OLD', now=NOW + 60)
        self.assertEqual(self.ids(ask_tier.load(self.home, ask_tier.PENDING, ask_tier.empty_pending())), ['simulator-delete:OLD'])

    def test_agent_asks_survive_the_next_refresh(self):
        now = time.time()
        self.asked_run('simulator-delete:NEW', now=now)
        simctl = self.simctl
        pending = self.refresh(simctl, now=now + 3600)
        self.assertIn('simulator-delete:NEW', self.ids(pending))      # the rules would not list a sim used yesterday
        with patch.object(ask_tier, 'last_used', return_value=now):   # still in use by the rules' measure
            pending = self.refresh(simctl, now=now + (ask_tier.AGENT_ASK_DAYS + 1) * DAY)
        self.assertNotIn('simulator-delete:NEW', self.ids(pending))   # nobody answered for days: it leaves


def proc(pid, command, ppid=1, hours=30, cpu=10.0, pcpu=0.0, mb=100, lstart='Thu Oct 29 13:22:55 2026'):
    return dict(pid=pid, ppid=ppid, age=hours * 3600, cpu=cpu, pcpu=pcpu, rss=mb * 2 ** 20, lstart=lstart, command=command)


class InvestigateTest(AskFixture):
    """agent-investigates: the agent answers why first, can act on what it finds, and the background has the last word."""
    TABLE = {10: proc(10, '/System/Applications/Utilities/Terminal.app/Contents/MacOS/Terminal', hours=80),
             13: proc(13, '/opt/homebrew/bin/claude --resume abc', ppid=10, hours=80),
             20: proc(20, '/Applications/Cursor.app/Contents/MacOS/Cursor', hours=80, mb=900),
             61: proc(61, 'sim-mirror stream --udid 4E5D --fps 1', ppid=13, hours=72, cpu=72 * 3600 * 0.99, pcpu=99.0, mb=12),
             62: proc(62, 'tail -f /tmp/web.log', ppid=20, hours=50, mb=4),
             101: proc(101, 'node /code/shop/server.js', ppid=13, mb=1500),
             700: proc(700, 'postgres -D data', mb=300)}
    WHERE = {13: ('/code/shop', False), 61: ('/code/shop', False), 62: ('/code/shop', False), 101: ('/code/shop', False)}

    def setUp(self):
        super().setUp()
        for name, value in (('launch_app', None),):
            stub = patch.object(ask_tier, name); stub.start(); self.addCleanup(stub.stop)

    def watch(self, pressures, now=NOW, **more):
        samples = [dict(at=now - (len(pressures) - i) * 60, pressure=p, swap_used=8.0 + i * 0.1, swap_total=17.0)
                   for i, p in enumerate(pressures)]
        path = E.state_root(self.home) / 'watch.json'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(dict(samples=samples, **more)))

    def material(self, now=NOW):
        with patch.object(O, 'looks', return_value=self.WHERE):
            return E.collect(self.home, self.cfg(), self.settings, 'memory', now, activity=Activity(), simctl=self.sims(),
                             table=self.TABLE, cwd_of=lambda p: {}, prs=lambda r: None, sizer=lambda p: 0, free=80 * GIB,
                             managed=set())

    def test_the_material_says_whose_it_is_how_the_hour_went_and_what_the_watch_did(self):
        self.watch([2] * 50 + [1] * 10, agents={'13': dict(lstart='a', cpu=1.0, at=NOW, moved=NOW - 7 * 3600)},
                   acted=[dict(at=NOW - 600, stopped=1, paused=2, names=['tail', 'tsc', 'tsc'])], paused=[dict(name='tsc')])
        material = self.material()
        json.dumps(material)
        lines = [w['line'] for w in material['who']]
        self.assertIn('node server.js · shop · Claude Code 起的 · 7 小时没动', lines)
        self.assertIn('sim-mirror · shop · Claude Code 起的 · 7 小时没动 · 占满 1 个核已经 3 天', lines)
        mates = {m['pid']: m for m in material['companions']}
        self.assertTrue(mates[61]['may_stop'])
        self.assertIn('Claude Code 已经 7 小时没动', mates[61]['why'])
        self.assertFalse(mates[62]['may_stop'])                       # Cursor is open: someone may be watching
        self.assertNotIn(700, mates)                                   # a database is never anyone's to stop
        trend = material['trend']
        self.assertIn('「警告」50 分钟', trend['words'])
        self.assertTrue(trend['calm'])
        self.assertEqual((trend['swap_from'], trend['swap_to']), (8.0, 13.9))
        self.assertEqual(material['watch_did'], dict(stopped=1, paused=2, names=['tail', 'tsc', 'tsc'], queued=['tsc']))
        # 11-10: the goal and the raw facts, not a rulebook of how to look.
        self.assertIn('你自己判断', material['steps'][0])
        self.assertIn('ps.txt', material['steps'][1])
        self.assertTrue(any('path-delete' in step for step in material['steps']))

    def test_show_says_when_the_material_was_made_and_whether_it_is_this_runs(self):
        folder = E.new_folder(self.home, NOW)
        (folder / E.MATERIAL).write_text(json.dumps(dict(at=NOW - 180, reason='memory')))
        shown = E.show(folder, self.home, now=NOW)
        self.assertIn('生成的（3 分钟前）', shown['generated'])
        self.assertEqual(shown['reason'], 'memory')
        E.save_state(self.home, dict(run=dict(folder=str(self.home / 'elsewhere'))))
        self.assertIn('这不是这次的材料', E.show(folder, self.home, now=NOW)['generated'])

    def test_the_agent_can_stop_what_may_go_ask_for_the_rest_and_must_say_why_it_was_tight(self):
        folder = E.new_folder(self.home, NOW)
        (folder / E.MATERIAL).write_text(json.dumps(dict(at=NOW, orphans=[], tasks=[], simulators=[], waiting=[], companions=[
            dict(pid=61, lstart='Thu Oct 29 13:22:55 2026', may_stop=True), dict(pid=62, lstart='Thu Oct 29 13:22:55 2026', may_stop=False)])))
        self.assertTrue(E.decide(folder, 'stop', '61', why='Claude Code 7 小时没动')['companion'])
        with self.assertRaises(ValueError) as owner_there:
            E.decide(folder, 'stop', '62', why='开了两天')
        self.assertIn('process-stop:62', str(owner_there.exception))
        with patch.object(E, 'process_table', return_value=self.TABLE), patch.object(E, 'launchd_jobs', return_value=set()), \
                patch.object(O, 'looks', return_value=self.WHERE):
            asked = E.decide(folder, 'ask', 'process-stop:62', why='Cursor 开着，但这个 tail 开了两天没人看')
            self.assertEqual((asked['title'], asked['lstart']), ('tail（Cursor 开着，shop）', 'Thu Oct 29 13:22:55 2026'))
            for pid in ('20', '700'):
                with self.assertRaises(ValueError) as never:
                    E.decide(folder, 'ask', 'process-stop:' + pid, why='x')
                self.assertIn('不能列', str(never.exception))
        with self.assertRaises(ValueError) as no_cause:
            E.decide(folder, 'done', summary='关了一个流')
        self.assertIn('为什么紧', str(no_cause.exception))
        done = E.decide(folder, 'done', summary='关了一个流', cause='一路截屏流占满一个核', need='看一下 Cursor 里那个 tail')
        self.assertEqual((done['cause'], done['need']), ('一路截屏流占满一个核', '看一下 Cursor 里那个 tail'))

    def test_something_new_brings_it_back_sooner_and_it_is_told_what(self):
        executable(self.home / '.local/bin/codex')
        (self.home / '.codex').mkdir(); (self.home / '.codex/auth.json').write_text('{}')
        cfg = self.cfg()
        heavy = lambda tool, owner: dict(tool=tool, title=tool, owner=owner, projects=['shop'], bytes=2 * GIB, cores=0.5,
                                         fact=owner['name'] + ' 在用', busy_since=None, count=1, pids=[1])
        tsc = heavy('tsc', dict(kind='agent', name='Codex'))
        start = lambda now: E.tick(self.home, cfg, self.settings, now, run=FakeRun(pressure=2), env={},
                                   starter=lambda reason, folder, runner: 4243, free=80 * GIB)
        self.watch([2] * 20, who=dict(at=NOW, groups=[tsc]))
        E.save_state(self.home, dict(warn_since=NOW - 3600))
        self.assertEqual(start(NOW)['reason'], 'memory')
        later = NOW + 40 * 60
        self.watch([2] * 20, now=later, who=dict(at=later, groups=[tsc]))
        self.assertIsNone(start(later))                       # nothing new: it would only say the same again
        sim = heavy('iOS 模拟器 · iPhone 17 Pro', dict(kind='agent', name='Claude Code', via='uses'))
        self.watch([2] * 20, now=later, who=dict(at=later, groups=[tsc, sim]))
        got = start(later)
        self.assertEqual(got['reason'], 'memory')
        prompts = []

        def codex(home, folder, prompt, env, timeout, popen=None):
            prompts.append(prompt)
            return dict(agent='codex', status='done', thread='t', said='')

        (Path(got['folder']) / E.MATERIAL).write_text(json.dumps(dict(at=later, orphans=[], tasks=[], simulators=[], waiting=[], companions=[])))
        with patch.object(E, 'run_codex', codex):
            E.run(self.home, cfg, self.settings, 'memory', got['folder'], 'codex', now=later)
        self.assertIn('跟上次（{}）比，这次新出现的：iOS 模拟器 · iPhone 17 Pro · shop · Claude Code 在用。先看这些。'.format(
            time.strftime('%H:%M', time.localtime(NOW))), prompts[0])
        # However many runs a day: still tight and something new again, it comes again; counters older builds
        # kept are dropped, and no 「今天已经来了」 notice is sent (RAW-53).
        state = E.load_state(self.home)
        state.update(starts=[later - i * 60 for i in range(40)], capped=dict(at=later, reason='memory'), capped_told=0)
        state['run'] = {}
        E.save_state(self.home, state)
        before = len(agent_plan.queued_notices(self.home))
        again = later + 40 * 60
        tsc2 = dict(heavy('tsc', dict(kind='agent', name='Codex')), projects=['other-project'])
        self.watch([2] * 20, now=again, who=dict(at=again, groups=[tsc, sim, tsc2]))
        self.assertEqual(start(again)['reason'], 'memory')
        self.assertFalse({'starts', 'capped', 'capped_told'} & set(E.load_state(self.home)))
        self.assertEqual([n['title'] for n in agent_plan.queued_notices(self.home)][before:], [])

    def test_lessons_are_rules_kept_once_and_old_notes_can_be_rewritten(self):
        diary = agent_plan.remember(self.home, 'lesson', 'emergency', note='22:30 本轮无新增孤儿，40223 仍保留')
        dns = agent_plan.remember(self.home, 'lesson', 'emergency', note='clusterdns 是本机共用的 DNS，不能只凭父进程退出就关')
        material = self.material()
        self.assertEqual(([n['id'] for n in material['old_notes']], material['lessons']), ([diary['id'], dns['id']], []))
        folder = E.new_folder(self.home, NOW)
        (folder / E.MATERIAL).write_text(json.dumps(material))
        for refused in (dict(note='本轮 swap 18 GB'), dict(when='本轮内存紧', then='没关'), dict(when='内存紧', then='22:37 查过了'),
                        dict(when='内存紧', then='先看谁在占', replaces='m99')):
            with self.assertRaises(ValueError):
                E.decide(folder, 'lesson', **refused)
        E.decide(folder, 'lesson', when='孤儿列表里有 clusterdns 或 mirrord intproxy', then='保留：是本机共用的 DNS', replaces=dns['id'])
        E.decide(folder, 'lesson', when='截屏流开了一天以上、主人还在', then='列进「等你确认」，不自己关')
        E.decide(folder, 'done', summary='记了两条', cause='截屏流占着核')
        result = E.apply(self.home, self.cfg(), self.settings, folder, now=NOW, table=self.TABLE)
        receipt = E.receipt(E.decisions(folder), dict(received=0, recorded=[], refused=[]), result)
        self.assertEqual((receipt['words'], receipt['learned']), ('记下 2 条经验（1 条是改写旧的）', 2))
        self.assertEqual([r['rule']['when'] for r in agent_plan.lessons(self.home)],
                         ['孤儿列表里有 clusterdns 或 mirrord intproxy', '截屏流开了一天以上、主人还在'])
        self.assertEqual([n['id'] for n in agent_plan.old_notes(self.home)], [diary['id']])   # the rewritten note is gone
        # The same situation again rewrites its rule instead of adding a second one.
        again = agent_plan.learn(self.home, 'emergency', '截屏流开了一天以上、主人还在', '列进「等你确认」；主人 6 小时没动就关')
        self.assertEqual(again['replaces'], result['learned'][1]['id'])
        lessons = agent_plan.lessons(self.home)
        self.assertEqual((len(lessons), lessons[-1]['rule']['then']), (2, '列进「等你确认」；主人 6 小时没动就关'))

    def test_a_run_that_did_not_help_says_so_and_the_next_one_hears_it(self):
        executable(self.home / '.local/bin/codex')
        (self.home / '.codex').mkdir(); (self.home / '.codex/auth.json').write_text('{}')
        folder = E.new_folder(self.home, NOW)
        (folder / E.MATERIAL).write_text(json.dumps(dict(at=NOW, orphans=[], tasks=[], simulators=[], waiting=[], companions=[
            dict(pid=61, lstart='Thu Oct 29 13:22:55 2026', may_stop=True)])))
        self.watch([2] * 10)                                          # still 「警告」 when the agent is done
        said = '\n'.join(['RG-DECISION {"action": "stop", "pid": 61, "why": "Claude Code 7 小时没动"}',
                          'RG-DECISION {"action": "stop", "pid": 99, "why": "x"}',
                          'RG-DECISION {"action": "done", "summary": "关了一路截屏流", "cause": "截屏流占满一个核"}'])
        prompts = []

        def codex(home, folder, prompt, env, timeout, popen=None):
            prompts.append(prompt)
            return dict(agent='codex', status='done', thread='t', said=said)

        cfg = self.cfg()
        with patch.object(E, 'run_codex', codex), patch.object(E, 'stop_companion', return_value=None):
            E.run(self.home, cfg, self.settings, 'memory', folder, 'codex', now=NOW)
            result = json.loads((folder / E.RESULT).read_text())
            self.assertIn('从回复里收到 3 条，记上 2 条（没记上：99 不在告急材料里能直接关的', result['receipt']['words'])
            self.assertIn('交回 1 条决定，照做 1 条', result['receipt']['words'])
            self.assertEqual(result['judged'], dict(tight=True, words='后台看：最近 10 分钟有 10 分钟「警告」或「严重」，还紧'))
            last = E.load_state(self.home)['last']
            self.assertEqual((last['tight'], last['streak'], last['cause']), (True, 1, '截屏流占满一个核'))
            [notice] = agent_plan.queued_notices(self.home)
            self.assertTrue(notice['body'].startswith('后台看：最近 10 分钟有 10 分钟'))
            # Six hours on it is still tight: the next run is the second in a row, and both the agent and the user hear it.
            later = NOW + E.MEMORY_QUIET_HOURS * 3600 + 60
            state = E.load_state(self.home)
            state['warn_since'] = later - 3600
            state['last']['at'] = NOW                                   # the run above, on this test's clock
            E.save_state(self.home, state)
            got = E.tick(self.home, cfg, self.settings, later, run=FakeRun(pressure=2), env={},
                         starter=lambda reason, folder, runner: 4243, free=80 * GIB)
            self.assertEqual(got['reason'], 'memory')
            self.assertEqual(E.load_state(self.home)['run']['streak'], 2)
            second = Path(got['folder'])
            (second / E.MATERIAL).write_text(json.dumps(dict(at=later, orphans=[], tasks=[], simulators=[], waiting=[], companions=[])))
            self.watch([2] * 10, now=later)
            E.run(self.home, cfg, self.settings, 'memory', second, 'codex', now=later)
        self.assertTrue(prompts[1].startswith('上次（'))
        self.assertIn('交回 1 条决定，照做 1 条', prompts[1])
        self.assertIn('这是连续第 2 次因为内存吃紧叫你', prompts[1])
        titles = [n['title'] for n in agent_plan.queued_notices(self.home)]
        self.assertIn('agent 处理了 2 次还没缓过来', titles)

    def test_the_background_judges_by_the_last_ten_minutes_not_by_one(self):
        calm = [dict(at=NOW - (10 - i) * 60, pressure=1, swap_used=8.0, swap_total=17.0) for i in range(10)]
        self.assertEqual(E.judge(self.home, 'memory', self.settings, NOW, samples=calm),
                         dict(tight=False, words='后台看：最近 10 分钟都正常'))
        flicker = calm[:-1] + [dict(calm[-1], pressure=2)]
        self.assertTrue(E.judge(self.home, 'memory', self.settings, NOW, samples=flicker)['tight'])
        short = self.settings.warning_free_gib - 2
        self.assertEqual(E.judge(self.home, 'disk', self.settings, NOW, free=short * GIB, swap=dict(used_gib=1, total_gib=2)),
                         dict(tight=True, words='后台看：内置盘还剩 {:.0f} GB，比目标少 2 GB'.format(short)))
        # 11-10: 24 GB of the disk was swap and no notice said so.
        self.assertIn('（其中 24 GB 是交换文件：内存不够时系统写在磁盘上的，内存松下来会还回来）',
                      E.judge(self.home, 'disk', self.settings, NOW, free=short * GIB, swap=dict(used_gib=23, total_gib=24))['words'])
        self.assertNotIn('交换文件', E.judge(self.home, 'disk', self.settings, NOW, free=(short + 9) * GIB,
                                         swap=dict(used_gib=23, total_gib=24))['words'])   # disk fine: not worth saying


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


class ProcessStopTest(AskFixture):
    """agent-investigates AC-03: a process whose owner is still around goes only on the user's click, rechecked."""
    def ask(self, child, lstart=None):
        lstart = lstart or E.process_table()[child.pid]['lstart']
        key = 'process-stop:{}'.format(child.pid)
        E.add_asks(self.home, [dict(id=key, kind='process-stop', title='sleep（Terminal 开着）', bytes=GIB, why='证据', lstart=lstart)], time.time())
        return key

    def test_confirmed_it_stops_and_a_changed_one_is_left_alone(self):
        child = subprocess.Popen(['/bin/sleep', '300'])
        self.addCleanup(child.kill)
        key, sims = self.ask(child), self.sims()
        [item] = ask_tier.load(self.home, ask_tier.PENDING, ask_tier.empty_pending())['items']
        self.assertEqual((item['loses'], item['lstart']), (ask_tier.LOSES['process-stop'], E.process_table()[child.pid]['lstart']))
        [done] = ask_tier.execute(self.home, self.cfg(), self.settings, [key], sims, activity=Activity())
        self.assertEqual(done['outcome'], '已完成')
        self.assertIsNotNone(child.wait(timeout=10))
        other = subprocess.Popen(['/bin/sleep', '300'])
        self.addCleanup(other.kill)
        stale = self.ask(other, lstart='Mon Jan  1 00:00:00 2001')   # the pid now names another program
        [skipped] = ask_tier.execute(self.home, self.cfg(), self.settings, [stale], sims, activity=Activity())
        self.assertEqual(skipped['outcome'], '已跳过')
        self.assertIsNone(other.poll())


class SandboxedAskTest(AskFixture):
    """agent-investigates AC-10, 11-04: in the Codex sandbox ps is refused, and the agent could not list a process for
    the user twice. It lists what its material saw; the background checks it before it reaches 「等你确认」."""
    def test_a_process_the_material_saw_is_listed_and_settled_where_ps_runs(self):
        child = subprocess.Popen(['/bin/sleep', '300'])
        self.addCleanup(child.kill)
        folder = E.new_folder(self.home, time.time())
        who = [dict(line='sleep · shop · Terminal 开着', pids=[child.pid], gib=0.0, owner=dict(kind='terminal', name='Terminal')),
               dict(line='Claude · App 本身', pids=[4242], gib=2.6, owner=dict(kind='app', name='Claude', self=True)),
               dict(line='iOS 模拟器 · iPhone 17 Pro · Claude 在用', pids=[4343], gib=1.3, owner=dict(kind='app', name='Claude'))]
        (folder / E.MATERIAL).write_text(json.dumps(dict(at=time.time() + 5, orphans=[], companions=[], simulators=[], tasks=[],
                                                         waiting=[], who=who)))
        with patch.object(E, 'process_table', side_effect=PermissionError('ps')):
            asked = E.decide(folder, 'ask', 'process-stop:{}'.format(child.pid), why='开了两天没人看')
            self.assertEqual((asked['title'], asked['lstart']), ('sleep · shop · Terminal 开着', None))
            for pid in (4242, 4343, 5555):
                with self.assertRaises(ValueError):
                    E.decide(folder, 'ask', 'process-stop:{}'.format(pid), why='x')
        [settled] = E.settle_asks(self.home, [asked])
        self.assertEqual(settled['lstart'], E.process_table()[child.pid]['lstart'])
        late = dict(asked, seen_at=time.time() - 3600)   # this pid started after the agent looked: another process
        self.assertEqual(E.settle_asks(self.home, [late]), [])
        gone = dict(asked, pid=999999)
        self.assertEqual(E.settle_asks(self.home, [gone]), [])


class CommandTest(AskFixture):
    def test_the_agent_can_only_decide_about_what_it_was_shown(self):
        folder = E.new_folder(self.home, NOW)
        material = E.collect(self.home, self.cfg(), self.settings, 'memory', NOW, activity=Activity(), simctl=self.sims(),
                             table={}, cwd_of=lambda p: {}, prs=lambda r: None, sizer=lambda p: GIB, free=80 * GIB)
        (folder / E.MATERIAL).write_text(json.dumps(material))
        self.assertEqual(E.run_folder(cwd=str(folder), env={}, home=self.home), folder)
        with self.assertRaises(ValueError):
            E.run_folder(cwd=str(self.home), env={}, home=self.home)
        E.save_state(self.home, {'run': {'folder': str(folder)}})   # the agent's shell dropped the variable (CH-07)
        self.assertEqual(E.run_folder(cwd=str(self.home), env={}, home=self.home), folder)
        E.save_state(self.home, {})
        for args in (('stop', '12345', '没人用'), ('move', '/nowhere', '闲置'), ('ask', 'trash-empty:/x', '满了'),
                     ('ask', 'simulator-delete:NOPE', '旧'), ('ask', 'simulator-delete:OLD', '')):
            with self.assertRaises(ValueError):
                E.decide(folder, *args[:2], why=args[2])
        with self.assertRaises(ValueError):
            E.decide(folder, 'done')
        E.decide(folder, 'lesson', when='Codex 的 app-server 留在任务目录里', then='放满一天、所属任务的 PR 合了再关')
        self.assertEqual([d['action'] for d in E.decisions(folder)], ['lesson'])



class EntryTest(AskFixture):
    """AC-11/12: the panel opens the agent's conversation; Claude needs one sign-in, offered once a day."""
    def setUp(self):
        super().setUp()
        self.opened = []
        self.run = lambda args, **kwargs: self.opened.append(args) or subprocess.CompletedProcess(args, 0, '', '')
        which = patch.object(E.shutil, 'which', return_value=None); which.start(); self.addCleanup(which.stop)
        launch = patch.object(ask_tier, 'launch_app'); launch.start(); self.addCleanup(launch.stop)

    def finished(self, **session):
        folder = E.new_folder(self.home, NOW)
        E.note_session(folder, **session)
        E.save_state(self.home, {'last': {'at': NOW, 'folder': str(folder), 'agent': 'done', 'talk': True}})
        return folder

    def test_codex_opens_in_its_app_and_claude_continues_in_terminal(self):
        self.assertEqual(E.open_session(self.home, NOW, run=self.run)['status'], 'none')
        self.finished(agent='codex', thread='t9')
        self.assertEqual(E.open_session(self.home, NOW, run=self.run)['status'], 'ok')
        self.assertEqual(self.opened[-1], ['/usr/bin/open', 'codex://threads/t9'])
        folder = self.finished(agent='claude', session='s-1', binary='/apps/claude code/claude', cwd='/runs/a b')
        self.assertEqual(E.open_session(self.home, NOW, run=self.run)['status'], 'ok')
        self.assertEqual(self.opened[-1][:3], ['/usr/bin/open', '-a', 'Terminal'])
        script = Path(self.opened[-1][3]).read_text()
        self.assertIn("cd '/runs/a b'", script)
        self.assertIn("exec '/apps/claude code/claude' --resume s-1", script)
        self.assertTrue(os.access(self.opened[-1][3], os.X_OK))
        E.save_state(self.home, dict(E.load_state(self.home), run={'pid': os.getpid(), 'started': NOW, 'folder': str(folder)}))
        self.assertEqual(E.open_session(self.home, NOW + 60, run=self.run)['status'], 'busy')   # Claude still at it

    def test_one_sign_in_to_claude_code_is_offered_once_a_day(self):
        executable(self.home / 'Library/Application Support/Claude/claude-code/2.1.309/abc/claude.app/Contents/MacOS/claude')
        cfg = self.cfg()
        self.assertEqual(E.readiness(self.home, cfg, env={}, run=FakeRun())['missing'], 'claude-login')
        self.assertTrue(E.readiness(self.home, cfg, env={}, run=FakeRun(keychain=True))['ready'])
        reply = E.login(self.home, run=self.run)
        self.assertEqual(reply['status'], 'ok')
        self.assertIn('auth login', Path(self.opened[-1][3]).read_text())
        for minutes in (0, 10):
            E.tick(self.home, cfg, self.settings, NOW + minutes * 60, run=FakeRun(), env={}, starter=None, free=(20 - minutes) * GIB)
        hints = [n for n in agent_plan.queued_notices(self.home) if n['tab'] == 'login']
        self.assertEqual(len(hints), 1)
        self.assertIn('登录一次 Claude Code', hints[0]['body'])
        self.assertEqual(E.load_state(self.home)['missed']['missing'], 'claude-login')

    def test_the_notice_opens_the_conversation_and_offers_the_list(self):
        folder = E.new_folder(self.home, NOW)
        material = E.collect(self.home, self.cfg(), self.settings, 'disk', NOW, activity=Activity(), simctl=self.sims(),
                             table={}, cwd_of=lambda p: {}, prs=lambda r: None, report={'tasks': []}, free=8 * GIB)
        (folder / E.MATERIAL).write_text(json.dumps(material))
        E.decide(folder, 'ask', 'simulator-delete:OLD', why='关机 10 天')
        def codex(home, folder, *a, **k):
            E.note_session(folder, agent='codex', thread='t3')
            return dict(agent='codex', status='done', thread='t3')
        with patch.object(E, 'run_codex', side_effect=codex):
            E.run(self.home, self.cfg(), self.settings, 'disk', folder, 'codex', now=NOW)
        [notice] = agent_plan.queued_notices(self.home)
        self.assertEqual(notice['tab'], 'emergency-ask')
        last = E.load_state(self.home)['last']
        self.assertEqual((last['runner'], last['asks'], last['talk']), ('codex', 1, True))
        self.assertIn('还有 1 项等你确认', last['brief'])



class SlowMacTest(AskFixture):
    """10-31 18:56: the first run the agent started by itself died on a 60-second `git log` while memory was critical."""
    def collect(self, task, **kwargs):
        return E.collect(self.home, self.cfg(), self.settings, 'memory', NOW, activity=Activity(), simctl=self.sims(),
                         table={}, cwd_of=lambda p: {}, prs=lambda r: None, report={'tasks': [{'path': str(task), 'bytes': GIB}]},
                         free=60 * GIB, **kwargs)

    def test_a_repository_too_slow_to_read_reads_as_unknown_not_a_crash(self):
        task = self.worktree_task('slow-task', 0)
        def stuck(repo, *args, timeout=60):
            raise subprocess.TimeoutExpired(['git'] + list(args), timeout)
        with patch.object(E.T, 'git', side_effect=stuck):
            material = self.collect(task)
        [entry] = [t for t in material['tasks'] if t['path'] == str(task)]
        [repo] = entry['repos']
        self.assertIn('读不出状态', repo['unsaved'])     # unknown counts as unsaved: nothing is moved or deleted on it
        self.assertIsNotNone(entry['idle_days'])         # the folder's own dates still say roughly when
        json.dumps(material)

    def test_out_of_time_the_rest_is_marked_not_looked_at(self):
        task = self.worktree_task('late-task', 0)
        material = self.collect(task, budget=0)
        [entry] = [t for t in material['tasks'] if t['path'] == str(task)]
        self.assertIn('没来得及查', entry['repos'][0]['unsaved'])

    def test_no_process_list_means_no_leftovers_listed(self):
        task = self.worktree_task('any-task', 0)
        with patch.object(E, 'orphans', side_effect=subprocess.TimeoutExpired(['ps'], 20)):
            material = self.collect(task)
        self.assertEqual(material['orphans'], [])

    def test_a_run_the_user_asked_for_leaves_the_quiet_hours_alone(self):
        state = {}
        E.mark_fired(state, 'asked', 20 * GIB, CRITICAL, NOW)
        self.assertEqual(state, {})
        E.mark_fired(state, 'memory', 60 * GIB, CRITICAL, NOW)
        self.assertEqual(state, {'memory_fired_at': NOW})


class WakeOrderTest(AskFixture):
    def test_the_emergency_check_comes_first_even_while_a_slow_wake_holds_the_lock(self):
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
        import service
        with patch.object(service, 'emergency_tick', return_value={'reason': 'memory'}) as check, \
                patch.object(service, 'auto_update', return_value='skipped'):
            with service.operation_lock(self.home, 'periodic-storage'):   # a slow wake is still going
                result = service.tick_storage(self.home, NOW)
            self.assertEqual((result['status'], result['emergency']), ('busy', {'reason': 'memory'}))
            check.assert_called_once()
            (service.state_root(self.home) / 'storage-paused').touch()
            service.tick_storage(self.home, NOW)
            check.assert_called_once()   # paused maintenance pauses this too


if __name__ == '__main__':
    unittest.main()
