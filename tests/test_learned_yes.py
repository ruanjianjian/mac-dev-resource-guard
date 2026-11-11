"""11-10: the guard did not do its job while the disk ran out. The user's yes, said again and again, becomes the rule
(learned-yes); each emergency goes to a sub-agent with the raw facts and more it can hand back, and a main agent
looks over the runs once a day."""
import json
import os
from pathlib import Path
import sys
import subprocess
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_ask_tier import AskFixture, FakeSimctl
from test_tiers import DAY, age, git
import agent_plan
import ask_tier
import emergency as E
import watch as W
from storage import GIB, Activity

NOW = time.time()


def said_yes(home, about, notes, days):
    """The user's yeses in 「等你确认」, one per note, on the given days back."""
    for note, back in zip(notes, days):
        entry = dict(id='m{}'.format(len(agent_plan.memories(home)) + 1), at=NOW - back * DAY, kind='decision', source='confirm',
                     about=about, decision='confirmed', path='X', words='', note=note)
        agent_plan._append(home, agent_plan.MEMORY, entry)


class LearnTest(AskFixture):
    def test_a_yes_said_on_three_days_becomes_the_rule_with_the_youngest_age_they_agreed_to(self):
        said_yes(self.home, 'simulator-delete', ['确认处理「A」：后台补列：关机 6 天没开过', '确认处理「B」：已关机 2 天没开过'], [5, 4])
        self.assertEqual(agent_plan.learn_yes(self.home), [])          # two days are not yet a habit
        said_yes(self.home, 'simulator-delete', ['确认处理「C」：后台补列：关机 4 天没开过，没有开发工具连着'], [0])
        said_yes(self.home, 'simulator-shutdown', ['确认处理「D」：开着 7 小时没有开发工具连着',
                                                   '确认处理「E」：开着 5 小时没有开发工具连着', '确认处理「F」：开着 60 小时'], [3, 1, 0])
        self.assertEqual(sorted(agent_plan.learn_yes(self.home)), ['simulator-delete', 'simulator-shutdown'])
        plan = agent_plan.load(self.home)
        delete, shut = plan['rules']['simulator-delete'], plan['rules']['simulator-shutdown']
        self.assertEqual((delete['action'], delete['set_by'], delete['params']['min_days']), ('auto', 'learned', 2))
        self.assertEqual((shut['action'], shut['params']['min_hours'], shut['params']['stream_hours']), ('auto', 5, 12))
        self.assertEqual(agent_plan.rule_words('simulator-delete', delete), '你同意过，磁盘不够时直接删关机 2 天以上没开过的')
        self.assertEqual(delete['evidence'], ['m1', 'm2', 'm3'])
        notices = agent_plan.queued_notices(self.home)
        self.assertEqual(len(notices), 2)
        self.assertIn('你在 3 天里 3 次都同意了，一次没忽略', notices[0]['body'])
        self.assertIn('跟你的 agent 说一声', notices[0]['body'])
        self.assertEqual([c['by'] for c in agent_plan.changes(self.home)], ['learned', 'learned'])
        self.assertEqual(agent_plan.learn_yes(self.home), [])          # said once

    def test_one_no_or_the_users_own_setting_keeps_it_asking(self):
        said_yes(self.home, 'simulator-delete', ['关机 6 天', '关机 5 天', '关机 4 天'], [3, 2, 1])
        agent_plan._append(self.home, agent_plan.MEMORY, dict(id='m9', at=NOW, kind='decision', source='confirm',
                                                              about='simulator-delete', decision='ignored', note='忽略「G」'))
        said_yes(self.home, 'simulator-shutdown', ['开着 7 小时', '开着 6 小时', '开着 5 小时'], [3, 2, 1])
        agent_plan.set_rule(self.home, 'simulator-shutdown', 'ask', by='user', words='关模拟器先问我')
        self.assertEqual(agent_plan.learn_yes(self.home), [])
        self.assertEqual(agent_plan.load(self.home)['rules']['simulator-delete']['action'], 'ask')

    def test_the_agent_cannot_make_deleting_simulators_automatic_but_the_user_can(self):
        with self.assertRaises(ValueError) as refused:
            agent_plan.set_rule(self.home, 'simulator-delete', 'auto', by='agent', why='省事')
        self.assertIn('用户自己同意过', str(refused.exception))
        agent_plan.set_rule(self.home, 'simulator-shutdown', 'auto', by='agent', why='内存紧时开着没人连的都是残留')
        entry = agent_plan.set_rule(self.home, 'simulator-delete', 'auto', by='user', words='关机的以后直接删')
        self.assertEqual(entry['params']['min_days'], 7)               # set by hand: the rules' own waiting time


