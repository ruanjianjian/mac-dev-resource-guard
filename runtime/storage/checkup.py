"""The checkup page (checkup-home): where the space went, three suggestions, one confirmation."""
import fcntl
import json
import os
from pathlib import Path
import shutil
import time

from storage import GIB, atomic_json
import auto_tier
import discover
import spaces
import tasks as T
import tiers_config
import toolcaches

REPORT = 'checkup.json'
SUGGESTIONS = ('caches', 'clean', 'external')


def report_path(home):
    return auto_tier.state_root(home) / REPORT


def load_report(home):
    try:
        return json.loads(report_path(home).read_text())
    except (OSError, ValueError):
        return None


def _where(path):
    real = os.path.realpath(path)
    return '外接盘' if toolcaches.volume_of(real) else '内置盘'


def _sum(values):
    """0 for nothing found; None only when something exists but could not be measured."""
    values = list(values)
    known = [v for v in values if v is not None]
    return sum(known) if known or not values else None


def task_rows(roots, now, idle_days):
    """Each task folder once: size on the internal disk, how many already sit on the drive, which are idle."""
    rows = []
    for root in roots:
        try:
            children = sorted(c for c in Path(root['path']).iterdir() if not c.name.startswith('.'))
        except OSError:
            continue
        for child in children:
            if child.is_symlink():
                rows.append(dict(path=str(child), linked=True, bytes=None, idle=False))
            elif child.is_dir():
                try:
                    latest = T.latest_write(child, T.skip_names({'prune_names': []}), stop_after=now - idle_days * T.DAY)
                    idle = now - latest >= idle_days * T.DAY
                except T.Unreadable:
                    idle = False
                rows.append(dict(path=str(child), linked=False, bytes=T.size(child), idle=idle))
    return rows


def scan(home, cfg, simulators_bytes=None, now=None, volumes=Path('/Volumes'), info=discover.disk_info, is_mount=os.path.ismount,
         foreground=False):
    """One checkup at a time: a second caller waits and takes the result that just finished."""
    started = time.time()
    lock = auto_tier.state_root(home) / 'checkup.lock'
    lock.parent.mkdir(parents=True, exist_ok=True)
    with lock.open('a') as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        report = load_report(home)
        if report and report.get('finished', 0) >= started:
            return report
        return _scan(home, cfg, simulators_bytes, now, volumes, info, is_mount, foreground)


def _scan(home, cfg, simulators_bytes, now, volumes, info, is_mount, foreground):
    """Measure and discover; slow (du over the task folders), so the result is kept in checkup.json."""
    home = Path(home)
    now = time.time() if now is None else now
    # A background checkup reads nothing on external drives or in Downloads (macOS would ask and wait).
    found = discover.scan(home, [dict(path=str(r['path']), external=r['external']) for r in cfg['task_roots']], volumes, info, is_mount,
                          look_inside=foreground)
    task_list = task_rows(found['roots'], now, cfg['auto_idle_days'])
    caches = toolcaches.inspect(home, cfg['external_root'], touch_external=foreground)
    xcode = home / 'Library/Developer/Xcode'
    derived = [c for c in caches if c['tool'] == 'xcode']
    if simulators_bytes is None:
        devices = home / 'Library/Developer/CoreSimulator/Devices'
        simulators_bytes = T.size(devices) if devices.is_dir() else 0
    installers = spaces.installers(home, now, foreground=foreground)
    support = spaces.device_support(home, now)
    archives = spaces.archives(home, now)
    trash = spaces.trash_size(home)
    internal_tasks = [t for t in task_list if not t['linked']]
    categories = [
        dict(id='tasks', title='任务目录', bytes=_sum(t['bytes'] for t in internal_tasks), where='内置盘',
             handling='只扫描；收尾和删除先问你',
             note='{} 个在内置盘，{} 个已在外接盘'.format(len(internal_tasks), len(task_list) - len(internal_tasks))),
        dict(id='simulators', title='模拟器', bytes=simulators_bytes, where='内置盘', handling='先问你',
             note='模拟器不能放外接盘'),
        dict(id='xcode', title='Xcode 构建缓存', bytes=_sum(c['bytes'] for c in derived),
             where=derived[0]['where'] if derived else '内置盘', handling='自动清过期的；共享后放外接盘', note=''),
        dict(id='toolcaches', title='各工具缓存', bytes=_sum(c['bytes'] for c in caches if c['tool'] != 'xcode'),
             where='、'.join(sorted({c['where'] for c in caches if c['tool'] != 'xcode'})) or '内置盘',
             handling='共享后放外接盘', note='、'.join(sorted({c['name'] for c in caches if c['tool'] != 'xcode'}))),
        dict(id='device-support', title='Xcode 设备支持文件', bytes=_sum(s['bytes'] for s in support), where='内置盘',
             handling='{} 天没用的先问你'.format(spaces.DEVICE_SUPPORT_IDLE_DAYS), note='{} 个系统版本'.format(len(support))),
        dict(id='archives', title='Xcode 归档', bytes=_sum(a['bytes'] for a in archives), where='内置盘',
             handling='{} 天前的先问你'.format(spaces.ARCHIVE_AGE_DAYS), note='{} 个'.format(len(archives))),
        dict(id='installers', title='下载里的安装包', bytes=_sum(i['bytes'] for i in installers), where='内置盘',
             handling='{} 天没动的先问你'.format(spaces.INSTALLER_IDLE_DAYS), note='{} 个'.format(len(installers))),
        dict(id='trash', title='废纸篓', bytes=trash, where='内置盘', handling='超过 1 GB 先问你',
             note='' if trash is not None else 'macOS 不让读取废纸篓大小；可以在访达里清空'),
    ]
    for item in spaces.containers(home):
        categories.append(dict(id='container-' + item['name'].lower(), title=item['name'], bytes=item['bytes'], where='内置盘',
                               handling='只提醒', note=item['note']))
    categories = [c for c in categories if c['bytes'] or c['id'] in ('tasks', 'trash') or c['bytes'] is None]
    report = dict(schema=1, at=now, roots=found['roots'], drives=found['drives'], recommended=found['recommended'],
                  categories=sorted(categories, key=lambda c: -(c['bytes'] or 0)), caches=caches, tasks=task_list,
                  notified_version=(load_report(home) or {}).get('notified_version'))
    report['finished'] = time.time()
    auto_tier.state_root(home).mkdir(parents=True, exist_ok=True)
    atomic_json(report_path(home), report)
    return report


