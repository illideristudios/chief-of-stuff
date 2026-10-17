"""Engine passes with a fake forge and launch port, and a real issue-keyed store."""

import contextlib
from dataclasses import replace
import io
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import engine
import issue_gate
import policy
import reconcile
import slots
import store
import supervisor
from tree_state import TreeState
from orphans import Claim
from evals import test_forge as tf
from forge_port import ForgeError
from settings import Kanban

NOW = datetime(2000, 1, 1, tzinfo=timezone.utc)
KEY = "github:example/project#1"
OTHER = "github:example/project#2"


def policies():
    return policy.parse({
        "default_workflow": "standard",
        "personas": {"builder": {"role": "implements", "model": "example"},
                     "critic": {"role": "reviews", "model": "example"}},
        "stages": {"implement": {"persona": "builder"}, "review": {"persona": "critic"}, "merge": {}},
        "workflows": {"standard": {"start": "implement", "edges": [
            {"from": "implement", "to": "review", "gate": "plan"},
            {"from": "review", "to": "implement"}, {"from": "review", "to": "merge"},
        ]}},
    })


class FakeForge:
    def __init__(self):
        self.rows = [{"issue_key": KEY, "stage": "implement", "forge_state": "open", "merge_state": "ready"}]
        self.writes, self.merges = [], []
        self.kanban, self.github = None, True
        self.home = SimpleNamespace(repo=KEY.split(":", 1)[1].rsplit("#", 1)[0])

    def blocked_by(self):
        return {}

    def snapshots(self):
        return [self.snapshot(row["issue_key"]) for row in self.rows]

    def snapshot(self, issue_key, *, issue=None, request=None):
        row = next(r for r in self.rows if r["issue_key"] == issue_key)
        return {"body": "", **dict(row), "stage_mark": None}

    def mark(self, number):
        return f"#{number}"

    def mark_for_stage(self, stage):
        return None

    def write_stage(self, number, stage, expected=None, commit=False, initialize=False, release_hold=False):
        self.writes.append((number, stage, expected, commit))
        next(row for row in self.rows if row["issue_key"].endswith(f"#{number}"))["stage"] = stage
        return SimpleNamespace(ok=True, done=True, error="")

    def add_human_hold(self, number):
        self.rows[0]["human_review"] = True
        return ""

    def closing_request(self, issue_key):
        return SimpleNamespace(number=10, verdict="ready", approval="", head=tf.tma.HEAD)

    def issue(self, number):
        row = next(row for row in self.rows if row["issue_key"].endswith(f"#{number}"))
        return SimpleNamespace(ok=True, issue=SimpleNamespace(is_open=row["forge_state"] == "open", labels=[],
                               body=row.get("body", "")))

    def merge(self, number, commit=False, issue_key=None, approval=None, head=None, stage_mark=None):
        self.merges.append((number, commit, issue_key))
        next(row for row in self.rows if row["issue_key"] == issue_key)["forge_state"] = "closed"
        return "merged #10"


class EngineTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.trees = self.root / "trees"
        self.trees.mkdir()
        path = self.root / "store.db"
        store.migrate(path)
        self.db = store.Store(path, clock=lambda: NOW)
        self.addCleanup(self.db.engine.dispose)
        self.forge, self.p = FakeForge(), policies()
        self.now, self.launched = NOW, []
        cleanup = mock.patch.object(engine.cleanup, "execute", return_value=0)
        self.cleanup = cleanup.start()
        self.addCleanup(cleanup.stop)
        identity = mock.patch.object(engine.worker.process_status, "identity", return_value="fake-start")
        identity.start()
        self.addCleanup(identity.stop)
        limits = mock.patch.object(engine, "_record_limits")
        limits.start()
        self.addCleanup(limits.stop)

    def launch(self, key, stage, attempt, persona):
        self.launched.append((key, stage, attempt, persona))
        # A live fake worker must supply the run record the production Launch port now promises.
        data = self.db.resume(key) or {}
        data["runs"] = {**data.get("runs", {}), f"{stage}-{attempt}": {
            "pid": 123, "session_id": "fake-session", "identity": "fake-start",
            "runner_pid": 124, "runner_identity": "fake-start",
            "run_dir": str(supervisor.state_dir(self.root) / "runs" / f"{key.rpartition('#')[2]}-{stage}-{attempt}"),
            "tree": str(self.trees / "fake"), "starting_head": "starting-head",
        }}
        self.db.set_resume(key, data)
        return "used-model", "high"

    def run_pass(self, **kwargs):
        return engine.run_pass(forge=self.forge, store=self.db, policies=self.p, root=self.root,
                               trees=self.trees, launch=self.launch, workers=engine.settings.Workers(),
                               clock=lambda: self.now, **kwargs)

    def finish(self, stage, attempt=1, outcome="done"):
        seg = next(s for s in self.db.segments(KEY) if s.stage == stage and s.attempt == attempt)
        self.now += timedelta(minutes=1)
        self.db.record_segment(KEY, stage, attempt, started_at=seg.started_at,
                               ended_at=self.now.isoformat(), outcome=outcome)
        self.now += timedelta(minutes=1)

    def findings(self, attempt=1, counts=None):
        self.db.set_resume(KEY, {**(self.db.resume(KEY) or {}),
                                 "review_findings": {"attempt": attempt, "counts": counts or {}}})

    def test_dispatch_review_merge_end_to_end(self):
        self.assertEqual(self.run_pass(), [reconcile.Dispatch(KEY, "implement", "builder", 1)])
        self.assertEqual(self.launched, [(KEY, "implement", 1, "builder")])
        self.assertEqual(self.db.segments(KEY)[0].model, "used-model")
        self.finish("implement")
        self.assertEqual(self.run_pass(), [reconcile.Review(KEY, 1)])
        self.assertEqual(self.launched[-1], (KEY, "review", 1, "critic"))
        self.finish("review")
        self.findings()
        self.assertEqual(self.run_pass(), [reconcile.Merge(KEY)])
        self.assertEqual(self.forge.merges, [(10, True, KEY)])
        self.assertEqual(self.forge.writes, [(1, "review", "implement", True), (1, "merge", "review", True)])
        self.assertEqual(self.db.items()[0].stage, "merge")
        self.run_pass()
        self.assertEqual(self.db.items()[0].forge_state, "closed")
        self.assertEqual(len(self.launched), 2)
        self.cleanup.assert_called_with(self.root, self.trees, self.db, remove=True, completed=set())

    def test_standard_merge_waits_until_the_merged_commit_has_passing_main_ci(self):
        self.run_pass()
        self.finish("implement")
        self.run_pass()
        self.finish("review")
        self.findings()
        self.run_pass()
        self.forge.rows[0]["merge_state"] = "merged"
        status = "running"
        self.forge.branch_head = lambda ref: SimpleNamespace(sha="main-sha", status=status)
        self.forge.landings = lambda pipeline, keys, **kwargs: {KEY: (True, pipeline.status == "passed")}
        self.assertEqual(self.run_pass(), [reconcile.Wait(KEY, "merged, awaiting main CI: suite has not run on main")])
        status = "failed"
        self.assertEqual(self.run_pass(), [reconcile.Wait(KEY, "merged, awaiting main CI: suite has not run on main")])
        status = "passed"
        self.assertEqual(self.run_pass(), [])
        self.assertEqual(len(self.forge.merges), 1)

    def test_closed_merge_ends_exit_segment_but_keeps_cleanup_hold_until_main_ci_passes(self):
        self.run_pass()
        self.publish_exit()
        self.forge.rows[0].update(forge_state="closed", merge_state="merged", human_review=True)
        status = "running"
        self.forge.branch_head = lambda ref: SimpleNamespace(sha="main-sha", status=status)
        self.forge.landings = lambda pipeline, keys, **kwargs: {KEY: (True, pipeline.status == "passed")}
        with mock.patch.object(engine.engine_facts, "_tree_facts", return_value=(TreeState("work", 0, 0, 0), True, NOW)), \
             mock.patch.object(engine.engine_facts.git_trees, "git", return_value=(0, "main-sha")):
            for status in ("running", "failed", "none"):
                actions = self.run_pass()
                self.assertIsInstance(actions[0], reconcile.Wait)
                self.assertIn("merged, awaiting main CI", actions[0].reason)
                self.assertIsNotNone(self.db.segments(KEY)[0].ended_at)
                self.assertEqual(self.db.segments(KEY)[0].outcome, "done")
                self.assertTrue(self.db.items()[0].human_review)
                self.assertNotIn(KEY, self.cleanup.call_args.kwargs["completed"])
                self.assertEqual(sum(row.message.startswith("worker implement attempt 1:")
                                     for row in self.db.log(KEY)), 1)
                self.assertEqual(self.forge.writes, [])
            status = "passed"
            self.run_pass()
        self.assertEqual(self.db.segments(KEY)[0].outcome, "done")
        self.assertIn(KEY, self.cleanup.call_args.kwargs["completed"])

    def test_merged_review_exit_releases_slot_and_scope_while_main_ci_waits(self):
        self.forge.rows[0]["stage"] = "review"
        self.assertEqual(self.run_pass(max_concurrency=1), [reconcile.Review(KEY, 1)])
        self.publish_exit(stage="review")
        self.forge.rows[0].update(forge_state="closed", merge_state="merged", human_review=True)
        # Undeclared scopes own the whole repository, so a retained claim would block OTHER.
        self.forge.rows.append({"issue_key": OTHER, "stage": "implement", "forge_state": "open"})
        status = "none"
        self.forge.branch_head = lambda ref: SimpleNamespace(sha="main-sha", status=status)
        self.forge.landings = lambda pipeline, keys, **kwargs: {KEY: (True, pipeline.status == "passed")}
        with mock.patch.object(engine.engine_facts, "_tree_facts", return_value=(TreeState("work", 0, 0, 0), True, NOW)), \
             mock.patch.object(engine.engine_facts, "_changed", return_value=False):
            for status in ("none", "running", "failed"):
                with self.subTest(status=status):
                    actions = self.run_pass(max_concurrency=1)
                    self.assertIsInstance(actions[0], reconcile.Wait)
                    self.assertIn("merged, awaiting main CI", actions[0].reason)
                    self.assertIn((OTHER, "implement", 1, "builder"), self.launched)
                    self.assertEqual(len(self.launched), 2)
                    self.assertEqual(slots.running(self.db), 1)  # Only OTHER's worker is live.
                    self.assertIsNotNone(self.db.segments(KEY)[0].ended_at)
                    self.assertTrue(next(item for item in self.db.items() if item.issue_key == KEY).human_review)
                    self.assertNotIn(KEY, self.cleanup.call_args.kwargs["completed"])
                    self.assertEqual(self.forge.writes, [])
            status = "passed"
            self.run_pass(max_concurrency=1)
        self.assertIn(KEY, self.cleanup.call_args.kwargs["completed"])
        self.assertEqual(sum(row.message.startswith("worker review attempt 1:") for row in self.db.log(KEY)), 1)

    def test_a_verified_landing_is_not_reread_on_later_gathers(self):
        self.forge.rows[0].update(forge_state="closed", merge_state="merged", stage="merge")
        self.db.upsert_item(OTHER, stage="merge", forge_state="closed", merge_state="merged")
        self.db.set_resume(OTHER, {"verified_landing": True})
        self.forge.branch_head = lambda ref: SimpleNamespace(sha="main-sha", status="passed", ref="main")
        reads = []

        def landings(pipeline, keys, **kwargs):
            reads.append(tuple(keys))
            return {key: (True, True) for key in keys}

        self.forge.landings = landings
        self.run_pass()
        self.assertEqual(reads, [(KEY,)])
        self.assertIs(self.db.resume(KEY)["verified_landing"], True)
        self.assertIs(self.db.resume(OTHER)["verified_landing"], True)
        self.forge.landings = lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("reread"))
        self.run_pass()
        self.assertIn(KEY, self.cleanup.call_args.kwargs["completed"])
        self.assertIn(OTHER, self.cleanup.call_args.kwargs["completed"])

    def test_landing_comparison_failures_reach_the_deduplicated_wait_log(self):
        self.forge.rows[0].update(forge_state="closed", merge_state="merged")
        self.forge.branch_head = lambda ref: SimpleNamespace(sha="main-sha", status="passed")
        def landings(pipeline, keys, *, errors=None):
            if errors is not None:
                errors[KEY] = "compare: permission denied"
            return {KEY: (None, None)}
        self.forge.landings = landings
        self.run_pass()
        self.run_pass()
        waits = [row.message for row in self.db.log(KEY) if row.message.startswith("wait: ")]
        self.assertEqual(len(waits), 1)
        self.assertIn("compare: permission denied", waits[0])

    def test_terminal_failed_exit_replaces_its_live_claim_but_keeps_other_live_owners(self):
        self.p.workflows["standard"] = policy.Workflow("standard", "implement")
        self.run_pass()
        segment = self.db.segments(KEY)[0]
        for others in ((), (Claim(OTHER, True, "running"),)):
            facts = reconcile.Facts(trees=((KEY, TreeState("work", 1, 2, 0)),),
                tree_names=((KEY, "work"),), tree_claims=((KEY, (Claim(KEY, True, ""), *others)),))
            with self.subTest(others=others), \
                 mock.patch.object(engine, "_close_segment") as close:
                engine._finish_worker(self.forge, self.db, segment,
                                      "failed", "exit 1", NOW, policies=self.p, facts=facts)
                self.assertEqual(close.call_args.args[7], not bool(others))

    def assert_unconfirmed_merge_keeps_review_stage(self, outcome, message):
        self.run_pass()
        self.finish("implement")
        self.run_pass()
        self.finish("review")
        self.findings()
        writes = list(self.forge.writes)
        with mock.patch.object(self.forge, "merge", side_effect=[outcome, outcome]) as merge:
            for attempt in range(1, 3):
                self.assertEqual(self.run_pass(), [reconcile.Merge(KEY)])
                self.assertEqual(self.db.items()[0].stage, "review")
                self.assertEqual(self.db.items()[0].forge_state, "open")
                self.assertEqual(self.forge.rows[0]["stage"], "review")
                self.assertEqual(self.forge.rows[0]["forge_state"], "open")
                self.assertEqual(self.forge.writes, writes)
                self.assertNotIn("pending_stage", self.db.resume(KEY))
                self.assertEqual(self.db.log(KEY)[-1].message, message)
                self.assertEqual(merge.call_args_list, [mock.call(10, commit=True, issue_key=KEY,
                                                                 approval="", head=tf.tma.HEAD,
                                                                 stage_mark=None)] * attempt)

    def test_queued_merge_keeps_stage_and_is_planned_again_next_pass(self):
        self.assert_unconfirmed_merge_keeps_review_stage("queued #10", "queued #10")

    def test_unknown_merge_keeps_stage_and_is_planned_again_next_pass(self):
        self.assert_unconfirmed_merge_keeps_review_stage("unknown #10", "unknown #10")

    def test_refused_verification_keeps_stage_and_is_planned_again_next_pass(self):
        self.assert_unconfirmed_merge_keeps_review_stage(ForgeError("head changed"), "merge refused: head changed")

    def test_declared_file_scopes_conflict_before_dispatch(self):
        self.db.upsert_item(KEY, stage="implement", forge_state="open")
        self.db.upsert_item(OTHER, stage="implement", forge_state="open")
        self.forge.rows.append({"issue_key": OTHER, "stage": "implement", "forge_state": "open"})
        self.forge.rows[0]["body"] = "## File ownership\n- write: `src/a.py`"
        self.forge.rows[1]["body"] = "## File ownership\n- write: `src/a.py`"
        self.db.record_segment(OTHER, "implement", 1, started_at=NOW.isoformat())
        run_dir = engine.worker._run_dir(self.root, OTHER, "implement", 1)
        self.db.set_resume(OTHER, {"runs": {"implement-1": {
            "pid": 123, "session_id": "fake-session", "identity": "fake-start",
            "run_dir": str(run_dir), "tree": str(self.trees / "fake")}}})
        self.assertEqual(self.run_pass(), [reconcile.Wait(KEY, f"file ownership conflict with {OTHER}")])
        self.assertEqual(self.launched, [])
        self.assertEqual([(r.path, r.mode) for r in self.db.ownership(KEY)], [("src/a.py", "write")])

    def test_launch_worktree_rows_do_not_conflict_two_fresh_issues(self):
        self.forge.rows.append({"issue_key": OTHER, "stage": "implement", "forge_state": "open"})
        self.forge.rows[0]["body"] = "## File ownership\n- read-only: `.`"
        self.forge.rows[1]["body"] = "## File ownership\n- read-only: `.`"
        self.db.upsert_item(KEY, stage="implement", forge_state="open")
        self.db.upsert_item(OTHER, stage="implement", forge_state="open")
        self.db.set_ownership(KEY, str(self.trees / "issue-1-implement-1"))
        self.db.set_ownership(OTHER, str(self.trees / "issue-2-implement-1"))
        self.assertEqual({a.issue_key for a in self.run_pass()}, {KEY, OTHER})
        facts = engine.engine_facts.gather(forge=self.forge, store=self.db, root=self.root,
                                          trees=self.trees, now=self.now)
        self.assertEqual(set(facts.read_only), {KEY, OTHER})

    def test_undeclared_fresh_issues_conflict_in_the_same_pass(self):
        self.forge.rows.append({"issue_key": OTHER, "stage": "implement", "forge_state": "open"})
        self.assertEqual(self.run_pass(), [reconcile.Dispatch(KEY, "implement", "builder", 1),
                                          reconcile.Wait(OTHER, f"file ownership conflict with {KEY}")])

    def test_scope_edits_replace_old_claims_and_keep_worktree_registrations(self):
        self.forge.rows[0]["body"] = "## File ownership\n- write: `src/a.py`"
        self.run_pass()
        tree = str(self.trees / "fake")
        self.db.set_ownership(KEY, tree)
        self.forge.rows[0]["body"] = "## File ownership\n- read-only: `docs/`"
        self.run_pass()
        self.assertEqual({(r.path, r.mode) for r in self.db.ownership(KEY)}, {(tree, "tree"), ("docs", "read")})
        self.forge.rows[0]["body"] = "scope removed"
        self.run_pass()
        self.assertEqual({(r.path, r.mode) for r in self.db.ownership(KEY)}, {(tree, "tree"), (".", "write")})

    def test_root_layout_keeps_declared_file_scopes(self):
        self.trees = self.root
        self.forge.rows[0]["body"] = "## File ownership\n- write: `src/`"
        self.forge.rows.append({"issue_key": OTHER, "stage": "implement", "forge_state": "open",
                                "body": "## File ownership\n- write: `src/a.py`"})
        self.assertEqual(self.run_pass(), [reconcile.Dispatch(KEY, "implement", "builder", 1),
                                          reconcile.Wait(OTHER, f"file ownership conflict with {KEY}")])

    def test_a_closed_issue_still_releases_its_gone_worker_slot(self):
        self.run_pass()
        self.forge.rows[0]["forge_state"] = "closed"
        with mock.patch.object(engine.worker.process_status, "identity", return_value=None):
            actions = self.run_pass()
        self.assertIsInstance(actions[0], reconcile.Close)
        self.assertIsNotNone(self.db.segments(KEY)[0].ended_at)

    def test_a_root_level_or_missing_tree_holds_a_gone_worker(self):
        self.trees = self.root
        self.run_pass()
        with mock.patch.object(engine.worker.process_status, "identity", return_value=None):
            actions = self.run_pass()
        self.assertTrue(actions[0].human_review)
        self.assertTrue(self.db.items()[0].human_review)

    def test_unreadable_live_tree_still_reaches_stall_judgement(self):
        self.run_pass()
        self.now += timedelta(hours=1)
        self.assertEqual(self.run_pass(), [reconcile.Wait(KEY, "stalled")])

    def publish_exit(self, outcome="done", stage="implement"):
        run = Path(self.db.resume(KEY)["runs"][f"{stage}-1"]["run_dir"])
        run.mkdir(parents=True, exist_ok=True)
        (run / "stdout").write_text(json.dumps({"is_error": False, "result": "finished"}))
        (run / "stderr").write_text("")
        (run / "exit.json").write_text('{"exit_code": 0}')
        self.db.add_report(KEY, stage, 1, "fake-session", outcome, "finished", [])

    def test_exited_workers_and_planning_share_one_facts_gather_per_pass(self):
        self.forge.rows.append({**self.forge.rows[0], "issue_key": OTHER})
        for key in (KEY, OTHER):
            self.db.upsert_item(key, stage="implement")
            self.db.record_segment(key, "implement", 1, started_at=NOW.isoformat())
            self.launch(key, "implement", 1, "builder")
            run = Path(self.db.resume(key)["runs"]["implement-1"]["run_dir"])
            run.mkdir(parents=True)
            (run / "stdout").write_text('{"is_error": false, "result": "finished"}')
            (run / "stderr").write_text("")
            (run / "exit.json").write_text('{"exit_code": 0}')
            self.db.add_report(key, "implement", 1, "fake-session", "done", "finished", [])
        with mock.patch.object(engine.engine_facts, "gather", wraps=engine.engine_facts.gather) as gather, \
             mock.patch.object(engine.reconcile, "completion_verdict", wraps=engine.reconcile.completion_verdict) as verdict, \
             mock.patch.object(engine.reconcile, "plan", return_value=[]) as plan:
            self.run_pass()
        gather.assert_called_once()
        self.assertEqual(verdict.call_count, 2)
        for call in verdict.call_args_list:
            self.assertIs(call.args[2], plan.call_args.kwargs["facts"])
        self.assertTrue(all(s.ended_at is not None for s in self.db.segments()))

    def test_a_failed_open_closer_read_waits_for_this_pass_and_recovers_next_pass(self):
        self.forge.rows[0].update(forge_state="closed", merge_state="merged")
        self.forge.rows.append({**self.forge.rows[0], "issue_key": OTHER})
        pipeline = tf.forge.Pipeline(1, "main", "squashed", "passed", "")
        self.forge.branch_head = lambda ref: pipeline
        gh = tf.FakeGh(fail=[("pr", "list")])
        adapter = tf.forge.Forge(tf.backlog.GitHubBacklog("example/project"), gh=gh)
        self.forge.landings = adapter.landings
        merged = {"number": 21, "headRefOid": "head", "body": "Closes #1, #2",
                  "baseRefName": "main", "mergeCommit": {"oid": "squashed"}}
        with mock.patch.object(tf.forge.Forge, "_merged_requests", return_value=[merged]):
            actions = self.run_pass()
            self.assertEqual({a.issue_key for a in actions if isinstance(a, reconcile.Wait)}, {KEY, OTHER})
            self.assertEqual(self.cleanup.call_args.kwargs["completed"], set())
            for key in (KEY, OTHER):
                self.assertFalse((self.db.resume(key) or {}).get("verified_landing"))
            gh.fail.clear()
            self.assertEqual(self.run_pass(), [])
        self.assertEqual(self.cleanup.call_args.kwargs["completed"], {KEY, OTHER})
        for key in (KEY, OTHER):
            self.assertIs(self.db.resume(key)["verified_landing"], True)

    def test_normal_exit_enforces_read_only_scope_before_closing(self):
        self.forge.rows[0]["body"] = "## File ownership\n- read-only: `.`"
        self.run_pass()
        self.publish_exit()
        self.forge.branch_head = lambda ref: SimpleNamespace(sha="main-sha", status="passed")
        # HEAD changed after launch, but all commits are pushed and on main: only the read-only rule holds it.
        with mock.patch.object(engine.engine_facts, "_tree_facts", return_value=(TreeState("work", 0, 0, 0), True, NOW)), \
             mock.patch.object(engine.engine_facts.git_trees, "discover", return_value=[self.trees / "fake"]), \
             mock.patch.object(engine.engine_facts.git_trees, "git", return_value=(0, "current-head")):
            self.run_pass()
            self.assertTrue(self.db.items()[0].human_review)
            self.assertTrue(any("worker implement attempt 1:" in row.message and "read-only run left changes" in row.message
                            for row in self.db.log(KEY)))
            self.assertEqual(self.db.segments(KEY)[0].outcome, "done")
            self.assertEqual(len(self.launched), 1)

    def test_writable_control_with_the_same_clean_on_main_tree_has_no_hold(self):
        self.run_pass()
        self.publish_exit()
        self.forge.branch_head = lambda ref: SimpleNamespace(sha="main-sha", status="passed")
        with mock.patch.object(engine.engine_facts, "_tree_facts", return_value=(TreeState("work", 0, 0, 0), True, NOW)), \
             mock.patch.object(engine.engine_facts.git_trees, "discover", return_value=[self.trees / "fake"]), \
             mock.patch.object(engine.engine_facts.git_trees, "git", return_value=(0, "current-head")):
            self.assertEqual(self.run_pass(), [reconcile.Review(KEY, 1)])
        self.assertFalse(self.db.items()[0].human_review)

    def test_clean_premerge_done_advances_without_a_landing_hold(self):
        self.run_pass()
        self.publish_exit()
        with mock.patch.object(engine.engine_facts, "_tree_facts", return_value=(TreeState("work", 0, 1, 0), False, NOW)), \
             mock.patch.object(engine.engine_facts.git_trees, "discover", return_value=[self.trees / "fake"]):
            self.assertEqual(self.run_pass(), [reconcile.Review(KEY, 1)])
        self.assertFalse(self.db.items()[0].human_review)
        self.assertEqual(self.db.segments(KEY)[0].outcome, "done")

    def test_clean_detached_review_finishes_without_a_human_hold(self):
        self.forge.rows[0].update(stage="review", body="## File ownership\n- read-only: `.`")
        self.run_pass()
        self.publish_exit(stage="review")
        with mock.patch.object(engine.engine_facts, "_tree_facts", return_value=(TreeState("(detached)", 0, 2, 0), False, NOW)), \
             mock.patch.object(engine.engine_facts, "_changed", return_value=False):
            self.run_pass()
        self.assertFalse(self.db.items()[0].human_review)
        self.assertEqual(self.db.segments(KEY)[0].outcome, "done")

    def test_releasing_a_read_only_completion_hold_allows_the_next_stage(self):
        self.forge.rows[0]["body"] = "## File ownership\n- read-only: `.`"
        self.forge.kanban = SimpleNamespace(stage_map={}, holds=lambda stage: False, hold_stages=())
        self.forge.add_human_hold = lambda number: self.forge.rows[0].update(human_review=True)
        self.run_pass()
        self.publish_exit()
        with mock.patch.object(engine.engine_facts, "_tree_facts", return_value=(TreeState("work", 1, 2, None), False, NOW)):
            self.assertEqual(self.run_pass(), [reconcile.Wait(KEY, "human review")])
            self.forge.rows[0]["human_review"] = False
            self.assertEqual(self.run_pass(), [reconcile.Review(KEY, 1)])
        self.assertFalse(self.db.items()[0].human_review)
        self.assertEqual(len(self.launched), 2)

    def test_terminal_worker_exit_requires_landing_from_its_workflow(self):
        self.p.workflows["standard"] = policy.Workflow("standard", "implement")
        self.run_pass()
        self.publish_exit()
        with mock.patch.object(engine.engine_facts, "_tree_facts", return_value=(TreeState("work", 0, 2, 0), False, NOW)):
            self.assertEqual(self.run_pass(), [reconcile.Wait(KEY, "human review")])
        self.assertTrue(any("not on main" in row.message for row in self.db.log(KEY)))

    def test_both_completion_paths_use_the_same_missing_and_unknown_facts(self):
        self.run_pass()
        segment = self.db.segments(KEY)[0]
        for changed, unreadable in (((), ()), (((KEY, None),), ()), (((KEY, None),), (KEY,))):
            facts = reconcile.Facts(alive=(((KEY, "implement", 1), False),), changed=changed, unreadable=unreadable)
            planned = reconcile._abandoned(self.db.items()[0], segment, facts, self.p.workflow_for(None))
            with self.subTest(changed=changed, unreadable=unreadable), \
                 mock.patch.object(engine, "_close_segment") as close:
                engine._finish_worker(self.forge, self.db, segment,
                                      "failed", "", NOW, policies=self.p, facts=facts)
                self.assertEqual((close.call_args.args[5], close.call_args.args[7]), (planned.outcome, planned.human_review))
                if planned.human_review:
                    self.assertEqual(close.call_args.args[6], planned.reason)

    def test_unreadable_failed_and_quota_runs_are_held_without_advancing(self):
        self.forge.rows[0]["body"] = "## File ownership\n- read-only: `.`"
        self.run_pass()
        self.publish_exit("failed")
        self.run_pass()  # the fake recorded tree is missing
        self.assertEqual(self.db.segments(KEY)[0].outcome, "failed")
        self.assertTrue(self.db.items()[0].human_review)
        self.assertTrue(any("worker tree could not be read" in row.message for row in self.db.log(KEY)))

    def test_retryable_exits_keep_their_outcome_and_tree_without_an_orphan_hold(self):
        self.run_pass()
        self.publish_exit()
        for attempt, outcome in enumerate(("quota", "unknown", "blocked"), 1):
            if attempt > 1:
                self.db.record_segment(KEY, "implement", attempt, started_at=NOW.isoformat())
                data = self.db.resume(KEY)
                data["runs"][f"implement-{attempt}"] = dict(data["runs"]["implement-1"])
                self.db.set_resume(KEY, data)
            with self.subTest(outcome=outcome), \
                 mock.patch.object(engine.engine_facts, "_tree_facts", return_value=(TreeState("work", 1, 2, None), False, NOW)), \
                 mock.patch.object(engine.engine_facts.git_trees, "discover", return_value=[self.trees / "fake"]):
                segment = self.db.segments(KEY)[-1]
                facts = engine.engine_facts.gather(forge=self.forge, store=self.db, root=self.root,
                                                  trees=self.trees, now=NOW)
                engine._finish_worker(self.forge, self.db, segment, outcome, "retry later", NOW,
                                      policies=self.p, facts=facts)
                self.assertFalse(self.db.items()[0].human_review)
                self.assertEqual(self.db.segments(KEY)[-1].outcome, outcome)
                self.assertIn("retry later", self.db.log(KEY)[-1].message)

    def test_read_only_verdict_preserves_failure_after_a_failed_adapter_exit(self):
        self.forge.rows[0]["body"] = "## File ownership\n- read-only: `.`"
        self.run_pass()
        self.publish_exit("failed")
        with mock.patch.object(engine.engine_facts, "_tree_facts", return_value=(TreeState("work", 1, 1, 0), False, NOW)), \
             mock.patch.object(engine.engine_facts.git_trees, "discover", return_value=[self.trees / "fake"]):
            self.run_pass()
        self.assertTrue(self.db.items()[0].human_review)
        self.assertEqual(self.db.segments(KEY)[0].outcome, "failed")
        self.assertTrue(any("worker implement attempt 1:" in row.message and "read-only run left changes" in row.message
                            for row in self.db.log(KEY)))

    def test_root_layout_reads_a_live_tree_and_advances_a_clean_read_only_exit(self):
        self.trees = self.root
        self.forge.rows[0]["body"] = "## File ownership\n- read-only: `.`"
        self.run_pass()
        self.now += timedelta(hours=1)
        with mock.patch.object(engine.engine_facts, "_tree_facts", return_value=(TreeState("work", 0, 0, 0), True, self.now)), \
             mock.patch.object(engine.engine_facts.git_trees, "git", return_value=(0, "starting-head")):
            self.assertEqual(self.run_pass(), [])
            self.publish_exit()
            self.assertEqual(self.run_pass(), [reconcile.Review(KEY, 1)])
        self.assertFalse(self.db.items()[0].human_review)

    def test_malformed_record_requires_a_human_hold_before_closing(self):
        self.run_pass()
        self.db.set_resume(KEY, {"runs": []})
        self.run_pass()
        self.assertTrue(self.db.items()[0].human_review)
        self.assertEqual(len(self.launched), 1)

    def test_a_failed_adapter_exit_with_unlanded_changes_requires_a_hold(self):
        self.run_pass()
        run = Path(self.db.resume(KEY)["runs"]["implement-1"]["run_dir"])
        run.mkdir(parents=True)
        (run / "stdout").write_text(json.dumps({"is_error": True, "result": "failed"}))
        (run / "stderr").write_text("")
        (run / "exit.json").write_text('{"exit_code": 1}')
        with mock.patch.object(engine.engine_facts, "_tree_facts", return_value=(TreeState("work", 1, 2, None), False, NOW)), \
             mock.patch.object(engine.engine_facts.git_trees, "discover", return_value=[self.trees / "fake"]):
            self.run_pass()
        self.assertTrue(self.db.items()[0].human_review)
        self.assertEqual(len(self.launched), 1)

    def test_failed_completion_hold_write_keeps_the_segment_open(self):
        self.run_pass()
        self.db.set_resume(KEY, {"runs": []})
        self.forge.kanban = SimpleNamespace(holds=lambda stage: False, stage_map={}, hold_stages=())
        self.forge.add_human_hold = lambda number: "forge offline"
        self.run_pass()
        self.assertIsNone(self.db.segments(KEY)[0].ended_at)
        self.assertFalse(self.db.items()[0].human_review)
        self.assertIn("close failed: forge offline", self.db.log(KEY)[-1].message)

    def test_snapshot_bodies_avoid_per_issue_reads_even_for_closed_issues(self):
        self.run_pass()
        self.publish_exit()
        self.forge.rows[0]["forge_state"] = "closed"
        with mock.patch.object(self.forge, "issue", side_effect=ForgeError("offline")) as read, \
             mock.patch.object(engine.engine_facts, "_tree_facts", return_value=(TreeState("work", 0, 0, 0), True, NOW)):
            self.run_pass()
        read.assert_not_called()
        self.assertIsNotNone(self.db.segments(KEY)[0].ended_at)
        self.cleanup.assert_called()

    def test_a_failed_ownership_read_keeps_existing_scopes_and_collects_workers(self):
        self.forge.rows[0]["body"] = "## File ownership\n- read-only: `src/`"
        self.run_pass()
        self.publish_exit("blocked")
        self.forge.rows[0]["body"] = None  # an older snapshot provider needs a per-issue fallback
        with mock.patch.object(self.forge, "issue", side_effect=ForgeError("offline")), \
             mock.patch.object(engine.engine_facts, "_tree_facts", return_value=(TreeState("work", 0, 0, 0), True, NOW)):
            self.run_pass()
        self.assertEqual([(r.path, r.mode) for r in self.db.ownership(KEY)], [("src", "read")])
        self.assertIsNotNone(self.db.segments(KEY)[0].ended_at)
        self.cleanup.assert_called()

    def test_a_new_issue_with_an_unreadable_body_defaults_to_repository_ownership(self):
        self.forge.rows[0]["body"] = None
        with mock.patch.object(self.forge, "issue", side_effect=ForgeError("offline")):
            self.assertEqual(self.run_pass(), [reconcile.Dispatch(KEY, "implement", "builder", 1)])
        self.assertEqual([(r.path, r.mode) for r in self.db.ownership(KEY)], [(".", "write")])
        self.assertIn("ownership read failed: offline", self.db.log(KEY)[0].message)

    def test_a_close_that_holds_for_review_writes_the_hold_to_the_forge(self):
        self.forge.kanban = SimpleNamespace(holds=lambda stage: False, stage_map={}, hold_stages=())
        self.forge.holds = []
        self.forge.add_human_hold = lambda number: self.forge.holds.append(number) or ""
        self.db.upsert_item(KEY, stage="implement")
        self.db.record_segment(KEY, "implement", 1, started_at=NOW.isoformat())
        self.launch(KEY, "implement", 1, "builder")
        action = reconcile.Close(KEY, "implement", 1, "done", "read-only run left changes", True)
        with mock.patch.object(engine.reconcile, "plan", return_value=[action]):
            self.run_pass()
        self.assertEqual(self.forge.holds, [1])
        self.assertEqual(self.db.segments(KEY)[0].outcome, "done")

    def test_a_close_whose_hold_write_fails_leaves_the_segment_open(self):
        self.forge.kanban = SimpleNamespace(holds=lambda stage: False, stage_map={}, hold_stages=())
        self.forge.add_human_hold = lambda number: "forge offline"
        self.db.upsert_item(KEY, stage="implement")
        self.db.record_segment(KEY, "implement", 1, started_at=NOW.isoformat())
        self.launch(KEY, "implement", 1, "builder")
        action = reconcile.Close(KEY, "implement", 1, "done", "read-only run left changes", True)
        with mock.patch.object(engine.reconcile, "plan", return_value=[action]):
            self.run_pass()
        self.assertIsNone(self.db.segments(KEY)[0].ended_at)
        self.assertIn("forge offline", self.db.log(KEY)[-1].message)

    def test_closed_forge_snapshot_is_not_dispatched(self):
        self.db.upsert_item(KEY, forge_state="open")
        self.forge.rows[0]["forge_state"] = "closed"
        self.assertEqual(self.run_pass(), [])
        self.assertEqual(self.db.items()[0].forge_state, "closed")
        self.assertEqual(self.launched, [])

    def test_successful_open_only_read_closes_absent_items(self):
        self.db.upsert_item(OTHER, forge_state="open")
        self.run_pass()
        self.assertEqual(next(i for i in self.db.items() if i.issue_key == OTHER).forge_state, "closed")
        self.assertEqual([call[0] for call in self.launched], [KEY])

    def test_failed_forge_read_closes_nothing_and_launches_nothing(self):
        self.db.upsert_item(OTHER, forge_state="open")
        with mock.patch.object(self.forge, "snapshots", side_effect=RuntimeError("forge offline")):
            with self.assertRaisesRegex(RuntimeError, "forge offline"):
                self.run_pass()
        self.assertEqual(self.db.items()[0].forge_state, "open")
        self.assertEqual(self.launched, [])
        self.cleanup.assert_not_called()

    def test_partial_read_is_materialized_before_sync_or_closures(self):
        self.db.upsert_item(OTHER, forge_state="open")

        def partial():
            yield {"issue_key": OTHER, "forge_state": "closed"}
            raise RuntimeError("partial read")

        with mock.patch.object(self.forge, "snapshots", side_effect=partial):
            with self.assertRaisesRegex(RuntimeError, "partial read"):
                self.run_pass()
        self.assertEqual(self.db.items()[0].forge_state, "open")

    def test_one_failed_action_does_not_stop_the_next(self):
        self.forge.rows[0]["body"] = "## File ownership\n- write: `src/`"
        self.forge.rows.append({"issue_key": OTHER, "stage": "implement", "forge_state": "open",
                                "body": "## File ownership\n- write: `docs/`"})
        self.launch = mock.Mock(side_effect=[RuntimeError("spawn failed"), ("model", "low")])
        self.run_pass()
        self.assertEqual(self.launch.call_args_list[-1].args, (OTHER, "implement", 1, "builder"))
        self.assertEqual([(s.issue_key, s.outcome) for s in self.db.segments()], [(KEY, "failed"), (OTHER, None)])
        self.assertIn("spawn failed", self.db.log(KEY)[-1].message)
        self.cleanup.assert_called_once()

    def test_session_launch_policy_is_refused_not_silently_adapted(self):
        self.p = policy.parse({
            "default_workflow": "standard",
            "policy": {"launch": "session"},
            "personas": {"builder": {"role": "implements", "model": "example"},
                         "critic": {"role": "reviews", "model": "example"}},
            "stages": {"implement": {"persona": "builder"}, "review": {"persona": "critic"}, "merge": {}},
            "workflows": {"standard": {"start": "implement", "edges": [
                {"from": "implement", "to": "review"}, {"from": "review", "to": "merge"},
            ]}},
        })
        self.assertEqual(self.run_pass(), [reconcile.Dispatch(KEY, "implement", "builder", 1)])
        self.assertEqual(self.launched, [])
        self.assertEqual(self.db.segments(), [])
        self.assertIn("launch=session is not supported", self.db.log(KEY)[-1].message)

    def test_repeated_wait_logs_once_but_a_changed_reason_logs_again(self):
        self.forge.rows[0]["stage"] = "outside"
        self.run_pass()
        self.run_pass()
        self.assertEqual([log.message for log in self.db.log(KEY)], ["wait: stage is outside the workflow"])
        self.db.set_human_review(KEY, True)
        self.run_pass()
        self.assertEqual(self.db.log(KEY)[-1].message, "wait: human review")

    def test_ask_and_escalate_are_recorded_once_and_preserve_resume_data(self):
        self.db.sync_from_forge(self.forge.snapshots())
        for action in (reconcile.Ask(KEY, "plan", "approval needed"), reconcile.Escalate(KEY, "retry limit")):
            with self.subTest(action=action):
                self.db.set_resume(KEY, {"session": "keep"})
                before = len(self.db.log(KEY))
                with mock.patch.object(engine.reconcile, "plan", return_value=[action]):
                    self.run_pass()
                    self.run_pass()
                self.assertEqual(len(self.db.log(KEY)), before + 1)
                self.assertEqual(self.db.resume(KEY)["session"], "keep")
                self.assertIn("interrupt", self.db.resume(KEY))

    def test_free_slots_cap_dispatches_including_existing_workers(self):
        self.forge.rows[0]["body"] = "## File ownership\n- write: `src/`"
        self.forge.rows.append({"issue_key": OTHER, "stage": "implement", "forge_state": "open",
                                "body": "## File ownership\n- write: `docs/`"})
        self.run_pass(max_concurrency=1)
        self.assertEqual([call[0] for call in self.launched], [KEY])
        self.assertEqual(self.db.log(OTHER)[-1].message, "wait: no free worker slots")
        self.run_pass(max_concurrency=1)
        self.assertEqual(len(self.launched), 1)

    def test_unknown_or_stale_review_findings_do_not_merge(self):
        self.run_pass()
        self.finish("implement")
        self.run_pass()
        self.finish("review")
        for resume in (None, {"review_findings": {"attempt": 0, "counts": {}}},
                       {"review_findings": {"attempt": 1, "counts": {"minor": -1}}}):
            with self.subTest(resume=resume):
                self.db.set_resume(KEY, resume)
                self.assertEqual(self.run_pass(), [reconcile.Wait(KEY, "review findings are unknown")])
        self.assertEqual(self.forge.merges, [])

    def test_pending_decision_at_merge_prevents_a_merge_plan(self):
        self.forge.rows[0]["stage"] = "merge"
        self.db.sync_from_forge(self.forge.snapshots())
        self.db.record_segment(KEY, "review", 1, NOW.isoformat(), NOW.isoformat(), "done")
        self.findings()
        self.db.add_decision(KEY, "design", "Split the parser?")
        with mock.patch.object(engine.engine_merge, "execute") as merged:
            self.assertEqual(self.run_pass(), [reconcile.Wait(KEY, "decision pending")])
        merged.assert_not_called()
        self.assertEqual(self.forge.merges, [])

    def test_blocking_findings_follow_the_planned_fix_stage(self):
        self.run_pass()
        self.finish("implement")
        self.run_pass()
        self.finish("review")
        self.findings(counts={"minor": 1})
        self.assertEqual(self.run_pass(), [reconcile.Dispatch(KEY, "implement", "builder", 2)])
        self.assertEqual(self.forge.writes[-1], (1, "implement", "review", True))

    def test_the_engine_initializes_only_when_the_store_has_no_prior_stage(self):
        for prior, initialize in (("", True), ("review", False)):
            with self.subTest(prior=prior):
                forge = mock.Mock()
                forge.write_stage.return_value = SimpleNamespace(ok=True, error="")
                engine._write_stage(forge, mock.Mock(), KEY, "implement", prior)
                self.assertIs(forge.write_stage.call_args.kwargs["initialize"], initialize)

    def test_stage_write_failure_is_logged_and_retried_before_planning(self):
        self.run_pass()
        self.finish("implement")
        with mock.patch.object(self.forge, "write_stage", return_value=SimpleNamespace(ok=False, error="label offline")):
            self.run_pass()
        self.assertIn("label offline", self.db.log(KEY)[-1].message)
        with mock.patch.object(self.forge, "write_stage", return_value=SimpleNamespace(ok=False, error="label offline")):
            self.run_pass()
            self.run_pass()
        self.assertEqual(sum(e.message == "stage write failed: label offline" for e in self.db.log(KEY)), 1)  # a repeat is not logged again
        self.finish("review")
        self.findings()
        self.run_pass()
        self.assertEqual(self.forge.writes[0], (1, "review", "implement", True))
        self.assertEqual(self.forge.writes[-1], (1, "merge", "review", True))

    def test_merge_stage_write_is_retried_after_closed_issue_disappears(self):
        self.run_pass()
        self.finish("implement")
        self.run_pass()
        self.finish("review")
        self.findings()
        with mock.patch.object(self.forge, "write_stage", return_value=SimpleNamespace(ok=False, error="offline")):
            self.assertEqual(self.run_pass(), [reconcile.Merge(KEY)])
        self.assertEqual(self.db.resume(KEY)["pending_stage"], {"stage": "merge", "expected": "review"})
        self.assertEqual(self.forge.rows[0]["forge_state"], "closed")
        # Production snapshots contain only open issues; label writes still address the closed issue.
        with mock.patch.object(self.forge, "snapshots", return_value=[]):
            self.assertEqual(self.run_pass(), [])
            self.assertEqual(self.db.items()[0].stage, "merge")
            self.assertNotIn("pending_stage", self.db.resume(KEY))
            self.assertEqual(self.run_pass(), [])
        self.assertEqual(self.db.items()[0].forge_state, "closed")
        self.assertEqual(self.forge.writes[-1], (1, "merge", "review", True))
        self.assertEqual(len(self.forge.merges), 1)
        self.assertEqual(len(self.launched), 2)

    def test_stage_write_human_hold_prevents_merge_after_worker_finishes(self):
        gh = tf.FakeGh(issues=[tf.gh_issue(1, "Held review", ["stage: doing"])],
                       prs=[tf.tma.pr(10, [])], bodies={10: "Closes #1"})
        board = Kanban(tf.FLOW.stages, "needs human", {"implement": 1, "review": 2, "merge": 0},
                       hold_stages=("review",))
        self.forge = engine.forge.Forge(tf.backlog.GitHubBacklog("example/project"), kanban=board, gh=gh)
        self.run_pass()
        self.finish("implement")
        self.assertEqual(self.run_pass(), [reconcile.Review(KEY, 1)])
        self.assertIn("needs human", [label["name"] for label in gh.issues[1]["labels"]])
        self.finish("review")
        self.findings()
        self.assertEqual(self.forge.snapshots()[0]["merge_state"], "ready")
        self.assertEqual(self.run_pass(), [reconcile.Wait(KEY, "human review")])
        self.assertTrue(self.db.items()[0].human_review)
        self.findings(counts={"minor": 1})
        self.assertEqual(self.run_pass(), [reconcile.Wait(KEY, "human review")])
        self.assertEqual(len(self.launched), 2)
        gh.issues[1]["labels"] = [label for label in gh.issues[1]["labels"] if label["name"] != "needs human"]
        self.assertEqual(self.run_pass(), [reconcile.Dispatch(KEY, "implement", "builder", 2)])
        self.assertFalse(self.db.items()[0].human_review)

    def test_implementation_relaunch_releases_hold_before_starting_the_next_worker(self):
        self.assert_relaunch_releases_hold("implement")

    def test_review_relaunch_releases_hold_before_starting_the_next_worker(self):
        self.assert_relaunch_releases_hold("review")

    def assert_relaunch_releases_hold(self, stage):
        gh = tf.FakeGh(issues=[tf.gh_issue(1, "Retry", ["stage: doing", "bug"])])
        board = Kanban(tf.FLOW.stages, "needs human", {"implement": 1, "review": 2, "merge": 0})
        gh.issues[1]["labels"][0]["name"] = board.label_for(stage)
        self.forge = engine.forge.Forge(tf.backlog.GitHubBacklog("example/project"), kanban=board, gh=gh)
        self.db.sync_from_forge(self.forge.snapshots())
        self.db.set_resume(KEY, {"keep": "context"})
        # A failed start is the real transition: it restores the hold and records the ownership.
        with mock.patch.object(self, "launch", side_effect=RuntimeError("spawn failed")):
            self.run_pass()
        self.assertEqual(self.db.resume(KEY)["launcher_hold"], {"stage": stage, "attempt": 1})
        self.assertIn("needs human", [x["name"] for x in gh.issues[1]["labels"]])

        def launch(key, launched_stage, attempt, persona):
            self.assertNotIn("needs human", [x["name"] for x in gh.issues[1]["labels"]])
            self.assertFalse(self.db.items()[0].human_review)
            return self.launch(key, launched_stage, attempt, persona)

        with mock.patch.object(self.forge.__class__, "write_stage",
                               side_effect=self.forge.write_stage) as write:
            actions = engine.run_pass(forge=self.forge, store=self.db, policies=self.p, root=self.root,
                                      trees=self.trees, launch=launch, workers=engine.settings.Workers(),
                                      clock=lambda: self.now)
        self.assertIsInstance(actions[0], reconcile.Review if stage == "review" else reconcile.Dispatch)
        write.assert_called_once_with(1, stage, expected=stage, initialize=False, commit=True,
                                      release_hold=True)
        self.assertEqual(self.launched[-1][1:3], (stage, 2))
        self.assertIn("bug", [x["name"] for x in gh.issues[1]["labels"]])
        self.assertEqual(self.db.resume(KEY)["keep"], "context")

    def test_failed_hold_release_keeps_human_review_and_does_not_launch(self):
        gh = tf.FakeGh(issues=[tf.gh_issue(1, "Retry", ["stage: doing", "needs human"])],
                       fail=[("issue", "edit")])
        board = Kanban(tf.FLOW.stages, "needs human", {"implement": 1, "review": 2, "merge": 0})
        self.forge = engine.forge.Forge(tf.backlog.GitHubBacklog("example/project"), kanban=board, gh=gh)
        self.db.sync_from_forge(self.forge.snapshots())
        self.db.record_segment(KEY, "implement", 1, NOW.isoformat(), NOW.isoformat(), "failed")
        self.db.set_resume(KEY, {"launcher_hold": {"stage": "implement", "attempt": 1}})
        self.run_pass()
        self.assertTrue(self.db.items()[0].human_review)
        self.assertIn("needs human", [x["name"] for x in gh.issues[1]["labels"]])
        self.assertEqual(self.launched, [])
        self.assertEqual(len(self.db.segments(KEY)), 1)
        self.assertIn("hold release failed", self.db.log(KEY)[-1].message)
        self.assertIn("rate limited", self.db.log(KEY)[-1].message)
        gh.fail.clear()
        self.run_pass()
        self.assertEqual(self.launched, [(KEY, "implement", 2, "builder")])

    def test_relaunch_keeps_a_stage_required_hold(self):
        gh = tf.FakeGh(issues=[tf.gh_issue(1, "Held retry", ["stage: doing", "needs human"])])
        board = Kanban(tf.FLOW.stages, "needs human", {"implement": 1, "review": 2, "merge": 0},
                       hold_stages=("implement",))
        self.forge = engine.forge.Forge(tf.backlog.GitHubBacklog("example/project"), kanban=board, gh=gh)
        self.db.sync_from_forge(self.forge.snapshots())
        self.db.record_segment(KEY, "implement", 1, NOW.isoformat(), NOW.isoformat(), "failed")
        self.assertEqual(self.run_pass(), [reconcile.Wait(KEY, "human review")])
        self.assertEqual(self.launched, [])
        self.assertTrue(self.db.items()[0].human_review)
        self.assertFalse(any(c[:2] == ["issue", "edit"] for c in gh.all_calls))

    def test_unverified_release_is_retried_even_if_the_label_was_already_removed(self):
        gh = tf.FakeGh(issues=[tf.gh_issue(1, "Retry", ["stage: doing", "needs human"])])
        board = Kanban(tf.FLOW.stages, "needs human", {"implement": 1, "review": 2, "merge": 0})
        self.forge = engine.forge.Forge(tf.backlog.GitHubBacklog("example/project"), kanban=board, gh=gh)
        self.db.sync_from_forge(self.forge.snapshots())
        self.db.record_segment(KEY, "implement", 1, NOW.isoformat(), NOW.isoformat(), "failed")
        self.db.set_resume(KEY, {"launcher_hold": {"stage": "implement", "attempt": 1}})
        real_gh = gh.__call__

        def fail_verification(args, input_text=None):
            result = real_gh(args, input_text)
            if args[:2] == ["issue", "edit"]:
                gh.fail.add(("issue", "view"))
            return result

        self.forge = engine.forge.Forge(tf.backlog.GitHubBacklog("example/project"), kanban=board,
                                        gh=fail_verification)
        self.run_pass()
        self.assertTrue(self.db.items()[0].human_review)
        self.assertEqual(self.launched, [])
        self.assertNotIn("needs human", [x["name"] for x in gh.issues[1]["labels"]])
        self.assertTrue(self.db.resume(KEY)["pending_stage"]["release_hold"])
        gh.fail.clear()
        # A cached snapshot now says the label is absent, but the guarded release must still verify.
        self.forge = engine.forge.Forge(tf.backlog.GitHubBacklog("example/project"), kanban=board, gh=gh)
        with mock.patch.object(self.forge.__class__, "write_stage",
                               return_value=SimpleNamespace(ok=False, error="verification offline")):
            self.run_pass()
        self.assertEqual(self.launched, [])
        self.assertTrue(self.db.items()[0].human_review)
        self.assertIn("verification offline", self.db.log(KEY)[-1].message)
        self.run_pass()
        self.assertEqual(self.launched, [(KEY, "implement", 2, "builder")])
        self.assertFalse(self.db.items()[0].human_review)
        self.assertNotIn("pending_stage", self.db.resume(KEY))

    def pending_release(self, *, stage="implement", held=True):
        self.forge.kanban = Kanban(tf.FLOW.stages, "needs human", {"implement": 1, "review": 2, "merge": 0})
        self.forge.rows[0].update(stage=stage, human_review=held)
        self.db.sync_from_forge(self.forge.snapshots())
        self.db.record_segment(KEY, "implement", 1, NOW.isoformat(), NOW.isoformat(), "failed")
        self.db.set_resume(KEY, {"launcher_hold": {"stage": "implement", "attempt": 1},
                                 "pending_stage": {"stage": "implement", "expected": "implement", "release_hold": True}})

    def assert_pending_wait(self, condition):
        self.pending_release()
        reason = {"slots": "no free worker slots", "quota": "quota reset pending"}.get(condition)
        if condition == "quota":
            self.db.set_resume(KEY, {**self.db.resume(KEY), "quota": {"retry_at": (NOW + timedelta(hours=1)).isoformat()}})
        if condition == "closed":
            self.forge.rows[0]["forge_state"] = "closed"
        if condition in ("drift", "blank"):
            self.forge.rows[0]["stage"] = "review" if condition == "drift" else ""
        actions = self.run_pass(max_concurrency=0 if condition == "slots" else None)
        self.assertEqual(self.forge.writes, [])
        self.assertEqual(self.launched, [])
        if reason:
            # The gate waits with its release record and ownership intact, then continues once it clears.
            self.assertEqual(actions, [reconcile.Wait(KEY, reason)])
            self.assertTrue(self.db.items()[0].human_review)
            self.assertTrue(self.db.resume(KEY)["pending_stage"]["release_hold"])
            self.assertEqual(self.db.resume(KEY)["launcher_hold"], {"stage": "implement", "attempt": 1})
            if condition == "quota":
                self.now = NOW + timedelta(hours=2)
            self.run_pass()
            self.assertEqual(self.launched, [(KEY, "implement", 2, "builder")])
        if condition in ("closed", "drift", "blank"):
            self.assertNotIn("pending_stage", self.db.resume(KEY))

    def test_pending_release_waits_for_slots(self):
        self.assert_pending_wait("slots")

    def test_pending_release_waits_for_quota(self):
        self.assert_pending_wait("quota")

    def test_pending_release_is_dropped_when_closed(self):
        self.assert_pending_wait("closed")

    def test_pending_release_is_dropped_after_stage_drift(self):
        self.assert_pending_wait("drift")

    def test_pending_release_is_dropped_for_a_blank_stage(self):
        self.assert_pending_wait("blank")

    def test_permanent_release_refusal_escalates_instead_of_retrying_forever(self):
        self.pending_release(held=False)
        self.p = replace(self.p, base=policy.Policy(retry_limit=1))
        with mock.patch.object(self.forge, "write_stage", return_value=SimpleNamespace(ok=False, error="permanent refusal")) as write:
            self.run_pass()
            self.run_pass()
            actions = self.run_pass()
        self.assertEqual(write.call_count, 2)
        self.assertIsInstance(actions[0], reconcile.Escalate)
        self.assertTrue(self.db.items()[0].human_review)
        self.assertEqual(self.launched, [])

    def test_legacy_unverified_release_without_an_owner_returns_to_a_human_hold(self):
        self.pending_release(held=False)
        data = self.db.resume(KEY)
        data.pop("launcher_hold")
        self.db.set_resume(KEY, data)
        self.assertEqual(self.run_pass(), [reconcile.Wait(KEY, "human review")])
        self.assertEqual(self.launched, [])
        self.assertNotIn("pending_stage", self.db.resume(KEY))
        self.assertTrue(self.forge.rows[0]["human_review"])
        self.assertNotIn("pending_hold", self.db.resume(KEY))

    def test_slot_race_does_not_release_the_hold(self):
        self.pending_release()
        with mock.patch.object(engine.slots, "claim", side_effect=ValueError("slot taken")):
            self.run_pass()
        self.assertEqual(self.forge.writes, [])
        self.assertTrue(self.db.items()[0].human_review)

    def test_launch_failure_restores_hold_and_spends_retry_budget(self):
        self.pending_release()
        self.p = replace(self.p, base=policy.Policy(retry_limit=1))
        with mock.patch.object(self.forge, "add_human_hold", create=True, return_value="") as restore:
            with mock.patch.object(self, "launch", side_effect=RuntimeError("spawn failed")):
                self.run_pass()
            restore.assert_called_once_with(1)
        self.assertTrue(self.db.items()[0].human_review)
        self.assertEqual([(s.attempt, s.outcome) for s in self.db.segments(KEY)], [(1, "failed"), (2, "failed")])
        self.assertEqual(self.db.resume(KEY)["launcher_hold"], {"stage": "implement", "attempt": 2})
        self.assertEqual(self.run_pass(), [reconcile.Escalate(KEY, "retry limit reached for implement")])

    def test_launch_failure_creates_the_owned_hold_used_by_the_next_pass(self):
        gh = tf.FakeGh(issues=[tf.gh_issue(1, "Retry", ["stage: doing", "bug"])])
        board = Kanban(tf.FLOW.stages, "needs human", {"implement": 1, "review": 2, "merge": 0})
        self.forge = engine.forge.Forge(tf.backlog.GitHubBacklog("example/project"), kanban=board, gh=gh)
        with mock.patch.object(self, "launch", side_effect=RuntimeError("spawn failed")):
            self.run_pass()
        self.assertIn("needs human", [x["name"] for x in gh.issues[1]["labels"]])
        self.assertEqual(self.db.resume(KEY)["launcher_hold"], {"stage": "implement", "attempt": 1})
        self.run_pass()
        self.assertEqual(self.launched, [(KEY, "implement", 2, "builder")])
        self.assertNotIn("needs human", [x["name"] for x in gh.issues[1]["labels"]])
        self.assertNotIn("launcher_hold", self.db.resume(KEY))

    def test_exhausted_release_returns_to_a_human_releasable_hold(self):
        self.pending_release(held=False)
        self.p = replace(self.p, base=policy.Policy(retry_limit=1))
        with mock.patch.object(self.forge, "write_stage", return_value=SimpleNamespace(ok=False, error="refused")):
            self.run_pass()
            self.run_pass()
            self.run_pass()
        self.assertNotIn("pending_stage", self.db.resume(KEY))
        def restore(number):
            self.forge.rows[0]["human_review"] = True
            return ""
        with mock.patch.object(self.forge, "add_human_hold", create=True, side_effect=restore):
            self.assertEqual(self.run_pass(), [reconcile.Wait(KEY, "human review")])
        self.forge.rows[0]["human_review"] = False
        self.assertIsInstance(self.run_pass()[0], reconcile.Dispatch)

    def test_failed_hold_restoration_survives_the_next_forge_sync(self):
        self.pending_release(held=False)
        with mock.patch.object(self.forge, "add_human_hold", create=True, return_value="offline"):
            with mock.patch.object(self, "launch", side_effect=RuntimeError("spawn failed")):
                self.run_pass()
            self.assertEqual(self.run_pass(), [reconcile.Wait(KEY, "human review")])
        self.assertTrue(self.db.items()[0].human_review)
        self.assertEqual(self.launched, [])

    def test_manual_hold_after_quota_is_not_released_again(self):
        self.pending_release()
        self.run_pass()
        self.finish("implement", 2, "quota")
        self.forge.rows[0]["human_review"] = True
        self.forge.writes.clear()
        self.assertEqual(self.run_pass(), [reconcile.Wait(KEY, "human review")])
        self.assertEqual(self.forge.writes, [])

    def test_stale_ownership_after_a_killed_release_returns_to_a_human_hold(self):
        # A kill inside the reserved release left owner attempt 1; collection closed reservation 2.
        self.pending_release()
        self.db.record_segment(KEY, "implement", 2, NOW.isoformat(), NOW.isoformat(), "failed")
        self.forge.rows[0]["human_review"] = False
        self.assertEqual(self.run_pass(), [reconcile.Wait(KEY, "human review")])
        self.assertEqual((self.forge.writes, self.launched), ([], []))
        self.assertNotIn("pending_stage", self.db.resume(KEY))
        self.assertTrue(self.forge.rows[0]["human_review"])
        self.forge.rows[0]["human_review"] = False
        self.assertEqual(self.run_pass(), [reconcile.Dispatch(KEY, "implement", "builder", 3)])

    def test_failed_review_launch_after_implementation_moves_the_card_and_owns_the_hold(self):
        gh = tf.FakeGh(issues=[tf.gh_issue(1, "Handoff", ["stage: doing", "bug"])])
        board = Kanban(tf.FLOW.stages, "needs human", {"implement": 1, "review": 2, "merge": 0})
        self.forge = engine.forge.Forge(tf.backlog.GitHubBacklog("example/project"), kanban=board, gh=gh)
        self.db.sync_from_forge(self.forge.snapshots())
        self.db.record_segment(KEY, "implement", 1, (NOW - timedelta(minutes=2)).isoformat(),
                               (NOW - timedelta(minutes=1)).isoformat(), "done")
        with mock.patch.object(self, "launch", side_effect=RuntimeError("spawn failed")):
            self.run_pass()
        self.assertEqual(self.db.items()[0].stage, "review")
        self.assertIn("needs human", [x["name"] for x in gh.issues[1]["labels"]])
        self.assertEqual(self.db.resume(KEY)["launcher_hold"], {"stage": "review", "attempt": 1})
        self.assertEqual(self.run_pass(), [reconcile.Review(KEY, 2)])
        self.assertEqual(self.launched[-1][1:3], ("review", 2))
        self.assertNotIn("needs human", [x["name"] for x in gh.issues[1]["labels"]])

    def test_failed_handoff_is_dropped_when_a_person_moves_the_card(self):
        self.assert_failed_handoff_drift("merge", "open", restore=True)

    def test_failed_handoff_drift_does_not_restore_a_hold_on_a_closed_card(self):
        self.assert_failed_handoff_drift("merge", "closed", restore=False)

    def test_failed_handoff_drift_does_not_restore_a_hold_on_a_card_without_a_stage(self):
        self.assert_failed_handoff_drift("", "open", restore=False)

    def assert_failed_handoff_drift(self, stage, state, *, restore):
        self.forge.kanban = Kanban(tf.FLOW.stages, "needs human", {"implement": 1, "review": 2, "merge": 0})
        self.db.sync_from_forge(self.forge.snapshots())
        self.db.record_segment(KEY, "implement", 1, NOW.isoformat(), NOW.isoformat(), "done")
        refused = SimpleNamespace(ok=False, error="forge offline")
        with mock.patch.object(self.forge, "write_stage", return_value=refused):
            with mock.patch.object(self, "launch", side_effect=RuntimeError("spawn failed")):
                self.run_pass()
        self.assertEqual(self.db.resume(KEY)["pending_stage"]["stage"], "review")
        self.forge.rows[0].update(stage=stage, forge_state=state)  # changed while the forge was failing
        self.forge.writes.clear()
        with mock.patch.object(self.forge, "add_human_hold", wraps=self.forge.add_human_hold) as hold:
            self.run_pass()
        self.assertEqual(hold.call_count, int(restore))
        self.assertEqual(self.forge.writes, [])
        self.assertNotIn("pending_stage", self.db.resume(KEY))
        self.assertEqual(bool(self.forge.rows[0].get("human_review")), restore)
        self.assertNotIn("pending_hold", self.db.resume(KEY))

    def test_exhausted_launch_failures_leave_a_plain_human_hold(self):
        self.pending_release()
        self.p = replace(self.p, base=policy.Policy(retry_limit=1))
        with mock.patch.object(self, "launch", side_effect=RuntimeError("spawn failed")):
            self.run_pass()
        self.assertIsInstance(self.run_pass()[0], reconcile.Escalate)
        self.assertNotIn("launcher_hold", self.db.resume(KEY))
        self.assertTrue(self.forge.rows[0]["human_review"])

    def test_a_failed_launch_and_a_failed_restoration_are_both_logged(self):
        self.pending_release(held=False)
        with mock.patch.object(self.forge, "add_human_hold", return_value="offline"):
            with mock.patch.object(self, "launch", side_effect=RuntimeError("spawn failed")):
                self.run_pass()
        message = self.db.log(KEY)[-1].message
        self.assertIn("spawn failed", message)
        self.assertIn("offline", message)

    def test_an_unrelated_launch_error_that_starts_like_a_release_failure_is_logged(self):
        self.pending_release(held=False)
        with mock.patch.object(self, "launch", side_effect=RuntimeError("hold release failed: unrelated")):
            self.run_pass()
        self.assertIn("hold release failed: unrelated", [e.message for e in self.db.log(KEY)][-1])

    def test_merge_hold_is_written_before_merging_and_waits_for_release(self):
        gh = tf.FakeGh(issues=[tf.gh_issue(1, "Held merge", ["stage: doing"])],
                       prs=[tf.tma.pr(10, [])], bodies={10: "Closes #1"})
        board = Kanban(tf.FLOW.stages, "needs human", {"implement": 1, "review": 2, "merge": 0},
                       hold_stages=("merge",))
        self.forge = engine.forge.Forge(tf.backlog.GitHubBacklog("example/project"), kanban=board, gh=gh)
        self.run_pass()
        self.finish("implement")
        self.run_pass()
        self.finish("review")
        self.findings()
        with mock.patch.object(engine.engine_merge, "execute", wraps=engine.engine_merge.execute) as merged:
            self.assertEqual(self.run_pass(), [reconcile.Merge(KEY)])
            merged.assert_not_called()
            self.assertEqual(self.db.items()[0].stage, "merge")
            self.assertIn("needs human", [label["name"] for label in gh.issues[1]["labels"]])
            self.assertEqual(self.run_pass(), [reconcile.Wait(KEY, "human review")])
            merged.assert_not_called()
            gh.issues[1]["labels"] = [label for label in gh.issues[1]["labels"] if label["name"] != "needs human"]
            self.assertEqual(self.run_pass(), [reconcile.Merge(KEY)])
            merged.assert_called_once()
        self.assertEqual(len([call for call in gh.all_calls if call[:2] == ["pr", "merge"]]), 1)
        self.assertNotIn("needs human", [label["name"] for label in gh.issues[1]["labels"]])

    def test_shared_label_preserves_implement_when_it_reads_back_as_plan(self):
        gh = tf.FakeGh(issues=[tf.gh_issue(1, "Shared stages", ["stage: doing"])])
        board = Kanban(tf.FLOW.stages, "needs human",
                       {"plan": 1, "implement": 1, "fix": 1, "review": 2, "merge": 0})
        self.forge = engine.forge.Forge(tf.backlog.GitHubBacklog("example/project"), kanban=board, gh=gh)
        self.p = policy.parse({
            "default_workflow": "standard",
            "personas": {"builder": {"role": "implements", "model": "example"},
                         "critic": {"role": "reviews", "model": "example"}},
            "stages": {"plan": {"persona": "builder"}, "implement": {"persona": "builder"},
                       "review": {"persona": "critic"}, "merge": {}},
            "workflows": {"standard": {"start": "plan", "edges": [
                {"from": "plan", "to": "implement"}, {"from": "implement", "to": "review"},
                {"from": "review", "to": "implement"}, {"from": "review", "to": "merge"},
            ]}},
        })
        self.assertEqual(self.run_pass(), [reconcile.Dispatch(KEY, "plan", "builder", 1)])
        self.finish("plan")
        self.assertEqual(self.run_pass(), [reconcile.Dispatch(KEY, "implement", "builder", 1)])
        self.assertEqual(self.db.items()[0].stage, "implement")
        self.assertEqual(self.forge.snapshots()[0]["stage"], "plan")
        self.finish("implement")
        self.assertEqual(self.run_pass(), [reconcile.Review(KEY, 1)])
        self.assertEqual([call[1] for call in self.launched], ["plan", "implement", "review"])

    def test_retried_merge_hold_write_cannot_merge_on_the_retry_pass(self):
        gh = tf.FakeGh(issues=[tf.gh_issue(1, "Held merge", ["stage: doing"])],
                       prs=[tf.tma.pr(10, [])], bodies={10: "Closes #1"})
        board = Kanban(tf.FLOW.stages, "needs human", {"implement": 1, "review": 2, "merge": 0},
                       hold_stages=("merge",))
        self.forge = engine.forge.Forge(tf.backlog.GitHubBacklog("example/project"), kanban=board, gh=gh)
        self.run_pass()
        self.finish("implement")
        self.run_pass()
        self.finish("review")
        self.findings()
        with mock.patch.object(engine.forge.Forge, "write_stage", return_value=SimpleNamespace(ok=False, error="offline")):
            self.assertEqual(self.run_pass(), [reconcile.Merge(KEY)])
        self.assertFalse(any(call[:2] == ["pr", "merge"] for call in gh.all_calls))
        self.assertEqual(self.db.resume(KEY)["pending_stage"], {"stage": "merge", "expected": "review"})
        self.assertEqual(self.run_pass(), [reconcile.Wait(KEY, "human review")])
        self.assertFalse(any(call[:2] == ["pr", "merge"] for call in gh.all_calls))
        self.assertEqual(self.db.items()[0].stage, "merge")
        self.assertTrue(self.db.items()[0].human_review)
        self.assertNotIn("pending_stage", self.db.resume(KEY))

    def test_manual_move_to_a_different_label_overrides_the_stored_stage(self):
        gh = tf.FakeGh(issues=[tf.gh_issue(1, "Moved by hand", ["stage: review"])])
        board = Kanban(tf.FLOW.stages, "needs human",
                       {"plan": 1, "implement": 1, "fix": 1, "review": 2, "merge": 0})
        self.forge = engine.forge.Forge(tf.backlog.GitHubBacklog("example/project"), kanban=board, gh=gh)
        self.db.upsert_item(KEY, stage="implement")
        self.assertEqual(self.run_pass(), [reconcile.Review(KEY, 1)])
        self.assertEqual(self.db.items()[0].stage, "review")

    def test_unresolved_stage_write_cannot_dispatch_work_from_the_old_stage(self):
        self.run_pass()
        self.finish("implement")
        with mock.patch.object(self.forge, "write_stage", return_value=SimpleNamespace(ok=False, error="offline")):
            self.run_pass()
            self.finish("review")
            self.findings()
            self.assertEqual(self.run_pass(), [])
        self.assertEqual(len(self.launched), 2)

    def test_clock_orders_latest_review_by_instant_and_passes_now_to_planner(self):
        self.db.sync_from_forge(self.forge.snapshots())
        self.db.record_segment(KEY, "review", 1, started_at="2000-01-01T01:00:00+02:00",
                               ended_at="2000-01-01T01:30:00+02:00", outcome="done")
        self.db.record_segment(KEY, "review", 2, started_at="2000-01-01T00:00:00+00:00",
                               ended_at="2000-01-01T00:30:00+00:00", outcome="done")
        self.findings(attempt=2)
        with mock.patch.object(engine.reconcile, "plan", return_value=[]) as planned:
            self.run_pass()
        self.assertEqual(planned.call_args.kwargs["now"], self.now)
        self.assertEqual(planned.call_args.kwargs["open_findings"], {KEY: {}})

    def test_cleanup_failure_is_a_failed_pass_after_actions_run(self):
        self.cleanup.return_value = 1
        with self.assertRaisesRegex(RuntimeError, "cleanup failed"):
            self.run_pass()
        self.assertEqual(len(self.launched), 1)


class MainTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        (self.root / "CLAUDE.md").write_text(
            "# Workspace\n\n## Coordinator\n\n- Worktrees: `trees/`\n\n"
            "- Backlog: https://github.com/example/project/issues\n\n"
            "- Settings: `settings.toml`\n\n")
        (self.root / "settings.toml").write_text(
            '[kanban]\nstages = ["In progress", "In review", "Merging"]\n'
            'human_review_label = "Human review"\n'
            '[kanban.map]\nplan = 0\nimplement = 0\nfix = 0\nreview = 1\nmerge = 2\n'
            '[workers]\nclaude_as_profile = "secondary"\n')

    def test_once_refuses_a_held_workspace_lock_before_opening_the_forge(self):
        errors = io.StringIO()
        with supervisor.lock(supervisor.state_dir(self.root) / "engine.lock"), \
             mock.patch.object(engine.forge, "for_workspace") as opened, contextlib.redirect_stderr(errors):
            self.assertEqual(engine.main(["--root", str(self.root), "--once"]), 2)
        opened.assert_not_called()
        self.assertIn("held", errors.getvalue())

    def test_once_opens_workspace_store_and_runs_one_pass_without_sleep(self):
        settings_path = self.root / "settings.toml"
        settings_path.write_text(settings_path.read_text() + 'teardown = "exit 7"\n')
        fake = FakeForge()
        with mock.patch.object(engine.forge, "for_workspace", return_value=fake), \
             mock.patch.object(engine, "run_pass") as passed, mock.patch.object(engine.time, "sleep") as sleep:
            self.assertEqual(engine.main(["--root", str(self.root), "--once"]), 0)
        self.assertTrue((self.root / "store.db").is_file())
        self.assertEqual(passed.call_count, 1)
        self.assertIs(passed.call_args.kwargs["forge"], fake)
        self.assertEqual(passed.call_args.kwargs["trees"], self.root / "trees")
        self.assertEqual(passed.call_args.kwargs["workers"].claude_as_profile, "secondary")
        self.assertEqual(passed.call_args.kwargs["teardown_command"], "exit 7")
        self.assertEqual(passed.call_args.kwargs["launch"].keywords["teardown_command"], "exit 7")
        sleep.assert_not_called()

    def test_engine_passes_workspace_rules_to_its_worker_launch_port(self):
        path = self.root / "settings.toml"
        path.write_text(path.read_text() + 'rules = ["Keep tests."]\n')
        with mock.patch.object(engine.forge, "for_workspace", return_value=FakeForge()), \
             mock.patch.object(engine, "run_pass") as passed:
            self.assertEqual(engine.main(["--root", str(self.root), "--once"]), 0)
        self.assertEqual(passed.call_args.kwargs["launch"].keywords["rules"], ("Keep tests.",))

    def test_loaded_suite_policy_reaches_collect_and_launch(self):
        with (self.root / "settings.toml").open("a") as config:
            config.write("\nrequire_green_tests = true\n")
        with mock.patch.object(engine.forge, "for_workspace", return_value=FakeForge()), \
             mock.patch.object(engine, "run_pass") as passed:
            self.assertEqual(engine.main(["--root", str(self.root), "--once"]), 0)
        self.assertTrue(passed.call_args.kwargs["require_green"])
        self.assertTrue(passed.call_args.kwargs["launch"].keywords["require_green"])

    def test_rules_that_exceed_a_stage_budget_refuse_before_the_engine_loop(self):
        from dataclasses import replace
        import dispatch_prompt as dp
        workspace_rules = self.root / "CLAUDE.md"
        workspace_rules.write_text(workspace_rules.read_text() +
                                   "\n## Architecture\n- Canonical document: `design/ARCHITECTURE.md`\n")
        path = self.root / "settings.toml"
        path.write_text(path.read_text() + '\nrules = ["Keep tests."]\nrequire_green_tests = true\n'
                        '[stages.implement]\nskills = ["verify"]\n')
        rules = ("p" * 1000,)
        policies = engine.settings.load_policy()
        policies.personas["implementer"] = replace(policies.personas["implementer"], rules=rules)
        row = SimpleNamespace(issue_key="k" * dp.LINE_CAP, title="t" * dp.LINE_CAP)
        prompt = dp.compose_issue(row, "", "", "implement", policies.personas["implementer"],
                                  root=self.root, skills=("verify",), rules=("Keep tests.",), require_green=True)
        budget = len(prompt.split("\n## Forge issue\n")[0])
        # Include implement delivery, persona rules, skills, architecture and the suite gate.
        for cap, expected in ((budget, 0), (budget - 1, 2)):
            errors = io.StringIO()
            with self.subTest(cap=cap), mock.patch.object(dp, "BODY_CAP", cap), \
                 mock.patch.object(engine.settings, "load_policy", return_value=policies), \
                 mock.patch.object(engine.forge, "for_workspace", return_value=FakeForge()), \
                 mock.patch.object(engine, "run_pass") as passed, contextlib.redirect_stderr(errors):
                self.assertEqual(engine.main(["--root", str(self.root), "--once"]), expected)
                if expected:
                    passed.assert_not_called()
                    self.assertIn("[workers] rules", errors.getvalue())
                    self.assertIn("implement", errors.getvalue())
                else:
                    passed.assert_called_once()

    def test_failed_pass_is_logged_and_the_loop_continues_at_the_interval(self):
        errors = io.StringIO()
        with mock.patch.object(engine.forge, "for_workspace", return_value=FakeForge()), \
             mock.patch.object(engine, "run_pass", side_effect=[RuntimeError("offline"), None]) as passed, \
             mock.patch.object(engine.time, "sleep", side_effect=[None, KeyboardInterrupt]) as sleep, \
             contextlib.redirect_stderr(errors):
            self.assertEqual(engine.main(["--root", str(self.root), "--interval", "7"]), 0)
        self.assertEqual(passed.call_count, 2)
        self.assertEqual(sleep.call_args_list, [mock.call(7), mock.call(7)])
        self.assertIn("offline", errors.getvalue())

    def test_once_failed_pass_reports_failure(self):
        with mock.patch.object(engine.forge, "for_workspace", return_value=FakeForge()), \
             mock.patch.object(engine, "run_pass", side_effect=RuntimeError("offline")), \
             contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(engine.main(["--root", str(self.root), "--once"]), 1)

    def test_no_backlog_refuses_without_creating_a_store(self):
        path = self.root / "CLAUDE.md"
        path.write_text(path.read_text().replace("- Backlog: https://github.com/example/project/issues", ""))
        errors = io.StringIO()
        with mock.patch.object(engine, "run_pass") as passed, contextlib.redirect_stderr(errors):
            self.assertEqual(engine.main(["--root", str(self.root), "--once"]), 2)
        passed.assert_not_called()
        self.assertFalse((self.root / "store.db").exists())
        self.assertIn("Backlog:", errors.getvalue())
        self.assertIn(issue_gate.SETUP, errors.getvalue())

    def test_workspace_without_claude_md_offers_setup(self):
        (self.root / "CLAUDE.md").unlink()
        errors = io.StringIO()
        with contextlib.redirect_stderr(errors):
            self.assertEqual(engine.main(["--root", str(self.root), "--once"]), 2)
        self.assertIn(issue_gate.SETUP, errors.getvalue())
        self.assertFalse((self.root / "store.db").exists())

    def test_no_kanban_refuses_without_creating_a_store_or_running_a_pass(self):
        (self.root / "settings.toml").write_text("[workers]\nmax_concurrency = 1\n")
        errors = io.StringIO()
        with mock.patch.object(engine.forge, "for_workspace", return_value=FakeForge()), \
             mock.patch.object(engine, "run_pass") as passed, contextlib.redirect_stderr(errors):
            self.assertEqual(engine.main(["--root", str(self.root), "--once"]), 2)
        passed.assert_not_called()
        self.assertFalse((self.root / "store.db").exists())
        self.assertIn("[kanban]", errors.getvalue())
        self.assertIn("stage labels", errors.getvalue())

    def test_a_workflow_stage_missing_from_the_map_refuses_startup(self):
        (self.root / "settings.toml").write_text(
            '[kanban]\nstages = ["In progress", "Merging"]\nhuman_review_label = "Human review"\n'
            '[kanban.map]\nimplement = 0\nfix = 0\nreview = 0\nmerge = 1\n')
        errors = io.StringIO()
        with mock.patch.object(engine.forge, "for_workspace", return_value=FakeForge()), \
             mock.patch.object(engine, "run_pass") as passed, contextlib.redirect_stderr(errors):
            self.assertEqual(engine.main(["--root", str(self.root), "--once"]), 2)
        passed.assert_not_called()
        self.assertIn("plan", errors.getvalue())

    def test_github_project_starts_now_that_snapshots_read_project_status(self):
        path = self.root / "settings.toml"
        path.write_text(path.read_text() + '\n[kanban.github_project]\nowner = "example"\nnumber = 3\n')
        with mock.patch.object(engine.forge, "for_workspace", return_value=FakeForge()), \
             mock.patch.object(engine, "run_pass") as passed:
            self.assertEqual(engine.main(["--root", str(self.root), "--once"]), 0)
        passed.assert_called_once()
        self.assertTrue((self.root / "store.db").exists())

    def test_default_interval_is_sixty_seconds(self):
        with mock.patch.object(engine.forge, "for_workspace", return_value=FakeForge()), \
             mock.patch.object(engine, "run_pass"), \
             mock.patch.object(engine.time, "sleep", side_effect=KeyboardInterrupt) as sleep:
            self.assertEqual(engine.main(["--root", str(self.root)]), 0)
        sleep.assert_called_once_with(60)

    def test_the_supervisor_starts_the_engine_driver(self):
        self.assertEqual(supervisor.ENGINE, (str(Path(engine.__file__).resolve()),))


if __name__ == "__main__":
    unittest.main()
