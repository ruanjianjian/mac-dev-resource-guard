"""The resource-management agent's plan and memory (agent-plan epic, RAW-01).

The plan is a list of rules: for each kind of thing, whether the background moves it to the drive, cleans it,
asks first, lists it quietly or leaves it alone, and who set that and why. The user's own agent changes the
rules with commands, from what it remembers of the user's decisions. What no rule can change stays in the code
that carries the rules out: things in use and unsaved work are kept, and nothing is lost without asking
(RAW-05). Only this Mac keeps the plan and the memory; every agent reads the same copy (RAW-03).
"""
import contextlib
import fcntl
import functools
import json
import os
from pathlib import Path
import threading
import time

from storage import atomic_json

# The safety floor: what each kind of thing may be set to. No kind may be deleted without the user's yes.
RULES = {
    'tool-caches': dict(title='各开发工具的缓存', actions=('move', 'off'), default='move'),
    'task-builds': dict(title='任务里的构建产物', actions=('move', 'off'), default='move'),
    'xcode-intermediates': dict(title='7 天没用的 Xcode 编译中间缓存', actions=('clean', 'off'), default='clean'),
    'idle-tasks': dict(title='闲置且改动都已推送的任务', actions=('move', 'off'), default='move'),
    'simulator-shutdown': dict(title='开着却没人用的模拟器', actions=('ask', 'quiet', 'off'), default='ask',
                               # A simulator held only by a screen or log stream this many hours counts as unused.
                               params={'stream_hours': 12}),
    'simulator-delete': dict(title='不可用或很久没开的模拟器', actions=('ask', 'quiet', 'off'), default='ask'),
    'runtime-delete': dict(title='没有模拟器在用的运行时', actions=('ask', 'quiet', 'off'), default='ask'),
    'task-delete': dict(title='闲置很久的任务', actions=('ask', 'quiet', 'off'), default='ask'),
    'installer-delete': dict(title='下载里的大安装包', actions=('ask', 'quiet', 'off'), default='ask'),
    'trash-empty': dict(title='废纸篓', actions=('ask', 'quiet', 'off'), default='ask'),
    'device-support-delete': dict(title='旧版本的设备支持文件', actions=('ask', 'quiet', 'off'), default='ask'),
    'archive-delete': dict(title='旧的归档', actions=('ask', 'quiet', 'off'), default='ask'),
}
ACTION_WORDS = {'move': '挪到外接盘', 'clean': '自动清', 'ask': '先问', 'quiet': '只列出、不催', 'off': '不管'}
# Switches and numbers the user sets in 「帮你管着」; the agent may change them unless locked (RAW-07).
SETTINGS = {'target': '空间目标', 'caches': '共享构建缓存', 'clean': '自动清理', 'external': '外接盘',
            'update': '自动更新', 'ask': '等你确认'}
KINDS = ('decision', 'lesson')
PLAN, MEMORY, CHANGES, NOTICES, REVIEWS = 'rules.json', 'memory.jsonl', 'plan-changes.jsonl', 'notices', 'reviews.jsonl'
WEEK = 7 * 86400
# plan-settle (RAW-09): this many reviews in a row without a change, and no correction by the user meanwhile.
SETTLE_REVIEWS = 4
# The user's decisions that correct the plan, as opposed to answering what it asked.
CORRECTIONS = ('switched', 'set', 'keep')
META = ('started_at', 'round_at', 'settled_at', 'schedule')


def plan_path(home):
    return Path(home) / '.config/dev-resource-guard' / PLAN


def state_dir(home):
    return Path(home) / '.local/state/dev-resource-guard'


_held = threading.local()


@contextlib.contextmanager
def _locked(home):
    """One writer at a time: the background round and the agent's commands both read, change and save the plan
    and append memories; without this one of two overlapping changes was lost, or two memories shared an id."""
    if getattr(_held, 'depth', 0):
        _held.depth += 1
        try:
            yield
        finally:
            _held.depth -= 1
        return
    path = state_dir(home) / 'plan.lock'
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, 'a') as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        _held.depth = 1
        try:
            yield
        finally:
            _held.depth = 0


