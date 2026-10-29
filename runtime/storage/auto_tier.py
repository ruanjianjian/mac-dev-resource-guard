"""Safe maintenance receipts and explicitly opted-in cache relocation helpers."""
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import time

from storage import GIB, atomic_json

RECEIPT, LOG, MOVE_STATE = 'auto-tier.json', 'auto-tier-log.jsonl', 'auto-tier-move.json'
LOG_KEEP = 2000


class Aborted(Exception):
    """The move gave up safely; the original directory is untouched."""


WORKING = 'working.json'


def state_root(home):
    return Path(home) / '.local/state/dev-resource-guard'


def volume_ready(path):
    parts = Path(path).parts
    if len(parts) > 2 and parts[1] == 'Volumes':
        return os.path.ismount(os.path.join('/', parts[1], parts[2]))
    return True


def external_access(path):
    """None when this process may write there; otherwise why not (macOS keeps removable volumes private)."""
    probe = Path(path) / '.guard-access-{}'.format(os.getpid())
    try:
        Path(path).mkdir(parents=True, exist_ok=True)
        probe.write_text('')
        probe.unlink()
        return None
    except PermissionError:
        return '后台任务没有访问外接盘的权限：请在系统提示里允许「资源看板」访问可移除宗卷'
    except OSError as error:
        return '外接盘无法写入：' + str(error)


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


def force_rmtree(path):
    """rmtree that also clears read-only folders (Go's module cache is read-only on purpose)."""
    def writable(function, target, _):
        os.chmod(os.path.dirname(target), 0o755)
        if os.path.isdir(target) and not os.path.islink(target):
            os.chmod(target, 0o755)
        function(target)
    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=writable)
    else:
        shutil.rmtree(path, onerror=writable)


def move(home, task, hook=None):
    return move_dir(home, task.path, Path(task.external) / task.path.name, hook)


def move_dir(home, src, dest, hook=None):
    """Copy, verify nothing changed, then swap in a link. Crash states are in MOVE_STATE."""
    marker = state_root(home) / MOVE_STATE
    if marker.exists():
        raise Aborted('已有未完成的搬迁记录，保留现场，请先核对')
    src, dest = Path(src), Path(dest)
    dest_dir = dest.parent
    tmp = dest_dir / ('.guard-moving-' + dest.name)
    if dest.exists() or dest.is_symlink():
        raise Aborted('外接盘上已有同名目录：' + str(dest))
    dest_dir.mkdir(parents=True, exist_ok=True)
    if tmp.exists():
        force_rmtree(tmp)
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
        if tmp.exists():
            force_rmtree(tmp)
        marker.unlink(missing_ok=True)
        raise
    atomic_json(marker, dict(src=str(src), dest=str(dest), tmp=str(tmp), phase='swapping', at=time.time()))
    os.rename(tmp, dest)
    old = src.with_name('.guard-old-' + src.name)
    os.rename(src, old)
    os.symlink(dest, src)
    force_rmtree(old)
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
            force_rmtree(old)
    elif src.is_dir():
        outcome = '上次搬迁未完成，原目录保留，删除外接盘上的不完整副本'
        shutil.rmtree(tmp, ignore_errors=True)
        if s['phase'] == 'swapping' and dest.is_dir() and not old.exists():
            shutil.rmtree(dest, ignore_errors=True)
    elif old.is_dir() and dest.is_dir():
        os.symlink(dest, src)
        force_rmtree(old)
        outcome = '上次搬迁停在换链接前，已补上链接'
    else:
        # Never guess with data at stake; keep the marker for a person to look at.
        return '上次搬迁状态无法判断，保留现场：' + str(src)
    marker.unlink()
    return outcome


def default_simctl(arguments):
    out = subprocess.run(['xcrun', 'simctl', *arguments], capture_output=True, text=True, timeout=600)
    if out.returncode:
        raise RuntimeError((out.stderr or out.stdout).strip() or 'simctl failed')
    return out.stdout


def working(home, kind=None, title='', bytes_=0, free_before=None):
    """What the background is doing right now (move / prune / clean), read by the menu-bar app to animate;
    called with no kind when it stops."""
    path = state_root(home) / WORKING
    if kind is None:
        path.unlink(missing_ok=True)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_json(path, dict(at=time.time(), kind=kind, title=title, bytes=bytes_, free_before_gib=free_before))


def write_receipt(home, receipt):
    root = state_root(home)
    root.mkdir(parents=True, exist_ok=True)
    atomic_json(root / RECEIPT, receipt)
    append_log(home, receipt)


def append_log(home, receipt):
    """The 「自动记录」 tab reads every action from this log, cache moves included."""
    root = state_root(home)
    root.mkdir(parents=True, exist_ok=True)
    log = root / LOG
    lines = log.read_text().splitlines() if log.exists() else []
    lines.append(json.dumps(receipt, ensure_ascii=False))
    log.write_text('\n'.join(lines[-LOG_KEEP:]) + '\n')


def run(home, cfg, settings, activity_factory=None, simctl=default_simctl, free_fn=None, now=None, tool_run=subprocess.run):
    """Record the safe tier; service.clean_caches owns all automatic deletion.

    Do not resume legacy moves here: a task left in a partial move requires
    explicit inspection, even when its old configuration enabled automation.
    """
    home = Path(home)
    now = time.time() if now is None else now
    free = free_fn or (lambda: shutil.disk_usage(str(settings.volume)).free)
    value = round(free() / GIB, 2)
    receipt = dict(schema=1, at=now, status='managed-caches' if cfg['auto_enabled'] else 'disabled',
                   free_before=value, free_after=value, actions=[], kept=[], config_error=None,
                   external_missing=[], reason='每 12 小时仅清理至少 7 天未用的编译中间缓存；保留任务、模拟器和全局工具缓存')
    if (state_root(home) / MOVE_STATE).exists():
        receipt['kept'].append(dict(path=str(state_root(home) / MOVE_STATE),
                                    reason='旧版未完成的搬迁记录已保留，需人工核对，不自动恢复或删除'))
    write_receipt(home, receipt)
    return receipt
