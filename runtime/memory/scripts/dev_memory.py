#!/usr/bin/python3
"""Observe macOS pressure and reclaim resources through ownership protocols.

Discovered PIDs are never signalled. Devices use lease CAS, Gradle uses its
single-daemon graceful protocol, and MCP brokers atomically drain if idle.
"""
from __future__ import annotations

import argparse
import contextlib
import ctypes
import dataclasses
import errno
import fcntl
import importlib
import json
import os
from pathlib import Path
import re
import signal
import shutil
import socket
import subprocess
import sys
import time
import uuid

VERSION = "2.1.0"
STATE = Path(os.environ.get("DEV_MEMORY_STATE_DIR", str(Path.home() / ".local/state/dev-memory")))
LEASES = Path.home() / ".device-leases/leases"
STACKS = Path.home() / ".local/state/dev-resource-guard/stacks"
MAX_JSON = 1024 * 1024
NON_RECLAIMABLE = re.compile(
    r"/Applications/|/System/|/usr/libexec/|app-server-broker|\bcodex\b|\bclaude\b|"
    r"Feishu Codex Bot|GradleDaemon|KotlinCompileDaemon|\bgradlew?\b|\bxcodebuild\b|"
    r"frontend_server|flutter_tools|flutter_tester|\bflutter\b|swift-frontend|SWBBuild|"
    r"\b(?:vitest|jest|maestro.*test|iproxy|lark-cli|qemu-system)\b", re.I)


@dataclasses.dataclass
class Process:
    pid: int
    ppid: int
    uid: int
    start: str
    comm: str
    command: str
    rss_kib: int = 0


def pressure_transition(state, sample, now):
    s = dict(state)
    last = s.get("last_ts")
    reboot = s.get("boot_id") not in (None, sample.get("boot_id"))
    # A slow or missed sample (the monitor timed out) restarts the cleanup streak but not the
    # incident, so a timeout never delays the warning; a reboot, a backward clock or a long
    # sleep (15 minutes) starts over.
    reset = last is not None and (now < last or now - last > 180) or reboot
    if reset:
        for key in ("warn_count", "normal_count", "critical_since", "observations", "external_observations"):
            s.pop(key, None)
    if last is not None and (now < last or now - last > INCIDENT_GAP) or reboot:
        for key in ("incident_started", "notification_sent", "notification_level", "notification_at"):
            s.pop(key, None)
    p = sample.get("pressure")
    s.update(last_ts=now, boot_id=sample.get("boot_id"))
    event = None
    if p not in (1, 2, 4):
        s.update(warn_count=0, normal_count=0, critical_since=None)
        return s, None
    if p == 1:
        increment = last is None or reset or now - last >= 45
        s.update(warn_count=0, critical_since=None, normal_count=s.get("normal_count", 0) + int(increment))
        if s["normal_count"] >= 5:
            s.update(incident_started=None, notification_sent=False, notification_level=None)
    else:
        s["normal_count"] = 0
        if s.get("incident_started") is None:
            s.update(incident_started=now, notification_sent=False)
        if p == 2:
            increment = last is None or reset or now - last >= 45
            s.update(warn_count=s.get("warn_count", 0) + int(increment), critical_since=None)
            confirmed = s["warn_count"] >= 3
        else:
            s["warn_count"] = 0
            if s.get("critical_since") is None:
                s["critical_since"] = now
            confirmed = now - s["critical_since"] >= 15
        if confirmed and (s.get("last_cleanup") is None or now - s["last_cleanup"] >= 600):
            event = "candidate"
    return s, event


ALERT_AFTER = 120        # warning-level pressure this long before the first alert; critical alerts at once
REMIND_AFTER = 1800     # still under pressure this long after the last alert: say so again
INCIDENT_GAP = 900      # a gap between samples longer than this ends the incident


def alert_due(state, pressure, now):
    """Whether to alert now: first alert, escalation to critical, or a reminder; never after a delivery
    failure was recorded as sent."""
    incident = state.get("incident_started")
    if pressure not in (2, 4) or incident is None:
        return False
    if not state.get("notification_sent"):
        return pressure == 4 or now - incident >= ALERT_AFTER
    if pressure == 4 and state.get("notification_level") != 4:
        return True
    return now - (state.get("notification_at") or incident) >= REMIND_AFTER


def classify_process(proc, registered, protected_pids, knowledge_ok):
    if not knowledge_ok:
        return "knowledge_unavailable"
    if proc.pid in protected_pids or proc.uid != os.getuid():
        return "protected"
    if not registered:
        return "unregistered"
    if registered.get("pid") != proc.pid or registered.get("start") != proc.start:
        return "identity_changed"
    if NON_RECLAIMABLE.search(proc.comm + " " + proc.command):
        return "non_reclaimable"
    if registered.get("kind") not in ("tail", "mcp"):
        return "non_reclaimable"
    if registered.get("owner_dead") is not True:
        return "active_owner"
    if any(registered.get(k) is not True for k in ("disposable", "supervised")) or registered.get("state") != "idle":
        return "non_reclaimable"
    return "managed_idle"


