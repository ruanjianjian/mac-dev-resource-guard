"""Protocol-only resource adapters. Never signal a discovered process."""
import importlib
import json
import os
from pathlib import Path
import subprocess
import time


def adapter(name):
    return importlib.import_module((__package__ + "." if __package__ else "") + name)


DEVLEASE = Path.home() / '.local/bin/devlease'
SIM_IDLE_AUTO = 2 * 3600   # not connected to any dev tool this long: closed automatically when memory is tight


def simulator_inspect(run=subprocess.run, now=None):
    """Booted iOS simulators when devlease is not installed: who is connected and how much memory each
    takes (the whole process tree under its launchd_sim). Read-only: nothing here is shut down."""
    now = time.time() if now is None else now
    listed = run(['/usr/bin/xcrun', 'simctl', 'list', 'devices', 'booted', '-j'], capture_output=True, text=True, timeout=20)
    if listed.returncode:
        raise RuntimeError('simctl unavailable')
    devices = [d for group in json.loads(listed.stdout).get('devices', {}).values() for d in group]
    table = run(['/bin/ps', '-axww', '-o', 'pid=,ppid=,rss=,command='], capture_output=True, text=True,
                errors='surrogateescape', env=dict(os.environ, LC_ALL='en_US.UTF-8'), timeout=20).stdout
    procs = []
    for line in table.splitlines():
        parts = line.split(None, 3)
        if len(parts) == 4 and parts[0].isdigit() and parts[1].isdigit() and parts[2].isdigit():
            procs.append((int(parts[0]), int(parts[1]), int(parts[2]), parts[3]))
    children = {}
    for pid, ppid, _, _ in procs:
        children.setdefault(ppid, []).append(pid)
    # The check itself and whatever started it (the panel's backend, an agent's shell) name this simulator in their
    # own command line; they are not a dev tool using it (10-30: 「关掉所选」 always answered 「刚有开发工具连上」).
    mine, seen, parents = set(), os.getpid(), {pid: ppid for pid, ppid, _, _ in procs}
    while seen and seen not in mine and seen != 1:
        mine.add(seen)
        seen = parents.get(seen)
    rss = {pid: kib for pid, _, kib, _ in procs}
    rows = []
    for device in devices:
        udid = device.get('udid', '')
        tree, todo = set(), [pid for pid, _, _, cmd in procs if 'launchd_sim' in cmd and udid in cmd]
        while todo:
            pid = todo.pop()
            if pid not in tree:
                tree.add(pid)
                todo += children.get(pid, [])
        tools = sorted({Path(cmd.split()[0]).name for pid, _, _, cmd in procs
                        if udid and udid in cmd and pid not in tree and pid not in mine and 'CoreSimulator' not in cmd})
        rows.append(dict(id=udid, device_id=udid, kind='ios-sim', name=device.get('name', udid), pids=sorted(tree),
                         rss_kib=sum(rss.get(pid, 0) for pid in tree), attached=tools, eligible=False,
                         reason='simulator_in_use' if tools else 'simulator_not_attached', _observed_at=now))
    return rows


def shutdown_idle_simulator(udid, run=subprocess.run):
    """Shut down one simulator, checked again right before: still running and still no dev tool connected.
    Only its screen and running apps are lost; its data stays."""
    row = next((r for r in simulator_inspect(run=run) if r['id'] == udid), None)
    if row is None:
        return {'stopped': True, 'accepted': False, 'reason': 'already_shut_down'}
    if row['reason'] != 'simulator_not_attached':
        return {'stopped': False, 'accepted': False, 'reason': 'simulator_in_use', 'name': row['name'], 'attached': row['attached']}
    out = run(['/usr/bin/xcrun', 'simctl', 'shutdown', udid], capture_output=True, text=True, timeout=60)
    if out.returncode:
        return {'stopped': False, 'accepted': False, 'name': row['name'],
                'reason': (out.stderr or 'simctl shutdown failed').strip()[-200:]}
    return {'stopped': True, 'accepted': True, 'reason': 'simulator_shut_down', 'rss_kib': row['rss_kib'], 'name': row['name']}


def device_inspect():
    if not DEVLEASE.exists():
        return simulator_inspect()
    result = subprocess.run([str(DEVLEASE), 'memory-inspect', '--json'],
                            capture_output=True, text=True, timeout=20)
    if result.returncode or len(result.stdout) > 1024 * 1024:
        raise RuntimeError('device inspection unavailable')
    body = json.loads(result.stdout)
    rows = []
    for raw in body['candidates']:
        if raw.get('kind') not in ('ios-sim', 'android-emu'):
            continue
        row = dict(raw)
        row.update(id=raw['device_id'], fingerprint=raw['lease_signature'], _observed_at=body['observed_at'])
        rows.append(row)
    return rows


