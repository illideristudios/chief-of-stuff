"""The model port and its `claude -p --output-format json` adapter (design 7, 7a; ledger E5, E12, E13, E14, E17, E20).

Every call runs `evals/fixtures/fake_claude.py`; no test here reaches a real model.
"""

import json
import os
import signal
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
FAKE = ROOT / "evals" / "fixtures" / "fake_claude.py"
SID = "6f1c2a3e-8a4b-4c1d-9e2f-0a1b2c3d4e5f"
sys.path.insert(0, str(ROOT / "scripts"))
import claude_adapter  # noqa: E402
import model_port  # noqa: E402
from model_port import Failure, Reply, Request  # noqa: E402


class QuotaDetectorTest(unittest.TestCase):
    def test_a_standalone_429_with_resource_exhausted_or_quota_is_a_quota_stop_with_its_reset_text(self):
        self.assertEqual(model_port.quota_reset("Error: code 429 RESOURCE_EXHAUSTED: resets in about four hours."),
                         "resets in about four hours.")
        self.assertEqual(model_port.quota_reset("status=429 quota exceeded"), "")

    def test_anything_else_is_no_quota_stop(self):
        for text in ("", "RESOURCE_EXHAUSTED without the code", "error 4290 quota", "429 too many requests"):
            with self.subTest(text=text):
                self.assertIsNone(model_port.quota_reset(text))


class RotationTest(unittest.TestCase):
    """E20: Claude models only; a quota stop falls back to the next model, anything else is the answer."""

    def calls(self, outcomes):
        seen = []

        def call(request):
            seen.append((request.model, request.effort))
            return outcomes[request.model]
        return seen, call

    def test_a_quota_stop_moves_to_the_next_model_with_its_effort(self):
        ok = Reply(text="done", data=None, session_id="s", cost_usd=0.0, num_turns=1)
        seen, call = self.calls({"opus": Failure("quota", "q"), "sonnet": ok})
        got = model_port.rotate(call, Request(prompt="p"), [("opus", "high"), ("sonnet", "medium")])
        self.assertIs(got, ok)
        self.assertEqual(seen, [("opus", "high"), ("sonnet", "medium")])

    def test_any_other_failure_stops_the_rotation(self):
        seen, call = self.calls({"opus": Failure("error", "bad"), "sonnet": Failure("quota", "q")})
        self.assertEqual(model_port.rotate(call, Request(prompt="p"), [("opus", ""), ("sonnet", "")]).kind, "error")
        self.assertEqual(seen, [("opus", "")])

    def test_every_model_out_of_quota_is_the_last_quota_stop_never_a_failure(self):
        # #422: a quota stop is never reported as a worker failure.
        seen, call = self.calls({"opus": Failure("quota", "q1"), "sonnet": Failure("quota", "q2")})
        got = model_port.rotate(call, Request(prompt="p"), [("opus", ""), ("sonnet", "")])
        self.assertEqual((got.kind, got.message, got.retryable), ("quota", "q2", True))


class ArgvTest(unittest.TestCase):
    def test_a_judgement_call_is_stateless_tool_free_and_in_safe_mode(self):
        self.assertEqual(claude_adapter.argv(Request(prompt="-starts with a dash"), "/bin/claude", []),
                         ["/bin/claude", "-p", "--output-format", "json", "--no-session-persistence",
                          "--tools", "", "--safe-mode", "--", "-starts with a dash"])

    def test_model_effort_schema_session_and_launch_profile(self):
        request = Request(prompt="p", model="opus", effort="high", schema={"type": "object"}, session_id=SID,
                          plugin_dirs=("/guard",))
        self.assertEqual(claude_adapter.argv(request, "/bin/claude-as", ["run", "secondary"]),
                         ["/bin/claude-as", "run", "secondary", "-p", "--output-format", "json",
                          "--model", "opus", "--effort", "high", "--json-schema", '{"type": "object"}',
                          "--session-id", SID, "--plugin-dir", "/guard", "--", "p"])

    def test_a_resume_names_the_session(self):
        got = claude_adapter.argv(Request(prompt="p", session_id="u1", resume=True), "/bin/claude", [])
        self.assertIn(["--resume", "u1"], [got[i:i + 2] for i in range(len(got))])
        self.assertNotIn("--session-id", got)


