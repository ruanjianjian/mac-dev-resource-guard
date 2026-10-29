"""Shared build caches (toolchain-cache-takeover): one managed place per tool, the Xcode compile cache on.

Caches stay where each tool expects them; with an external drive they move there and
the old path becomes a link. Everything done is recorded so it can be undone.
"""
import json
import os
from pathlib import Path
import re
import subprocess
import time

from storage import atomic_json
import auto_tier
import plan as space_plan
import tasks as T

BUILD_QUIET = 30 * 60   # a build folder untouched this long is between builds, not in one

# (id, name, [(path under HOME, name on the drive)], busy-process pattern, why it must stay beside the projects)
TOOLS = (
    ('xcode', 'Xcode 构建缓存', [('Library/Developer/Xcode/DerivedData', 'DerivedData')],
     r'xcodebuild|/Xcode\.app/Contents/MacOS/Xcode|XCBBuildService|SWBBuildService', None),
    ('cocoapods', 'CocoaPods', [('Library/Caches/CocoaPods', 'cache'), ('.cocoapods', 'repos')],
     r'(^|/)pod (install|update|repo)', None),
    ('flutter', 'Flutter / Dart 依赖', [('.pub-cache', 'pub-cache')], r'(^|/)(flutter|dart)( |$)', None),
    ('npm', 'npm', [('.npm', 'npm')], r'(^|/)(npm|npx)( |$)', None),
    ('yarn', 'yarn', [('Library/Caches/Yarn', 'yarn')], r'(^|/)yarn( |$)', None),
    ('go', 'Go', [('Library/Caches/go-build', 'go-build'), ('go/pkg/mod', 'go-mod')],
     r'(^|/)go (build|test|run|vet|install|mod|get|generate)\b|gopls', None),
    ('gradle', 'Gradle', [('.gradle/caches', 'caches')], r'GradleDaemon|(^|/)gradlew?( |$)', None),
    ('cargo', 'Cargo', [('.cargo/registry', 'registry'), ('.cargo/git', 'git')], r'(^|/)cargo ', None),
    ('homebrew', 'Homebrew 下载', [('Library/Caches/Homebrew', 'downloads')], r'(^|/)brew ', None),
    ('pip', 'pip', [('Library/Caches/pip', 'pip')], r'(^|/)pip3? (install|download)', None),
    ('pnpm', 'pnpm 包仓库', [('Library/pnpm/store', 'store')], None, '要和项目在同一块盘上才能共享文件，不搬'),
    ('uv', 'uv', [('.cache/uv', 'uv')], None, '要和项目在同一块盘上才能共享文件，不搬'),
)
STATE = 'cache-takeover.json'
XCCONFIG = '.config/dev-resource-guard/xcode-global.xcconfig'
BEGIN, END = '# >>> mac-dev-resource-guard >>>', '# <<< mac-dev-resource-guard <<<'
ENV = 'XCODE_XCCONFIG_FILE'
NAMES = {t[0]: t[1] for t in TOOLS}


class Conflict(ValueError):
    """The user already runs their own global Xcode settings file; it is never overwritten."""


def state_path(home):
    return auto_tier.state_root(home) / STATE


def load_state(home):
    try:
        return json.loads(state_path(home).read_text())
    except (OSError, ValueError):
        return dict(schema=1, entries={}, leftovers=[])


def save_state(home, state):
    state_path(home).parent.mkdir(parents=True, exist_ok=True)
    atomic_json(state_path(home), state)


def custom_locations(home):
    """Locations the user set on purpose; they are adopted as they are, never rewritten."""
    found = {}
    env = Path(home) / 'Library/Application Support/go/env'
    try:
        for line in env.read_text().splitlines():
            key, _, value = line.partition('=')
            if key in ('GOCACHE', 'GOMODCACHE') and value:
                found['go:' + ('Library/Caches/go-build' if key == 'GOCACHE' else 'go/pkg/mod')] = value
    except OSError:
        pass
    try:
        for line in (Path(home) / '.npmrc').read_text().splitlines():
            key, _, value = line.partition('=')
            if key.strip() == 'cache' and value.strip():
                found['npm:.npm'] = os.path.expanduser(value.strip())
    except OSError:
        pass
    return found