class BSDInfo(ctypes.Structure):
    _fields_ = [(n, ctypes.c_uint32) for n in (
        "flags", "status", "xstatus", "pid", "ppid", "uid", "gid", "ruid", "rgid", "svuid", "svgid", "reserved")]
    _fields_ += [("comm", ctypes.c_char * 16), ("name", ctypes.c_char * 32)]
    _fields_ += [(n, ctypes.c_uint32) for n in ("nfiles", "pgid", "jobc", "tdev", "tpgid")]
    _fields_ += [("nice", ctypes.c_int32), ("sec", ctypes.c_uint64), ("usec", ctypes.c_uint64)]


def identity(pid):
    """Return exact kernel birth identity; None means unknown, never dead."""
    try:
        lib = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
        info = BSDInfo()
        n = lib.proc_pidinfo(int(pid), 3, 0, ctypes.byref(info), ctypes.sizeof(info))
        if n != ctypes.sizeof(info) or info.pid != int(pid):
            return None
        return {"pid": info.pid, "ppid": info.ppid, "uid": info.uid,
                "start": f"{info.sec}:{info.usec:06d}", "status": info.status}
    except (OSError, ValueError, TypeError):
        return None


def owner_state(owner):
    current = identity(owner["pid"])
    if current is not None:
        # Zombie or a different process occupying the original PID proves exit.
        return "dead" if current["start"] != owner["start"] or current["status"] == 5 else "alive"
    try:
        os.kill(owner["pid"], 0)
    except ProcessLookupError:
        return "dead"
    except (PermissionError, OSError):
        pass
    return "unknown"


def command(args, timeout=10):
    r = subprocess.run(args, capture_output=True, text=True, errors="surrogateescape", timeout=timeout)
    if r.returncode:
        raise RuntimeError(f"{Path(args[0]).name}: exit {r.returncode}")
    return r.stdout


def read_json(path, default=None):
    try:
        if path.stat().st_size > MAX_JSON:
            raise ValueError("oversized state")
        with path.open() as f:
            return json.load(f)
    except FileNotFoundError:
        return default


def setup():
    STATE.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(STATE, 0o700)
    (STATE / "resources").mkdir(exist_ok=True, mode=0o700)


def atomic_json(path, value):
    temp = path.with_name(path.name + "." + str(os.getpid()) + ".new")
    with open(temp, "w", encoding="utf-8") as f:
        os.chmod(temp, 0o600)
        json.dump(value, f, ensure_ascii=False, indent=2)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
        if sys.platform == 'darwin' and hasattr(fcntl, 'F_FULLFSYNC'):
            fcntl.fcntl(f.fileno(), fcntl.F_FULLFSYNC)
    os.replace(temp, path)
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


@contextlib.contextmanager
def locked():
    setup()
    with open(STATE / ".lock", "a") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        yield


def log_event(value):
    path = STATE / "events.jsonl"
    service_log = STATE / "launchd.log"
    if service_log.exists() and service_log.stat().st_size > 2 * 1024 * 1024:
        os.replace(service_log, STATE / "launchd.log.1")
    # Called under the global lock; total <= ~21 MiB plus latest.json.
    if path.exists() and path.stat().st_size > 4 * 1024 * 1024:
        for n in range(3, 0, -1):
            src = path.with_name(path.name + f".{n}")
            dst = path.with_name(path.name + f".{n + 1}")
            if src.exists():
                os.replace(src, dst)
        os.replace(path, path.with_name(path.name + ".1"))
    with path.open("a") as f:
        os.chmod(path, 0o600)
        f.write(json.dumps({"time": time.time(), **value}, ensure_ascii=False) + "\n")


def sample_system():
    raw = command(["/usr/sbin/sysctl", "hw.memsize", "vm.swapusage", "kern.memorystatus_vm_pressure_level", "kern.bootsessionuuid"])
    vm = command(["/usr/bin/vm_stat"])
    fields = {k: int(v) for k, v in re.findall(r"^([^:\n]+):\s+(\d+)", vm, re.M)}
    pg = int(re.search(r"page size of (\d+)", vm).group(1))
    # Wall-clock corrections can change kern.boottime without a reboot.
    # A session UUID stays stable across those corrections and sleep/wake.
    boot = re.search(r"kern.bootsessionuuid:\s*([0-9A-Fa-f-]+)", raw)
    if boot is None:
        raise ValueError('boot session identity unavailable')
    boot_id = str(uuid.UUID(boot.group(1)))
    return {"at": time.time(), "pressure": int(re.search(r"vm_pressure_level: (\d+)", raw).group(1)),
            "boot_id": boot_id, "memory_bytes": int(re.search(r"hw.memsize: (\d+)", raw).group(1)),
            "swap_used_bytes": int(float(re.search(r"used = ([\d.]+)M", raw).group(1)) * 1024 ** 2),
            "compressed_bytes": fields["Pages occupied by compressor"] * pg,
            "wired_bytes": fields["Pages wired down"] * pg, "page_size": pg,
            "swapins": fields["Swapins"], "swapouts": fields["Swapouts"]}


