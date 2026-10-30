"""Automatic updates (one-line-install AC-10/11): once a day, look for the latest GitHub release and install it
quietly once the Mac is idle, with exactly the command people run by hand, pinned to that release's tag.

Only releases count (a tag plus release notes), never whatever was last merged to main. Settings, the signing
identity and therefore every macOS permission stay as they are; the new app says once that it was updated.
"""
import json
import os
from pathlib import Path
import plistlib
import re
import subprocess
import time
import urllib.request

from storage import atomic_json

REPO = 'ruanjianjian/mac-dev-resource-guard'
DAY = 24 * 3600
RETRY = 3600                      # a failed check or install is tried again after an hour, not a day
GIVE_UP = 3600                    # an install still running after an hour is treated as stuck
STATE = 'update.json'
TAG = re.compile(r'^v\d+\.\d+\.\d+$')
APP_INFO = 'Applications/资源看板.app/Contents/Info.plist'


def state_path(home):
    return Path(home) / '.local/state/dev-resource-guard' / STATE


def read(home):
    try:
        return json.loads(state_path(home).read_text())
    except (OSError, ValueError):
        return {}


def save(home, data):
    state_path(home).parent.mkdir(parents=True, exist_ok=True)
    atomic_json(state_path(home), data)


def version_tuple(text):
    return tuple(int(n) for n in re.findall(r'\d+', str(text or ''))[:3])


def installed(home):
    try:
        return plistlib.loads((Path(home) / APP_INFO).read_bytes()).get('CFBundleShortVersionString')
    except (OSError, ValueError, plistlib.InvalidFileException):
        return None


def latest(fetch=urllib.request.urlopen):
    """The newest published release: tag, version, title and its first line of notes (the update notification)."""
    request = urllib.request.Request('https://api.github.com/repos/{}/releases/latest'.format(REPO),
                                     headers={'Accept': 'application/vnd.github+json', 'User-Agent': 'mac-dev-resource-guard'})
    with fetch(request, timeout=15) as response:
        body = json.load(response)
    lines = [line.strip() for line in (body.get('body') or '').splitlines() if line.strip()]
    return dict(tag=body['tag_name'], version=body['tag_name'].lstrip('v'), title=body.get('name') or body['tag_name'],
                summary=lines[0].lstrip('#-* ').strip() if lines else '')


def command(tag):
    """What people run by hand, pinned to the release: its install.sh and its source."""
    if not TAG.match(tag):
        raise ValueError('不是正式版本号：' + tag)
    return ['/bin/bash', '-c', 'curl -fsSL https://raw.githubusercontent.com/{repo}/{tag}/install.sh | '
            'RESOURCE_GUARD_REF=refs/tags/{tag} bash'.format(repo=REPO, tag=tag)]


def alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, TypeError):
        return False
    except PermissionError:
        return True


def busy_reason(home):
    """Not now: memory is critical (building the app needs a few hundred MB). Plain 「紧张」 does not wait:
    a Mac running several simulators sits there for hours, and the build takes under half a minute."""
    try:
        sample = json.loads((Path(home) / '.local/state/dev-memory/latest.json').read_text()).get('sample') or {}
    except (OSError, ValueError):
        return None
    return '内存很紧' if sample.get('pressure') == 4 else None


def log_tail(home, lines=5):
    try:
        return '\n'.join((state_path(home).parent / 'update.log').read_text(errors='replace').splitlines()[-lines:])
    except OSError:
        return ''


def follow(home, data, now):
    """An install started earlier: done when the app reports the new version, failed when it stopped short of it."""
    if version_tuple(installed(home)) >= version_tuple(data.get('to')):
        data.update(status='installed', finished_at=now)
    elif alive(data.get('pid')) and now - data.get('started_at', 0) < GIVE_UP:
        return data
    else:
        data.update(status='failed', finished_at=now, error=log_tail(home) or '安装没有完成', checked_at=now - DAY + RETRY)
    save(home, data)
    return data


def tick(home, enabled, now=None, fetch=urllib.request.urlopen, spawn=subprocess.Popen, force=False):
    """One step after the storage tick released its lock. Returns the state it left (status says what happened)."""
    now = time.time() if now is None else now
    data = read(home)
    if data.get('status') == 'installing':
        data = follow(home, data, now)
        if data['status'] == 'installing':
            return data
    if not enabled and not force:
        return dict(data, status='off')
    if installed(home) is None:
        # Nothing installed for this home: updates replace an install, they never make one (and a test suite's
        # throwaway home must not reach the network and install a real copy).
        return dict(data, status='not-installed')
    if not force and now - data.get('checked_at', 0) < DAY:
        return data
    waiting = busy_reason(home)
    if waiting and not force:
        data.update(waiting=waiting, waiting_at=now)   # kept, so 「为什么还没更新」 has an answer
        save(home, data)
        return data
    data.update(checked_at=now, waiting=None)
    try:
        release = latest(fetch)
    except (OSError, ValueError, KeyError) as error:   # offline, rate-limited, no release yet
        data.update(status='check-failed', error=str(error), checked_at=now - DAY + RETRY)
        save(home, data)
        return data
    current = installed(home)
    data.update(latest=release['version'], error=None)
    if not TAG.match(release['tag']) or version_tuple(release['version']) <= version_tuple(current):
        data['status'] = 'current'
        save(home, data)
        return data
    log = state_path(home).parent / 'update.log'
    log.parent.mkdir(parents=True, exist_ok=True)
    with open(log, 'w') as out:
        # Its own session, so it outlives this tick; the installer waits for the storage lock and never pops a panel.
        process = spawn(command(release['tag']), stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
                        start_new_session=True)
    data.update(status='installing', pid=process.pid, started_at=now, tag=release['tag'], to=release['version'],
                title=release['title'], summary=release['summary'], **{'from': current})
    save(home, data)
    return data
