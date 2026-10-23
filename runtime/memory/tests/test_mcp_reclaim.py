"""Cooperative broker tests. Every launched process is a disposable fake fixture."""
import contextlib
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest import mock

from scripts import mcp_reclaim as reclaim


def status(pid=424242, **updates):
    result = {
        "protocol": reclaim.PROTOCOL, "instance_id": "a" * 32, "pid": pid, "uid": os.getuid(),
        "draining": False, "consumer_count": 0, "pending_requests": 0, "task_count": 0,
        "active_tool_calls": 0, "knowledge_ok": True, "natural_close_supported": True,
    }
    result.update(updates)
    return result


class AdapterSafetyTests(unittest.TestCase):
    def setUp(self):
        self.birth = {"pid": 424242, "start": "synthetic:1", "uid": os.getuid(), "status": 2}
        self.row = {"pid": 424242, "ppid": 1, "uid": os.getuid(),
                    "command": f"node {reclaim.BROKER_SOURCE} serve --endpoint unix:/synthetic/broker.sock"}
        self.candidate = {"id": "mcp-fixture", "kind": "mcp", "pid": 424242, "start": "synthetic:1",
                          "endpoint": "unix:/synthetic/broker.sock", "eligible": True,
                          "tracked_processes": [{"pid": 424242, "start": "synthetic:1"}],
                          "evidence": {"instance_id": "a" * 32}}
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(mock.patch.object(reclaim, "_processes", return_value=[self.row]))
        self.stack.enter_context(mock.patch.object(reclaim, "_identity", return_value=self.birth))
        self.capability = self.stack.enter_context(mock.patch.object(reclaim, "_capability", return_value={"instance_id": "a" * 32}))
        self.rpc = self.stack.enter_context(mock.patch.object(reclaim, "_rpc", return_value=status()))
        self.kill = self.stack.enter_context(mock.patch.object(os, "kill", side_effect=AssertionError("adapter cannot signal any process")))

    def test_old_broker_without_protocol_is_preserved(self):
        self.rpc.return_value = None
        result = reclaim.inspect()
        self.assertFalse(result[0]["eligible"])
        self.assertEqual(result[0]["reason"], "cooperative_protocol_unavailable")
        self.rpc.assert_called_once_with("unix:/synthetic/broker.sock", "broker/memoryStatus")

    def test_old_instance_without_capability_file_receives_no_rpc(self):
        self.capability.return_value = None
        result = reclaim.inspect()
        self.assertFalse(result[0]["eligible"])
        self.rpc.assert_not_called()

    def test_fingerprint_is_stable_across_time_and_changes_with_activity_or_identity(self):
        first = reclaim.inspect(now=1000)[0]
        self.assertEqual(first["fingerprint"], reclaim.inspect(now=1060)[0]["fingerprint"])
        self.rpc.return_value = status(active_tool_calls=1)
        self.assertNotEqual(first["fingerprint"], reclaim.inspect(now=1120)[0]["fingerprint"])
        self.rpc.return_value = status()
        with mock.patch.object(reclaim, "_identity", return_value=dict(self.birth, start="reused:2")):
            self.assertNotEqual(first["fingerprint"], reclaim.inspect(now=1180)[0]["fingerprint"])

    def test_only_authoritative_all_zero_counters_are_eligible(self):
        self.assertTrue(reclaim.inspect()[0]["eligible"])
        for field in ("consumer_count", "pending_requests", "task_count", "active_tool_calls"):
            with self.subTest(field=field):
                self.rpc.return_value = status(**{field: 1})
                self.assertFalse(reclaim.inspect()[0]["eligible"])

    def test_unknown_tool_counter_or_knowledge_does_not_mean_zero(self):
        for updates in ({"active_tool_calls": None}, {"active_tool_calls": False}, {"knowledge_ok": False}):
            with self.subTest(updates=updates):
                self.rpc.return_value = status(**updates)
                self.assertFalse(reclaim.inspect()[0]["eligible"])

    def test_changed_pid_identity_prevents_any_write(self):
        with mock.patch.object(reclaim, "_identity", return_value=dict(self.birth, start="reused:2")):
            result = reclaim.reclaim(self.candidate)
        self.assertFalse(result["stopped"])
        self.rpc.assert_not_called()

    def test_new_consumer_at_recheck_prevents_drain(self):
        self.rpc.return_value = status(consumer_count=1)
        result = reclaim.reclaim(self.candidate)
        self.assertFalse(result["stopped"])
        self.rpc.assert_called_once_with(self.candidate["endpoint"], "broker/memoryStatus")

    def test_new_instance_at_same_endpoint_prevents_drain(self):
        self.rpc.return_value = status(instance_id="b" * 32)
        result = reclaim.reclaim(self.candidate)
        self.assertFalse(result["stopped"])
        self.assertEqual(self.rpc.call_count, 1)

    def test_ambiguous_drain_timeout_is_pending(self):
        self.rpc.side_effect = [status(), TimeoutError("synthetic timeout after send")]
        result = reclaim.reclaim(self.candidate)
        self.assertFalse(result["stopped"])
        self.assertTrue(result["pending"])
        self.assertTrue(result["requested"])
        self.assertEqual(self.rpc.call_count, 2)

    def test_accepted_response_is_not_evidence_of_process_exit(self):
        self.rpc.side_effect = [status(), status(pending=True, app_server_exited=True, reason="normal_stdin_exit", draining=True)]
        with mock.patch.object(reclaim, "_exited", return_value=False):
            result = reclaim.reclaim(self.candidate)
        self.assertFalse(result["stopped"])
        self.assertTrue(result["pending"])

    def test_malformed_response_after_write_is_pending_and_never_retryable(self):
        self.rpc.side_effect = [status(), {}]
        result = reclaim.reclaim(self.candidate)
        self.assertTrue(result["pending"])
        self.assertEqual(result["reason"], "drain_response_unknown_reconcile_only")

    def test_explicit_busy_response_is_preserved_without_claiming_exit(self):
        self.rpc.side_effect = [status(), status(reason="busy_or_unknown_preserved", consumer_count=1)]
        result = reclaim.reclaim(self.candidate)
        self.assertFalse(result["pending"])
        self.assertFalse(result["stopped"])

    def test_reconciliation_is_read_only_and_waits_for_all_original_children(self):
        candidate = dict(self.candidate, tracked_processes=[
            {"pid": 424242, "start": "synthetic:1"}, {"pid": 424243, "start": "mcp:1"}])
        with mock.patch.object(reclaim, "_exited", side_effect=[True, False]):
            self.assertTrue(reclaim.reconcile(candidate)["pending"])
        with mock.patch.object(reclaim, "_exited", return_value=True):
            self.assertTrue(reclaim.reconcile(candidate)["stopped"])
        self.rpc.assert_not_called()
        self.kill.assert_not_called()

    def test_process_query_error_is_not_proof_of_exit(self):
        with mock.patch.object(reclaim, "_identity", return_value=None), \
                mock.patch.object(reclaim.subprocess, "run", return_value=SimpleNamespace(returncode=1, stdout="", stderr="synthetic query failure")):
            self.assertFalse(reclaim._exited(self.candidate))