def volume_of(path):
    """The drive a path sits on: …/Volumes/<name>; None for the internal disk."""
    parts = Path(path).parts
    index = next((i for i, part in enumerate(parts[:-1]) if part == 'Volumes' and parts[i - 1] != 'System'), None)
    return str(Path(*parts[:index + 2])) if index is not None else None


def link_target(path):
    """Where a link points, read from the link itself without stepping onto the other disk."""
    target = os.readlink(path)
    return target if os.path.isabs(target) else os.path.normpath(os.path.join(os.path.dirname(path), target))


def inspect(home, external_root=None, sizes=True, touch_external=True):
    """One row per cache path the user actually has: where it is and what will happen to it.

    touch_external=False never reads anything on an external drive: after a reinstall macOS asks
    again before 资源看板 may read one, and a background run would sit waiting for that answer.
    """
    home = Path(home)
    state = load_state(home)
    custom = custom_locations(home)
    rows = []
    for tool, name, paths, busy, stays in TOOLS:
        for relative, label in paths:
            path = home / relative
            key = tool + ':' + relative
            target = custom.get(key)
            record = state['entries'].get(key)
            row = dict(key=key, tool=tool, name=name, label=label, path=str(path), dest=None, bytes=None, movable=stays is None)
            if target:
                row.update(where='外接盘' if volume_of(target) else '内置盘', status='custom', dest=target,
                           note='你已改过位置：' + target + '，保持不动')
                measured = target
            elif path.is_symlink():
                real = os.path.realpath(path) if touch_external else link_target(path)
                ours = bool(record and record.get('state') == 'moved')
                volume = volume_of(real)
                reachable = (os.path.exists(real) if touch_external or not volume else os.path.ismount(volume))
                row.update(where='外接盘' if volume_of(real) else '内置盘', dest=real,
                           status='moved' if ours else 'linked',
                           note=('原位置留了链接，工具照常用' if ours else '原位置已是链接，保持不动') + ('' if reachable else '；外接盘未连接'))
                measured = real if reachable else None
            elif path.is_dir():
                row.update(where='内置盘', status='stays' if stays else 'local', note=stays or '')
                measured = path
            else:
                continue
            if sizes and measured and (touch_external or not volume_of(measured)):
                row['bytes'] = T.size(measured)
            rows.append(row)
    return rows


def busy_tools(activity):
    commands = [command for _, command in activity.commands]
    return {tool for tool, _, _, pattern, _ in TOOLS if pattern and any(re.search(pattern, c) for c in commands)}


def take_over(home, external_root, activity, free_bytes=None):
    """Move every local cache onto the managed folder of the drive; one receipt row per path."""
    home = Path(home)
    state = load_state(home)
    actions = []
    if external_root is None:
        return actions
    if not auto_tier.volume_ready(external_root):
        return [dict(kind='cache-move', path=str(external_root), bytes=0, result='skipped', reason='外接盘未连接')]
    denied = auto_tier.external_access(external_root)
    if denied:
        return [dict(kind='cache-move', path=str(external_root), bytes=0, result='skipped', reason=denied)]
    busy = busy_tools(activity)
    free_before = round(auto_tier.shutil.disk_usage(str(home)).free / auto_tier.GIB, 2)
    try:
        _move_all(home, external_root, state, busy, actions, free_bytes, free_before)
    finally:
        auto_tier.working(home)
    return actions


