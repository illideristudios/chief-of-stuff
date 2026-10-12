"""Worktree workers, verified starts and completion from engine-owned evidence."""

import json
import os
import sys
import subprocess
import tempfile
import time
import unittest
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import engine
import claude_adapter
import forge_review
import process_status
import reconcile
import slots
import store
import supervisor
import worker
from evals.test_engine import KEY, NOW, policies


class WorkerTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.trees = self.root / "trees"
        self.trees.mkdir()
        self.tree = self.trees / "issue-1-implement-1"
        self.tree.mkdir()
        path = self.root / "store.db"
        store.migrate(path)
        self.db = store.Store(path, clock=lambda: NOW)
        self.addCleanup(self.db.engine.dispose)
        self.db.upsert_item(KEY, title="Build a thing", stage="implement")
        self.p = policies()
        self.persona = self.p.personas["builder"]
        self.forge = SimpleNamespace(
            issue=lambda n: SimpleNamespace(ok=True, issue=SimpleNamespace(title="Forge title", body="Issue body")),
            closing_request=lambda key: SimpleNamespace(branch="feature/existing"))

    def launch(self, stage="implement", attempt=1):
        return worker.launch(KEY, stage, attempt, None, store=self.db, root=self.root,
                             trees=self.trees, policies=self.p, forge=self.forge, clock=lambda: NOW)

    def start(self, request, run_dir, **kwargs):
        (run_dir / "started.json").write_text(json.dumps({"pid": 77}))
        return SimpleNamespace(pid=78, poll=lambda: None)

    def launched(self, stage="implement", attempt=1):
        with mock.patch.object(worker.make_worktree, "build", return_value=SimpleNamespace(path=self.tree)), \
             mock.patch.object(worker.claude_adapter, "start", side_effect=self.start), \
             mock.patch.object(worker.process_status, "identity", return_value="process-start"):
            self.launch(stage, attempt)
        slots.claim(self.trees, self.db, issue_key=KEY, stage=stage, attempt=attempt,
                    max_concurrency=1, clock=lambda: NOW, launch=lambda: ("example", ""))
        return Path(self.db.resume(KEY)["runs"][f"{stage}-{attempt}"]["run_dir"])

    def finish(self, outcome):
        run = self.launched()
        text = "done" if outcome == "done" else "bad" if outcome == "failed" else "429 quota exceeded; resets in 2 hours"
        (run / "stdout").write_text(json.dumps({"is_error": outcome != "done", "result": text}))
        (run / "stderr").write_text("")
        (run / "exit.json").write_text(json.dumps({"exit_code": 0 if outcome == "done" else 1}))
        with mock.patch.object(worker.process_status, "identity", return_value=None):
            worker.collect(self.db, self.root, lambda: NOW + timedelta(minutes=1))
        return self.db.segments(KEY)[0]

    def test_assignment_has_fixed_header_row_issue_job_and_persona_rules(self):
        persona = SimpleNamespace(role="builder", rules=("Tests first.",))
        row = self.db.items()[0]
        prompt = worker.compose(row, "Forge title", "Issue body", "implement", persona)
        self.assertTrue(prompt.startswith(worker.HEADER))
        for text in (KEY, row.title, "Forge title", "Issue body", "implement", "builder", "Tests first.",
                     "commit small and often, never merge"):
            self.assertIn(text, prompt)

    def test_assignment_refuses_peer_row(self):
        self.db.upsert_item(KEY, title="Build (via peer)")
        with self.assertRaisesRegex(ValueError, "via"):
            worker.compose(self.db.items()[0], "title", "body", "implement", self.persona)

    def test_verification_failure_records_no_segment_or_run(self):
        with mock.patch.object(worker.make_worktree, "build", return_value=SimpleNamespace(path=self.tree)), \
             mock.patch.object(worker.claude_adapter, "start", side_effect=self.start), \
             mock.patch.object(worker.claude_adapter, "cancel"), \
             mock.patch.object(worker.time, "monotonic", side_effect=[0, 10]), \
             mock.patch.object(worker.process_status, "identity", return_value=None):
            with self.assertRaisesRegex(RuntimeError, "verify"):
                slots.claim(self.trees, self.db, issue_key=KEY, stage="implement", attempt=1,
                            max_concurrency=1, clock=lambda: NOW, launch=self.launch)
        self.assertEqual(self.db.segments(), [])
        self.assertFalse((self.db.resume(KEY) or {}).get("runs"))

    def test_launch_uses_policy_and_engine_run_dir_outside_tree(self):
        run = self.launched()
        record = self.db.resume(KEY)["runs"]["implement-1"]
        self.assertEqual(run, supervisor.state_dir(self.root) / "runs" / "1-implement-1")
        self.assertFalse(run.is_relative_to(self.tree))
        self.assertEqual(record["pid"], 77)
        self.assertEqual(record["identity"], "process-start")
        self.assertEqual(record["started_at"], NOW.isoformat())
        self.assertTrue(record["session_id"])

    def test_review_checks_out_request_branch(self):
        with mock.patch.object(worker.make_worktree, "build", return_value=SimpleNamespace(path=self.tree)) as build, \
             mock.patch.object(worker.claude_adapter, "start", side_effect=self.start), \
             mock.patch.object(worker.process_status, "identity", return_value="start"):
            self.launch("review")
        self.assertEqual(build.call_args.kwargs["from_branch"], "feature/existing")

    def test_rework_reuses_tree_and_branch_without_build(self):
        self.launched()
        self.db.end_segment(KEY, "implement", 1, NOW.isoformat(), "done")
        with mock.patch.object(worker.make_worktree, "build") as build, \
             mock.patch.object(worker.make_worktree, "git", return_value=(0, "work/issue-1-implement-1")), \
             mock.patch.object(worker.claude_adapter, "start", side_effect=self.start) as start, \
             mock.patch.object(worker.process_status, "identity", return_value="start"):
            self.launch("implement", 2)
        build.assert_not_called()
        self.assertEqual(start.call_args.args[0].cwd, str(self.tree))

    def test_collect_done_failed_and_quota(self):
        for outcome in ("done", "failed", "quota"):
            with self.subTest(outcome=outcome):
                if self.db.segments():
                    # Independent store for each outcome.
                    self.db.engine.dispose()
                    self.setUp()
                seg = self.finish(outcome)
                self.assertEqual(seg.outcome, outcome)
                self.assertEqual(seg.ended_at, (NOW + timedelta(minutes=1)).isoformat())
                if outcome == "quota":
                    quota = self.db.resume(KEY)["quota"]
                    self.assertIn("resets in 2 hours", quota["reset"])
                    self.assertEqual(quota["retry_at"], (NOW + timedelta(hours=2, minutes=1)).isoformat())

    def test_alive_worker_stays_open(self):
        self.launched()
        with mock.patch.object(worker.process_status, "identity", return_value="process-start"):
            worker.collect(self.db, self.root, lambda: NOW)
        self.assertIsNone(self.db.segments()[0].ended_at)

    def test_missing_and_unreadable_run_records_end_failed(self):
        self.db.record_segment(KEY, "implement", 1, NOW.isoformat())
        self.db.record_segment(KEY, "review", 1, NOW.isoformat())
        self.db.set_resume(KEY, {"runs": {"review-1": {"run_dir": "/missing"}}})
        worker.collect(self.db, self.root, lambda: NOW)
        self.assertEqual([s.outcome for s in self.db.segments()], ["failed", "failed"])
        self.assertEqual(len(self.db.log(KEY)), 2)

    def test_malformed_run_records_and_result_end_failed(self):
        self.db.record_segment(KEY, "implement", 1, NOW.isoformat())
        self.db.set_resume(KEY, {"runs": ["invalid"]})
        worker.collect(self.db, self.root, lambda: NOW)
        self.assertEqual(self.db.segments()[0].outcome, "failed")
        self.db.set_resume(KEY, {})
        run = self.launched("review")
        (run / "stdout").write_text("broken json")
        (run / "stderr").write_text("adapter error")
        (run / "exit.json").write_text('{"exit_code": 1}')
        with mock.patch.object(worker.process_status, "identity", return_value=None):
            worker.collect(self.db, self.root, lambda: NOW)
        self.assertEqual(self.db.segments()[-1].outcome, "failed")
        self.assertIn("adapter error", self.db.log(KEY)[-1].message)

    def test_empty_identity_is_an_unreadable_record_not_a_live_worker(self):
        self.launched()
        data = self.db.resume(KEY)
        data["runs"]["implement-1"]["identity"] = None
        self.db.set_resume(KEY, data)
        with mock.patch.object(worker.process_status, "identity", return_value=None):
            worker.collect(self.db, self.root, lambda: NOW)
        self.assertEqual(self.db.segments()[0].outcome, "failed")

    def test_quota_waits_then_retries_without_consuming_limit(self):
        self.finish("quota")
        for attempt in range(2, 6):
            self.db.record_segment(KEY, "implement", attempt, (NOW + timedelta(minutes=attempt)).isoformat(),
                                   ended_at=(NOW + timedelta(minutes=attempt + 1)).isoformat(), outcome="quota")
        planned = reconcile.plan(self.db.items(), {KEY: self.db.segments()}, self.p, {}, 1, NOW, {},
                                 quota_resets={KEY: NOW + timedelta(hours=2)})
        self.assertIsInstance(planned[0], reconcile.Wait)
        planned = reconcile.plan(self.db.items(), {KEY: self.db.segments()}, self.p, {}, 1,
                                 NOW + timedelta(hours=3), {}, quota_resets={KEY: NOW + timedelta(hours=2)})
        self.assertEqual(planned, [reconcile.Dispatch(KEY, "implement", "builder", 6)])

    def test_engine_collects_before_reading_forge(self):
        order = []
        self.forge.snapshots = mock.Mock(side_effect=lambda: order.append("forge") or [])
        with mock.patch.object(engine.worker, "collect", side_effect=lambda *a: order.append("collect")), \
             mock.patch.object(engine.cleanup, "execute", return_value=0):
            engine.run_pass(forge=self.forge, store=self.db, policies=self.p, root=self.root,
                            trees=self.trees, launch=self.launch, clock=lambda: NOW)
        self.assertEqual(order[:2], ["collect", "forge"])

    def test_failed_review_start_can_reuse_its_prepared_tree(self):
        with mock.patch.object(worker.make_worktree, "build", return_value=SimpleNamespace(path=self.tree)) as build, \
             mock.patch.object(worker.claude_adapter, "start", side_effect=self.start), \
             mock.patch.object(worker.claude_adapter, "cancel"), \
             mock.patch.object(worker.time, "monotonic", side_effect=[0, 10, 20]), \
             mock.patch.object(worker.process_status, "identity", side_effect=[None, None, "start", "start"]):
            with self.assertRaises(RuntimeError):
                self.launch("review")
            with mock.patch.object(worker.make_worktree, "git", return_value=(0, "head")):
                self.launch("review")
        self.assertEqual(build.call_count, 1)

    def test_collect_does_not_count_a_reused_pid_as_live(self):
        run = self.launched()
        (run / "stdout").write_text('{"is_error": false, "result": "done"}')
        (run / "stderr").write_text("")
        (run / "exit.json").write_text('{"exit_code": 0}')
        with mock.patch.object(worker.process_status, "identity", return_value="another-birth-time"):
            worker.collect(self.db, self.root, lambda: NOW)
        self.assertEqual(self.db.segments()[0].outcome, "done")

    def test_unavailable_process_view_waits_without_ending_a_live_worker(self):
        self.launched()
        with mock.patch.object(worker.process_status, "identity", side_effect=RuntimeError("ps blocked")):
            worker.collect(self.db, self.root, lambda: NOW)
        self.assertIsNone(self.db.segments()[0].ended_at)

    def test_collect_refuses_evidence_redirected_inside_the_tree(self):
        self.launched()
        data = self.db.resume(KEY)
        data["runs"]["implement-1"]["run_dir"] = str(self.tree)
        self.db.set_resume(KEY, data)
        worker.collect(self.db, self.root, lambda: NOW)
        self.assertEqual(self.db.segments()[0].outcome, "failed")

    def test_real_detached_adapter_collects_after_losing_engine_process_handle(self):
        fake = Path(__file__).parent / "fixtures" / "fake_claude.py"
        cli = self.root / "fake-cli"
        cli.write_text(f"#!{sys.executable}\nimport time, runpy\ntime.sleep(0.5)\n"
                       f"runpy.run_path({str(fake.resolve())!r}, run_name='__main__')\n")
        cli.chmod(0o755)
        # The adapter still resolves through its registry, and uses its real argv builder and runner.
        with mock.patch.object(worker.make_worktree, "build", return_value=SimpleNamespace(path=self.tree)), \
             mock.patch.object(claude_adapter, "resolve", return_value=str(cli)), \
             mock.patch.object(worker.process_status, "identity", return_value="fake-start"), \
             mock.patch.dict(os.environ, {"SHELL": "/bin/sh"}):
            slots.claim(self.trees, self.db, issue_key=KEY, stage="implement", attempt=1,
                        max_concurrency=1, clock=lambda: NOW, launch=self.launch)
        record = self.db.resume(KEY)["runs"]["implement-1"]
        runner = worker._runners.pop(record["run_dir"])
        self.addCleanup(lambda: claude_adapter.cancel(runner) if runner.poll() is None else None)
        runner.wait(timeout=5)
        with mock.patch.object(worker.process_status, "identity", return_value=None):
            worker.collect(self.db, self.root, lambda: NOW + timedelta(minutes=1))
        self.assertEqual(self.db.segments()[0].outcome, "done")
        command = json.loads((Path(record["run_dir"]) / "command.json").read_text())["argv"]
        for token in ("-p", "--output-format", "json", "--model", "example", "--session-id", record["session_id"]):
            self.assertIn(token, command)
        self.assertNotIn("--safe-mode", command)

    def test_agent_effort_profile_are_passed_to_adapter(self):
        self.p.personas["builder"] = SimpleNamespace(name="builder", role="build", rules=(),
                                                       agent="specialist", model="opus", effort="high")
        with mock.patch.object(worker.make_worktree, "build", return_value=SimpleNamespace(path=self.tree)), \
             mock.patch.object(worker.claude_adapter, "start", side_effect=self.start) as start, \
             mock.patch.object(worker.process_status, "identity", return_value="start"):
            used = worker.launch(KEY, "implement", 1, "builder", store=self.db, root=self.root,
                                 trees=self.trees, policies=self.p, forge=self.forge, claude_as_profile="secondary")
        self.assertEqual(used, ("opus", "high"))
        self.assertEqual(start.call_args.args[0].agent, "specialist")
        self.assertEqual(start.call_args.kwargs["claude_as_profile"], "secondary")

    def test_adapter_worker_argv_enables_tools_and_named_agent(self):
        request = worker.Request("prompt", worker=True, agent="specialist", effort="high")
        args = claude_adapter.argv(request, "/fake", [])
        self.assertIn("--agent", args)
        self.assertIn("specialist", args)
        self.assertNotIn("--safe-mode", args)

    def test_forge_retains_request_head_branch(self):
        payload = [{"number": 1, "title": "Request", "url": "url", "headRefOid": "sha",
                    "headRefName": "feature/existing"}]
        gh = mock.Mock(return_value=(0, json.dumps(payload), ""))
        self.assertEqual(forge_review.github_requests("example/project", "", gh)[0].branch, "feature/existing")
        self.assertIn("headRefName", gh.call_args.args[0][-1])

    def test_quota_reset_formats_and_backoff(self):
        self.assertEqual(worker.retry_at("resets at 5pm.", NOW), NOW + timedelta(hours=17))
        self.assertEqual(worker.retry_at("resets in about four hours.", NOW), NOW + timedelta(hours=4))
        self.assertEqual(worker.retry_at("reset at 2000-01-01T02:00:00Z", NOW), NOW + timedelta(hours=2))
        self.assertEqual(worker.retry_at("unknown", NOW, 4), NOW + timedelta(minutes=8))
        self.assertEqual(worker.retry_at("reset at 2000-99-99T02:00:00Z", NOW), NOW + timedelta(minutes=1))