FAKE_CODEX = r'''#!PYTHON
import json, os, pathlib, signal, subprocess, sys, time
fixture_dir = pathlib.Path(os.environ["DEV_MEMORY_FIXTURE_DIR"])
def got_term(*args):
    (fixture_dir / "received-sigterm").touch()
    raise SystemExit(143)
signal.signal(signal.SIGTERM, got_term)
# This fake MCP only reads its owned stdio until its fake app-server closes it.
mcp = subprocess.Popen([sys.executable, "-c", "import sys; sys.stdin.read()"], stdin=subprocess.PIPE)
def emit(value):
    print(json.dumps(value), flush=True)
for line in sys.stdin:
    message = json.loads(line)
    method = message.get("method")
    ident = message.get("id")
    if ident is None:
        continue
    if method == "fixture/hold":
        continue
    if method == "turn/start":
        if (fixture_dir / "delayed-start").exists():
            emit({"id":ident,"result":{"turn":{"id":"fixture-turn"}}})
            while not (fixture_dir / "allow-start-notification").exists():
                time.sleep(0.01)
            emit({"method":"turn/started", "params":{"threadId":"fixture-thread", "turn":{"id":"fixture-turn"}}})
            continue
        emit({"method":"turn/started", "params":{"threadId":"fixture-thread", "turn":{"id":"fixture-turn"}}})
        emit({"method":"item/started", "params":{"threadId":"fixture-thread", "item":{"type":"mcpToolCall", "id":"fixture-tool"}}})
        emit({"id":ident,"result":{"turn":{"id":"fixture-turn"}}})
        continue
    if method == "turn/interrupt":
        emit({"method":"item/completed", "params":{"threadId":"fixture-thread", "item":{"type":"mcpToolCall", "id":"fixture-tool"}}})
        emit({"method":"turn/completed", "params":{"threadId":"fixture-thread", "turn":{"id":"fixture-turn"}}})
    if method == "fixture/finishTask":
        emit({"method":"turn/completed", "params":{"threadId":"fixture-thread", "turn":{"id":"fixture-turn"}}})
    if method in ("fixture/startTool", "fixture/finishTool", "fixture/malformedTool"):
        item = {"type":"mcpToolCall", "id":"fixture-tool"}
        if method == "fixture/malformedTool":
            del item["id"]
        emit({"method":"item/completed" if method == "fixture/finishTool" else "item/started",
              "params":{"threadId":"fixture-thread", "item":item}})
    emit({"id":ident,"result":{}})
mcp.stdin.close()
mcp.wait(timeout=3)
if (fixture_dir / "invalid-json-on-eof").exists():
    print("synthetic invalid JSON", flush=True)
(fixture_dir / "eof-seen").touch()
while (fixture_dir / "hold-eof").exists() and not (fixture_dir / "allow-exit").exists():
    time.sleep(0.02)
'''


