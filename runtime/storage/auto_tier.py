"""Auto tier (design C2/C3): only actions that lose nothing.

Below the warning line, idle task directories move to the external drive (a link
keeps the old path), or lose their rebuildable outputs when the drive is absent;
then unavailable simulators and tools' own caches are cleaned. Stops at the target.
"""
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import time

from storage import GIB, Activity, StorageManager, atomic_json
import tasks as T

RECEIPT, LOG, MOVE_STATE = 'auto-tier.json', 'auto-tier-log.jsonl', 'auto-tier-move.json'
LOG_KEEP = 2000
# Tool caches are cleaned only with the tool's own command, and never while that tool runs.
TOOLS = (
    ('go', ['go', 'env', 'GOCACHE'], ['go', 'clean', '-cache'], r'(^|/)go (build|test|run|vet|install)\b|gopls'),
    ('npm', ['npm', 'config', 'get', 'cache'], ['npm', 'cache', 'verify'], r'(^|/)npm (ci|install|i|exec)\b'),
    ('pnpm', ['pnpm', 'store', 'path'], ['pnpm', 'store', 'prune'], r'(^|/)pnpm (i|install|add)\b'),
)


class Aborted(Exception):
    """The move gave up safely; the original directory is untouched."""


def state_root(home):
    return Path(home) / '.local/state/dev-resource-guard'


def volume_ready(path):
    parts = Path(path).parts
    if len(parts) > 2 and parts[1] == 'Volumes':
        return os.path.ismount(os.path.join('/', parts[1], parts[2]))
    return True


def snapshot(path):
    """Entry count, file bytes and newest mtime, never following links."""
    count = total = 0
    newest = 0.0
    for folder, dirs, files in os.walk(path, followlinks=False):
        for name in dirs + files:
            try:
                st = os.lstat(os.path.join(folder, name))
            except OSError:
                continue
            count += 1
            newest = max(newest, st.st_mtime)
            if not stat.S_ISDIR(st.st_mode):
                total += st.st_size
    return count, total, newest


def move(home, task, hook=None):
    """Copy, verify nothing changed, then swap in a link. Crash states are in MOVE_STATE."""
    src, dest_dir = task.path, Path(task.external)
    dest = dest_dir / src.name
    tmp = dest_dir / ('.guard-moving-' + src.name)
    if dest.exists() or dest.is_symlink():
        raise Aborted('外接盘上已有同名目录：' + str(dest))
    dest_dir.mkdir(parents=True, exist_ok=True)
    if tmp.exists():
        shutil.rmtree(tmp)
    marker = state_root(home) / MOVE_STATE
    before = snapshot(src)
    atomic_json(marker, dict(src=str(src), dest=str(dest), tmp=str(tmp), phase='copying', at=time.time()))
    try:
        subprocess.run(['/usr/bin/ditto', str(src), str(tmp)], check=True, capture_output=True, timeout=6 * 3600)
        if hook:
            hook()
        after, copied = snapshot(src), snapshot(tmp)
        if before != after or after[:2] != copied[:2]:
            raise Aborted('搬迁中目录有变化，已放弃，下一轮再试')
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        marker.unlink(missing_ok=True)
        raise
    atomic_json(marker, dict(src=str(src), dest=str(dest), tmp=str(tmp), phase='swapping', at=time.time()))
    os.rename(tmp, dest)
    old = src.with_name('.guard-old-' + src.name)
    os.rename(src, old)
    os.symlink(dest, src)
    shutil.rmtree(old)
    marker.unlink(missing_ok=True)
    return dest


def recover(home):
    """Finish or undo an interrupted move so the original or its link always exists."""
    marker = state_root(home) / MOVE_STATE
    if not marker.is_file():
        return None
    s = json.loads(marker.read_text())
    src, dest, tmp = Path(s['src']), Path(s['dest']), Path(s['tmp'])
    old = src.with_name('.guard-old-' + src.name)
    if src.is_symlink():
        outcome = '上次搬迁已完成，清理旧副本'
        if old.exists():
            shutil.rmtree(old)
    elif src.is_dir():
        outcome = '上次搬迁未完成，原目录保留，删除外接盘上的不完整副本'
        shutil.rmtree(tmp, ignore_errors=True)
        if s['phase'] == 'swapping' and dest.is_dir() and not old.exists():
            shutil.rmtree(dest, ignore_errors=True)
    elif old.is_dir() and dest.is_dir():
        os.symlink(dest, src)
        shutil.rmtree(old)
        outcome = '上次搬迁停在换链接前，已补上链接'
    else:
        # Never guess with data at stake; keep the marker for a person to look at.
        return '上次搬迁状态无法判断，保留现场：' + str(src)
    marker.unlink()
    return outcome


def unavailable_simulators(simctl):
    listing = json.loads(simctl(['list', 'devices', '--json']))
    found = []
    for runtime, devices in listing.get('devices', {}).items():
        for device in devices:
            if device.get('isAvailable') is False:
                found.append(device)
    return found


def default_simctl(arguments):
    out = subprocess.run(['xcrun', 'simctl', *arguments], capture_output=True, text=True, timeout=600)
    if out.returncode:
        raise RuntimeError((out.stderr or out.stdout).strip() or 'simctl failed')
    return out.stdout


