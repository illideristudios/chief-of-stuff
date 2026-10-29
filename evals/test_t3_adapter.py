"""The T3 launcher behind the host adapter contract (§13, #1178), against a fake `t3` CLI. No test needs T3."""

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import guard_binding
import t3_adapter
from model_port import Request
from shell_setup import ShellError

BINDING = {"CHIEF_ISSUE_KEY": "github:o/r#1", "CHIEF_STAGE": "build", "CHIEF_ATTEMPT": "1", "CHIEF_SESSION_ID": "s",
           "CHIEF_ROOT": "/ws", "CHIEF_PYTHON": sys.executable, "CHIEF_PROTECTED_BRANCHES": '["main"]'}

# A fake `t3`: `project add` registers the tree, `thread start` refuses an unregistered tree as T3 does, `thread show`
# answers running twice and then completed, and `thread send` is logged. State lives in the file named by CHIEF_FAKE_T3 (CHIEF_* is the one prefix a runner keeps).
FAKE_T3 = '''#!{python}
import json, os, sys
state_path = os.environ["CHIEF_FAKE_T3"]
state = json.load(open(state_path)) if os.path.exists(state_path) else {{"projects": [], "calls": [], "shows": 0}}
args = sys.argv[1:]
state["calls"].append(args)
def done(code=0):
    json.dump(state, open(state_path, "w")); sys.exit(code)
if args[:2] == ["project", "add"]:
    state["projects"].append(args[2]); print("Added project"); done()
if args[:2] == ["thread", "start"]:
    if args[2] not in state["projects"]:
        print("No T3 Code project matches", file=sys.stderr); done(1)
    state["prompt"] = sys.stdin.read()
    print(json.dumps({{"threadId": "mcp:thread-1"}})); done()
if args[:2] == ["thread", "show"]:
    state["shows"] += 1
    finished = state["shows"] > 2
    thread = {{"threadId": args[2], "status": "completed" if finished else "running",
               "activeRunId": None if finished else "run-1", "pendingRequestCount": 0}}
    print(json.dumps({{"thread": thread, "messages": [{{"role": "assistant", "text": "all done"}}]}})); done()
if args[:2] == ["thread", "send"]:
    done()
done(2)
'''


