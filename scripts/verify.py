#!/usr/bin/env python3
"""Run only disposable fixtures with an isolated HOME, never enable services."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile

root=Path(__file__).resolve().parents[1]
with tempfile.TemporaryDirectory(prefix='mac-resource-verify-') as temp:
    env={**os.environ,'HOME':temp,'PYTHONDONTWRITEBYTECODE':'1'}
    for key in ['DEV_RESOURCE_CONFIG','DEV_MEMORY_STATE_DIR','DEV_MEMORY_GRADLE_INTEGRATION','DEV_MEMORY_BROKER_INTEGRATION','DEV_MEMORY_BROKER_SOURCE','DEV_MEMORY_GRADLE_REQUEST_DIR']:
        env.pop(key,None)
    results=[]
    for folder in [root/'runtime/memory',root/'runtime/storage',root]:
        r=subprocess.run([sys.executable,'-m','unittest','discover','-s','tests','-v'],cwd=folder,env=env)
        results.append(r.returncode)
    sys.exit(0 if all(x==0 for x in results) else 1)
