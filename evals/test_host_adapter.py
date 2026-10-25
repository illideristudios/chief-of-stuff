"""Codex, Cursor and Antigravity worker adapters against fake host binaries (§13, #700). No test needs a real host."""

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import forge
import host_adapter
import runtimes
from model_port import Failure, Reply, Request
from shell_setup import ShellError

# §13 (#949): a request's creation time against the run's start. Equal is not before.
OLD = "2026-10-20T00:00:00+00:00"
START = "2026-10-25T12:00:00+00:00"
DURING = "2026-10-25T13:00:00+00:00"

BINDING = {"CHIEF_ISSUE_KEY": "github:o/r#1", "CHIEF_SESSION_ID": "s", "CHIEF_PYTHON": sys.executable}


class HostAdapterTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name).resolve()

    def git_tree(self) -> Path:
        tree = self.dir / "tree"
        tree.mkdir()
        subprocess.run(["git", "init", "-q", str(tree)], check=True)
        return tree

    def test_each_host_builds_its_worker_argv_from_the_registry(self):
        codex = runtimes.get("codex").worker_command("/b/codex", "/t", "gpt-6.1-sol", "high", "PROMPT", ["-c", "x=1"])
        self.assertEqual(codex[:6], ["/b/codex", "-a", "never", "exec", "-C", "/t"])
        self.assertIn("-c", codex)
        self.assertEqual(codex[-4:], ["gpt-6.1-sol", "-c", "model_reasoning_effort=high", "PROMPT"])
        cursor = runtimes.get("cursor").worker_command("/b/agent", "/t", "auto", "", "PROMPT")
        self.assertEqual(cursor, ["/b/agent", "--print", "--force", "--trust", "--approve-mcps", "--workspace", "/t",
                                  "--model", "auto", "PROMPT"])
        agy = runtimes.get("agy").worker_command("/b/agy", "/t", "gemini-4-argon-high", "", "PROMPT")
        self.assertEqual(agy[-2:], ["--print", "PROMPT"])  # agy's --print takes the prompt as its value
        self.assertIn("--dangerously-skip-permissions", agy)

    def test_the_bootstrap_carries_the_session_id_as_its_own_token(self):
        session = str(uuid.uuid4())
        prompt = runtimes.WORKER_BOOTSTRAP.format(assignment="/r/assignment.md", cwd="/t", session=session)
        self.assertIn(session, prompt.split())
        self.assertIn("report", prompt)

    def test_start_runs_the_host_detached_with_shims_first_and_evidence_written(self):
        fake = self.dir / "codex"
        fake.write_text(f"#!{sys.executable}\nimport json, os, sys\n"
                        f"open({str(self.dir / 'argv.json')!r}, 'w').write(json.dumps(sys.argv))\n")
        fake.chmod(0o755)
        run_dir = self.dir / "run"
        run_dir.mkdir()
        cwd = self.dir / "work"
        cwd.mkdir()
        session = str(uuid.uuid4())
        seen = {}

        def login(argv, path_prefix=""):
            seen["prefix"] = path_prefix
            return list(argv)
        with mock.patch.object(host_adapter, "resolve", return_value=str(fake)), \
             mock.patch.object(host_adapter, "login_argv", side_effect=login):
            runner, name = host_adapter.start("codex", Request("Do the issue.", session_id=session, cwd=str(cwd),
                                                               worker=True), run_dir, env=dict(BINDING))
        runner.wait(timeout=30)
        self.assertEqual(name, "")
        deadline = time.monotonic() + 10
        while not (run_dir / "exit.json").exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertEqual(json.loads((run_dir / "exit.json").read_text())["exit_code"], 0)
        self.assertTrue((run_dir / "started.json").exists())
        self.assertEqual((run_dir / "assignment.md").read_text(), "Do the issue.")
        argv = json.loads((self.dir / "argv.json").read_text())
        self.assertIn(session, argv[-1].split())
        self.assertEqual(seen["prefix"], str(run_dir / "bin"))
        for name in host_adapter.SHIMMED:
            self.assertTrue(os.access(run_dir / "bin" / name, os.X_OK))

    def test_the_shim_refuses_what_the_guard_refuses_and_passes_the_rest_to_the_real_tool(self):
        shims = host_adapter.shim_dir(self.dir)
        real = self.dir / "real"
        real.mkdir()
        (real / "git").write_text(f"#!{sys.executable}\nimport sys\nprint('real git', *sys.argv[1:])\n")
        (real / "git").chmod(0o755)
        env = {**os.environ, "PATH": f"{shims}{os.pathsep}{real}", "CHIEF_PROTECTED_BRANCHES": json.dumps(["main"])}
        pushed = subprocess.run([str(shims / "git"), "push", "origin", "main"], env=env, capture_output=True, text=True)
        self.assertNotEqual(pushed.returncode, 0)
        self.assertIn("chief-of-stuff guard", pushed.stderr)
        merged = subprocess.run([str(shims / "git"), "merge", "feature"], env=env, capture_output=True, text=True)
        self.assertNotEqual(merged.returncode, 0)
        status = subprocess.run([str(shims / "git"), "status"], env=env, capture_output=True, text=True)
        self.assertEqual(status.returncode, 0, status.stderr)
        self.assertEqual(status.stdout.strip(), "real git status")

    def test_codex_takes_the_engine_channel_and_its_binding_per_launch(self):
        extra, name = host_adapter.register("codex", Request("p", session_id="s", cwd=str(self.dir)),
                                            {**BINDING, "OTHER": "secret"})
        self.assertEqual(name, "")
        joined = " ".join(extra)
        self.assertIn("mcp_servers.engine.command=", joined)
        self.assertIn("engine_mcp.py", joined)
        self.assertIn('CHIEF_ISSUE_KEY = "github:o/r#1"', joined)
        self.assertNotIn("OTHER", joined)

    def test_codex_may_call_the_engine_tools_under_approval_never(self):
        # Codex runs `-a never`, which refused every report call ("MCP tool call requires approval"), so no Codex
        # run ever recorded an outcome. The engine's own channel is approved; other servers keep Codex's default.
        extra, _ = host_adapter.register("codex", Request("p", session_id="s", cwd=str(self.dir)), BINDING)
        self.assertIn('mcp_servers.engine.default_tools_approval_mode="approve"', extra)

    def test_cursor_gets_a_per_run_config_with_the_binding_kept_out_of_commits(self):
        tree = self.git_tree()
        request = Request("p", session_id="s", cwd=str(tree))
        host_adapter.register("cursor", request, {**BINDING, "OTHER": "secret"})
        config = json.loads((tree / ".cursor" / "mcp.json").read_text())["mcpServers"]["engine"]
        self.assertEqual(config["env"], BINDING)
        self.assertIn("/.cursor/mcp.json", (tree / ".git" / "info" / "exclude").read_text().splitlines())
        self.assertEqual(subprocess.run(["git", "status", "--porcelain"], cwd=tree, capture_output=True,
                                        text=True).stdout, "")
        with self.assertRaisesRegex(ShellError, "already has"):
            host_adapter.register("cursor", request, BINDING)

    def test_a_cursor_retry_replaces_the_engines_config_from_an_earlier_run(self):
        # drago 2026-10-19: seven Cursor retries failed to start because the earlier run's config was still there.
        tree = self.git_tree()
        host_adapter.register("cursor", Request("p", session_id="s1", cwd=str(tree)), BINDING)
        retry = {**BINDING, "CHIEF_ATTEMPT": "2", "CHIEF_SESSION_ID": "s2"}
        host_adapter.register("cursor", Request("p", session_id="s2", cwd=str(tree)), retry)
        self.assertEqual(json.loads((tree / ".cursor" / "mcp.json").read_text())["mcpServers"]["engine"]["env"], retry)

    def test_a_cursor_retry_after_a_release_install_replaces_the_older_versions_config(self):
        # Architect review of #973: the earlier run's args name another release's engine_mcp.py.
        tree = self.git_tree()
        (tree / ".cursor").mkdir()
        older = {"command": sys.executable, "args": ["/v/0.129.99-abc/scripts/engine_mcp.py"], "env": BINDING}
        (tree / ".cursor" / "mcp.json").write_text(json.dumps({"mcpServers": {"engine": older}}))
        retry = {**BINDING, "CHIEF_SESSION_ID": "s2"}
        host_adapter.register("cursor", Request("p", session_id="s2", cwd=str(tree)), retry)
        self.assertEqual(json.loads((tree / ".cursor" / "mcp.json").read_text())["mcpServers"]["engine"]["env"], retry)

    def test_a_cursor_config_the_engine_did_not_write_is_never_replaced(self):
        tree = self.git_tree()
        (tree / ".cursor").mkdir()
        (tree / ".cursor" / "mcp.json").write_text(json.dumps({"mcpServers": {"theirs": {"command": "x"}}}))
        with self.assertRaisesRegex(ShellError, "already has"):
            host_adapter.register("cursor", Request("p", session_id="s", cwd=str(tree)), BINDING)

    def test_agy_adds_a_uniquely_named_entry_and_cleanup_removes_it(self):
        calls = []
        with mock.patch.object(host_adapter, "resolve", return_value="/b/agy"), \
             mock.patch.object(host_adapter, "login_argv", side_effect=lambda argv, **_: list(argv)), \
             mock.patch.object(host_adapter.subprocess, "run",
                               side_effect=lambda argv, **_: calls.append(argv) or SimpleNamespace(returncode=0)):
            extra, name = host_adapter.register("agy", Request("p", session_id="abc", cwd=str(self.dir)), BINDING)
            host_adapter.cleanup({"agy_mcp": name})
        self.assertEqual((extra, name), ([], "chief-abc"))
        add, remove = calls
        self.assertEqual(add[:3], ["/b/agy", "mcp", "add"])
        self.assertIn("CHIEF_ISSUE_KEY=github:o/r#1", add)
        self.assertEqual(add[add.index("chief-abc") + 1], sys.executable)
        self.assertEqual(remove, ["/b/agy", "mcp", "remove", "chief-abc"])

    def test_each_exit_maps_to_the_port_result_shape(self):
        self.assertEqual(host_adapter.classify("codex", 1, "", "429 RESOURCE_EXHAUSTED quota; resets in 2h").kind, "quota")
        self.assertTrue(host_adapter.classify("agy", 1, "error 429 quota exceeded", "").retryable)
        self.assertIsInstance(host_adapter.classify("cursor", 0, "ok", ""), Reply)
        self.assertEqual(host_adapter.classify("codex", 127, "", "not found").kind, "missing_program")
        unknown = host_adapter.classify("cursor", 2, "", "connection reset")
        self.assertIsInstance(unknown, Failure)
        self.assertEqual(unknown.kind, "unknown")

    def check(self, *, after, commits=(), recorded=(), fetch=0, rev_list=0, branch="work/issue-1-plan-1",
              started_at=START, requests=None, unreadable=False):
        """§13 against a fake git and a fake forge. `requests` is a list of (merge sha, created_at) or a dict keyed
        by commit; None means the workspace has no forge. `unreadable` makes the forge call fail."""
        record = {"tree": str(self.dir), "runtime": "codex", "default_branch": "main", "default_head": "a" * 40,
                  "branch": branch, "started_at": started_at}
        store = SimpleNamespace(items=lambda: [SimpleNamespace(issue_key="k"), SimpleNamespace(issue_key="other")],
                                resume=lambda key: {"merges": [{"request": 2, "sha": s, "at": "t"} for s in recorded]}
                                if key == "other" else {"review_findings": {}})
        self.forge_asks, self.rev_args = [], []

        def git(args, cwd):
            if args[0] == "fetch":
                return fetch, ""
            if args[0] == "ls-remote":
                return (0, f"{after}\trefs/heads/main") if after else (0, "")
            if args[0] == "rev-list":
                self.rev_args.append(args)
                return rev_list, "\n".join(commits)
            raise AssertionError(args)

        def requests_for_commit(sha):
            self.forge_asks.append(sha)
            if unreadable:
                raise forge.ForgeError("unreadable")
            if isinstance(requests, dict):
                return list(requests.get(sha, []))
            return list(requests or [])

        source = None if requests is None and not unreadable else SimpleNamespace(requests_for_commit=requests_for_commit)
        with mock.patch.object(host_adapter.make_worktree, "git_trusted", side_effect=git), \
             mock.patch("forge.for_workspace", return_value=source):
            return host_adapter.moved_outside_engine(store, record, "now")

    def test_an_unmoved_default_branch_or_only_engine_merges_pass(self):
        self.assertEqual(self.check(after="a" * 40), "")
        # Another issue's engine merge during the run is the engine's, and is excused by its recorded commit.
        self.assertEqual(self.check(after="m" * 40, commits=["m" * 40], recorded=["m" * 40]), "")

    def test_any_commit_the_engine_did_not_record_holds_and_is_named(self):
        # A worker's own merge or push beside an engine merge of another issue must not be masked by it.
        held = self.check(after="w" * 40, commits=["w" * 40, "m" * 40], recorded=["m" * 40])
        self.assertIn("w" * 12, held)
        self.assertNotIn("m" * 12, held)
        self.assertIn("holding for human review", self.check(after="p" * 40, commits=["p" * 40]))

    def test_a_worker_merge_into_a_non_default_protected_branch_holds_beside_an_engine_merge_on_default(self):
        # #860: chief-of-stuff merges into develop while its default is main; every protected branch is checked.
        record = {"tree": str(self.dir), "runtime": "codex",
                  "branch_heads": {"main": "a" * 40, "develop": "b" * 40}}
        after = {"main": "m" * 40, "develop": "w" * 40}
        store = SimpleNamespace(items=lambda: [SimpleNamespace(issue_key="other")],
                                resume=lambda key: {"merges": [{"request": 2, "sha": "m" * 40, "at": "t"}]})

        def git(args, cwd):
            if args[0] == "fetch":
                return 0, ""
            if args[0] == "ls-remote":
                return 0, "\n".join(f"{after[ref.rsplit('/', 1)[1]]}\t{ref}" for ref in args[2:])
            if args[0] == "rev-list":
                return 0, {"a" * 40 + ".." + "m" * 40: "m" * 40, "b" * 40 + ".." + "w" * 40: "w" * 40}[args[2]]
            raise AssertionError(args)
        with mock.patch.object(host_adapter.make_worktree, "git_trusted", side_effect=git):
            held = host_adapter.moved_outside_engine(store, record, "now")
        self.assertIn("develop gained commits the engine did not merge: " + "w" * 12, held)
        self.assertNotIn("main gained", held)

    def test_branch_heads_reads_every_branch_in_one_call_and_marks_absent_ones(self):
        calls = []

        def git(args, cwd):
            calls.append(args)
            return 0, f"{'a' * 40}\trefs/heads/main\n{'b' * 40}\trefs/heads/develop"
        with mock.patch.object(host_adapter.make_worktree, "git_trusted", side_effect=git):
            heads = host_adapter.branch_heads(self.dir, ["main", "develop", "release", "main"])
        self.assertEqual(heads, {"main": "a" * 40, "develop": "b" * 40, "release": ""})
        self.assertEqual(len(calls), 1)

    def test_an_unreadable_branch_or_range_holds(self):
        self.assertIn("cannot verify", self.check(after="b" * 40, fetch=1))
        self.assertIn("cannot verify", self.check(after=""))
        self.assertIn("cannot list", self.check(after="b" * 40, rev_list=128))

    def test_a_request_opened_before_the_run_does_not_hold(self):
        # #949: the forge records the commit as the merge of a request opened before the run. The subject is not read.
        sha = "c" * 40
        self.assertEqual(self.check(after=sha, commits=[sha], requests=[(sha, OLD)]), "")
        self.assertEqual(self.rev_args, [["rev-list", "--first-parent", f"{'a' * 40}..{sha}"]])
        recorded = "b" * 40
        self.assertEqual(self.check(after=sha, commits=[sha, recorded], recorded=[recorded],
                                    requests={sha: [(sha, OLD)]}), "")
        self.assertEqual(self.forge_asks, [sha])

    def test_a_request_opened_during_the_run_holds_and_is_named(self):
        sha = "d" * 40
        # Two parents and another branch's subject still hold when the forge says the request opened during the run.
        line = f"{sha} {'a' * 40} {'b' * 40}\tMerge pull request #946 from illideristudios/docs/decision-pages-945"
        held = self.check(after=sha, commits=[line], requests=[(sha, DURING)])
        self.assertIn(sha[:12], held)
        self.assertIn("holding for human review", held)
        same = self.check(after=sha, commits=[sha], requests=[(sha, START)])
        self.assertIn(sha[:12], same)

    def test_an_unreadable_forge_record_holds(self):
        sha = "e" * 40
        self.assertIn(sha[:12], self.check(after=sha, commits=[sha], unreadable=True))
        self.assertIn(sha[:12], self.check(after=sha, commits=[sha], requests=None))
        self.assertIn(sha[:12], self.check(after=sha, commits=[sha], requests=[(sha, OLD)], started_at=""))

    def test_a_commit_with_a_pr_subject_but_no_forge_record_holds(self):
        sha = "f" * 40
        line = f"{sha} {'a' * 40} {'b' * 40}\tMerge pull request #946 from illideristudios/docs/decision-pages-945"
        self.assertIn(sha[:12], self.check(after=sha, commits=[line], requests=[]))
        # Associated with a request whose merge commit is a different sha.
        self.assertIn(sha[:12], self.check(after=sha, commits=[sha], requests=[("1" * 40, OLD)]))

    def test_moved_outside_engine_asks_the_forge_about_a_real_merge_commit(self):
        origin = self.dir / "origin.git"
        self.assertEqual(host_adapter.make_worktree.git_trusted(["init", "-q", "--bare", str(origin)], self.dir)[0], 0)
        tree = self.git_tree()

        def git(*args):
            code, out = host_adapter.make_worktree.git_trusted(
                ["-c", "user.name=t", "-c", "user.email=t@t", *args], tree)
            self.assertEqual(code, 0, out)
            return out.strip()

        def pr_merge(title):
            git("checkout", "-q", "-b", "side", "develop")
            git("commit", "-q", "--allow-empty", "-m", "side work")
            git("checkout", "-q", "develop")
            git("merge", "-q", "--no-ff", "-m", title, "side")
            git("branch", "-q", "-D", "side")
            git("push", "-q", "origin", "develop")
            return git("rev-parse", "HEAD")
        git("checkout", "-q", "-b", "develop")
        git("commit", "-q", "--allow-empty", "-m", "start")
        git("remote", "add", "origin", str(origin))
        git("push", "-q", "origin", "develop")
        record = {"tree": str(tree), "runtime": "codex", "branch": "work/issue-949-plan-1", "started_at": START,
                  "branch_heads": {"develop": git("rev-parse", "HEAD")}}
        store = SimpleNamespace(items=lambda: [])
        records, asked = {}, []

        def requests_for_commit(sha):
            asked.append(sha)
            return list(records.get(sha, []))

        with mock.patch("forge.for_workspace", return_value=SimpleNamespace(requests_for_commit=requests_for_commit)):
            first = pr_merge("Merge pull request #946 from illideristudios/docs/decision-pages-945")
            records[first] = [(first, OLD)]
            self.assertEqual(host_adapter.moved_outside_engine(store, record, "now"), "")
            self.assertEqual(asked, [first])
            second = pr_merge("Merge pull request #949 from illideristudios/work/issue-949-plan-1")
            records[second] = [(second, DURING)]
            held = host_adapter.moved_outside_engine(store, record, "now")
        self.assertIn(f"develop gained commits the engine did not merge: {second[:12]} during", held)
        self.assertNotIn(first[:12], held)

    def test_an_unknown_host_has_no_adapter(self):
        with self.assertRaisesRegex(ValueError, "no host adapter"):
            host_adapter.start("claude", Request("p"), self.dir, env={})