def process_table():
    table = {}
    # comm may contain spaces; separate metadata and args with a second ps call.
    metadata = command(["/bin/ps", "-axo", "pid=,ppid=,uid=,rss="])
    for line in metadata.splitlines():
        p = line.split()
        if len(p) == 4:
            pid, ppid, uid, rss = map(int, p)
            table[pid] = Process(pid, ppid, uid, "", "", "", rss)
    for line in command(["/bin/ps", "-axww", "-o", "pid=,command="]).splitlines():
        p = line.strip().split(None, 1)
        if len(p) == 2 and int(p[0]) in table:
            proc = table[int(p[0])]
            proc.command = p[1]
            proc.comm = Path(p[1].split()[0]).name
    return table


def protection_knowledge():
    """Read occupancy, never mutate it or invoke a reap/guard operation."""
    leases = []
    if not LEASES.is_dir():
        return []  # Optional device adapter stays unavailable; never invent leases.
    for path in LEASES.glob("*.json"):
        data = read_json(path)
        if not isinstance(data, dict) or not data.get("session"):
            raise ValueError("invalid lease")
        leases.append({"device": path.stem, "state": data.get("state"), "task": str(data.get("task", ""))[:120]})
    # Old stack state is retention-only. Do not infer ownership/stop protocols.
    for path in STACKS.glob("*.json"):
        if not isinstance(read_json(path), dict):
            raise ValueError("invalid stack retention state")
    return leases


def protected_tree(table, supervisor_ids=()):
    # A registered supervisor is an explicit ownership boundary. Its disposable
    # child is protected by owner identity and its local stop protocol instead.
    boundaries = set(supervisor_ids)
    protected = {p.pid for p in table.values() if p.pid not in boundaries and NON_RECLAIMABLE.search(p.command)}
    changed = True
    while changed:
        changed = False
        for p in table.values():
            if p.pid not in protected and p.pid not in boundaries and p.ppid in protected:
                protected.add(p.pid)
                changed = True
    return protected


def resource_request(record, action):
    rid = record.get("id", "")
    if not re.fullmatch(r"[0-9a-f]{16}", rid):
        raise ValueError("invalid resource id")
    with socket.socket(socket.AF_UNIX) as sock:
        sock.settimeout(3 if action == "probe" else 15)
        sock.connect(str(STATE / "resources" / (rid + ".sock")))
        sock.sendall(json.dumps({"action": action, "id": rid, "start": record.get("start")}).encode() + b"\n")
        data = sock.makefile("rb").readline(16385)
        if len(data) > 16384:
            raise ValueError("oversized supervisor response")
        return json.loads(data)


def inspect_resources(table, state, now, knowledge_ok):
    records = []
    observations = {}
    previous = state.get("observations", {})
    for path in sorted((STATE / "resources").glob("*.json")):
        rec = read_json(path)
        if not isinstance(rec, dict):
            raise ValueError("invalid managed resource")
        records.append(rec)
    boundaries = []
    for rec in records:
        sup = identity(rec.get("supervisor_pid", -1))
        if sup and sup["start"] == rec.get("supervisor_start"):
            boundaries.append(sup["pid"])
    protected = protected_tree(table, boundaries)
    output = []
    for rec in records:
        item = {"id": rec.get("id"), "pid": rec.get("pid"), "kind": rec.get("kind"), "reason": "supervisor_unavailable", "eligible": False}
        if item['id'] in state.get('external_pending', {}):
            item['reason'] = 'pending_readonly_no_retry'
            output.append(item)
            continue
        try:
            probe = resource_request(rec, "probe")
            rec['single_stop_supported'] = probe.get('single_stop_supported') is True
            item['single_stop_supported'] = rec['single_stop_supported']
            proc = table.get(rec.get("pid"))
            birth = identity(rec.get("pid", -1))
            if not proc or not birth:
                item["reason"] = "process_identity_unknown"
            else:
                proc.start = birth["start"]
                rec.update(state="idle" if probe.get("owner_dead") is True else "active", owner_dead=probe.get("owner_dead"))
                reason = classify_process(proc, rec, protected, knowledge_ok)
                if rec.get("kind") != "tail":
                    reason = "cooperative_shutdown_required"
                item["reason"] = reason
                if reason == "managed_idle":
                    old = previous.get(item["id"], {})
                    continuous = old.get("start") == proc.start and 0 <= now - old.get("at", 0) <= 180
                    first = old.get("since", now) if continuous else now
                    observations[item["id"]] = {"since": first, "at": now, "start": proc.start}
                    item["eligible"] = now - first >= 30 and probe.get("safe_to_stop") is True
                    if not item["eligible"]:
                        item["reason"] = "awaiting_second_check"
        except (OSError, ValueError, RuntimeError, TypeError, KeyError):
            pass
        output.append(item)
    state["observations"] = observations
    return output, records


