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
from test_one_click import CacheFixture, idle
import toolcaches
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
        [asked] = agent_plan.queued_notices(self.home)
        self.assertEqual(asked['title'], '资源看板的计划改了')
        self.assertIn('不可用或很久没开的模拟器：先问 → 只列出、不催。你拒绝过 3 次删模拟器', asked['body'])

    def test_a_lock_stops_the_agent_not_the_user(self):
        agent_plan.set_lock(self.home, 'idle-tasks', True, words='任务别挪，我要在本机对比')
        with self.assertRaises(ValueError) as caught:
            agent_plan.set_rule(self.home, 'idle-tasks', 'off', why='空间够了')
        self.assertIn('锁住', str(caught.exception))
        agent_plan.set_rule(self.home, 'idle-tasks', 'off', by='user', words='那就先不挪')
        agent_plan.set_lock(self.home, 'target', True, words='目标我自己定')
        with self.assertRaises(ValueError):
            agent_plan.check_setting(self.home, 'target', '过去两周 12 次低于目标')
        [first, second] = agent_plan.memories(self.home)
        self.assertEqual((first['decision'], first['words']), ('locked', '任务别挪，我要在本机对比'))
        self.assertEqual(second['about'], 'target')
        self.assertEqual(agent_plan.queued_notices(self.home), [], 'the user is not told of their own change')


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
        agent_plan.remove_keep(self.home, task, by='user', why='可以挪了')
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


class CommandFixture(TierFixture):
    def rgs(self, *args):
        out = subprocess.run([sys.executable, str(ROOT / 'scripts/service.py'), *args], capture_output=True, text=True,
                             env=dict(os.environ, HOME=str(self.home)))
        return out.returncode, (json.loads(out.stdout) if out.returncode == 0 else out.stderr.strip())


class CommandTest(CommandFixture):
    """What the user's agent runs (SKILL.md): `resource-guard-service memory / rules / rule / keep / setting`."""

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
        [asked] = agent_plan.queued_notices(self.home)
        self.assertIn('空间目标：', asked['body']); self.assertIn('→ 60 GB', asked['body'])
        self.assertEqual(agent_plan.load(self.home)['settings']['target']['evidence'], ['m2'])

    def test_a_locked_setting_is_refused_to_the_agent_with_a_reason_to_pass_on(self):
        self.assertEqual(self.rgs('setting', 'target', 'lock', '--words', '目标我自己定')[0], 2, 'only the user locks')
        self.assertEqual(self.rgs('setting', 'target', 'lock', '--by', 'user', '--words', '目标我自己定')[0], 0)
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



DAY = 86400
QUIET = dict(gap_bytes=0, free_bytes=80 * GIB, suggestions=[],
             sections=[dict(id='auto', bytes=0), dict(id='ask', bytes=0), dict(id='judge', bytes=0)])


