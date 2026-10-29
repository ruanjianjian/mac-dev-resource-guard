#!/usr/bin/python3
"""Single Gradle daemon reclaim using its authenticated graceful-stop protocol.

Never signals a discovered PID, runs gradle --stop, starts a daemon, or changes
Gradle configuration. StopWhenIdle is the daemon's own atomic drain operation:
a build that wins the final race finishes normally and subsequent builds are
rejected so the Gradle client can select/start another daemon.
"""
from __future__ import annotations

import argparse
import ctypes
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import time
import uuid

SUPPORTED = {"8.14", "8.11.1"}
BUILD_ROOT = Path(os.environ.get("DEV_MEMORY_STATE_DIR", str(Path.home() / ".local/state/dev-memory"))) / "gradle-helper"
MAX_OUTPUT = 2 * 1024 * 1024
STARTUP_TIMEOUT = 45
RESPONSE_TIMEOUT = 15
_OWN_HELPERS = {}


JAVA_SOURCE = r'''
import java.io.*;
import java.nio.channels.*;
import java.nio.file.*;
import java.nio.charset.StandardCharsets;
import java.util.*;
import java.net.*;
import com.google.gson.Gson;
import org.gradle.cache.internal.filelock.DefaultLockStateSerializer;
import org.gradle.cache.internal.filelock.LockStateAccess;
import org.gradle.internal.invocation.BuildAction;
import org.gradle.internal.remote.internal.RemoteConnection;
import org.gradle.internal.remote.internal.inet.InetEndpoint;
import org.gradle.internal.remote.internal.inet.TcpOutgoingConnector;
import org.gradle.internal.serialize.*;
import org.gradle.launcher.daemon.protocol.*;
import org.gradle.launcher.daemon.protocol.Message;
import org.gradle.launcher.daemon.registry.*;
import org.gradle.launcher.daemon.server.api.DaemonState;
import org.gradle.util.GradleVersion;

public final class DevMemoryGradle {
  static String version;
  static String user = System.getProperty("user.name");
  static final Gson JSON = new Gson();
  static Path journal;
  static final Set<String> SAFE_REASONS = Set.of("process_missing", "start_unknown", "process_identity_changed",
      "not_expected_gradle_version", "registry_missing_or_oversized", "registry_lock_missing", "registry_locked",
      "registry_dirty", "registry_empty", "duplicate_pid", "daemon_missing", "registry_context_mismatch",
      "address_not_inet", "address_empty", "address_not_loopback", "connections_timeout", "connections_unavailable",
      "connection_state_unknown", "listener_unproven", "jar_version_mismatch", "not_idle_or_identity_changed",
      "final_state_changed", "final_consumers_changed", "commit_not_received", "stop_response_unknown", "invalid_mode");

  static void writeJournal(String name, Map<String,Object> value) throws Exception {
    if (journal == null) return;
    byte[] bytes = JSON.toJson(value).getBytes(StandardCharsets.UTF_8);
    Path temporary = journal.resolve(name+".new");
    try (FileChannel f = FileChannel.open(temporary, StandardOpenOption.CREATE_NEW, StandardOpenOption.WRITE)) {
      java.nio.ByteBuffer buffer = java.nio.ByteBuffer.wrap(bytes);
      while (buffer.hasRemaining()) f.write(buffer);
      f.force(true);
    }
    Files.move(temporary, journal.resolve(name), StandardCopyOption.ATOMIC_MOVE);
    try (FileChannel directory = FileChannel.open(journal, StandardOpenOption.READ)) { directory.force(true); }
  }

  static void checkProcess(long pid, long start) throws Exception {
    ProcessHandle ph = ProcessHandle.of(pid).orElseThrow(() -> new IOException("process_missing"));
    ProcessHandle.Info info = ph.info();
    if (!ph.isAlive() || !user.equals(info.user().orElse("")) ||
        info.startInstant().orElseThrow(() -> new IOException("start_unknown")).toEpochMilli() != start) {
      throw new IOException("process_identity_changed");
    }
    List<String> args = Arrays.asList(info.arguments().orElse(new String[0]));
    int index = args.indexOf("org.gradle.launcher.daemon.bootstrap.GradleDaemon");
    if (index < 0 || index + 1 >= args.size() || !version.equals(args.get(index+1))) {
      throw new IOException("not_expected_gradle_version");
    }
  }

  static DaemonInfo read(Path registry, long pid) throws Exception {
    if (!Files.isRegularFile(registry, LinkOption.NOFOLLOW_LINKS) || Files.size(registry) > 1048576)
      throw new IOException("registry_missing_or_oversized");
    Path lock = registry.resolveSibling("registry.bin.lock");
    if (!Files.isRegularFile(lock, LinkOption.NOFOLLOW_LINKS) || Files.size(lock) < 17)
      throw new IOException("registry_lock_missing");
    // Gradle locks [0,17). A short overlapping shared lock prevents a writer
    // during this read. Never create/change the lock file or registry contents.
    try (RandomAccessFile rf = new RandomAccessFile(lock.toFile(), "r");
         FileLock held = rf.getChannel().tryLock(0, 1, true)) {
      if (held == null) throw new IOException("registry_locked");
      if (new LockStateAccess(new DefaultLockStateSerializer()).readState(rf).isDirty())
        throw new IOException("registry_dirty");
      try (InputStreamBackedDecoder decoder = new InputStreamBackedDecoder(
          new BufferedInputStream(Files.newInputStream(registry)))) {
        DaemonRegistryContent content = DaemonRegistryContent.SERIALIZER.read(decoder);
        if (content == null) throw new IOException("registry_empty");
        DaemonInfo found = null;
        for (DaemonInfo d : content.getInfos()) if (d.getPid() != null && d.getPid() == pid) {
          if (found != null) throw new IOException("duplicate_pid");
          found = d;
        }
        if (found == null) throw new IOException("daemon_missing");
        Path expected = found.getContext().getDaemonRegistryDir().toPath().resolve(version).resolve("registry.bin");
        if (!Files.isSameFile(expected, registry)) throw new IOException("registry_context_mismatch");
        return found;
      }
    }
  }

  static int port(DaemonInfo d) throws Exception {
    if (!(d.getAddress() instanceof InetEndpoint)) throw new IOException("address_not_inet");
    InetEndpoint address = (InetEndpoint)d.getAddress();
    if (address.getCandidates().isEmpty()) throw new IOException("address_empty");
    for (InetAddress host : address.getCandidates()) if (!host.isLoopbackAddress())
      throw new IOException("address_not_loopback");
    return address.getPort();
  }

  static int consumers(long pid, int port) throws Exception {
    Process p = new ProcessBuilder("/usr/sbin/lsof", "-nP", "-a", "-p", Long.toString(pid),
        "-iTCP", "-F", "fnT").redirectError(ProcessBuilder.Redirect.DISCARD).start();
    byte[] bytes = p.getInputStream().readNBytes(131073);
    if (!p.waitFor(5, java.util.concurrent.TimeUnit.SECONDS)) {
      p.destroy(); throw new IOException("connections_timeout");
    }
    if (p.exitValue() != 0 || bytes.length > 131072) throw new IOException("connections_unavailable");
    String name = null, state = null;
    int listeners = 0, count = 0;
    for (String line : (new String(bytes, java.nio.charset.StandardCharsets.UTF_8)+"\nfEND").split("\n")) {
      if (line.startsWith("f")) {
        if (name != null && state != null) {
          String local = name.split("->", 2)[0];
          if (local.endsWith(":"+port)) {
            if (state.equals("LISTEN")) listeners++;
            else if (state.equals("ESTABLISHED")) count++;
            else throw new IOException("connection_state_unknown");
          }
        }
        name = state = null;
      } else if (line.startsWith("n")) name = line.substring(1);
      else if (line.startsWith("TST=")) state = line.substring(4);
    }
    if (listeners != 1) throw new IOException("listener_unproven");
    return count;
  }

  static Map<String,Object> metadata(DaemonInfo d, long start) throws Exception {
    Map<String,Object> out = new LinkedHashMap<>();
    int port = port(d);
    out.put("pid", d.getPid()); out.put("java_start_ms", start);
    out.put("daemon_uid", d.getUid()); out.put("registry_state", d.getState().name());
    out.put("last_busy_ms", d.getLastBusy().getTime()); out.put("port", port);
    out.put("consumer_count", d.getState() == DaemonState.Idle ? consumers(d.getPid(), port) : -1);
    return out;
  }

  public static void main(String[] args) {
    Map<String,Object> result = new LinkedHashMap<>();
    boolean sent = false;
    try {
      String mode = args[0]; version = args[1];
      if (mode.equals("stop") && args.length > 8) journal = Paths.get(args[8]);
      if (!version.equals(GradleVersion.current().getVersion())) throw new IOException("jar_version_mismatch");
      Path registry = Paths.get(args[2]); long pid = Long.parseLong(args[3]);
      long start = Long.parseLong(args[4]);
      checkProcess(pid, start);
      DaemonInfo first = read(registry, pid);
      result.putAll(metadata(first, start));
      result.put("ok", true);
      if (mode.equals("stop")) {
        String uid = args[5]; long lastBusy = Long.parseLong(args[6]); int port = Integer.parseInt(args[7]);
        if (!uid.equals(first.getUid()) || lastBusy != first.getLastBusy().getTime() ||
            port != port(first) || first.getState() != DaemonState.Idle ||
            !Integer.valueOf(0).equals(result.get("consumer_count"))) throw new IOException("not_idle_or_identity_changed");
        // Repeat after connection inspection. The daemon's locked graceful
        // state machine resolves the remaining race with a starting build.
        checkProcess(pid, start);
        DaemonInfo second = read(registry, pid);
        if (!uid.equals(second.getUid()) || lastBusy != second.getLastBusy().getTime() ||
            port != port(second) || second.getState() != DaemonState.Idle ||
            !Arrays.equals(first.getToken(), second.getToken())) throw new IOException("final_state_changed");
        Serializer<Message> serializer = DaemonMessageSerializer.create(new DefaultSerializer<BuildAction>());
        RemoteConnection<Message> c = new TcpOutgoingConnector().connect(second.getAddress()).create(Serializers.stateful(serializer));
        try {
          // Only this authenticated daemon. Never Stop, Cancel, Build, or a
          // version-wide command. Once dispatched an unknown result is NOT retried.
          if (journal != null) {
            writeJournal("ready.json", Map.of("phase", "ready", "dispatched", false));
            String commit = new BufferedReader(new InputStreamReader(System.in, StandardCharsets.UTF_8)).readLine();
            if (!"COMMIT".equals(commit)) throw new IOException("commit_not_received");
            // Loading and the handshake may take time. Revalidate again before
            // the write. This uses the same daemon UID/token and never stops a
            // daemon already observed Busy or with a fresh consumer.
            checkProcess(pid, start);
            DaemonInfo finalInfo = read(registry, pid);
            if (!uid.equals(finalInfo.getUid()) || lastBusy != finalInfo.getLastBusy().getTime() ||
                port != port(finalInfo) || finalInfo.getState() != DaemonState.Idle ||
                !Arrays.equals(second.getToken(), finalInfo.getToken())) throw new IOException("final_state_changed");
            // The only connected consumer at this point is this helper's own
            // already established command connection.
            if (consumers(pid, port) != 1) throw new IOException("final_consumers_changed");
            writeJournal("dispatching.json", Map.of("phase", "dispatching", "dispatched", true));
          }
          sent = true;
          c.dispatch(new StopWhenIdle(UUID.randomUUID(), second.getToken())); c.flush();
          Message reply = c.receive();
          if (!(reply instanceof Success)) throw new IOException("stop_response_unknown");
          c.dispatch(new Finished()); c.flush();
          result.put("accepted", true);
        } finally { c.stop(); }
      } else if (!mode.equals("inspect")) throw new IOException("invalid_mode");
    } catch (Throwable ex) {
      result.clear(); result.put("ok", false);
      // Do not print exception text: third-party exceptions can contain tokens.
      result.put("reason", SAFE_REASONS.contains(ex.getMessage()) ? ex.getMessage() : "adapter_"+ex.getClass().getSimpleName());
      result.put("unknown", sent);
    }
    result.put("dispatched", sent);
    try { writeJournal("result.json", result); }
    catch (Throwable ignored) { /* No result is an unknown outcome after commit. */ }
    System.out.println(JSON.toJson(result));
  }
}
'''