class AutoRunTest(AskFixture):
    def learned(self, rid, **params):
        plan = agent_plan.load(self.home)
        plan['rules'][rid].update(action='auto', set_by='learned', why='同意过')
        plan['rules'][rid]['params'].update(params)
        agent_plan.save(self.home, plan)

    def test_shut_down_simulators_go_biggest_first_only_as_many_as_the_disk_is_short(self):
        cfg = self.cfg(task_roots=[])
        old = self.device('OLD', 'Shutdown', 10, name='Old'); old['dataPathSize'] = 6 * GIB
        mid = self.device('MID', 'Shutdown', 3, name='Mid'); mid['dataPathSize'] = 2 * GIB
        new = self.device('NEW', 'Shutdown', 1, name='New')
        simctl = FakeSimctl({self.IOS: [old, mid, new]})
        run = lambda short: ask_tier.auto_run(self.home, cfg, self.settings, simctl, 'simulator-delete', NOW, short, Activity())
        self.assertEqual(run(GIB), [])                                  # still 「先问」: nothing without the user's yes
        self.learned('simulator-delete', min_days=2)
        self.assertEqual(run(0), [])                                    # the disk is fine
        done = run(GIB)
        self.assertEqual([r['id'] for r in done], ['simulator-delete:OLD'])   # the biggest covers it
        self.assertIn(['delete', 'OLD'], simctl.calls)
        simctl.devices[self.IOS].remove(old)                            # gone for real
        done = run(10 * GIB)
        self.assertEqual([r['id'] for r in done], ['simulator-delete:MID'])   # the 1-day-old one is younger than the yes
        [first, second] = agent_plan.queued_notices(self.home)
        self.assertEqual(first['title'], '按你同意过的，删了 1 个模拟器')
        self.assertIn('关机 2 天以上没开过，腾出约 6 GB：Old', first['body'])
        auto = [m for m in agent_plan.memories(self.home) if m.get('source') == 'auto']
        self.assertEqual([m['path'] for m in auto], ['OLD', 'MID'])     # remembered, but not as one more yes of theirs
        self.assertTrue(all(m['note'].startswith('照你同意过的直接处理') for m in auto))

    def test_open_simulators_nobody_connected_to_are_shut_when_the_rule_says_so(self):
        cfg = self.cfg(task_roots=[])
        simctl = FakeSimctl({self.IOS: [self.device('ON', 'Booted', 0, name='On'), self.device('NEW', 'Booted', 0, name='New')]})
        ask_tier.save(self.home, ask_tier.ASK_STATE, {'unattached_since': {'ON': NOW - 6 * 3600, 'NEW': NOW - 3 * 3600}})
        self.learned('simulator-shutdown', min_hours=5)
        busy = Activity(commands=[(9, 'xcodebuild -destination id=ON test')])
        self.assertEqual(ask_tier.auto_run(self.home, cfg, self.settings, simctl, 'simulator-shutdown', NOW, activity=busy), [])
        done = ask_tier.auto_run(self.home, cfg, self.settings, simctl, 'simulator-shutdown', NOW, activity=Activity())
        self.assertEqual([r['id'] for r in done], ['simulator-shutdown:ON'])   # 3 hours is less than the yes covered
        [notice] = agent_plan.queued_notices(self.home)
        self.assertEqual((notice['title'], notice['tab']), ('内存紧，按你同意过的关了 1 个模拟器', 'memory'))
        self.assertIn('数据都在，要用再开', notice['body'])

    def test_the_watch_looks_at_most_every_ten_minutes_and_gives_way_to_the_panel(self):
        cfg = self.cfg(task_roots=[])
        self.learned('simulator-delete', min_days=2)
        calls = []
        fake = lambda home, cfg, settings, simctl, kind, now=None, short_bytes=None: calls.append((kind, short_bytes)) or []
        W.auto_after(self.home, cfg, self.settings, NOW, auto_run=fake, free=GIB)
        self.assertEqual(calls, [('simulator-delete', self.settings.warning_free_gib * GIB - GIB)])
        W.auto_after(self.home, cfg, self.settings, NOW + 60, auto_run=fake, free=GIB)
        self.assertEqual(len(calls), 1)
        W.auto_after(self.home, cfg, self.settings, NOW + W.AUTO_GAP, auto_run=fake, free=50 * GIB)
        self.assertEqual(len(calls), 1)                                 # the disk is fine again
        import fcntl
        lock = W.state_root(self.home) / 'dashboard' / 'operation.lock'
        with lock.open('a') as held:
            fcntl.flock(held, fcntl.LOCK_EX)
            W.auto_after(self.home, cfg, self.settings, NOW + W.AUTO_GAP, auto_run=fake, free=GIB)
        self.assertEqual(len(calls), 1)                                 # someone is cleaning in the panel


