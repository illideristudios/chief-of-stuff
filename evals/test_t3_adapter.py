"""The T3 launcher behind the host adapter contract (§13, #1178), against a fake `t3` CLI. No test needs T3."""

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from pathlib import Path

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

    def test_a_thread_ends_when_it_is_idle_and_has_nothing_pending(self):
        running = {"status": "running", "activeRunId": "r", "pendingRequestCount": 0}
        self.assertIsNone(t3_adapter.outcome(running))
        self.assertEqual(t3_adapter.outcome({**running, "status": "completed", "activeRunId": None}), 0)
        self.assertEqual(t3_adapter.outcome({**running, "status": "error", "activeRunId": None}), 1)
        # A thread waiting on a request is waiting, not finished.
        self.assertIsNone(t3_adapter.outcome({"status": "completed", "activeRunId": None, "pendingRequestCount": 1}))

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