def estimate(report, cfg, pending_bytes=0):
    """What turning everything on would free on the internal disk, counting only what the rules really move."""
    drive = cfg['external_root'] or report.get('recommended')
    freed = pending_bytes
    if drive:
        freed += sum(c['bytes'] or 0 for c in report['caches'] if c['status'] == 'local')
    return freed


def drive_label(path):
    """「T9」上的 dev-resource-guard/tasks/wt — the drive's name first, which is what people recognise."""
    volume = toolcaches.volume_of(str(path))
    if volume:
        rest = str(Path(str(path)).relative_to(volume)) if str(path) != volume else ''
        return '「{}」{}'.format(Path(volume).name, ('上的 ' + rest) if rest else '')
    return str(path)


def suggestion_state(home, cfg, report):
    drive = report.get('recommended')
    external = cfg['external_root']
    mounted = external is not None and auto_tier.volume_ready(external)
    local = [c for c in report['caches'] if c['status'] == 'local']
    states = dict(
        caches=dict(on=cfg['caches_enabled'], available=True,
                    detail='{} 个工具的缓存统一管理{}；打开 Xcode 编译缓存'.format(
                        len({c['tool'] for c in report['caches']}),
                        '，{} 个搬到外接盘'.format(len({c['tool'] for c in local})) if (external or drive) and local else '')),
        clean=dict(on=cfg['auto_enabled'] and cfg['ask_enabled'], available=True,
                   detail='每 12 小时清理至少 7 天未用的编译中间缓存；任务目录和模拟器保留'),
        external=dict(on=external is not None, available=external is not None or drive is not None,
                      detail=('使用{}'.format(drive_label(external)) + ('' if mounted else '（未连接）')) if external
                      else ('使用「{}」，剩 {:.0f} GB'.format(drive['name'], drive['free'] / GIB) if drive else '没有插外接盘')),
    )
    return states


def disconnected(home, cfg, is_mount=os.path.ismount):
    """What is unavailable because the drive in use is not plugged in (external-drive AC-02/03)."""
    volumes = set()
    if cfg['external_root']:
        volumes.add(toolcaches.volume_of(str(cfg['external_root'])))
    for root in cfg['task_roots']:
        if root['external']:
            volumes.add(toolcaches.volume_of(str(root['external'])))
    result = []
    for volume in sorted(v for v in volumes if v):
        if is_mount(volume):
            continue
        caches = [c['name'] for c in toolcaches.inspect(home, sizes=False, touch_external=False)
                  if c.get('dest') and c['dest'].startswith(volume + '/')]
        tasks = 0
        for root in cfg['task_roots']:
            try:
                tasks += sum(1 for c in Path(root['path']).iterdir() if c.is_symlink() and os.readlink(c).startswith(volume + '/'))
            except OSError:
                continue
        result.append(dict(volume=volume, name=Path(volume).name, caches=sorted(set(caches)), tasks=tasks))
    return result