SCHEMA = {"type": "object", "properties": {"vote": {"type": "string"}}, "required": ["vote"]}


class JudgeTest(unittest.TestCase):
    """#879 (§6): a trio consult on Codex, Cursor or agy runs read-only and returns the host's structured answer."""

    def judge(self, runtime, stdout, answer=None, returncode=0):
        seen = {}

        def run(argv, **kwargs):
            seen["argv"], seen["cwd"] = argv, kwargs["cwd"]
            if answer is not None:  # codex writes its answer to -o
                Path(argv[argv.index("-o") + 1]).write_text(json.dumps(answer))
            return SimpleNamespace(returncode=returncode, stdout=stdout, stderr="")

        with mock.patch.object(host_adapter, "resolve", return_value=f"/b/{runtime}"), \
             mock.patch.object(host_adapter, "login_argv", side_effect=lambda argv, **_: list(argv)), \
             mock.patch.object(host_adapter.subprocess, "run", side_effect=run):
            got = host_adapter.judge(Request("Vote?", model="m", runtime=runtime, schema=SCHEMA, timeout_s=5))
        return got, seen

    def test_each_host_judges_in_its_read_only_mode(self):
        for runtime, flags in (("codex", ["--sandbox", "read-only"]), ("cursor", ["--mode", "ask"]),
                               ("agy", ["--mode", "plan"])):
            with self.subTest(runtime=runtime):
                _, seen = self.judge(runtime, '{"status": "SUCCESS", "structured_output": {}, "result": "{}"}',
                                     answer={} if runtime == "codex" else None)
                argv = seen["argv"]
                self.assertTrue(any(argv[i:i + 2] == flags for i in range(len(argv))), argv)
                for unsafe in ("--force", "--dangerously-skip-permissions", "workspace-write", "accept-edits"):
                    self.assertNotIn(unsafe, argv)
                self.assertIn("m", argv)  # the drawn model
        _, seen = self.judge("agy", '{"status": "SUCCESS", "structured_output": {"vote": "adopt"}}')
        self.assertTrue(seen["argv"][-1].startswith("--print=Vote?"))  # agy takes the prompt attached to --print

    def test_each_hosts_structured_answer_is_read_from_its_own_shape(self):
        got, _ = self.judge("codex", "", answer={"vote": "adopt"})
        self.assertEqual(got.data, {"vote": "adopt"})
        got, _ = self.judge("agy", json.dumps({"status": "SUCCESS", "structured_output": {"vote": "adopt"}}))
        self.assertEqual(got.data, {"vote": "adopt"})
        got, _ = self.judge("cursor", json.dumps({"is_error": False, "result": 'I looked. {"vote": "adopt"}'}))
        self.assertEqual(got.data, {"vote": "adopt"})

    def test_a_quota_line_is_a_quota_stop_and_no_answer_is_an_error(self):
        got, _ = self.judge("codex", "error 429 RESOURCE_EXHAUSTED quota", returncode=1)
        self.assertEqual(got.kind, "quota")
        got, _ = self.judge("cursor", json.dumps({"is_error": True, "result": "failed"}))
        self.assertEqual(got.kind, "error")

    def test_a_host_with_no_read_only_mode_or_no_cli_cannot_judge(self):
        self.assertEqual(host_adapter.judge(Request("Vote?", runtime="claude")).kind, "missing_program")
        with mock.patch.object(host_adapter, "resolve", return_value=None):
            self.assertEqual(host_adapter.judge(Request("Vote?", runtime="codex")).kind, "missing_program")

if __name__ == "__main__":
    unittest.main()