def private_metadata(value):
    """Defense in depth: never persist credentials returned by an adapter."""
    if isinstance(value, dict):
        return {k: private_metadata(v) for k, v in value.items()
                if not any(s in k.lower() for s in ('token', 'secret', 'password', 'account', 'phone'))}
    if isinstance(value, list):
        return [private_metadata(v) for v in value]
    return value


def collect(state, now):
    public, records, observations = [], {}, {}
    previous = state.get('external_observations', {})
    pending = state.get('external_pending', {})
    idle_since, seen_idle = state.get('simulator_idle_since', {}), {}
    for kind, discover in [('simulator', device_inspect),
                           ('gradle', lambda: adapter('gradle_reclaim').inspect(now=now)),
                           ('mcp', lambda: adapter('mcp_reclaim').inspect(now=now))]:
        try:
            rows = discover()
            if not isinstance(rows, list) or len(rows) > 200:
                raise ValueError('invalid resource inventory')
            for raw in rows:
                key = kind + ':' + str(raw['id'])
                if kind == 'simulator' and 'lease_signature' not in raw:
                    # Our own check (no devlease): count how long nothing has been connected.
                    if raw.get('reason') == 'simulator_not_attached':
                        since = idle_since.get(key, now)
                        seen_idle[key] = since
                        raw.update(unattached_since=since, eligible=now - since >= SIM_IDLE_AUTO,
                                   fingerprint='idle:{}:{:.0f}'.format(raw['id'], since))
                row = private_metadata({k: raw.get(k) for k in ('id', 'pid', 'start', 'reason', 'fingerprint', 'evidence', 'name', 'rss_kib', 'attached', 'unattached_since')
                                        if k in raw or k in ('id', 'pid', 'start', 'reason', 'fingerprint', 'evidence')})
                row.update(kind=kind, id=key, eligible=False)
                records[key] = raw
                fp = raw.get('fingerprint')
                if key in pending:
                    row['reason'] = 'pending_readonly_no_retry'
                elif raw.get('eligible') is True and isinstance(fp, str) and fp:
                    old = previous.get(key, {})
                    continuous = old.get('fingerprint') == fp and 0 <= now - old.get('at', 0) <= 180
                    first = old.get('since', now) if continuous else now
                    observations[key] = {'since': first, 'at': now, 'fingerprint': fp}
                    row['eligible'] = now - first >= 30
                    if not row['eligible']:
                        row['reason'] = 'awaiting_second_idle_check'
                public.append(row)
        except (OSError, ValueError, RuntimeError, KeyError, TypeError, ImportError, subprocess.TimeoutExpired) as exc:
            public.append({'kind': kind, 'id': kind + ':unavailable', 'eligible': False,
                           'reason': 'inspection_unavailable_preserved', 'error': type(exc).__name__})
    state['external_observations'] = observations
    state['simulator_idle_since'] = seen_idle
    return public, records


def reclaim(kind, raw, state_dir):
    if kind in ('gradle', 'mcp'):
        module = adapter('gradle_reclaim' if kind == 'gradle' else 'mcp_reclaim')
        return module.reclaim(raw)
    if kind != 'simulator':
        return {'stopped': False, 'reason': 'unsupported_resource'}
    if 'lease_signature' not in raw:
        return shutdown_idle_simulator(raw['id'])
    # This short-lived metadata file never contains account data or credentials.
    # The authoritative devlease endpoint rechecks owner and full-lease CAS.
    request = Path(state_dir) / 'device-reclaim-request.json'
    payload = {'version': 1, 'observed_at': raw['_observed_at'], 'candidates': [raw]}
    fd = os.open(request, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, 'w') as f:
            json.dump(payload, f)
        result = subprocess.run([str(Path.home() / '.local/bin/devlease'), 'memory-reap',
                                 '--expected-file', str(request), '--json'],
                                capture_output=True, text=True, timeout=90)
        if result.returncode or len(result.stdout) > 1024 * 1024:
            return {'stopped': False, 'unknown': True, 'reason': 'device_outcome_unknown_no_retry'}
        body = json.loads(result.stdout)
        answer = body['results'][0] if body.get('results') else {'stopped': False, 'reason': 'device_revalidation_preserved'}
        answer['stopped'] = answer.get('closed') is True or answer.get('stopped') is True
        if answer.get('attempted') is not True and not answer['stopped']:
            # A temporary lease reservation that was cancelled is not an
            # accepted device shutdown; it is safe to observe it afresh.
            answer['accepted'] = False
        return answer
    except (OSError, ValueError, KeyError, subprocess.TimeoutExpired):
        return {'stopped': False, 'unknown': True, 'reason': 'device_outcome_unknown_no_retry'}
    finally:
        request.unlink(missing_ok=True)
