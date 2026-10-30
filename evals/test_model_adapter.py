"""The model port and its `claude -p --output-format json` adapter (design 7, 7a; ledger E5, E12, E13, E14, E17, E20).

Every call runs `evals/fixtures/fake_claude.py`; no test here reaches a real model.
"""

import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
FAKE = ROOT / "evals" / "fixtures" / "fake_claude.py"
SID = "6f1c2a3e-8a4b-4c1d-9e2f-0a1b2c3d4e5f"
sys.path.insert(0, str(ROOT / "scripts"))
import claude_adapter  # noqa: E402
import limits_probe  # noqa: E402
import model_port  # noqa: E402
import session_logs  # noqa: E402
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


class AvailabilityTest(unittest.TestCase):
    def test_availability_uses_the_same_login_resolution_as_command(self):
        with mock.patch.object(claude_adapter, "resolve", return_value="/login/claude") as resolve:
            self.assertEqual(claude_adapter.available(), "/login/claude")
        resolve.assert_called_once_with("claude")
        with mock.patch.object(claude_adapter, "resolve", return_value="/login/claude"), \
             mock.patch.object(claude_adapter, "available", wraps=claude_adapter.available) as available:
            cmd = claude_adapter.command(Request(prompt="p"))
            self.assertIn("/login/claude", cmd)
            available.assert_called_once_with()
        with mock.patch.object(claude_adapter, "resolve", return_value=None):
            self.assertIsNone(claude_adapter.available())
            with self.assertRaises(claude_adapter.ShellError):
                claude_adapter.command(Request(prompt="p"))


class EventsTest(unittest.TestCase):
    def test_startup_noise_and_events_after_result_do_not_hide_a_reply(self):
        raw = 'shell startup\n' + json.dumps({'type': 'result', 'is_error': False, 'result': 'done'})
        raw += '\n' + json.dumps({'type': 'system', 'subtype': 'task_notification'}) + '\nlogout noise\n'
        got = claude_adapter.classify(0, raw, 'warning', keep_events=True)
        self.assertIsInstance(got, Reply)
        self.assertEqual(got.text, 'done')
        self.assertEqual(len(got.events), 2)

    def test_events_are_opt_in_and_preserve_tool_calls_and_denials(self):
        events = [
            {"type": "system", "subtype": "init", "model": "sonnet"},
            {"type": "assistant", "message": {"content": [
                {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "date"}}]}},
            {"type": "result", "is_error": False, "result": "done", "session_id": SID,
             "structured_output": {"answer": 1}, "num_turns": 2, "total_cost_usd": 0.01,
             "permission_denials": [{"tool_use_id": "t1"}]},
        ]
        request = Request(prompt="-hello", keep_events=True)
        cmd = claude_adapter.argv(request, "/host", [])
        self.assertEqual(cmd[cmd.index("--output-format") + 1], "stream-json")
        self.assertIn("--verbose", cmd)
        raw = "\n".join(json.dumps(e) for e in events)
        got = claude_adapter.classify(0, raw, "", keep_events=True)
        self.assertEqual(got.events, events)
        self.assertEqual((got.text, got.data, got.session_id, got.num_turns, got.cost_usd),
                         ("done", {"answer": 1}, SID, 2, 0.01))
        self.assertIsNone(claude_adapter.classify(0, json.dumps(events[-1]), "").events)

    def test_stream_failures_use_the_same_failure_boundary(self):
        for raw, code, stderr, kind in (
            ('{"type":"system"}', 0, "", "unknown"),
            ('not json', 0, "", "unknown"),
            ('{"type":"result","is_error":true,"result":"Not logged in"}', 0, "", "auth"),
            ('{"type":"result","is_error":false}', 1, "429 quota exceeded", "quota"),
        ):
            with self.subTest(kind=kind):
                self.assertEqual(claude_adapter.classify(code, raw, stderr, keep_events=True).kind, kind)


