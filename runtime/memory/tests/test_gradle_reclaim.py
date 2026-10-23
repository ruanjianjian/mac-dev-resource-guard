import importlib.util
from concurrent.futures import ThreadPoolExecutor
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

PATH = Path(__file__).resolve().parents[1] / "scripts/gradle_reclaim.py"
SPEC = importlib.util.spec_from_file_location("gradle_reclaim", PATH)
g = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(g)


def candidate():
    c = {"kind": "gradle", "id": "gradle:123:100:123456", "pid": 123,
         "start": "100:123456", "java_start_ms": 100123, "version": "8.14",
         "registry": "/private/fixture/8.14/registry.bin", "distribution": "/fixture/gradle-8.14",
         "java": "/fixture/bin/java", "eligible": True,
         "evidence": {"daemon_uid": "fixture-uid", "registry_state": "Idle", "last_busy_ms": 999,
                      "port": 12345, "consumer_count": 0}}
    c["fingerprint"] = g.fingerprint(c)
    return c


def identity(start="100:123456", status=2):
    return {"pid": 123, "uid": os.getuid(), "start": start, "status": status, "java_start_ms": 100123}


class GradleSafetyTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.patch = patch.object(g, "_request_root", return_value=Path(self.directory.name))
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def test_pid_reuse_never_sends_request(self):
        with patch.object(g, "identity", return_value=identity("200:123456")), patch.object(g, "_helper") as helper:
            result = g.reclaim(candidate())
        self.assertFalse(result["stopped"])
        helper.assert_not_called()

    def test_unknown_process_identity_never_sends_request(self):
        with patch.object(g, "identity", return_value=None), patch.object(g, "_helper") as helper:
            result = g.reclaim(candidate())
        self.assertFalse(result["stopped"])
        helper.assert_not_called()

    def test_other_user_never_sends_request(self):
        proc = identity()
        proc["uid"] += 1
        with patch.object(g, "identity", return_value=proc), patch.object(g, "_helper") as helper:
            g.reclaim(candidate())
        helper.assert_not_called()

    def test_changed_observation_never_sends_request(self):
        c = candidate()
        c["evidence"]["last_busy_ms"] += 1
        with patch.object(g, "_helper") as helper:
            result = g.reclaim(c)
        self.assertEqual(result["reason"], "candidate_changed")
        helper.assert_not_called()

    def test_busy_candidate_never_sends_request(self):
        c = candidate()
        c["eligible"] = False
        with patch.object(g, "_helper") as helper:
            g.reclaim(c)
        helper.assert_not_called()

    def test_final_busy_recheck_is_preserved(self):
        with patch.object(g, "identity", return_value=identity()), patch.object(g, "_helper", return_value={
                "ok": False, "unknown": False, "reason": "adapter_IOException"}) as helper:
            result = g.reclaim(candidate())
        self.assertFalse(result["accepted"])
        self.assertFalse(result["stopped"])
        self.assertFalse(result["unknown"])
        self.assertEqual(helper.call_count, 1)

    def test_unknown_outcome_is_returned_once_without_retry(self):
        with patch.object(g, "identity", return_value=identity()), patch.object(g, "_helper", return_value={
                "ok": False, "unknown": True, "reason": "timeout"}) as helper:
            result = g.reclaim(candidate())
        self.assertTrue(result["unknown"])
        self.assertFalse(result["stopped"])
        self.assertEqual(helper.call_count, 1)

    def test_accepted_is_not_reported_as_exited(self):
        with patch.object(g, "identity", return_value=identity()), patch.object(g, "_helper", return_value={
                "ok": True, "accepted": True}), patch.object(g.time, "monotonic", side_effect=[0, 6]):
            result = g.reclaim(candidate())
        self.assertTrue(result["accepted"])
        self.assertFalse(result["stopped"])

    def test_actual_pid_exit_counts_as_stopped(self):
        with patch.object(g, "identity", side_effect=[identity(), None]), patch.object(g, "_helper", return_value={
                "ok": True, "accepted": True}), patch.object(g.os, "kill", side_effect=ProcessLookupError) as kill:
            result = g.reclaim(candidate())
        self.assertTrue(result["stopped"])
        kill.assert_called_once_with(123, 0)

    def test_unknown_exit_state_does_not_count_as_stopped(self):
        with patch.object(g, "identity", side_effect=[identity(), None]), patch.object(g, "_helper", return_value={
                "ok": True, "accepted": True}), patch.object(g.os, "kill", side_effect=PermissionError), \
                patch.object(g.time, "monotonic", side_effect=[0, 1, 6]), patch.object(g.time, "sleep"):
            result = g.reclaim(candidate())
        self.assertFalse(result["stopped"])
        self.assertTrue(result["accepted"])

    def test_helper_failure_does_not_expose_process_secrets(self):
        c = candidate()
        with patch.object(g, "_prepare", return_value="classpath"), patch.object(g, "_run", side_effect=RuntimeError("token=SECRET")):
            output = g._helper(c, "inspect")
        self.assertNotIn("SECRET", json.dumps(output))
        self.assertFalse(output["unknown"])

    def test_discovery_filters_current_user_and_keeps_all_daemons(self):
        text = (f"{os.getuid()} 123 /java org.gradle.launcher.daemon.bootstrap.GradleDaemon 8.14\n"
                f"{os.getuid()} 456 /java org.gradle.launcher.daemon.bootstrap.GradleDaemon 8.11.1\n"
                f"{os.getuid()+1} 789 /java org.gradle.launcher.daemon.bootstrap.GradleDaemon 8.14\n")
        with patch.object(g, "_run", return_value=text), patch.object(g, "_inspect_one", side_effect=lambda p, v, _: {"pid": p, "version": v}):
            rows = g.inspect()
        self.assertEqual([x["pid"] for x in rows], [123, 456])

    def test_global_enumeration_failure_is_not_empty_success(self):
        with patch.object(g, "_run", side_effect=RuntimeError("failed")):
            with self.assertRaises(RuntimeError):
                g.inspect()

    def test_short_build_between_observations_changes_fingerprint(self):
        one = candidate()
        two = candidate()
        two["evidence"]["last_busy_ms"] += 1000
        self.assertNotEqual(g.fingerprint(one), g.fingerprint(two))

    def test_no_unsafe_stop_protocol_or_signals_exist(self):
        source = PATH.read_text()
        self.assertNotIn("new Stop(", source)
        self.assertNotIn("new Cancel(", source)
        self.assertIn("new StopWhenIdle(", source)
        self.assertNotIn("SIGKILL", source)
        self.assertNotIn("SIGTERM", source)
        self.assertNotIn('"--stop"', source)

    def test_stop_uses_durable_request_path_once(self):
        with patch.object(g, "_request_stop", return_value={"pending": True}) as request:
            result = g._helper(candidate(), "stop")
        self.assertTrue(result["pending"])
        self.assertEqual(request.call_count, 1)

    def test_readonly_timeout_is_classified_and_not_a_write(self):
        with patch.object(g, "_prepare", return_value="classpath"), patch.object(g, "_run", side_effect=subprocess.TimeoutExpired("java", 15)):
            result = g._helper(candidate(), "inspect")
        self.assertFalse(result["unknown"])
        self.assertEqual(result["reason"], "helper_invoke_timeout")

    def test_compile_failure_is_proven_pre_dispatch(self):
        with patch.object(g, "_prepare", side_effect=RuntimeError("SECRET token")), patch.object(g, "_run") as run:
            result = g._helper(candidate(), "stop")
        self.assertFalse(result["unknown"])
        self.assertEqual(result["reason"], "request_journal_or_worker_unavailable")
        self.assertNotIn("SECRET", json.dumps(result))
        run.assert_not_called()

    def test_invalid_reply_is_unknown_after_possible_dispatch(self):
        with patch.object(g, "_prepare", return_value="classpath"), patch.object(g, "_run", return_value="invalid SECRET reply"):
            result = g._helper(candidate(), "inspect")
        self.assertFalse(result["unknown"])
        self.assertEqual(result["reason"], "helper_decode_invalid_output")
        self.assertNotIn("SECRET", json.dumps(result))

    def journal(self, committed=False, dispatched=False, response=None, worker=True):
        c = candidate()
        root = g._request_root() / g._resource_key(c)
        attempt = "1" * 32
        folder = root / attempt
        folder.mkdir(parents=True)
        request = {"resource_key": g._resource_key(c), "committed": committed,
                   "worker": {"pid": 456, "start": "200:123456"} if worker else None}
        g._durable_json(folder / "request.json", request)
        g._durable_json(root / "current.json", {"attempt": attempt})
        if dispatched:
            g._durable_json(folder / "dispatching.json", {"dispatched": True})
        if response is not None:
            g._durable_json(folder / "result.json", response)
        return c, folder

    def state(self, worker="dead", daemon="alive"):
        return lambda birth: daemon if birth and birth.get("pid") == 123 else worker

    def test_legacy_unknown_never_becomes_retryable(self):
        with patch.object(g, "_process_state", return_value="alive"):
            result = g.reconcile(candidate())
        self.assertFalse(result["safe_to_retry"])
        self.assertTrue(result["unknown"])

    def test_precise_worker_exit_and_no_commit_proves_retryable(self):
        c, _ = self.journal()
        with patch.object(g, "_process_state", side_effect=self.state()):
            result = g.reconcile(c)
        self.assertTrue(result["safe_to_retry"])
        self.assertFalse(result["pending"])

    def test_active_precommit_worker_is_not_retryable(self):
        c, _ = self.journal()
        with patch.object(g, "_process_state", side_effect=self.state(worker="alive")):
            result = g.reconcile(c)
        self.assertFalse(result["safe_to_retry"])

    def test_unknown_worker_birth_is_not_retryable(self):
        c, _ = self.journal(worker=False)
        with patch.object(g, "_process_state", side_effect=self.state(worker="unknown")):
            result = g.reconcile(c)
        self.assertFalse(result["safe_to_retry"])

    def test_committed_unknown_is_not_retryable_even_without_dispatch_marker(self):
        c, _ = self.journal(committed=True)
        with patch.object(g, "_process_state", side_effect=self.state()):
            result = g.reconcile(c)
        self.assertFalse(result["safe_to_retry"])
        self.assertTrue(result["unknown"])

    def test_commit_then_explicit_nondispatch_result_is_recoverable(self):
        c, _ = self.journal(committed=True, response={"unknown": False, "dispatched": False})
        with patch.object(g, "_process_state", side_effect=self.state()):
            result = g.reconcile(c)
        self.assertTrue(result["safe_to_retry"])

    def test_dispatch_marker_overrides_any_inconsistent_nondispatch(self):
        c, _ = self.journal(committed=False, dispatched=True, response={"unknown": False, "dispatched": False})
        with patch.object(g, "_process_state", side_effect=self.state()):
            result = g.reconcile(c)
        self.assertFalse(result["safe_to_retry"])

    def test_acknowledged_stop_stays_pending_until_daemon_exit(self):
        c, _ = self.journal(committed=True, dispatched=True, response={"accepted": True, "dispatched": True})
        with patch.object(g, "_process_state", side_effect=self.state()):
            result = g.reconcile(c)
        self.assertTrue(result["accepted"])
        self.assertFalse(result["stopped"])
        self.assertFalse(result["safe_to_retry"])

    def test_unknown_journal_is_never_overwritten_by_reclaim(self):
        c, folder = self.journal(committed=True, dispatched=True)
        before = (folder / "request.json").read_bytes()
        with patch.object(g, "_process_state", side_effect=self.state()), patch.object(g.subprocess, "Popen") as popen:
            result = g._request_stop(c)
        self.assertTrue(result["unknown"])
        self.assertEqual(before, (folder / "request.json").read_bytes())
        popen.assert_not_called()

    def test_pending_resolves_to_confirmed_original_daemon_exit(self):
        c, _ = self.journal(committed=True, dispatched=True)
        with patch.object(g, "_process_state", side_effect=self.state(daemon="dead")):
            result = g.reconcile(c)
        self.assertTrue(result["stopped"])
        self.assertFalse(result["pending"])

    def test_corrupt_journal_is_not_retryable(self):
        c, folder = self.journal()
        (folder / "request.json").write_text("corrupt")
        with patch.object(g, "_process_state", side_effect=self.state()):
            result = g.reconcile(c)
        self.assertFalse(result["safe_to_retry"])
        self.assertTrue(result["unknown"])

    def test_exact_worker_birth_reuse_counts_as_original_exit(self):
        with patch.object(g, "identity", return_value=identity("999:999999")):
            result = g._process_state({"pid": 123, "start": "100:123456"})
        self.assertEqual(result, "dead")

    def test_unknown_kernel_state_with_permission_error_is_not_dead(self):
        with patch.object(g, "identity", return_value=None), patch.object(g.os, "kill", side_effect=PermissionError):
            result = g._process_state({"pid": 123, "start": "100:123456"})
        self.assertEqual(result, "unknown")

    def fake_worker(self, ready=True):
        outer = self
        class Worker:
            pid = 456
            terminated = False
            exited = False
            committed_bytes = b""

            def __init__(self, argv, **kwargs):
                self.folder = Path(argv[-1])
                worker = self
                class Input(io.BytesIO):
                    def write(self, payload):
                        request = g._load_json(worker.folder / "request.json")
                        outer.assertTrue(request["committed"], "journal must be durable before COMMIT")
                        worker.committed_bytes += payload
                        return super().write(payload)
                self.stdin = Input()
                if ready:
                    g._durable_json(self.folder / "ready.json", {"phase": "ready"})

            def poll(self):
                return 0 if self.exited else None

            def terminate(self):
                self.terminated = True
                self.exited = True

            def wait(self, timeout=None):
                return 0
        return Worker

    def test_response_timeout_does_not_terminate_committed_helper(self):
        workers = []
        def spawn(*args, **kwargs):
            worker = self.fake_worker()(*args, **kwargs)
            workers.append(worker)
            return worker
        def birth(pid):
            return identity() if pid == 123 else {**identity("200:123456"), "pid": 456}
        with patch.object(g, "_prepare", return_value="classpath"), patch.object(g.subprocess, "Popen", side_effect=spawn), \
                patch.object(g, "identity", side_effect=birth), patch.object(g, "RESPONSE_TIMEOUT", 0):
            result = g._request_stop(candidate())
        self.assertTrue(result["pending"])
        self.assertTrue(result["unknown"])
        self.assertFalse(workers[0].terminated)
        self.assertEqual(workers[0].committed_bytes, b"COMMIT\n")

    def test_precommit_startup_timeout_is_retryable_only_after_exact_child_exit(self):
        workers = []
        def spawn(*args, **kwargs):
            worker = self.fake_worker(ready=False)(*args, **kwargs)
            workers.append(worker)
            return worker
        def birth(pid):
            if pid == 123:
                return identity()
            return {**identity("200:123456", status=5 if workers[0].exited else 2), "pid": 456}
        with patch.object(g, "_prepare", return_value="classpath"), patch.object(g.subprocess, "Popen", side_effect=spawn), \
                patch.object(g, "identity", side_effect=birth), patch.object(g, "STARTUP_TIMEOUT", 0):
            result = g._request_stop(candidate())
        self.assertTrue(result["safe_to_retry"])
        self.assertTrue(workers[0].terminated)
        self.assertEqual(workers[0].committed_bytes, b"")

    def test_commit_journal_failure_never_sends_commit(self):
        workers = []
        original = g._durable_json
        def write(path, value):
            if path.name == "request.json" and value.get("committed") is True:
                raise OSError("disk write failed")
            original(path, value)
        def spawn(*args, **kwargs):
            worker = self.fake_worker()(*args, **kwargs)
            workers.append(worker)
            return worker
        with patch.object(g, "_prepare", return_value="classpath"), patch.object(g.subprocess, "Popen", side_effect=spawn), \
                patch.object(g, "identity", return_value={**identity("200:123456"), "pid": 456}), patch.object(g, "_durable_json", side_effect=write):
            result = g._request_stop(candidate())
        self.assertFalse(result["unknown"])
        self.assertEqual(workers[0].committed_bytes, b"")
        self.assertTrue(workers[0].stdin.closed)

    def test_initial_journal_failure_prevents_helper_spawn(self):
        with patch.object(g, "_prepare", return_value="classpath"), patch.object(g, "_durable_json", side_effect=OSError), \
                patch.object(g.subprocess, "Popen") as popen:
            result = g._request_stop(candidate())
        self.assertFalse(result["unknown"])
        popen.assert_not_called()

    def test_java_enforces_commit_and_durable_dispatch_marker(self):
        source = g.JAVA_SOURCE
        self.assertLess(source.index('if (!"COMMIT".equals(commit))'), source.index('c.dispatch(new StopWhenIdle'))
        self.assertLess(source.index('writeJournal("dispatching.json"'), source.index('c.dispatch(new StopWhenIdle'))
        self.assertIn('f.force(true)', source)
        self.assertIn('directory.force(true)', source)