def _move_all(home, external_root, state, busy, actions, free_bytes, free_before):
    for row in inspect(home, external_root, sizes=False):
        if row['status'] != 'local':
            continue
        tool, path = row['tool'], Path(row['path'])
        dest = Path(external_root) / 'caches' / tool / row['label']
        if tool in busy:
            actions.append(dict(kind='cache-move', path=str(path), bytes=0, result='skipped', reason=row['name'] + ' 正在运行，下一轮再搬'))
            continue
        bytes_ = T.size(path) or 0
        free = free_bytes(external_root) if free_bytes else auto_tier.shutil.disk_usage(str(external_root)).free
        if free < bytes_ * 1.1 + (1 << 30):
            actions.append(dict(kind='cache-move', path=str(path), bytes=0, result='skipped', reason='外接盘空间不够'))
            continue
        try:
            if dest.exists() and not dest.is_symlink() and not any(dest.iterdir()):
                dest.rmdir()   # an empty copy left by an earlier restore
            auto_tier.working(home, 'move', row['name'] + ' 缓存', bytes_, free_before)
            auto_tier.move_dir(home, path, dest)
            state['entries'][row['key']] = dict(state='moved', path=str(path), dest=str(dest), at=time.time())
            save_state(home, state)
            actions.append(dict(kind='cache-move', path=str(path), dest=str(dest), bytes=bytes_, result='done', reason=row['name']))
        except auto_tier.Aborted as error:
            actions.append(dict(kind='cache-move', path=str(path), bytes=0, result='aborted', reason=str(error)))
        except (OSError, subprocess.SubprocessError) as error:
            actions.append(dict(kind='cache-move', path=str(path), bytes=0, result='failed', reason=str(error)))


def task_builds(cfg):
    """Each task's own Xcode build folders (DerivedData under any name) still on the internal disk."""
    found = []
    for task in T.discover(cfg):
        if task.linked or not task.reachable:
            continue   # the whole task is already on the drive, or that drive is unplugged
        found += [(task, copy) for copy in space_plan.duplicate_builds(task.path)]
    return found


def take_over_builds(home, external_root, cfg, activity, now=None, free_bytes=None, limit=1):
    """Move tasks' own build folders to the drive and leave a link, one per round (RAW-36).

    Build scripts that pass -derivedDataPath <task>/DerivedData keep working: they now write
    through the link. Nothing is deleted; a folder in use or built in the last 30 minutes waits."""
    home = Path(home)
    now = time.time() if now is None else now
    actions = []
    if external_root is None or not auto_tier.volume_ready(external_root):
        return actions
    if auto_tier.external_access(external_root):
        return actions   # take_over already reports a refused drive
    state = load_state(home)
    free_before = round(auto_tier.shutil.disk_usage(str(home)).free / auto_tier.GIB, 2)
    try:
        for task, copy in task_builds(cfg):
            if len(actions) >= limit:
                break
            if activity.reasons(copy) or now - space_plan.last_used(copy) < BUILD_QUIET:
                continue   # in use, or a build just ran: next round
            dest = Path(external_root) / 'builds' / task.path.name / '--'.join(copy.relative_to(task.path).parts)
            bytes_ = T.size(copy) or 0
            free = free_bytes(external_root) if free_bytes else auto_tier.shutil.disk_usage(str(external_root)).free
            if free < bytes_ * 1.1 + auto_tier.GIB:
                actions.append(dict(kind='build-move', path=str(copy), bytes=0, result='skipped', reason='外接盘空间不够'))
                continue
            try:
                auto_tier.working(home, 'move', '{} 的构建产物'.format(task.path.name), bytes_, free_before)
                auto_tier.move_dir(home, copy, dest)
                state['entries']['build:' + str(copy)] = dict(state='moved', kind='build', path=str(copy), dest=str(dest), at=time.time())
                save_state(home, state)
                actions.append(dict(kind='build-move', path=str(copy), dest=str(dest), bytes=bytes_, result='done', reason=task.path.name))
            except auto_tier.Aborted as error:
                actions.append(dict(kind='build-move', path=str(copy), bytes=0, result='aborted', reason=str(error)))
            except (OSError, subprocess.SubprocessError) as error:
                actions.append(dict(kind='build-move', path=str(copy), bytes=0, result='failed', reason=str(error)))
    finally:
        auto_tier.working(home)
    return actions


def restore(home):
    """Undo what this tool did: links become empty folders again, its copies on the drive are removed.
    Tasks' build folders stay on the drive behind their links: turning caches off loses no build."""
    home = Path(home)
    state = load_state(home)
    results = []
    for key, entry in list(state['entries'].items()):
        if entry.get('state') != 'moved' or entry.get('kind') == 'build':
            continue
        path, dest = Path(entry['path']), Path(entry['dest'])
        if path.is_symlink() and os.readlink(path) == str(dest):
            os.unlink(path)
            path.mkdir(parents=True, exist_ok=True)
        if dest.exists():
            auto_tier.force_rmtree(dest)
        elif auto_tier.volume_ready(dest):
            pass
        else:
            state['leftovers'].append(str(dest))   # removed when the drive is back
        del state['entries'][key]
        results.append(dict(path=str(path), dest=str(dest)))
    save_state(home, state)
    return results


