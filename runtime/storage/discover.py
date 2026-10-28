"""Find where tasks live and which external drive to use, so nobody has to type paths (checkup AC-02/03)."""
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import time

# Folders under HOME that never hold source checkouts worth scanning.
SKIP = {'Library', 'Applications', 'Pictures', 'Music', 'Movies', 'Public', 'node_modules', 'DerivedData',
        'Pods', 'build', '.build', 'target', 'dist', 'vendor'}
SCAN_DEPTH = 4
SCAN_SECONDS = 20


def main_repositories(home, depth=SCAN_DEPTH, seconds=SCAN_SECONDS):
    """Git directories of checkouts under HOME (hidden folders and links are not entered)."""
    home = Path(home)
    found, deadline = [], time.monotonic() + seconds
    for folder, dirs, _ in os.walk(home, followlinks=False):
        if time.monotonic() > deadline:
            break
        level = len(Path(folder).relative_to(home).parts)
        if '.git' in dirs:
            found.append(Path(folder) / '.git')
        dirs[:] = [] if level >= depth else [d for d in dirs if not d.startswith('.') and d not in SKIP]
    return found


def worktrees(git_dirs):
    """Linked worktrees registered by these repositories, submodules included."""
    result = set()
    for git_dir in git_dirs:
        records = list(git_dir.glob('worktrees/*/gitdir'))
        modules = git_dir / 'modules'
        if modules.is_dir():
            records += list(modules.glob('**/worktrees/*/gitdir'))
        for record in records:
            try:
                target = Path(record.read_text().strip()).parent
            except OSError:
                continue
            if target.is_absolute() and (target.exists() or target.is_symlink()):
                result.add(target)
    return sorted(result)


def _holders(folder, paths):
    """Direct children of folder that contain at least one of the paths."""
    names = set()
    for path in paths:
        try:
            names.add(path.relative_to(folder).parts[0])
        except (ValueError, IndexError):
            continue
    return names


def task_roots(paths, home):
    """The folder whose children are tasks: a worktree's parent, or its grandparent when that holds several tasks."""
    home = Path(home)
    roots = set()
    for worktree in paths:
        parent, grand = worktree.parent, worktree.parent.parent
        root = grand if grand != home and home in grand.parents and len(_holders(grand, paths)) >= 2 else parent
        if root != home and home in root.parents and 'Library' not in root.relative_to(home).parts[:1]:
            roots.add(root)
    # Nested roots count once, as the outermost.
    return sorted(r for r in roots if not any(o in r.parents for o in roots))


def task_count(root):
    try:
        return sum(1 for c in root.iterdir() if not c.name.startswith('.') and (c.is_dir() or c.is_symlink()))
    except OSError:
        return 0


def disk_info(mount, run=subprocess.run):
    out = run(['/usr/sbin/diskutil', 'info', '-plist', str(mount)], capture_output=True, timeout=30)
    return plistlib.loads(out.stdout) if out.returncode == 0 and out.stdout else {}


def is_time_machine(mount, info):
    roles = info.get('VolumeRoles') or []
    return 'Backup' in roles or any((Path(mount) / name).exists() for name in ('Backups.backupdb', '.timemachine'))


def external_drives(volumes=Path('/Volumes'), info=disk_info, is_mount=os.path.ismount):
    """Writable local APFS drives that are not the startup disk or a Time Machine target, largest free first."""
    drives = []
    try:
        mounts = sorted(volumes.iterdir())
    except OSError:
        return drives
    for mount in mounts:
        if mount.is_symlink() or not is_mount(mount):
            continue   # "Macintosh HD" is a link to /
        detail = info(mount)
        kind = (detail.get('FilesystemType') or detail.get('FilesystemName') or '').lower()
        reasons = []
        if detail.get('Internal', True) and not detail.get('Ejectable'):
            reasons.append('不是外接盘')
        if 'apfs' not in kind:
            reasons.append('格式不是 APFS')
        if not detail.get('WritableVolume', False):
            reasons.append('只读')
        if is_time_machine(mount, detail):
            reasons.append('是时间机器备份盘')
        try:
            free = shutil.disk_usage(str(mount)).free
        except OSError:
            free = 0
        drives.append(dict(path=str(mount), name=detail.get('VolumeName') or mount.name, free=free,
                           usable=not reasons, reason='、'.join(reasons)))
    return sorted(drives, key=lambda d: (not d['usable'], -d['free']))


def recommended_drive(drives):
    return next((d for d in drives if d['usable']), None)


def managed_external(drive_path):
    return Path(drive_path) / 'dev-resource-guard'


def scan(home, configured_roots=(), volumes=Path('/Volumes'), info=disk_info, is_mount=os.path.ismount):
    """Everything the checkup page lists for confirmation."""
    found = task_roots(worktrees(main_repositories(home)), home)
    configured = [Path(r['path']) for r in configured_roots]
    roots = []
    for path in configured + [r for r in found if r not in configured and not any(c in r.parents or r in c.parents for c in configured)]:
        entry = next((r for r in configured_roots if Path(r['path']) == path), None)
        roots.append(dict(path=str(path), tasks=task_count(path), configured=entry is not None,
                          external=str(entry['external']) if entry and entry.get('external') else None))
    drives = external_drives(volumes, info, is_mount)
    return dict(roots=roots, drives=drives, recommended=recommended_drive(drives))
