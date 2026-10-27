#!/usr/bin/env python3
"""Opt-in launchd integration using an empty test HOME and unique service labels."""
import argparse
import contextlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import time
import uuid

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--home', required=True, type=Path, help='new disposable directory; must not already exist')
args = parser.parse_args()
home = args.home.resolve()
home.mkdir()  # Refuse any existing HOME, including the real user home.
os.environ['HOME'] = str(home)
for key in ('DEV_MEMORY_STATE_DIR', 'DEV_RESOURCE_CONFIG', 'DEV_MEMORY_BROKER_SOURCE'):
    os.environ.pop(key, None)
spec = importlib.util.spec_from_file_location('isolated_setup', Path(__file__).with_name('setup.py'))
setup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(setup)
setup.LABELS = {kind: 'local.dev-resource-guard.test.' + uuid.uuid4().hex + '.' + kind for kind in ('memory', 'storage')}
results = []
try:
    setup.install(home)
    for kind in ('memory', 'storage'):
        setup.scheduler(home, kind, True)
        service = 'gui/{}/{}'.format(os.getuid(), setup.LABELS[kind])
        subprocess.run(['/bin/launchctl', 'kickstart', service], check=True)
        result_path = home / ('.local/state/dev-memory/latest.json' if kind == 'memory' else '.local/state/dev-storage/last-maintenance.json')
        deadline = time.monotonic() + 45
        while not result_path.exists() and time.monotonic() < deadline:
            time.sleep(.25)
        body = json.loads(result_path.read_text())
        if kind == 'memory':
            assert body['paused'] is True and body['actions'] == []
        else:
            assert body['deleted'] == [] and body['errors'] == []
        setup.scheduler(home, kind, False)
        assert subprocess.run(['/bin/launchctl', 'print', service], capture_output=True).returncode != 0
        results.append({'kind': kind, 'loaded': True, 'executed': True, 'removed': True, 'resource_actions': 0})
finally:
    for kind in ('memory', 'storage'):
        with contextlib.suppress(Exception):
            setup.scheduler(home, kind, False)
print(json.dumps(results, indent=2))