def _writes(function):
    @functools.wraps(function)
    def wrapper(home, *args, **kwargs):
        with _locked(home):
            return function(home, *args, **kwargs)
    return wrapper


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _clean_meta(got):
    """Who set something and why, keeping only values of the right type: a hand-edited file never crashes a round."""
    entry = dict(set_by='default', why='', evidence=[], locked=False, at=None)
    if isinstance(got, dict):
        if isinstance(got.get('set_by'), str):
            entry['set_by'] = got['set_by']
        if isinstance(got.get('why'), str):
            entry['why'] = got['why']
        if isinstance(got.get('evidence'), list):
            entry['evidence'] = [str(e) for e in got['evidence']]
        if isinstance(got.get('locked'), bool):
            entry['locked'] = got['locked']
        if _number(got.get('at')):
            entry['at'] = got['at']
    return entry


def _dict(value):
    return value if isinstance(value, dict) else {}


def load(home):
    """The plan with every rule and setting present. A missing or unreadable file means today's defaults; wrong
    values inside a readable one are dropped one by one, so a damaged entry never takes the rest with it."""
    try:
        raw = json.loads(plan_path(home).read_text())
    except (OSError, ValueError):
        raw = {}
    raw = _dict(raw)
    rules = {}
    for rid, spec in RULES.items():
        got = _dict(_dict(raw.get('rules')).get(rid))
        entry = dict(_clean_meta(got), action=spec['default'], params=dict(spec.get('params', {})))
        if got.get('action') in spec['actions']:
            entry['action'] = got['action']
        else:
            entry.update(set_by='default', why='', evidence=[], at=None)
        for key, value in _dict(got.get('params')).items():
            if key in entry['params'] and _number(value) and value > 0:
                entry['params'][key] = value
        rules[rid] = entry
    settings = {name: _clean_meta(_dict(raw.get('settings')).get(name)) for name in SETTINGS}
    keep = []
    for entry in raw.get('keep') if isinstance(raw.get('keep'), list) else []:
        if isinstance(entry, dict) and isinstance(entry.get('path'), str) and os.path.isabs(entry['path']):
            keep.append(dict(_clean_meta(entry), path=entry['path']))
    meta = dict.fromkeys(META)
    for key, value in _dict(raw.get('meta')).items():
        if key == 'schedule' and isinstance(value, dict) and value.get('agent') in ('claude', 'codex'):
            meta[key] = dict(agent=value['agent'], at=value.get('at') if _number(value.get('at')) else None)
        elif key in META and key != 'schedule' and _number(value):
            meta[key] = value
    version = raw.get('version') if _number(raw.get('version')) else 0
    return dict(schema=1, version=version, rules=rules, settings=settings, keep=keep, meta=meta)


def save(home, plan, bump=True):
    plan['version'] = plan.get('version', 0) + (1 if bump else 0)
    path = plan_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_json(path, plan)


def _append(home, name, entry):
    path = state_dir(home) / name
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, 'a') as out:
        out.write(json.dumps(entry, ensure_ascii=False) + '\n')


def _read_lines(home, name):
    try:
        lines = (state_dir(home) / name).read_text().splitlines()
    except OSError:
        return []
    out = []
    for line in lines:
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict):
            out.append(entry)
    return out


def action(plan, kind):
    """What the plan says to do with this kind of thing; kinds without a rule are asked about."""
    return plan['rules'][kind]['action'] if kind in plan['rules'] else 'ask'


def _forms(path):
    path = os.path.abspath(os.path.expanduser(str(path)))
    return {os.path.normpath(path), os.path.realpath(path)}


def _inside(path, root):
    return path == root or path.startswith(root.rstrip('/') + '/')


def kept(plan, path):
    """The 「先别动」 entry that touches this path, or None. Touching means the path is kept, lies in a kept folder,
    or holds something kept: moving or deleting a whole folder would take a kept part of it along."""
    if not path:
        return None
    given = _forms(path)
    for entry in plan['keep']:
        for root in _forms(entry['path']):
            if any(_inside(p, root) or _inside(root, p) for p in given):
                return entry
    return None


