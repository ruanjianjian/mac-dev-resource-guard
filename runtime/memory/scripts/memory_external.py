"""Protocol-only resource adapters. Never signal a discovered process."""
import importlib
import json
import os
from pathlib import Path
import subprocess
import time


def adapter(name):
    return importlib.import_module((__package__ + "." if __package__ else "") + name)


def device_inspect():
    result = subprocess.run([str(Path.home() / '.local/bin/devlease'), 'memory-inspect', '--json'],
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
    for kind, discover in [('simulator', device_inspect),
                           ('gradle', lambda: adapter('gradle_reclaim').inspect(now=now)),
                           ('mcp', lambda: adapter('mcp_reclaim').inspect(now=now))]:
        try:
            rows = discover()
            if not isinstance(rows, list) or len(rows) > 200:
                raise ValueError('invalid resource inventory')
            for raw in rows:
                key = kind + ':' + str(raw['id'])
                row = private_metadata({k: raw.get(k) for k in ('id', 'pid', 'start', 'reason', 'fingerprint', 'evidence')})
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
    return public, records


def reclaim(kind, raw, state_dir):
    if kind in ('gradle', 'mcp'):
        module = adapter('gradle_reclaim' if kind == 'gradle' else 'mcp_reclaim')
        return module.reclaim(raw)
    if kind != 'simulator':
        return {'stopped': False, 'reason': 'unsupported_resource'}
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