class _BSDInfo(ctypes.Structure):
    _fields_ = [(n, ctypes.c_uint32) for n in (
        "flags", "status", "xstatus", "pid", "ppid", "uid", "gid", "ruid", "rgid", "svuid", "svgid", "reserved")]
    _fields_ += [("comm", ctypes.c_char * 16), ("name", ctypes.c_char * 32)]
    _fields_ += [(n, ctypes.c_uint32) for n in ("nfiles", "pgid", "jobc", "tdev", "tpgid")]
    _fields_ += [("nice", ctypes.c_int32), ("sec", ctypes.c_uint64), ("usec", ctypes.c_uint64)]


def identity(pid):
    try:
        lib = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
        info = _BSDInfo()
        if lib.proc_pidinfo(int(pid), 3, 0, ctypes.byref(info), ctypes.sizeof(info)) != ctypes.sizeof(info) or info.pid != int(pid):
            return None
        return {"pid": info.pid, "uid": info.uid, "status": info.status,
                "start": f"{info.sec}:{info.usec:06d}", "java_start_ms": info.sec * 1000 + info.usec // 1000}
    except (OSError, ValueError, TypeError):
        return None


def _run(argv, timeout=10):
    p = subprocess.run(argv, capture_output=True, text=True, errors="surrogateescape", timeout=timeout)
    if p.returncode != 0 or len(p.stdout) > MAX_OUTPUT:
        raise RuntimeError("command_unavailable")
    return p.stdout


def _distribution(version, command):
    for family in ("all", "bin"):
        root = Path.home() / ".gradle/wrapper/dists" / f"gradle-{version}-{family}"
        for dist in root.glob(f"*/gradle-{version}"):
            if str(dist / "lib" / f"gradle-daemon-main-{version}.jar") in command:
                return dist
    raise RuntimeError("distribution_unverified")


def _prepare(dist, version, java):
    dest = BUILD_ROOT / version
    dest.mkdir(parents=True, exist_ok=True, mode=0o700)
    # These are core runtime/protocol classes. Scanning unrelated plugin JARs
    # on every JVM startup caused long background I/O before main() could run.
    cp = f"{dist}/lib/*"
    digest = hashlib.sha256(JAVA_SOURCE.encode()).hexdigest()
    with (dest / "compile.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        marker = dest / "source.sha256"
        if not (dest / "DevMemoryGradle.class").is_file() or not marker.is_file() or marker.read_text() != digest:
            source = dest / "DevMemoryGradle.java"
            source.write_text(JAVA_SOURCE)
            _run([str(Path(java).with_name("javac")), "-J-Xmx128m", "-proc:none", "-cp", cp, "-d", str(dest), str(source)], timeout=30)
            marker.write_text(digest)
    return f"{dest}:{cp}"


def _helper(candidate, mode):
    if mode == "stop":
        return _request_stop(candidate)
    stage = "prepare"
    try:
        cp = _prepare(Path(candidate["distribution"]), candidate["version"], candidate["java"])
        args = [candidate["java"], "-Xms8m", "-Xmx64m", "-XX:MaxMetaspaceSize=64m", "-cp", cp,
                "DevMemoryGradle", mode, candidate["version"], candidate["registry"],
                str(candidate["pid"]), str(candidate["java_start_ms"])]
        if mode == "stop":
            e = candidate["evidence"]
            args += [e["daemon_uid"], str(e["last_busy_ms"]), str(e["port"])]
        stage = "invoke"
        output = _run(args, timeout=15)
        stage = "decode"
        result = json.loads(output.strip().splitlines()[-1])
        if not isinstance(result, dict):
            raise ValueError("nonobject")
        return result
    except (subprocess.TimeoutExpired, RuntimeError, ValueError, IndexError, OSError) as exc:
        # Only fixed categories; no stderr or exception message can leak an
        # authentication token. Keep the bounded 15s deadline under pressure.
        category = ("timeout" if isinstance(exc, subprocess.TimeoutExpired) else
                    "command_failed" if isinstance(exc, RuntimeError) else
                    "os_unavailable" if isinstance(exc, OSError) else "invalid_output")
        # Prepare cannot have contacted a daemon. Invoke/decode failures may
        # follow dispatch and remain indeterminate, so they must never replay.
        return {"ok": False, "unknown": mode == "stop" and stage != "prepare",
                "reason": f"helper_{stage}_{category}"}


def _request_root():
    return Path(os.environ.get("DEV_MEMORY_GRADLE_REQUEST_DIR", str(Path.home() / ".local/state/dev-memory/gradle-requests")))


def _resource_key(candidate):
    identity_fields = {k: candidate[k] for k in ("pid", "start", "version", "registry")}
    return hashlib.sha256(json.dumps(identity_fields, sort_keys=True).encode()).hexdigest()


def _durable_json(path, value):
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".new")
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(value, f, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


def _load_json(path):
    if path.is_symlink() or path.stat().st_size > 65536:
        raise ValueError("invalid_journal")
    result = json.loads(path.read_text())
    if not isinstance(result, dict):
        raise ValueError("invalid_journal")
    return result


def _process_state(birth):
    """Exact birth check; unknown is never treated as exit."""
    if not birth or not isinstance(birth.get("pid"), int) or not birth.get("start"):
        return "unknown"
    current = identity(birth["pid"])
    if current is not None:
        if current["uid"] != os.getuid():
            return "unknown"
        return "dead" if current["start"] != birth["start"] or current["status"] == 5 else "alive"
    try:
        os.kill(birth["pid"], 0)
    except ProcessLookupError:
        return "dead"
    except (PermissionError, OSError):
        pass
    return "unknown"


def reconcile(candidate):
    """Read only. Recover a pre-dispatch abort, never replay an unknown write.

    Root may remove pending only when safe_to_retry is exactly True, then take
    two fresh idle observations before a new request. Legacy attempts without a
    handshake journal remain unknown until their original daemon exits.
    """
    result = {"stopped": False, "accepted": False, "pending": True, "unknown": False,
              "safe_to_retry": False}
    try:
        # Retain Popen ownership for asynchronous requests while this sampler
        # lives, then reap only our children. A later sampler uses birth records.
        for request_id, child in list(_OWN_HELPERS.items()):
            if child.poll() is not None:
                _OWN_HELPERS.pop(request_id, None)
        if _process_state(candidate) == "dead":
            # Exit is authoritative even if a journal is missing/corrupt. Keep
            # an acknowledgement when available instead of losing it merely
            # because normal daemon shutdown beat this read-only check.
            try:
                root = _request_root() / _resource_key(candidate)
                attempt = _load_json(root / "current.json")["attempt"]
                if re.fullmatch(r"[a-f0-9]{32}", attempt):
                    result["accepted"] = _load_json(root / attempt / "result.json").get("accepted") is True
            except (OSError, ValueError, KeyError, TypeError):
                pass
            return {**result, "stopped": True, "pending": False, "reason": "daemon_exited"}
        root = _request_root() / _resource_key(candidate)
        if not (root / "current.json").exists():
            return {**result, "unknown": True, "reason": "legacy_outcome_unknown_no_journal"}
        current = _load_json(root / "current.json")
        attempt = current["attempt"]
        if not re.fullmatch(r"[a-f0-9]{32}", attempt):
            raise ValueError("invalid_attempt")
        folder = root / attempt
        request = _load_json(folder / "request.json")
        if request["resource_key"] != _resource_key(candidate):
            raise ValueError("journal_identity_mismatch")
        worker_state = _process_state(request.get("worker"))
        dispatched = (folder / "dispatching.json").exists()
        response = _load_json(folder / "result.json") if (folder / "result.json").exists() else {}
        result.update(request_id=attempt, accepted=response.get("accepted") is True)
        if request.get("spawn_failed") is True and request.get("worker") is None and request.get("committed") is False and not dispatched:
            return {**result, "pending": False, "safe_to_retry": True, "reason": "worker_not_spawned_no_dispatch"}
        if worker_state != "dead":
            return {**result, "reason": "request_worker_active" if worker_state == "alive" else "request_worker_identity_unknown"}
        # The helper accepts dispatch only after COMMIT. A parent abort or JVM
        # startup failure before that handshake is authoritative non-dispatch.
        # After COMMIT, only the helper's explicit non-dispatch result suffices.
        if not dispatched and (request.get("committed") is False or
                               response.get("dispatched") is False and response.get("unknown") is False):
            return {**result, "pending": False, "safe_to_retry": True,
                    "reason": "worker_exited_before_dispatch_proven"}
        return {**result, "unknown": not result["accepted"],
                "reason": "drain_accepted_pending_exit" if result["accepted"] else "dispatch_outcome_unknown_no_retry"}
    except (OSError, ValueError, KeyError, TypeError):
        return {**result, "unknown": True, "reason": "request_journal_unknown_no_retry"}


def _request_stop(candidate):
    """One durable attempt. A helper startup timeout is proven pre-dispatch.

    The Java process survives response timeouts after COMMIT, recording its
    eventual result without holding the periodic sampler alive. No retry occurs
    until the exact helper birth exits and its journal proves non-dispatch.
    """
    helper = None
    committed = False
    try:
        root = _request_root() / _resource_key(candidate)
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        with (root / ".lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return {"ok": False, "pending": True, "reason": "request_already_active"}
            if (root / "current.json").exists():
                previous = reconcile(candidate)
                if previous.get("safe_to_retry") is not True:
                    return previous
            # Prepare first: a compilation/configuration failure has no helper
            # and no possible protocol write, so it never poisons pending.
            cp = _prepare(Path(candidate["distribution"]), candidate["version"], candidate["java"])
            attempt = uuid.uuid4().hex
            folder = root / attempt
            folder.mkdir(mode=0o700)
            request = {"version": 1, "attempt": attempt, "resource_key": _resource_key(candidate),
                       "created_at": time.time(), "committed": False, "worker": None}
            _durable_json(folder / "request.json", request)
            _durable_json(root / "current.json", {"attempt": attempt})
            e = candidate["evidence"]
            args = [candidate["java"], "-Xms8m", "-Xmx64m", "-XX:MaxMetaspaceSize=64m", "-cp", cp,
                    "DevMemoryGradle", "stop", candidate["version"], candidate["registry"],
                    str(candidate["pid"]), str(candidate["java_start_ms"]), e["daemon_uid"],
                    str(e["last_busy_ms"]), str(e["port"]), str(folder)]
            try:
                helper = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                          stderr=subprocess.DEVNULL, start_new_session=True)
                _OWN_HELPERS[attempt] = helper
            except OSError:
                request["spawn_failed"] = True
                _durable_json(folder / "request.json", request)
                return reconcile(candidate)
            request["worker"] = identity(helper.pid)
            _durable_json(folder / "request.json", request)
            deadline = time.monotonic() + STARTUP_TIMEOUT
            while not (folder / "ready.json").exists() and helper.poll() is None and time.monotonic() < deadline:
                time.sleep(0.05)
            if helper.poll() is not None or not (folder / "ready.json").exists():
                # No COMMIT was sent. EOF prevents this worker ever writing a
                # Gradle request, even if its class loading completes later.
                helper.stdin.close()
                if helper.poll() is None:
                    helper.terminate()  # Only the exact child Popen we created.
                    try:
                        helper.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        pass  # No escalation. Reconciliation waits for exit.
                return reconcile(candidate)
            if request["worker"] is None or _process_state(request["worker"]) != "alive":
                helper.stdin.close()
                return {"ok": False, "pending": True, "reason": "request_worker_identity_unknown"}
            request["committed"] = True
            _durable_json(folder / "request.json", request)  # fsync BEFORE COMMIT
            committed = True
            helper.stdin.write(b"COMMIT\n")
            helper.stdin.flush()
            helper.stdin.close()
            deadline = time.monotonic() + RESPONSE_TIMEOUT
            while helper.poll() is None and time.monotonic() < deadline:
                time.sleep(0.05)
            if helper.poll() is not None:
                return reconcile(candidate)
            # Deliberately do not terminate a helper once COMMIT may have been
            # delivered. Its durable result will resolve on subsequent ticks.
            return {"ok": False, "pending": True, "unknown": True, "request_id": attempt,
                    "reason": "response_timeout_worker_continues_no_retry"}
    except (OSError, ValueError, KeyError, TypeError, RuntimeError, subprocess.TimeoutExpired):
        if helper is not None and helper.stdin is not None and not helper.stdin.closed:
            helper.stdin.close()
        return {"ok": False, "pending": helper is not None, "unknown": committed,
                "reason": "request_journal_or_worker_unavailable"}


def fingerprint(candidate):
    fields = {k: candidate.get(k) for k in ("pid", "start", "version", "registry")}
    fields["evidence"] = candidate.get("evidence")
    return hashlib.sha256(json.dumps(fields, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _inspect_one(pid, version, command):
    out = {"kind": "gradle", "id": f"gradle:{pid}", "pid": pid, "version": version,
           "eligible": False, "reason": "unverified"}
    proc = identity(pid)
    if proc is None or proc["uid"] != os.getuid() or proc["status"] == 5:
        return {**out, "reason": "process_identity_unknown"}
    out.update(start=proc["start"], java_start_ms=proc["java_start_ms"])
    out["id"] = f"gradle:{pid}:{proc['start']}"
    if version not in SUPPORTED:
        return {**out, "reason": "unsupported_version"}
    try:
        dist = _distribution(version, command)
        java = _run(["/bin/ps", "-p", str(pid), "-o", "comm="]).strip()
        if not java.startswith("/") or Path(java).name != "java":
            raise RuntimeError("java_unverified")
        # During a build Gradle changes cwd to that build's project. Its open
        # daemon log is stable across builds and points at the true registry.
        files = _run(["/usr/sbin/lsof", "-a", "-p", str(pid), "-Fn"])
        names = list({line[1:] for line in files.splitlines()
                      if line.startswith("n/") and Path(line[1:]).name == f"daemon-{pid}.out.log"})
        if len(names) != 1 or Path(names[0]).parent.name != version:
            raise RuntimeError("registry_path_unverified")
        registry = Path(names[0]).parent / "registry.bin"
        if registry.stat().st_uid != os.getuid():
            raise RuntimeError("registry_owner_mismatch")
        out.update(distribution=str(dist), java=java, registry=str(registry))
        result = _helper(out, "inspect")
        if result.get("ok") is not True:
            return {**out, "reason": result.get("reason", "inspection_unknown")}
        after = identity(pid)
        if after is None or after["start"] != out["start"] or after["uid"] != os.getuid():
            return {**out, "reason": "identity_changed"}
        out["evidence"] = {k: result[k] for k in ("daemon_uid", "registry_state", "last_busy_ms", "port", "consumer_count")}
        out["fingerprint"] = fingerprint(out)
        if result["registry_state"] != "Idle":
            out["reason"] = "daemon_not_idle"
        elif result["consumer_count"] != 0:
            out["reason"] = "active_consumers"
        else:
            out.update(eligible=True, reason="authenticated_registry_idle_no_consumers")
    except (OSError, RuntimeError, ValueError, KeyError, subprocess.TimeoutExpired):
        out["reason"] = "inspection_unknown"
    return out


def inspect(now=None):
    """Read all current-user Gradle daemons. Root enforces two stable observations.

    Every failed resource remains in the result as ineligible. A global process
    enumeration failure raises, so callers must preserve old observations.
    """
    rows = _run(["/bin/ps", "-axo", "uid=,pid=,command="])
    found = []
    for row in rows.splitlines():
        match = re.match(r"\s*(\d+)\s+(\d+)\s+(.*)", row)
        if not match or int(match[1]) != os.getuid():
            continue
        version = re.search(r"(?:^|\s)org\.gradle\.launcher\.daemon\.bootstrap\.GradleDaemon\s+([\d.]+)(?:\s|$)", match[3])
        if version:
            found.append(_inspect_one(int(match[2]), version[1], match[3]))
    return found


def reclaim(candidate):
    """Request exact daemon drain once; never retry an indeterminate write.

    Caller MUST persist a pending marker before entering and reconcile it with
    identity() on later ticks. Acknowledged drain is not an exited process.
    """
    out = {"kind": "gradle", "pid": candidate.get("pid"), "stopped": False,
           "accepted": False, "unknown": False}
    try:
        if candidate.get("eligible") is not True or candidate.get("version") not in SUPPORTED:
            return {**out, "reason": "candidate_ineligible"}
        if candidate.get("fingerprint") != fingerprint(candidate):
            return {**out, "reason": "candidate_changed"}
        proc = identity(candidate["pid"])
        if proc is None or proc["uid"] != os.getuid() or proc["start"] != candidate.get("start"):
            return {**out, "reason": "identity_unknown_or_changed"}
        # The helper repeats registry identity, state, socket-consumer and
        # process checks itself immediately before the authenticated request.
        result = _helper(candidate, "stop")
        if result.get("stopped") is True:
            return {**out, **result, "kind": "gradle", "pid": candidate["pid"]}
        if result.get("accepted") is not True:
            return {**out, **result, "stopped": False, "kind": "gradle", "pid": candidate["pid"],
                    "reason": result.get("reason", "request_not_accepted"), "unknown": result.get("unknown") is True}
        out.update(accepted=True, reason="drain_accepted_pending_exit")
        # Waiting is bounded; never kill an unresponsive daemon or its children.
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            current = identity(candidate["pid"])
            if current is not None and (current["start"] != candidate["start"] or current["status"] == 5):
                return {**out, "stopped": True, "reason": "daemon_exited"}
            if current is None:
                try:
                    os.kill(candidate["pid"], 0)
                except ProcessLookupError:
                    return {**out, "stopped": True, "reason": "daemon_exited"}
                except (PermissionError, OSError):
                    pass
            time.sleep(0.1)
        return out
    except (OSError, ValueError, KeyError, TypeError):
        return {**out, "reason": "revalidation_unknown"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["inspect"], default="inspect", nargs="?")
    parser.parse_args()
    print(json.dumps(inspect(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
