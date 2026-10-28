"""Space target, plan, agent handoff, Mole guard, companion skill install, agent-requested prune."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import unittest
import unittest.mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_tiers import DAY, ROOT, TierFixture, age, git
sys.path.insert(0, str(ROOT / 'scripts'))
import mole
import plan as space_plan
import service
import tiers_config
from portable_config import configure
from storage import Activity

GIB = 1 << 30


def sizer(sizes):
    return lambda path: sizes.get(Path(path).name, 1 << 20)


class PlanTest(TierFixture):
    def setUp(self):
        super().setUp()
        self.settings.warning_free_gib = 50

    def busy_task_with_copies(self):
        task = self.root / '20261026-layout'
        for folder in ('.mobile-v/native/DerivedData', 'DerivedData-flutter', '.codex-w2b-DerivedData'):
            (task / folder / 'Build').mkdir(parents=True)
        return task

    def report(self, tasks, caches=()):
        return dict(tasks=tasks, caches=list(caches), categories=[])

    def test_one_number_drives_the_three_lines(self):
        self.assertEqual(space_plan.thresholds(50), dict(warning_free_gib=50.0, target_free_gib=55.0, critical_free_gib=25.0))
        self.assertEqual(space_plan.suggested_target(460 * GIB), 50)
        self.assertEqual(space_plan.suggested_target(100 * GIB), 20)

    def test_three_sections_and_how_far_they_reach(self):
        busy = self.busy_task_with_copies()
        report = self.report(
            [dict(path=str(self.root / '20261001-idle'), linked=False, bytes=12 * GIB, idle=True),
             dict(path=str(busy), linked=False, bytes=40 * GIB, idle=False),
             dict(path=str(self.root / '20261002-moved'), linked=True, bytes=None, idle=False)],
            [dict(status='local', bytes=2 * GIB, name='npm', path='/x/.npm')])
        pending = dict(items=[dict(id='simulator-delete:A', kind='simulator-delete', title='iPhone', bytes=GIB, reason='12 天没开', state='pending'),
                              dict(id='x', kind='task-delete', title='done', bytes=5 * GIB, reason='', state='ignored')])
        cfg = self.cfg(caches_enabled=True)
        result = space_plan.build(self.home, cfg, self.settings, report, pending, 20 * GIB, True,
                                  sizer=lambda p: 8 * GIB if 'DerivedData' in Path(p).name else 1 << 20)
        sections = {s['id']: s for s in result['sections']}
        self.assertEqual(sections['auto']['bytes'], 14 * GIB)            # idle task move + npm cache move
        self.assertEqual(sections['ask']['bytes'], GIB)                  # ignored items do not count
        self.assertEqual(sections['judge']['bytes'], 16 * GIB)           # two spare copies of three
        self.assertIn('3 份 Xcode 构建产物', sections['judge']['items'][0]['title'])
        self.assertEqual(result['gap_bytes'], 30 * GIB)
        self.assertEqual(result['reach_bytes'], 51 * GIB)
        self.assertEqual(result['shortfall_bytes'], 0)

    def test_shortfall_is_said_with_suggestions_not_wider_rules(self):
        report = self.report([dict(path=str(self.root / 'big'), linked=False, bytes=54 * GIB, idle=False)])
        result = space_plan.build(self.home, self.cfg(), self.settings, report, dict(items=[]), 10 * GIB, False)
        self.assertEqual(result['shortfall_bytes'], 40 * GIB)
        titles = [s['title'] for s in result['suggestions']]
        self.assertEqual(titles, ['收尾不再用的任务 big', '插一块外接盘'])

    def test_agent_is_woken_once_a_day_for_the_unexpected_only(self):
        quiet = dict(gap_bytes=0, sections=[dict(id='auto', bytes=0), dict(id='ask', bytes=0), dict(id='judge', bytes=0)], suggestions=[])
        self.assertIsNone(space_plan.agent_needed(self.home, quiet, []))
        still_auto = dict(quiet, gap_bytes=GIB, sections=[dict(id='auto', bytes=GIB), dict(id='ask', bytes=GIB), dict(id='judge', bytes=0)])
        self.assertIsNone(space_plan.agent_needed(self.home, still_auto, []), 'scripts still have work to do')
        stuck = dict(quiet, gap_bytes=9 * GIB, sections=[dict(id='auto', bytes=0), dict(id='ask', bytes=GIB), dict(id='judge', bytes=0)])
        self.assertIn('还差 9 GB', space_plan.agent_needed(self.home, stuck, []))
        self.assertIsNone(space_plan.agent_needed(self.home, stuck, []), 'at most once a day')

    def test_fast_growth_is_noticed_against_a_daily_baseline(self):
        now = time.time()
        first = self.report([dict(path=str(self.root / 'a'), linked=False, bytes=5 * GIB, idle=False)])
        self.assertEqual(space_plan.growth(self.home, first, now), [])
        later = self.report([dict(path=str(self.root / 'a'), linked=False, bytes=17 * GIB, idle=False)])
        grown = space_plan.growth(self.home, later, now + 3600)
        self.assertEqual(grown[0]['bytes'], 12 * GIB)
        quiet = dict(gap_bytes=0, sections=[dict(id='auto', bytes=0), dict(id='ask', bytes=0), dict(id='judge', bytes=0)], suggestions=[])
        self.assertIn('一天涨了 12 GB', space_plan.agent_needed(self.home, quiet, grown, now + 3600))

    def test_target_is_validated_and_written_as_one_set(self):
        home = self.home
        with unittest.mock.patch.object(service.Path, 'home', return_value=home):
            body = service.set_target(home, 50)
        self.assertEqual(body['warning_free_gib'], 50.0)
        self.assertEqual(configure(home).warning_free_gib, 50.0)
        with self.assertRaises(ValueError):
            service.set_target(home, 1)


class MoleGuardTest(TierFixture):
    def fake_mole(self, defaults=True):
        root = self.home / 'mole'; (root / 'lib/core').mkdir(parents=True, exist_ok=True)
        (root / 'mo').write_text('#!/bin/sh\n'); os.chmod(root / 'mo', 0o755)
        body = 'declare -a DEFAULT_WHITELIST_PATTERNS=(\n    "$HOME/.gradle/caches/*"\n    "$HOME/.ollama/models/*"\n    "$FINDER_METADATA_SENTINEL"\n)\n'
        (root / 'lib/core/base.sh').write_text(body if defaults else 'nothing\n')
        return lambda name: str(root / 'mo') if name == 'mo' else None

    def cache_rows(self):
        return [dict(status='linked', path=str(self.home / '.npm'), dest='/Volumes/T9/dev-cache/npm'),
                dict(status='local', path=str(self.home / 'Library/Caches/pip'), dest=None)]

    def test_first_write_keeps_moles_defaults_and_our_block_is_idempotent(self):
        cfg = self.cfg(external_root=str(self.home / 'drive/dev-resource-guard'))
        patterns = mole.guard(self.home, cfg, self.cache_rows(), which=self.fake_mole())
        text = mole.whitelist_path(self.home).read_text()
        self.assertIn(str(self.home) + '/.ollama/models/*', text)
        self.assertNotIn('FINDER', text)
        self.assertIn('/Volumes/T9/dev-cache/npm/*', patterns)
        self.assertIn(str(self.root) + '/*', patterns)
        self.assertNotIn(str(self.home / 'Library/Caches/pip'), patterns, 'local caches are not ours to protect')
        mole.guard(self.home, cfg, self.cache_rows(), which=self.fake_mole())
        self.assertEqual(mole.whitelist_path(self.home).read_text().count(mole.BEGIN), 1)
        mole.guard(self.home, cfg, [], remove=True)
        after = mole.whitelist_path(self.home).read_text()
        self.assertNotIn(mole.BEGIN, after)
        self.assertIn('.ollama/models', after)

    def test_users_own_lines_stay_and_missing_defaults_refuse(self):
        path = mole.whitelist_path(self.home); path.parent.mkdir(parents=True)
        path.write_text('/Users/me/keep-this/*\n')
        mole.guard(self.home, self.cfg(), [], which=lambda name: None)
        self.assertTrue(path.read_text().startswith('/Users/me/keep-this/*\n'))
        path.unlink()
        with self.assertRaises(ValueError):
            mole.guard(self.home, self.cfg(), [], which=self.fake_mole(defaults=False))
        self.assertFalse(path.exists())


class SkillInstallTest(TierFixture):
    def source(self):
        src = self.home / 'src'; (src / 'references').mkdir(parents=True, exist_ok=True); (src / 'tests').mkdir(exist_ok=True)
        (src / 'SKILL.md').write_text('new skill'); (src / 'references/install.md').write_text('x')
        return src

    def clone(self, dirty=False):
        old = self.home / '.codex/skills/mac-dev-resource-guard'; old.mkdir(parents=True)
        git(old, 'init', '-q'); (old / 'SKILL.md').write_text('old'); git(old, 'add', '.'); git(old, 'commit', '-qm', 'old')
        if dirty:
            (old / 'SKILL.md').write_text('my edit')
        return old

    def test_clean_old_clone_is_backed_up_and_replaced_and_claude_linked(self):
        old = self.clone()
        notes = service.install_skill(self.home, self.source())
        self.assertEqual((old / 'SKILL.md').read_text(), 'new skill')
        self.assertFalse((old / 'tests').exists())
        self.assertTrue((old / service.SKILL_MARKER).exists())
        self.assertIn('旧 skill 已备份', notes[0])
        claude = self.home / '.claude/skills/mac-dev-resource-guard'
        self.assertEqual(claude.resolve(), old.resolve())
        self.assertEqual(service.install_skill(self.home, self.source()), [], 'upgrades replace our own copy quietly')

    def test_clone_with_local_changes_is_kept(self):
        old = self.clone(dirty=True)
        notes = service.install_skill(self.home, self.source())
        self.assertEqual((old / 'SKILL.md').read_text(), 'my edit')
        self.assertIn('有本地改动', notes[0])


class AgentPruneTest(TierFixture):
    def test_agent_may_remove_a_build_copy_inside_a_task_only(self):
        self.cfg()
        copy = self.root / '20261026-layout/.mobile-v/old/DerivedData'; (copy / 'Build').mkdir(parents=True)
        (copy / 'Build/out.o').write_bytes(b'x' * 4096)
        with unittest.mock.patch.object(service.Activity, 'collect', classmethod(lambda cls, root=None: Activity())):
            result = service.prune_output(self.home, copy)
            self.assertFalse(copy.exists())
            self.assertEqual(result['removed'], str(copy.resolve()))
            source = self.root / '20261026-layout/app'; source.mkdir(parents=True)
            with self.assertRaises(ValueError):
                service.prune_output(self.home, source)
            outside = self.home / 'elsewhere/DerivedData'; outside.mkdir(parents=True)
            with self.assertRaises(ValueError):
                service.prune_output(self.home, outside)
        busy = self.root / '20261026-layout/.mobile-v/native/DerivedData'; busy.mkdir(parents=True)
        using = Activity(); using.open_paths.append((7, str(busy.resolve()) + '/Build/x'))
        with unittest.mock.patch.object(service.Activity, 'collect', classmethod(lambda cls, root=None: using)):
            with self.assertRaises(ValueError):
                service.prune_output(self.home, busy)
        self.assertTrue(busy.exists())


class BackendPlanTest(TierFixture):
    def backend(self, *args):
        env = dict(os.environ, HOME=str(self.home))
        out = subprocess.run([sys.executable, str(ROOT / 'widget/backend.py'), *args, '--home', str(self.home)],
                             capture_output=True, text=True, env=env)
        return json.loads(out.stdout)

    def test_target_plan_rows_and_handoff_words(self):
        (self.home / '.resource-dashboard-fixture').touch()
        self.task('20261001-old', idle_days=5)
        tiers_config.update(self.home, dict(task_roots=[dict(path=str(self.root))]))
        self.assertEqual(self.backend('checkup-scan')['status'], 'ok')
        reply = self.backend('plan-target', '--path', '60')
        target = next(r for r in reply['rows'] if r['category'] == '目标')
        self.assertEqual(target['title'], '目标 60 GB')
        self.assertEqual([r['title'] for r in reply['rows'] if r['category'] == '计划'], ['会自动做的', '等你点头的', '需要判断的'])
        self.assertTrue(reply['message'].startswith('全做完能到'))
        handoff = self.backend('plan-handoff')
        self.assertIn('resource-guard-service plan --json', handoff['message'])


if __name__ == '__main__':
    unittest.main()
