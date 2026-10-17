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
import cleanup
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
        worker._runners.clear()
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
            closing_request=lambda key: SimpleNamespace(branch="feature/existing", head="head"),
            protected_branches=lambda: ("trunk", "main"))
        git = mock.patch.object(worker.make_worktree, "git", return_value=(0, "head"))
        git.start()
        self.addCleanup(git.stop)

    def launch(self, stage="implement", attempt=1):
        return worker.launch(KEY, stage, attempt, None, store=self.db, root=self.root,
                             trees=self.trees, policies=self.p, forge=self.forge, clock=lambda: NOW)

    def start(self, request, run_dir, **kwargs):
        (run_dir / "started.json").write_text(json.dumps({
            "pid": 77, "runner_pid": 78, "session_id": request.session_id}))
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
        if outcome == "done":
            self.report(run)
        (run / "exit.json").write_text(json.dumps({"exit_code": 0 if outcome == "done" else 1}))
        with mock.patch.object(worker.process_status, "identity", return_value=None):
            worker.collect(self.db, self.root, lambda: NOW + timedelta(minutes=1))
        return self.db.segments(KEY)[0]

    def test_assignment_has_fixed_header_row_issue_job_and_persona_rules(self):
        persona = SimpleNamespace(role="builder", rules=("Tests first.",))
        row = self.db.items()[0]
        prompt = worker.dispatch_prompt.compose_issue(row, "Forge title", "Issue body", "implement", persona)
        self.assertTrue(prompt.startswith(worker.dispatch_prompt.ISSUE_HEADER))
        for text in (KEY, row.title, "Forge title", "Issue body", "implement", "builder", "Tests first.",
                     "commit small and often, never merge"):
            self.assertIn(text, prompt)

    def test_assignment_refuses_peer_row(self):
        self.db.upsert_item(KEY, title="Build (via peer)")
        with self.assertRaisesRegex(ValueError, "via"):
            worker.dispatch_prompt.compose_issue(self.db.items()[0], "title", "body", "implement", self.persona)

    def test_issue_assignments_include_prevalidated_workspace_rules_for_every_stage(self):
        dp = worker.dispatch_prompt
        for stage in ("plan", "implement", "review", "fix"):
            with self.subTest(stage=stage):
                prompt = dp.compose_issue(self.db.items()[0], "title", "body", stage, self.persona,
                                          rules=("Keep tests.", "Keep <your_ref_or_name> literal."))
                self.assertIn("\n\nStanding worker rules:\n\n- Keep tests.\n- Keep <your_ref_or_name> literal.\n\n", prompt)
                self.assertIn("workspace settings (`[workers] rules`)", prompt)
                self.assertLess(prompt.index("- Keep tests."), prompt.index("Issue:"))

    def test_issue_assignment_header_still_bounds_standing_rules(self):
        dp = worker.dispatch_prompt
        with self.assertRaisesRegex(dp.RefusedError, "over the .* cap"):
            dp.compose_issue(self.db.items()[0], "title", "body", "implement", self.persona, rules=("x" * 2000,) * 9)

    def test_large_forge_issues_are_preserved_with_or_without_standing_rules(self):
        dp = worker.dispatch_prompt
        title = "Forge title " + "t" * dp.BODY_CAP
        body = "Issue body " + "b" * dp.BODY_CAP
        for stage in ("plan", "implement", "review", "fix"):
            for rules in ((), ("Keep tests.",)):
                with self.subTest(stage=stage, rules=rules):
                    prompt = dp.compose_issue(self.db.items()[0], title, body, stage, self.persona, rules=rules)
                    self.assertTrue(prompt.endswith(f"\n## Forge issue\n\n{title}\n\n{body}\n"))
                    self.assertEqual("Standing worker rules:" in prompt, bool(rules))

    def test_launch_passes_workspace_rules_to_the_host_assignment(self):
        with mock.patch.object(worker.make_worktree, "build", return_value=SimpleNamespace(path=self.tree)), \
             mock.patch.object(worker.claude_adapter, "start", side_effect=self.start) as start, \
             mock.patch.object(worker.process_status, "identity", return_value="start"):
            worker.launch(KEY, "implement", 1, None, store=self.db, root=self.root,
                          trees=self.trees, policies=self.p, forge=self.forge, rules=("Keep tests.",), clock=lambda: NOW)
        self.assertIn("- Keep tests.\n", start.call_args.args[0].prompt)

    def test_implement_assignment_requires_delivery_before_reporting_done(self):
        prompt = worker.dispatch_prompt.compose_issue(self.db.items()[0], "title", "body", "implement", self.persona)
        self.assertIn(worker.dispatch_prompt.COMMITS, prompt)
        self.assertIn("Closes #1", prompt)
        self.assertIn("before reporting done", prompt)
        review = worker.dispatch_prompt.compose_issue(self.db.items()[0], "title", "body", "review", self.persona)
        self.assertNotIn(worker.dispatch_prompt.COMMITS, review)

    def test_fix_assignment_pushes_existing_request_before_reporting_done(self):
        prompt = worker.dispatch_prompt.compose_issue(self.db.items()[0], "title", "body", "fix", self.persona)
        self.assertIn("push the existing request branch", prompt)
        self.assertIn("do not open a new request", prompt)
        self.assertIn("before reporting done", prompt)

    def test_review_assignment_names_code_review_skill_and_report_rule(self):
        prompt = worker.dispatch_prompt.compose_issue(self.db.items()[0], "title", "body", "review", self.persona)
        self.assertIn("Review: run the extras:code-review skill on the request; report findings and do not fix.",
                      prompt)
        self.assertIn(worker.dispatch_prompt.REVIEW_RULE, prompt)
        self.assertIn("review-report.md", prompt)

    def test_launch_implement_and_fix_receive_workspace_architecture(self):
        self.p.stages["fix"] = self.p.stages["implement"]
        (self.root / "CLAUDE.md").write_text(
            "## Architecture\n- Canonical document: `design/ARCHITECTURE.md`; see `other.md`.\n")
        # A worker checkout's declaration must not replace the workspace's document.
        (self.tree / "CLAUDE.md").write_text(
            "## Architecture\n- Canonical document: `wrong.md`\n")
        with mock.patch.object(worker.make_worktree, "build", return_value=SimpleNamespace(path=self.tree)), \
             mock.patch.object(worker.make_worktree, "git", return_value=(0, "work/issue-1-implement-1")), \
             mock.patch.object(worker.claude_adapter, "start", side_effect=self.start) as start, \
             mock.patch.object(worker.process_status, "identity", return_value="start"):
            for stage in ("implement", "fix"):
                with self.subTest(stage=stage):
                    self.launch(stage)
                    prompt = start.call_args.args[0].prompt
                    self.assertIn(f"Architecture: {self.root / 'design/ARCHITECTURE.md'}", prompt)
                    self.assertIn("read it before changing code", prompt)
                    self.assertNotIn("wrong.md", prompt)

    def test_assignment_omits_architecture_without_a_canonical_document(self):
        for text in (None, "", "## Coordinator\n- Canonical document: `wrong.md`\n",
                     "## Architecture\n- Architecture directory: `design/`\n",
                     "## Architecture\n- Canonical document: not a backticked path\n"):
            with self.subTest(text=text):
                if text is not None:
                    (self.root / "CLAUDE.md").write_text(text)
                prompt = worker.dispatch_prompt.compose_issue(self.db.items()[0], "title", "body", "implement",
                                                             self.persona, root=self.root)
                self.assertNotIn("Architecture:", prompt)

    def test_runner_spawn_failure_allows_retry_on_the_same_tree_and_attempt(self):
        with mock.patch.object(worker.make_worktree, "build", return_value=SimpleNamespace(path=self.tree)) as build, \
             mock.patch.object(claude_adapter, "command", return_value=["/fake"]), \
             mock.patch.object(claude_adapter.subprocess, "Popen", side_effect=OSError("spawn failed")):
            with self.assertRaisesRegex(OSError, "spawn failed"):
                self.launch()
        self.assertEqual(self.db.segments(), [])
        self.assertFalse(self.db.resume(KEY).get("runs"))
        with mock.patch.object(worker.make_worktree, "build") as rebuild, \
             mock.patch.object(worker.make_worktree, "git", return_value=(0, "work/issue-1-implement-1")), \
             mock.patch.object(worker.claude_adapter, "start", side_effect=self.start) as start, \
             mock.patch.object(worker.process_status, "identity", return_value="start"):
            self.launch()
        build.assert_called_once()
        rebuild.assert_not_called()
        self.assertEqual(start.call_args.args[0].cwd, str(self.tree))

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

    def test_reviewed_head_round_trips_through_run_and_collected_result(self):
        run = self.launched("review")
        record = self.db.resume(KEY)["runs"]["review-1"]
        self.assertEqual(record.get("reviewed_head"), "head")
        # Completion must use the launch's pinned SHA, even after the request moves.
        self.forge.closing_request = mock.Mock(return_value=SimpleNamespace(branch="feature/existing", head="new-head"))
        data = self.db.resume(KEY)
        data["review_trees"]["1"]["head"] = "new-head"
        self.db.set_resume(KEY, data)
        (run / "stdout").write_text('{"is_error": false, "result": "reviewed"}')
        (run / "stderr").write_text("")
        self.report(run, stage="review")
        (run / "exit.json").write_text('{"exit_code": 0}')
        with mock.patch.object(worker.process_status, "identity", return_value=None):
            worker.collect(self.db, self.root, lambda: NOW + timedelta(minutes=1))
        reopened = store.Store(self.root / "store.db")
        self.addCleanup(reopened.engine.dispose)
        self.assertEqual(reopened.resume(KEY)["runs"]["review-1"]["result"], {
            "reviewed_head": "head", "outcome": "done", "ended_at": (NOW + timedelta(minutes=1)).isoformat()})
        self.assertEqual(reopened.segments(KEY)[0].outcome, "done")
        self.forge.closing_request.assert_not_called()

    def test_generated_trees_have_cleanup_ownership_even_after_refused_start(self):
        for stage in ("implement", "review"):
            with self.subTest(stage=stage):
                tree = self.trees / f"owned-{stage}"
                tree.mkdir()
                with mock.patch.object(worker.make_worktree, "build", return_value=SimpleNamespace(path=tree)), \
                     mock.patch.object(worker.claude_adapter, "start", side_effect=RuntimeError("refused")):
                    with self.assertRaisesRegex(RuntimeError, "refused"):
                        self.launch(stage)
                self.assertIn(str(tree), [row.path for row in self.db.ownership(KEY)])
                self.db.upsert_item(KEY, forge_state="closed")
                items, guards = cleanup.owner_guards(tree, cleanup.owned_items(self.root, self.db))
                self.assertEqual([item.issue_key for item in items], [KEY])
                self.assertEqual(guards, [])

    def test_restart_adopts_live_run_before_slot_record_with_or_without_resume_record(self):
        for recorded in (True, False):
            with self.subTest(recorded=recorded):
                if self.db.resume(KEY):
                    self.db.engine.dispose()
                    self.setUp()
                with mock.patch.object(worker.make_worktree, "build", return_value=SimpleNamespace(path=self.tree)), \
                     mock.patch.object(worker.claude_adapter, "start", side_effect=self.start), \
                     mock.patch.object(worker.process_status, "identity", return_value="process-start"):
                    self.launch()
                data = self.db.resume(KEY)
                record = data["runs"]["implement-1"]
                run = Path(record["run_dir"])
                if not recorded:
                    data.pop("runs")
                    self.db.set_resume(KEY, data)
                worker._runners.pop(str(run), None)
                with mock.patch.object(worker.make_worktree, "build") as build, \
                     mock.patch.object(worker.make_worktree, "git", return_value=(0, record["branch"])) as git, \
                     mock.patch.object(worker.claude_adapter, "start", side_effect=self.start) as start, \
                     mock.patch.object(worker.process_status, "identity", return_value="process-start") as identity:
                    slots.claim(self.trees, self.db, issue_key=KEY, stage="implement", attempt=1,
                                max_concurrency=1, clock=lambda: NOW, launch=self.launch)
                start.assert_not_called()
                build.assert_not_called()
                git.assert_not_called()
                identity.assert_any_call(77, record["session_id"], host=True)
                identity.assert_any_call(78, record["session_id"])
                self.assertEqual(slots.running(self.db), 1)
                self.assertEqual(self.db.resume(KEY)["runs"]["implement-1"], record)
                self.assertEqual(list(run.parent.glob(f"{run.name}-refused-*")), [])
                (run / "stdout").write_text('{"is_error": false, "result": "done"}')
                (run / "stderr").write_text("")
                self.report(run)
                (run / "exit.json").write_text('{"exit_code": 0}')
                with mock.patch.object(worker.process_status, "identity", return_value=None):
                    worker.collect(self.db, self.root, lambda: NOW)
                self.assertEqual(self.db.segments()[0].outcome, "done")

    def test_dead_or_reused_run_is_preserved_as_refused_before_relaunch(self):
        for recorded, old_identity in ((True, None), (True, "different-birth"), (False, None)):
            with self.subTest(recorded=recorded, old_identity=old_identity):
                if self.db.resume(KEY):
                    self.db.engine.dispose()
                    self.setUp()
                with mock.patch.object(worker.make_worktree, "build", return_value=SimpleNamespace(path=self.tree)), \
                     mock.patch.object(worker.claude_adapter, "start", side_effect=self.start), \
                     mock.patch.object(worker.process_status, "identity", return_value="process-start"):
                    self.launch()
                old = self.db.resume(KEY)["runs"]["implement-1"]
                run = Path(old["run_dir"])
                if not recorded:
                    data = self.db.resume(KEY)
                    data.pop("runs")
                    self.db.set_resume(KEY, data)
                (run / "exit.json").write_text('{"exit_code": "unknown"}')
                with mock.patch.object(worker.make_worktree, "git", return_value=(0, old["branch"])), \
                     mock.patch.object(worker.claude_adapter, "start", side_effect=self.start) as start, \
                     mock.patch.object(worker.process_status, "identity", side_effect=[old_identity, old_identity,
                                                                                          "new-start", "new-start"]):
                    self.launch()
                self.assertEqual(start.call_count, 1)
                refused, = run.parent.glob(f"{run.name}-refused-*")
                self.assertTrue((refused / "exit.json").exists())
                self.assertNotEqual(self.db.resume(KEY)["runs"]["implement-1"]["session_id"], old["session_id"])

    def test_restart_adopts_completed_run_before_slot_record(self):
        for stage in ("implement", "review"):
            for recorded, outcome in ((True, "done"), (False, "done"), (False, "failed"), (False, "quota")):
                with self.subTest(stage=stage, recorded=recorded, outcome=outcome):
                    if self.db.resume(KEY):
                        self.db.engine.dispose()
                        self.setUp()
                    with mock.patch.object(worker.make_worktree, "build", return_value=SimpleNamespace(path=self.tree)), \
                         mock.patch.object(worker.claude_adapter, "start", side_effect=self.start), \
                         mock.patch.object(worker.process_status, "identity", return_value="process-start"):
                        self.launch(stage)
                    data = self.db.resume(KEY)
                    record = data["runs"][f"{stage}-1"]
                    run = Path(record["run_dir"])
                    if not recorded:
                        data.pop("runs")
                        self.db.set_resume(KEY, data)
                    worker._runners.pop(str(run), None)
                    text = "429 quota exceeded; resets in 2 hours" if outcome == "quota" else outcome
                    (run / "stdout").write_text(json.dumps({"is_error": outcome != "done", "result": text}))
                    (run / "stderr").write_text("")
                    if outcome == "done":
                        self.report(run, stage=stage)
                    (run / "exit.json").write_text(json.dumps({"exit_code": 0 if outcome == "done" else 1}))
                    with mock.patch.object(worker.make_worktree, "build", return_value=SimpleNamespace(path=self.tree)) as build, \
                         mock.patch.object(worker.make_worktree, "git", return_value=(0, "head" if stage == "review" else record["branch"])) as git, \
                         mock.patch.object(worker.claude_adapter, "start", side_effect=AssertionError("completed worker must not be relaunched")) as start, \
                         mock.patch.object(worker.process_status, "identity", return_value=None):
                        slots.claim(self.trees, self.db, issue_key=KEY, stage=stage, attempt=1,
                                    max_concurrency=1, clock=lambda: NOW, launch=lambda: self.launch(stage))
                        start.assert_not_called()
                        build.assert_not_called()
                        git.assert_not_called()
                        worker.collect(self.db, self.root, lambda: NOW + timedelta(minutes=1))
                    self.assertEqual(list(run.parent.glob(f"{run.name}-refused-*")), [])
                    self.assertEqual(self.db.resume(KEY)["runs"][f"{stage}-1"]["session_id"], record["session_id"])
                    self.assertEqual(self.db.segments(KEY)[0].outcome, outcome)
                    if outcome == "quota":
                        self.assertEqual(self.db.resume(KEY)["quota"]["retry_at"],
                                         (NOW + timedelta(hours=2, minutes=1)).isoformat())
                    if stage == "review":
                        self.assertEqual(self.db.resume(KEY)["runs"]["review-1"]["result"]["reviewed_head"], "head")

    def test_unverified_existing_run_keeps_evidence_and_refuses_second_worker(self):
        run = self.launched()
        with mock.patch.object(worker.claude_adapter, "start") as start, \
             mock.patch.object(worker.process_status, "identity", side_effect=RuntimeError("ps blocked")):
            with self.assertRaisesRegex(RuntimeError, "ps blocked"):
                self.launch()
        start.assert_not_called()
        self.assertTrue((run / "started.json").exists())
        self.assertEqual(list(run.parent.glob(f"{run.name}-refused-*")), [])

    def test_adopted_host_with_dead_runner_can_be_collected_after_exit(self):
        with mock.patch.object(worker.make_worktree, "build", return_value=SimpleNamespace(path=self.tree)), \
             mock.patch.object(worker.claude_adapter, "start", side_effect=self.start), \
             mock.patch.object(worker.process_status, "identity", return_value="process-start"):
            self.launch()
        data = self.db.resume(KEY)
        run = Path(data.pop("runs")["implement-1"]["run_dir"])
        self.db.set_resume(KEY, data)
        with mock.patch.object(worker.claude_adapter, "start") as start, \
             mock.patch.object(worker.process_status, "identity", side_effect=["process-start", None]):
            slots.claim(self.trees, self.db, issue_key=KEY, stage="implement", attempt=1,
                        max_concurrency=1, clock=lambda: NOW, launch=self.launch)
        start.assert_not_called()
        (run / "stdout").write_text('{"is_error": false, "result": "done"}')
        (run / "stderr").write_text("")
        self.report(run)
        (run / "exit.json").write_text('{"exit_code": 0}')
        with mock.patch.object(worker.process_status, "identity", return_value=None):
            worker.collect(self.db, self.root, lambda: NOW)
        self.assertEqual(self.db.segments()[0].outcome, "done")

    def test_refused_review_tree_is_rebuilt_when_request_head_changes(self):
        with mock.patch.object(worker.make_worktree, "build", return_value=SimpleNamespace(path=self.tree)), \
             mock.patch.object(worker.claude_adapter, "start", side_effect=RuntimeError("refused")):
            with self.assertRaisesRegex(RuntimeError, "refused"):
                self.launch("review")
        self.assertEqual(self.db.resume(KEY)["review_trees"]["1"].get("head"), "head")
        self.forge.closing_request = lambda key: SimpleNamespace(branch="feature/existing", head="new-head")
        fresh = self.trees / "fresh-review"
        fresh.mkdir()
        with mock.patch.object(worker.make_worktree, "build", return_value=SimpleNamespace(path=fresh)) as build, \
             mock.patch.object(worker.make_worktree, "git", side_effect=[(0, "head"), (0, "new-head")]), \
             mock.patch.object(worker.claude_adapter, "start", side_effect=self.start) as start, \
             mock.patch.object(worker.process_status, "identity", return_value="start"):
            self.launch("review")
        build.assert_called_once()
        self.assertNotEqual(build.call_args.args[3], self.tree.name)
        self.assertEqual(start.call_args.args[0].cwd, str(fresh))
        self.assertEqual(self.db.resume(KEY)["review_trees"]["1"]["head"], "new-head")
        self.assertEqual({row.path for row in self.db.ownership(KEY)}, {str(self.tree), str(fresh)})

    def test_live_review_is_adopted_before_changed_head_can_rebuild_its_tree(self):
        with mock.patch.object(worker.make_worktree, "build", return_value=SimpleNamespace(path=self.tree)), \
             mock.patch.object(worker.claude_adapter, "start", side_effect=self.start), \
             mock.patch.object(worker.process_status, "identity", return_value="process-start"):
            self.launch("review")
        self.forge.closing_request = mock.Mock(return_value=SimpleNamespace(branch="feature/existing", head="new-head"))
        with mock.patch.object(worker.make_worktree, "build") as build, \
             mock.patch.object(worker.claude_adapter, "start") as start, \
             mock.patch.object(worker.process_status, "identity", return_value="process-start"):
            slots.claim(self.trees, self.db, issue_key=KEY, stage="review", attempt=1,
                        max_concurrency=1, clock=lambda: NOW, launch=lambda: self.launch("review"))
        build.assert_not_called()
        start.assert_not_called()
        self.forge.closing_request.assert_not_called()
        self.assertEqual(slots.running(self.db), 1)

    def test_review_refuses_a_checkout_that_does_not_match_the_request_sha(self):
        with mock.patch.object(worker.make_worktree, "build", return_value=SimpleNamespace(path=self.tree)), \
             mock.patch.object(worker.make_worktree, "git", return_value=(0, "unexpected-head")), \
             mock.patch.object(worker.claude_adapter, "start") as start:
            with self.assertRaisesRegex(RuntimeError, "does not match the request head"):
                self.launch("review")
        start.assert_not_called()
        self.assertEqual([row.path for row in self.db.ownership(KEY)], [str(self.tree)])

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

    def report(self, run, outcome="done", stage="implement"):
        session = json.loads((run / "started.json").read_text())["session_id"]
        self.db.add_report(KEY, stage, 1, session, outcome, f"reported {outcome}", [])

    def test_successful_host_takes_the_segment_outcome_from_its_session_report(self):
        for outcome, expected in (("done", "done"), ("blocked", "blocked"), ("failed", "failed"), (None, "failed")):
            with self.subTest(report=outcome):
                if self.db.segments():
                    self.db.engine.dispose()
                    self.setUp()
                run = self.launched()
                if outcome:
                    self.report(run, outcome)
                # A stale report from another session must not count.
                self.db.add_report(KEY, "implement", 1, "other-session", "done", "stale", [])
                (run / "stdout").write_text('{"is_error": false, "result": "stopped"}')
                (run / "stderr").write_text("")
                (run / "exit.json").write_text('{"exit_code": 0}')
                with mock.patch.object(worker.process_status, "identity", return_value=None):
                    worker.collect(self.db, self.root, lambda: NOW + timedelta(minutes=1))
                self.assertEqual(self.db.segments(KEY)[0].outcome, expected)

    def test_collect_unknown_backs_off_like_quota(self):
        run = self.launched()
        (run / "stdout").write_text("not json")
        (run / "stderr").write_text("connection reset")
        (run / "exit.json").write_text(json.dumps({"exit_code": 1}))
        with mock.patch.object(worker.process_status, "identity", return_value=None):
            worker.collect(self.db, self.root, lambda: NOW + timedelta(minutes=1))
        seg = self.db.segments(KEY)[0]
        self.assertEqual(seg.outcome, "unknown")
        self.assertEqual(self.db.resume(KEY)["quota"]["retry_at"],
                         (NOW + timedelta(minutes=2)).isoformat())

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
        # No JSON envelope is a retryable unknown (Codex P1), not a hard failed.
        self.assertEqual(self.db.segments()[-1].outcome, "unknown")
        self.assertIn("adapter error", self.db.log(KEY)[-1].message)
        self.assertIn("retry_at", self.db.resume(KEY)["quota"])

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
             mock.patch.object(engine, "_record_limits"), \
             mock.patch.object(engine.cleanup, "execute", return_value=0):
            engine.run_pass(forge=self.forge, store=self.db, policies=self.p, root=self.root,
                            trees=self.trees, launch=self.launch, workers=engine.settings.Workers(), clock=lambda: NOW)
        self.assertEqual(order[:2], ["collect", "forge"])

    def test_failed_review_start_can_reuse_its_prepared_tree(self):
        with mock.patch.object(worker.make_worktree, "build", return_value=SimpleNamespace(path=self.tree)) as build, \
             mock.patch.object(worker.claude_adapter, "start", side_effect=self.start) as start, \
             mock.patch.object(worker.claude_adapter, "cancel"):
            with mock.patch.object(worker.time, "monotonic", side_effect=[0, 10]), \
                 mock.patch.object(worker.process_status, "identity", return_value=None), \
                 self.assertRaises(RuntimeError):
                self.launch("review")
            with mock.patch.object(worker.process_status, "identity", side_effect=[None, None, "start", "start"]):
                self.launch("review")
        self.assertEqual(build.call_count, 1)
        self.assertEqual(start.call_count, 2)
        self.assertEqual(start.call_args.args[0].cwd, str(self.tree))

    def test_collect_does_not_count_a_reused_pid_as_live(self):
        run = self.launched()
        (run / "stdout").write_text('{"is_error": false, "result": "done"}')
        (run / "stderr").write_text("")
        self.report(run)
        (run / "exit.json").write_text('{"exit_code": 0}')
        with mock.patch.object(worker.process_status, "identity", return_value="another-birth-time"):
            worker.collect(self.db, self.root, lambda: NOW)
        self.assertEqual(self.db.segments()[0].outcome, "done")

    def test_unavailable_process_view_waits_without_ending_a_live_worker(self):
        self.launched()
        with mock.patch.object(worker.process_status, "identity", side_effect=RuntimeError("ps blocked")):
            worker.collect(self.db, self.root, lambda: NOW)
        self.assertIsNone(self.db.segments()[0].ended_at)

    def test_unavailable_runner_view_waits_then_collects_durable_completion(self):
        run = self.launched()
        for error in (OSError("ps failed"), RuntimeError("ps blocked")):
            with self.subTest(error=error):
                with mock.patch.object(worker.process_status, "identity", side_effect=[None, error]):
                    worker.collect(self.db, self.root, lambda: NOW)
                segment = self.db.segments()[-1]
                self.assertIsNone(segment.ended_at)
                self.assertIsNone(segment.outcome)
                self.assertEqual(self.db.log(KEY)[-1].message, f"worker identity unverified: {error}")
        (run / "stdout").write_text('{"is_error": false, "result": "done"}')
        (run / "stderr").write_text("")
        self.report(run)
        (run / "exit.json").write_text('{"exit_code": 0}')
        worker.collect(self.db, self.root, lambda: NOW + timedelta(minutes=1))
        self.assertEqual(self.db.segments()[-1].outcome, "done")

    def test_collect_refuses_evidence_redirected_inside_the_tree(self):
        self.launched()
        data = self.db.resume(KEY)
        data["runs"]["implement-1"]["run_dir"] = str(self.tree)
        self.db.set_resume(KEY, data)
        worker.collect(self.db, self.root, lambda: NOW)
        self.assertEqual(self.db.segments()[0].outcome, "failed")

    def test_real_detached_adapter_is_adopted_and_collected_after_engine_restart(self):
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
            self.launch()
        record = self.db.resume(KEY)["runs"]["implement-1"]
        runner = worker._runners.pop(record["run_dir"])
        self.addCleanup(lambda: claude_adapter.cancel(runner) if runner.poll() is None else None)
        data = self.db.resume(KEY)
        data.pop("runs")
        self.db.set_resume(KEY, data)
        with mock.patch.object(worker.claude_adapter, "start") as start, \
             mock.patch.object(worker.process_status, "identity", return_value="fake-start"):
            slots.claim(self.trees, self.db, issue_key=KEY, stage="implement", attempt=1,
                        max_concurrency=1, clock=lambda: NOW, launch=self.launch)
        start.assert_not_called()
        self.assertEqual(self.db.resume(KEY)["runs"]["implement-1"], record)
        runner.wait(timeout=5)
        self.report(Path(record["run_dir"]))
        with mock.patch.object(worker.process_status, "identity", return_value=None):
            worker.collect(self.db, self.root, lambda: NOW + timedelta(minutes=1))
        self.assertEqual(self.db.segments()[0].outcome, "done")
        command = json.loads((Path(record["run_dir"]) / "command.json").read_text())["argv"]
        for token in ("-p", "--output-format", "json", "--model", "example", "--session-id", record["session_id"]):
            self.assertIn(token, command)
        self.assertNotIn("--safe-mode", command)

    def test_worker_launch_attaches_guard_plugin_and_chief_binding(self):
        guard = (Path(worker.guard_binding.__file__).resolve().parent.parent / "guard").resolve()
        self.assertTrue(guard.is_dir())
        with mock.patch.object(worker.make_worktree, "build", return_value=SimpleNamespace(path=self.tree)), \
             mock.patch.object(worker.claude_adapter, "start", side_effect=self.start) as start, \
             mock.patch.object(worker.process_status, "identity", return_value="start"):
            self.launch()
        request = start.call_args.args[0]
        env = start.call_args.kwargs["env"]
        self.assertEqual(request.plugin_dirs, (str(guard),))
        self.assertEqual(env["CHIEF_OF_STUFF_WORKSPACE"], str(self.root))
        self.assertEqual(env["CHIEF_ISSUE_KEY"], KEY)
        self.assertEqual(env["CHIEF_STAGE"], "implement")
        self.assertEqual(env["CHIEF_ATTEMPT"], "1")
        self.assertEqual(env["CHIEF_ROOT"], str(self.root))
        self.assertEqual(env["CHIEF_SESSION_ID"], request.session_id)
        self.assertEqual(env["CHIEF_LAUNCHED_AT"], NOW.isoformat())
        self.assertEqual(env["CHIEF_PYTHON"], sys.executable)
        self.assertEqual(env["CHIEF_PROTECTED_BRANCHES"], '["main", "master", "develop", "trunk"]')
        argv = claude_adapter.argv(request, "claude", [])
        self.assertEqual([argv[i + 1] for i, x in enumerate(argv) if x == "--plugin-dir"], [str(guard)])
        self.assertEqual(env["CHIEF_ISSUE_KEY"], KEY)

    def test_unreadable_protected_branches_refuse_the_start(self):
        def broken():
            raise worker.ForgeError("protected branches: rate limited")
        self.forge.protected_branches = broken
        with mock.patch.object(worker.make_worktree, "build", return_value=SimpleNamespace(path=self.tree)), \
             mock.patch.object(worker.claude_adapter, "start") as start:
            with self.assertRaisesRegex(worker.ForgeError, "protected branches"):
                self.launch()
        start.assert_not_called()

    def test_collect_recovers_a_reservation_whose_run_record_was_never_saved(self):
        for exited in (False, True):
            with self.subTest(exited=exited):
                if self.db.resume(KEY):
                    self.db.engine.dispose()
                    self.setUp()
                run = self.launched()
                data = self.db.resume(KEY)
                saved = data.pop("runs")["implement-1"]
                self.db.set_resume(KEY, data)
                if exited:
                    (run / "stdout").write_text('{"is_error": false, "result": "done"}')
                    (run / "stderr").write_text("")
                    self.report(run)
                    (run / "exit.json").write_text('{"exit_code": 0}')
                with mock.patch.object(worker.process_status, "identity", return_value="process-start"):
                    worker.collect(self.db, self.root, lambda: NOW, trees=self.trees)
                segment = self.db.segments()[0]
                self.assertEqual(segment.outcome, "done" if exited else None)
                self.assertEqual(self.db.resume(KEY)["runs"]["implement-1"]["session_id"], saved["session_id"])
                self.assertEqual(list(run.parent.glob(f"{run.name}-refused-*")), [])

    def test_collect_keeps_a_command_only_start_reserved(self):
        # Engine restart between Popen and started.json must not fail the reservation.
        run = worker._run_dir(self.root, KEY, "implement", 1)
        run.mkdir(parents=True)
        (run / "command.json").write_text("{}")
        self.db.record_segment(KEY, "implement", 1, NOW.isoformat())
        worker.collect(self.db, self.root, lambda: NOW, trees=self.trees)
        seg = self.db.segments(KEY)[0]
        self.assertIsNone(seg.ended_at)
        self.assertIsNone(seg.outcome)

    def test_collect_drops_a_reservation_with_no_start_evidence_without_failing_the_attempt(self):
        # A launcher killed before it started anything ran no attempt; failing it would spend retry_limit.
        self.db.record_segment(KEY, "implement", 1, NOW.isoformat())
        self.db.set_resume(KEY, {"pending_stage": {"stage": "implement", "expected": "plan"}})
        worker.collect(self.db, self.root, lambda: NOW, trees=self.trees)
        self.assertEqual(self.db.segments(), [])
        self.assertNotIn("pending_stage", self.db.resume(KEY))

    def test_collect_still_fails_a_reservation_when_no_trees_dir_can_vouch_for_it(self):
        self.db.record_segment(KEY, "implement", 1, NOW.isoformat())
        worker.collect(self.db, self.root, lambda: NOW)
        self.assertEqual(self.db.segments()[0].outcome, "failed")

    def test_collect_fills_the_model_and_effort_a_killed_claim_never_recorded(self):
        self.db.record_segment(KEY, "implement", 1, NOW.isoformat())
        with mock.patch.object(worker.make_worktree, "build", return_value=SimpleNamespace(path=self.tree)), \
             mock.patch.object(worker.claude_adapter, "start", side_effect=self.start), \
             mock.patch.object(worker.process_status, "identity", return_value="process-start"):
            self.launch()
            worker.collect(self.db, self.root, lambda: NOW, trees=self.trees)
        segment = self.db.segments()[0]
        self.assertIsNone(segment.ended_at)
        self.assertEqual(segment.model, self.persona.model)
        self.assertEqual(segment.effort or "", self.persona.effort or "")

    def test_a_non_utf8_transcript_does_not_make_the_run_unreadable(self):
        run = self.launched()
        (run / "stdout").write_bytes(b'{"is_error": false, "result": "caf\xc3')
        (run / "stderr").write_bytes(b"\xe2\x82")
        (run / "exit.json").write_text('{"exit_code": 0}')
        self.report(run)
        with mock.patch.object(worker.process_status, "identity", return_value=None):
            worker.collect(self.db, self.root, lambda: NOW + timedelta(minutes=1))
        self.assertFalse(any("unreadable worker run" in e.message for e in self.db.log(KEY)))

    def test_an_exit_written_while_identities_are_probed_is_read_not_inferred(self):
        run = self.launched()
        data = self.db.resume(KEY)
        record = data["runs"]["implement-1"]

        def exits_during_probe(*args, **kwargs):
            (run / "exit.json").write_text('{"exit_code": 0}')
            return None

        with mock.patch.object(worker.process_status, "identity", side_effect=exits_during_probe):
            kept = worker._existing_run(data, run, "implement", 1, self.trees, lambda: NOW, store=self.db, key=KEY)
        self.assertEqual(kept["session_id"], record["session_id"])

    def test_collect_reads_an_exit_written_while_identities_are_probed(self):
        run = self.launched()
        self.report(run)

        def exits_during_probe(*args, **kwargs):
            (run / "stdout").write_text('{"is_error": false, "result": "done"}')
            (run / "exit.json").write_text('{"exit_code": 1}')
            return None

        with mock.patch.object(worker.process_status, "identity", side_effect=exits_during_probe):
            worker.collect(self.db, self.root, lambda: NOW + timedelta(minutes=1))
        # exit 1 with a clean envelope is a failure; inferring from the envelope alone would say done.
        self.assertEqual(self.db.segments()[0].outcome, "failed")

    def test_an_interrupted_launch_cancels_the_runner_it_spawned(self):
        runner = SimpleNamespace(pid=78, poll=mock.Mock(side_effect=KeyboardInterrupt))
        with mock.patch.object(worker.make_worktree, "build", return_value=SimpleNamespace(path=self.tree)), \
             mock.patch.object(worker.claude_adapter, "start", side_effect=lambda r, d, **k: (self.start(r, d), runner)[1]), \
             mock.patch.object(worker.claude_adapter, "cancel") as cancel, \
             mock.patch.object(worker.process_status, "identity", return_value="process-start"):
            with self.assertRaises(KeyboardInterrupt):
                self.launch()
        cancel.assert_called_once_with(runner)

    def test_inferred_exit_follows_the_adapters_own_reading_of_the_transcript(self):
        quota = "API Error: 429 RESOURCE_EXHAUSTED quota resets at 5pm."
        for stdout, stderr, expected in (
                ('{"is_error": true, "result": "bad"}', "", 1), ('{"is_error": false, "result": "ok"}', "", 0),
                ("", quota, 1), ('{"is_error": false, "result": "x"}', quota, 0),
                (quota, "", 1), ("", "", None)):
            with self.subTest(stdout=stdout, stderr=stderr):
                self.assertEqual(claude_adapter.inferred_exit(stdout, stderr), expected)

    def test_plaintext_stdout_quota_overrides_a_session_report_without_exit_evidence(self):
        run = self.launched()
        self.report(run)
        (run / "stdout").write_text("API Error: 429 RESOURCE_EXHAUSTED quota resets at 5pm.")
        with mock.patch.object(worker.process_status, "identity", return_value=None):
            worker.collect(self.db, self.root, lambda: NOW, trees=self.trees)
        self.assertEqual(self.db.segments()[0].outcome, "quota")

    def test_actor_metadata_survives_a_kill_before_save_run(self):
        self.p.personas["builder"] = SimpleNamespace(role="build", rules=(), agent="", model="original", effort="high")
        def killed_save(*args, **kwargs):
            raise KeyboardInterrupt
        with mock.patch.object(worker.make_worktree, "build", return_value=SimpleNamespace(path=self.tree)), \
             mock.patch.object(worker.claude_adapter, "start", side_effect=self.start), \
             mock.patch.object(worker.process_status, "identity", return_value="process-start"), \
             mock.patch.object(worker, "_save_run", side_effect=killed_save), \
             mock.patch.object(worker.claude_adapter, "cancel"):
            with self.assertRaises(KeyboardInterrupt):
                slots.claim(self.trees, self.db, issue_key=KEY, stage="implement", attempt=1,
                            max_concurrency=1, clock=lambda: NOW, launch=self.launch)
        with mock.patch.object(worker.process_status, "identity", return_value="process-start"):
            worker.collect(self.db, self.root, lambda: NOW, trees=self.trees)
        segment = self.db.segments()[0]
        self.assertEqual((segment.model, segment.effort), ("original", "high"))

    def test_canceled_unstarted_launch_releases_its_slot_on_restart(self):
        def unstarted(request, run_dir, **kwargs):
            (run_dir / "command.json").write_text("{}")
            return SimpleNamespace(pid=78, poll=mock.Mock(side_effect=KeyboardInterrupt))
        with mock.patch.object(worker.make_worktree, "build", return_value=SimpleNamespace(path=self.tree)), \
             mock.patch.object(worker.claude_adapter, "start", side_effect=unstarted), \
             mock.patch.object(worker.claude_adapter, "cancel") as cancel:
            with self.assertRaises(KeyboardInterrupt):
                slots.claim(self.trees, self.db, issue_key=KEY, stage="implement", attempt=1,
                            max_concurrency=1, clock=lambda: NOW, launch=self.launch)
        cancel.assert_called_once()
        worker.collect(self.db, self.root, lambda: NOW, trees=self.trees)
        self.assertEqual(self.db.segments(), [])
        self.assertEqual(slots.running(self.db), 0)

    def test_an_interrupt_after_started_json_is_renamed_on_the_next_pass(self):
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True)
        self.addCleanup(lambda: proc.poll() is not None or proc.kill())

        def spawn(request, run_dir, **kwargs):
            (run_dir / "started.json").write_text(json.dumps({
                "pid": proc.pid, "runner_pid": proc.pid, "session_id": request.session_id}))
            return proc

        calls = {"n": 0}

        def identity(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise KeyboardInterrupt
            return None

        with mock.patch.object(worker.make_worktree, "build", return_value=SimpleNamespace(path=self.tree)), \
             mock.patch.object(worker.claude_adapter, "start", side_effect=spawn), \
             mock.patch.object(worker.process_status, "identity", side_effect=identity):
            with self.assertRaises(KeyboardInterrupt):
                slots.claim(self.trees, self.db, issue_key=KEY, stage="implement", attempt=1,
                            max_concurrency=1, clock=lambda: NOW, launch=self.launch)
        run = worker._run_dir(self.root, KEY, "implement", 1)
        self.assertTrue((run / "started.json").is_file())
        self.assertEqual(len(self.db.segments()), 1)
        worker.collect(self.db, self.root, lambda: NOW, trees=self.trees)
        self.assertEqual(self.db.segments(), [])
        self.assertEqual(len(list(run.parent.glob(f"{run.name}-canceled-*"))), 1)
        self.assertFalse(run.exists())

    def orphans(self, worktrees, **spec):
        import shutil
        from evals import make_repo
        shutil.rmtree(self.trees)
        clone = make_repo.build(self.root, {"worktrees": worktrees, **spec})
        self.forge.closing_request = lambda key: None
        self.stop_git_mock()
        return clone

    def launch_in(self, clone, attempt=1, stage="implement"):
        with mock.patch.object(worker.claude_adapter, "start", side_effect=self.start) as start, \
             mock.patch.object(worker.process_status, "identity", return_value="start"):
            worker.launch(KEY, stage, attempt, None, store=self.db, root=self.root,
                          trees=self.trees, policies=self.p, forge=self.forge, clone=clone, clock=lambda: NOW)
        return start.call_args.args[0]

    def diverged(self):
        return self.orphans([
            {"name": "issue-1-implement-1", "branch": "work/issue-1-implement-1", "merged": False},
            {"name": "issue-1-implement-2", "branch": "work/issue-1-implement-2", "merged": False}])

    def test_abort_starts_fresh_from_main_and_does_not_ask_again(self):
        from evals import git_taint as taint, make_repo
        with taint.private_git_views(self.root / "private-views"):
            clone = self.diverged()
            with self.assertRaisesRegex(RuntimeError, "diverged"):
                self.launch_in(clone, attempt=3)
            kept = make_repo.git(["rev-parse", "HEAD"], self.trees / "issue-1-implement-1")
            self.db.add_decision(KEY, "conflict", self.db.decisions(KEY)[0].question, answer="abort")
            request = self.launch_in(clone, attempt=3)
            self.assertNotIn(Path(request.cwd).name, ("issue-1-implement-1", "issue-1-implement-2"))
            self.assertEqual(make_repo.git(["rev-parse", "HEAD"], self.trees / "issue-1-implement-1"), kept)
            self.assertEqual(len(self.db.asks(KEY)), 1)
            self.assertNotIn(worker.dispatch_prompt.RECOVERY, request.prompt)

    def test_abort_starts_fresh_on_the_next_engine_pass(self):
        from evals import git_taint as taint
        from evals.test_engine import FakeForge
        with taint.private_git_views(self.root / "private-views"):
            clone = self.diverged()
            forge = FakeForge()

            def launch(key, stage, attempt, persona):
                return worker.launch(key, stage, attempt, persona, store=self.db, root=self.root,
                                     trees=self.trees, policies=self.p, forge=self.forge, clone=clone, clock=lambda: NOW)

            def run():
                return engine.run_pass(forge=forge, store=self.db, policies=self.p, root=self.root,
                                       trees=self.trees, launch=launch, workers=engine.settings.Workers(),
                                       clock=lambda: NOW)

            with mock.patch.object(engine, "_record_limits"), \
                 mock.patch.object(engine.cleanup, "execute", return_value=0), \
                 mock.patch.object(worker.claude_adapter, "start", side_effect=self.start) as start, \
                 mock.patch.object(worker.process_status, "identity", return_value="start"):
                run()
                self.db.add_decision(KEY, "conflict", self.db.decisions(KEY)[0].question, answer="abort")
                actions = run()
            self.assertTrue(any(isinstance(a, reconcile.Dispatch) for a in actions))
            self.assertEqual(start.call_count, 1)

    def test_hold_waits_and_resumes_once_the_conflict_is_gone(self):
        import shutil
        from evals import git_taint as taint, make_repo
        with taint.private_git_views(self.root / "private-views"):
            clone = self.diverged()
            with self.assertRaisesRegex(RuntimeError, "diverged"):
                self.launch_in(clone, attempt=3)
            self.db.add_decision(KEY, "conflict", self.db.decisions(KEY)[0].question, answer="hold")
            with mock.patch.object(worker.claude_adapter, "start") as start:
                with self.assertRaisesRegex(RuntimeError, "held"):
                    self.launch_in(clone, attempt=3)
            start.assert_not_called()
            self.assertEqual(len(self.db.asks(KEY)), 1)
            shutil.rmtree(self.trees / "issue-1-implement-2")
            make_repo.git(["branch", "-D", "work/issue-1-implement-2"], clone)
            request = self.launch_in(clone, attempt=3)
            self.assertEqual(Path(request.cwd).name, "issue-1-implement-1")

    def test_an_abort_prefix_is_not_the_abort_option(self):
        from evals import git_taint as taint
        with taint.private_git_views(self.root / "private-views"):
            clone = self.diverged()
            with self.assertRaisesRegex(RuntimeError, "diverged"):
                self.launch_in(clone, attempt=3)
            self.db.add_decision(KEY, "conflict", self.db.decisions(KEY)[0].question,
                                 answer="abort the old one, keep work/issue-1-implement-2")
            with mock.patch.object(worker.claude_adapter, "start") as start:
                with self.assertRaises(RuntimeError) as caught:
                    self.launch_in(clone, attempt=3)
            self.assertNotIn("aborted", str(caught.exception))
            start.assert_not_called()
            self.assertTrue(reconcile._decision_pending(self.db.decisions(KEY)))

    def test_hold_stays_settled_when_the_orphan_heads_change(self):
        from evals import git_taint as taint, make_repo
        with taint.private_git_views(self.root / "private-views"):
            clone = self.diverged()
            with self.assertRaises(RuntimeError):
                self.launch_in(clone)
            self.db.add_decision(KEY, "conflict", self.db.decisions(KEY)[0].question, answer="hold")
            make_repo.git(["commit", "--allow-empty", "-m", "new work"], self.trees / "issue-1-implement-2")
            with mock.patch.object(worker.claude_adapter, "start") as start:
                with self.assertRaisesRegex(RuntimeError, "held"):
                    self.launch_in(clone)
            start.assert_not_called()
            self.assertEqual(len(self.db.asks(KEY)), 1)
            self.assertFalse(reconcile._decision_pending(self.db.decisions(KEY)))

    def test_a_checked_out_branch_is_not_also_a_branch_only_candidate(self):
        from evals import git_taint as taint, make_repo
        with taint.private_git_views(self.root / "private-views"):
            clone = self.orphans([{"name": "issue-1-implement-1", "branch": "work/issue-1-implement-1",
                                   "merged": False}])
            orphan = self.trees / "issue-1-implement-1"
            side = make_repo.git(["commit-tree", make_repo.git(["rev-parse", "HEAD^{tree}"], clone), "-m", "side"], clone)
            make_repo.git(["update-ref", "refs/remotes/origin/work/issue-1-implement-1", side], clone)
            request = self.launch_in(clone)
            self.assertEqual(Path(request.cwd).resolve(), orphan.resolve())
            self.assertEqual(self.db.asks(KEY), [])

    def test_merge_head_without_unmerged_index_entries_requires_a_decision(self):
        from evals import git_taint as taint, make_repo
        with taint.private_git_views(self.root / "private-views"):
            clone = self.orphans([{"name": "issue-1-implement-1", "branch": "work/issue-1-implement-1"}])
            orphan = self.trees / "issue-1-implement-1"
            layout = worker.git_view.locate(orphan)
            (layout.gitdir / "MERGE_HEAD").write_text(make_repo.git(["rev-parse", "HEAD"], orphan) + "\n")
            with self.assertRaisesRegex(RuntimeError, "merge conflicts"):
                self.launch_in(clone)
            self.assertIsNone(self.db.decisions(KEY)[-1].answer)

    def test_another_repositorys_tree_for_the_same_issue_is_never_adopted(self):
        from evals import git_taint as taint, make_repo
        with taint.private_git_views(self.root / "private-views"):
            other = self.orphans([{"name": "issue-1-implement-1", "branch": "work/issue-1-implement-1",
                                   "merged": False, "dirty": True}], clone="other", origin="other.git")
            foreign = self.trees / "issue-1-implement-1"
            head = make_repo.git(["rev-parse", "HEAD"], foreign)
            mine = make_repo.build(self.root, {"clone": "mine", "origin": "mine.git", "trees": "elsewhere"})
            with mock.patch.object(worker.claude_adapter, "start") as start:
                with self.assertRaisesRegex(RuntimeError, "no existing worktree"):
                    self.launch_in(mine, attempt=2)
            start.assert_not_called()
            self.assertEqual(make_repo.git(["rev-parse", "HEAD"], foreign), head)
            self.assertEqual(self.db.decisions(KEY), [])

    def test_a_bare_clone_recreates_the_worktree_from_its_surviving_branch(self):
        import shutil
        from evals import git_taint as taint, make_repo
        with taint.private_git_views(self.root / "private-views"):
            clone = self.orphans([{"name": "issue-1-implement-1", "branch": "work/issue-1-implement-1",
                                   "merged": False}])
            orphan = self.trees / "issue-1-implement-1"
            expected = make_repo.git(["rev-parse", "HEAD"], orphan)
            shutil.rmtree(orphan)
            bare = self.root / "bare.git"
            make_repo.git(["clone", "--bare", str(clone), str(bare)], self.root)
            request = self.launch_in(bare, attempt=2)
            self.assertEqual(make_repo.git(["rev-parse", "HEAD"], Path(request.cwd)), expected)
            self.assertIn(worker.dispatch_prompt.RECOVERY, request.prompt)
            self.assertEqual(self.db.asks(KEY), [])

    def test_engine_metadata_alone_is_not_uncommitted_work_but_a_lookalike_name_is(self):
        import shutil
        from evals import git_taint as taint, make_repo
        files = {".chief-of-stuff/x": "1\n", "a.chief-of-stuff": "1\n"}
        with taint.private_git_views(self.root / "private-views"):
            clone = self.orphans([{"name": "issue-1-implement-1", "branch": "work/issue-1-implement-1",
                                   "merged": True}], files=files)
            orphan = self.trees / "issue-1-implement-1"
            known = {"worktree": str(orphan), "work_branch": "work/issue-1-implement-1"}
            self.db.set_resume(KEY, known)
            (orphan / ".chief-of-stuff" / "x").write_text("2\n")
            self.assertNotIn(worker.dispatch_prompt.RECOVERY, self.launch_in(clone).prompt)
            self.db.set_resume(KEY, known)
            shutil.rmtree(worker._run_dir(self.root, KEY, "implement", 1))
            (orphan / "a.chief-of-stuff").write_text("2\n")
            self.assertIn(worker.dispatch_prompt.RECOVERY, self.launch_in(clone).prompt)

    def test_commits_count_as_recovery_when_the_clone_has_no_local_main(self):
        from evals import git_taint as taint, make_repo
        with taint.private_git_views(self.root / "private-views"):
            clone = self.orphans([{"name": "issue-1-implement-1", "branch": "work/issue-1-implement-1",
                                   "merged": False}])
            orphan = self.trees / "issue-1-implement-1"
            make_repo.git(["checkout", "-b", "other"], clone)
            make_repo.git(["branch", "-D", "main"], clone)
            self.db.set_resume(KEY, {"worktree": str(orphan), "work_branch": "work/issue-1-implement-1"})
            self.assertIn(worker.dispatch_prompt.RECOVERY, self.launch_in(clone).prompt)

    def test_a_fix_assignment_never_tells_the_worker_to_open_a_request(self):
        prompt = worker.dispatch_prompt.compose_issue(self.db.items()[0], "t", "b", "fix", self.persona, recover=True)
        self.assertIn(worker.dispatch_prompt.RECOVERY, prompt)
        self.assertNotIn("open or update", prompt)
        self.assertIn("do not open a new request", prompt)

    def test_worker_launch_refuses_without_the_guard_plugin(self):
        with mock.patch.object(worker.guard_binding, "guard_plugin_dir", return_value=None), \
             mock.patch.object(worker.make_worktree, "build", return_value=SimpleNamespace(path=self.tree)), \
             mock.patch.object(worker.claude_adapter, "start") as start:
            with self.assertRaisesRegex(RuntimeError, "guard/ plugin"):
                self.launch()
        start.assert_not_called()

    def test_adapter_runner_gives_the_host_the_chief_env(self):
        run = self.root / "run"
        run.mkdir()
        out = self.root / "seen"
        code = f"import os; open({str(out)!r}, 'w').write(os.environ['CHIEF_ISSUE_KEY'])"
        (run / "command.json").write_text(json.dumps({
            "argv": [sys.executable, "-c", code], "cwd": str(self.root), "session_id": "s",
            "env": {"CHIEF_ISSUE_KEY": KEY}}))
        claude_adapter._worker(run)
        self.assertEqual(out.read_text(), KEY)

    def test_agent_effort_profile_are_passed_to_adapter(self):
        (self.root / "CLAUDE.md").write_text(
            "## Coordinator\n- User: Robin\n- Agent: specialist task\n")
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

    def test_review_stage_configuration_reaches_engine_worker(self):
        for declared in (False, True):
            with self.subTest(declared=declared):
                (self.root / "CLAUDE.md").write_text(
                    "## Coordinator\n- Settings: `chief-of-stuff.toml`\n"
                    + ("- Agent: reviewer task\n" if declared else ""))
                (self.root / "chief-of-stuff.toml").write_text(
                    '[lanes.code]\nstages = ["review"]\n'
                    '[stages.review]\nagent = "reviewer"\nskills = ["security-review"]\n')
                self.p.personas["critic"] = SimpleNamespace(
                    name="critic", role="reviews", rules=(), agent="", model="example", effort="")

                def cut(*args, **kwargs):
                    worker.make_worktree.check_type((self.root / "CLAUDE.md").read_text(), args[5])
                    return SimpleNamespace(path=self.tree)

                with mock.patch.object(worker.make_worktree, "build", side_effect=cut) as build, \
                     mock.patch.object(worker.claude_adapter, "start", side_effect=self.start) as start, \
                     mock.patch.object(worker.process_status, "identity", return_value="start"):
                    self.launch("review", attempt=1 + int(declared))
                request = start.call_args.args[0]
                self.assertEqual(request.agent, "reviewer" if declared else "")
                self.assertIn("Skills: load and follow security-review", request.prompt)
                self.assertEqual(build.call_args.args[5], request.agent)

    def test_restart_adopts_run_despite_broken_launch_configuration(self):
        for completed in (False, True):
            for broken in ("settings", "claude"):
                with self.subTest(completed=completed, broken=broken):
                    if self.db.resume(KEY):
                        self.db.engine.dispose()
                        self.setUp()
                    with mock.patch.object(worker.make_worktree, "build", return_value=SimpleNamespace(path=self.tree)), \
                         mock.patch.object(worker.claude_adapter, "start", side_effect=self.start), \
                         mock.patch.object(worker.process_status, "identity", return_value="process-start"):
                        self.launch()
                    record = self.db.resume(KEY)["runs"]["implement-1"]
                    if completed:
                        (Path(record["run_dir"]) / "exit.json").write_text('{"exit_code": 0}')
                    if broken == "settings":
                        (self.root / "CLAUDE.md").write_text("## Coordinator\n- Settings: chief-of-stuff.toml\n")
                        (self.root / "chief-of-stuff.toml").write_text("invalid toml [")
                    else:
                        (self.root / "CLAUDE.md").write_bytes(b"\xff")
                    with mock.patch.object(worker.claude_adapter, "start") as start, \
                         mock.patch.object(worker.make_worktree, "build") as build, \
                         mock.patch.object(worker.process_status, "identity", return_value="process-start"):
                        slots.claim(self.trees, self.db, issue_key=KEY, stage="implement", attempt=1,
                                    max_concurrency=1, clock=lambda: NOW, launch=self.launch)
                    self.assertEqual(slots.running(self.db), 1)
                    self.assertEqual(self.db.resume(KEY)["runs"]["implement-1"], record)
                    start.assert_not_called()
                    build.assert_not_called()

    def test_undeclared_agent_is_omitted_so_default_agent_runs(self):
        (self.root / "CLAUDE.md").write_text(
            "## Coordinator\n- User: Robin\n- Agent: implementer task\n")
        self.p.personas["builder"] = SimpleNamespace(name="builder", role="build", rules=(),
                                                       agent="specialist", model="opus", effort="")

        def cut(*args, **kwargs):
            # Real check_type: empty type must pass when Agent: lines exist (Codex on #675).
            worker.make_worktree.check_type((self.root / "CLAUDE.md").read_text(), args[5])
            return SimpleNamespace(path=self.tree)

        with mock.patch.object(worker.make_worktree, "build", side_effect=cut) as build, \
             mock.patch.object(worker.claude_adapter, "start", side_effect=self.start) as start, \
             mock.patch.object(worker.process_status, "identity", return_value="start"):
            worker.launch(KEY, "implement", 1, "builder", store=self.db, root=self.root,
                          trees=self.trees, policies=self.p, forge=self.forge)
        # Cut and request share the declared-only type; never actor.name ("builder").
        self.assertEqual(build.call_args.args[5], "")
        self.assertEqual(start.call_args.args[0].agent, "")

    def test_adapter_worker_argv_enables_tools_and_named_agent(self):
        request = worker.Request("prompt", worker=True, agent="specialist", effort="high")
        args = claude_adapter.argv(request, "/fake", [])
        self.assertIn("--agent", args)
        self.assertIn("specialist", args)
        self.assertNotIn("--safe-mode", args)
        self.assertEqual(args[args.index("--setting-sources") + 1], "user")
        self.assertIn("--strict-mcp-config", args)

    def test_forge_retains_request_head_branch(self):
        payload = [{"number": 1, "title": "Request", "url": "url", "headRefOid": "sha",
                    "headRefName": "feature/existing"}]
        gh = mock.Mock(return_value=(0, json.dumps(payload), ""))
        self.assertEqual(forge_review.github_requests("example/project", "", gh)[0].branch, "feature/existing")
        self.assertIn("headRefName", gh.call_args.args[0][-1])

    def test_quota_reset_formats_and_backoff(self):
        from model_port import retry_at
        self.assertEqual(retry_at("resets at 5pm.", NOW), NOW + timedelta(hours=17))
        self.assertEqual(retry_at("resets in about four hours.", NOW), NOW + timedelta(hours=4))
        self.assertEqual(retry_at("reset at 2000-01-01T02:00:00Z", NOW), NOW + timedelta(hours=2))
        self.assertEqual(retry_at("unknown", NOW, 4), NOW + timedelta(minutes=8))
        self.assertEqual(retry_at("reset at 2000-99-99T02:00:00Z", NOW), NOW + timedelta(minutes=1))

    def test_assignment_includes_recovery_instructions_only_when_recovering(self):
        row = self.db.items()[0]
        fresh = worker.dispatch_prompt.compose_issue(row, "title", "body", "implement", self.persona)
        self.assertNotIn(worker.dispatch_prompt.RECOVERY, fresh)
        for stage in ("implement", "fix"):
            with self.subTest(stage=stage):
                recovered = worker.dispatch_prompt.compose_issue(
                    row, "title", "body", stage, self.persona, recover=True)
                self.assertIn(worker.dispatch_prompt.RECOVERY, recovered)
        review = worker.dispatch_prompt.compose_issue(row, "title", "body", "review", self.persona, recover=True)
        self.assertNotIn(worker.dispatch_prompt.RECOVERY, review)

    def test_collect_recovers_finished_run_when_runner_died_without_exit_json(self):
        for recorded, outcome in ((True, "done"), (False, "done"), (False, "quota"), (False, "report_only")):
            with self.subTest(recorded=recorded, outcome=outcome):
                if self.db.segments() or self.db.resume(KEY):
                    self.db.engine.dispose()
                    self.setUp()
                run = self.launched()
                if not recorded:
                    data = self.db.resume(KEY)
                    data.pop("runs")
                    self.db.set_resume(KEY, data)
                if outcome in ("done", "report_only"):
                    self.report(run)
                if outcome == "done":
                    (run / "stdout").write_text('{"is_error": false, "result": "done"}')
                    (run / "stderr").write_text("")
                elif outcome == "quota":
                    (run / "stdout").write_text("")
                    (run / "stderr").write_text(
                        'API Error: 429 {"error":{"status":"RESOURCE_EXHAUSTED"}} resets at 5pm.')
                else:
                    (run / "stdout").write_text("")
                    (run / "stderr").write_text("")
                with mock.patch.object(worker.process_status, "identity", return_value=None):
                    worker.collect(self.db, self.root, lambda: NOW + timedelta(minutes=1), trees=self.trees)
                seg = self.db.segments(KEY)[0]
                self.assertEqual(seg.outcome, "quota" if outcome == "quota" else "done")

    def test_orphaned_worktree_and_branch_are_recovered_without_asking_keeping_commits_and_dirty_work(self):
        import shutil
        from evals import git_taint as taint, make_repo
        with taint.private_git_views(self.root / "private-views"):
            shutil.rmtree(self.trees)
            clone = make_repo.build(self.root, {"worktrees": [
                {"name": "issue-1-implement-1", "branch": "work/issue-1-implement-1", "merged": False, "dirty": True},
            ]})
            orphan = self.trees / "issue-1-implement-1"
            expected_sha = make_repo.git(["rev-parse", "HEAD"], orphan)
            self.forge.closing_request = lambda key: None
            self.stop_git_mock()
            with mock.patch.object(worker.claude_adapter, "start", side_effect=self.start) as start, \
                 mock.patch.object(worker.process_status, "identity", return_value="start"):
                worker.launch(KEY, "implement", 1, None, store=self.db, root=self.root,
                              trees=self.trees, policies=self.p, forge=self.forge, clone=clone, clock=lambda: NOW)
            request = start.call_args.args[0]
            self.assertEqual(Path(request.cwd).resolve(), orphan.resolve())
            self.assertIn(worker.dispatch_prompt.RECOVERY, request.prompt)
            self.assertEqual(make_repo.git(["rev-parse", "HEAD"], orphan), expected_sha)
            self.assertEqual((orphan / "scratch.txt").read_text(), "unsaved\n")
            self.assertEqual(self.db.decisions(KEY), [])
            self.assertEqual(self.db.asks(KEY), [])
            self.assertNotIn("interrupt", self.db.resume(KEY) or {})

            # If the worktree directory is lost, recover from the orphaned branch keeping its commits.
            (orphan / "scratch.txt").unlink()
            shutil.rmtree(orphan)
            self.db.set_resume(KEY, {})
            with mock.patch.object(worker.claude_adapter, "start", side_effect=self.start) as start, \
                 mock.patch.object(worker.process_status, "identity", return_value="start"):
                worker.launch(KEY, "implement", 2, None, store=self.db, root=self.root,
                              trees=self.trees, policies=self.p, forge=self.forge, clone=clone, clock=lambda: NOW)
            recovered_tree = Path(start.call_args.args[0].cwd)
            self.assertTrue(recovered_tree.is_dir())
            self.assertEqual(make_repo.git(["symbolic-ref", "--short", "HEAD"], recovered_tree),
                             "work/issue-1-implement-1")
            self.assertEqual(make_repo.git(["rev-parse", "HEAD"], recovered_tree), expected_sha)
            self.assertIn(worker.dispatch_prompt.RECOVERY, start.call_args.args[0].prompt)
            self.assertEqual(self.db.decisions(KEY), [])
            self.assertEqual(self.db.asks(KEY), [])

    def test_genuine_conflict_in_orphaned_work_asks_without_overwriting(self):
        import shutil
        from evals import git_taint as taint, make_repo
        with taint.private_git_views(self.root / "private-views"):
            shutil.rmtree(self.trees)
            clone = make_repo.build(self.root, {"worktrees": [
                {"name": "issue-1-implement-1", "branch": "work/issue-1-implement-1", "merged": False},
                {"name": "issue-1-implement-2", "branch": "work/issue-1-implement-2", "merged": False},
            ]})
            tree_a = self.trees / "issue-1-implement-1"
            tree_b = self.trees / "issue-1-implement-2"
            sha_a = make_repo.git(["rev-parse", "HEAD"], tree_a)
            sha_b = make_repo.git(["rev-parse", "HEAD"], tree_b)
            self.forge.closing_request = lambda key: None
            self.stop_git_mock()
            with mock.patch.object(worker.claude_adapter, "start") as start:
                with self.assertRaisesRegex(RuntimeError, "conflict|diverged"):
                    worker.launch(KEY, "implement", 3, None, store=self.db, root=self.root,
                                  trees=self.trees, policies=self.p, forge=self.forge, clone=clone, clock=lambda: NOW)
            start.assert_not_called()
            self.assertEqual(make_repo.git(["rev-parse", "HEAD"], tree_a), sha_a)
            self.assertEqual(make_repo.git(["rev-parse", "HEAD"], tree_b), sha_b)
            self.assertEqual(len(self.db.decisions(KEY)), 1)
            self.assertIsNone(self.db.decisions(KEY)[0].answer)
            self.assertEqual(len(self.db.asks(KEY)), 1)
            self.assertEqual(self.db.asks(KEY)[0].options, ["abort", "hold"])
            self.assertNotIn("Candidates", self.db.asks(KEY)[0].question)
            self.assertNotIn(sha_a, self.db.asks(KEY)[0].question)
            self.assertEqual(self.db.resume(KEY)["interrupt"]["kind"], "Ask")

    def stop_git_mock(self):
        mock.patch.stopall()
        git_env = mock.patch.object(worker.make_worktree.git_trees, "audit_env",
                                    side_effect=worker.make_worktree.git_trees.audit_env)
        git_env.start()
        self.addCleanup(git_env.stop)


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
