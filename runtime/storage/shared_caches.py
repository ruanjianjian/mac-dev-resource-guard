"""shared-caches (11-18, the user: 「共享缓存也不是都不能删，也要经常根据项目的实际情况迭代下」).

The shared tool caches keep what today's projects use; a version none of them uses any more is listed in 「等你确认」,
or removed each round when the rule says 「直接清」. The projects are the code repositories under the task roots and
the main repositories their worktrees hang on; what they use is read from their lock files. Nothing is proposed
when no project can be found at all, so a task root that cannot be read never empties a cache.
"""
import json
import os
from pathlib import Path
import re

import tasks as T

REFS_HOURS = 6   # the lock files are read again this often; a confirmation always reads them again
LOCKS = ('pubspec.lock', 'Podfile.lock', '.fvmrc', 'fvm_config.json', 'gradle-wrapper.properties', 'build.gradle',
         'build.gradle.kts')
NAMES = {'flutter-sdk': 'Flutter SDK', 'pub': 'pub 依赖缓存', 'cocoapods': 'CocoaPods 缓存', 'gradle': 'Gradle'}


def main_repo(repo):
    """The repository a linked worktree hangs on (read from its .git file), or None."""
    dot = Path(repo) / '.git'
    if not dot.is_file():
        return None
    try:
        found = re.match(r'gitdir:\s*(.+)', dot.read_text().strip())
    except OSError:
        return None
    if not found:
        return None
    gitdir = Path(found.group(1))
    gitdir = gitdir if gitdir.is_absolute() else (Path(repo) / gitdir).resolve()
    if gitdir.parent.name == 'worktrees' and gitdir.parent.parent.name == '.git':
        return gitdir.parent.parent.parent
    return None


def projects(cfg):
    found = set()
    skipped = T.skip_names(cfg)
    for task in T.discover(cfg):
        if not task.reachable:
            continue
        for repo in T.repositories(task.real, skipped):
            found.add(Path(repo))
            main = main_repo(repo)
            if main is not None and main.is_dir():
                found.add(main)
    return sorted(found)


def lock_files(repo):
    out = T.git(repo, 'ls-files', '-z', '--', *['*' + name for name in LOCKS])
    return [Path(repo) / f for f in out.stdout.split('\0') if f] if out.returncode == 0 else []


def uses(paths):
    """What the projects use: hosted pub packages and pods as (name, version), Flutter and Gradle versions, and
    whether any project builds with Gradle at all."""
    refs = dict(pub=set(), cocoapods=set(), flutter=set(), gradle=set(), gradle_project=False)
    for path in paths:
        try:
            text = path.read_text(errors='replace')
        except OSError:
            continue
        if path.name == 'pubspec.lock':
            package, fields = None, {}
            for line in text.splitlines() + ['  end:']:
                start = re.match(r'^  ([A-Za-z0-9_]+):\s*$', line)
                if start:
                    if package and fields.get('source') == 'hosted' and fields.get('version'):
                        refs['pub'].add((package, fields['version']))
                    package, fields = start.group(1), {}
                    continue
                field = re.match(r'^    (source|version):\s*"?([^"\s]+)"?', line)
                if package and field:
                    fields[field.group(1)] = field.group(2)
        elif path.name == 'Podfile.lock':
            for pod, version in re.findall(r'(?m)^  - "?([^\s"(]+)\s+\(([^)]+)\)', text):
                refs['cocoapods'].add((pod.split('/')[0], version))
        elif path.name in ('.fvmrc', 'fvm_config.json'):
            try:
                body = json.loads(text)
            except ValueError:
                continue
            version = (body.get('flutter') or body.get('flutterSdkVersion')) if isinstance(body, dict) else None
            if isinstance(version, str):
                refs['flutter'].add(version)
        elif path.name == 'gradle-wrapper.properties':
            refs['gradle_project'] = True
            refs['gradle'].update(re.findall(r'gradle-([0-9][^-/\s]*)-(?:bin|all)\.zip', text))
        else:
            refs['gradle_project'] = True
    return refs