def top_memory():
    raw = command(["/usr/bin/top", "-l", "1", "-n", "12", "-o", "mem", "-stats", "pid,command,mem,cmprs"], timeout=15)
    rows = []
    for line in raw.splitlines():
        p = line.split()
        if len(p) == 4 and p[0].isdigit():
            # Only process display names and aggregate figures; no command args.
            rows.append({"pid": int(p[0]), "name": p[1], "mem": p[2], "compressed": p[3], "policy": "保留：现有任务/未托管资源"})
    return rows


def explain_rows(rows, table, leases):
    """Bounded metadata attribution; no transcript scan or credential output."""
    result = []
    for original in rows:
        row = dict(original)
        proc = table.get(row["pid"])
        if proc:
            cmd = proc.command
            avd = re.search(r"(?:-avd |@)([A-Za-z0-9_.-]+)", cmd) if "qemu-system" in cmd else None
            device = avd.group(1) if avd else next((l["device"] for l in leases if l["device"] in cmd), None)
            lease = next((l for l in leases if l["device"] == device), None)
            if lease:
                row["task"] = re.sub(r"(?<!\d)(?:\+?86)?1[3-9]\d{9}(?!\d)", "[手机号隐藏]", lease["task"])
            for needle, label in (("GradleDaemon", "Gradle编译后台"), ("KotlinCompileDaemon", "Kotlin编译后台"),
                                  ("qemu-system", "Android模拟器"), ("lldb-rpc-server", "Xcode调试后台")):
                if needle in cmd:
                    row["role"] = label
                    break
            if "GradleDaemon" in cmd and "task" not in row:
                try:
                    cwd = command(["/usr/sbin/lsof", "-a", "-p", str(proc.pid), "-d", "cwd", "-Fn"], timeout=2)
                    row["cwd"] = next((l[1:] for l in cwd.splitlines() if l.startswith("n")), "未知")
                except (OSError, RuntimeError, subprocess.TimeoutExpired):
                    row["cwd"] = "未知，保留"
        result.append(row)
    return result


def notify(message, title="内存紧张", wait=3.0):
    """Local desktop notification, not an external message. The menu-bar app posts it (as 资源看板,
    with the user's notification permission) and acknowledges; AppleScript is the fallback.
    True only when one of them confirmed delivery."""
    alert = {"id": "{:.6f}".format(time.time()), "title": title, "body": message, "at": time.time()}
    try:
        atomic_json(STATE / "alert.json", alert)
        deadline = time.time() + wait
        while time.time() < deadline:
            if read_json(STATE / "alert-ack.json", {}).get("id") == alert["id"]:
                return True
            time.sleep(0.2)
    except (OSError, ValueError):
        pass
    source = 'on run argv\ndisplay notification (item 2 of argv) with title (item 1 of argv)\nend run'
    try:
        r = subprocess.run(["/usr/bin/osascript", "-e", source, title, message], capture_output=True, timeout=8)
        return r.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def notification_message(rows, actions, pending, resources=(), cooldown=False):
    """Report only confirmed outcomes; explain why pressure can remain high."""
    stopped = sum(a['result'].get('stopped') is True for a in actions)
    if stopped:
        outcome = f'本轮已正常退出 {stopped} 项。'
    elif pending:
        outcome = f'{len(pending)} 项退出结果待核实，正在只读检查，不重复请求。'
    elif any(r.get('eligible') is True for r in resources):
        outcome = '已确认闲置资源，等待动作冷却。' if cooldown else '闲置资源等待执行前复核。'
    elif any(r.get('reason') in ('awaiting_second_idle_check', 'awaiting_second_check') for r in resources):
        outcome = '闲置资源正在等待第二次连续复核。'
    else:
        outcome = '当前没有可安全回收的闲置资源。'
    names = '、'.join(r['name'] for r in rows[:3]) or '开发进程'
    sims = [r for r in resources if r.get('kind') == 'simulator' and r.get('rss_kib') is not None]
    if sims:
        idle = sum(1 for r in sims if r.get('reason') == 'simulator_not_attached')
        size = sum(r['rss_kib'] for r in sims) / 1024 ** 2
        names = '{} 台模拟器占约 {:.0f} GB（{} 台没被开发工具连着）、{}'.format(len(sims), size, idle, names)
    return f'主要占用：{names}。{outcome}点开资源看板看哪些能关。'


