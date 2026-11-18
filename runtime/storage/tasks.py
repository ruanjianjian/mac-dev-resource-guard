"""Task directories under the configured roots: idleness, git state, rebuildable outputs (design C2)."""
import dataclasses
import json
import os
from pathlib import Path
import shutil
import subprocess
import time

PRUNE_NAMES = ('build', '.dart_tool', 'node_modules', 'DerivedData', '.gradle', 'Pods', '.build', 'target',
               '.next', '.turbo', '.venv', '.mobile-v')
IGNORED_FILES = {'.DS_Store'}
DAY = 86400
PR_RECHECK = 6 * 3600   # an open PR, or none found, is asked about again after this; merged and closed are final
GH_PLACES = ('/opt/homebrew/bin', '/usr/local/bin')   # launchd's PATH has neither


class Unreadable(OSError):
    """Part of the folder could not be read (often a missing privacy permission)."""


@dataclasses.dataclass
class Task:
    root: Path
    path: Path          # where the user sees it, under the task root
    real: Path          # resolved location; differs once moved to the external drive
    external: Path = None

    @property
    def linked(self):
        # On the drive: its own link, or its whole task root is a link to the drive (work-drive).
        return self.path.is_symlink() or self.root.is_symlink()

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
    def failed(error):
        raise Unreadable(error.errno, '目录无法完整读取：' + str(error.filename))
    for folder, dirs, files in os.walk(path, followlinks=False, onerror=failed):
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


def pr_state(repo, memory=None, now=None, run=subprocess.run):
    """('merged' | 'closed' | 'open', PR number or None) for the work at this checkout's HEAD, or None when it
    cannot be told. In the default branch already counts as merged without asking anyone; otherwise GitHub is asked
    (gh, if installed and signed in) for the pull requests that contain HEAD. `memory` keeps the answers."""
    head = git(repo, 'rev-parse', 'HEAD')
    head = head.stdout.strip() if head.returncode == 0 else ''
    if not head:
        return None
    if git(repo, 'merge-base', '--is-ancestor', head, 'refs/remotes/origin/HEAD').returncode == 0:
        return 'merged', None
    now = time.time() if now is None else now
    cache = memory.setdefault('pr_state', {}) if memory is not None else {}
    hit = cache.get(head)
    if isinstance(hit, list) and len(hit) == 3 and (hit[0] in ('merged', 'closed') or now - hit[2] < PR_RECHECK):
        return (hit[0], hit[1]) if hit[0] else None
    remote = git(repo, 'remote', 'get-url', 'origin')
    gh = shutil.which('gh', path=os.pathsep.join([os.environ.get('PATH', ''), *GH_PLACES]))
    if not gh or remote.returncode or 'github.com' not in remote.stdout:
        return None   # only GitHub can say; another host, or no gh, leaves it unknown
    try:
        out = run([gh, 'pr', 'list', '--state', 'all', '--search', head, '--json', 'number,state', '--limit', '10'],
                  cwd=str(repo), capture_output=True, text=True, timeout=30)
        found = json.loads(out.stdout or '[]') if out.returncode == 0 else None
    except (OSError, subprocess.SubprocessError, ValueError):
        found = None
    if not isinstance(found, list):
        return None   # offline or not signed in: not remembered, asked again next time
    states = {str(p.get('state', '')).lower(): p.get('number') for p in found if isinstance(p, dict)}
    state = next((s for s in ('merged', 'closed', 'open') if s in states), None)
    cache[head] = [state, states.get(state), now]
    return (state, states[state]) if state else None


def kept_only_here(repo):
    """What a full clone keeps that nothing else does (11-18): commits on any local branch no remote has, or stashes.
    A linked worktree's branches and stashes live in its main repository, which stays, so it has none of its own."""
    if not (Path(repo) / '.git').is_dir():
        return None
    try:
        # HEAD's own commits are work_left's to judge (a merged PR may hold them); here only the other branches.
        ahead = git(repo, 'rev-list', '--count', '--branches', '--not', '--remotes', 'HEAD')
        stash = git(repo, 'stash', 'list')
        if ahead.returncode or stash.returncode:
            return 'git 状态读取失败'
        if int(ahead.stdout.strip() or 0):
            return '别的本地分支上有未推送的提交'
        if stash.stdout.strip():
            return '有 stash 里存着的改动'
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return 'git 状态读取失败'
    return None


def work_left(repo, memory=None, now=None):
    """(why it still holds work only here, or None; its PR state). Commits no branch on the remote holds any more
    but a merged or closed PR does (squash-merged and the branch deleted, 11-16) are saved: GitHub keeps them."""
    change = git_changes(repo)
    if change not in (None, '有未推送的提交'):
        return change, None
    extra = kept_only_here(repo)
    if extra:
        return extra, None
    pr = pr_state(repo, memory, now)
    if change and not (pr and pr[0] in ('merged', 'closed')):
        return change, pr
    return None, pr


def pr_words(states):
    """「PR #12 已合入」 for what the task's repositories' PRs say, or ''."""
    words = []
    for state, number in states:
        name = 'PR #{}'.format(number) if number else '已在主干' if state == 'merged' else 'PR'
        if state == 'merged':
            words.append(name if name == '已在主干' else name + ' 已合入')
        elif state == 'closed':
            words.append(name + ' 已关闭')
    return '、'.join(words)


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
    try:
        latest = latest_write(task.real, skip_names(cfg), stop_after=now - days * DAY)
    except Unreadable as error:
        return reasons + [str(error) + '（可能缺少访问权限），保留'], now
    if now - latest < days * DAY:
        if days < 1:   # work-drive waits minutes, not days: 「要求闲置 0.041666… 天」 said nothing (11-10)
            reasons.append('{:.0f} 分钟内有改写（要求 {:.0f} 分钟没人动）'.format(max(0.0, (now - latest) / 60), days * 1440))
        else:
            reasons.append('{:.1f} 天内有改写（要求闲置 {:g} 天）'.format(max(0.0, (now - latest) / DAY), days))
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
