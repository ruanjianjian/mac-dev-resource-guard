"""Space target and plan (space-target-plan): how far below the user's target, and what each tier can free.

The plan only counts space on the internal disk. Moving to the external drive counts as freed.
Three sections, in the order the work happens:
  auto    what the scheduled tiers will do on their own
  ask     what waits for a yes in 「等你确认」 (or from the agent in chat)
  judge   what needs a judgement call the rules cannot make (agent-handoff)
"""
from datetime import datetime
import json
import os
from pathlib import Path
import plistlib
import stat
import time

from storage import GIB, atomic_json
import agent_plan
import auto_tier
import tasks as T

PLAN = 'plan.json'
BASELINE = 'growth-baseline.json'
AGENT_STATE = 'agent-notify.json'
GROWTH_BYTES = 10 * GIB
# A build copy used within this many days is never offered for deletion (user rule, 2026-10-28).
BUILD_COPY_KEEP_DAYS = 7
# The cache-directory standard (https://bford.info/cachedir/): SwiftPM, Cargo and others put it in their build output.
CACHEDIR_SIGNATURE = b'Signature: 8a477f597d28d172789f06886806bc55'


def is_xcode_build(name):
    """DerivedData under any name people and scripts give it: DerivedData-flutter, .codex-w2b-DerivedData…"""
    return 'deriveddata' in name.lower()


def is_cache_tagged(path):
    """Build output its own tool marked as rebuildable with CACHEDIR.TAG, e.g. `swift build --scratch-path` folders."""
    try:
        with open(Path(path) / 'CACHEDIR.TAG', 'rb') as tag:
            return tag.read(len(CACHEDIR_SIGNATURE)) == CACHEDIR_SIGNATURE
    except OSError:
        return False


def is_build_output(path):
    return is_xcode_build(Path(path).name) or is_cache_tagged(path)


def suggested_target(capacity_bytes):
    """About 20% of the disk, rounded to 10 GB, never below 20 GB (RAW-41)."""
    return max(20, int(round(capacity_bytes / GIB * 0.2 / 10.0)) * 10)


def thresholds(target_gib):
    """One number from the user drives all three lines (P01)."""
    target = float(target_gib)
    return dict(warning_free_gib=round(target, 1), target_free_gib=round(target * 1.1, 1),
                critical_free_gib=round(target / 2, 1))


def item(title, bytes_, reason, path=None, kind=None):
    return dict(title=title, bytes=int(bytes_ or 0), reason=reason, path=path, kind=kind)


def duplicate_builds(task_path, depth=5):
    """Build folders inside one task (Xcode DerivedData, or output tagged CACHEDIR.TAG such as one SwiftPM
    scratch folder per test round); more than one usually means old copies are lying around."""
    found, repos = [], []
    base = len(Path(task_path).parts)
    for folder, dirs, files in os.walk(task_path, followlinks=False):
        here = Path(folder)
        if '.git' in dirs or '.git' in files:
            repos.append(here)
        if len(here.parts) > base and 'CACHEDIR.TAG' in files and is_cache_tagged(here):
            # Inside a repository a link in its place would show up in `git status` (a `.build/` rule skips links).
            if not any(repo == here or repo in here.parents for repo in repos):
                found.append(here)
            dirs[:] = []
            continue
        if len(here.parts) - base >= depth:
            dirs[:] = []
            continue
        for name in list(dirs):
            if is_xcode_build(name):
                if not (here / name).is_symlink():
                    found.append(here / name)
                dirs.remove(name)
            elif name in ('.git', 'node_modules', 'Pods'):
                dirs.remove(name)
    return found


def last_used(copy):
    """When Xcode last used this build copy: its own record, the newest build log, or the folder itself."""
    times = []
    try:
        accessed = plistlib.loads((Path(copy) / 'info.plist').read_bytes()).get('LastAccessedDate')
        if isinstance(accessed, datetime):
            times.append(accessed.timestamp())
    except (OSError, ValueError, plistlib.InvalidFileException):
        pass
    for folder in (Path(copy) / 'Logs/Build', Path(copy)):
        try:
            times.append(max([folder.stat().st_mtime] + [entry.stat().st_mtime for entry in folder.iterdir()]))
        except (OSError, ValueError):
            continue
    return max(times) if times else 0