class FreerAgentTest(AskFixture):
    def material(self, folder):
        (folder / E.MATERIAL).write_text(json.dumps(dict(at=NOW, orphans=[], tasks=[], simulators=[], waiting=[], companions=[])))

    def test_anything_big_it_finds_can_be_put_to_the_user_and_the_floor_still_holds(self):
        folder = E.new_folder(self.home, NOW); self.material(folder)
        cache = self.home / 'Library/Caches/com.example.big'; cache.mkdir(parents=True)
        (cache / 'blob').write_bytes(b'x' * 8192)
        repo = self.home / 'code/app'; repo.mkdir(parents=True); git(repo, 'init', '-q')
        with patch.object(E.Path, 'home', return_value=self.home):
            entry = E.decide(folder, 'ask', 'path-delete:' + str(cache), why='一个 App 的缓存，8 天没动，能重新生成')
            for bad, words in ((self.home / 'Library', '至少两层深'), (repo, '代码仓库'), (Path('/tmp/x'), '主目录里'),
                               (self.home / 'Library/nope', '不存在')):
                with self.assertRaises(ValueError) as refused:
                    E.decide(folder, 'ask', 'path-delete:' + str(bad), why='x')
                self.assertIn(words, str(refused.exception))
        self.assertEqual((entry['kind'], entry['path']), ('path-delete', str(cache)))
        self.assertGreater(entry['bytes'], 0)
        [asked] = E.add_asks(self.home, [entry], NOW)
        self.assertEqual(asked['loses'], ask_tier.LOSES['path-delete'])
        cfg = self.cfg(task_roots=[])
        go = lambda activity: ask_tier.execute(self.home, cfg, self.settings, [asked['id']], FakeSimctl({}), activity, NOW)
        [fresh] = go(Activity())
        self.assertEqual(fresh['reason'], '最近一天还在改，先不删')
        age(cache, 3)
        [held] = go(Activity(open_paths=[(42, str(cache / 'blob'))]))
        self.assertIn('有程序正在用它', held['reason'])
        [done] = go(Activity())
        self.assertEqual(done['outcome'], '已完成')
        self.assertFalse(cache.exists())

    def test_it_can_change_the_plan_within_the_floor(self):
        folder = E.new_folder(self.home, NOW); self.material(folder)
        E.decide(folder, 'rule', 'simulator-shutdown', why='三天里开着没人连的都是验收残留', note='auto')
        E.decide(folder, 'rule', 'simulator-delete', why='省事', note='auto')
        with self.assertRaises(ValueError):
            E.decide(folder, 'rule', 'task-delete', why='x', note='clean')   # not one of its choices
        result = E.apply(self.home, self.cfg(task_roots=[]), self.settings, folder, now=NOW, table={})
        self.assertEqual([d['result'] for d in result['done']], ['已改', '删模拟器要用户自己同意过才能直接做：列给用户点头，同意多了后台会自己学会'])
        self.assertEqual(agent_plan.load(self.home)['rules']['simulator-shutdown']['action'], 'auto')
        told = E.receipt(E.decisions(folder), dict(received=0, recorded=[], refused=[]), result)
        self.assertEqual((told['handed'], told['done']), (2, 1))

    def test_a_run_that_handed_back_nothing_is_told_so_and_not_asked_to_restate_it(self):
        prompts = []

        def codex(home, folder, prompt, env, timeout, popen=None):
            prompts.append(prompt)
            return dict(agent='codex', status='done', thread='t', said='')

        cfg = self.cfg(task_roots=[])
        for n in (1, 2):
            folder = E.new_folder(self.home, NOW + n); self.material(folder)
            state = E.load_state(self.home)
            state['run'] = dict(pid=os.getpid(), started=NOW, reason='memory', folder=str(folder), streak=n)
            E.save_state(self.home, state)
            with patch.object(E, 'run_codex', codex), patch.object(ask_tier, 'launch_app'):
                E.run(self.home, cfg, self.settings, 'memory', folder, 'codex', now=NOW + n)
        self.assertEqual(E.load_state(self.home)['last']['handed'], 0)
        self.assertIn(E.IDLE, prompts[1])
        self.assertNotIn('先说清上次漏了什么', prompts[1])
        self.assertNotIn(E.IDLE, prompts[0])


class DoneCheckTest(AskFixture):
    """11-10 17:23: a summary said two services went to the background that never did, and a run 23 GB short of
    the target never looked at the disk."""

    def material(self, reason='disk', free=27, goal=50):
        self.made = getattr(self, 'made', 0) + 1
        folder = E.new_folder(self.home, NOW + self.made)   # a folder of its own for each run
        (folder / E.MATERIAL).write_text(json.dumps(dict(
            at=NOW, reason=reason, orphans=[dict(pid=61, lstart='x', cpu_seconds=0)], tasks=[], waiting=[], companions=[],
            simulators=[dict(id='S1', name='Old', system='iOS 27.0', state='Shutdown', gib=4.0)],
            disk=dict(free_gib=free, goal_gib=goal, target_gib=goal + 5, danger_gib=25))))
        return folder

    def test_the_first_done_reads_back_what_was_handed_and_asks_once_when_the_disk_is_still_short(self):
        folder = self.material()
        E.decide(folder, 'stop', '61', why='没人管的 vite，9 天没连接')
        with self.assertRaises(ValueError) as held:
            E.decide(folder, 'done', summary='关了 vite，两个 node 服务交后台复核', cause='磁盘差 23 GB')
        words = str(held.exception)
        self.assertIn('你这轮交回的是：关掉 PID 61', words)
        self.assertIn('不在这里就都不会发生', words)
        self.assertIn('内置盘还差 23 GB 到目标', words)
        self.assertIn('du -xhd1', words)
        E.decide(folder, 'ask', 'simulator-delete:S1', why='关机 9 天没开过')   # it adds what it meant
        done = E.decide(folder, 'done', summary='关了 vite，列了一台旧模拟器', cause='磁盘差 23 GB')
        self.assertEqual(done['handed'], ['关掉 PID 61', '列给用户：Old（iOS 27.0）'])

    def test_it_asks_once_only_and_not_when_there_is_nothing_to_ask(self):
        folder = self.material()
        with self.assertRaises(ValueError) as held:
            E.decide(folder, 'done', summary='都不能动', cause='查了 du')
        self.assertIn('一个决定都没交回', str(held.exception))
        self.assertEqual(E.decide(folder, 'done', summary='查了 ~/Library 和任务目录，都在用', cause='查了 du')['handed'], [])
        fine = self.material(free=80)
        E.decide(fine, 'stop', '61', why='残留')
        self.assertEqual(E.decide(fine, 'done', summary='关了残留', cause='内存')['handed'], ['关掉 PID 61'])
        daily = self.material(reason='daily')
        self.assertEqual(E.decide(daily, 'done', summary='看过了', cause='一天里磁盘一直紧')['handed'], [])

    def test_a_done_carried_back_in_a_reply_is_not_held(self):
        folder = self.material()
        said = 'RG-DECISION {"action": "done", "summary": "都不能动", "cause": "查了 du"}'
        carried = E.carry(folder, said)
        self.assertEqual((carried['received'], len(carried['recorded']), carried['refused']), (1, 1, []))