def notice(home, title, body, tab='cover'):
    """Queue one notification for the menu app. Each is its own file, so two changes between the app's looks are
    both said, and the app removes only the ones it posted."""
    folder = state_dir(home) / NOTICES
    folder.mkdir(parents=True, exist_ok=True)
    atomic_json(folder / '{}-{}.json'.format(time.time_ns(), os.getpid()), dict(at=time.time(), title=title, body=body, tab=tab))


def queued_notices(home):
    folder = state_dir(home) / NOTICES
    out = []
    for path in sorted(folder.glob('*.json')) if folder.is_dir() else []:
        try:
            out.append(json.loads(path.read_text()))
        except (OSError, ValueError):
            continue
    return out


def _changed(home, by, what, key, title, before, after, why, evidence):
    entry = dict(at=time.time(), by=by, what=what, id=key, before=before, after=after, why=why, evidence=list(evidence))
    _append(home, CHANGES, entry)
    if by == 'agent':
        # Every change the agent makes is said once (RAW-07), with its reason.
        notice(home, '资源看板的计划改了', '{}：{} → {}。{}'.format(title, before, after, why))
    return entry


def changes(home):
    return _read_lines(home, CHANGES)


def _check_agent(entry, title, why, plan=None):
    if not str(why or '').strip():
        raise ValueError('agent 改计划要写明为什么')
    if entry['locked']:
        raise ValueError('「{}」被用户锁住了，agent 不能改；把理由告诉用户，请用户决定要不要解锁'.format(title))
    if plan and plan['meta']['settled_at']:
        raise ValueError('计划已在 {} 定稿：只有出现新类型的占用、换了硬盘或外接盘、或用户要求时才改。确有这种情况，'
                         '先运行 review reopen --why "原因"'.format(time.strftime('%m-%d', time.localtime(plan['meta']['settled_at']))))


def _check_user(words):
    if not str(words or '').strip():
        raise ValueError('用户亲口要求的改动要带上原话（--words "…"）')


def _user_reopens(home, plan, why):
    """The user changing the plan themselves corrects it: a settled plan opens again (plan-settle)."""
    if plan['meta']['settled_at']:
        reopen(home, why, by='user')


def rule_words(rid, entry):
    """The action as people read it, with its setting where there is one, so a change of hours is visible too."""
    words = ACTION_WORDS[entry['action']]
    hours = entry['params'].get('stream_hours')
    if rid == 'simulator-shutdown' and hours:
        words += '（看画面或日志超过 {:g} 小时算没人用）'.format(hours)
    return words


@_writes
def set_rule(home, rid, new_action, by='agent', why='', evidence=(), params=None, words=''):
    if rid not in RULES:
        raise ValueError('没有这条规则：{}；可选：{}'.format(rid, '、'.join(RULES)))
    spec = RULES[rid]
    if new_action not in spec['actions']:
        raise ValueError('「{}」只能设成：{}（安全底线，改不动）'.format(
            spec['title'], '、'.join('{}（{}）'.format(a, ACTION_WORDS[a]) for a in spec['actions'])))
    plan = load(home)
    entry = plan['rules'][rid]
    for key, value in (params or {}).items():
        if key not in entry['params']:
            raise ValueError('「{}」没有参数 {}'.format(spec['title'], key))
        if not _number(value) or value <= 0:
            raise ValueError('{} 必须是正数'.format(key))
    if by == 'agent':
        _check_agent(entry, spec['title'], why, plan)
    else:
        _check_user(words)
        _user_reopens(home, plan, '用户自己把「{}」改成{}'.format(spec['title'], ACTION_WORDS[new_action]))
        plan = load(home)
        entry = plan['rules'][rid]
    before = rule_words(rid, entry)
    entry.update(action=new_action, set_by=by, why=why or words, evidence=list(evidence), at=time.time())
    entry['params'].update(params or {})
    save(home, plan)
    _changed(home, by, 'rule', rid, spec['title'], before, rule_words(rid, entry), why or words, evidence)
    return entry


