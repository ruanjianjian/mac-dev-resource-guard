"""Settings for tiered cleanup (design C1).

Kept in auto-cleanup.json, apart from config.json: the 1.3 reader rejects unknown
keys, so mixing them would break older dev-storage commands still installed.
"""
import copy
import json
import os
from pathlib import Path

from storage import atomic_json

DEFAULTS = {
    'schema': 1,
    'auto_enabled': False,
    'ask_enabled': True,
    'check_interval_minutes': 30,
    'task_roots': [],
    'auto_idle_days': 3,
    'ask': {'simulator_unused_days': 7, 'simulator_unattached_hours': 4, 'task_idle_days': 7},
    'prune_names': [],
    # 装好就省心: the checkup page writes these; people can still edit them by hand.
    'caches_enabled': False,
    'xcode_compile_cache': False,
    'external_root': None,
}
FLAGS = ('auto_enabled', 'ask_enabled', 'caches_enabled', 'xcode_compile_cache')


class ConfigError(ValueError):
    """The settings file is wrong; the tiered cleanup skips this round."""


def settings_path(home):
    return Path(home) / '.config/dev-resource-guard/auto-cleanup.json'


def _positive(value, name):
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ConfigError('{} 必须是正整数'.format(name))
    return value


def _too_broad(path, home, owned):
    return (path == Path(path.anchor) or path == home or path in home.parents or os.path.ismount(path) or
            any(path == p or path in p.parents or p in path.parents for p in owned))


def load(home, owned=()):
    """Return validated settings; `owned` are this tool's cache/state roots."""
    home = Path(home).resolve()
    path = settings_path(home)
    try:
        raw = json.loads(path.read_text()) if path.exists() else {}
    except ValueError as error:
        raise ConfigError('设置文件不是合法 JSON：{}'.format(error)) from error
    if not isinstance(raw, dict):
        raise ConfigError('设置文件必须是一个对象')
    unknown = set(raw) - set(DEFAULTS)
    if unknown:
        raise ConfigError('不认识的设置：' + '、'.join(sorted(unknown)))
    cfg = copy.deepcopy(DEFAULTS)
    cfg.update({k: v for k, v in raw.items() if k != 'ask'})
    ask = raw.get('ask', {})
    if not isinstance(ask, dict) or set(ask) - set(DEFAULTS['ask']):
        raise ConfigError('ask 里有不认识的设置')
    cfg['ask'].update(ask)
    if cfg['schema'] != 1:
        raise ConfigError('schema 只支持 1')
    for key in FLAGS:
        if not isinstance(cfg[key], bool):
            raise ConfigError(key + ' 必须是 true 或 false')
    _positive(cfg['check_interval_minutes'], 'check_interval_minutes')
    _positive(cfg['auto_idle_days'], 'auto_idle_days')
    for key, value in cfg['ask'].items():
        _positive(value, 'ask.' + key)
    names = cfg['prune_names']
    if not isinstance(names, list) or any(not isinstance(n, str) or not n or '/' in n or n in ('.', '..', '.git') for n in names):
        raise ConfigError('prune_names 必须是目录名列表')
    owned = [Path(p).resolve() for p in owned]
    if cfg['external_root'] is not None:
        external_root = Path(str(cfg['external_root']))
        if not external_root.is_absolute() or _too_broad(external_root, home, owned):
            raise ConfigError('external_root 必须是外接盘上的一个目录：' + str(external_root))
        cfg['external_root'] = external_root
    roots = []
    if not isinstance(cfg['task_roots'], list):
        raise ConfigError('task_roots 必须是列表')
    for i, entry in enumerate(cfg['task_roots']):
        label = 'task_roots[{}]'.format(i)
        if not isinstance(entry, dict) or set(entry) - {'path', 'external'} or 'path' not in entry:
            raise ConfigError(label + ' 只能包含 path 和 external')
        root = Path(str(entry['path']))
        if not root.is_absolute() or not root.is_dir():
            raise ConfigError(label + '.path 必须是已存在的绝对路径：' + str(root))
        root = root.resolve()
        if _too_broad(root, home, owned):
            raise ConfigError(label + '.path 范围过大：' + str(root))
        external = entry.get('external')
        if external is not None:
            external = Path(str(external))
            if not external.is_absolute():
                raise ConfigError(label + '.external 必须是绝对路径')
            # The drive may be unplugged now, so only reject what is wrong on its face.
            if _too_broad(external, home, owned) or external == root or root in external.parents or external in root.parents:
                raise ConfigError(label + '.external 范围过大或与任务根重叠：' + str(external))
        roots.append({'path': root, 'external': external})
    cfg['task_roots'] = roots
    return cfg


def read_raw(home):
    path = settings_path(home)
    return json.loads(path.read_text()) if path.exists() else {'schema': 1}


def update(home, changes, owned=()):
    """Merge changes into what the user wrote; nothing is saved unless the result is valid."""
    raw = read_raw(home)
    raw.update(changes)
    path = settings_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    previous = path.read_bytes() if path.exists() else None
    atomic_json(path, raw)
    try:
        return load(home, owned)
    except ConfigError:
        if previous is None:
            path.unlink()
        else:
            path.write_bytes(previous)
        raise


def set_flag(home, key, value):
    """Change one switch, keeping everything else the user wrote."""
    if key not in FLAGS or not isinstance(value, bool):
        raise ValueError('只能切换 ' + ' / '.join(FLAGS))
    path = settings_path(home)
    raw = read_raw(home)
    raw[key] = value
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_json(path, raw)
    return raw