class StartTest(unittest.TestCase):
    def test_spawn_record_is_atomic_and_precedes_started_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp)
            (run / "command.json").write_text(json.dumps({"argv": ["fake"], "cwd": tmp, "session_id": SID}))
            process = mock.Mock(pid=4242)
            def wait():
                record = json.loads((run / "spawn.json").read_text())
                self.assertEqual(record["pid"], 4242)
                self.assertGreater(record["pgid"], 0)
                self.assertGreater(record["start_time"], 0)
                return 0
            process.wait.side_effect = wait
            write = claude_adapter._write
            def record(name_dir, name, data):
                if name == "started.json":
                    self.assertTrue((run / "spawn.json").is_file())
                write(name_dir, name, data)
            with mock.patch.object(claude_adapter.subprocess, "Popen", return_value=process), \
                 mock.patch.object(claude_adapter, "_write", side_effect=record):
                claude_adapter._worker(run)
            self.assertTrue((run / "spawn.json").is_file())
            self.assertTrue((run / "started.json").is_file())

    def test_runner_spawn_failure_removes_launch_evidence_and_reraises(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            error = OSError("runner spawn failed")
            with mock.patch.object(claude_adapter, "command", return_value=["/fake"]), \
                 mock.patch.object(claude_adapter.subprocess, "Popen", side_effect=error):
                with self.assertRaises(OSError) as raised:
                    claude_adapter.start(Request(prompt="p", session_id=SID), run_dir)
            self.assertIs(raised.exception, error)
            self.assertFalse((run_dir / "command.json").exists())
            self.assertFalse((run_dir / "runner.stderr").exists())


class CancelTest(unittest.TestCase):
    def test_cancel_kills_a_child_that_outlives_its_group_leader(self):
        import os
        import sys
        import time
        with tempfile.TemporaryDirectory() as tmp:
            ready = Path(tmp) / "ready"
            child = "import signal,time,pathlib; signal.signal(signal.SIGTERM, signal.SIG_IGN); pathlib.Path(" + repr(str(ready)) + ").touch(); time.sleep(60)"
            leader = "import subprocess,sys,time; subprocess.Popen([sys.executable, '-c', " + repr(child) + "]); time.sleep(60)"
            process = subprocess.Popen([sys.executable, "-c", leader], start_new_session=True)
            try:
                deadline = time.monotonic() + 5
                while not ready.exists() and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertTrue(ready.exists())
                claude_adapter.cancel(process)
                self.assertIsNotNone(process.poll())
                self.assertFalse(claude_adapter.group_alive(process.pid))
            finally:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait()

    def test_exited_runner_does_not_hide_a_surviving_group(self):
        process = mock.Mock(pid=4242)
        process.wait.return_value = 0
        with mock.patch.object(claude_adapter.os, "killpg") as killpg, \
             mock.patch.object(claude_adapter, "group_alive", side_effect=[True, False]):
            claude_adapter.cancel(process)
        self.assertIn(mock.call(4242, signal.SIGKILL), killpg.call_args_list)

    def test_a_host_that_ignores_sigterm_is_killed_and_reaped(self):
        process = mock.Mock(pid=4242)
        process.wait.side_effect = [subprocess.TimeoutExpired("host", 5), 0]
        with mock.patch.object(claude_adapter.os, "killpg") as killpg, \
             mock.patch.object(claude_adapter, "group_alive", return_value=False):
            claude_adapter.cancel(process)
        self.assertEqual([c.args for c in killpg.call_args_list],
                         [(4242, signal.SIGTERM), (4242, signal.SIGKILL)])
        self.assertEqual(process.wait.call_count, 2)

    def test_a_host_that_survives_sigkill_is_not_reported_cancelled(self):
        process = mock.Mock(pid=4242)
        process.wait.side_effect = subprocess.TimeoutExpired("host", 5)
        with mock.patch.object(claude_adapter.os, "killpg"):
            with self.assertRaises(RuntimeError):
                claude_adapter.cancel(process)

    def test_a_host_that_exits_on_sigterm_is_not_killed(self):
        process = mock.Mock(pid=4242)
        process.wait.return_value = 0
        with mock.patch.object(claude_adapter.os, "killpg") as killpg, \
             mock.patch.object(claude_adapter, "group_alive", return_value=False):
            claude_adapter.cancel(process)
        killpg.assert_called_once_with(4242, signal.SIGTERM)


class ClassifyTest(unittest.TestCase):
    """The spike: judge by exit code, `is_error` and parseability together, never by `subtype`."""

    def envelope(self, **fields):
        return json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": "hi",
                           "session_id": "u1", "num_turns": 1, "total_cost_usd": 0.01, **fields})

    def test_exit_zero_parseable_and_not_an_error_is_a_reply(self):
        got = claude_adapter.classify(0, self.envelope(structured_output={"a": 1}), "")
        self.assertEqual(got, Reply(text="hi", data={"a": 1}, session_id="u1", cost_usd=0.01, num_turns=1))

    def test_an_error_flag_with_subtype_success_is_an_error(self):
        got = claude_adapter.classify(1, self.envelope(is_error=True, result="model not found"), "")
        self.assertEqual((got.kind, got.message, got.retryable), ("error", "model not found", False))

    def test_exit_zero_with_the_error_flag_or_a_nonzero_exit_without_it_is_still_an_error(self):
        self.assertEqual(claude_adapter.classify(0, self.envelope(is_error=True), "").kind, "error")
        self.assertEqual(claude_adapter.classify(2, self.envelope(), "").kind, "error")

    def test_unparseable_or_empty_stdout_is_a_retryable_unknown_carrying_stderr(self):
        for out in ("", "not json", "[1]"):
            with self.subTest(out=out):
                got = claude_adapter.classify(0 if out else 1, out, "boom")
                self.assertEqual((got.kind, got.retryable), ("unknown", True))
                self.assertIn("boom", got.message)

    def test_the_observed_failures_have_their_own_kinds(self):
        self.assertEqual(claude_adapter.classify(1, "", "No conversation found with session ID: u9").kind,
                         "session_missing")
        self.assertEqual(claude_adapter.classify(1, self.envelope(is_error=True, result="Not logged in"), "").kind,
                         "auth")
        self.assertEqual(claude_adapter.classify(1, self.envelope(is_error=True, subtype="error_max_budget_usd"), "").kind,
                         "budget")

    def test_a_quota_stop_in_stderr_or_the_envelope_is_quota_never_an_error(self):
        # Ledger E5, #422.
        stderr = claude_adapter.classify(1, "", "API Error: 429 RESOURCE_EXHAUSTED, resets at 5pm.")
        envelope = claude_adapter.classify(1, self.envelope(is_error=True, result="429: quota exceeded"), "")
        for got in (stderr, envelope):
            self.assertEqual((got.kind, got.retryable), ("quota", True))
        self.assertEqual(stderr.message, "resets at 5pm.")


