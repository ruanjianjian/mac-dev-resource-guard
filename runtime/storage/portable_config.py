"""Portable configuration; managed cache roots only by default."""
import json
import math
import os
from pathlib import Path
from storage import Settings
import slots


def config_path():
    return Path(os.environ.get('DEV_RESOURCE_CONFIG', str(Path.home()/'.config/dev-resource-guard/config.json')))


def configure(home=None):
    explicit_home = home is not None
    home = Path(home).resolve() if explicit_home else Path.home().resolve()
    path = home / '.config/dev-resource-guard/config.json' if explicit_home else config_path()
    cfg = json.loads(path.read_text()) if path.exists() else {}
    if not isinstance(cfg, dict):
        raise ValueError('configuration must be an object')
    allowed = {'cache_root', 'state_root', 'volume', 'target_free_gib', 'warning_free_gib',
               'critical_free_gib', 'intermediate_budget_gib'}
    if set(cfg) - allowed:
        raise ValueError('unknown configuration keys: ' + ', '.join(sorted(set(cfg)-allowed)))
    def expand(value):
        value = str(value)
        return (home / value[2:] if value.startswith('~/') else Path(value)).resolve()
    cache = expand(cfg.get('cache_root', home / 'Library/Caches/dev-resource-guard'))
    state = expand(cfg.get('state_root', home / '.local/state/dev-storage'))
    # Never allow a typo to turn an entire home/volume into the maintenance root.
    if cache == home or cache in home.parents or state == cache or state.is_relative_to(cache):
        raise ValueError('cache must be a dedicated directory, with state outside it')
    values = {k: cfg[k] for k in allowed if k.endswith('_gib') and k in cfg}
    if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v <= 0 for v in values.values()):
        raise ValueError('GiB values must be finite and positive')
    volume = expand(cfg.get('volume', str(home)))
    if not volume.is_dir():
        raise ValueError('volume path must exist')
    settings = Settings(cache_roots=[cache], work_roots=[], state_root=state, volume=volume,
                        lease_root=home / '.device-leases/leases', **values)
    if not settings.critical_free_gib <= settings.warning_free_gib <= settings.target_free_gib:
        raise ValueError('expected critical <= warning <= target')
    slots.DEFAULT_CACHE_ROOT = cache / 'shared/xcode'
    slots.DEFAULT_STATE_ROOT = state
    return settings