@_writes
def note_setting(home, name, by, why='', evidence=(), before=None, after=None):
    """A setting just changed (by the user in 「帮你管着」 or by the agent): remember who and why."""
    if name not in SETTINGS:
        raise ValueError('没有这个设置：{}；可选：{}'.format(name, '、'.join(SETTINGS)))
    plan = load(home)
    entry = plan['settings'][name]
    if by == 'agent':
        _check_agent(entry, SETTINGS[name], why, plan)
    elif before != after:
        _user_reopens(home, plan, '用户自己把「{}」从{}改成{}'.format(SETTINGS[name], before, after))
        plan = load(home)
        entry = plan['settings'][name]
    entry.update(set_by=by, why=why, evidence=list(evidence), at=time.time())
    save(home, plan)
    if before != after:
        _changed(home, by, 'setting', name, SETTINGS[name], before, after, why, evidence)
    return entry


def check_setting(home, name, why):
    """Raises unless the agent may change this setting now."""
    if name not in SETTINGS:
        raise ValueError('没有这个设置：{}；可选：{}'.format(name, '、'.join(SETTINGS)))
    plan = load(home)
    _check_agent(plan['settings'][name], SETTINGS[name], why, plan)


@_writes
def set_lock(home, key, locked, words='', source='chat', by='user'):
    """Only the user locks or unlocks (RAW-07), in their own words; it is remembered as their decision."""
    if by != 'user':
        raise ValueError('只有用户能锁住或解锁；用户这样说了，就带上 --by user --words "原话"')
    _check_user(words)
    plan = load(home)
    if key in plan['rules']:
        entry, title, what = plan['rules'][key], RULES[key]['title'], 'rule'
    elif key in plan['settings']:
        entry, title, what = plan['settings'][key], SETTINGS[key], 'setting'
    else:
        raise ValueError('没有这条规则或设置：' + key)
    before = '锁住' if entry['locked'] else '未锁'
    entry['locked'] = bool(locked)
    save(home, plan)
    remember(home, 'decision', key, decision='locked' if locked else 'unlocked', words=words, source=source,
             note='{}「{}」'.format('锁住' if locked else '解锁', title))
    _changed(home, 'user', what, key, title, before, '锁住' if locked else '未锁', words, ())
    return entry


@_writes
def add_keep(home, path, why, by='agent', evidence=()):
    if by == 'agent' and not str(why or '').strip():
        raise ValueError('要写明为什么先别动')
    plan = load(home)
    path = os.path.normpath(os.path.abspath(os.path.expanduser(str(path))))
    same = [k for k in plan['keep'] if _forms(k['path']) & _forms(path)]
    if by == 'agent':
        if any(k.get('set_by') == 'user' for k in same):
            return next(k for k in same if k.get('set_by') == 'user')   # the user's own keep stays theirs
        if plan['meta']['settled_at']:
            _check_agent(dict(locked=False), '先别动', why, plan)
    else:
        _check_user(why)
        _user_reopens(home, plan, '用户说先别动：' + path)
        plan = load(home)
    plan['keep'] = [k for k in plan['keep'] if not (_forms(k['path']) & _forms(path))]
    plan['keep'].append(dict(path=path, why=why, set_by=by, evidence=list(evidence), at=time.time(), locked=False))
    save(home, plan)
    _changed(home, by, 'keep', path, '先别动：' + Path(path).name, '无', '先别动', why, evidence)
    return plan['keep'][-1]


@_writes
def remove_keep(home, path, by='agent', why=''):
    plan = load(home)
    path = os.path.normpath(os.path.abspath(os.path.expanduser(str(path))))
    entry = next((k for k in plan['keep'] if _forms(k['path']) & _forms(path)), None)
    if entry is None:
        raise ValueError('「先别动」名单里没有：' + path)
    if by == 'agent':
        if entry.get('set_by') == 'user':
            raise ValueError('这是用户说的「先别动」，agent 不能拿掉；请用户决定')
        _check_agent(dict(locked=False), '先别动', why or '拿掉', plan)
    else:
        _check_user(why)
    plan['keep'].remove(entry)
    save(home, plan)
    _changed(home, by, 'keep', path, '先别动：' + Path(path).name, '先别动', '无', why, ())
    return entry


