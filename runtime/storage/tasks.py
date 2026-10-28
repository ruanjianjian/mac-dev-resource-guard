"""Task directories under the configured roots: idleness, git state, rebuildable outputs (design C2)."""
import dataclasses
import os
from pathlib import Path
import shutil
import subprocess
import time

PRUNE_NAMES = ('build', '.dart_tool', 'node_modules', 'DerivedData', '.gradle', 'Pods', '.build', 'target',
               '.next', '.turbo', '.venv', '.mobile-v')
IGNORED_FILES = {'.DS_Store'}
DAY = 86400


@dataclasses.dataclass
class Task:
    root: Path
    path: Path          # where the user sees it, under the task root
    real: Path          # resolved location; differs once moved to the external drive
    external: Path = None

    @property
    def linked(self):
        return self.path.is_symlink()

    @property
    def reachable(self):
        return self.real.is_dir()


def discover(cfg):
    tasks = []
    for root in cfg['task_roots']:
        try:
            children = sorted(root['path'].iterdir())
        except OSError:
            continue
        for child in children:
            if child.name.startswith('.') or not (child.is_dir() or child.is_symlink()):
                continue
            tasks.append(Task(root['path'], child, child.resolve(), root['external']))
    return tasks


def skip_names(cfg):
    return set(PRUNE_NAMES) | set(cfg.get('prune_names', [])) | {'.git'}


def latest_write(path, skipped, stop_after=None):
    """Newest mtime of anything but build outputs and git internals (our own git calls touch .git).

    With stop_after, the walk ends at the first entry newer than it: a busy folder is
    answered quickly, while an idle one still gets its real age for ordering.
    """
    newest = 0.0
    for folder, dirs, files in os.walk(path, followlinks=False):
        dirs[:] = [d for d in dirs if d not in skipped]
        for name in dirs + files:
            try:
                newest = max(newest, os.lstat(os.path.join(folder, name)).st_mtime)
            except OSError:
                continue
            if stop_after is not None and newest > stop_after:
                return newest
    return newest


def repositories(path, skipped, depth=3):
    """Git worktrees at or below the task directory, without entering build outputs."""
    found = []
    def visit(folder, level):
        if (folder / '.git').exists():
            found.append(folder)
            return
        if level >= depth:
            return
        try:
            for child in sorted(folder.iterdir()):
                if child.is_dir() and not child.is_symlink() and child.name not in skipped:
                    visit(child, level + 1)
        except OSError:
            pass
    visit(Path(path), 0)
    return found


def git(repo, *args, timeout=60):
    return subprocess.run(['git', '-C', str(repo), *args], capture_output=True, text=True, timeout=timeout)


def git_changes(repo):
    """Reason the repository still has work only here, or None when committed and pushed."""
    try:
        status = git(repo, 'status', '--porcelain')
        if status.returncode:
            return 'git 状态读取失败'
        if status.stdout.strip():
            return '有未提交改动'
        ahead = git(repo, 'rev-list', '--count', 'HEAD', '--not', '--remotes')
        if ahead.returncode:
            return 'git 提交读取失败'
        if int(ahead.stdout.strip() or 0):
            return '有未推送的提交'
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return 'git 状态读取失败'
    return None


def loose_files(task, skipped):
    """Top-level entries that are not git worktrees, e.g. notes or reports kept beside the code."""
    if (task.real / '.git').exists():
        return []
    loose = []
    try:
        for child in task.real.iterdir():
            if child.name in IGNORED_FILES:
                continue
            if not (child.is_dir() and not child.is_symlink() and (child / '.git').exists()):
                loose.append(child.name)
    except OSError:
        return ['（目录无法读取）']
    return sorted(loose)


def pin_reason(pins, task):
    keys = {task.path, task.real}
    for pinned, value in pins.items():
        pinned = Path(pinned)
        if any(pinned == k or pinned in k.parents or k in pinned.parents for k in keys):
            return '已被标记保护：' + str(value.get('reason', ''))
    return None


def in_use(activity, task):
    return activity.reasons(task.path, task.real if task.real != task.path else None)


def idle_check(task, activity, pins, days, cfg, now=None):
    """Return (reasons it must stay, latest write time). No reasons means idle."""
    now = time.time() if now is None else now
    reasons = list(in_use(activity, task))
    pinned = pin_reason(pins, task)
    if pinned:
        reasons.append(pinned)
    latest = latest_write(task.real, skip_names(cfg), stop_after=now - days * DAY)
    if now - latest < days * DAY:
        reasons.append('{:.1f} 天内有改写（要求闲置 {} 天）'.format(max(0.0, (now - latest) / DAY), days))
    return reasons, latest


def rebuildable(task, cfg):
    """Ignored build-output directories inside git worktrees; nothing outside a worktree."""
    skipped = skip_names(cfg)
    names = skipped - {'.git'}
    result = []
    for repo in repositories(task.real, skipped):
        candidates = []
        for folder, dirs, _ in os.walk(repo, followlinks=False):
            keep = []
            for d in dirs:
                if d == '.git':
                    continue
                full = Path(folder) / d
                if full.is_symlink():
                    continue
                if d in names:
                    candidates.append(full)
                elif not (full / '.git').exists():   # nested repos are visited on their own
                    keep.append(d)
            dirs[:] = keep
        if not candidates:
            continue
        relative = '\0'.join(str(c.relative_to(repo)) for c in candidates) + '\0'
        try:
            checked = subprocess.run(['git', '-C', str(repo), 'check-ignore', '-z', '--stdin'], input=relative,
                                     capture_output=True, text=True, timeout=60)
        except (OSError, subprocess.TimeoutExpired):
            continue
        ignored = {p for p in checked.stdout.split('\0') if p}
        result.extend(c for c in candidates if str(c.relative_to(repo)) in ignored)
    return result


def size(path):
    try:
        out = subprocess.run(['/usr/bin/du', '-sk', str(path)], capture_output=True, text=True, timeout=300)
        return int(out.stdout.split()[0]) * 1024 if out.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired, ValueError, IndexError):
        return None


def remove_tree(path):
    """Rename first so a half-deleted output never looks like a valid one."""
    path = Path(path)
    trash = path.with_name('.guard-removing-{}-{}'.format(path.name, time.time_ns()))
    os.rename(path, trash)
    shutil.rmtree(trash)