def _children(folder):
    try:
        return sorted(p for p in Path(folder).iterdir() if p.is_dir() and not p.is_symlink() and not p.name.startswith('.'))
    except OSError:
        return []


def unused(home, refs):
    """{family: [paths]} of versions no project uses."""
    home = Path(home)
    found = {}
    for root in (home / 'fvm', home / '.fvm'):
        default = root / 'default'
        keep = default.resolve().name if default.is_symlink() else None
        found.setdefault('flutter-sdk', []).extend(
            str(p) for p in _children(root / 'versions') if p.name not in refs['flutter'] and p.name != keep)
    pub = Path(os.environ.get('PUB_CACHE') or home / '.pub-cache') / 'hosted'
    for host in _children(pub):
        found.setdefault('pub', []).extend(
            str(p) for p in _children(host) if '-' in p.name and tuple(p.name.split('-', 1)) not in refs['pub'])
    for pod in _children(home / 'Library/Caches/CocoaPods/Pods/Release'):
        found.setdefault('cocoapods', []).extend(
            str(v) for v in _children(pod) if (pod.name, v.name.rsplit('-', 1)[0]) not in refs['cocoapods'])
    gradle = home / '.gradle'
    for dist in _children(gradle / 'wrapper/dists'):
        version = re.match(r'gradle-(.+)-(?:bin|all)$', dist.name)
        if version and version.group(1) not in refs['gradle']:
            found.setdefault('gradle', []).append(str(dist))
    if not refs['gradle_project']:
        found.setdefault('gradle', []).extend(str(p) for p in (gradle / 'caches', gradle / 'daemon') if p.is_dir() and not p.is_symlink())
    return {family: paths for family, paths in found.items() if paths}


def look(home, cfg):
    """{family: {'paths': [...], 'bytes': n, 'projects': n}} right now."""
    found = projects(cfg)
    if not found:
        return {}
    refs = uses([lock for repo in found for lock in lock_files(repo)])
    return {family: dict(paths=paths, bytes=sum(T.size(p) or 0 for p in paths), projects=len(found))
            for family, paths in unused(home, refs).items()}


def candidates(home, cfg, activity, now, memory, add, only=None):
    """One ask per cache family; the lock files are read at most every REFS_HOURS, except to recheck a yes."""
    if only is not None and not any(key.startswith('shared-caches:') for key in only):
        return
    cache = memory.setdefault('shared_caches', {}) if memory is not None else {}
    if only is None and isinstance(cache.get('at'), (int, float)) and now - cache['at'] < REFS_HOURS * 3600:
        items = cache.get('items') or {}
    else:
        items = look(home, cfg)
        cache.update(at=now, items=items)
    for family, item in sorted(items.items()):
        if only is not None and 'shared-caches:' + family not in only and 'shared-caches:' not in only:
            continue
        paths = [p for p in item['paths'] if os.path.lexists(p) and not activity.reasons(Path(p))]
        if not paths:
            continue
        sample = '、'.join(Path(p).name for p in paths[:3]) + (' 等' if len(paths) > 3 else '')
        add('shared-caches', family, '{} 里现在的项目都不用的 {} 个版本'.format(NAMES[family], len(paths)), item['bytes'],
            '读了 {} 个项目的锁文件，这些版本没有项目在用：{}'.format(item['projects'], sample),
            'count:{}'.format(len(paths)), paths=paths)


def remove(paths, activity):
    """Remove the listed versions, each rechecked for use; returns the ones kept because something uses them."""
    kept = []
    for path in paths:
        path = Path(path)
        if not os.path.lexists(str(path)):
            continue
        if activity.reasons(path):
            kept.append(str(path))
            continue
        if path.is_symlink() or not path.is_dir():
            path.unlink()
        else:
            T.remove_tree(path)
    return kept