def stale_copies(copies, now):
    """Copies that may be offered: the newest one always stays, and none used within a week."""
    ordered = sorted(copies, key=last_used, reverse=True)
    return [c for c in ordered[1:] if now - last_used(c) >= BUILD_COPY_KEEP_DAYS * 86400]


def cache_still_local(home, path):
    """Recheck stale checkup rows without following links or scanning cache contents."""
    try:
        home, path = Path(home), Path(path)
        parts = path.relative_to(home).parts
        if not parts or '..' in parts:
            return False
        device = home.stat().st_dev
        current = home
        for part in parts:
            current = current / part
            info = current.lstat()
            # Check parents first: even an existing child may now live behind a migrated parent.
            if not stat.S_ISDIR(info.st_mode) or info.st_dev != device:
                return False
        return True
    except (OSError, ValueError):
        return False


def build(home, cfg, settings, report, pending, free_bytes, external_ready, sizer=T.size, now=None):
    """The plan from what the last checkup measured; cheap enough to run every tick."""
    now = time.time() if now is None else now
    target = int(settings.warning_free_gib * GIB)
    auto, ask, judge = [], [], []
    rules = agent_plan.load(home)
    tasks = [t for t in report.get('tasks', []) if not t.get('linked')]
    # 「先别动」 is checked per thing that would move: a whole task, or one build copy inside it.
    whole = [t for t in tasks if not agent_plan.kept(rules, t['path'])]
    if cfg['caches_enabled'] and external_ready:
        for cache in report.get('caches', []):
            if (cache.get('status') == 'local' and cache.get('bytes')
                    and agent_plan.action(rules, 'tool-caches') == 'move'
                    and cache_still_local(home, cache['path'])):
                auto.append(item('把 {} 的缓存搬到外接盘'.format(cache['name']), cache['bytes'], '共享构建缓存', cache['path'], 'cache-move'))
        # Each task's own Xcode build folder goes to the drive too, behind a link (RAW-36);
        # an idle task moves whole below, its builds included.
        for t in (tasks if agent_plan.action(rules, 'task-builds') == 'move' else []):
            if t.get('idle'):
                continue
            for copy in duplicate_builds(t['path']):
                size = 0 if agent_plan.kept(rules, copy) else (sizer(copy) or 0)
                if size:
                    auto.append(item('把 {} 的构建产物挪到外接盘'.format(Path(t['path']).name), size,
                                     '共享构建缓存：不在构建时挪，原路径留链接', str(copy), 'build-move'))
    for entry in pending.get('items', []):
        if entry.get('state') == 'pending' and not entry.get('unreachable'):
            ask.append(dict(item(entry['title'], entry.get('bytes'), entry['reason'], entry['id'], entry['kind']),
                            quiet=bool(entry.get('quiet'))))
    if external_ready and agent_plan.action(rules, 'idle-tasks') == 'move':
        for t in whole:
            if t.get('idle') and t.get('bytes'):
                auto.append(item('把闲置的 {} 挪到外接盘'.format(Path(t['path']).name), t['bytes'],
                                 '闲置满 {} 天；空间不够时整个挪过去，原路径留链接'.format(cfg['auto_idle_days']), t['path'], 'move'))
    busy = sorted((t for t in whole if not t.get('idle') and t.get('bytes')), key=lambda t: -t['bytes'])
    # With the drive in use every build copy is already in 「会自动做的」; deleting old ones frees no internal space.
    for t in ([] if cfg['caches_enabled'] and external_ready else busy[:5]):
        copies = duplicate_builds(t['path'])
        stale = [c for c in (stale_copies(copies, now) if len(copies) > 1 else []) if not agent_plan.kept(rules, c)]
        if stale:
            oldest = time.strftime('%m-%d', time.localtime(min(last_used(c) for c in stale)))
            judge.append(item('{} 里有 {} 份一周没用的构建副本'.format(Path(t['path']).name, len(stale)),
                              sum(sizer(c) or 0 for c in stale),
                              '最早一份上次用在 {}；一周内用过的和最新的一份不列'.format(oldest), t['path'], 'duplicate-builds'))
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
            plan['suggestions'].append(item('插一块外接盘', 0, '缓存、构建产物和闲置任务可以挪过去，内置盘只放正在做的', None, 'drive'))
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