@_writes
def remember(home, kind, about, note='', words='', path=None, decision=None, source='agent'):
    """One memory: a decision of the user's (with their own words when there are any) or a lesson (RAW-04)."""
    if kind not in KINDS:
        raise ValueError('记忆只有两种：decision（用户的决定）或 lesson（学到的教训）')
    if not str(about or '').strip():
        raise ValueError('要写明是关于什么的（规则名、设置名，或一类东西）')
    if not (str(note or '').strip() or str(words or '').strip()):
        raise ValueError('要写下用户的原话或说明')
    entry = dict(id='m{}'.format(len(_read_lines(home, MEMORY)) + 1), at=time.time(), kind=kind, source=source,
                 about=str(about), decision=decision, path=str(path) if path else None, words=words, note=note)
    _append(home, MEMORY, entry)
    return entry


def memories(home, about=None, last=None):
    found = [m for m in _read_lines(home, MEMORY) if about is None or m.get('about') == about]
    return found[-last:] if last else found


@_writes
def ensure_started(home, now=None):
    """The first plan's start (first-plan): the first review is due a week after it."""
    plan = load(home)
    if not plan['meta']['started_at']:
        plan['meta']['started_at'] = plan['meta']['round_at'] = time.time() if now is None else now
        save(home, plan, bump=False)
    return plan['meta']['started_at']


def reviews(home):
    return _read_lines(home, REVIEWS)


@_writes
def note_schedule(home, agent, now=None):
    """The user's agent put a weekly review in its own scheduler (agent-review); the dashboard's weekly nudge
    then only comes if that review is missed for more than a few days."""
    if agent not in ('claude', 'codex'):
        raise ValueError('只认 claude 或 codex 的定时任务')
    plan = load(home)
    plan['meta']['schedule'] = dict(agent=agent, at=time.time() if now is None else now)
    save(home, plan, bump=False)
    return plan['meta']['schedule']


def next_review_at(home):
    """When the next review is owed: a week after the last one (or after the first plan started)."""
    plan = load(home)
    done = [r for r in reviews(home) if _number(r.get('at'))]
    last = done[-1]['at'] if done else plan['meta']['started_at']
    return last + WEEK if last else None


def review_due(home, now=None):
    """'first' or 'weekly' when a review is owed, else None. A scheduled review gets two days' grace."""
    now = time.time() if now is None else now
    plan = load(home)
    owed = next_review_at(home)
    if owed is None or now < owed + (2 * 86400 if plan['meta']['schedule'] else 0):
        return None
    return 'weekly' if reviews(home) else 'first'


REVIEW_STEPS = (
    '复查这台 Mac 的资源管理计划：1）看 memories（上次复查以来用户的决定和你记下的教训）和 changes（计划改过什么）；'
    '2）用户反复做同一种选择的，按它改规则（rule … --why … --evidence …），设得不对的设置也可以改，锁住的不碰；'
    '3）发现一类新的占用，记一条 lesson；4）运行 review done --changed yes/no --summary "三句话以内"，再把这三句话告诉用户。')


def review_packet(home, now=None):
    """What the agent reads to review the plan (agent-review): what happened since the last review."""
    now = time.time() if now is None else now
    plan = load(home)
    done = reviews(home)
    since = done[-1]['at'] if done else (plan['meta']['started_at'] or 0)
    steps = REVIEW_STEPS
    if not done:
        steps = '这是第一周结束的复查：出计划的修订版，告诉用户这一周学到了什么、改了什么、为什么。' + steps
    if plan['meta']['settled_at']:
        steps = ('计划已定稿：只看有没有出现新类型的占用、是否换了硬盘或外接盘。没有就 review done --changed no；'
                 '有就先 review reopen --why "原因"，再按下面做。') + steps
    owed = next_review_at(home)
    if owed and now < owed:
        # A weekly schedule fires every Monday; the first review waits for a full week of the user's choices.
        steps = ('还没到复查时间（下次是 {}）。如果是定时任务叫你来的，回复一句「还没到复查时间」就结束，不运行 review done；'
                 '用户主动让你看计划时再照下面做。').format(time.strftime('%m-%d', time.localtime(owed))) + steps
    return dict(since=since, first_review=not done, reviews_done=len(done), settled_at=plan['meta']['settled_at'],
                due=bool(owed and now >= owed), next_review_at=owed,
                schedule=plan['meta']['schedule'], steps=steps,
                memories=[m for m in memories(home) if m.get('at', 0) > since],
                changes=[c for c in changes(home) if c.get('at', 0) > since], plan=describe(home))