class CallTest(unittest.TestCase):
    """End to end through the login shell against the fake CLI."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.cli = self.dir / "claude"  # a Python shim: no shell scripts
        self.cli.write_text(f"#!{sys.executable}\nimport runpy\nrunpy.run_path({str(FAKE)!r}, run_name='__main__')\n")
        self.cli.chmod(0o755)
        patches = (mock.patch.dict(os.environ, {"SHELL": "/bin/sh"}),
                   mock.patch.object(claude_adapter, "resolve", side_effect=self.resolve))
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        self.resolved = []

    def resolve(self, program):
        self.resolved.append(program)
        return str(self.cli)

    def call(self, prompt, **fields):
        return claude_adapter.call(Request(prompt=prompt, cwd=str(self.dir), **fields))

    def test_a_reply_and_its_structured_output(self):
        got = self.call("hello", schema={"type": "object"})
        self.assertEqual((got.text, got.data), ("reply: hello", {"answer": "hello"}))
        self.assertEqual(self.resolved, ["claude"])

    def test_a_session_minted_then_resumed_recalls_the_first_call(self):
        self.call("the code word is heron", session_id=SID)
        self.assertIn("heron", self.call("what was it?", session_id=SID, resume=True).text)
        missing = self.call("again", session_id=str(uuid.uuid4()), resume=True)
        self.assertEqual(missing.kind, "session_missing")

    def test_a_new_session_needs_a_uuid_and_is_refused_before_launch(self):
        # The CLI refuses a non-UUID --session-id; the port says so without spending a launch.
        got = self.call("p", session_id="u1")
        self.assertEqual((got.kind, got.retryable), ("error", False))
        self.assertEqual(self.resolved, [])

    def test_a_working_directory_gone_at_launch_is_a_retryable_unknown(self):
        got = claude_adapter.call(Request(prompt="p", cwd=str(self.dir / "removed")))
        self.assertEqual((got.kind, got.retryable), ("unknown", True))

    def test_a_quota_stop_is_quota(self):
        self.assertEqual(self.call("QUOTA").kind, "quota")

    def test_a_call_past_its_timeout_is_a_retryable_unknown(self):
        got = self.call("SLEEP", timeout_s=1)
        self.assertEqual((got.kind, got.retryable), ("unknown", True))

    def test_a_profile_launches_through_claude_as_and_reports_it_missing_never_falling_back(self):
        # Ledger E12: no silent fallback to a bare `claude`.
        self.assertEqual(claude_adapter.call(Request(prompt="p", cwd=str(self.dir)), claude_as_profile="secondary").text,
                         "reply: p")
        self.assertEqual(self.resolved, ["claude-as"])
        self.resolved.clear()
        with mock.patch.object(claude_adapter, "resolve", side_effect=lambda p: self.resolved.append(p)):
            got = claude_adapter.call(Request(prompt="p", cwd=str(self.dir)), claude_as_profile="secondary")
        self.assertEqual((got.kind, self.resolved), ("missing_program", ["claude-as"]))
        self.assertIn("claude_as_profile", got.message)


if __name__ == "__main__":
    unittest.main()
