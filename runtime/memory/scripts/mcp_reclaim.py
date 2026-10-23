"""Cooperative Codex companion MCP reclamation. Never sends process signals.

Old brokers are report-only. A v1-capable broker owns the admission barrier,
authoritative activity counters and EOF-only close of its own app-server/MCPs.
Unknown transaction outcomes are reconciled read-only, never retried here.
"""
from __future__ import annotations

import ctypes
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import socket
import stat
import subprocess

PROTOCOL = "codex-memory-drain-v1"
BROKER_SOURCE = Path(os.environ.get("DEV_MEMORY_BROKER_SOURCE", "/nonexistent/unconfigured/app-server-broker.mjs"))
MAX_RESPONSE = 65536


class BSDInfo(ctypes.Structure):
    _fields_ = [(n, ctypes.c_uint32) for n in (
        "flags", "status", "xstatus", "pid", "ppid", "uid", "gid", "ruid", "rgid", "svuid", "svgid", "reserved")]
    _fields_ += [("comm", ctypes.c_char * 16), ("name", ctypes.c_char * 32)]
    _fields_ += [(n, ctypes.c_uint32) for n in ("nfiles", "pgid", "jobc", "tdev", "tpgid")]
    _fields_ += [("nice", ctypes.c_int32), ("sec", ctypes.c_uint64), ("usec", ctypes.c_uint64)]


def _identity(pid):
    try:
        lib = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
        value = BSDInfo()
        count = lib.proc_pidinfo(int(pid), 3, 0, ctypes.byref(value), ctypes.sizeof(value))
        if count == ctypes.sizeof(value) and value.pid == int(pid):
            return {"pid": value.pid, "start": f"{value.sec}:{value.usec:06d}", "uid": value.uid, "status": value.status}
    except (OSError, ValueError, TypeError):
        pass
    return None


def _processes():
    result = subprocess.run(["/bin/ps", "-axww", "-o", "pid=,ppid=,uid=,command="],
                            capture_output=True, text=True, timeout=8, check=True)
    rows = []
    for line in result.stdout.splitlines():
        fields = line.strip().split(None, 3)
        if len(fields) == 4:
            rows.append({"pid": int(fields[0]), "ppid": int(fields[1]), "uid": int(fields[2]), "command": fields[3]})
    return rows


def _endpoint_for(row):
    if row["uid"] != os.getuid() or "app-server-broker.mjs" not in row["command"]:
        return None
    try:
        argv = shlex.split(row["command"])
        script_index = next(i for i, value in enumerate(argv) if value.endswith("/app-server-broker.mjs"))
        if Path(argv[script_index]).resolve() != BROKER_SOURCE.resolve() or argv[script_index + 1] != "serve":
            return None
        endpoint = argv[argv.index("--endpoint", script_index + 2) + 1]
        return endpoint if endpoint.startswith("unix:") else None
    except (ValueError, IndexError, StopIteration, OSError):
        return None


def _rpc(endpoint, method, params=None, timeout=3):
    if not endpoint.startswith("unix:"):
        raise ValueError("unsupported endpoint")
    path = Path(endpoint[5:])
    mode = path.lstat()
    if not stat.S_ISSOCK(mode.st_mode) or mode.st_uid != os.getuid():
        raise ValueError("unowned or non-socket endpoint")
    with socket.socket(socket.AF_UNIX) as channel:
        channel.settimeout(timeout)
        channel.connect(str(path))
        channel.sendall(json.dumps({"id": 1, "method": method, "params": params or {}}).encode() + b"\n")
        with channel.makefile("rb") as stream:
            line = stream.readline(MAX_RESPONSE + 1)
        if len(line) > MAX_RESPONSE:
            raise ValueError("oversized broker response")
        response = json.loads(line)
        if response.get("id") != 1:
            raise ValueError("unexpected response identity")
        if "error" in response:
            return None
        result = response.get("result")
        if not isinstance(result, dict):
            raise ValueError("invalid broker response")
        return result


def _capability(endpoint, pid):
    """Old processes have no advertisement: do not send them an unknown RPC."""
    path = Path(endpoint[5:] + ".memory.json")
    try:
        metadata = path.lstat()
        if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid()
                or metadata.st_mode & 0o022 or metadata.st_size > 4096):
            return None
        with path.open(encoding="utf-8") as stream:
            value = json.load(stream)
        if (value.get("protocol") == PROTOCOL and value.get("pid") == pid
                and value.get("uid") == os.getuid()
                and isinstance(value.get("instance_id"), str)
                and re.fullmatch(r"[0-9a-f]{32}", value["instance_id"])):
            return value
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    return None


def _valid_status(status, pid):
    return (isinstance(status, dict) and status.get("protocol") == PROTOCOL
            and status.get("pid") == pid and status.get("uid") == os.getuid()
            and isinstance(status.get("instance_id"), str)
            and re.fullmatch(r"[0-9a-f]{32}", status["instance_id"]) is not None
            and status.get("knowledge_ok") is True and status.get("natural_close_supported") is True
            and type(status.get("draining")) is bool
            and all(type(status.get(key)) is int and status[key] >= 0 for key in
                    ("consumer_count", "pending_requests", "task_count", "active_tool_calls")))


def _idle(status):
    return not status["draining"] and all(status[key] == 0 for key in
        ("consumer_count", "pending_requests", "task_count", "active_tool_calls"))