@_writes
def finish_review(home, changed, summary, now=None):
    """One review done; settle the plan when it has stopped changing (plan-settle)."""
    if not str(summary or '').strip():
        raise ValueError('写一句复查结论（--summary），也要告诉用户')
    now = time.time() if now is None else now
    entry = dict(at=now, changed=bool(changed), summary=summary)
    _append(home, REVIEWS, entry)
    return dict(review=entry, settled_at=maybe_settle(home, now))


@_writes
def maybe_settle(home, now=None):
    now = time.time() if now is None else now
    plan = load(home)
    if plan['meta']['settled_at']:
        return plan['meta']['settled_at']
    # Only this round counts: after the plan was opened again, the quiet weeks start over.
    round_at = plan['meta']['round_at'] or 0
    recent = [r for r in reviews(home) if _number(r.get('at')) and r['at'] > round_at][-SETTLE_REVIEWS:]
    if len(recent) < SETTLE_REVIEWS or any(r.get('changed') for r in recent):
        return None
    since = recent[0]['at']
    if any(m.get('kind') == 'decision' and m.get('decision') in CORRECTIONS and m.get('at', 0) >= since
           for m in memories(home)):
        return None   # the user still corrects the plan by hand
    if any(c.get('what') in ('rule', 'setting', 'keep') and c.get('by') in ('agent', 'user') and c.get('at', 0) >= since
           for c in changes(home)):
        return None   # a review said "no change" while the plan did change
    plan['meta']['settled_at'] = now
    save(home, plan)
    _changed(home, 'review', 'plan', 'settle', '计划', '调整中', '定稿', '连续 {} 次复查没改'.format(SETTLE_REVIEWS), ())
    notice(home, '资源看板的计划定稿了', '连续 {} 次复查都没改，这期间你也没有手动纠正过。以后只在出现新类型的占用、'
                                        '换了硬盘或外接盘、或你要求时才改。'.format(SETTLE_REVIEWS))
    return now


@_writes
def reopen(home, why, by='agent'):
    """A settled plan opens again: something new appeared, the drive changed, or the user asked."""
    if not str(why or '').strip():
        raise ValueError('写明为什么重新打开计划（新类型的占用、换了盘，或用户要求）')
    plan = load(home)
    if not plan['meta']['settled_at']:
        return None
    plan['meta']['settled_at'] = None
    plan['meta']['round_at'] = time.time()
    save(home, plan)
    return _changed(home, by, 'plan', 'settle', '计划', '定稿', '重新打开', why, ())


def describe(home, cfg=None):
    """The plan as the agent and the panel read it: each rule with its words, who set it and why."""
    plan = load(home)
    rules = [dict(id=rid, title=RULES[rid]['title'], action=e['action'], words=rule_words(rid, e),
                  choices=list(RULES[rid]['actions']), **{k: e[k] for k in ('set_by', 'why', 'evidence', 'locked', 'at', 'params')})
             for rid, e in plan['rules'].items()]
    settings = [dict(id=name, title=SETTINGS[name], **plan['settings'][name]) for name in SETTINGS]
    return dict(version=plan['version'], rules=rules, settings=settings, keep=plan['keep'], meta=plan['meta'],
                memories=len(_read_lines(home, MEMORY)), changes=len(_read_lines(home, CHANGES)),
                reviews=len(_read_lines(home, REVIEWS)))