class BelowTargetTest(AskFixture):
    """11-10: 「把内存目标定到100g…没达到目标的话，agent 要自己去想办法出方案，然后强提醒用户去看」."""

    def test_below_the_target_the_agent_comes_every_six_hours_even_above_the_danger_line(self):
        state = {}
        goal, critical = 100 * GIB, 25 * GIB
        self.assertEqual(E.due(state, 60 * GIB, critical, 1, NOW, goal=goal), 'disk')
        E.mark_fired(state, 'disk', 60 * GIB, critical, NOW)
        self.assertIsNone(E.due(state, 60 * GIB, critical, 1, NOW + 3600, goal=goal))
        self.assertEqual(E.due(state, 60 * GIB, critical, 1, NOW + E.SHORT_HOURS * 3600, goal=goal), 'disk')
        self.assertIsNone(E.due({}, 120 * GIB, critical, 1, NOW, goal=goal))   # at the target: nothing to plan

    def test_a_plan_still_waiting_is_said_again_every_two_hours_until_answered_or_the_disk_is_back(self):
        goal = 100 * GIB
        state = dict(last=dict(at=NOW - 3 * 3600, need=''))
        self.assertIsNone(E.remind(self.home, state, 60 * GIB, goal, NOW))      # nothing waits: nothing to say
        E.add_asks(self.home, [dict(id='simulator-delete:S1', kind='simulator-delete', title='Old', bytes=4 * GIB, why='关机 9 天')], NOW)
        self.assertEqual(E.remind(self.home, state, 60 * GIB, goal, NOW), '1 项等你确认，约 4 GB')
        [notice] = agent_plan.queued_notices(self.home)
        self.assertEqual((notice['title'], notice['tab']), ('离目标还差 40 GB：agent 的方案还等你看', 'pending'))
        self.assertIsNone(E.remind(self.home, state, 60 * GIB, goal, NOW + 3600))
        self.assertIsNotNone(E.remind(self.home, state, 60 * GIB, goal, NOW + E.REMIND_HOURS * 3600))
        self.assertIsNone(E.remind(self.home, state, 120 * GIB, goal, NOW + 9 * 3600))
        self.assertNotIn('reminded_at', state)

    def test_an_idle_task_with_unsaved_work_can_go_to_the_drive_on_the_users_yes(self):
        cfg = self.cfg()
        task = self.task('half-done', 10, dirty=True)
        self.assertIsNotNone(E.move_task(self.home, cfg, self.settings, task, Activity(), {}, unsaved_ok=False))
        folder = E.new_folder(self.home, NOW)
        (folder / E.MATERIAL).write_text(json.dumps(dict(at=NOW, orphans=[], simulators=[], waiting=[], companions=[],
                                                         tasks=[dict(path=str(task), on_drive=False, gib=1.0)])))
        entry = E.decide(folder, 'ask', 'task-move:' + str(task), why='10 天没动，改动没提交，挪走不丢东西')
        [asked] = E.add_asks(self.home, [entry], NOW)
        self.assertIn('不丢东西', asked['loses'])
        self.external.mkdir(parents=True, exist_ok=True)
        with patch.object(E.auto_tier, 'volume_ready', return_value=True), \
                patch.object(E.auto_tier, 'external_access', return_value=None):
            [done] = ask_tier.execute(self.home, cfg, self.settings, [asked['id']], FakeSimctl({}), Activity(), NOW)
        self.assertEqual(done['outcome'], '已完成', done)
        self.assertTrue(task.is_symlink())
        self.assertEqual((task / 'app/main.swift').read_text(), 'changed\n')   # the unsaved work came along