class DisposableBroker:
    def __init__(self):
        self.temp = tempfile.TemporaryDirectory(prefix="dmcp-")
        self.directory = Path(self.temp.name)
        executable = self.directory / "codex"
        executable.write_text(FAKE_CODEX.replace("#!PYTHON", "#!" + sys.executable), encoding="utf-8")
        executable.chmod(0o700)
        self.endpoint = "unix:" + str(self.directory / "b.sock")
        env = dict(os.environ, PATH=str(self.directory) + os.pathsep + os.environ["PATH"], DEV_MEMORY_FIXTURE_DIR=str(self.directory))
        self.proc = subprocess.Popen([shutil.which("node"), str(reclaim.BROKER_SOURCE), "serve",
                                     "--endpoint", self.endpoint, "--cwd", str(self.directory)],
                                    env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        for _ in range(100):
            if (self.directory / "b.sock").exists() and (self.directory / "b.sock.memory.json").exists():
                return
            if self.proc.poll() is not None:
                raise AssertionError(self.proc.stderr.read().decode())
            time.sleep(0.02)
        raise AssertionError("disposable broker did not start")

    def rpc(self, method, params=None):
        return reclaim._rpc(self.endpoint, method, params)

    def consumer(self):
        channel = socket.socket(socket.AF_UNIX)
        channel.settimeout(3)
        channel.connect(self.endpoint[5:])
        return channel

    def close(self):
        (self.directory / "allow-exit").touch()
        (self.directory / "allow-start-notification").touch()
        if self.proc.poll() is None:
            # This endpoint belongs only to this test's fake broker and fake MCP.
            with contextlib.suppress(OSError, ValueError):
                self.rpc("broker/shutdown")
            self.proc.wait(timeout=5)
        for stream in (self.proc.stdout, self.proc.stderr):
            stream.close()
        self.temp.cleanup()


@unittest.skipUnless(os.environ.get("DEV_MEMORY_BROKER_INTEGRATION") == "1" and reclaim.BROKER_SOURCE.is_file(), "optional external compatible broker integration")
class BrokerProtocolFixtureTests(unittest.TestCase):
    def setUp(self):
        self.broker = DisposableBroker()
        self.addCleanup(self.broker.close)

    def request(self, channel, method):
        channel.sendall(json.dumps({"id": 7, "method": method, "params": {"threadId": "fixture-thread"}}).encode() + b"\n")
        # Notifications may precede the requested JSON-RPC response.
        with channel.makefile("rb") as stream:
            while True:
                response = json.loads(stream.readline())
                if response.get("id") == 7:
                    return response

    def drain(self, instance_id):
        return self.broker.rpc("broker/drainIfIdle", {"expected_instance_id": instance_id})

    def test_connected_consumer_blocks_atomic_drain_and_remains_usable(self):
        with self.broker.consumer() as consumer:
            self.request(consumer, "initialize")
            snapshot = self.broker.rpc("broker/memoryStatus")
            self.assertEqual(snapshot["consumer_count"], 1)
            result = self.drain(snapshot["instance_id"])
            self.assertEqual(result["reason"], "busy_or_unknown_preserved")
            self.assertFalse(result["draining"])
            self.assertIn("result", self.request(consumer, "fixture/ping"))

    def test_disconnected_client_does_not_erase_its_running_task(self):
        consumer = self.broker.consumer()
        self.request(consumer, "turn/start")
        consumer.close()
        time.sleep(0.03)
        snapshot = self.broker.rpc("broker/memoryStatus")
        self.assertEqual(snapshot["consumer_count"], 0)
        self.assertEqual(snapshot["task_count"], 1)
        result = self.drain(snapshot["instance_id"])
        self.assertEqual(result["reason"], "busy_or_unknown_preserved")

    def test_response_before_delayed_turn_started_notification_is_already_reserved(self):
        (self.broker.directory / "delayed-start").touch()
        with self.broker.consumer() as consumer:
            self.request(consumer, "turn/start")
        time.sleep(0.03)
        snapshot = self.broker.rpc("broker/memoryStatus")
        self.assertEqual(snapshot["consumer_count"], 0)
        self.assertEqual(snapshot["pending_requests"], 0)
        self.assertEqual(snapshot["task_count"], 1)
        self.assertEqual(self.drain(snapshot["instance_id"])["reason"], "busy_or_unknown_preserved")
        (self.broker.directory / "allow-start-notification").touch()

    def test_unknown_async_start_method_is_never_assumed_idle(self):
        with self.broker.consumer() as consumer:
            self.request(consumer, "futureTask/start")
        snapshot = self.broker.rpc("broker/memoryStatus")
        self.assertFalse(snapshot["knowledge_ok"])
        self.assertEqual(self.drain(snapshot["instance_id"])["reason"], "busy_or_unknown_preserved")

    def test_authoritative_turn_and_tool_completion_make_used_broker_idle(self):
        with self.broker.consumer() as consumer:
            self.request(consumer, "turn/start")
            self.request(consumer, "turn/interrupt")
        time.sleep(0.03)
        snapshot = self.broker.rpc("broker/memoryStatus")
        self.assertTrue(snapshot["knowledge_ok"])
        self.assertEqual(snapshot["task_count"], 0)
        self.assertEqual(snapshot["active_tool_calls"], 0)
        self.assertEqual(snapshot["consumer_count"], 0)
        result = self.drain(snapshot["instance_id"])
        self.assertTrue(result["app_server_exited"])
        self.assertEqual(self.broker.proc.wait(timeout=5), 0)

    def test_active_mcp_tool_remains_counted_after_client_disconnect(self):
        consumer = self.broker.consumer()
        self.request(consumer, "fixture/startTool")
        consumer.close()
        time.sleep(0.03)
        snapshot = self.broker.rpc("broker/memoryStatus")
        self.assertEqual(snapshot["consumer_count"], 0)
        self.assertEqual(snapshot["active_tool_calls"], 1)
        self.assertEqual(self.drain(snapshot["instance_id"])["reason"], "busy_or_unknown_preserved")

    def test_unknown_tool_identity_is_fail_closed(self):
        with self.broker.consumer() as consumer:
            self.request(consumer, "fixture/malformedTool")
        snapshot = self.broker.rpc("broker/memoryStatus")
        self.assertFalse(snapshot["knowledge_ok"])
        self.assertEqual(self.drain(snapshot["instance_id"])["reason"], "busy_or_unknown_preserved")

    def test_pending_request_blocks_drain_even_when_client_disconnects(self):
        with self.broker.consumer() as consumer:
            consumer.sendall(b'{"id":7,"method":"fixture/hold","params":{}}\n')
            time.sleep(0.03)
        snapshot = self.broker.rpc("broker/memoryStatus")
        self.assertEqual(snapshot["pending_requests"], 1)
        self.assertEqual(self.drain(snapshot["instance_id"])["reason"], "busy_or_unknown_preserved")

    def test_wrong_instance_never_closes_broker(self):
        result = self.drain("f" * 32)
        self.assertEqual(result["reason"], "instance_changed")
        self.assertIsNone(self.broker.proc.poll())

    def test_idle_drain_closes_own_app_server_and_fake_mcp_normally(self):
        snapshot = self.broker.rpc("broker/memoryStatus")
        rows = reclaim._processes()
        tracked = reclaim._tracked_processes(self.broker.proc.pid, rows)
        self.assertGreaterEqual(len(tracked), 3)  # broker, fake app-server, fake MCP
        result = self.drain(snapshot["instance_id"])
        self.assertTrue(result["app_server_exited"])
        self.assertTrue(result["pending"])
        self.assertFalse(result["stopped"])
        self.assertEqual(self.broker.proc.wait(timeout=5), 0)
        candidate = {"tracked_processes": tracked}
        self.assertTrue(reclaim.reconcile(candidate)["stopped"])

    def test_drain_admission_barrier_and_eof_timeout_never_send_a_signal(self):
        (self.broker.directory / "hold-eof").touch()
        snapshot = self.broker.rpc("broker/memoryStatus")
        result = {}
        failure = []

        def request_drain():
            try:
                result.update(reclaim._rpc(self.broker.endpoint, "broker/drainIfIdle",
                                          {"expected_instance_id": snapshot["instance_id"]}, timeout=11))
            except BaseException as error:
                failure.append(error)

        worker = threading.Thread(target=request_drain)
        worker.start()
        for _ in range(100):
            if (self.broker.directory / "eof-seen").exists():
                break
            time.sleep(0.02)
        self.assertTrue((self.broker.directory / "eof-seen").exists())
        # Once EOF is initiated, a new consumer cannot enter the drained broker.
        with self.broker.consumer() as newcomer:
            self.assertEqual(newcomer.recv(1024), b"")
        worker.join(timeout=11)
        self.assertFalse(worker.is_alive())
        self.assertEqual(failure, [])
        self.assertTrue(result["pending"])
        self.assertFalse(result["app_server_exited"])
        self.assertEqual(result["reason"], "normal_exit_timeout_preserved")
        self.assertFalse((self.broker.directory / "received-sigterm").exists())
        self.assertIsNone(self.broker.proc.poll())
        # A later natural child exit completes this same drain, with no resend.
        (self.broker.directory / "allow-exit").touch()
        self.assertEqual(self.broker.proc.wait(timeout=5), 0)

    def test_protocol_parse_failure_does_not_count_as_owned_process_exit(self):
        (self.broker.directory / "hold-eof").touch()
        (self.broker.directory / "invalid-json-on-eof").touch()
        snapshot = self.broker.rpc("broker/memoryStatus")
        result = reclaim._rpc(self.broker.endpoint, "broker/drainIfIdle",
                              {"expected_instance_id": snapshot["instance_id"]}, timeout=11)
        self.assertFalse(result["app_server_exited"])
        self.assertFalse(result["knowledge_ok"])
        self.assertTrue(result["draining"])
        self.assertEqual(result["reason"], "normal_exit_timeout_preserved")
        self.assertIsNone(self.broker.proc.poll())
        self.assertFalse((self.broker.directory / "received-sigterm").exists())
        with self.broker.consumer() as newcomer:
            self.assertEqual(newcomer.recv(1024), b"")
        (self.broker.directory / "allow-exit").touch()
        self.assertEqual(self.broker.proc.wait(timeout=5), 0)


if __name__ == "__main__":
    unittest.main()