def agent_needed(home, plan, grown, now=None, critical_bytes=0):
    """Wake the agent when the scripts are done but the target is still missed, or something new grew fast:
    at most once a day. Dropping below the critical line is said at once, once per drop (space-target-plan AC-02),
    unless 「等你确认」 has items, whose own notice already says so.

    Returns dict(title, reason), or None.
    """
    now = time.time() if now is None else now
    sections = {s['id']: s['bytes'] for s in plan['sections']}
    auto_left = sections['auto']
    # 「只列出、不催」 items stay on the page but never nudge, in the daily notice as in the drop below the line.
    ask = next((s for s in plan['sections'] if s['id'] == 'ask'), {})
    loud = sum(i['bytes'] for i in ask['items'] if not i.get('quiet')) if 'items' in ask else sections.get('ask', 0)
    others = sections.get('judge', 0) + loud + sum(i['bytes'] for i in plan['suggestions'])
    path = auto_tier.state_root(home) / AGENT_STATE
    try:
        state = json.loads(path.read_text())
    except (OSError, ValueError):
        state = {}
    before = dict(state)
    today = time.strftime('%Y-%m-%d', time.localtime(now))
    low = bool(critical_bytes) and plan.get('free_bytes', critical_bytes) < critical_bytes
    notice = None
    if not low:
        state.pop('critical_at', None)
    elif not state.get('critical_at'):
        state['critical_at'] = now
        if not loud:
            notice = dict(title='磁盘快满了', reason='内置盘只剩 {:.0f} GB，低于危险线 {:.0f} GB；{}'.format(
                plan['free_bytes'] / GIB, critical_bytes / GIB,
                '后台正在把构建产物挪到外接盘，可能还不够' if auto_left else '自动能做的都做完了'))
    if notice is None and state.get('day') != today:
        if plan['gap_bytes'] and not auto_left and (others or plan['suggestions']):
            notice = dict(title='有新占用需要判断', reason='自动能做的都做完了，离目标还差 {:.0f} GB'.format(plan['gap_bytes'] / GIB))
        elif grown:
            top = max(grown, key=lambda g: g['bytes'])
            notice = dict(title='有新占用需要判断', reason='{} 一天涨了 {:.0f} GB'.format(
                Path(top['key'].split(':', 1)[1]).name or top['key'], top['bytes'] / GIB))
    if notice:
        state.update(day=today, at=now, reason=notice['reason'])
    else:
        # agent-review: when no review has happened for a week (or a scheduled one was missed), nudge once a week.
        due = agent_plan.review_due(home, now)
        if due and now - state.get('review_at', 0) >= agent_plan.WEEK:
            done = agent_plan.reviews(home)
            since = done[-1]['at'] if done else agent_plan.load(home)['meta']['started_at']
            decided = sum(1 for m in agent_plan.memories(home) if m.get('kind') == 'decision' and m.get('at', 0) > since)
            notice = dict(title='资源看板第一周复查' if due == 'first' else '资源看板每周复查',
                          reason='该复查计划了：{}记了 {} 条你的决定'.format('这一周' if due == 'first' else '上次复查以来', decided))
            state['review_at'] = now
    if state != before:
        atomic_json(path, state)
    return notice


AGENT_PROMPT = ('用 mac-dev-resource-guard 这个 skill 帮我看一下磁盘空间：先运行 '
                '`~/.local/bin/resource-guard-service review` 看这段时间记下的决定和现在的计划，按里面的步骤复查；'
                '再运行 `~/.local/bin/resource-guard-service plan --json` 读计划，按 skill 里的步骤处理'
                '「等你点头」和「需要判断」两段，每一项先跟我确认再执行。')
