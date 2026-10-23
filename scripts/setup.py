#!/usr/bin/env python3
"""Install without overwriting existing tools; scheduling is an explicit step."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import plistlib
import shlex
import shutil
import subprocess
import sys

NAME = 'mac-dev-resource-guard'
SOURCE = Path(__file__).resolve().parents[1]
LABELS = {'memory': 'local.dev-resource-guard.memory', 'storage': 'local.dev-resource-guard.storage'}


def exclusive_write(path, data, mode=0o600):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('xb') as f:
        f.write(data)
    path.chmod(mode)


def install(home):
    canonical = home / '.codex/skills' / NAME
    links = {
        home/'.local/bin/dev-memory': canonical/'runtime/memory/scripts/dev_memory.py',
        home/'.local/bin/dev-storage': canonical/'runtime/storage/cli.py',
    }
    python = shlex.quote(sys.executable)
    scripts = {p: ('#!/bin/sh\nexec '+python+' '+shlex.quote(str(t))+' "$@"\n').encode() for p,t in links.items()}
    claude = home / '.claude/skills' / NAME
    # Preflight every destination before any installation mutation.
    if canonical.exists() and canonical.resolve() != SOURCE:
        for f in SOURCE.rglob('*'):
            if f.is_file() and '__pycache__' not in f.parts:
                other = canonical/f.relative_to(SOURCE)
                if not other.is_file() or other.read_bytes() != f.read_bytes():
                    raise ValueError('existing skill differs; keep it and review migration: '+str(canonical))
    for path, data in scripts.items():
        if path.exists() or path.is_symlink():
            if path.is_symlink() or path.read_bytes() != data:
                raise ValueError('existing command preserved; review migration: '+str(path))
    if (claude.exists() or claude.is_symlink()) and (not claude.is_symlink() or claude.resolve() != canonical.resolve()):
        raise ValueError('existing Claude skill preserved: '+str(claude))
    already_installed = canonical.exists() and all(path.is_file() and not path.is_symlink() and path.read_bytes() == data for path,data in scripts.items())
    if not already_installed and any((home/s).exists() for s in ('.local/state/dev-memory', '.local/state/dev-storage')):
        raise ValueError('existing runtime state preserved; review migration before installing')
    for kind,label in LABELS.items():
        if (home/'Library/LaunchAgents'/f'{label}.plist').exists():
            raise ValueError('scheduler already installed; use existing installation')
    if not canonical.exists():
        canonical.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(SOURCE, canonical, ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
    for path,data in scripts.items():
        if not path.exists(): exclusive_write(path,data,0o755)
    if not claude.exists():
        claude.parent.mkdir(parents=True,exist_ok=True)
        claude.symlink_to(canonical, target_is_directory=True)
    config = home/'.config/dev-resource-guard/config.json'
    if not config.exists():
        capacity = shutil.disk_usage(home).total/2**30
        cfg = {'target_free_gib': round(capacity*.20, 1), 'warning_free_gib': round(capacity*.15, 1),
               'critical_free_gib': round(capacity*.10, 1), 'intermediate_budget_gib': round(capacity*.10, 1)}
        exclusive_write(config,(json.dumps(cfg,indent=2)+'\n').encode())
    state = home/'.local/state/dev-memory'
    fresh = not state.exists()
    state.mkdir(parents=True,exist_ok=True,mode=0o700)
    if fresh: exclusive_write(state/'paused',b'')
    print(json.dumps({'skill':str(canonical),'claude_same_realpath':claude.resolve()==canonical.resolve(),
                      'memory_paused':(state/'paused').exists(),'scheduled':False,'config':str(config)},indent=2))


def scheduler(home, kind, enable):
    if home.resolve()!=Path.home().resolve() or sys.platform!='darwin':
        raise ValueError('scheduler only operates on current macOS user')
    label=LABELS[kind]; path=home/'Library/LaunchAgents'/f'{label}.plist'
    domain=f'gui/{os.getuid()}'
    if not enable:
        if not path.exists():
            print('No scheduler installed by this skill.'); return
        result=subprocess.run(['launchctl','bootout',domain+'/'+label],capture_output=True,text=True)
        if result.returncode:
            probe=subprocess.run(['launchctl','print',domain+'/'+label],capture_output=True)
            if probe.returncode==0: raise ValueError('scheduler still loaded; file retained')
        path.unlink(); print('Disabled '+label); return
    command=home/'.local/bin'/('dev-memory' if kind=='memory' else 'dev-storage')
    expected = ('#!/bin/sh\nexec '+shlex.quote(sys.executable)+' '+shlex.quote(str(home/'.codex/skills'/NAME/('runtime/memory/scripts/dev_memory.py' if kind=='memory' else 'runtime/storage/cli.py')))+' "$@"\n').encode()
    if not command.is_file() or command.is_symlink() or command.read_bytes()!=expected:
        raise ValueError('install first; existing command is not this installation')
    # Refuse another known scheduler. Also inspect host automations manually per skill.
    for other in (home/'Library/LaunchAgents').glob('*.plist'):
        try: body=plistlib.loads(other.read_bytes())
        except Exception: continue
        args=' '.join(body.get('ProgramArguments',[]))
        if ('dev-memory' if kind=='memory' else 'dev-storage') in args or (kind=='memory' and 'dev-idle-cleanup' in str(other)):
            raise ValueError('existing scheduler preserved: '+str(other))
    state=home/'.local/state/dev-resource-guard'; state.mkdir(parents=True,exist_ok=True,mode=0o700)
    args=[str(command),'tick'] if kind=='memory' else [str(command),'maintain','--apply']
    body={'Label':label,'ProgramArguments':args,'StartInterval':60 if kind=='memory' else 43200,
          'RunAtLoad':False, 'EnvironmentVariables':{'PATH':os.environ.get('PATH','/usr/bin:/bin:/usr/sbin:/sbin')},
          'StandardOutPath':'/dev/null','StandardErrorPath':str(state/(kind+'-scheduler.err'))}
    if os.environ.get('DEV_MEMORY_BROKER_SOURCE'):
        body['EnvironmentVariables']['DEV_MEMORY_BROKER_SOURCE']=os.environ['DEV_MEMORY_BROKER_SOURCE']
    exclusive_write(path,plistlib.dumps(body))
    result=subprocess.run(['launchctl','bootstrap',domain,str(path)],capture_output=True,text=True)
    if result.returncode:
        path.unlink(); raise ValueError('launchctl bootstrap failed; scheduler file removed')
    check=subprocess.run(['launchctl','print',domain+'/'+label],capture_output=True)
    if check.returncode: raise ValueError('scheduler loaded but verification unavailable; inspect launchctl')
    print('Enabled '+label+('. Memory remains paused until dev-memory resume.' if kind=='memory' else ''))


def doctor(home):
    cli=home/'.local/bin/devlease'
    info={'platform':sys.platform,'python':sys.version.split()[0], 'memory_scheduler':(home/'Library/LaunchAgents'/f'{LABELS["memory"]}.plist').exists(),
          'storage_scheduler':(home/'Library/LaunchAgents'/f'{LABELS["storage"]}.plist').exists(),
          'devlease_present':cli.exists(),'device_reclaim':'requires working memory-inspect/memory-reap; absent or unknown retains all devices',
          'mcp_reclaim':'disabled unless DEV_MEMORY_BROKER_SOURCE points to a compatible protocol implementation',
          'gradle_versions':['8.14','8.11.1'],'xcodebuild':shutil.which('xcodebuild'),
          'memory_paused':(home/'.local/state/dev-memory/paused').exists()}
    print(json.dumps(info,ensure_ascii=False,indent=2))


def main():
    p=argparse.ArgumentParser();p.add_argument('--home',type=Path,default=Path.home(),help='isolated installation tests only; normally omit')
    sub=p.add_subparsers(dest='cmd',required=True)
    sub.add_parser('install');sub.add_parser('doctor')
    for verb in ('enable-scheduler','disable-scheduler'):
        q=sub.add_parser(verb);q.add_argument('kind',choices=['memory','storage'])
    a=p.parse_args(); home=a.home.expanduser().resolve()
    if a.cmd=='install': install(home)
    elif a.cmd=='doctor': doctor(home)
    else: scheduler(home,a.kind,a.cmd=='enable-scheduler')

if __name__=='__main__':
    try: main()
    except (OSError,ValueError) as e:
        print(str(e),file=sys.stderr);sys.exit(2)