def external_module():
    return importlib.import_module((__package__ + "." if __package__ else "") + 'memory_external')


def inspect_external_resources(state, now):
    return external_module().collect(state, now)


def reconcile_external(state):
    """Read-only reconciliation of durable intents; never resend a request."""
    pending = state.setdefault('external_pending', {})
    for key, intent in list(pending.items()):
        raw = intent['resource']
        if intent['kind'] == 'simulator':
            continue  # A missing lease alone does not prove a shutdown succeeded.
        try:
            if intent['kind'] in ('mcp', 'gradle'):
                module = 'mcp_reclaim' if intent['kind'] == 'mcp' else 'gradle_reclaim'
                result = external_module().adapter(module).reconcile(raw)
                exited = result.get('stopped') is True
                if not exited and intent['kind'] == 'gradle' and result.get('safe_to_retry') is True:
                    # Only the request journal can prove the old worker exited
                    # before dispatch. Legacy/unknown attempts never qualify.
                    log_event({'type': 'cleanup_not_dispatched', 'id': key, 'kind': 'gradle',
                               'reason': result.get('reason', 'dispatch_absent_confirmed')})
                    del pending[key]
                    state.setdefault('external_observations', {}).pop(key, None)
                    continue
            else:
                exited = owner_state({'pid': raw['pid'], 'start': raw['start']}) == 'dead'
            if exited:
                log_event({'type': 'cleanup_reconciled', 'id': key, 'kind': intent['kind'],
                           'reason': 'original_resource_exit_confirmed_readonly', 'stopped': True})
                del pending[key]
        except (OSError, ValueError, RuntimeError, KeyError, TypeError, ImportError):
            pass