class ReviewTest(TierFixture):
    """agent-review, first-plan and plan-settle."""
    T0 = 1_800_000_000.0

    def test_the_first_review_is_owed_a_week_after_the_first_plan_and_nudged_once(self):
        agent_plan.ensure_started(self.home, self.T0)
        self.assertIsNone(agent_plan.review_due(self.home, self.T0 + 6 * DAY))
        self.assertEqual(agent_plan.review_due(self.home, self.T0 + 7 * DAY + 1), 'first')
        with patch.object(agent_plan.time, 'time', return_value=self.T0 + DAY):
            agent_plan.remember(self.home, 'decision', 'simulator-delete', decision='ignored', note='忽略', source='confirm')
        nudge = space_plan.agent_needed(self.home, QUIET, [], self.T0 + 7 * DAY + 1)
        self.assertEqual(nudge['title'], '资源看板第一周复查')
        self.assertIn('这一周记了 1 条你的决定', nudge['reason'])
        self.assertIsNone(space_plan.agent_needed(self.home, QUIET, [], self.T0 + 8 * DAY), 'once a week')
        agent_plan.finish_review(self.home, True, '你不删模拟器，改成只列出', self.T0 + 8 * DAY)
        self.assertIsNone(agent_plan.review_due(self.home, self.T0 + 14 * DAY))
        self.assertEqual(agent_plan.review_due(self.home, self.T0 + 15 * DAY + 1), 'weekly')
        self.assertEqual(space_plan.agent_needed(self.home, QUIET, [], self.T0 + 15 * DAY + 1)['title'], '资源看板每周复查')

    def test_a_review_in_the_agents_own_scheduler_gets_two_days_grace(self):
        agent_plan.ensure_started(self.home, self.T0)
        agent_plan.note_schedule(self.home, 'claude', self.T0)
        self.assertIsNone(agent_plan.review_due(self.home, self.T0 + 8 * DAY))
        self.assertEqual(agent_plan.review_due(self.home, self.T0 + 9 * DAY + 1), 'first', 'the scheduled review was missed')
        with self.assertRaises(ValueError):
            agent_plan.note_schedule(self.home, 'cron')

    def test_the_review_packet_holds_what_happened_since_the_last_review(self):
        agent_plan.ensure_started(self.home, self.T0)
        first = agent_plan.review_packet(self.home)
        self.assertTrue(first['first_review'])
        self.assertIn('第一周结束的复查', first['steps'])
        agent_plan.remember(self.home, 'lesson', 'task-builds', note='旧的')
        agent_plan.finish_review(self.home, False, '没改', time.time())
        time.sleep(0.01)
        agent_plan.remember(self.home, 'decision', 'keep', words='这个先别动', source='chat')
        later = agent_plan.review_packet(self.home)
        self.assertEqual(([m['note'] or m['words'] for m in later['memories']], later['first_review']), (['这个先别动'], False))
        with self.assertRaises(ValueError):
            agent_plan.finish_review(self.home, False, ' ')

    def test_a_scheduled_run_before_the_first_week_is_told_to_wait(self):
        started = time.time() - 3 * DAY
        agent_plan.ensure_started(self.home, started)
        early = agent_plan.review_packet(self.home)
        self.assertEqual((early['due'], early['next_review_at']), (False, started + 7 * DAY))
        self.assertTrue(early['steps'].startswith('还没到复查时间'))
        later = agent_plan.review_packet(self.home, now=started + 7 * DAY + 60)
        self.assertTrue(later['due'])
        self.assertTrue(later['steps'].startswith('这是第一周结束的复查'))

    def review(self, at, changed=False):
        return agent_plan.finish_review(self.home, changed, '第 {} 次'.format(len(agent_plan.reviews(self.home)) + 1), at)

    def test_the_plan_settles_after_four_quiet_reviews_without_corrections(self):
        agent_plan.ensure_started(self.home, self.T0)
        self.review(self.T0 + 7 * DAY, changed=True)
        for week in (2, 3, 4):
            self.assertIsNone(self.review(self.T0 + week * 7 * DAY)['settled_at'])
        # The user corrected the plan by hand in week 5: the four quiet weeks start again after it.
        with patch.object(agent_plan.time, 'time', return_value=self.T0 + 34 * DAY):
            agent_plan.remember(self.home, 'decision', 'target', decision='switched', note='用户把目标改成 60 GB', source='panel')
        for week in (5, 6, 7):
            self.assertIsNone(self.review(self.T0 + week * 7 * DAY)['settled_at'], week)
        self.assertEqual(self.review(self.T0 + 8 * 7 * DAY)['settled_at'], self.T0 + 8 * 7 * DAY)
        asked = agent_plan.queued_notices(self.home)[-1]
        self.assertEqual(asked['title'], '资源看板的计划定稿了')
        self.assertIn('定稿', agent_plan.review_packet(self.home)['steps'])

    def settle(self):
        agent_plan.ensure_started(self.home, self.T0)
        for week in (1, 2, 3, 4):
            self.review(self.T0 + week * 7 * DAY)
        self.assertTrue(agent_plan.load(self.home)['meta']['settled_at'])

    def test_a_settled_plan_refuses_the_agent_until_reopened_with_a_reason(self):
        self.settle()
        for change in (lambda: agent_plan.set_rule(self.home, 'trash-empty', 'quiet', why='废纸篓你自己清'),
                       lambda: agent_plan.check_setting(self.home, 'target', '空间够了'),
                       lambda: agent_plan.add_keep(self.home, self.root / 'x', '先留着')):
            with self.assertRaises(ValueError) as caught:
                change()
            self.assertIn('定稿', str(caught.exception))
        with self.assertRaises(ValueError):
            agent_plan.reopen(self.home, '')
        agent_plan.reopen(self.home, '新出现了 Android 的 Gradle 缓存，一周涨了 15 GB')
        agent_plan.set_rule(self.home, 'trash-empty', 'quiet', why='废纸篓你自己清')
        self.assertEqual(agent_plan.changes(self.home)[-2]['after'], '重新打开')

    def test_the_user_changing_a_settled_plan_opens_it_again(self):
        self.settle()
        agent_plan.set_rule(self.home, 'installer-delete', 'off', by='user', words='安装包我自己管')
        self.assertIsNone(agent_plan.load(self.home)['meta']['settled_at'])
        reopened = [c for c in agent_plan.changes(self.home) if c['after'] == '重新打开']
        self.assertEqual((reopened[0]['by'], reopened[0]['why']), ('user', '用户自己把「下载里的大安装包」改成不管'))


