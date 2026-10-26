"""The shared eval runner builds and reads all four CLI backends."""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import run  # noqa: E402


def has_run(cmd, runtime):
    """#52: the eval harness passes an effort as the same consecutive tokens the worker launcher does."""
    tokens = run.runtimes.get(runtime).effort_tokens("high")
    return any(cmd[i:i + len(tokens)] == tokens for i in range(len(cmd)))


class HostCommandTest(unittest.TestCase):
    def test_agy_and_claude_pass_their_effort_as_the_launcher_does(self):
        with mock.patch.object(run.shutil, "which", return_value="/bin/agy"):
            self.assertTrue(has_run(run.host_command("agy", "m", "hi", Path("/tmp/work"), Path("/tmp/plugin"),
                                                     effort="high"), "agy"))
        self.assertTrue(has_run(run.claude_adapter.argv(run.claude_request(run.Case("c", Path("/c"), {}), "baseline", "sonnet", "x", Path("/work"), effort="high"), "/host", []),
                                "claude"))

    def test_each_backend_runs_headlessly_on_an_explicit_model(self):
        with mock.patch.object(run.shutil, "which", side_effect=lambda name: "/bin/" + name):
            for runtime, flag in (("codex", "--json"), ("cursor", "stream-json"), ("agy", "stream-json")):
                with self.subTest(runtime=runtime):
                    cmd = run.host_command(runtime, "gpt-6", "hi", Path("/tmp/work"), Path("/tmp/plugin"))
                    self.assertIn(flag, cmd)
                    self.assertIn("gpt-6", cmd)
                    self.assertIn("hi", cmd)

    def test_codex_mock_server_is_scoped_to_the_run(self):
        config = {"mcpServers": {"calendar": {"command": "/usr/bin/python3", "args": ["mock.py"]}}}
        with mock.patch.object(run.shutil, "which", return_value="/bin/codex"):
            cmd = run.host_command("codex", "gpt-6", "hi", Path("/tmp/work"), Path("/tmp/plugin"), config)
        self.assertIn('mcp_servers.calendar.command="/usr/bin/python3"', cmd)
        self.assertIn('mcp_servers.calendar.args=["mock.py"]', cmd)

    def test_codex_resume_uses_the_explicit_conversation_id(self):
        with mock.patch.object(run.shutil, "which", return_value="/bin/codex"):
            cmd = run.host_command("codex", "gpt-6-sol", "next", Path("/tmp/work"),
                                   Path("/tmp/plugin"), session_id="thread-123", persistent=True,
                                   effort="high")
        self.assertEqual(cmd[1:3], ["exec", "resume"])
        self.assertEqual(cmd[-2:], ["thread-123", "next"])
        self.assertTrue(has_run(cmd, "codex"), cmd)  # the launcher's own tokens, not a second spelling
        self.assertIn('sandbox_mode="workspace-write"', cmd)
        self.assertNotIn("--last", cmd)
        with mock.patch.object(run.shutil, "which", return_value="/bin/codex"):
            scoped = run.host_command("codex", "gpt-6-sol", "next", Path("/tmp/work"),
                Path("/tmp/plugin"), session_id="thread-123", persistent=True,
                extra_write=Path("/tmp/result"))
        self.assertIn('sandbox_workspace_write.writable_roots=["/tmp/result"]', scoped)

    def test_other_hosts_refuse_unisolated_mocks(self):
        config = {"mcpServers": {"calendar": {"command": "python3", "args": []}}}
        with mock.patch.object(run.shutil, "which", return_value="/bin/agent"):
            with self.assertRaisesRegex(RuntimeError, "isolated MCP"):
                run.host_command("cursor", "model", "hi", Path("/tmp/work"), Path("/tmp/plugin"), config)