def run_cycle(mode, selected_ids=None, owned_only=False):
    manual = mode == 'clean-once'
    if manual and not selected_ids:
        raise ValueError('单次回收必须指定预览中的资源 ID')
    with locked():
        now = time.time()
        state_path = STATE / "state.json"
        state = read_json(state_path, {})
        if not isinstance(state, dict):
            raise ValueError("invalid state; no actions allowed")
        paused = (STATE / "paused").exists()
        sample = sample_system()
        # A CRITICAL state is rechecked after 15s; no forced memory allocation.
        if mode == "tick" and sample["pressure"] == 4 and state.get("critical_since") is None:
            state, _ = pressure_transition(state, sample, now)
            time.sleep(15)
            sample, now = sample_system(), time.time()
        old_sample = state.get("sample", {})
        old_ts = old_sample.get("at")
        if mode == "tick":
            state, event = pressure_transition(state, sample, now)
        else:
            event = None
        if old_ts and 0 < now - old_ts <= 180 and old_sample.get("boot_id") == sample["boot_id"]:
            elapsed = now - old_ts
            for field in ("swapins", "swapouts"):
                sample[field + "_mib_s"] = round(max(0, sample[field] - old_sample.get(field, sample[field])) * sample["page_size"] / 1024 ** 2 / elapsed, 2)
        leases = protection_knowledge()
        needs_top = mode in ("status", "inspect", "clean", "clean-once") or now - state.get("last_top", 0) >= 300
        needs_processes = needs_top or any((STATE / "resources").glob("*.json"))
        table = process_table() if needs_processes else {}
        # Device occupancy is optional. Owned tail supervisors establish their
        # own identity boundary; malformed existing protection still raises above.
        resources, records = inspect_resources(table, state, now, True)
        if paused:
            for resource in resources:
                if resource.get('eligible') and not resource.get('single_stop_supported'):
                    resource.update(eligible=False, reason='legacy_supervisor_paused_preserved')
        if owned_only:
            external, external_records = [], {}
        else:
            reconcile_external(state)
            external, external_records = inspect_external_resources(state, now)
        actions = []
        effective_pressure = sample['pressure']
        last_normal_sweep = state.get('external_last_sweep')
        normal_sweep = sample["pressure"] == 1 and (last_normal_sweep is None or now - last_normal_sweep >= 86400)
        pressure_driven = sample['pressure'] in (2, 4)
        cooldown = state.get("last_cleanup") is None or now - state["last_cleanup"] >= 600
        apply = (manual or not paused) and cooldown and sample["pressure"] in (1, 2, 4) and (manual or mode == "clean" or mode == "tick" and (event == "candidate" or normal_sweep))
        if apply:
            candidates = [r for r in external + resources if r.get('eligible') is True
                          and r.get('id') not in state.get('external_pending', {})
                          and (not manual or r.get('id') in selected_ids)]
            if candidates and not table:
                table = process_table()
            for item in candidates[:3]:
                before_metrics = sample_system()
                effective_pressure = before_metrics['pressure']
                if effective_pressure not in (1, 2, 4) or pressure_driven and effective_pressure == 1:
                    break
                # Persist the cooldown before any potentially successful write,
                # including when a worker exits before receiving its result.
                state['last_cleanup'] = now
                is_external = item in external
                rec = external_records[item['id']] if is_external else next(r for r in records if r['id'] == item['id'])
                intent = {'kind': item['kind'] if is_external else 'tail',
                          'resource': external_module().private_metadata(rec),
                          'requested_at': time.time(), 'status': 'intent_persisted_before_request'}
                state.setdefault('external_pending', {})[item['id']] = intent
                # All resource classes use durable intents: a lost response or
                # a crash after any request must never cause a replay.
                atomic_json(state_path, state)
                try:
                    if is_external:
                        result = external_module().reclaim(item['kind'], rec, STATE)
                    else:
                        result = resource_request(rec, 'stop_if_orphan_once' if manual and rec.get('single_stop_supported') else 'stop_if_orphan')
                    if not isinstance(result, dict):
                        raise ValueError('invalid shutdown response')
                    if type(result.get('stopped')) is not bool:
                        raise ValueError('missing shutdown outcome')
                    if not result['stopped'] and not (
                            any(result.get(k) is True for k in ('accepted', 'unknown', 'pending'))
                            or result.get('accepted') is False or result.get('pending') is False):
                        # Valid JSON alone is not evidence that no request was
                        # accepted (including replies from older supervisors).
                        result = {**result, 'unknown': True, 'reason': 'shutdown_outcome_incomplete_no_retry'}
                except (OSError, ValueError, RuntimeError, KeyError, TypeError, ImportError, subprocess.TimeoutExpired):
                    result = {'stopped': False, 'unknown': True, 'reason': 'outcome_unknown_no_retry'}
                if result.get('stopped') is True or not any(result.get(k) is True for k in ('accepted', 'unknown', 'pending')):
                    state['external_pending'].pop(item['id'], None)
                    if result.get('safe_to_retry') is True:
                        state.setdefault('external_observations', {}).pop(item['id'], None)
                else:
                    intent['status'] = 'pending_readonly_no_retry'
                # Successful requests and unknown outcomes are both persisted
                # before any following resource can be processed.
                atomic_json(state_path, state)
                try:
                    after = sample_system()
                except (OSError, ValueError, RuntimeError, KeyError, AttributeError, subprocess.TimeoutExpired):
                    after = {'pressure': None}
                effective_pressure = after['pressure']
                action = {"id": item["id"], "pid": item.get("pid"), 'kind': item.get('kind', 'tail'),
                          'reason': item.get('reason'), "result": result,
                          'rss_kib_estimate': table[item['pid']].rss_kib if item.get('pid') in table else None,
                          'before': before_metrics, 'after': after}
                actions.append(action)
                log_event({"type": "cleanup", **action})
                state["last_cleanup"] = now
                if after["pressure"] not in (1, 2, 4) or pressure_driven and after['pressure'] == 1:
                    break
            state["last_sweep"] = now
            if normal_sweep and actions:
                state['external_last_sweep'] = now
        rows = state.get("top_memory", [])
        if needs_top:
            rows = explain_rows(top_memory(), table, leases)
            state.update(top_memory=rows, last_top=now)
        for row in rows:
            matched = next((r for r in external if r.get('pid') == row.get('pid')), None)
            if matched:
                row['policy'] = '可安全回收' if matched['eligible'] else '保留：' + reason_label(matched.get('reason'))
        if mode == "tick" and not paused and alert_due(state, effective_pressure, now):
            minutes = max(1, round((now - state["incident_started"]) / 60))
            title = ('内存很紧' if effective_pressure == 4 else '内存紧张') + '，已持续 {} 分钟'.format(minutes)
            ok = notify(notification_message(rows, actions, state.get('external_pending', {}),
                                             external + resources, cooldown=not cooldown), title)
            if ok:   # a failed delivery is tried again next round, never recorded as sent
                state.update(notification_sent=True, notification_level=effective_pressure, notification_at=now)
            log_event({"type": "notification", "delivered": ok, "pressure": effective_pressure})
        if mode == "tick":
            state["sample"] = sample
        if not cooldown:
            for resource in resources + external:
                if resource.get('eligible'):
                    resource.update(eligible=False, reason='cleanup_cooldown')
        result = {"version": VERSION, "at": now, "paused": paused, "sample": sample,
                  "manual_clean_supported": True, "manual": manual,
                  "managed_resources": resources, 'external_resources': external,
                  'pending_requests': list(state.get('external_pending', {})),
                  "actions": actions, "protected_devices": len(leases),
                  "top_memory": rows, "policy": "模拟器按任务退出证据与占用CAS收尾；Gradle仅闲置无消费者时正常退出；MCP需原子排空协议；在用、旧协议和未知资源保留"}
        atomic_json(state_path, state)
        atomic_json(STATE / "latest.json", result)
        if actions:
            atomic_json(STATE / 'last-cleanup.json', result)
        if mode == "tick":
            log_event({"type": "sample", "pressure": sample["pressure"], "swap_used_bytes": sample["swap_used_bytes"], "compressed_bytes": sample["compressed_bytes"], "actions": len(actions), "paused": paused})
        return result


