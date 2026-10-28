"""Space target and plan (space-target-plan): how far below the user's target, and what each tier can free.

The plan only counts space on the internal disk. Moving to the external drive counts as freed.
Three sections, in the order the work happens:
  auto    what the scheduled tiers will do on their own
  ask     what waits for a yes in 「等你确认」 (or from the agent in chat)
  judge   what needs a judgement call the rules cannot make (agent-handoff)
"""
import json
import os
from pathlib import Path
import time

from storage import GIB, atomic_json
import auto_tier
import tasks as T

PLAN = 'plan.json'
BASELINE = 'growth-baseline.json'
AGENT_STATE = 'agent-notify.json'
GROWTH_BYTES = 10 * GIB
def is_xcode_build(name):
    """DerivedData under any name people and scripts give it: DerivedData-flutter, .codex-w2b-DerivedData…"""
    return 'deriveddata' in name.lower()


def suggested_target(capacity_bytes):
    """About 10% of the disk, rounded to 10 GB, never below 20 GB."""
    return max(20, int(round(capacity_bytes / GIB / 10 / 10.0)) * 10)


def thresholds(target_gib):
    """One number from the user drives all three lines (P01)."""
    target = float(target_gib)
    return dict(warning_free_gib=round(target, 1), target_free_gib=round(target * 1.1, 1),
                critical_free_gib=round(target / 2, 1))


def item(title, bytes_, reason, path=None, kind=None):
    return dict(title=title, bytes=int(bytes_ or 0), reason=reason, path=path, kind=kind)


def duplicate_builds(task_path, depth=4):
    """Xcode build folders inside one task; more than one usually means old copies are lying around."""
    found = []
    base = len(Path(task_path).parts)
    for folder, dirs, _ in os.walk(task_path, followlinks=False):
        if len(Path(folder).parts) - base >= depth:
            dirs[:] = []
            continue
        for name in list(dirs):
            if is_xcode_build(name):
                found.append(Path(folder) / name)
                dirs.remove(name)
            elif name in ('.git', 'node_modules', 'Pods'):
                dirs.remove(name)
    return found


def build(home, cfg, settings, report, pending, free_bytes, external_ready, sizer=T.size, now=None):
    """The plan from what the last checkup measured; cheap enough to run every tick."""
    now = time.time() if now is None else now
    target = int(settings.warning_free_gib * GIB)
    auto, ask, judge = [], [], []
    tasks = [t for t in report.get('tasks', []) if not t.get('linked')]
    for t in tasks:
        if not t.get('idle'):
            continue
        name = Path(t['path']).name
        if external_ready:
            auto.append(item('把 {} 挪到外接盘'.format(name), t.get('bytes'), '闲置满 {} 天'.format(cfg['auto_idle_days']), t['path'], 'move'))
        else:
            outputs = T.rebuildable(T.Task(Path(t['path']).parent, Path(t['path']), Path(t['path']).resolve()), cfg)
            bytes_ = sum(sizer(o) or 0 for o in outputs)
            if bytes_:
                auto.append(item('删 {} 里能重新生成的构建产物'.format(name), bytes_, '闲置，没有外接盘', t['path'], 'prune'))
    if cfg['caches_enabled'] and external_ready:
        for cache in report.get('caches', []):
            if cache.get('status') == 'local' and cache.get('bytes'):
                auto.append(item('把 {} 的缓存搬到外接盘'.format(cache['name']), cache['bytes'], '共享构建缓存', cache['path'], 'cache-move'))
    for entry in pending.get('items', []):
        if entry.get('state') == 'pending' and not entry.get('unreachable'):
            ask.append(item(entry['title'], entry.get('bytes'), entry['reason'], entry['id'], entry['kind']))
    busy = sorted((t for t in tasks if not t.get('idle') and t.get('bytes')), key=lambda t: -t['bytes'])
    for t in busy[:5]:
        copies = duplicate_builds(t['path'])
        if len(copies) > 1:
            sizes = sorted(((sizer(c) or 0, c) for c in copies), reverse=True)
            spare = sum(s for s, _ in sizes[1:])
            judge.append(item('{} 里有 {} 份 Xcode 构建产物'.format(Path(t['path']).name, len(copies)), spare,
                              '最近还在用；确认哪几份不再需要后可以删', t['path'], 'duplicate-builds'))
    for category in report.get('categories', []):
        if category['id'].startswith('container-') and category.get('bytes'):
            judge.append(item(category['title'] + ' 的镜像和容器', 0, category.get('note', ''), None, 'container'))
    sections = [
        dict(id='auto', title='会自动做的', items=sorted(auto, key=lambda i: -i['bytes'])),
        dict(id='ask', title='等你点头的', items=sorted(ask, key=lambda i: -i['bytes'])),
        dict(id='judge', title='需要判断的', items=sorted(judge, key=lambda i: -i['bytes'])),
    ]
    for section in sections:
        section['bytes'] = sum(i['bytes'] for i in section['items'])
    reach = free_bytes + sum(s['bytes'] for s in sections)
    plan = dict(schema=1, at=now, target_gib=settings.warning_free_gib, free_bytes=free_bytes, target_bytes=target,
                gap_bytes=max(0, target - free_bytes), reach_bytes=reach, shortfall_bytes=max(0, target - reach),
                sections=sections, suggestions=[])
    if plan['shortfall_bytes']:
        for t in busy[:3]:
            plan['suggestions'].append(item('收尾不再用的任务 ' + Path(t['path']).name, t['bytes'],
                                            '最近还动过，工具不会自己处理；不再需要的话可以交给 agent 收尾', t['path'], 'task'))
        if not external_ready:
            plan['suggestions'].append(item('插一块外接盘', 0, '闲置任务和缓存可以挪过去，内置盘只放正在做的', None, 'drive'))
    return plan


