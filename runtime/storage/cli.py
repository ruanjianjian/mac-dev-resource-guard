#!/usr/bin/env python3
"""Local storage entry point, shared by Claude and Codex."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
import time

from storage import GIB, Settings, StorageManager, atomic_json, summarize
from slots import run_xcode, slot_identity, slot_paths
from portable_config import configure


def emit(value):
    print(json.dumps(value, ensure_ascii=False, indent=2), flush=True)


def owner():
    return os.environ.get('CLAUDE_CODE_SESSION_ID') or os.environ.get('CODEX_THREAD_ID') or os.environ.get('CODEX_SESSION_ID') or ('terminal:' + str(os.getuid()))


def main(argv=None):
    parser = argparse.ArgumentParser(description='Check space, reuse build slots and reclaim only expired generated caches.')
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('status', help='Fast physical free space and policy; no recursive scan')
    check = sub.add_parser('check', help='Advisory free-space check; never blocks a coding build')
    check.add_argument('--required-gib', type=int, help='Advisory reserve for this check; does not block builds')
    plan = sub.add_parser('plan', help='Read-only scan with active-use protection')
    plan.add_argument('--output', type=Path)
    maintain = sub.add_parser('maintain', help='Preview by default; --apply performs bounded cleanup')
    maintain.add_argument('--apply', action='store_true')
    maintain.add_argument('--scan', action='store_true', help='Scan even when healthy and scanned in the last 24 hours')
    pin = sub.add_parser('pin', help='Protect cache/worktree while awaiting acceptance')
    pin.add_argument('path', type=Path)
    pin.add_argument('--reason', required=True)
    pin.add_argument('--days', type=int)
    unpin = sub.add_parser('unpin', help='Release only your own pin')
    unpin.add_argument('path', type=Path)
    xcode = sub.add_parser('xcode', help='Run xcodebuild in a stable, exclusive DerivedData slot')
    xcode.add_argument('--worktree', type=Path, required=True)
    xcode.add_argument('--platform', choices=['ios-device', 'ios-simulator', 'macos'], required=True)
    xcode.add_argument('--configuration', default='Profile')
    xcode.add_argument('argv', nargs=argparse.REMAINDER)
    paths = sub.add_parser('xcode-path', help='Query path; this does not acquire a build lease')
    paths.add_argument('--worktree', type=Path, required=True)
    paths.add_argument('--platform', choices=['ios-device', 'ios-simulator', 'macos'], required=True)
    paths.add_argument('--configuration', default='Profile')
    deps = sub.add_parser('deps', help='Serialize shared pub/CocoaPods cache mutation without changing cache paths')
    deps.add_argument('--family', choices=['pub', 'pods'], required=True)
    deps.add_argument('argv', nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    settings = configure()
    manager = StorageManager(settings)
    if args.command in {'status', 'check'}:
        free = manager.free_bytes()
        value = {'free_gib': round(free / GIB, 2), 'target_free_gib': settings.target_free_gib,
                 'warning_free_gib': settings.warning_free_gib, 'critical_free_gib': settings.critical_free_gib,
                 'intermediate_budget_gib': settings.intermediate_budget_gib,
                 'status': 'critical' if free < settings.critical_free_gib * GIB else ('warning' if free < settings.warning_free_gib * GIB else 'healthy')}
        if args.command == 'check':
            if args.required_gib is not None and args.required_gib < 1:
                raise ValueError('required-gib must be positive')
            required = max(settings.critical_free_gib, args.required_gib or settings.critical_free_gib)
            value['recommended_free_gib'] = required
            value['below_recommended_reserve'] = free < required * GIB
            value['build_allowed'] = True
            value['advisory_only'] = True
        emit(value)
        return 0
    if args.command in {'pin', 'unpin'}:
        if args.command == 'pin':
            manager.pin(args.path, args.reason, owner(), days=args.days)
        else:
            manager.unpin(args.path, owner())
        emit({'status': 'ok', 'command': args.command, 'path': str(args.path.resolve())})
        return 0
    if args.command == 'xcode-path':
        emit(slot_paths(slot_identity(args.worktree, args.platform, args.configuration)))
        return 0
    if args.command in {'xcode', 'deps'}:
        command = args.argv[1:] if args.argv[:1] == ['--'] else args.argv
        if not command:
            raise ValueError('supply a command after --')
        if args.command == 'xcode':
            return run_xcode(args.worktree, args.platform, args.configuration, command)
        directory = manager.state_root / 'dependency-locks'
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        with open(directory / (args.family + '.lock'), 'a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            return subprocess.run(command, shell=False, pass_fds=(lock.fileno(),)).returncode
    if args.command == 'maintain' and not args.scan and manager.free_bytes() >= settings.target_free_gib * GIB:
        last = manager.state_root / 'last-plan.json'
        try:
            if time.time() - json.loads(last.read_text())['created'] < 86400:
                emit({'status': 'healthy', 'scan': 'not due', 'free_gib': round(manager.free_bytes() / GIB, 2)})
                return 0
        except (OSError, ValueError, KeyError, TypeError):
            pass
    manager.state_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    with open(manager.state_root / 'scan.lock', 'a') as scanning:
        try:
            fcntl.flock(scanning, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            emit({'status': 'busy', 'reason': 'another storage scan is running'})
            return 0
        if args.command == 'maintain':
            with open(manager.lock_path, 'a') as maintenance:
                try:
                    fcntl.flock(maintenance, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    emit({'status': 'busy', 'reason': 'another maintenance is running'})
                    return 0
        emit({'phase': 'planning', 'mode': 'apply' if args.command == 'maintain' and args.apply else 'read-only'})
        result = manager.plan()
        atomic_json(manager.state_root / 'last-plan.json', result)
        if args.command == 'plan' and args.output:
            atomic_json(args.output, result)
        emit({'phase': 'plan_complete', **summarize(result), 'plan_path': str(manager.state_root / 'last-plan.json')})
        if args.command == 'maintain' and args.apply:
            applied = manager.apply(result)
            emit(applied)
            return 2 if applied['status'] == 'error' else 0
        return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        print('dev-storage: ' + str(error), file=sys.stderr)
        sys.exit(2)