def supervise(args):
    """Own a new leaf tail; NEVER adopt any existing PID or arbitrary task."""
    setup()
    argv = args.command
    if argv and argv[0] == "--":
        argv = argv[1:]
    executable = shutil.which(argv[0]) if argv else None
    if not argv or args.kind != "tail" or not executable or Path(executable).resolve() != Path("/usr/bin/tail"):
        raise ValueError("run仅支持托管新日志tail；其他资源通过各自安全回收协议处理")
    argv[0] = "/usr/bin/tail"
    owner = identity(args.owner_pid)
    if not owner or owner["uid"] != os.getuid() or args.owner_pid <= 1:
        raise ValueError("无法核实所属任务进程，未启动资源")
    rid = uuid.uuid4().hex[:16]
    path = STATE / "resources" / (rid + ".json")
    sock_path = STATE / "resources" / (rid + ".sock")
    # A bounded registry also bounds the number of resident Python supervisors.
    # Lost controllers are retained, not adopted; only records whose controller
    # and child have both definitely exited can be discarded.
    with locked():
        for old_path in (STATE / "resources").glob("*.json"):
            old = read_json(old_path)
            if isinstance(old, dict) and owner_state({"pid": old["supervisor_pid"], "start": old["supervisor_start"]}) == "dead" and owner_state({"pid": old["pid"], "start": old["start"]}) == "dead":
                old_path.unlink()
                old_path.with_suffix(".sock").unlink(missing_ok=True)
        if len(list((STATE / "resources").glob("*.json"))) >= 4:
            raise ValueError("最多同时托管4个日志tail；未启动新资源")
        child = subprocess.Popen(argv)  # no process groups or tree-wide signals
        born = identity(child.pid)
        if born:
            sup = identity(os.getpid())
            rec = {"id": rid, "pid": child.pid, "start": born["start"], "uid": os.getuid(),
                   "supervisor_pid": os.getpid(), "supervisor_start": sup["start"],
                   "kind": "tail", "state": "active", "owner": owner,
                   "disposable": True, "supervised": True, "created": time.time()}
            atomic_json(path, rec)
    if not born:
        # Do not guess that a process is dead or signal an unknown PID.
        return child.wait()
    def request_exit(signum, frame):
        pass  # Never propagate a service signal to a still-owned child.

    signal.signal(signal.SIGTERM, request_exit)
    with socket.socket(socket.AF_UNIX) as server:
        server.bind(str(sock_path))
        os.chmod(sock_path, 0o600)
        server.listen(1)
        server.settimeout(1)
        while child.poll() is None:
            # Do not detach the sole ownership handle while the child is alive.
            # An ownerless tail is terminated only after a cleaner handshake.
            try:
                conn, _ = server.accept()
            except socket.timeout:
                continue
            with conn:
                conn.settimeout(2)
                try:
                    payload = conn.makefile("rb").readline(16385)
                    req = json.loads(payload)
                    if len(payload) > 16384 or req.get("id") != rid or req.get("start") != born["start"]:
                        raise ValueError("identity mismatch")
                    owner_dead = owner_state(owner) == "dead"
                    current = identity(child.pid)
                    table = process_table()
                    leaf = not any(p.ppid == child.pid for p in table.values())
                    p = table.get(child.pid)
                    protected = not p or bool(NON_RECLAIMABLE.search(p.command))
                    safe = owner_dead and leaf and not protected and current is not None and current["start"] == born["start"]
                    result = {"owner_dead": owner_dead, "safe_to_stop": safe, "stopped": False, "accepted": False, "single_stop_supported": True}
                    if req.get("action") in ("stop_if_orphan", "stop_if_orphan_once"):
                        # Automatic requests honor pause at the endpoint. An
                        # explicit one-shot request still requires the same safety proof.
                        if safe and (req['action'] == 'stop_if_orphan_once' or not (STATE / "paused").exists()):
                            # Our unreaped Popen child cannot have its PID reused.
                            result['accepted'] = True
                            child.terminate()
                            try:
                                child.wait(timeout=8)
                                result.update(stopped=True, reason="owner_exited_normal_term")
                            except subprocess.TimeoutExpired:
                                result["reason"] = "term_timeout_preserved_no_sigkill"
                        else:
                            result["reason"] = "active_unknown_or_protected"
                    elif req.get("action") != "probe":
                        raise ValueError("unsupported operation")
                    conn.sendall(json.dumps(result).encode() + b"\n")
                except (ValueError, OSError, RuntimeError, KeyError, subprocess.TimeoutExpired):
                    with contextlib.suppress(OSError):
                        conn.sendall(b'{"stopped":false,"unknown":true,"reason":"unknown_preserved"}\n')
        status = child.wait()
    with locked():
        path.unlink(missing_ok=True)
        sock_path.unlink(missing_ok=True)
        log_event({"type": "resource_exit", "id": rid, "exit_code": status})
    return status if status >= 0 else 128 - status


