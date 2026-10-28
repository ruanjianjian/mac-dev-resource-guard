"""Other places that fill a developer's disk (space-watch): installers, Trash, Xcode leftovers, Docker."""
import json
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import time

import tasks as T

MIB, GIB, DAY = 1 << 20, 1 << 30, 86400
INSTALLER_EXT = ('.dmg', '.pkg', '.mpkg', '.xip', '.iso')
INSTALLER_MIN_BYTES = 200 * MIB
INSTALLER_IDLE_DAYS = 7
TRASH_MIN_BYTES = GIB
DEVICE_SUPPORT_IDLE_DAYS = 30
ARCHIVE_AGE_DAYS = 90
DEVICE_SUPPORT = ('iOS DeviceSupport', 'watchOS DeviceSupport', 'tvOS DeviceSupport', 'visionOS DeviceSupport')


def mounted_images(run=subprocess.run):
    """Disk images that are attached right now; their files must stay."""
    try:
        out = run(['/usr/bin/hdiutil', 'info', '-plist'], capture_output=True, timeout=30)
        return {image.get('image-path') for image in plistlib.loads(out.stdout).get('images', [])} if out.returncode == 0 else set()
    except (OSError, subprocess.SubprocessError, ValueError):
        return set()


def installers(home, now=None, mounted=None):
    """Big installer files at the top of Downloads (all ages; `idle` says whether one may be proposed)."""
    now = time.time() if now is None else now
    mounted = mounted_images() if mounted is None else mounted
    found = []
    try:
        entries = list(os.scandir(Path(home) / 'Downloads'))
    except OSError:
        return found
    for entry in entries:
        if not entry.name.lower().endswith(INSTALLER_EXT) or entry.name.startswith('.'):
            continue
        try:
            st = entry.stat(follow_symlinks=False)
        except OSError:
            continue
        size = st.st_size if entry.is_file(follow_symlinks=False) else (T.size(entry.path) or 0)
        if size < INSTALLER_MIN_BYTES:
            continue
        idle_days = (now - st.st_mtime) / DAY
        found.append(dict(path=entry.path, bytes=size, idle_days=idle_days, mounted=entry.path in mounted,
                          idle=idle_days >= INSTALLER_IDLE_DAYS and entry.path not in mounted, mtime=st.st_mtime))
    return found


def trash_size(home):
    """None when macOS does not let this process read the Trash."""
    trash = Path(home) / '.Trash'
    try:
        os.listdir(trash)
    except FileNotFoundError:
        return 0
    except OSError:
        return None
    return T.size(trash)


def device_support(home, now=None):
    now = time.time() if now is None else now
    found = []
    for folder in DEVICE_SUPPORT:
        base = Path(home) / 'Library/Developer/Xcode' / folder
        try:
            versions = [p for p in base.iterdir() if p.is_dir() and not p.is_symlink()]
        except OSError:
            continue
        for version in versions:
            try:
                latest = max([version.stat().st_mtime] + [c.stat().st_mtime for c in version.iterdir()])
            except OSError:
                continue
            found.append(dict(path=str(version), platform=folder.split()[0], version=version.name,
                              bytes=T.size(version) or 0, idle_days=(now - latest) / DAY, mtime=latest))
    return found


def archives(home, now=None):
    now = time.time() if now is None else now
    found = []
    for archive in (Path(home) / 'Library/Developer/Xcode/Archives').glob('*/*.xcarchive'):
        try:
            mtime = archive.stat().st_mtime
        except OSError:
            continue
        found.append(dict(path=str(archive), name=archive.stem, bytes=T.size(archive) or 0, age_days=(now - mtime) / DAY, mtime=mtime))
    return found


def containers(home, run=subprocess.run):
    """Docker and OrbStack disk images: sizes only, they clean them themselves."""
    found = []
    orb = Path(home) / '.orbstack'
    if orb.is_dir():
        found.append(dict(name='OrbStack', bytes=T.size(orb), note='请在 OrbStack 里清理不用的镜像和容器'))
    if Path('/Applications/Docker.app').exists():
        size = None
        docker = shutil.which('docker') or ('/usr/local/bin/docker' if os.path.exists('/usr/local/bin/docker') else None)
        if docker:
            try:
                out = run([docker, 'system', 'df', '--format', '{{json .}}'], capture_output=True, text=True, timeout=10)
                if out.returncode == 0:
                    size = sum(_parse_size(json.loads(line).get('Size', '0B')) for line in out.stdout.splitlines() if line.strip())
            except (OSError, subprocess.SubprocessError, ValueError):
                size = None
        found.append(dict(name='Docker', bytes=size,
                          note='请在 Docker 里清理不用的镜像和容器' + ('' if size is not None else '（Docker 没在运行，读不到大小）')))
    return found


def _parse_size(text):
    units = {'B': 1, 'KB': 1e3, 'MB': 1e6, 'GB': 1e9, 'TB': 1e12, 'kB': 1e3}
    text = text.strip()
    for unit in sorted(units, key=len, reverse=True):
        if text.endswith(unit):
            try:
                return int(float(text[:-len(unit)]) * units[unit])
            except ValueError:
                return 0
    return 0