class ReviewCommandTest(CommandFixture):
    def test_the_review_commands(self):
        code, packet = self.rgs('review')
        self.assertEqual((code, packet['first_review']), (0, True))
        self.assertEqual(self.rgs('review', 'done', '--summary', '没改')[0], 2, '--changed is required')
        code, done = self.rgs('review', 'done', '--changed', 'no', '--summary', '这周没有新情况')
        self.assertEqual((code, done['review']['changed'], done['settled_at']), (0, False, None))
        self.assertEqual(self.rgs('review', 'scheduled', 'codex')[1]['agent'], 'codex')
        self.assertEqual(self.rgs('review', 'reopen', '--why', '换了外接盘')[1], {'status': 'not-settled'})
        self.assertIn('review', space_plan.AGENT_PROMPT)



class ReviewFindingsTest(AskFixture):
    """Codex review CR-01 (2026-10-30) of batches 1 and 2: each case is a sequence it showed would go wrong."""

    def test_1_the_agent_cannot_take_over_and_then_lift_the_users_keep(self):
        task = self.root / 'x'
        agent_plan.add_keep(self.home, task, '我要对比', by='user')
        agent_plan.add_keep(self.home, task, '先留着')   # the agent adds the same path again
        [entry] = agent_plan.load(self.home)['keep']
        self.assertEqual((entry['set_by'], entry['why']), ('user', '我要对比'))
        with self.assertRaises(ValueError):
            agent_plan.remove_keep(self.home, task, why='可以挪了')

    def test_2_a_stale_confirmation_cannot_run_what_the_plan_now_leaves_alone(self):
        simctl = self.sims(); now = time.time()
        self.refresh(simctl, now=now, task_roots=[])
        agent_plan.set_rule(self.home, 'runtime-delete', 'off', why='运行时你自己管')
        old = Path(json.loads(simctl(['list', 'devices', '--json']))['devices'][self.IOS][0]['dataPath'])
        agent_plan.add_keep(self.home, old.parent, '这台模拟器有测试账号', by='user')
        results = ask_tier.execute(self.home, self.cfg(task_roots=[]), self.settings, ['runtime-delete:R1', 'simulator-delete:OLD'],
                                   simctl, Activity(), now)
        self.assertEqual([r['outcome'] for r in results], ['已跳过', '已跳过'])
        self.assertIn('不管', results[0]['reason']); self.assertIn('先别动', results[1]['reason'])
        self.assertFalse(any(c[:2] in (['runtime', 'delete'], ['delete', 'OLD']) for c in simctl.calls))

    def test_3_the_xcode_cache_cleanup_leaves_a_kept_folder(self):
        import service
        from test_tiers import age
        self.cfg()
        cache = self.home / 'Library/Developer/Xcode/DerivedData/Old/Build/Intermediates.noindex'
        cache.mkdir(parents=True); (cache / 'file.o').write_bytes(b'x' * 4096); age(cache, 20)
        agent_plan.add_keep(self.home, self.home / 'Library/Developer/Xcode/DerivedData/Old', '还在对比编译结果', by='user')
        with patch.object(service.common, 'activity', return_value=Activity()):
            service.clean_caches(self.home)
        self.assertTrue((cache / 'file.o').exists())

    def test_4_overlapping_writers_lose_nothing_and_memories_get_their_own_ids(self):
        import threading
        def remember(i):
            agent_plan.remember(self.home, 'lesson', 'task-builds', note='第 {} 条'.format(i))
        def change(i):
            agent_plan.set_rule(self.home, 'trash-empty' if i % 2 else 'archive-delete', 'quiet', why='第 {} 次'.format(i))
        threads = [threading.Thread(target=remember, args=(i,)) for i in range(20)]
        threads += [threading.Thread(target=change, args=(i,)) for i in range(6)]
        threads += [threading.Thread(target=lambda: agent_plan.set_lock(self.home, 'target', True, words='别动目标'))]
        for t in threads: t.start()
        for t in threads: t.join()
        ids = [m['id'] for m in agent_plan.memories(self.home)]
        self.assertEqual(len(ids), len(set(ids)), ids)
        plan = agent_plan.load(self.home)
        self.assertEqual((plan['rules']['trash-empty']['action'], plan['rules']['archive-delete']['action']), ('quiet', 'quiet'))
        self.assertTrue(plan['settings']['target']['locked'])

    def test_10_a_readable_but_wrong_plan_file_degrades_entry_by_entry(self):
        path = agent_plan.plan_path(self.home); path.parent.mkdir(parents=True, exist_ok=True)
        for broken in ({'rules': [1]}, {'meta': [1]}, {'settings': 'x', 'keep': {'a': 1}},
                       {'rules': {'simulator-shutdown': {'action': 'ask', 'params': {'stream_hours': 'soon'}}}},
                       {'keep': [{'path': 'relative/path'}, {'path': '/abs/kept', 'why': 3}]}):
            path.write_text(json.dumps(broken))
            plan = agent_plan.load(self.home)
            self.assertEqual(plan['rules']['simulator-shutdown']['params']['stream_hours'], 12, broken)
        self.assertEqual([k['path'] for k in plan['keep']], ['/abs/kept'])
        memory = agent_plan.state_dir(self.home) / agent_plan.MEMORY; memory.parent.mkdir(parents=True, exist_ok=True)
        memory.write_text('null\n[1]\n{"id": "m1", "kind": "lesson", "about": "x", "note": "y"}\n')
        self.assertEqual([m['id'] for m in agent_plan.memories(self.home)], ['m1'])

    def test_9_paths_relative_inside_a_task_and_simulator_data_are_all_covered(self):
        here = os.getcwd(); os.chdir(self.home)
        try:
            entry = agent_plan.add_keep(self.home, 'work/worktrees/t1/notes', '笔记', by='user')
        finally:
            os.chdir(here)
        self.assertEqual(entry['path'], str(self.root / 't1/notes'), 'kept as an absolute path')
        plan = agent_plan.load(self.home)
        self.assertTrue(agent_plan.kept(plan, self.root / 't1'), 'moving the whole task would take the kept notes along')
        self.assertIsNone(agent_plan.kept(plan, self.root / 't2'))
        simctl = self.sims()
        data = Path(json.loads(simctl(['list', 'devices', '--json']))['devices'][self.IOS][0]['dataPath'])
        agent_plan.add_keep(self.home, data, '有登录态', by='user')
        self.assertNotIn('simulator-delete:OLD', self.ids(self.refresh(simctl, task_roots=[])))

    def test_11_quiet_items_never_nudge_through_the_agent_notice(self):
        plan = dict(free_bytes=40 * GIB, gap_bytes=10 * GIB, suggestions=[],
                    sections=[dict(id='auto', bytes=0, items=[]), dict(id='ask', bytes=GIB, items=[dict(bytes=GIB, quiet=True)]),
                              dict(id='judge', bytes=0, items=[])])
        self.assertIsNone(space_plan.agent_needed(self.home, plan, [], time.time(), 25 * GIB))
        loud = dict(plan, sections=[plan['sections'][0], dict(id='ask', bytes=GIB, items=[dict(bytes=GIB, quiet=False)]), plan['sections'][2]])
        self.assertIn('还差 10 GB', space_plan.agent_needed(self.home, loud, [], time.time(), 25 * GIB)['reason'])

    def test_12_two_changes_are_both_said_and_a_change_of_hours_is_visible(self):
        agent_plan.set_rule(self.home, 'trash-empty', 'quiet', why='废纸篓你自己清')
        agent_plan.set_rule(self.home, 'simulator-shutdown', 'ask', why='看画面 6 小时就算没人用', params={'stream_hours': 6})
        bodies = [n['body'] for n in agent_plan.queued_notices(self.home)]
        self.assertEqual(len(bodies), 2)
        self.assertIn('超过 12 小时算没人用） → 先问（看画面或日志超过 6 小时算没人用）', bodies[1])

    def test_13_the_plan_page_counts_only_build_copies_that_will_move(self):
        task = self.root / 't'
        for copy in ('DerivedData', 'old/DerivedData'):
            (task / copy / 'Build').mkdir(parents=True)
        agent_plan.add_keep(self.home, task / 'DerivedData', '对比用', by='user')
        report = dict(tasks=[dict(path=str(task), linked=False, bytes=9 * GIB, idle=False)], caches=[], categories=[])
        cfg = self.cfg(caches_enabled=True)
        result = space_plan.build(self.home, cfg, self.settings, report, dict(items=[]), GIB, True, sizer=lambda p: 8 * GIB)
        self.assertEqual([i['path'] for i in result['sections'][0]['items']], [str(task / 'old/DerivedData')])

    def test_14_a_path_holding_the_words_is_not_a_stream(self):
        for command, passive in (('xcodebuild -project /repos/baguette/stream/App.xcodeproj -destination id=ON test', False),
                                 ('/Users/me/.local/share/baguette-0.2.12/Baguette stream --udid ON --fps 1', True),
                                 ('/bin/zsh -c source s.sh && baguette stream --udid ON --fps 1', True),
                                 ('/Applications/Claude.app/Contents/Helpers/disclaimer -- xcrun simctl spawn ON log stream --style compact', True),
                                 ('flutter test -d ON', False)):
            self.assertEqual(bool(ask_tier.PASSIVE.search(command)), passive, command)