class IdentityTest(unittest.TestCase):
    def test_host_identity_waits_for_login_shell_to_exec(self):
        output = 'S Sat Jan 1 00:00:00 2000 /bin/zsh -lic exec "$@" worker /fake --session-id session'
        with mock.patch.object(process_status, "process_exists", return_value=True), \
             mock.patch.object(process_status.subprocess, "run", return_value=SimpleNamespace(returncode=0, stdout=output)):
            self.assertIsNone(process_status.identity(12, "session", host=True))

    def test_live_process_identity(self):
        try:
            process_status.identity(os.getpid(), "probe-session")
        except PermissionError:
            self.skipTest("sandbox blocks ps, including through the configured login shell")
        session = "test-worker-session"
        process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(5)", session])
        try:
            # A launcher may re-exec (macOS venv python does), so poll as worker.launch does.
            deadline = time.monotonic() + 5
            while process_status.identity(process.pid, session) is None and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertIsNotNone(process_status.identity(process.pid, session))
            self.assertIsNone(process_status.identity(process.pid, "wrong-session"))
        finally:
            process.terminate()
            process.wait(timeout=5)

    def test_pid_exists_but_wrong_session_or_zombie_is_not_identity(self):
        for output in ("S Sat Jan 1 00:00:00 2000 other-session", "Z Sat Jan 1 00:00:00 2000 session"):
            with mock.patch.object(process_status, "process_exists", return_value=True), \
                 mock.patch.object(process_status.subprocess, "run", return_value=SimpleNamespace(returncode=0, stdout=output)):
                self.assertIsNone(process_status.identity(12, "session"))


if __name__ == "__main__":
    unittest.main()