def clear_leftovers(home):
    state = load_state(home)
    kept = []
    for dest in state.get('leftovers', []):
        if Path(dest).exists():
            auto_tier.force_rmtree(dest)
        elif not auto_tier.volume_ready(dest):
            kept.append(dest)
    if kept != state.get('leftovers', []):
        state['leftovers'] = kept
        save_state(home, state)


# ---- Xcode compile cache: one global settings file for every xcodebuild and Xcode run ----

def xcconfig_path(home):
    return Path(home) / XCCONFIG


def shell_files(home):
    """zsh always; bash only where the user already has a profile."""
    home = Path(home)
    return [home / '.zshenv'] + [p for p in (home / '.bash_profile',) if p.exists()]


def _strip_block(text):
    return re.sub(r'\n?' + re.escape(BEGIN) + r'.*?' + re.escape(END) + r'\n?', '\n', text, flags=re.S).strip('\n')


def default_launchctl(*args):
    return subprocess.run(['/bin/launchctl', *args], capture_output=True, text=True, timeout=30)


def xcode_conflict(home, launchctl=default_launchctl):
    """Someone else's global Xcode settings file, if any."""
    mine = str(xcconfig_path(home))
    current = launchctl('getenv', ENV).stdout.strip()
    if current and current != mine:
        return current
    for path in [Path(home) / name for name in ('.zshenv', '.zshrc', '.zprofile', '.bash_profile', '.bashrc', '.profile')]:
        try:
            text = _strip_block(path.read_text())
        except OSError:
            continue
        if ENV in text:
            return str(path)
    return None


def enable_xcode_cache(home, launchctl=default_launchctl):
    home = Path(home)
    conflict = xcode_conflict(home, launchctl)
    if conflict:
        raise Conflict('已有你自己的全局 Xcode 设置（{}），没有覆盖'.format(conflict))
    path = xcconfig_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    cas = home / 'Library/Developer/Xcode/DerivedData/CompilationCache.noindex'
    path.write_text('// Managed by 资源看板 (mac-dev-resource-guard). 关闭请在看板里操作。\n'
                    'COMPILATION_CACHE_ENABLE_CACHING = YES\n'
                    'COMPILATION_CACHE_CAS_PATH = {}\n'.format(cas))
    line = 'export {}="{}"'.format(ENV, path)
    for rc in shell_files(home):
        text = _strip_block(rc.read_text()) if rc.exists() else ''
        rc.write_text((text + '\n\n' if text else '') + '{}\n{}\n{}\n'.format(BEGIN, line, END))
    launchctl('setenv', ENV, str(path))


def disable_xcode_cache(home, launchctl=default_launchctl):
    """Settings off for new terminals and apps; the file stays (empty) so shells opened earlier still build."""
    home = Path(home)
    for rc in shell_files(home):
        if rc.exists():
            text = rc.read_text()
            stripped = _strip_block(text)
            if stripped != text.strip('\n'):
                rc.write_text(stripped + '\n' if stripped else '')
    if launchctl('getenv', ENV).stdout.strip() == str(xcconfig_path(home)):
        launchctl('unsetenv', ENV)
    path = xcconfig_path(home)
    if path.exists():
        path.write_text('// 资源看板已关闭 Xcode 编译缓存；保留空文件，已打开的终端照常构建。\n')


def xcode_cache_on(home):
    path = xcconfig_path(home)
    try:
        return 'COMPILATION_CACHE_ENABLE_CACHING = YES' in path.read_text()
    except OSError:
        return False


def ensure_env(home, launchctl=default_launchctl):
    """launchd forgets setenv at logout; the periodic job puts it back."""
    path = str(xcconfig_path(home))
    if xcode_cache_on(home) and launchctl('getenv', ENV).stdout.strip() != path and not xcode_conflict(home, launchctl):
        launchctl('setenv', ENV, path)