class ToolCacheKeepTest(CacheFixture):
    def test_8_a_kept_tool_cache_is_not_moved(self):
        npm = self.cache('.npm')
        agent_plan.add_keep(self.home, npm, '离线时要用', by='user')
        actions = toolcaches.take_over(self.home, self.managed, idle())
        self.assertFalse(npm.is_symlink())
        self.assertEqual([a['reason'] for a in actions if a['path'] == str(npm)], ['先别动：离线时要用'])

class LockAndSettleFindingsTest(CommandFixture):
    def test_5_only_the_users_words_lock_unlock_or_speak_for_the_user(self):
        agent_plan.set_lock(self.home, 'target', True, words='目标我自己定')
        with self.assertRaises(ValueError):
            agent_plan.set_lock(self.home, 'target', False, words='', by='user')
        with self.assertRaises(ValueError):
            agent_plan.set_lock(self.home, 'target', False, words='用户说可以', by='agent')
        self.assertEqual(self.rgs('setting', 'target', 'unlock')[0], 2)
        self.assertEqual(self.rgs('target', '30')[0], 2, 'the old command goes through the same door')
        self.assertEqual(self.rgs('target', '30', '--why', '空间够了')[0], 2, 'locked')
        self.assertEqual(self.rgs('rule', 'trash-empty', 'quiet', '--by', 'user')[0], 2, 'no words, no user')
        code, done = self.rgs('target', '30', '--by', 'user', '--words', '改成 30')
        self.assertEqual((code, done['after']), (0, '30 GB'))

    def test_6_a_switch_that_moves_a_locked_one_is_refused(self):
        import service
        with patch.object(service.checkup, 'apply', side_effect=AssertionError('must not run')):
            agent_plan.set_lock(self.home, 'ask', True, words='等你确认别动')
            with self.assertRaises(ValueError) as caught:
                service.change_setting(self.home, 'clean', 'on', why='该清就清')
        self.assertIn('等你确认', str(caught.exception))

    def test_7_after_reopening_the_quiet_weeks_start_over(self):
        t0 = 1_800_000_000.0
        agent_plan.ensure_started(self.home, t0)
        for week in (1, 2, 3, 4):
            agent_plan.finish_review(self.home, False, '没改', t0 + week * 7 * DAY)
        with patch.object(agent_plan.time, 'time', return_value=t0 + 30 * DAY):
            agent_plan.reopen(self.home, '换了一块外接盘')
        self.assertIsNone(agent_plan.finish_review(self.home, False, '没改', t0 + 35 * DAY)['settled_at'])
        for week in (6, 7):
            self.assertIsNone(agent_plan.finish_review(self.home, False, '没改', t0 + week * 7 * DAY)['settled_at'])
        self.assertTrue(agent_plan.finish_review(self.home, False, '没改', t0 + 8 * 7 * DAY)['settled_at'])

    def test_7b_a_review_that_says_no_change_while_the_plan_changed_does_not_count(self):
        t0 = 1_800_000_000.0
        agent_plan.ensure_started(self.home, t0)
        agent_plan.finish_review(self.home, False, '没改', t0 + 7 * DAY)
        with patch.object(agent_plan.time, 'time', return_value=t0 + 10 * DAY):
            agent_plan.set_rule(self.home, 'trash-empty', 'quiet', why='废纸篓你自己清')
        for week in (2, 3, 4):
            self.assertIsNone(agent_plan.finish_review(self.home, False, '没改', t0 + week * 7 * DAY)['settled_at'])


if __name__ == '__main__':
    unittest.main()