class DefaultClaudeLoginTest(unittest.TestCase):
    def test_default_login_does_not_set_config_override_but_records_transcript_directory(self):
        environment = dict(os.environ)
        environment.pop("CLAUDE_CONFIG_DIR", None)
        with mock.patch.dict(os.environ, environment, clear=True), \
             mock.patch.object(claude_adapter, "command", return_value=["claude", "--", "p"]), \
             mock.patch.object(claude_adapter, "spawn") as spawn:
            claude_adapter.start(Request(prompt="p", session_id=SID), Path("/tmp/run"))
        self.assertNotIn("CLAUDE_CONFIG_DIR", spawn.call_args.args[3])
        self.assertEqual(spawn.call_args.kwargs["config_dir"], str(Path(environment["HOME"]) / ".claude"))


class StartTest(unittest.TestCase):
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
    def test_cancellation_waits_until_killed_leader_is_recorded_as_reaped(self):
        process = mock.Mock(pid=4242, returncode=None)
        seen = []
        previous = signal.signal(signal.SIGTERM, lambda *_: seen.append(process.returncode))
        def reap(*, timeout):
            os.kill(os.getpid(), signal.SIGTERM)
            process.returncode = -signal.SIGKILL
        process.wait.side_effect = reap
        try:
            with mock.patch.object(claude_adapter.os, "killpg"):
                claude_adapter._stop_after_write_failure(process)
        finally:
            signal.signal(signal.SIGTERM, previous)
        self.assertEqual(seen, [-signal.SIGKILL])

    def test_write_failure_does_not_claim_cleanup_if_group_signal_is_denied(self):
        process = mock.Mock(pid=4242, returncode=None)
        with mock.patch.object(claude_adapter.os, "killpg", side_effect=PermissionError):
            with self.assertRaises(PermissionError):
                claude_adapter._stop_after_write_failure(process)
        process.wait.assert_not_called()

    def test_write_failure_does_not_signal_a_reaped_hosts_reusable_group_id(self):
        process = mock.Mock(pid=4242, returncode=0)
        with mock.patch.object(claude_adapter.os, "killpg") as killpg:
            claude_adapter._stop_after_write_failure(process)
        killpg.assert_not_called()

    def test_serve_cleanup_signals_once_and_never_claims_a_denied_cleanup(self):
        for denied in (False, True):
            with self.subTest(denied=denied), tempfile.TemporaryDirectory() as tmp:
                run_dir = Path(tmp)
                process = mock.Mock(pid=4242, returncode=None)
                process.wait.side_effect = lambda **_: setattr(process, "returncode", -signal.SIGKILL)
                prepared = {"argv": ["host"], "cwd": tmp, "session_id": SID, "stdin_prompt": "p"}
                with mock.patch.object(claude_adapter.subprocess, "Popen", return_value=process), \
                     mock.patch.object(claude_adapter, "_serve", side_effect=OSError("stream failed")), \
                     mock.patch.object(claude_adapter, "_close_intake", side_effect=OSError("close failed")), \
                     mock.patch.object(claude_adapter.os, "killpg", side_effect=PermissionError if denied else None) as killpg:
                    if denied:
                        with self.assertRaises(PermissionError):
                            claude_adapter._messaging_worker(run_dir, prepared)
                        self.assertFalse((run_dir / "exit.json").exists())
                        process.wait.assert_not_called()
                    else:
                        claude_adapter._messaging_worker(run_dir, prepared)
                        self.assertNotEqual(json.loads((run_dir / "exit.json").read_text())["exit_code"], 0)
                    killpg.assert_called_once_with(4242, signal.SIGKILL)

    def test_a_host_that_ignores_sigterm_is_killed_and_reaped(self):
        process = mock.Mock(pid=4242)
        process.wait.side_effect = [subprocess.TimeoutExpired("host", 5), 0]
        with mock.patch.object(claude_adapter.os, "killpg") as killpg:
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
        with mock.patch.object(claude_adapter.os, "killpg") as killpg:
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

    def test_real_stream_call_keeps_raw_events_and_resumes(self):
        first = self.call("the word is heron", session_id=SID, keep_events=True)
        self.assertEqual(first.events[1]["message"]["content"][0]["type"], "tool_use")
        second = self.call("recall", session_id=SID, resume=True, keep_events=True)
        self.assertIn("heron", second.text)
        self.assertEqual(second.events[-1]["session_id"], SID)

    def test_driver_options_and_environment_reach_the_cli_with_a_literal_prompt(self):
        capture = self.dir / "capture.py"
        capture.write_text(f"#!{sys.executable}\nimport json,os,sys\nprint(json.dumps({{'type':'result','is_error':False,"
                           "'result':json.dumps([sys.argv[1:], os.environ.get('CHIEF_EVAL_TEST'), "
                           "os.environ.get('UNRELATED_SECRET')])}))\n")
        capture.chmod(0o755)
        with mock.patch.object(claude_adapter, "resolve", return_value=str(capture)):
            got = claude_adapter.call(Request(prompt="-literal"), extra_args=("--max-turns", "4"),
                                      env={"CHIEF_EVAL_TEST": "set", "UNRELATED_SECRET": "discard"})
        args, marker, secret = json.loads(got.text)
        self.assertEqual(args[-4:], ["--max-turns", "4", "--", "-literal"])
        self.assertEqual((marker, secret), ("set", None))

    def test_supported_auth_routing_and_network_environment_reaches_process(self):
        names = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL",
                 "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "AWS_REGION",
                 "AWS_PROFILE", "AWS_SECRET_ACCESS_KEY", "GOOGLE_APPLICATION_CREDENTIALS",
                 "HTTPS_PROXY", "SSL_CERT_FILE", "CLAUDE_CONFIG_DIR", "CLAUDE_AS_PROFILES_DIR")
        capture = self.dir / "environment"
        capture.write_text(f"#!{sys.executable}\nimport json,os\n"
                           "print(json.dumps({'type':'result','is_error':False,'result':json.dumps(dict(os.environ))}))\n")
        capture.chmod(0o755)
        env = {name: "fixture-value" for name in names}
        with mock.patch.object(claude_adapter, "resolve", return_value=str(capture)):
            got = claude_adapter.call(Request(prompt="p"), env={**env, "UNRELATED_SECRET": "drop"})
        received = json.loads(got.text)
        self.assertEqual({name: received.get(name) for name in names}, env)
        self.assertNotIn("UNRELATED_SECRET", received)

    def test_probe_and_launched_profile_share_one_account_directory(self):
        profiles = self.dir / "profiles"
        decoy = self.dir / "decoy"
        account = profiles / "secondary"
        transcript = account / "projects" / "any" / "session.jsonl"
        transcript.parent.mkdir(parents=True)
        decoy.mkdir()
        stamp = datetime.now(timezone.utc)
        transcript.write_text(json.dumps({"type": "rate_limit_event", "timestamp": stamp.isoformat(),
                                          "rate_limit_info": {"rateLimitType": "weekly", "utilization": 0.4,
                                                              "resetsAt": (stamp + timedelta(days=2)).timestamp()}}) + "\n")
        capture = self.dir / "environment"
        capture.write_text(f"#!{sys.executable}\nimport json,os\n"
                           "print(json.dumps({'type':'result','is_error':False,'result':json.dumps(dict(os.environ))}))\n")
        capture.chmod(0o755)
        with mock.patch.dict(os.environ, {"CLAUDE_AS_PROFILES_DIR": str(profiles), "CLAUDE_CONFIG_DIR": str(decoy)}):
            self.assertEqual(limits_probe.read("claude", "secondary")[0].used, 40)
            self.assertEqual(session_logs.claude_home("secondary"), account)
            with mock.patch.object(claude_adapter, "resolve", return_value=str(capture)):
                got = claude_adapter.call(Request(prompt="p", cwd=str(self.dir)), claude_as_profile="secondary")
            received = json.loads(got.text)
            self.assertEqual(received.get("CLAUDE_AS_PROFILES_DIR"), str(profiles))
            self.assertEqual(received.get("CLAUDE_CONFIG_DIR"), str(account))
            run_dir = self.dir / "run"
            run_dir.mkdir()
            with mock.patch.object(claude_adapter, "resolve", return_value=str(capture)):
                process = claude_adapter.start(Request(prompt="p", session_id=SID, cwd=str(self.dir)), run_dir,
                                                claude_as_profile="secondary")
                self.assertEqual(process.wait(timeout=5), 0)
            self.assertTrue((run_dir / "exit.json").exists())
            self.assertEqual(json.loads((run_dir / "command.json").read_text())["env"]["CLAUDE_CONFIG_DIR"], str(account))
            child = json.loads(json.loads((run_dir / "stdout").read_text())["result"])
            self.assertEqual(child.get("CLAUDE_CONFIG_DIR"), str(account))
            self.assertEqual(child.get("CLAUDE_AS_PROFILES_DIR"), str(profiles))
            os.symlink(account, profiles / "linked")
            before = len(self.resolved)
            # A trio consult reads a Failure; a raise would escape trio_gate._ask and fail the engine pass.
            got = claude_adapter.call(Request(prompt="p", cwd=str(self.dir)), claude_as_profile="linked")
            self.assertIsInstance(got, Failure)
            self.assertEqual((got.kind, got.retryable), ("error", False))
            self.assertIn("symlink", got.message)
            linked_run = self.dir / "linked-run"
            linked_run.mkdir()
            with self.assertRaises(ValueError):
                claude_adapter.start(Request(prompt="p", session_id=SID, cwd=str(self.dir)), linked_run,
                                     claude_as_profile="linked")
            self.assertEqual(len(self.resolved), before)
            self.assertFalse((linked_run / "command.json").exists())
            self.assertFalse((linked_run / "exit.json").exists())

    def test_login_setup_cannot_replace_the_eval_shim_path(self):
        shell = self.dir / "bash"
        # Python stand-in for a login shell that resets PATH before executing its command source.
        shell.write_text(f"#!{sys.executable}\nimport os,sys\nos.environ['PATH']='/usr/bin:/bin'\n"
                         "os.execv('/bin/bash', ['/bin/bash', '-c', *sys.argv[2:]])\n")
        shell.chmod(0o755)
        capture = self.dir / "path-capture"
        capture.write_text(f"#!{sys.executable}\nimport json,os\n"
                           "print(json.dumps({'type':'result','is_error':False,'result':os.environ['PATH']}))\n")
        capture.chmod(0o755)
        with mock.patch.dict(os.environ, {"SHELL": str(shell)}), \
             mock.patch.object(claude_adapter, "resolve", return_value=str(capture)):
            got = claude_adapter.call(Request(prompt="p"), env={"CHIEF_OF_STUFF_PATH_PREFIX": "/eval/shims"})
        self.assertEqual(got.text.split(os.pathsep)[0], "/eval/shims")

    def test_raw_output_stderr_and_executed_command_survive_a_failed_call(self):
        capture = self.dir / "failure"
        capture.write_text(f"#!{sys.executable}\nimport sys\nprint('startup noise')\n"
                           "print('{\"type\":\"system\"}')\nprint('diagnostic', file=sys.stderr)\nsys.exit(2)\n")
        capture.chmod(0o755)
        artifacts = self.dir / "artifacts"
        with mock.patch.object(claude_adapter, "resolve", return_value=str(capture)):
            got = claude_adapter.call(Request(prompt="p", keep_events=True), artifacts=artifacts,
                                      env={"ANTHROPIC_API_KEY": "never-record-this", "UNRELATED_SECRET": "drop"})
        self.assertIsInstance(got, Failure)
        self.assertIn('startup noise', (artifacts / 'stdout').read_text())
        self.assertTrue((artifacts / 'stderr').read_text().endswith('diagnostic\n'))
        command = json.loads((artifacts / 'command.json').read_text())
        self.assertIn(str(capture), command['argv'])
        self.assertEqual(command['argv'][-2:], ['--', 'p'])
        self.assertIn('ANTHROPIC_API_KEY', command['env_keys'])
        self.assertNotIn('UNRELATED_SECRET', command['env_keys'])
        self.assertNotIn('never-record-this', (artifacts / 'command.json').read_text())

    def test_timeout_kills_pipe_holding_descendants_and_preserves_partial_output(self):
        capture = self.dir / "descendant"
        capture.write_text(f"#!{sys.executable}\nimport subprocess,sys\n"
                           "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)'])\n"
                           "print(child.pid, flush=True)\n")
        capture.chmod(0o755)
        artifacts = self.dir / "timeout"
        started = time.monotonic()
        with mock.patch.object(claude_adapter, "resolve", return_value=str(capture)):
            got = claude_adapter.call(Request(prompt="p", timeout_s=0.3), artifacts=artifacts)
        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual(got.kind, "unknown")
        pid = int((artifacts / 'stdout').read_text().strip())
        # A zombie may remain until the system reaps it; a running descendant may not. The kill lands
        # asynchronously, so read the state once it has had time to (#955).
        deadline = time.monotonic() + 2
        while True:
            status = subprocess.run(['ps', '-o', 'stat=', '-p', str(pid)], capture_output=True, text=True).stdout.strip()
            if not status or status.startswith('Z') or time.monotonic() > deadline:
                break
            time.sleep(0.05)
        self.assertTrue(not status or status.startswith('Z'), status)

    def assert_auth_mode(self, names):
        capture = self.dir / "auth-capture"
        capture.write_text(f"#!{sys.executable}\nimport json,os\n"
                           "print(json.dumps({'is_error':False,'result':json.dumps(dict(os.environ))}))\n")
        capture.chmod(0o755)
        expected = {name: "fixture-auth" for name in names}
        with mock.patch.object(claude_adapter, "resolve", return_value=str(capture)):
            got = claude_adapter.call(Request(prompt="p"), env={**expected, "UNRELATED_SECRET": "drop"})
        received = json.loads(got.text)
        self.assertEqual({name: received.get(name) for name in names}, expected)
        self.assertNotIn("UNRELATED_SECRET", received)
        run_dir = self.dir / (names[0] + "-worker")
        run_dir.mkdir()
        with mock.patch.dict(os.environ, {**expected, "UNRELATED_SECRET": "drop"}), \
             mock.patch.object(claude_adapter, "resolve", return_value=str(capture)):
            process = claude_adapter.start(Request(prompt="p", session_id=SID), run_dir)
            self.assertEqual(process.wait(timeout=5), 0)
        received = json.loads(json.loads((run_dir / "stdout").read_text())["result"])
        self.assertEqual({name: received.get(name) for name in names}, expected)
        self.assertNotIn("UNRELATED_SECRET", received)

    def test_oauth_authentication(self):
        self.assert_auth_mode(["CLAUDE_CODE_OAUTH_TOKEN"])

    def test_anthropic_authentication(self):
        self.assert_auth_mode(["ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL"])

    def test_bedrock_authentication(self):
        self.assert_auth_mode(["CLAUDE_CODE_USE_BEDROCK", "AWS_BEARER_TOKEN_BEDROCK",
                               "ANTHROPIC_BEDROCK_BASE_URL", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY",
                               "AWS_SESSION_TOKEN", "AWS_PROFILE", "AWS_REGION"])

    def test_vertex_authentication(self):
        self.assert_auth_mode(["CLAUDE_CODE_USE_VERTEX", "ANTHROPIC_VERTEX_BASE_URL",
                               "ANTHROPIC_VERTEX_PROJECT_ID", "CLOUD_ML_REGION", "GOOGLE_APPLICATION_CREDENTIALS"])

    def test_foundry_authentication(self):
        self.assert_auth_mode(["CLAUDE_CODE_USE_FOUNDRY", "ANTHROPIC_FOUNDRY_API_KEY",
                               "ANTHROPIC_FOUNDRY_AUTH_TOKEN", "ANTHROPIC_FOUNDRY_BASE_URL",
                               "ANTHROPIC_FOUNDRY_RESOURCE", "AZURE_CLIENT_ID", "AZURE_CLIENT_SECRET",
                               "AZURE_TENANT_ID", "AZURE_FEDERATED_TOKEN_FILE", "AZURE_AUTHORITY_HOST"])

    def test_keyboard_interrupt_kills_and_reaps_before_propagating(self):
        process = mock.Mock(pid=4242)
        process.communicate.side_effect = [KeyboardInterrupt, ("partial", "")]
        with mock.patch.object(claude_adapter.subprocess, "Popen", return_value=process), \
             mock.patch.object(claude_adapter.os, "killpg") as killpg:
            with self.assertRaises(KeyboardInterrupt):
                self.call("p")
        killpg.assert_called_once_with(4242, signal.SIGKILL)
        self.assertEqual(process.communicate.call_count, 2)
        self.assertIsNotNone(process.communicate.call_args.kwargs.get("timeout"))

    def test_keyboard_interrupt_reaps_a_real_detached_process(self):
        ready = self.dir / "ready"
        self.cli.write_text(f"#!{sys.executable}\nfrom pathlib import Path\nimport time\n"
                            f"Path({str(ready)!r}).touch()\ntime.sleep(30)\n")
        popen = subprocess.Popen
        processes = []
        def launch(*args, **kwargs):
            process = popen(*args, **kwargs)
            processes.append(process)
            communicate = process.communicate
            interrupted = False
            def interrupt_once(*args, **kwargs):
                nonlocal interrupted
                if not interrupted:
                    interrupted = True
                    deadline = time.monotonic() + 3
                    while not ready.exists() and time.monotonic() < deadline:
                        time.sleep(0.01)
                    self.assertTrue(ready.exists())
                    raise KeyboardInterrupt
                return communicate(*args, **kwargs)
            process.communicate = interrupt_once
            return process
        started = time.monotonic()
        try:
            with mock.patch.object(claude_adapter.subprocess, "Popen", side_effect=launch):
                with self.assertRaises(KeyboardInterrupt):
                    self.call("p")
            self.assertLess(time.monotonic() - started, 5)
            self.assertEqual(processes[0].returncode, -signal.SIGKILL)
        finally:
            for process in processes:
                if process.poll() is None:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.communicate(timeout=1)

    def test_timeout_drain_is_bounded_even_when_an_escaped_child_holds_a_pipe(self):
        capture = self.dir / "escaped-child"
        capture.write_text(f"#!{sys.executable}\nimport subprocess,sys\n"
                           "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)'],start_new_session=True)\n"
                           "print(child.pid,flush=True)\n")
        capture.chmod(0o755)
        artifacts = self.dir / "escaped-output"
        started = time.monotonic()
        try:
            with mock.patch.object(claude_adapter, "resolve", return_value=str(capture)):
                got = claude_adapter.call(Request(prompt="p", timeout_s=0.5), artifacts=artifacts)
            self.assertLess(time.monotonic() - started, 5)
            self.assertEqual(got.kind, "unknown")
            self.assertTrue(json.loads((artifacts / "exit.json").read_text())["timed_out"])
        finally:
            if (artifacts / "stdout").exists():
                try:
                    os.kill(int((artifacts / "stdout").read_text().strip()), signal.SIGKILL)
                except ProcessLookupError:
                    pass

    def test_timeout_cleanup_tolerates_permission_error_after_leader_exits(self):
        process = mock.Mock(pid=123)
        process.poll.return_value = 0
        process.communicate.return_value = ("partial", "")
        with mock.patch.object(claude_adapter.os, "killpg", side_effect=PermissionError):
            self.assertEqual(claude_adapter._kill_and_drain(process), ("partial", ""))

    def test_timeout_cleanup_does_not_hide_permission_error_for_live_leader(self):
        process = mock.Mock(pid=123)
        process.poll.return_value = None
        with mock.patch.object(claude_adapter.os, "killpg", side_effect=PermissionError):
            with self.assertRaises(PermissionError):
                claude_adapter._kill_and_drain(process)

    def test_truncated_utf8_output_is_saved_and_classified(self):
        self.cli.write_text(f"#!{sys.executable}\nimport os,time\nos.write(1,b'partial: \\xe2')\ntime.sleep(30)\n")
        artifacts = self.dir / "truncated-output"
        got = claude_adapter.call(Request(prompt="p", timeout_s=0.5), artifacts=artifacts)
        self.assertEqual(got.kind, "unknown")
        self.assertIn("partial:", (artifacts / "stdout").read_text())

    def test_extra_options_do_not_depend_on_the_prompt_being_the_last_two_tokens(self):
        with mock.patch.object(claude_adapter, "argv", return_value=[str(self.cli), '--', 'p', '--future']), \
             mock.patch.object(claude_adapter, "resolve", return_value=str(self.cli)):
            cmd = claude_adapter.command(Request(prompt='p'), extra_args=('--max-turns', '4'))
        self.assertEqual(cmd[-5:], ['--max-turns', '4', '--', 'p', '--future'])

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
        # Ledger E12: no silent fallback to a bare `claude`. The pin stats this directory, not ~/.claude-profiles.
        profiles = self.dir / "profiles"
        (profiles / "secondary").mkdir(parents=True)
        with mock.patch.dict(os.environ, {"CLAUDE_AS_PROFILES_DIR": str(profiles)}):
            self.assertEqual(claude_adapter.call(Request(prompt="p", cwd=str(self.dir)), claude_as_profile="secondary").text,
                             "reply: p")
            self.assertEqual(self.resolved, ["claude-as"])
            self.resolved.clear()
            with mock.patch.object(claude_adapter, "resolve", side_effect=lambda p: self.resolved.append(p)):
                got = claude_adapter.call(Request(prompt="p", cwd=str(self.dir)), claude_as_profile="secondary")
            self.assertEqual((got.kind, self.resolved), ("missing_program", ["claude-as"]))
            self.assertIn("claude_as_profile", got.message)