class T3AdapterTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name).resolve()

    def git_tree(self) -> Path:
        tree = self.dir / "tree"
        tree.mkdir()
        subprocess.run(["git", "init", "-q", str(tree)], check=True)
        return tree

    def fake_cli(self) -> Path:
        cli = self.dir / "t3"
        cli.write_text(FAKE_T3.format(python=sys.executable))
        cli.chmod(0o755)
        return cli

    def test_the_environment_id_is_read_from_t3s_data_directory(self):
        (self.dir / "userdata").mkdir()
        (self.dir / "userdata" / "environment-id").write_text("env-1234\n")
        self.assertEqual(t3_adapter.environment_id(self.dir), "env-1234")
        with self.assertRaises(ShellError):
            t3_adapter.environment_id(self.dir / "nowhere")

    def test_a_thread_link_is_the_deep_link_form(self):
        self.assertEqual(t3_adapter.thread_link("env-1", "mcp:abc"), "t3code://threads/env-1/mcp:abc")

    def test_the_adapter_contract_classifies_plain_replies_and_cancels_by_process(self):
        from model_port import Reply
        self.assertIsInstance(t3_adapter.classify(0, "plain T3 reply", ""), Reply)
        (self.dir / "t3.json").write_text("{}")
        (self.dir / "thread.json").write_text('{"thread_id":"t1","thread_run_id":"r1"}')
        process = mock.Mock(t3_run_dir=self.dir)
        with mock.patch.object(t3_adapter, "_mcp", return_value={}), \
             mock.patch.object(t3_adapter, "observe", return_value={"status": "interrupted"}), \
             mock.patch.object(t3_adapter.claude_adapter, "cancel") as stop:
            t3_adapter.cancel(process)
        stop.assert_called_once_with(process)

    def test_a_thread_ends_when_it_is_idle_and_has_nothing_pending(self):
        running = {"status": "running", "activeRunId": "r", "pendingRequestCount": 0}
        self.assertIsNone(t3_adapter.outcome(running))
        self.assertEqual(t3_adapter.outcome({**running, "status": "completed", "activeRunId": None}), 0)
        self.assertEqual(t3_adapter.outcome({**running, "status": "error", "activeRunId": None}), 1)
        # A thread waiting on a request is waiting, not finished.
        self.assertIsNone(t3_adapter.outcome({"status": "completed", "activeRunId": None, "pendingRequestCount": 1}))

    def test_incomplete_and_nonterminal_states_never_count_as_success(self):
        for status in (None, "queued", "preparing", "waiting", "new-state"):
            self.assertIsNone(t3_adapter.outcome({"status": status}), status)
        self.assertEqual(t3_adapter.outcome({"status": "cancelled"}), 1)

    def test_links_encode_each_path_component(self):
        self.assertEqual(t3_adapter.thread_link("env/x?#", "mcp:t/1?x#y"),
                         "t3code://threads/env%2Fx%3F%23/mcp:t%2F1%3Fx%23y")

    def test_hook_paths_are_shell_arguments_even_with_command_substitution_characters(self):
        import shlex
        guard = self.dir / 'guard$(touch injected)"'
        (guard / "hooks").mkdir(parents=True)
        (guard / "hooks/hooks.json").write_text((guard_binding.guard_plugin_dir() / "hooks/hooks.json").read_text())
        hooks = t3_adapter._hooks(guard, BINDING)
        command = hooks["Stop"][0]["hooks"][0]["command"]
        args = shlex.split(command)
        self.assertIn(str(guard / "../scripts/worker_guard.py"), args)
        # Parse and execute with a harmless Python stand-in that only prints argv.
        stop = command[command.index(shlex.quote(sys.executable)):]
        marker = self.dir / "injected"
        subprocess.run(["/bin/zsh", "-fc", stop], cwd=self.dir, capture_output=True)
        self.assertFalse(marker.exists())

    def test_missing_environment_does_not_poison_the_worker_tree(self):
        tree, run = self.git_tree(), self.dir / "run"
        run.mkdir()
        request = Request("x", cwd=str(tree), plugin_dirs=(str(guard_binding.guard_plugin_dir()),))
        with self.assertRaises(ShellError):
            t3_adapter.start(request, run, env=BINDING, cli=str(self.fake_cli()), home=self.dir)
        self.assertFalse((tree / ".mcp.json").exists())
        self.assertFalse((tree / ".claude").exists())

    def test_owned_channel_can_be_rebound_but_modified_files_are_refused(self):
        tree = self.git_tree()
        kwargs = dict(agent="", guard=guard_binding.guard_plugin_dir(), state=self.dir)
        t3_adapter.write_channel(tree, BINDING, **kwargs)
        rebound = {**BINDING, "CHIEF_SESSION_ID": "next"}
        t3_adapter.write_channel(tree, rebound, **kwargs)
        self.assertEqual(json.loads((tree / ".mcp.json").read_text())["mcpServers"]["engine"]["env"], rebound)
        (tree / ".claude/settings.local.json").write_text("{}")
        with self.assertRaises(ShellError):
            t3_adapter.write_channel(tree, BINDING, **kwargs)

    def test_swapping_the_claude_directory_during_write_cannot_escape(self):
        tree = self.git_tree()
        outside = self.dir / "outside"
        outside.mkdir()
        original = Path.mkdir
        def swap(path, *args, **kwargs):
            result = original(path, *args, **kwargs)
            if path == tree / ".claude":
                path.rename(tree / ".moved")
                path.symlink_to(outside)
            return result
        with mock.patch.object(Path, "mkdir", swap):
            with self.assertRaises((ShellError, OSError)):
                t3_adapter.write_channel(tree, BINDING, agent="", guard=guard_binding.guard_plugin_dir())
        self.assertEqual(list(outside.iterdir()), [])

    def test_start_registers_the_exact_tree_even_when_a_parent_project_matches(self):
        spec = {"tree": "/parent/worker", "model": "sonnet", "provider": "claudeAgent", "title": "x", "prompt": "x"}
        def cli(spec, *args, **kwargs):
            return subprocess.CompletedProcess(args, 0, '{"threadId":"t1"}', "")
        with mock.patch.object(t3_adapter, "_t3", side_effect=cli) as call:
            self.assertEqual(t3_adapter._start_thread(spec), "t1")
        self.assertEqual(call.call_args_list[0].args[1:3], ("project", "add"))
        self.assertFalse(any("--force" in c.args for c in call.call_args_list))

    def test_cancel_interrupts_the_remote_thread_before_stopping_its_runner(self):
        run = self.dir / "run"
        run.mkdir()
        (run / "t3.json").write_text('{"home":"/t3"}')
        (run / "thread.json").write_text('{"thread_id":"t1","thread_run_id":"r1"}')
        order = []
        with mock.patch.object(t3_adapter, "_mcp", side_effect=lambda *a: order.append(a[1]) or {}) as mcp, \
             mock.patch.object(t3_adapter, "observe", return_value={"status": "interrupted"}), \
             mock.patch.object(t3_adapter.claude_adapter, "cancel", side_effect=lambda p: order.append("runner")):
            t3_adapter.cancel(mock.Mock(), run)
        self.assertEqual(order, ["t3_thread_interrupt", "runner"])
        self.assertEqual(mcp.call_args.args[2], {"threadId": "t1", "runId": "r1"})

    def test_failed_remote_cancel_keeps_the_runner_and_evidence(self):
        run = self.dir / "run"
        run.mkdir()
        (run / "t3.json").write_text('{"home":"/t3"}')
        (run / "thread.json").write_text('{"thread_id":"t1","thread_run_id":"r1"}')
        with mock.patch.object(t3_adapter, "_mcp", side_effect=RuntimeError("offline")), \
             mock.patch.object(t3_adapter.claude_adapter, "cancel") as stop:
            with self.assertRaises(RuntimeError):
                t3_adapter.cancel(mock.Mock(), run)
        stop.assert_not_called()

    def test_an_unconfirmed_run_id_cannot_interrupt_a_later_run_in_the_thread(self):
        (self.dir / "t3.json").write_text("{}")
        (self.dir / "thread.json").write_text('{"thread_id":"t1"}')
        with mock.patch.object(t3_adapter, "observe", return_value={"status": "running"}), \
             mock.patch.object(t3_adapter, "_mcp") as interrupt, \
             mock.patch.object(t3_adapter.claude_adapter, "cancel") as stop:
            with self.assertRaisesRegex(RuntimeError, "run id is unconfirmed"):
                t3_adapter.cancel(mock.Mock(), self.dir)
        interrupt.assert_not_called()
        stop.assert_not_called()

    def test_recovered_remote_completion_replaces_a_pollers_failure_exit(self):
        (self.dir / "exit.json").write_text('{"exit_code": 1}')
        with mock.patch.object(t3_adapter, "observe", return_value={"status": "completed"}):
            t3_adapter.refresh(self.dir)
        self.assertEqual(json.loads((self.dir / "exit.json").read_text())["exit_code"], 0)

    def test_owned_channels_can_be_recreated_after_both_files_were_removed(self):
        tree = self.git_tree()
        kwargs = dict(agent="", guard=guard_binding.guard_plugin_dir(), state=self.dir)
        t3_adapter.write_channel(tree, BINDING, **kwargs)
        (tree / ".mcp.json").unlink()
        (tree / ".claude/settings.local.json").unlink()
        (tree / ".claude").rmdir()
        t3_adapter.write_channel(tree, BINDING, **kwargs)
        self.assertTrue((tree / ".mcp.json").is_file())

    def test_poll_timeout_bad_json_and_nonzero_results_do_not_end_a_thread(self):
        run = self.dir / "run"
        run.mkdir()
        (run / "t3.json").write_text(json.dumps({"environment": "env", "poll_s": 0}))
        replies = [subprocess.TimeoutExpired("t3", 120),
                   subprocess.CompletedProcess([], 1, '{"thread":{"status":"completed"}}', "offline"),
                   subprocess.CompletedProcess([], 0, '{}', ""),
                   subprocess.CompletedProcess([], 0, '{"thread":{"status":"completed"}}', "")]
        with mock.patch.object(t3_adapter, "_start_thread", return_value="t1"), \
             mock.patch.object(t3_adapter, "_t3", side_effect=replies):
            self.assertEqual(t3_adapter.run("s", run / "t3.json"), 0)

    def test_engine_facts_use_remote_liveness_even_after_the_runner_exits(self):
        import engine_facts
        (self.dir / "exit.json").write_text('{"exit_code": -15}')
        record = {"launcher": "t3", "run_dir": str(self.dir), "pid": 1}
        with mock.patch.object(t3_adapter, "observe", return_value=t3_adapter.Evidence(alive=True, launcher="t3")), \
             mock.patch.object(engine_facts.process_status, "identity") as pid:
            self.assertTrue(engine_facts._alive(record))
        pid.assert_not_called()

    def test_ambiguous_start_is_recovered_by_unique_title_without_another_start(self):
        (self.dir / "t3.json").write_text(json.dumps({"title": "issue session-uuid", "tree": "/tree", "environment": "env", "project_id": "p1"}))
        (self.dir / "launching.json").write_text("{}")
        listed = {"threads": [{"threadId": "t1", "title": "issue session-uuid"}], "nextCursor": None}
        with mock.patch.object(t3_adapter, "_mcp", return_value=listed) as mcp, \
             mock.patch.object(t3_adapter, "_t3", return_value=subprocess.CompletedProcess([], 0, '{"thread":{"status":"running"}}', "")):
            self.assertEqual(t3_adapter.observe(self.dir)["status"], "running")
        self.assertEqual(json.loads((self.dir / "thread.json").read_text())["thread_id"], "t1")
        self.assertEqual(mcp.call_args.args[1], "t3_thread_list")

    def test_recovered_start_retains_the_stall_watchers_structured_evidence(self):
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)
        (self.dir / "t3.json").write_text(json.dumps({"title": "unique-session", "tree": "/tree",
                                                    "environment": "env", "project_id": "p1"}))
        listed = {"threads": [{"threadId": "t1", "title": "unique-session"}], "nextCursor": None}
        shown = {"thread": {"threadId": "t1", "status": "running", "activeRunId": "r1",
                            "updatedAt": now.isoformat(), "pendingRequestCount": 1, "itemCount": 12}}
        with mock.patch.object(t3_adapter, "_mcp", return_value=listed), \
             mock.patch.object(t3_adapter, "_t3", return_value=subprocess.CompletedProcess([], 0, json.dumps(shown), "")):
            evidence = t3_adapter.observe({"run_dir": str(self.dir)})
        self.assertEqual((evidence.alive, evidence.last_activity_at, evidence.pending_requests), (True, now, 1))
        self.assertFalse(evidence.error)
        self.assertEqual(json.loads((self.dir / "thread.json").read_text())["thread_id"], "t1")

    def test_t3_not_running_is_a_known_failed_start_not_a_permanent_unknown_thread(self):
        spec = {"tree": "/tree", "title": "unique", "model": "m", "provider": "claudeAgent", "prompt": "x"}
        (self.dir / "t3.json").write_text(json.dumps(spec))
        with mock.patch.object(t3_adapter, "_t3", return_value=subprocess.CompletedProcess([], 1, "", "T3 is not running")):
            self.assertEqual(t3_adapter.run("s", self.dir / "t3.json"), 1)
            self.assertEqual(t3_adapter.observe(self.dir)["status"], "failed")
            evidence = t3_adapter.observe({"run_dir": str(self.dir)})
            self.assertIs(evidence.alive, False)
            self.assertFalse(evidence.error)
        self.assertFalse((self.dir / "thread.json").exists())

    def test_mcp_control_uses_the_pinned_clients_result_and_revokes_its_session(self):
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                if "id" not in request:
                    self.send_response(202)
                    self.end_headers()
                    return
                method = request["method"]
                if method == "initialize":
                    result = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}},
                              "serverInfo": {"name": "fake T3", "version": "1"}}
                elif method == "tools/list":
                    result = {"tools": [{"name": "t3_project_list", "inputSchema": {"type": "object"}}]}
                else:
                    result = {"content": [], "structuredContent": {"projects": []}, "isError": False}
                body = json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        (self.dir / "userdata").mkdir()
        (self.dir / "userdata/server-runtime.json").write_text(json.dumps({"origin": f"http://127.0.0.1:{server.server_port}"}))
        issued = subprocess.CompletedProcess([], 0, '{"token":"test-token","sessionId":"test-session"}', "")
        with mock.patch.object(t3_adapter, "_t3", return_value=issued) as cli:
            self.assertEqual(t3_adapter._mcp({"home": str(self.dir)}, "t3_project_list", {}), {"projects": []})
        self.assertEqual(cli.call_args.args[1:], ("auth", "session", "revoke", "test-session"))

    def test_an_unrecognized_or_queued_status_is_not_proof_a_thread_ended(self):
        for status in ("queued", "unrecognized", None):
            with self.subTest(status=status):
                self.assertIsNone(t3_adapter.outcome({"status": status, "pendingRequestCount": 0}))

    def test_the_channel_files_carry_the_binding_the_agent_and_the_guard_hooks_and_stay_out_of_git(self):
        tree = self.git_tree()
        t3_adapter.write_channel(tree, BINDING, agent="builder", guard=guard_binding.guard_plugin_dir())
        engine = json.loads((tree / ".mcp.json").read_text())["mcpServers"]["engine"]
        self.assertEqual(engine["env"], BINDING)
        self.assertEqual(Path(engine["args"][0]).name, "engine_mcp.py")
        settings = json.loads((tree / ".claude" / "settings.local.json").read_text())
        self.assertEqual(settings["agent"], "builder")
        stop = settings["hooks"]["Stop"][0]["hooks"][0]["command"]
        self.assertIn("worker_guard.py", stop)
        self.assertIn("CHIEF_ISSUE_KEY='github:o/r#1'", stop)  # the hook has no inherited environment to read
        self.assertNotIn("${", stop)
        status = subprocess.run(["git", "status", "--porcelain"], cwd=tree, capture_output=True, text=True).stdout
        self.assertEqual(status.strip(), "")

    def test_the_channel_never_replaces_a_file_it_did_not_write(self):
        tree = self.git_tree()
        (tree / ".mcp.json").write_text("{}")
        with self.assertRaises(ShellError):
            t3_adapter.write_channel(tree, BINDING, agent="", guard=guard_binding.guard_plugin_dir())

    def test_the_channel_refuses_a_tree_whose_branch_brings_its_own_claude_config(self):
        # T3 cannot pass `--setting-sources user --strict-mcp-config`, so a branch's own settings, hooks and
        # servers would run as this user: such a tree is refused rather than launched.
        for name in (".claude/settings.json", ".claude/hooks/x.py", ".claude/agents/builder.md", ".claude/anything.txt"):
            tree = self.dir / name.replace("/", "_")
            tree.mkdir()
            subprocess.run(["git", "init", "-q", str(tree)], check=True)
            (tree / name).parent.mkdir(parents=True, exist_ok=True)
            (tree / name).write_text("{}")
            with self.assertRaises(ShellError, msg=name):
                t3_adapter.write_channel(tree, BINDING, agent="", guard=guard_binding.guard_plugin_dir())

    def test_the_channel_never_writes_through_a_symlink(self):
        outside = self.dir / "outside"
        outside.mkdir()
        for link in (".claude", ".mcp.json"):
            tree = self.dir / f"tree{link}"
            tree.mkdir()
            (tree / link).symlink_to(outside if link == ".claude" else outside / "target.json")
            with self.assertRaises(ShellError, msg=link):
                t3_adapter.write_channel(tree, BINDING, agent="", guard=guard_binding.guard_plugin_dir())
        self.assertEqual(list(outside.iterdir()), [])

    def test_start_registers_the_tree_starts_a_thread_and_writes_the_run_evidence(self):
        tree, run_dir = self.git_tree(), self.dir / "run"
        run_dir.mkdir()
        (self.dir / "userdata").mkdir()
        (self.dir / "userdata" / "environment-id").write_text("env-9")
        session = str(uuid.uuid4())
        request = Request("do the work", model="claude-haiku-5-5", session_id=session, cwd=str(tree), worker=True,
                          plugin_dirs=(str(guard_binding.guard_plugin_dir()),))
        env = {**BINDING, "CHIEF_FAKE_T3": str(self.dir / "state.json")}
        runner = t3_adapter.start(request, run_dir, env=env, cli=str(self.fake_cli()), home=self.dir, poll_s=0.05)
        deadline = time.monotonic() + 20
        while not (run_dir / "exit.json").exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        runner.wait(timeout=5)
        self.assertEqual(json.loads((run_dir / "exit.json").read_text())["exit_code"], 0)
        self.assertEqual(json.loads((run_dir / "thread.json").read_text()),
                         {"thread_id": "mcp:thread-1", "thread_link": "t3code://threads/env-9/mcp:thread-1"})
        self.assertIn("all done", (run_dir / "stdout").read_text())
        state = json.loads((self.dir / "state.json").read_text())
        self.assertEqual(state["projects"], [str(tree)])  # refused once, registered, then started
        start = next(call for call in state["calls"] if call[:2] == ["thread", "start"])
        self.assertIn("claudeAgent", start)
        self.assertIn("claude-haiku-5-5", start)
        self.assertIn(session, state["prompt"].split())  # the session id stands alone, as process identity needs
        self.assertEqual(json.loads((run_dir / "started.json").read_text())["session_id"], session)


if __name__ == "__main__":
    unittest.main()
