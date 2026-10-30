"""agent-plan epic: the plan as rules the agent changes, the memory it learns from, and the user's locks."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_ask_tier import AskFixture
from test_tiers import ROOT, TierFixture
import agent_plan
import ask_tier
import plan as space_plan
from storage import GIB, Activity


class RulesTest(AskFixture):
    def test_defaults_are_todays_behaviour(self):
        plan = agent_plan.load(self.home)
        self.assertEqual({rid: rule['action'] for rid, rule in plan['rules'].items()}, {
            'tool-caches': 'move', 'task-builds': 'move', 'xcode-intermediates': 'clean', 'idle-tasks': 'move',
            'simulator-shutdown': 'ask', 'simulator-delete': 'ask', 'runtime-delete': 'ask', 'task-delete': 'ask',
            'installer-delete': 'ask', 'trash-empty': 'ask', 'device-support-delete': 'ask', 'archive-delete': 'ask'})
        self.assertTrue(all(rule['set_by'] == 'default' and not rule['locked'] for rule in plan['rules'].values()))
        agent_plan.plan_path(self.home).parent.mkdir(parents=True, exist_ok=True)
        agent_plan.plan_path(self.home).write_text('{broken')
        self.assertEqual(agent_plan.load(self.home)['rules']['idle-tasks']['action'], 'move', 'a damaged plan means defaults')

    def test_the_safety_floor_cannot_be_crossed(self):
        for rid, wanted in (('task-delete', 'clean'), ('idle-tasks', 'clean'), ('simulator-delete', 'move'), ('trash-empty', 'clean')):
            with self.assertRaises(ValueError, msg=rid) as caught:
                agent_plan.set_rule(self.home, rid, wanted, by='user', why='想省事')
            self.assertIn('安全底线', str(caught.exception))
        with self.assertRaises(ValueError):
            agent_plan.set_rule(self.home, 'no-such-rule', 'off', why='x')

    def test_agent_changes_need_a_reason_are_logged_and_said_once(self):
        with self.assertRaises(ValueError):
            agent_plan.set_rule(self.home, 'simulator-delete', 'quiet', why='  ')
        mine = agent_plan.remember(self.home, 'decision', 'simulator-delete', decision='ignored', note='忽略删 iPhone 15')
        agent_plan.set_rule(self.home, 'simulator-delete', 'quiet', why='你拒绝过 3 次删模拟器', evidence=[mine['id']])
        rule = agent_plan.load(self.home)['rules']['simulator-delete']
        self.assertEqual((rule['action'], rule['set_by'], rule['evidence']), ('quiet', 'agent', ['m1']))
        [change] = agent_plan.changes(self.home)
        self.assertEqual((change['by'], change['before'], change['after']), ('agent', '先问', '只列出、不催'))
        asked = json.loads((agent_plan.state_dir(self.home) / agent_plan.NOTICE).read_text())
        self.assertEqual(asked['title'], '资源看板的计划改了')
        self.assertIn('不可用或很久没开的模拟器：先问 → 只列出、不催。你拒绝过 3 次删模拟器', asked['body'])

    def test_a_lock_stops_the_agent_not_the_user(self):
        agent_plan.set_lock(self.home, 'idle-tasks', True, words='任务别挪，我要在本机对比')
        with self.assertRaises(ValueError) as caught:
            agent_plan.set_rule(self.home, 'idle-tasks', 'off', why='空间够了')
        self.assertIn('锁住', str(caught.exception))
        agent_plan.set_rule(self.home, 'idle-tasks', 'off', by='user')
        agent_plan.set_lock(self.home, 'target', True)
        with self.assertRaises(ValueError):
            agent_plan.check_setting(self.home, 'target', '过去两周 12 次低于目标')
        [first, second] = agent_plan.memories(self.home)
        self.assertEqual((first['decision'], first['words']), ('locked', '任务别挪，我要在本机对比'))
        self.assertEqual(second['about'], 'target')
        self.assertFalse((agent_plan.state_dir(self.home) / agent_plan.NOTICE).exists(), 'the user is not told of their own change')


class MemoryTest(AskFixture):
    def test_decisions_and_lessons_only_with_words_or_a_note(self):
        lesson = agent_plan.remember(self.home, 'lesson', 'task-builds', note='每轮测试都会编一份 Swift 包，可以挪')
        said = agent_plan.remember(self.home, 'decision', 'keep', words='generic-result 那个任务的构建产物别动', source='chat')
        self.assertEqual((lesson['id'], said['id']), ('m1', 'm2'))
        for kind, about, note in (('action', 'x', 'n'), ('lesson', '', 'n'), ('lesson', 'task-builds', '')):
            with self.assertRaises(ValueError):
                agent_plan.remember(self.home, kind, about, note=note)
        self.assertEqual([m['id'] for m in agent_plan.memories(self.home, about='task-builds')], ['m1'])
        self.assertEqual(len(agent_plan.memories(self.home, last=1)), 1)

    def test_yes_and_no_in_the_confirm_list_are_remembered(self):
        simctl = self.sims(); now = time.time()
        self.refresh(simctl, now=now - 5 * 3600, task_roots=[]); self.refresh(simctl, now=now, task_roots=[])
        ask_tier.ignore(self.home, {'simulator-delete:OLD'})
        results = ask_tier.execute(self.home, self.cfg(task_roots=[]), self.settings, ['runtime-delete:R1'], simctl, Activity(), now)
        self.assertEqual(results[0]['outcome'], '已完成')
        decisions = {(m['about'], m['decision'], m['source']) for m in agent_plan.memories(self.home)}
        self.assertEqual(decisions, {('simulator-delete', 'ignored', 'confirm'), ('runtime-delete', 'confirmed', 'confirm')})


class ExecutorTest(AskFixture):
    def test_quiet_kinds_are_listed_without_a_nudge_and_off_kinds_not_at_all(self):
        agent_plan.set_rule(self.home, 'simulator-delete', 'quiet', why='你从不删模拟器')
        agent_plan.set_rule(self.home, 'runtime-delete', 'off', why='运行时你自己管')
        agent_plan.set_rule(self.home, 'simulator-shutdown', 'off', why='模拟器开着没关系')
        pending = self.refresh(self.sims(), task_roots=[])
        self.assertEqual(self.ids(pending), ['simulator-delete:OLD'])
        self.assertTrue(pending['items'][0]['quiet'])
        self.assertIsNone(ask_tier.maybe_notify(self.home, pending, 50 * GIB, 10 * GIB, time.time(), launch=lambda: None),
                          'nothing to nudge about')

    def test_a_simulator_held_only_by_a_long_running_stream_is_asked_about(self):
        simctl = self.sims(); now = time.time()
        for command, hours, listed in (('baguette stream --udid ON --fps 1', 13, True),
                                       ('xcrun simctl spawn ON log stream --style compact', 30, True),
                                       ('baguette stream --udid ON --fps 1', 1, False),
                                       ('xcodebuild -destination id=ON test', 30, False)):
            (ask_tier.state_root(self.home) / ask_tier.ASK_STATE).unlink(missing_ok=True)
            (ask_tier.state_root(self.home) / ask_tier.PENDING).unlink(missing_ok=True)
            held = Activity(commands=[(41, command)])
            with patch.object(ask_tier, 'process_ages', return_value={41: hours * 3600}):
                self.refresh(simctl, held, now - 5 * 3600, task_roots=[])
                pending = self.refresh(simctl, held, now, task_roots=[])
            self.assertEqual('simulator-shutdown:ON' in self.ids(pending), listed, command)

    def test_kept_paths_are_left_alone_by_every_part(self):
        task = self.task('20261001-keep', 10)
        cfg = self.cfg()
        agent_plan.add_keep(self.home, task, '用户说还要对比', by='user')
        pending = self.refresh(self.sims(), task_roots=[{'path': str(self.root)}])
        self.assertNotIn('task-delete:' + str(task), self.ids(pending))
        receipt = self.run_tier(cfg)
        self.assertFalse(task.is_symlink())
        self.assertIn('先别动：用户说还要对比', [k['reason'] for k in receipt['kept'] if k['path'] == str(task)])
        with self.assertRaises(ValueError):
            agent_plan.remove_keep(self.home, task)   # the user's 「先别动」 is theirs to lift
        agent_plan.remove_keep(self.home, task, by='user')
        self.run_tier(self.cfg())
        self.assertTrue(task.is_symlink(), 'moved once the user lifted it')

    def test_idle_tasks_set_to_off_stay_on_the_internal_disk(self):
        task = self.task('20261001-idle', 10)
        agent_plan.set_rule(self.home, 'idle-tasks', 'off', why='用户说任务都留在内置盘')
        self.run_tier(self.cfg())
        self.assertFalse(task.is_symlink())

    def test_the_plan_page_follows_the_rules(self):
        report = dict(tasks=[dict(path=str(self.root / 'a'), linked=False, bytes=9 * GIB, idle=True)], caches=[], categories=[])
        cfg = self.cfg()
        before = space_plan.build(self.home, cfg, self.settings, report, dict(items=[]), GIB, True)
        self.assertEqual([i['kind'] for i in before['sections'][0]['items']], ['move'])
        agent_plan.add_keep(self.home, self.root / 'a', '先别动', by='user')
        after = space_plan.build(self.home, cfg, self.settings, report, dict(items=[]), GIB, True)
        self.assertEqual(after['sections'][0]['items'], [])


class CommandTest(TierFixture):
    """What the user's agent runs (SKILL.md): `resource-guard-service memory / rules / rule / keep / setting`."""
    def rgs(self, *args):
        out = subprocess.run([sys.executable, str(ROOT / 'scripts/service.py'), *args], capture_output=True, text=True,
                             env=dict(os.environ, HOME=str(self.home)))
        return out.returncode, (json.loads(out.stdout) if out.returncode == 0 else out.stderr.strip())

    def test_the_agent_reads_and_changes_the_plan_with_commands(self):
        code, lesson = self.rgs('memory', 'add', '--kind', 'lesson', '--about', 'task-builds', '--note', '每轮测试一份 Swift 包')
        self.assertEqual((code, lesson['id']), (0, 'm1'))
        code, rule = self.rgs('rule', 'simulator-shutdown', 'ask', '--why', '看画面的程序挂 6 小时就算没人用', '--evidence', 'm1',
                              '--stream-hours', '6')
        self.assertEqual((code, rule['params'], rule['evidence']), (0, {'stream_hours': 6.0}, ['m1']))
        code, said = self.rgs('keep', 'add', str(self.root / 'x'), '--by', 'user', '--words', '这个先别动')
        self.assertEqual(code, 0)
        code, rules = self.rgs('rules')
        self.assertEqual(next(r for r in rules['rules'] if r['id'] == 'simulator-shutdown')['set_by'], 'agent')
        self.assertEqual(rules['keep'][0]['why'], '这个先别动')
        code, memory = self.rgs('memory')
        self.assertEqual([m['source'] for m in memory['memories']], ['agent', 'chat'])

    def test_the_agent_changes_an_unlocked_setting_and_says_so(self):
        code, done = self.rgs('setting', 'target', '60', '--why', '这台 Mac 平时就在 30～60 GB 之间', '--evidence', 'm2')
        self.assertEqual((code, done['after'], done['by']), (0, '60 GB', 'agent'))
        self.assertEqual(self.rgs('setting', 'target', '70')[0], 2, 'no reason, no change')
        asked = json.loads((agent_plan.state_dir(self.home) / agent_plan.NOTICE).read_text())
        self.assertIn('空间目标：', asked['body']); self.assertIn('→ 60 GB', asked['body'])
        self.assertEqual(agent_plan.load(self.home)['settings']['target']['evidence'], ['m2'])

    def test_a_locked_setting_is_refused_to_the_agent_with_a_reason_to_pass_on(self):
        self.assertEqual(self.rgs('setting', 'target', 'lock', '--words', '目标我自己定')[0], 0)
        code, error = self.rgs('setting', 'target', '30', '--why', '过去两周 10 次低于目标你都没处理')
        self.assertEqual(code, 2)
        self.assertIn('锁住', error)
        code, done = self.rgs('setting', 'target', '30', '--by', 'user', '--words', '改成 30 吧')
        self.assertEqual((code, done['after'], done['by']), (0, '30 GB', 'user'))
        self.assertEqual(self.rgs('rule', 'task-delete', 'clean', '--why', 'x')[0], 2, 'the floor holds on the command line too')

    def test_a_change_in_the_panel_is_the_users(self):
        (self.home / '.resource-dashboard-fixture').touch()
        env = dict(os.environ, HOME=str(self.home))
        out = subprocess.run([sys.executable, str(ROOT / 'widget/backend.py'), 'plan-target', '--path', '60', '--home', str(self.home)],
                             capture_output=True, text=True, env=env)
        self.assertEqual(out.returncode, 0, out.stderr)
        target = agent_plan.load(self.home)['settings']['target']
        self.assertEqual(target['set_by'], 'user')
        [memory] = agent_plan.memories(self.home)
        self.assertEqual((memory['source'], memory['decision']), ('panel', 'switched'))
        self.assertIn('60 GB', memory['note'])


if __name__ == '__main__':
    unittest.main()