def reason_label(value):
    return {
        'owner_active_or_unknown': '任务仍在运行或结束证据不足',
        'run_active_or_unknown': '测试脚本仍在运行或状态不明',
        'test_process_active': '仍有测试进程',
        'protected_lease': '正在使用或关闭状态待核实',
        'awaiting_second_idle_check': '等待连续闲置复核',
        'daemon_not_idle': '编译后台仍忙碌',
        'cooperative_protocol_unavailable': '旧实例缺少安全排空协议',
        'active_consumer_or_task': '仍有会话、任务或工具调用',
        'pending_readonly_no_retry': '退出结果正在只读核实，不重复请求',
        'inspection_unavailable_preserved': '本类资源信息不完整，保留',
        'simulator_in_use': '有开发工具正连着',
        'simulator_not_attached': '没被开发工具连着，可以关掉',
    }.get(value, value or '状态不明，保留')


def main():
    ap = argparse.ArgumentParser(description="本机内存守护；保留工作任务，只回收明确托管的闲置资源")
    sub = ap.add_subparsers(dest="mode", required=True)
    for name in ("status", "inspect", "clean", "clean-once", "tick", "history", "pause", "resume"):
        p = sub.add_parser(name)
        p.add_argument("--json", action="store_true")
        p.add_argument('--owned-only', action='store_true', help='Only inspect/reclaim tails owned by this state directory; no external adapters')
        if name == 'clean-once':
            p.add_argument('--id', action='append', required=True, dest='selected_ids')
    p = sub.add_parser("run", help="托管一个新日志tail，所属任务退出后允许自动收尾")
    p.add_argument("--owner-pid", required=True, type=int)
    p.add_argument("--kind", choices=["tail"], default="tail")
    p.add_argument("command", nargs=argparse.REMAINDER)
    args = ap.parse_args()
    if args.mode == "run":
        return supervise(args)
    if args.mode in ("pause", "resume"):
        with locked():
            if args.mode == "pause":
                (STATE / "paused").touch(mode=0o600)
            else:
                (STATE / "paused").unlink(missing_ok=True)
            log_event({"type": args.mode})
        print("自动清理已暂停，监测继续" if args.mode == "pause" else "自动清理已恢复")
        return 0
    if args.mode == "history":
        setup()
        path = STATE / "events.jsonl"
        if path.exists():
            with path.open("rb") as f:
                size = path.stat().st_size
                f.seek(max(0, size - 65536))
                if size > 65536:
                    f.readline()
                print(f.read().decode("utf-8", "replace"), end="")
        return 0
    result = run_cycle(args.mode, getattr(args, 'selected_ids', None), args.owned_only)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    elif args.mode != "tick":
        s = result["sample"]
        label = {1: "正常", 2: "警告", 4: "严重"}.get(s["pressure"], "未知")
        print(f"内存压力：{label}；压缩 {s['compressed_bytes']/1024**3:.1f} GiB；交换空间 {s['swap_used_bytes']/1024**3:.1f} GiB")
        print(f"自动清理：{'暂停' if result['paused'] else '启用'}；本次正常退出 {sum(a['result'].get('stopped') is True for a in result['actions'])} 项；设备占用 {result['protected_devices']} 项保留")
        for r in result["top_memory"][:6]:
            attribution = r.get("task") or r.get("cwd") or "现有应用/归属待核实"
            print(f"  {r.get('role', r['name'])}（PID {r['pid']}）：MEM {r['mem']}；{attribution}；{r['policy']}")
        for r in result['external_resources']:
            print(f"  [{r['kind']}] {r['id']}：{'可安全回收' if r['eligible'] else '保留'}；{reason_label(r.get('reason'))}")
        print("MEM是进程记账，不能累加当作可释放RAM。" + result["policy"])
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, ValueError, RuntimeError, KeyError, AttributeError, subprocess.TimeoutExpired) as exc:
        # No fallback to the legacy killer. Error text excludes command output.
        with contextlib.suppress(OSError, ValueError, TypeError):
            with locked():
                state = read_json(STATE / "state.json", {})
                if isinstance(state, dict):
                    state.update(warn_count=0, normal_count=0, critical_since=None, observations={}, external_observations={})
                    atomic_json(STATE / "state.json", state)
                log_event({"type": "error_preserved", "error": type(exc).__name__})
        print("dev-memory：信息不完整，未执行进一步清理（" + type(exc).__name__ + "）", file=sys.stderr)
        sys.exit(2)