def _tracked_processes(pid, rows):
    pids = {pid}
    while True:
        expanded = pids | {row["pid"] for row in rows if row["ppid"] in pids}
        if expanded == pids:
            break
        pids = expanded
    tracked = []
    for child_pid in sorted(pids):
        current = _identity(child_pid)
        if not current or current["uid"] != os.getuid():
            return None
        tracked.append({"pid": child_pid, "start": current["start"]})
    return tracked


def inspect(now=None):
    """Read-only scan. Counts come from the broker, never guessed from ps/CPU."""
    del now
    rows = _processes()
    candidates = []
    for row in rows:
        endpoint = _endpoint_for(row)
        if endpoint is None:
            continue
        birth = _identity(row["pid"])
        if not birth or birth["uid"] != os.getuid():
            continue
        key = f"{row['pid']}:{birth['start']}:{endpoint}"
        candidate = {"id": "mcp-" + hashlib.sha256(key.encode()).hexdigest()[:16],
                     "kind": "mcp", "pid": row["pid"], "start": birth["start"],
                     "endpoint": endpoint, "eligible": False, "reason": "cooperative_protocol_unavailable", "evidence": {}}
        try:
            capability = _capability(endpoint, row["pid"])
            if capability is None:
                candidates.append(candidate)
                continue
            status = _rpc(endpoint, "broker/memoryStatus")
            if _valid_status(status, row["pid"]) and status["instance_id"] == capability["instance_id"]:
                candidate["evidence"] = {key: status[key] for key in (
                    "protocol", "instance_id", "consumer_count", "pending_requests", "task_count", "active_tool_calls", "draining")}
                candidate["reason"] = "active_consumer_or_task" if not _idle(status) else "cooperative_idle"
                if _idle(status):
                    tracked = _tracked_processes(row["pid"], rows)
                    if tracked is None:
                        candidate["reason"] = "child_identity_unknown"
                    else:
                        candidate.update(eligible=True, tracked_processes=tracked)
                material = {"pid": candidate["pid"], "start": candidate["start"], "endpoint": endpoint,
                            "evidence": candidate["evidence"], "tracked_processes": candidate.get("tracked_processes", [])}
                candidate["fingerprint"] = hashlib.sha256(json.dumps(material, sort_keys=True).encode()).hexdigest()
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
            candidate["reason"] = "cooperative_status_unknown"
        candidates.append(candidate)
    return candidates


def _exited(record):
    current = _identity(record["pid"])
    if current is not None:
        return current["start"] != record["start"] or current["status"] == 5
    result = subprocess.run(["/bin/ps", "-p", str(record["pid"]), "-o", "pid="],
                            capture_output=True, text=True, timeout=3)
    return result.returncode == 1 and not result.stdout.strip() and not result.stderr.strip()


def reconcile(candidate):
    """Resolve a previously sent drain only by reading original identities."""
    tracked = candidate.get("tracked_processes")
    if not isinstance(tracked, list) or not tracked:
        return {"stopped": False, "pending": True, "reason": "exit_evidence_unknown"}
    try:
        exited = all(_exited(record) for record in tracked)
    except (OSError, ValueError, TypeError, KeyError, subprocess.SubprocessError):
        exited = False
    return {"stopped": exited, "pending": not exited,
            "reason": "broker_and_observed_children_exited" if exited else "original_processes_preserved_or_exit_unknown"}


def reclaim(candidate):
    """One cooperative write; a timeout is pending, never a reason to resend."""
    birth = _identity(candidate.get("pid"))
    if not birth or birth["start"] != candidate.get("start") or birth["uid"] != os.getuid():
        return {"stopped": False, "pending": False, "reason": "identity_changed_or_unknown"}
    expected = candidate.get("evidence", {}).get("instance_id")
    if candidate.get("eligible") is not True or not candidate.get("tracked_processes") or not expected:
        return {"stopped": False, "pending": False, "reason": "missing_authoritative_idle_evidence"}
    try:
        status = _rpc(candidate["endpoint"], "broker/memoryStatus")
        if not _valid_status(status, candidate["pid"]) or status["instance_id"] != expected or not _idle(status):
            return {"stopped": False, "pending": False, "reason": "activity_changed_or_unknown"}
    except (OSError, ValueError, TypeError, KeyError):
        return {"stopped": False, "pending": False, "reason": "status_recheck_unknown"}
    try:
        result = _rpc(candidate["endpoint"], "broker/drainIfIdle", {"expected_instance_id": expected}, timeout=11)
    except (OSError, ValueError, TypeError, KeyError):
        return {"stopped": False, "pending": True, "requested": True, "reason": "drain_outcome_unknown_reconcile_only"}
    if not _valid_status(result, candidate["pid"]):
        return {"stopped": False, "pending": True, "requested": True, "reason": "drain_response_unknown_reconcile_only"}
    if result.get("reason") in ("busy_or_unknown_preserved", "instance_changed") and result.get("pending") is not True:
        return {"stopped": False, "pending": False, "requested": True, "reason": result.get("reason", "broker_declined_preserved")}
    if result.get("instance_id") != expected or (result.get("pending") is not True and result.get("reason") != "already_draining"):
        return {"stopped": False, "pending": True, "requested": True, "reason": "drain_response_unknown_reconcile_only"}
    # The IPC response deliberately never claims its own broker has exited.
    # The worker persists this candidate and subsequently calls reconcile only.
    outcome = reconcile(candidate)
    outcome.update(requested=True, broker_reason=result.get("reason", "normal_exit_requested"))
    return outcome
