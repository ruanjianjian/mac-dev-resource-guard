#!/usr/bin/env python3
"""Real launchd automatic cleanup of a disposable cache, never the user's cache."""
import argparse
import contextlib
import json
import os
from pathlib import Path
import subprocess
import time
import uuid

p=argparse.ArgumentParser();p.add_argument('--home',required=True,type=Path);a=p.parse_args()
home=a.home.resolve();home.mkdir()  # Must be new, never reuse a real HOME.
os.environ['HOME']=str(home)
for key in ('DEV_RESOURCE_CONFIG','DEV_MEMORY_STATE_DIR','DEV_MEMORY_BROKER_SOURCE'):os.environ.pop(key,None)
import service
service.LABELS['storage']='local.dev-resource-guard.test.'+uuid.uuid4().hex+'.storage'
leaf=home/'Library/Developer/Xcode/DerivedData/Disposable/Build/Intermediates.noindex'
leaf.mkdir(parents=True);(leaf/'object.o').write_bytes(b'x'*1024**2)
old=time.time()-14*86400
os.utime(leaf/'object.o',(old,old));os.utime(leaf,(old,old))
protected=home/'Library/Developer/Xcode/DerivedData/Protected/Index.noindex'
protected.mkdir(parents=True);(protected/'keep').write_text('keep')
service.common.manager(home).pin(protected,'test protection','fixture')
products=leaf.parent/'Products';products.mkdir();(products/'app').write_text('keep')
label=service.LABELS['storage'];domain=f'gui/{os.getuid()}'
try:
    service.adopt_config(home);runtime=service.install_runtime(home)
    result=service.enable_job(home,'storage',runtime)
    receipt=service.state_root(home)/'automatic-storage.json'
    deadline=time.monotonic()+90
    while time.monotonic()<deadline:
        if receipt.exists() and json.loads(receipt.read_text()).get('status')=='complete':break
        time.sleep(.5)
    body=json.loads(receipt.read_text())
    assert body['status']=='complete',body
    assert not leaf.exists() and protected.exists() and products.exists()
    assert len(body['results'][1]['result']['deleted'])==1
    print(json.dumps({'automatic_on_load':True,'deleted_bytes':1024**2,'pin_preserved':True,'products_preserved':True,'receipt':body},ensure_ascii=False))
finally:
    subprocess.run(['launchctl','bootout',domain+'/'+label],capture_output=True)
    (home/'Library/LaunchAgents'/f'{label}.plist').unlink(missing_ok=True)
    assert subprocess.run(['launchctl','print',domain+'/'+label],capture_output=True).returncode!=0