class HostStreamTest(unittest.TestCase):
    def test_codex_events_become_existing_grader_inputs(self):
        events = [
            {"type": "thread.started", "thread_id": "abc"},
            {"type": "item.started", "item": {"id": "1", "type": "command_execution", "command": "date"}},
            {"type": "item.completed", "item": {"id": "2", "type": "agent_message", "text": "Now 09:00 CDT"}},
            {"type": "turn.completed", "usage": {"input_tokens": 3}},
        ]
        stream = run.parse_host_stream("\n".join(json.dumps(e) for e in events), "codex", "gpt-6")
        self.assertEqual(stream.init["thread_id"], "abc")
        self.assertEqual(stream.tool_uses[0]["name"], "Bash")
        self.assertEqual(stream.tool_uses[0]["input"]["command"], "date")
        self.assertEqual(stream.last_text, "Now 09:00 CDT")
        self.assertIsNotNone(stream.result)

    def test_agy_result_response_becomes_last_text(self):
        event = {"event": "result", "result": {"status": "SUCCESS", "response": "done", "num_turns": 1}}
        stream = run.parse_host_stream(json.dumps(event), "agy", "gemini-x")
        self.assertEqual(stream.last_text, "done")
        self.assertEqual(stream.result["num_turns"], 1)

    def test_agy_step_updates_count_actual_shell_calls(self):
        event = {"event": "step_update", "step_update": {"step_type": "tool", "state": "ACTIVE",
            "step_index": 4, "tool_name": "run_command",
            "tool_info": {"parameters": {"CommandLine": "TZ=America/Chicago date"}}}}
        stream = run.parse_host_stream(json.dumps(event), "agy", "gemini-x")
        self.assertEqual(stream.tool_uses[0]["name"], "Bash")
        self.assertIn("date", stream.tool_uses[0]["input"]["CommandLine"])

    def test_cursor_started_tool_calls_count_once(self):
        events = [
            {"type": "system", "subtype": "init", "session_id": "cursor-123", "model": "Auto"},
            {"type": "tool_call", "subtype": "started", "call_id": "one",
             "tool_call": {"shellToolCall": {"args": {"command": "TZ=America/Chicago date"}}}},
            {"type": "tool_call", "subtype": "completed", "call_id": "one",
             "tool_call": {"shellToolCall": {"args": {"command": "TZ=America/Chicago date"}}}},
            {"type": "result", "result": "Now 09:00 CDT"},
        ]
        stream = run.parse_host_stream("\n".join(json.dumps(e) for e in events), "cursor", "auto")
        self.assertEqual(stream.init["session_id"], "cursor-123")
        self.assertEqual(stream.tool_uses, [{"id": "one", "name": "Bash",
            "input": {"command": "TZ=America/Chicago date"}}])

    def test_host_turns_grade_each_snapshot_through_one_conversation(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            binary = base / "codex"
            binary.write_text("#!/usr/bin/env python3\nimport json,sys\n"
                "print(json.dumps({'type':'thread.started','thread_id':'thread-123'}))\n"
                "print(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':'second' if 'resume' in sys.argv else 'first'}}))\n"
                "print(json.dumps({'type':'turn.completed'}))\n")
            binary.chmod(0o755)
            out, work = base / "out", base / "work"
            out.mkdir()
            work.mkdir()
            calls = base / "calls.jsonl"
            calls.write_text("")
            spec = {"turns": [
                {"prompt": "one", "graders": [{"type": "regex", "pattern": "first"}]},
                {"prompt": "two", "graders": [{"type": "regex", "pattern": "second"}]},
            ]}
            with mock.patch.object(run.shutil, "which", return_value=str(binary)):
                results, error, meta = run.run_host_turns(spec, "baseline", "gpt-6-sol", out, work,
                    dict(os.environ), None, calls, "America/Chicago", runtime="codex", root=run.PLUGIN_ROOT)
            self.assertIsNone(error)
            self.assertTrue(all(passed for _, passed, _ in results))
            self.assertEqual(meta["turns_reached"], 2)

    def test_cursor_and_agy_backends_grade_fake_cli_output(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            for runtime in ("cursor", "agy"):
                with self.subTest(runtime=runtime):
                    binary = base / runtime
                    event = ({"type": "result", "result": {"text": "done"}} if runtime == "cursor" else
                             {"event": "result", "result": {"status": "SUCCESS", "response": "done"}})
                    binary.write_text("#!/usr/bin/env python3\nimport json\nprint(json.dumps(" + repr(event) + "))\n")
                    binary.chmod(0o755)
                    out, work = base / f"out-{runtime}", base / f"work-{runtime}"
                    out.mkdir()
                    work.mkdir()
                    calls = base / f"calls-{runtime}.jsonl"
                    calls.write_text("")
                    spec = {"turns": [{"prompt": "go", "graders": [{"type": "regex", "pattern": "done"}]}]}
                    with mock.patch.object(run.shutil, "which", return_value=str(binary)):
                        results, error, _ = run.run_host_turns(spec, "baseline", "model", out, work,
                            dict(os.environ), None, calls, "America/Chicago", runtime=runtime,
                            root=run.PLUGIN_ROOT)
                    self.assertIsNone(error)
                    self.assertTrue(results[0][1])


if __name__ == "__main__":
    unittest.main()
