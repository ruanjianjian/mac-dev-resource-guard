"""Create explicit fake CLI endpoints for native UI testing. Never reclaims resources."""
from pathlib import Path
import sys

home = Path(sys.argv[1]).resolve()
home.mkdir(parents=True, exist_ok=True)
(home / '.resource-dashboard-fixture').touch()
tools = home / '.local/bin'
tools.mkdir(parents=True, exist_ok=True)
source = r'''
import json,os,sys,time
from pathlib import Path
home=Path(__file__).resolve().parents[2]
name=Path(sys.argv[0]).name
args=sys.argv[1:]
with (home/'calls.jsonl').open('a') as stream:
    stream.write(json.dumps([name]+args)+'\n')
if (home/'busy').exists():
    print(json.dumps({'status':'busy'}));sys.exit(0)
if name=='dev-storage':
    if args[0]=='plan':
        data={'schema_version':1,'created':time.time(),'candidates':[
            {'path':'/isolated-fixture/old/Build/Intermediates.noindex','decision':'eligible','reasons':[], 'stats':{'allocated_bytes':4*1024**3}},
            {'path':'/isolated-fixture/acceptance/Index.noindex','decision':'keep','reasons':['explicitly pinned for acceptance/use'],'stats':{'allocated_bytes':1024**3}},
            {'path':'/isolated-fixture/active/ModuleCache.noindex','decision':'keep','reasons':['build slot is locked'],'stats':{'allocated_bytes':2*1024**3}}
        ]}
        Path(args[args.index('--output')+1]).write_text(json.dumps(data))
        print(json.dumps({'phase':'planning'}));print(json.dumps({'phase':'plan_complete'}))
    else:
        print(json.dumps({'status':'ok','deleted':[{'path':'/isolated-fixture/old/Build/Intermediates.noindex'}],
                          'kept':[{'reason':'pinned'},{'reason':'active'}],'errors':[], 'observed_free_delta_bytes':0}))
else:
    clean=args[0]=='clean'
    body={'paused':False, 'managed_resources':[], 'external_resources':[
        {'id':'gradle:fixture','kind':'gradle','pid':12345,'eligible':True,'reason':'managed_idle'},
        {'id':'mcp:fixture','kind':'mcp','eligible':False,'reason':'pending_readonly_no_retry'}],
        'actions':[{'result':{'accepted':True,'stopped':False}}] if clean else [],
        'pending_requests':[{}] if clean else [], 'protected_devices':1,
        'top_memory':[{'pid':123,'name':'Xcode','mem':'1G','policy':'保留：正在开发'}]}
    print(json.dumps(body))
'''
for name in ('dev-storage', 'dev-memory'):
    path = tools / name
    path.write_text('#!' + sys.executable + '\n' + source)
    path.chmod(0o700)
print(home)