def apply(home, ids, owned=(), probe=auto_tier.external_access, launchctl=toolcaches.default_launchctl):
    """Turn suggestions on. Returns (cfg, notes) — notes explain anything that could not be done."""
    home = Path(home)
    report = load_report(home)
    if report is None:
        raise ValueError('请先体检')
    cfg = tiers_config.load(home, owned)
    changes, notes = {}, []
    roots = [dict(path=str(r['path']), **({'external': str(r['external'])} if r['external'] else {})) for r in cfg['task_roots']]
    known = {r['path'] for r in roots}
    for found in report['roots']:
        if found['path'] not in known and not any(Path(k) in Path(found['path']).parents for k in known):
            roots.append(dict(path=found['path']))
            known.add(found['path'])
    external = cfg['external_root']
    if 'external' in ids and external is None:
        drive = report.get('recommended')
        if drive is None:
            notes.append('没有插可用的外接盘，「使用外接盘」没有打开')
        else:
            external = discover.managed_external(drive['path'])
    if 'external' in ids and external is not None:
        denied = probe(external) if auto_tier.volume_ready(external) else '外接盘未连接'
        if denied:
            notes.append(denied)
            external = cfg['external_root']
        else:
            changes['external_root'] = str(external)
            used = {Path(r['external']).name for r in roots if r.get('external')}
            for root in roots:
                if not root.get('external'):
                    name = Path(root['path']).name
                    while name in used:
                        name += '-1'
                    used.add(name)
                    root['external'] = str(Path(external) / 'tasks' / name)
    if 'clean' in ids:
        changes.update(auto_enabled=True, ask_enabled=True, auto_scope='managed-caches')
    if 'clean' in ids or 'external' in changes:
        changes['task_roots'] = roots
    if 'caches' in ids:
        changes['caches_enabled'] = True
        try:
            toolcaches.enable_xcode_cache(home, launchctl)
            changes['xcode_compile_cache'] = True
        except toolcaches.Conflict as error:
            notes.append(str(error))
    if not changes:
        return cfg, notes
    updated = tiers_config.update(home, changes, owned)
    if 'clean' in ids:
        (auto_tier.state_root(home) / 'storage-paused').unlink(missing_ok=True)
    return updated, notes


def turn_off(home, which, owned=(), launchctl=toolcaches.default_launchctl):
    """Undo one suggestion. Turning caches off restores every tool's own location."""
    if which == 'caches':
        restored = toolcaches.restore(home)
        toolcaches.disable_xcode_cache(home, launchctl)
        tiers_config.update(home, dict(caches_enabled=False, xcode_compile_cache=False), owned)
        return '已还原 {} 处缓存位置，Xcode 编译缓存已关闭；各工具会重新下载或生成缓存。'.format(len(restored))
    if which == 'clean':
        tiers_config.update(home, dict(auto_enabled=False, ask_enabled=False), owned)
        return '自动清理和询问都已关闭；已挪到外接盘的任务目录保持原样。'
    if which == 'external':
        raw = tiers_config.read_raw(home)
        roots = [{k: v for k, v in r.items() if k != 'external'} for r in raw.get('task_roots', [])]
        tiers_config.update(home, dict(external_root=None, task_roots=roots), owned)
        return '不再往外接盘放东西；已经放过去的保持原样，原位置的链接照常可用。'
    raise ValueError('不认识的建议：' + which)


def first_run_message(report, freed, states=None):
    on = [name for key, name in (('caches', '共享构建缓存'), ('clean', '自动清理'), ('external', '外接盘'))
          if states and states[key]['on']]
    if on:
        return '{}都已开好。空间去哪了，点菜单栏图标看。'.format('、'.join(on))
    if freed >= GIB:
        return '把「帮你管着」里的开关打开，预计可腾出约 {:.0f} GB。'.format(freed / GIB)
    return '空间去哪了、可以怎么省，都在面板里。'


def free_space(volume):
    return shutil.disk_usage(str(volume)).free