class WorkerClockTest(unittest.TestCase):
    """M3 item 8: worker launch checks `claude --setting-sources user plugin list --json` before spawning."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.cli = self.dir / "claude"
        self.cli.write_text(f"#!{sys.executable}\nimport runpy\nrunpy.run_path({str(FAKE)!r}, run_name='__main__')\n")
        self.cli.chmod(0o755)
        self.resolved = []
        patches = (mock.patch.dict(os.environ, {"SHELL": "/bin/sh"}),
                   mock.patch.object(claude_adapter, "resolve", side_effect=self.resolve))
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def resolve(self, program):
        self.resolved.append(program)
        return str(self.cli)

    def test_passes_with_fake_claude_and_captured_fixture_and_uses_file_streams(self):
        claude_adapter.require_worker_clock(cwd=str(self.dir))
        self.assertEqual(self.resolved, ["claude"])
        fixture = (ROOT / "evals" / "fixtures" / "claude-plugin-list-user.json").read_text(encoding="utf-8")
        probe = self.dir / "probe"
        probe.write_text(f"#!{sys.executable}\nimport json,sys\n"
                         f"assert sys.argv[1:] == ['--setting-sources', 'user', 'plugin', 'list', '--json'], sys.argv\n"
                         f"print({fixture!r})\n")
        probe.chmod(0o755)
        seen = {}
        real_popen = subprocess.Popen

        def spy_popen(*args, **kwargs):
            seen.update(kwargs)
            return real_popen(*args, **kwargs)

        with mock.patch.object(claude_adapter, "resolve", return_value=str(probe)), \
             mock.patch.object(claude_adapter.subprocess, "Popen", side_effect=spy_popen):
            claude_adapter.require_worker_clock(cwd=str(self.dir))
        self.assertEqual(seen["stdin"], subprocess.DEVNULL)
        self.assertNotEqual(seen["stdout"], subprocess.PIPE)
        self.assertNotEqual(seen["stderr"], subprocess.PIPE)
        self.assertTrue(seen["start_new_session"])
        self.assertEqual(seen["cwd"], str(self.dir))

    def test_profile_runs_through_claude_as_and_missing_binary_raises_shell_error(self):
        capture = self.dir / "capture-profile"
        fixture = (ROOT / "evals" / "fixtures" / "claude-plugin-list-user.json").read_text(encoding="utf-8")
        capture.write_text(f"#!{sys.executable}\nimport sys\n"
                           "assert sys.argv[1:] == ['run', 'work', '--setting-sources', 'user', 'plugin', 'list', '--json'], sys.argv\n"
                           f"print({fixture!r})\n")
        capture.chmod(0o755)
        with mock.patch.object(claude_adapter, "resolve", side_effect=lambda p: self.resolved.append(p) or str(capture)):
            claude_adapter.require_worker_clock("work", cwd=str(self.dir))
        self.assertEqual(self.resolved[-1], "claude-as")
        with mock.patch.object(claude_adapter, "resolve", return_value=None):
            with self.assertRaises(claude_adapter.ShellError):
                claude_adapter.require_worker_clock("work", cwd=str(self.dir))

    def test_refuses_missing_disabled_project_scoped_or_wrong_marketplace_plugin(self):
        for payload in (
            [],
            [{"id": "datetime-inject@ai-additions", "scope": "user", "enabled": False}],
            [{"id": "datetime-inject@ai-additions", "scope": "project", "enabled": True}],
            [{"id": "datetime-inject@other", "scope": "user", "enabled": True}],
        ):
            with self.subTest(payload=payload):
                self.cli.write_text(f"#!{sys.executable}\nimport json\nprint(json.dumps({payload!r}))\n")
                with self.assertRaises(RuntimeError) as raised:
                    claude_adapter.require_worker_clock(cwd=str(self.dir))
                self.assertIn("datetime-inject@ai-additions", str(raised.exception))
                self.assertIn("user", str(raised.exception))

    def test_refuses_nonzero_exit_unparseable_output_and_timeout(self):
        self.cli.write_text(f"#!{sys.executable}\nimport sys\nprint('plugin list boom', file=sys.stderr)\nsys.exit(2)\n")
        with self.assertRaises(RuntimeError) as raised:
            claude_adapter.require_worker_clock(cwd=str(self.dir))
        self.assertIn("datetime-inject@ai-additions", str(raised.exception))
        self.assertIn("plugin list boom", str(raised.exception))

        for bad_out in ("not json", "{}"):
            with self.subTest(bad_out=bad_out):
                self.cli.write_text(f"#!{sys.executable}\nprint({bad_out!r})\n")
                with self.assertRaises(RuntimeError) as raised:
                    claude_adapter.require_worker_clock(cwd=str(self.dir))
                self.assertIn("datetime-inject@ai-additions", str(raised.exception))

        process = mock.Mock(pid=9876)
        process.wait.side_effect = [subprocess.TimeoutExpired("claude", 15), 0]
        with mock.patch.object(claude_adapter.subprocess, "Popen", return_value=process), \
             mock.patch.object(claude_adapter.os, "killpg") as killpg:
            with self.assertRaises(RuntimeError) as raised:
                claude_adapter.require_worker_clock(cwd=str(self.dir))
        killpg.assert_called_once_with(9876, signal.SIGKILL)
        self.assertIn("datetime-inject@ai-additions", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
