"""Working beside Mole (agent-handoff AC-07): keep its cleanup away from what 资源看板 manages.

Mole reads ~/.config/mole/whitelist. When that file does not exist Mole protects a built-in default
list; once the file exists it replaces those defaults. So the first time we write the file we copy
Mole's own defaults in first, then our block, and we only ever touch lines between our markers.
"""
import os
from pathlib import Path
import re
import shutil

BEGIN = '# >>> mac-dev-resource-guard: paths 资源看板 manages, kept out of Mole cleanup >>>'
END = '# <<< mac-dev-resource-guard <<<'


def whitelist_path(home):
    return Path(home) / '.config/mole/whitelist'


def installed(which=shutil.which):
    return which('mo') or which('mole')


def defaults(home, which=shutil.which):
    """Mole's built-in default whitelist, read from the installed Mole so it follows Mole's updates."""
    binary = installed(which)
    if not binary:
        return None
    root = Path(os.path.realpath(binary)).parent
    candidates = [root / 'lib/core/base.sh', root.parent / 'lib/core/base.sh'] + list(root.parent.glob('*/lib/core/base.sh'))
    for base in candidates:
        try:
            text = base.read_text()
        except OSError:
            continue
        block = re.search(r'declare -a DEFAULT_WHITELIST_PATTERNS=\((.*?)\n\)', text, re.S)
        if not block:
            continue
        patterns = []
        for line in block.group(1).splitlines():
            value = line.strip().strip('"')
            if not value or value.startswith('#') or '$' in value.replace('$HOME', ''):
                continue   # variables other than $HOME are Mole's own safety rules, merged by Mole itself
            patterns.append(value.replace('$HOME', str(home)))
        return patterns
    return None


def managed_paths(home, cfg, cache_rows):
    """Everything 资源看板 owns: the drive folder, task roots and their drive folders, cache links and targets."""
    paths = []
    if cfg.get('external_root'):
        paths.append(str(cfg['external_root']))
    for root in cfg.get('task_roots', []):
        paths.append(str(root['path']))
        if root.get('external'):
            paths.append(str(root['external']))
    for row in cache_rows:
        if row['status'] in ('moved', 'linked', 'custom'):
            paths.append(row['path'])
            if row.get('dest'):
                paths.append(row['dest'])
    paths.append(str(Path(home) / 'Library/Developer/Xcode/DerivedData'))
    seen, result = set(), []
    for path in paths:
        for pattern in (path, path.rstrip('/') + '/*'):
            if pattern not in seen:
                seen.add(pattern); result.append(pattern)
    return result


def guard(home, cfg, cache_rows, remove=False, which=shutil.which):
    """Write (or remove) our block in Mole's whitelist. Returns the protected patterns."""
    path = whitelist_path(home)
    exists = path.exists()
    text = path.read_text() if exists else ''
    kept = re.sub(r'\n?' + re.escape(BEGIN) + r'.*?' + re.escape(END) + r'\n?', '\n', text, flags=re.S).strip('\n')
    if remove:
        if exists:
            path.write_text(kept + '\n' if kept else '')
        return []
    if not exists:
        base = defaults(home, which)
        if base is None:
            raise ValueError('找不到 Mole 自带的默认白名单。请先运行一次 mo clean --whitelist 并保存，再重试')
        kept = '\n'.join(['# Mole defaults, copied when this file was first created'] + base)
    patterns = managed_paths(home, cfg, cache_rows)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text((kept + '\n\n' if kept else '') + '\n'.join([BEGIN] + patterns + [END]) + '\n')
    return patterns