class WorkDriveTest(AskFixture):
    """11-10: 「关键是我500gb，你这些也不解决根本问题啊」 → 「那么就按照你的解法推进吧」: the drive is the work disk."""

    def drive(self):
        plan = agent_plan.load(self.home)
        plan['rules']['idle-tasks'].update(action='work-drive', set_by='user', why='那么就按照你的解法推进吧')
        agent_plan.save(self.home, plan)

    def test_any_task_nobody_uses_goes_to_the_drive_unsaved_work_and_all_even_with_space_to_spare(self):
        import auto_tier
        cfg = self.cfg()
        quiet = self.task('quiet-today', 0, dirty=True); age(quiet, 0.1)        # two hours untouched, unsaved work
        busy = self.task('busy', 0); (busy / 'app/live.txt').write_text('now')
        held = self.task('held', 0); age(held, 0.1)
        plenty = lambda: 400 * GIB
        receipt = self.run_tier(cfg, Activity(open_paths=[(7, str(held / 'app/main.swift'))]), free=plenty)
        self.assertEqual([a['path'] for a in receipt['actions'] if a['kind'] == 'move'], [])   # 「move」: space is fine
        self.drive()
        receipt = self.run_tier(cfg, Activity(open_paths=[(7, str(held / 'app/main.swift'))]), free=plenty)
        moved = [a for a in receipt['actions'] if a['kind'] == 'move' and a['result'] == 'done']
        self.assertEqual([Path(a['path']).name for a in moved], ['quiet-today'])
        self.assertIn('外接盘当工作盘', moved[0]['reason'])
        self.assertTrue(quiet.is_symlink())
        self.assertEqual((quiet / 'app/main.swift').read_text(), 'changed\n')       # nothing lost
        self.assertFalse(busy.is_symlink()); self.assertFalse(held.is_symlink())
        kept = {Path(k['path']).name: k['reason'] for k in receipt['kept']}
        self.assertRegex(kept['busy'], r'^\d+ 分钟内有改写（要求 60 分钟没人动）$')
        self.assertFalse(self.root.is_symlink())                                       # two tasks still here

    def test_a_root_left_with_only_links_becomes_a_link_so_new_tasks_are_made_on_the_drive(self):
        cfg = self.cfg()
        self.drive()
        for name in ('a', 'b'):
            age(self.task(name, 0), 0.2)
        (self.root / '.DS_Store').write_text('x')
        receipt = self.run_tier(cfg, Activity(), free=lambda: 400 * GIB)
        self.assertEqual([a['kind'] for a in receipt['actions'] if a['result'] == 'done'], ['move', 'move', 'root-link'])
        self.assertTrue(self.root.is_symlink())
        self.assertEqual(Path(os.path.realpath(self.root)), Path(os.path.realpath(self.external)))
        self.assertEqual(sorted(p.name for p in self.root.iterdir() if not p.name.startswith('.')), ['a', 'b'])
        import tasks as T
        self.assertTrue(all(t.linked for t in T.discover(cfg)))                        # on the drive, not moved again
        (self.root / 'new-task').mkdir()                                                # a new task lands on the drive
        self.assertTrue((self.external / 'new-task').is_dir())
        again = self.run_tier(cfg, Activity(), free=lambda: 400 * GIB)
        self.assertEqual([a for a in again['actions'] if a['result'] == 'done'], [])

    def test_the_next_rounds_read_a_linked_root_as_on_the_drive_even_with_the_drive_unplugged(self):
        # 11-11: once the root was a link, every load of the settings failed (「与任务根重叠」) and both background
        # jobs stopped for 17 hours; read as resolved, it also looked like a folder of tasks on the internal disk.
        import tasks as T, tiers_config
        self.cfg()
        self.drive()
        age(self.task('only', 0), 0.2)
        self.run_tier(self.cfg(), Activity(), free=lambda: 400 * GIB)
        self.assertTrue(self.root.is_symlink())
        cfg = tiers_config.load(self.home, owned=[self.settings.cache_roots[0], self.settings.state_root])
        self.assertEqual(cfg['task_roots'][0]['path'], self.root)                   # kept as the link, not resolved
        self.assertTrue(all(t.linked for t in T.discover(cfg)))
        again = self.run_tier(cfg, Activity(), free=lambda: 400 * GIB)
        self.assertEqual(again['status'], 'managed-caches')
        unplugged = self.external.with_name('unplugged')
        os.rename(self.external, unplugged)                                         # the drive is gone: a dangling link
        try:
            cfg = tiers_config.load(self.home, owned=[self.settings.cache_roots[0], self.settings.state_root])
            self.assertEqual(cfg['task_roots'][0]['path'], self.root)
            self.assertEqual(T.discover(cfg), [])
        finally:
            os.rename(unplugged, self.external)

    def test_a_setting_that_cannot_be_read_is_said_once_a_day_and_stops_nothing_else(self):
        import tiers_config
        bad = tiers_config.settings_path(self.home); bad.parent.mkdir(parents=True, exist_ok=True); bad.write_text('{broken')
        self.assertIsNone(W.emergency_after(self.home, NOW))
        self.assertIsNone(W.emergency_after(self.home, NOW + 60))
        [notice] = agent_plan.queued_notices(self.home)
        self.assertEqual(notice['title'], '资源看板的设置有误，后台整理停了')

    def test_only_the_user_makes_the_drive_the_work_disk(self):
        with self.assertRaises(ValueError) as refused:
            agent_plan.set_rule(self.home, 'idle-tasks', 'work-drive', by='agent', why='盘不够')
        self.assertIn('emergency propose', str(refused.exception))
        entry = agent_plan.set_rule(self.home, 'idle-tasks', 'work-drive', by='user', words='那么就按照你的解法推进吧')
        self.assertEqual(agent_plan.rule_words('idle-tasks', entry), '外接盘当工作盘：没人在用就整个挪过去，新任务直接建在外接盘上')


class ProposeTest(AskFixture):
    """11-10: 「资源管理 agent 就应该像你这样—根据实际情况给出新的资源整理方案。而不是现在这样很呆板的」."""

    def test_a_new_way_of_doing_things_is_put_to_the_user_loudly_and_kept_until_answered(self):
        folder = E.new_folder(self.home, NOW)
        (folder / E.MATERIAL).write_text(json.dumps(dict(at=NOW, reason='disk', orphans=[], tasks=[], simulators=[], waiting=[],
                                                         companions=[], disk=dict(free_gib=10, goal_gib=100))))
        with self.assertRaises(ValueError):
            E.decide(folder, 'propose', note='外接盘当工作盘', summary='', why='x')
        E.decide(folder, 'propose', note='外接盘当工作盘', why='任务目录 173 GB 在内置盘上，外接盘空 1 TB',
                 summary='任务目录全放外接盘，新任务直接建在那里；能腾约 150 GB；代价是编译稍慢、拔盘时打不开')
        done = E.decide(folder, 'done', summary='提了一个方案', cause='任务都建在内置盘上')   # a plan counts for the disk
        self.assertEqual(done['handed'], ['提给用户的方案：外接盘当工作盘'])
        result = E.apply(self.home, self.cfg(task_roots=[]), self.settings, folder, now=NOW, table={})
        self.assertEqual(result['proposed'], [dict(title='外接盘当工作盘')])
        [notice] = agent_plan.queued_notices(self.home)
        self.assertEqual((notice['title'], notice['tab']), ('新的整理方案：外接盘当工作盘', 'emergency'))
        self.assertIn('能腾约 150 GB', notice['body'])
        self.assertIn('提了 1 个新方案', E.receipt(E.decisions(folder), dict(received=0, recorded=[], refused=[]), result)['words'])
        [kept] = E.proposals(self.home, NOW)
        self.assertEqual(kept['title'], '外接盘当工作盘')
        state = dict(last=dict(at=NOW - 3 * 3600))
        self.assertIn('1 个整理方案等你拿主意：外接盘当工作盘', E.remind(self.home, state, 10 * GIB, 100 * GIB, NOW))
        self.assertEqual(E.proposals(self.home, NOW + E.PROPOSAL_DAYS * 86400), [])   # nobody answered in a week