def save(home, plan):
    auto_tier.state_root(home).mkdir(parents=True, exist_ok=True)
    atomic_json(auto_tier.state_root(home) / PLAN, plan)


def load(home):
    try:
        return json.loads((auto_tier.state_root(home) / PLAN).read_text())
    except (OSError, ValueError):
        return None


def growth(home, report, now=None):
    """Task folders and categories that grew by 10 GB or more within a day (agent-handoff AC-05)."""
    now = time.time() if now is None else now
    path = auto_tier.state_root(home) / BASELINE
    try:
        base = json.loads(path.read_text())
    except (OSError, ValueError):
        base = None
    sizes = {('task:' + t['path']): t['bytes'] for t in report.get('tasks', []) if t.get('bytes') and not t.get('linked')}
    sizes.update({('category:' + c['id']): c['bytes'] for c in report.get('categories', []) if c.get('bytes')})
    grown = []
    if base and now - base.get('at', 0) <= 1.5 * 86400:
        for key, value in sizes.items():
            before = base['sizes'].get(key)
            if before is not None and value - before >= GROWTH_BYTES:
                grown.append(dict(key=key, bytes=value - before))
    if base is None or now - base.get('at', 0) >= 86400:
        atomic_json(path, dict(at=now, sizes=sizes))
    return grown


def agent_needed(home, plan, grown, now=None):
    """Wake the agent when the scripts are done but the target is still missed, or something new grew fast.

    At most once a day. Returns the reason, or None.
    """
    now = time.time() if now is None else now
    auto_left = next(s for s in plan['sections'] if s['id'] == 'auto')['bytes']
    others = sum(s['bytes'] for s in plan['sections'] if s['id'] != 'auto') + sum(i['bytes'] for i in plan['suggestions'])
    reason = None
    if plan['gap_bytes'] and not auto_left and (others or plan['suggestions']):
        reason = '自动能做的都做完了，离目标还差 {:.0f} GB'.format(plan['gap_bytes'] / GIB)
    elif grown:
        top = max(grown, key=lambda g: g['bytes'])
        reason = '{} 一天涨了 {:.0f} GB'.format(Path(top['key'].split(':', 1)[1]).name or top['key'], top['bytes'] / GIB)
    if not reason:
        return None
    path = auto_tier.state_root(home) / AGENT_STATE
    today = time.strftime('%Y-%m-%d', time.localtime(now))
    try:
        if json.loads(path.read_text()).get('day') == today:
            return None
    except (OSError, ValueError):
        pass
    atomic_json(path, dict(day=today, at=now, reason=reason))
    return reason


AGENT_PROMPT = ('用 mac-dev-resource-guard 这个 skill 帮我看一下磁盘空间：先运行 '
                '`~/.local/bin/resource-guard-service plan --json` 读计划，按 skill 里的步骤处理'
                '「等你点头」和「需要判断」两段，每一项先跟我确认再执行。')