@unittest.skipUnless(os.environ.get("DEV_MEMORY_GRADLE_INTEGRATION") == "1", "opt-in isolated Gradle builds")
class GradleIntegrationTests(unittest.TestCase):
    def setUp(self):
        directory = Path.home() / ".local/state/dev-resource-guard-test-fixtures/gradle-v3-integration/requests"
        patcher = patch.object(g, "_request_root", return_value=directory)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_slow_startup_recovers_then_committed_timeout_finishes_both_versions(self):
        base = Path.home() / ".local/state/dev-resource-guard-test-fixtures/gradle-v3-integration"
        results = []
        production_source = g.JAVA_SOURCE
        for version in ("8.14", "8.11.1"):
            with self.subTest(version=version):
                root = base / version
                project, registry = root / "project", root / "daemon"
                project.mkdir(parents=True, exist_ok=True)
                (project / "settings.gradle").write_text("rootProject.name = 'dev-memory-handshake-recovery'\n")
                (project / "build.gradle").write_text("tasks.register('warm') { doLast { println 'WARM_OK' } }\n")
                dist = next((Path.home() / f".gradle/wrapper/dists/gradle-{version}-all").glob(f"*/gradle-{version}"))
                env = {**os.environ, "JAVA_HOME": "/opt/homebrew/Cellar/openjdk@21/21.0.12.1/libexec/openjdk.jdk/Contents/Home"}
                command = [str(dist / "bin/gradle"), "--offline", "--no-watch-fs", "--console=plain",
                           f"-Dorg.gradle.daemon.registry.base={registry}",
                           "-Dorg.gradle.jvmargs=-Xmx128m -XX:MaxMetaspaceSize=128m",
                           "-Dorg.gradle.daemon.idletimeout=120000", "-p", str(project)]
                with (root / "warm.log").open("w") as log:
                    self.assertEqual(subprocess.run(command + ["warm"], env=env, stdout=log, stderr=subprocess.STDOUT, timeout=90).returncode, 0)
                idle = next(x for x in g.inspect() if x.get("registry") == str(registry / version / "registry.bin"))
                self.assertTrue(idle["eligible"])
                slow = production_source.replace('Map<String,Object> result = new LinkedHashMap<>();',
                    'try { Thread.sleep(1800); } catch (InterruptedException ignored) { return; } Map<String,Object> result = new LinkedHashMap<>();')
                with patch.object(g, "JAVA_SOURCE", slow), patch.object(g, "BUILD_ROOT", base / "slow-start-helper"), \
                        patch.object(g, "STARTUP_TIMEOUT", 0.2):
                    aborted = g.reclaim(idle)
                self.assertTrue(aborted["safe_to_retry"], aborted)
                self.assertFalse(aborted["stopped"])
                self.assertEqual(g.identity(idle["pid"])["start"], idle["start"])
                key_root = g._request_root() / g._resource_key(idle)
                attempt = g._load_json(key_root / "current.json")["attempt"]
                before = key_root / attempt
                self.assertFalse((before / "dispatching.json").exists())
                self.assertFalse(g._load_json(before / "request.json")["committed"])
                self.assertTrue(g.reconcile(idle)["safe_to_retry"])
                # Exercise the actual bridge's two fresh observations. This
                # uses fixed sample times, not a fake claim of 30s wall time.
                spec = importlib.util.spec_from_file_location("memory_external_handshake_test", PATH.with_name("memory_external.py"))
                bridge = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(bridge)
                empty = type("Empty", (), {"inspect": staticmethod(lambda **kwargs: [])})()
                state = {}
                fresh = next(x for x in g.inspect() if x["pid"] == idle["pid"])
                with patch.object(bridge, "device_inspect", return_value=[]), \
                        patch.object(bridge, "adapter", side_effect=lambda name: g if name == "gradle_reclaim" else empty), \
                        patch.object(g, "inspect", return_value=[fresh]):
                    first, _ = bridge.collect(state, 100)
                    second, records = bridge.collect(state, 131)
                self.assertFalse(first[0]["eligible"])
                self.assertTrue(second[0]["eligible"])
                # Simulate a slow response after COMMIT. The exact self-owned
                # worker must survive parent timeout and finish normal drain.
                delayed = production_source.replace('sent = true;', 'sent = true; Thread.sleep(1500);')
                with patch.object(g, "JAVA_SOURCE", delayed), patch.object(g, "BUILD_ROOT", base / "slow-response-helper"), \
                        patch.object(g, "RESPONSE_TIMEOUT", 0.1):
                    pending = g.reclaim(records[second[0]["id"]])
                self.assertTrue(pending["pending"], pending)
                self.assertTrue(pending["unknown"], pending)
                latest_attempt = g._load_json(key_root / "current.json")["attempt"]
                self.assertNotEqual(attempt, latest_attempt)
                active_request = g._load_json(key_root / latest_attempt / "request.json")
                self.assertEqual(g._process_state(active_request["worker"]), "alive")
                self.assertTrue(active_request["committed"])
                deadline = time.monotonic() + 10
                settled = g.reconcile(fresh)
                while not settled["stopped"] and time.monotonic() < deadline:
                    time.sleep(0.1)
                    settled = g.reconcile(fresh)
                self.assertTrue(settled["stopped"], settled)
                results.append({"version": version, "daemon_pid": fresh["pid"],
                                "slow_startup_precommit_abort": aborted,
                                "first_attempt_dispatch_marker_absent": True,
                                "exact_startup_worker_exit_proved": True,
                                "bridge_first_observation_eligible": first[0]["eligible"],
                                "bridge_second_observation_eligible": second[0]["eligible"],
                                "committed_response_timeout": pending,
                                "committed_worker_survived_parent_timeout": True,
                                "final_reconciliation": settled})
                (base / "results.json").write_text(json.dumps(results, indent=2))
    def test_atomic_drain_race_preserves_new_build_until_normal_completion(self):
        base = Path.home() / ".local/state/dev-resource-guard-test-fixtures/gradle-race-integration"
        results = []
        for version in ("8.14", "8.11.1"):
            with self.subTest(version=version):
                root = base / version
                project, registry = root / "project", root / "daemon"
                project.mkdir(parents=True, exist_ok=True)
                for marker in ("hold.started", "hold.release", "hold.done", "race.ready", "race.send"):
                    (project / marker).unlink(missing_ok=True)
                (project / "settings.gradle").write_text("rootProject.name = 'dev-memory-protocol-race'\n")
                (project / "build.gradle").write_text("""
tasks.register('warm') { doLast { println 'WARM_OK' } }
tasks.register('hold') { doLast {
    file('hold.started').text = 'started'
    def deadline = System.currentTimeMillis() + 60000
    while (!file('hold.release').exists() && System.currentTimeMillis() < deadline) { Thread.sleep(100) }
    if (!file('hold.release').exists()) { throw new GradleException('test release timeout') }
    file('hold.done').text = 'normal completion'
    println 'HOLD_COMPLETED_NORMALLY'
} }
""")
                dist = next((Path.home() / f".gradle/wrapper/dists/gradle-{version}-all").glob(f"*/gradle-{version}"))
                env = {**os.environ, "JAVA_HOME": "/opt/homebrew/Cellar/openjdk@21/21.0.12.1/libexec/openjdk.jdk/Contents/Home"}
                command = [str(dist / "bin/gradle"), "--offline", "--no-watch-fs", "--console=plain",
                           f"-Dorg.gradle.daemon.registry.base={registry}",
                           "-Dorg.gradle.jvmargs=-Xmx128m -XX:MaxMetaspaceSize=128m",
                           "-Dorg.gradle.daemon.idletimeout=120000", "-p", str(project)]
                with (root / "warm.log").open("w") as log:
                    self.assertEqual(subprocess.run(command + ["warm"], env=env, stdout=log, stderr=subprocess.STDOUT, timeout=90).returncode, 0)
                idle = next(x for x in g.inspect() if x.get("registry") == str(registry / version / "registry.bin"))
                self.assertTrue(idle["eligible"])
                # Add only a fixture latch after the final revalidation and
                # network connection, before protocol dispatch. Production
                # helper source and its compiled cache are unchanged.
                ready, send = project / "race.ready", project / "race.send"
                gate = (f'Files.writeString(Paths.get({json.dumps(str(ready))}), "ready"); '
                        f'while (!Files.exists(Paths.get({json.dumps(str(send))}))) {{ Thread.sleep(25); }} sent = true;')
                source = g.JAVA_SOURCE.replace("sent = true;", gate)
                child = None
                with patch.object(g, "JAVA_SOURCE", source), patch.object(g, "BUILD_ROOT", base / "test-only-helper"), \
                        ThreadPoolExecutor(max_workers=1) as executor, (root / "hold.log").open("w") as log:
                    future = executor.submit(g.reclaim, idle)
                    try:
                        deadline = time.monotonic() + 12
                        while not ready.exists() and not future.done() and time.monotonic() < deadline:
                            time.sleep(0.05)
                        self.assertTrue(ready.exists())
                        child = subprocess.Popen(command + ["hold"], env=env, stdout=log, stderr=subprocess.STDOUT)
                        deadline = time.monotonic() + 8
                        while not (project / "hold.started").exists() and child.poll() is None and time.monotonic() < deadline:
                            time.sleep(0.05)
                        self.assertTrue((project / "hold.started").exists())
                        send.write_text("send graceful stop after new build became busy")
                        outcome = future.result(timeout=20)
                        self.assertTrue(outcome["accepted"], outcome)
                        self.assertFalse(outcome["stopped"], outcome)
                        self.assertIsNone(child.poll(), "build must still be alive awaiting its normal release")
                    finally:
                        send.write_text("release helper")
                        (project / "hold.release").write_text("normal release")
                        if child is not None:
                            child.wait(timeout=45)
                self.assertEqual(child.returncode, 0)
                self.assertEqual((project / "hold.done").read_text(), "normal completion")
                deadline = time.monotonic() + 8
                while g.identity(idle["pid"]) is not None and time.monotonic() < deadline:
                    time.sleep(0.1)
                self.assertIsNone(g.identity(idle["pid"]))
                results.append({"version": version, "pid": idle["pid"], "race_build_completed_normally": True,
                                "accepted_while_busy": True, "exit_only_after_build_complete": True})
                (base / "results.json").write_text(json.dumps(results, indent=2))

    def test_both_versions_preserve_busy_then_stop_only_isolated_idle_daemon(self):
        base = Path.home() / ".local/state/dev-resource-guard-test-fixtures/gradle-integration"
        base.mkdir(parents=True, exist_ok=True)
        java = "/opt/homebrew/Cellar/openjdk@21/21.0.12.1/libexec/openjdk.jdk/Contents/Home/bin/java"
        original = {x["pid"]: x.get("start") for x in g.inspect()
                    if not str(x.get("registry", "")).startswith(str(base) + "/")}
        results = []
        for version in ("8.14", "8.11.1"):
            with self.subTest(version=version):
                root = base / version
                project, registry = root / "project", root / "daemon"
                project.mkdir(parents=True, exist_ok=True)
                for marker in ("hold.started", "hold.release", "hold.done"):
                    (project / marker).unlink(missing_ok=True)
                (project / "settings.gradle").write_text("rootProject.name = 'dev-memory-isolated-verification'\n")
                (project / "build.gradle").write_text("""
tasks.register('warm') { doLast { println 'WARM_OK' } }
tasks.register('hold') { doLast {
    file('hold.started').text = 'started'
    def deadline = System.currentTimeMillis() + 60000
    while (!file('hold.release').exists() && System.currentTimeMillis() < deadline) { Thread.sleep(100) }
    if (!file('hold.release').exists()) { throw new GradleException('test release timeout') }
    file('hold.done').text = 'normal completion'
    println 'HOLD_COMPLETED_NORMALLY'
} }
""")
                dist = next((Path.home() / f".gradle/wrapper/dists/gradle-{version}-all").glob(f"*/gradle-{version}"))
                env = {**os.environ, "JAVA_HOME": str(Path(java).parent.parent)}
                command = [str(dist / "bin/gradle"), "--offline", "--no-watch-fs", "--console=plain",
                           f"-Dorg.gradle.daemon.registry.base={registry}",
                           "-Dorg.gradle.jvmargs=-Xmx128m -XX:MaxMetaspaceSize=128m",
                           "-Dorg.gradle.daemon.idletimeout=120000", "-p", str(project)]
                with (root / "warm.log").open("w") as log:
                    warm = subprocess.run(command + ["warm"], env=env, stdout=log, stderr=subprocess.STDOUT, timeout=90)
                self.assertEqual(warm.returncode, 0, str(root / "warm.log"))
                own = [x for x in g.inspect() if x.get("registry") == str(registry / version / "registry.bin")]
                self.assertEqual(len(own), 1)
                idle_before = own[0]
                self.assertTrue(idle_before["eligible"], idle_before)
                with (root / "hold.log").open("w") as log:
                    child = subprocess.Popen(command + ["hold"], env=env, stdout=log, stderr=subprocess.STDOUT)
                    try:
                        deadline = time.monotonic() + 45
                        while not (project / "hold.started").exists() and child.poll() is None and time.monotonic() < deadline:
                            time.sleep(0.1)
                        self.assertTrue((project / "hold.started").exists())
                        busy = next(x for x in g.inspect() if x["pid"] == idle_before["pid"])
                        self.assertFalse(busy["eligible"])
                        self.assertIn("evidence", busy, busy)
                        self.assertEqual(busy["evidence"]["registry_state"], "Busy")
                        # A saved Idle observation cannot stop a newly Busy daemon.
                        refused = g.reclaim(idle_before)
                        self.assertFalse(refused["accepted"], refused)
                        self.assertFalse(refused["stopped"], refused)
                        self.assertFalse(refused["unknown"], refused)
                    finally:
                        (project / "hold.release").write_text("release")
                        child.wait(timeout=45)
                    self.assertEqual(child.returncode, 0)
                self.assertEqual((project / "hold.done").read_text(), "normal completion")
                fresh = next(x for x in g.inspect() if x["pid"] == idle_before["pid"])
                self.assertTrue(fresh["eligible"], fresh)
                stopped = g.reclaim(fresh)
                self.assertTrue(stopped["accepted"], stopped)
                self.assertTrue(stopped["stopped"], stopped)
                preserved = {pid: g.identity(pid) for pid in original}
                self.assertTrue(all(p and p["start"] == original[pid] for pid, p in preserved.items()))
                results.append({"version": version, "test_pid": fresh["pid"], "busy_preserved": True,
                                "stale_idle_refused": True, "build_completed": True, "idle_stop": stopped,
                                "original_pids_preserved": list(original)})
                (base / "results.json").write_text(json.dumps(results, indent=2))


if __name__ == "__main__":
    unittest.main()