class ReviewTest(AskFixture):
    """11-10: 「你让资源管理主agent回顾下我的近两周开发任务里，有哪些是影响了运行内存占用的但是不必要的。出个新方案」."""

    def test_the_users_question_brings_the_main_agent_with_two_weeks_of_evidence_and_its_answer_is_said(self):
        old = E.new_folder(self.home, NOW - 10 * 86400)
        (old / E.RESULT).write_text(json.dumps(dict(at=NOW - 10 * 86400, reason='memory', agent=dict(status='done'), done=[], asks=[],
                                                    summary='三台模拟器开着没人连', receipt=dict(handed=0, learned=0))))
        events = self.home / '.local/state/dev-memory/events.jsonl'; events.parent.mkdir(parents=True); events.write_text('{}\n')
        cfg = self.cfg(task_roots=[])
        started = []
        with patch.object(E, 'runners', return_value=['codex']):
            got = E.tick(self.home, cfg, self.settings, NOW, asked=True, words='近两周哪些占内存但不必要', free=500 * GIB,
                         starter=lambda reason, folder, runner: started.append(reason) or 4242)
        self.assertEqual((got['reason'], started), ('review', ['review']))
        state = E.load_state(self.home)
        self.assertEqual(state['run']['words'], '近两周哪些占内存但不必要')
        self.assertEqual((state.get('disk_fired', 0), state.get('short_fired_at')), (0, None))   # costs no emergency step
        seen = []

        def codex(home, folder, prompt, env, timeout, popen=None, main=False):
            seen.append((prompt, main, timeout, json.loads((Path(folder) / E.MATERIAL).read_text())))
            E.decide(folder, 'propose', note='模拟器开着没人连就关', summary='开着 2 小时没人连的模拟器自动关，约省 6 GB 内存',
                     why='10 天里 who 里三台模拟器开着没人连')
            E.decide(folder, 'done', summary='不必要的：三台没人连的模拟器约 6 GB；方案：自动关', cause='who 和事件时间线')
            return dict(agent='codex', status='done', thread='main', said='')

        with patch.object(E, 'run_codex', codex), patch.object(ask_tier, 'launch_app'):
            E.run(self.home, cfg, self.settings, 'review', got['folder'], 'codex', now=NOW)
        prompt, main, timeout, material = seen[0]
        self.assertTrue(main)
        self.assertEqual(timeout, E.REVIEW_MINUTES * 60)
        self.assertIn('「近两周哪些占内存但不必要」', prompt)
        self.assertEqual(material['question'], '近两周哪些占内存但不必要')
        self.assertEqual(material['steps'], list(E.REVIEW_STEPS))
        self.assertEqual([r['summary'] for r in material['rounds']], ['三台模拟器开着没人连'])   # ten days back
        self.assertIn(str(events), [h['path'] for h in material['history']])
        notices = agent_plan.queued_notices(self.home)
        self.assertEqual([n['title'] for n in notices], ['新的整理方案：模拟器开着没人连就关', '主 agent 回答了你的问题'])
        self.assertIn('三台没人连的模拟器约 6 GB', notices[1]['body'])
        state = E.load_state(self.home)
        self.assertEqual(state['review']['words'], '近两周哪些占内存但不必要')
        self.assertNotIn('run', state)