def tool_caches(home, activity, run=subprocess.run):
    """Run each tool's own cleanup when its cache sits on the home volume and the tool is idle."""
    actions = []
    home_dev = os.stat(home).st_dev
    for name, locate, clean, busy in TOOLS:
        if not shutil.which(name):
            continue
        try:
            where = run(locate, capture_output=True, text=True, timeout=60).stdout.strip()
            if not where or not os.path.isdir(where) or os.stat(where).st_dev != home_dev:
                continue
            if any(re.search(busy, command) for _, command in activity.commands):
                actions.append(dict(kind='tool-cache', path=where, bytes=0, result='skipped', reason=name + ' 正在运行'))
                continue
            before = T.size(where) or 0
            result = run(clean, capture_output=True, text=True, timeout=1800)
            after = T.size(where) or 0
            actions.append(dict(kind='tool-cache', path=where, bytes=max(0, before - after),
                                result='done' if result.returncode == 0 else 'failed',
                                reason=' '.join(clean) if result.returncode == 0 else (result.stderr or '').strip()[-200:]))
        except (OSError, subprocess.TimeoutExpired) as error:
            actions.append(dict(kind='tool-cache', path=name, bytes=0, result='failed', reason=str(error)))
    return actions


def write_receipt(home, receipt):
    root = state_root(home)
    root.mkdir(parents=True, exist_ok=True)
    atomic_json(root / RECEIPT, receipt)
    log = root / LOG
    lines = log.read_text().splitlines() if log.exists() else []
    lines.append(json.dumps(receipt, ensure_ascii=False))
    log.write_text('\n'.join(lines[-LOG_KEEP:]) + '\n')


def run(home, cfg, settings, activity_factory=None, simctl=default_simctl, free_fn=None, now=None, tool_run=subprocess.run):
    home = Path(home)
    now = time.time() if now is None else now
    collect = activity_factory or (lambda: Activity.collect(settings.lease_root))
    free = free_fn or (lambda: shutil.disk_usage(str(settings.volume)).free)
    target, warning = settings.target_free_gib * GIB, settings.warning_free_gib * GIB
    receipt = dict(schema=1, at=now, status='complete', free_before=round(free() / GIB, 2), free_after=None,
                   target_free_gib=settings.target_free_gib, warning_free_gib=settings.warning_free_gib,
                   config_error=None, recovered=recover(home), external_missing=[], actions=[], kept=[])
    current = free()
    if not cfg['auto_enabled']:
        receipt['status'] = 'disabled'
    elif current >= warning:
        receipt['status'] = 'healthy'
    else:
        pins, pin_errors = StorageManager(settings)._pins(now)
        activity = collect()
        candidates = []
        for task in T.discover(cfg):
            if task.linked:
                continue    # already on the external drive
            reasons, latest = T.idle_check(task, activity, pins, cfg['auto_idle_days'], cfg, now)
            reasons += pin_errors
            if reasons:
                receipt['kept'].append(dict(path=str(task.path), reason='；'.join(reasons)))
            else:
                candidates.append((latest, task))
        for latest, task in sorted(candidates, key=lambda c: c[0]):
            if current >= target:
                break
            reasons, _ = T.idle_check(task, collect(), pins, cfg['auto_idle_days'], cfg, now)
            if reasons:
                receipt['kept'].append(dict(path=str(task.path), reason='执行前复核：' + '；'.join(reasons)))
                continue
            idle_days = '闲置 {:.1f} 天'.format((now - latest) / T.DAY)
            external = task.external
            if external is not None and volume_ready(external):
                bytes_ = T.size(task.real) or 0
                probe = Path(external)
                while not probe.exists():
                    probe = probe.parent
                if shutil.disk_usage(str(probe)).free < bytes_ * 1.1 + GIB:
                    receipt['kept'].append(dict(path=str(task.path), reason='外接盘空间不够'))
                    continue
                try:
                    dest = move(home, task)
                    receipt['actions'].append(dict(kind='move', path=str(task.path), dest=str(dest), bytes=bytes_,
                                                   result='done', reason=idle_days))
                except Aborted as error:
                    receipt['actions'].append(dict(kind='move', path=str(task.path), bytes=0, result='aborted', reason=str(error)))
                except (OSError, subprocess.SubprocessError) as error:
                    receipt['actions'].append(dict(kind='move', path=str(task.path), bytes=0, result='failed', reason=str(error)))
            else:
                if external is not None and str(external) not in receipt['external_missing']:
                    receipt['external_missing'].append(str(external))
                for output in T.rebuildable(task, cfg):
                    bytes_ = T.size(output) or 0
                    try:
                        T.remove_tree(output)
                        receipt['actions'].append(dict(kind='prune', path=str(output), bytes=bytes_, result='done', reason=idle_days))
                    except OSError as error:
                        receipt['actions'].append(dict(kind='prune', path=str(output), bytes=0, result='failed', reason=str(error)))
            current = free()
        if current < target:
            try:
                for device in unavailable_simulators(simctl):
                    label = '{}（{}）'.format(device.get('name', ''), device.get('udid', ''))
                    bytes_ = device.get('dataPathSize') or T.size(device.get('dataPath', '')) or 0
                    try:
                        simctl(['delete', device['udid']])
                        receipt['actions'].append(dict(kind='simulator-delete', path=label, bytes=bytes_, result='done', reason='系统标为不可用'))
                    except RuntimeError as error:
                        receipt['actions'].append(dict(kind='simulator-delete', path=label, bytes=0, result='failed', reason=str(error)))
            except (RuntimeError, ValueError, OSError, subprocess.SubprocessError) as error:
                receipt['kept'].append(dict(path='模拟器', reason='模拟器列表读取失败：' + str(error)))
            current = free()
        if current < target:
            receipt['actions'] += tool_caches(home, activity, tool_run)
            current = free()
        if any(a['result'] == 'failed' for a in receipt['actions']):
            receipt['status'] = 'partial'
    receipt['free_after'] = round(free() / GIB, 2)
    write_receipt(home, receipt)
    return receipt