class MemoryCapsTest(AskFixture):
    """11-10, the main agent's look back + 「都改」: 4–6 simulators open at once took 10–35 GB of a 24 GB Mac;
    dev servers nobody connected to waited 12 hours; a Flutter compile (4 GB) was never queued."""

    def rule(self, rid, action, **params):
        plan = agent_plan.load(self.home)
        plan['rules'][rid].update(action=action, set_by='user', why='都改')
        plan['rules'][rid]['params'].update(params)
        agent_plan.save(self.home, plan)

    def test_more_simulators_open_than_the_cap_the_longest_unconnected_ones_are_shut(self):
        cfg = self.cfg(task_roots=[])
        devices = [self.device(u, 'Booted', 0, name=u.title()) for u in ('A', 'B', 'C', 'D')]
        simctl = FakeSimctl({self.IOS: devices})
        busy = Activity(commands=[(9, 'xcodebuild -destination id=A test')])
        self.assertEqual(ask_tier.cap_simulators(self.home, cfg, self.settings, simctl, NOW, busy), [])   # off by default
        self.rule('simulator-cap', 'auto', max_booted=2, quiet_minutes=30)
        self.assertEqual(ask_tier.cap_simulators(self.home, cfg, self.settings, simctl, NOW, busy), [])   # nobody quiet long enough yet
        ask_tier.save(self.home, ask_tier.ASK_STATE, {'unattached_since': {'B': NOW - 3600, 'C': NOW - 7200, 'D': NOW - 600}})
        done = ask_tier.cap_simulators(self.home, cfg, self.settings, simctl, NOW, busy)
        self.assertEqual([r['id'] for r in done], ['simulator-shutdown:C', 'simulator-shutdown:B'])   # 4 open, cap 2
        self.assertNotIn(['shutdown', 'A'], simctl.calls)          # someone is connected to it
        self.assertNotIn(['shutdown', 'D'], simctl.calls)          # quiet only 10 minutes
        [notice] = agent_plan.queued_notices(self.home)
        self.assertEqual(notice['title'], '同时开着 4 台模拟器，关了 2 台没人连的')
        self.assertIn('数据都在，要用再开', notice['body'])
        with self.assertRaises(ValueError):
            agent_plan.set_rule(self.home, 'simulator-cap', 'auto', by='agent', why='省内存')
        self.assertEqual(agent_plan.rule_words('simulator-cap', agent_plan.load(self.home)['rules']['simulator-cap']),
                         '最多同时开 2 台：多出来的，先关没人连满 30 分钟、最久没人用的那台')

    def test_the_watch_applies_the_cap_every_ten_minutes_tight_or_not(self):
        cfg = self.cfg(task_roots=[])
        self.rule('simulator-cap', 'auto')
        seen = []
        cap = lambda home, cfg, settings, simctl, now=None: seen.append(now) or []
        W.auto_after(self.home, cfg, self.settings, NOW, cap=cap, free=500 * GIB)
        W.auto_after(self.home, cfg, self.settings, NOW + 60, cap=cap, free=500 * GIB)
        W.auto_after(self.home, cfg, self.settings, NOW + W.AUTO_GAP, cap=cap, free=500 * GIB)
        self.assertEqual(seen, [NOW, NOW + W.AUTO_GAP])

    def test_dev_servers_go_sooner_and_tight_or_not_once_the_user_says_so(self):
        rule = agent_plan.load(self.home)['rules']['idle-servers']
        self.assertEqual(agent_plan.rule_words('idle-servers', rule), '内存快紧时，起它的会话已经不在、12 小时没人连的就关掉')
        agent_plan.set_rule(self.home, 'idle-servers', 'auto', by='user', words='都改', params=dict(quiet_hours=2))
        rule = agent_plan.load(self.home)['rules']['idle-servers']
        self.assertEqual(agent_plan.rule_words('idle-servers', rule), '起它的会话已经不在、2 小时没人连的就关掉')
        table = {500: dict(pid=500, ppid=1, lstart='Tue Nov 10 10:00:00 2026', command='node node_modules/.bin/vite --port 5253',
                           cpu=1.0, rss=300 * 2 ** 20, age=3 * 3600)}
        state = dict(served={'500': dict(lstart=table[500]['lstart'], cpu=1.0, clients=0, since=NOW - 3 * 3600)})
        stops = []
        with patch.object(W.O, 'owner', return_value=dict(kind='gone')), \
                patch.object(W.E, 'stop_tree', side_effect=lambda table, pid, lstart, **k: stops.append(pid)):
            self.assertEqual(W.idle_servers(state, table, None, None, NOW, set(), hours=12), [])
            [stopped] = W.idle_servers(state, table, None, None, NOW, set(), hours=2)
        self.assertEqual(stops, [500])
        self.assertIn('2 小时以上没人连的开发服务', W.words([stopped], [], [], []))

    def test_a_flutter_compile_is_a_heavy_job_the_analysis_server_is_not(self):
        heavy = lambda command: bool(W.HEAVY.search(command))
        self.assertTrue(heavy('/opt/flutter/bin/cache/dart-sdk/bin/dartaotruntime /opt/flutter/bin/cache/dart-sdk/bin/snapshots/frontend_server_aot.dart.snapshot --sdk-root x'))
        self.assertTrue(heavy('/opt/flutter/bin/cache/dart-sdk/bin/dart /opt/flutter/bin/cache/flutter_tools.snapshot build ios --simulator'))
        self.assertFalse(heavy('/opt/flutter/bin/cache/dart-sdk/bin/dart language-server --protocol=lsp'))


class ToolEnvTest(AskFixture):
    """11-11: several sessions' vitest runs, 10 workers each, filled 24 GB; the user: 「这也应该是agent做的智能决策才可以」
    「不要写死」. Which tool to limit and how far is the agent's call; only system, path and secret variables are not."""

    def setUp(self):
        super().setUp()
        self.calls = []
        self.launchctl = lambda *a: self.calls.append(a) or subprocess.CompletedProcess(a, 0, '', '')

    def test_the_agent_sets_and_lifts_a_tool_limit_with_its_reason_and_the_user_hears_it(self):
        import toolcaches
        folder = E.new_folder(self.home, NOW)
        (folder / E.MATERIAL).write_text(json.dumps(dict(at=NOW, reason='memory', orphans=[], tasks=[], simulators=[], waiting=[], companions=[])))
        for bad in ('PATH', 'DYLD_INSERT_LIBRARIES', 'GITHUB_TOKEN', 'lower'):
            with self.assertRaises(ValueError, msg=bad):
                E.decide(folder, 'env', bad, why='x', note='3')
        E.decide(folder, 'env', 'VITEST_MAX_THREADS', why='vitest 2.1.9 读它；三个会话同时跑测试各开 10 个', note='3')
        E.decide(folder, 'env', 'VITEST_MAX_FORKS', why='同上，forks 池', note='3')
        with patch.object(toolcaches, 'default_launchctl', self.launchctl):
            result = E.apply(self.home, self.cfg(task_roots=[]), self.settings, folder, now=NOW, table={})
        self.assertEqual([d['result'] for d in result['done']], ['已改', '已改'])
        zshenv = (self.home / '.zshenv').read_text()
        self.assertIn('export VITEST_MAX_THREADS="3"', zshenv)
        self.assertEqual(zshenv.count(toolcaches.BEGIN), 1)
        self.assertIn(('setenv', 'VITEST_MAX_FORKS', '3'), self.calls)
        self.assertEqual(subprocess.run(['/bin/zsh', '-c', 'source ~/.zshenv; echo $VITEST_MAX_THREADS'], capture_output=True, text=True,
                                        env=dict(os.environ, HOME=str(self.home))).stdout.strip(), '3')
        [notice] = [n for n in agent_plan.queued_notices(self.home) if 'VITEST_MAX_THREADS' in n['body']]
        self.assertIn('没设 → 3', notice['body'])
        self.assertIn('把 VITEST_MAX_THREADS 设成 3', E.said(E.decisions(folder)[0]))
        toolcaches.set_env(self.home, 'VITEST_MAX_THREADS', None, why='内存一周都宽松', launchctl=self.launchctl)
        self.assertNotIn('VITEST_MAX_THREADS', (self.home / '.zshenv').read_text())
        self.assertIn('VITEST_MAX_FORKS', (self.home / '.zshenv').read_text())
        self.assertIn(('unsetenv', 'VITEST_MAX_THREADS'), self.calls)

    def test_the_xcode_cache_line_and_the_tool_variables_live_in_one_block(self):
        import toolcaches
        toolcaches.set_env(self.home, 'CARGO_BUILD_JOBS', '4', by='user', words='cargo 也限一下', launchctl=self.launchctl)
        toolcaches.enable_xcode_cache(self.home, self.launchctl)
        text = (self.home / '.zshenv').read_text()
        self.assertEqual(text.count(toolcaches.BEGIN), 1)
        self.assertIn('XCODE_XCCONFIG_FILE', text); self.assertIn('export CARGO_BUILD_JOBS="4"', text)
        toolcaches.disable_xcode_cache(self.home, self.launchctl)
        text = (self.home / '.zshenv').read_text()
        self.assertNotIn('XCODE_XCCONFIG_FILE', text); self.assertIn('CARGO_BUILD_JOBS', text)
        toolcaches.set_env(self.home, 'CARGO_BUILD_JOBS', None, by='user', words='不用了', launchctl=self.launchctl)
        self.assertNotIn(toolcaches.BEGIN, (self.home / '.zshenv').read_text())

    def test_a_server_missing_from_one_minutes_read_keeps_its_quiet_clock(self):
        table = {500: dict(pid=500, ppid=1, lstart='L', command='node server.ts', cpu=1.0, rss=1, age=10)}
        state = dict(served={'500': dict(lstart='L', cpu=1.0, clients=0, since=NOW - 3 * 3600)})
        W.served(state, table, {}, NOW)                       # this minute's lsof did not see it
        self.assertEqual(state['served']['500']['since'], NOW - 3 * 3600)
        W.served(state, table, {500: 0}, NOW + 60)
        self.assertEqual(state['served']['500']['since'], NOW - 3 * 3600)
        W.served(state, {}, {}, NOW + 120)                    # gone for real
        self.assertNotIn('500', state['served'])


class MainAgentTest(AskFixture):
    def round(self, at, handed=0):
        folder = E.new_folder(self.home, at)
        (folder / E.RESULT).write_text(json.dumps(dict(at=at, reason='disk', agent=dict(status='done'), done=[], asks=[],
                                                       summary='先说明上轮漏项……', receipt=dict(handed=handed, learned=0))))
        return folder

    def test_once_a_day_from_nine_and_only_with_something_to_look_at(self):
        morning = time.mktime(time.strptime('2026-11-10 10:00', '%Y-%m-%d %H:%M'))
        self.assertFalse(E.daily_due(self.home, {}, morning))           # nothing happened
        self.round(morning - 3600)
        self.assertTrue(E.daily_due(self.home, {}, morning))
        self.assertFalse(E.daily_due(self.home, {}, morning - 3 * 3600))  # 07:00: not before nine
        self.assertFalse(E.daily_due(self.home, dict(daily_fired=morning - 3600), morning))
        rounds = E.past_rounds(self.home, morning - 86400)
        self.assertEqual((len(rounds), rounds[0]['handed'], rounds[0]['summary']), (1, 0, '先说明上轮漏项……'))

    def test_the_main_agent_keeps_its_conversation_and_is_heard_only_with_a_problem(self):
        self.round(NOW - 3600)
        cfg = self.cfg(task_roots=[])
        seen = []

        def codex(home, folder, prompt, env, timeout, popen=None, main=False):
            seen.append((prompt, main, json.loads((Path(folder) / E.MATERIAL).read_text())))
            E.decide(folder, 'done', summary='子 agent 一轮轮照抄', cause='磁盘一直不到目标',
                     need='模拟器能不能以后直接关，要你拍板' if len(seen) == 2 else '')
            return dict(agent='codex', status='done', thread='main', said='')

        for _ in (1, 2):
            folder = E.new_folder(self.home, NOW + len(seen))
            with patch.object(E, 'run_codex', codex), patch.object(ask_tier, 'launch_app'):
                E.run(self.home, cfg, self.settings, 'daily', folder, 'codex', now=NOW)
            if len(seen) == 1:
                self.assertEqual(agent_plan.queued_notices(self.home), [])   # nothing for the user: not a word
        prompt, main, material = seen[0]
        self.assertTrue(main)
        self.assertIn('你是它的主 agent', prompt)
        self.assertEqual(material['steps'], list(E.DAILY_STEPS))
        self.assertEqual(len(material['rounds']), 1)
        [notice] = agent_plan.queued_notices(self.home)
        self.assertIn('要你做：模拟器能不能以后直接关', notice['body'])
        state = E.load_state(self.home)
        self.assertEqual(state['daily']['summary'], '子 agent 一轮轮照抄')
        self.assertNotIn('last', state)                                # the emergencies' own record is untouched


if __name__ == '__main__':
    unittest.main()
