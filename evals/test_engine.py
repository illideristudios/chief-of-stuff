"""Engine passes with a fake forge and launch port, and a real issue-keyed store."""

import contextlib
from dataclasses import replace
import io
import json
import subprocess
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import decision_actions
import decision_escalation
import engine
import issue_gate
import model_port
import policy
import reconcile
import slots
import store
import supervisor
from tree_state import TreeState, at_risk
from orphans import Claim
from evals import test_forge as tf
from forge_port import ForgeError
from settings import Kanban

NOW = datetime(2000, 1, 1, tzinfo=timezone.utc)
KEY = "github:example/project#1"
OTHER = "github:example/project#2"
SCOPE = "## File ownership\n- write: `.`"


def review_report(db, key, attempt=1, counts=None):
    """Store a done review's report under its run's session, with `counts` findings by severity (#1032)."""
    data = db.resume(key) or {}
    runs = data.get("runs") or {}
    session = (runs.get(f"review-{attempt}") or {}).get("session_id")
    if not session:
        session = "review-session"
        db.set_resume(key, {**data, "runs": {**runs, f"review-{attempt}": {"session_id": session}}})
    db.add_report(key, "review", attempt, session, "done", "reviewed",
                  [{"severity": severity, "title": "finding", "evidence": "seen"}
                   for severity, n in (counts or {}).items() for _ in range(n)])


def owned_issue(*args, **kwargs):
    return {**tf.gh_issue(*args, **kwargs), "body": SCOPE}


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
        return {"body": SCOPE, **dict(row), "stage_mark": None}

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

    def set_body(self, number, body, commit=False):
        self.bodies = [*getattr(self, "bodies", []), (number, body, commit)]
        next(row for row in self.rows if row["issue_key"].endswith(f"#{number}"))["body"] = body
        return ""

    def closing_request(self, issue_key):
        return SimpleNamespace(number=10, verdict="ready", approval="", head=tf.tma.HEAD)

    def issue(self, number):
        row = next(row for row in self.rows if row["issue_key"].endswith(f"#{number}"))
        return SimpleNamespace(ok=True, issue=SimpleNamespace(is_open=row["forge_state"] == "open", labels=[],
                               body=row.get("body", SCOPE)))

    def merge(self, number, commit=False, issue_key=None, approval=None, head=None, stage_mark=None):
        self.merges.append((number, commit, issue_key))
        next(row for row in self.rows if row["issue_key"] == issue_key)["forge_state"] = "closed"
        return "merged #10"


class EngineTest(unittest.TestCase):
    def test_recovery_read_failure_is_isolated_to_its_issue(self):
        self.forge.rows.append({"issue_key": OTHER, "stage": "implement", "forge_state": "open"})
        def gather(db, key, **kwargs):
            if key == KEY:
                raise OSError("unreadable recovery")
            return None
        with mock.patch.object(engine.engine_facts, "recovery_candidates", side_effect=gather):
            actions = self.run_pass(clone=self.root)
        self.assertEqual([row[0] for row in self.launched], [OTHER])
        self.assertIn("recovery read failed: unreadable recovery", " ".join(row.message for row in self.db.log(KEY)))

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

    def test_pass_delivers_exception_pings_after_store_updates(self):
        observed = []
        def ping(root, now):
            observed.append((root, now, len(self.db.segments(KEY))))
        with mock.patch("notify.sync_workspace", side_effect=ping) as sync:
            self.run_pass()
        sync.assert_called_once_with(self.root, NOW)
        self.assertEqual(observed, [(self.root, NOW, 1)])

    def test_ping_failure_does_not_abort_pass(self):
        with mock.patch("notify.sync_workspace", side_effect=RuntimeError("queue unavailable")) as sync:
            actions = self.run_pass()
        self.assertTrue(actions)
        sync.assert_called_once_with(self.root, NOW)

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
        engine.worker._save_run(self.db, key, stage, attempt, data["runs"][f"{stage}-{attempt}"])
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
        review_report(self.db, KEY, attempt, counts)

    def released_implementation(self, *, request=True, held=False, shared=False):
        pr = {**tf.tma.pr(10, [], draft=True), "headRefName": "work/issue-1"}
        gh = tf.FakeGh(issues=[owned_issue(1, "Released work", ["stage: doing"] + (["needs human"] if held else []))],
                       prs=[pr] if request else [], bodies={10: "Closes #1"})
        board = Kanban(tf.FLOW.stages, "needs human", {"implement": 1, "review": 1 if shared else 2, "merge": 0})
        self.forge = engine.forge.Forge(tf.backlog.GitHubBacklog("example/project"), kanban=board, gh=gh)
        self.db.sync_from_forge(self.forge.snapshots())
        self.db.record_segment(KEY, "implement", 2, (NOW-timedelta(minutes=2)).isoformat(),
                               (NOW-timedelta(minutes=1)).isoformat(), "human_review")
        self.db.set_resume(KEY, {"work_branch": "legacy-other", "runs": {
            "implement-2": {"branch": "work/issue-1", "session_id": "implement-session"}}})
        self.db.add_report(KEY, "implement", 2, "implement-session", "human_review", "Please review", [])
        return gh

    def test_human_release_advances_implement_two_to_review_without_implement_three(self):
        gh = self.released_implementation(held=True)
        self.assertEqual(self.run_pass(), [reconcile.Wait(KEY, "human review")])
        gh.issues[1]["labels"] = [label for label in gh.issues[1]["labels"] if label["name"] != "needs human"]
        self.assertEqual(self.run_pass(), [reconcile.Review(KEY, 1)])
        self.assertEqual(self.launched, [(KEY, "review", 1, "critic")])
        self.assertEqual(self.db.item(KEY).stage, "review")
        self.assertEqual(self.db.segments(KEY)[0].outcome, "human_review")
        self.assertEqual(self.db.reports(KEY)[0].outcome, "human_review")
        self.assertEqual(self.run_pass(), [])
        self.assertEqual(len(self.launched), 1)

    def test_released_implementation_without_request_retries_implement(self):
        self.released_implementation(request=False)
        self.assertEqual(self.run_pass(), [reconcile.Dispatch(KEY, "implement", "builder", 3)])
        self.assertEqual(self.launched, [(KEY, "implement", 3, "builder")])

    def test_historical_release_uses_normal_handoff_on_shared_board_label(self):
        self.released_implementation(shared=True)
        self.assertEqual(self.run_pass(), [reconcile.Review(KEY, 1)])
        self.assertEqual(self.db.item(KEY).stage, "review")
        self.assertEqual(self.run_pass(), [])
        self.assertEqual(self.db.item(KEY).stage, "review")
        self.assertEqual(len(self.launched), 1)

    def test_cached_clear_hold_does_not_accept_when_snapshot_omits_hold(self):
        self.released_implementation()
        snapshot = {key: value for key, value in self.forge.snapshots()[0].items() if key != "human_review"}
        with mock.patch.object(engine.forge.Forge, "snapshots", return_value=[snapshot]):
            self.assertEqual(self.run_pass(), [reconcile.Wait(KEY, "implementation hold release is unobserved")])
        self.assertEqual(self.launched, [])
        self.assertEqual(self.run_pass(), [reconcile.Review(KEY, 1)])

    def test_request_read_failure_waits_and_does_not_hold_an_unaffected_sibling(self):
        self.released_implementation()
        snapshot = {**self.forge.snapshots()[0], "body": "## File ownership\n- write: `src/`"}
        sibling = {"issue_key": OTHER, "stage": "implement", "forge_state": "open", "human_review": False,
                   "body": "## File ownership\n- write: `docs/`", "stage_mark": None}
        with mock.patch.object(engine.forge.Forge, "snapshots", return_value=[snapshot, sibling]), \
             mock.patch.object(engine.forge.Forge, "requests", side_effect=ForgeError("partial request read")):
            self.assertEqual(self.run_pass(), [reconcile.Wait(KEY, "implementation request evidence is unknown"),
                                               reconcile.Dispatch(OTHER, "implement", "builder", 1)])
        self.assertEqual(self.launched, [(OTHER, "implement", 1, "builder")])

    def test_release_handoff_retries_failed_stage_write_without_duplicate_work(self):
        self.released_implementation()
        with mock.patch.object(engine.forge.Forge, "write_stage", return_value=SimpleNamespace(ok=False, error="offline")):
            self.assertEqual(self.run_pass(), [reconcile.Review(KEY, 1)])
            self.assertEqual(self.db.resume(KEY)["pending_stage"], {"stage": "review", "expected": "implement"})
            self.assertEqual(self.run_pass(), [])
        self.assertEqual(self.run_pass(), [])
        self.assertEqual(self.db.item(KEY).stage, "review")
        self.assertEqual(self.launched, [(KEY, "review", 1, "critic")])

    def test_release_handoff_launch_failure_recovers_in_review(self):
        self.released_implementation()
        with mock.patch.object(self, "launch", side_effect=ForgeError("request closed before launch")):
            self.assertEqual(self.run_pass(), [reconcile.Review(KEY, 1)])
        self.assertEqual(self.db.item(KEY).stage, "review")
        self.assertEqual(self.db.resume(KEY)["launcher_hold"], {"stage": "review", "attempt": 1})
        self.assertEqual(self.run_pass(), [reconcile.Review(KEY, 2)])
        self.assertEqual(self.launched, [(KEY, "review", 2, "critic")])

    def test_unpublished_suite_hold_is_not_accepted_by_request_evidence(self):
        self.released_implementation()
        data = self.db.resume(KEY)
        data["suite_hold"] = {"stage": "implement", "attempt": 2, "published": False}
        self.db.set_resume(KEY, data)
        self.assertEqual(self.run_pass(), [reconcile.Wait(KEY, "human review")])
        self.assertEqual(self.launched, [])
        self.assertEqual(self.db.segments(KEY)[0].outcome, "human_review")

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
        # Both issues explicitly claim `.`; KEY's ended review is no longer running, so its retained claim cannot block OTHER.
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
                    self.assertEqual([(r.path, r.mode) for r in self.db.ownership(KEY)], [(".", "write")])
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
        def landings(pipeline, keys, *, errors=None, heads=None):
            if errors is not None:
                errors[KEY] = "compare: permission denied"
            return {KEY: (None, None)}
        self.forge.landings = landings
        self.run_pass()
        self.run_pass()
        waits = [row.message for row in self.db.log(KEY) if row.message.startswith("wait: ")]
        self.assertEqual(len(waits), 1)
        self.assertIn("compare: permission denied", waits[0])

    def test_terminal_failed_exit_preserves_recoverable_work_without_a_hold(self):
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
                self.assertFalse(close.call_args.args[7])

    def assert_unconfirmed_merge_keeps_review_stage(self, outcome, message, *, merge_pending=False):
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
                if merge_pending:
                    self.assertEqual(self.db.resume(KEY)["merge_pending"],
                                     {"expected": "review", "request": 10})
                else:
                    self.assertNotIn("merge_pending", self.db.resume(KEY))
                self.assertEqual(self.db.log(KEY)[-1].message, message)
                self.assertEqual(merge.call_args_list, [mock.call(10, commit=True, issue_key=KEY,
                                                                 approval="", head=tf.tma.HEAD,
                                                                 stage_mark=None)] * attempt)

    def test_queued_merge_keeps_stage_and_is_planned_again_next_pass(self):
        self.assert_unconfirmed_merge_keeps_review_stage("queued #10", "queued #10", merge_pending=True)

    def test_merge_queue_reaches_terminal_stage_when_forge_confirms_merge(self):
        self.run_pass()
        self.finish("implement")
        self.run_pass()
        self.finish("review")
        self.findings()
        with mock.patch.object(self.forge, "merge", return_value="queued #10"):
            self.assertEqual(self.run_pass(), [reconcile.Merge(KEY)])
        self.assertEqual(self.db.resume(KEY)["merge_pending"], {"expected": "review", "request": 10})
        self.forge.rows[0].update(forge_state="closed", merge_state="merged")
        self.run_pass()
        self.assertEqual(self.db.items()[0].stage, "merge")
        self.assertNotIn("pending_stage", self.db.resume(KEY))
        self.assertNotIn("merge_pending", self.db.resume(KEY))
        self.assertEqual(self.forge.writes[-1], (1, "merge", "review", True))

    def test_merge_queue_gitlab_reaches_terminal_stage_when_forge_confirms_merge(self):
        self.run_pass()
        self.finish("implement")
        self.run_pass()
        self.finish("review")
        self.findings()
        with mock.patch.object(self.forge, "merge", return_value="queued !10"):
            self.assertEqual(self.run_pass(), [reconcile.Merge(KEY)])
        self.assertEqual(self.db.resume(KEY)["merge_pending"], {"expected": "review", "request": 10})
        self.forge.rows[0].update(forge_state="closed", merge_state="merged")
        self.run_pass()
        self.assertEqual(self.db.items()[0].stage, "merge")
        self.assertNotIn("pending_stage", self.db.resume(KEY))
        self.assertNotIn("merge_pending", self.db.resume(KEY))
        self.assertEqual(self.forge.writes[-1], (1, "merge", "review", True))

    def test_unknown_merge_keeps_stage_and_is_planned_again_next_pass(self):
        self.assert_unconfirmed_merge_keeps_review_stage("unknown #10", "unknown #10")

    def test_refused_verification_keeps_stage_and_is_planned_again_next_pass(self):
        self.assert_unconfirmed_merge_keeps_review_stage(ForgeError("head changed"), "merge refused: head changed")

    def test_two_items_touching_the_same_files_both_dispatch(self):
        # #1039 (C29): overlapping edits surface as merge conflicts at review, never as an ownership wait.
        self.db.upsert_item(KEY, stage="implement", forge_state="open")
        self.db.upsert_item(OTHER, stage="implement", forge_state="open")
        self.forge.rows.append({"issue_key": OTHER, "stage": "implement", "forge_state": "open"})
        self.forge.rows[0]["body"] = "## File ownership\n- write: `src/a.py`"
        self.forge.rows[1]["body"] = "## File ownership\n- write: `src/a.py`"
        self.db.replace_file_ownership(OTHER, [("src/a.py", "write")])
        self.db.record_segment(OTHER, "implement", 1, started_at=NOW.isoformat())
        run_dir = engine.worker._run_dir(self.root, OTHER, "implement", 1)
        self.db.set_resume(OTHER, {"runs": {"implement-1": {
            "pid": 123, "session_id": "fake-session", "identity": "fake-start",
            "run_dir": str(run_dir), "tree": str(self.trees / "fake")}}})
        self.assertEqual(self.run_pass(), [reconcile.Dispatch(KEY, "implement", "builder", 1)])
        self.assertEqual([row[0] for row in self.launched], [KEY])

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

    def test_undeclared_issues_dispatch_without_owning_files(self):
        # #1039 (C29): an issue with no File ownership section is never held for one.
        self.forge.rows[0]["body"] = ""
        self.forge.rows.append({"issue_key": OTHER, "stage": "implement", "forge_state": "open",
                                "body": "## File ownership\n- typo: `src/`"})
        self.assertEqual(self.run_pass(), [reconcile.Dispatch(KEY, "implement", "builder", 1),
                                          reconcile.Dispatch(OTHER, "implement", "builder", 1)])
        for key in (KEY, OTHER):
            self.assertEqual([r for r in self.db.ownership(key) if r.mode != "tree"], [])
        self.assertFalse(any("file ownership" in row.message for key in (KEY, OTHER) for row in self.db.log(key)))

    def test_removing_scope_after_implementation_does_not_hold_the_review_launch(self):
        self.run_pass()
        self.finish("implement")
        self.forge.rows[0]["body"] = ""
        self.assertEqual(self.run_pass(), [reconcile.Review(KEY, 1)])

    def test_scope_edits_wait_for_the_open_segment_and_keep_worktree_registrations(self):
        self.forge.rows[0]["body"] = "## File ownership\n- write: `src/a.py`"
        self.run_pass()
        tree = str(self.trees / "fake")
        self.db.set_ownership(KEY, tree, "tree")
        for body in ("## File ownership\n- read-only: `docs/`", "scope removed"):
            self.forge.rows[0]["body"] = body
            self.run_pass()
            self.assertEqual({(r.path, r.mode) for r in self.db.ownership(KEY)}, {(tree, "tree"), ("src/a.py", "write")})
        self.finish("implement")
        self.assertEqual(self.run_pass(), [reconcile.Review(KEY, 1)])
        self.assertEqual({(r.path, r.mode) for r in self.db.ownership(KEY)}, {(tree, "tree")})

    def test_unreadable_declaration_cannot_erase_read_only_completion_evidence(self):
        self.forge.rows[0]["body"] = "## File ownership\n- read-only: `.`"
        self.run_pass()
        self.forge.rows[0]["body"] = ""
        self.run_pass()
        self.assertEqual([(r.path, r.mode) for r in self.db.ownership(KEY)], [(".", "read")])
        self.forge.rows[0]["body"] = "## File ownership\n- typo: src/"
        self.publish_exit()
        self.forge.branch_head = lambda ref: SimpleNamespace(sha="main-sha", status="passed")
        with mock.patch.object(engine.engine_facts, "_tree_facts", return_value=(TreeState("work", 0, 0, 0), True, NOW)), \
             mock.patch.object(engine.engine_facts.git_trees, "git", return_value=(0, "current-head")):
            self.run_pass()
        self.assertIsNotNone(self.db.segments(KEY)[0].ended_at)
        self.assertTrue(self.db.items()[0].human_review)
        self.assertTrue(any("read-only run left changes" in row.message for row in self.db.log(KEY)))
        self.assertEqual(self.db.ownership(KEY), [])
        self.assertEqual(len(self.launched), 1)

    def test_root_layout_dispatches_overlapping_declared_scopes_together(self):
        self.trees = self.root
        self.forge.rows[0]["body"] = "## File ownership\n- write: `src/`"
        self.forge.rows.append({"issue_key": OTHER, "stage": "implement", "forge_state": "open",
                                "body": "## File ownership\n- write: `src/a.py`"})
        self.assertEqual(self.run_pass(), [reconcile.Dispatch(KEY, "implement", "builder", 1),
                                          reconcile.Dispatch(OTHER, "implement", "builder", 1)])

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
            self.db.replace_file_ownership(key, [(".", "write")])
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

    def squash_landed_final_tree(self):
        from evals import git_taint
        self.enterContext(git_taint.private_git_views(self.root / "git-views"))
        self.p.workflows["standard"] = policy.Workflow("standard", "implement")
        self.run_pass()
        self.publish_exit()
        tree = self.trees / "fake"
        tree.mkdir()

        def git(*args):
            return git_taint.git(list(args), tree).stdout.strip()

        git("init", "-b", "main")
        (tree / "file").write_text("base")
        git("add", ".")
        git("commit", "-m", "base")
        git("checkout", "-b", "work")
        for content in ("first", "second"):
            (tree / "file").write_text(content)
            git("commit", "-am", content)
        head = git("rev-parse", "HEAD")
        git("checkout", "main")
        git("merge", "--squash", "work")
        git("commit", "-m", "landed")
        squash = git("rev-parse", "HEAD")
        git("update-ref", "refs/remotes/origin/main", squash)
        git("checkout", "--detach", head)
        git("branch", "-D", "work")
        self.forge.rows[0].update(forge_state="closed", merge_state="merged")
        self.forge.branch_head = lambda ref: tf.forge.Pipeline(1, "main", squash, "passed", "")
        merged = {"number": 21, "headRefOid": head, "body": "Closes #1",
                  "baseRefName": "main", "mergeCommit": {"oid": squash}}
        self.forge.landings = tf.forge.Forge(tf.backlog.GitHubBacklog("example/project"),
                                           gh=tf.FakeGh(merged=[merged])).landings
        return tree, git

    def test_squash_landed_final_stage_clean_tree_completes_without_a_hold(self):
        tree, git = self.squash_landed_final_tree()
        self.assertEqual(at_risk(engine.engine_facts.git_trees.read_state(tree)),
                         ("2 not on origin/main", "no upstream"))
        self.assertEqual(git("branch", "--list", "work"), "")
        self.assertEqual(self.run_pass(), [])
        self.assertEqual(self.db.segments(KEY)[0].outcome, "done")
        self.assertFalse(self.db.items()[0].human_review)
        self.assertEqual(self.db.resume(KEY)["verified_landing_head"], git("rev-parse", "HEAD"))
        self.assertIn(KEY, self.cleanup.call_args.kwargs["completed"])
        self.assertEqual(self.run_pass(), [])  # remembered landing still certifies this head
        (tree / "extra").write_text("work after landing")
        git("add", ".")
        git("commit", "-m", "extra work")
        facts = engine.engine_facts.gather(forge=self.forge, store=self.db, root=self.root,
                                          trees=self.trees, now=self.now)
        verdict = reconcile.completion_verdict(KEY, self.db.segments(KEY)[0], facts,
                                               self.p.workflows["standard"], report="done", worker_alive=False)
        self.assertTrue(verdict.human_review)

    def test_real_squash_landing_still_holds_a_dirty_tree(self):
        tree, _ = self.squash_landed_final_tree()
        (tree / "extra").write_text("uncommitted work")
        self.run_pass()
        self.assertEqual(self.db.segments(KEY)[0].outcome, "done")
        self.assertTrue(self.db.items()[0].human_review)

    def test_real_squash_landing_still_holds_commits_outside_the_landed_change(self):
        tree, git = self.squash_landed_final_tree()
        (tree / "extra").write_text("unlanded work")
        git("add", ".")
        git("commit", "-m", "extra work")
        git("update-ref", "refs/remotes/origin/extra", git("rev-parse", "HEAD"))
        self.assertEqual(engine.engine_facts._tree_facts(tree)[0].unpushed, 0)
        self.run_pass()
        self.assertEqual(self.db.segments(KEY)[0].outcome, "done")
        self.assertTrue(self.db.items()[0].human_review)

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

    def test_a_failed_adapter_exit_with_unlanded_changes_retries_without_a_hold(self):
        self.run_pass()
        run = Path(self.db.resume(KEY)["runs"]["implement-1"]["run_dir"])
        run.mkdir(parents=True)
        (run / "stdout").write_text(json.dumps({"is_error": True, "result": "failed"}))
        (run / "stderr").write_text("")
        (run / "exit.json").write_text('{"exit_code": 1}')
        with mock.patch.object(engine.engine_facts, "_tree_facts", return_value=(TreeState("work", 1, 2, None), False, NOW)), \
             mock.patch.object(engine.engine_facts.git_trees, "discover", return_value=[self.trees / "fake"]):
            self.run_pass()
        self.assertFalse(self.db.items()[0].human_review)
        self.assertEqual(len(self.launched), 2)

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

    def test_a_new_issue_with_an_unreadable_body_dispatches_without_inventing_scope(self):
        self.forge.rows[0]["body"] = None
        with mock.patch.object(self.forge, "issue", side_effect=ForgeError("offline")):
            self.assertEqual(self.run_pass(), [reconcile.Dispatch(KEY, "implement", "builder", 1)])
        self.assertEqual([r for r in self.db.ownership(KEY) if r.mode != "tree"], [])
        self.assertEqual(len(self.launched), 1)
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
                self.db.set_resume(KEY, {**(self.db.resume(KEY) or {}), "session": "keep"})
                before = len(self.db.log(KEY))
                with mock.patch.object(engine.reconcile, "plan", return_value=[action]):
                    self.run_pass()
                    self.run_pass()
                self.assertEqual(len(self.db.log(KEY)), before + 1)
                self.assertEqual(self.db.resume(KEY)["session"], "keep")
                field = "decision_interrupts" if isinstance(action, reconcile.Ask) else "interrupt"
                self.assertIn(field, self.db.resume(KEY))

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
        self.assertEqual(self.run_pass(), [reconcile.Wait(KEY, "review findings are unknown")])
        session = self.db.resume(KEY)["runs"]["review-1"]["session_id"]
        # A foreign session, another attempt, then this session's report with a severity outside SEVERITIES.
        for session_id, attempt, found in (("foreign", 1, []), (session, 2, []),
                                           (session, 1, [{"severity": "nit", "title": "t", "evidence": "e"}])):
            with self.subTest(session_id=session_id, attempt=attempt, found=found):
                self.db.add_report(KEY, "review", attempt, session_id, "done", "reviewed", found)
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
                engine._write_stage(forge, mock.MagicMock(), KEY, "implement", prior)
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
        gh = tf.FakeGh(issues=[owned_issue(1, "Held review", ["stage: doing"])],
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
        gh = tf.FakeGh(issues=[owned_issue(1, "Retry", ["stage: doing", "bug"])])
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
        gh = tf.FakeGh(issues=[owned_issue(1, "Retry", ["stage: doing", "needs human"])],
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
        gh = tf.FakeGh(issues=[owned_issue(1, "Held retry", ["stage: doing", "needs human"])])
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
        gh = tf.FakeGh(issues=[owned_issue(1, "Retry", ["stage: doing", "needs human"])])
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
        unknown = self.db.segments(KEY)[-1]
        self.assertEqual((unknown.runtime, unknown.model, unknown.effort), ("", "", ""))
        self.assertEqual(self.db.resume(KEY)["launcher_hold"], {"stage": "implement", "attempt": 2})
        self.assertEqual(self.run_pass(), [reconcile.Escalate(KEY, "retry limit reached for implement")])

    def assert_cursor_start_failure(self, *, review=False, verification=False, restore_error=False):
        worker = engine.worker
        stage = "review" if review else "implement"
        self.pending_release(held=False)
        self.db.record_segment(KEY, "implement", 1, NOW.isoformat(), NOW.isoformat(), "failed",
                               runtime="claude", model="opus", effort="high")
        if review:
            self.forge.rows[0]["stage"] = "review"
            self.db.set_resume(KEY, {})
        (self.root / "CLAUDE.md").write_text(
            "## Coordinator\n- User: Robin\n- Settings: `cos.toml`\n- Agent: worker-type task\n")
        (self.root / "cos.toml").write_text(
            '[lanes]\nbuild = { stages = ["implement", "review"] }\n'
            f'[models.{"review" if review else "implement"}]\nrotation = ["cursor:composer-2.5"]\n'
            f'[stages.{stage}]\nagent = "worker-type"\n')
        tree = self.trees / "worker"
        tree.mkdir()
        (tree / ".cursor").mkdir()
        config = tree / ".cursor" / "mcp.json"
        config.write_text('{"mcpServers":{"custom":{}}}')
        self.forge.default_branch_protected = lambda: True
        self.forge.protected_branches = lambda: ("main",)
        self.forge.request_base = lambda n: "main"
        self.forge.closing_request = lambda key: SimpleNamespace(number=10, branch="feature/work", head="head")
        self.forge.issue = lambda n: SimpleNamespace(ok=True, issue=SimpleNamespace(
            title="Build a thing", body=SCOPE))

        def launch(key, stage, attempt, persona):
            return worker.launch(key, stage, attempt, persona, store=self.db, root=self.root,
                                 trees=self.trees, policies=self.p, forge=self.forge, clock=lambda: NOW)

        def unverified(runtime, request, run_dir, **kwargs):
            (run_dir / "started.json").write_text(json.dumps({"pid": 77}))
            return SimpleNamespace(pid=78, poll=lambda: 1), ""

        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch("notify.sync_workspace"))
            stack.enter_context(mock.patch.object(worker.make_worktree, "build", return_value=SimpleNamespace(path=tree)))
            stack.enter_context(mock.patch.object(worker.make_worktree, "git", return_value=(0, "head")))
            stack.enter_context(mock.patch.object(worker.host_adapter, "resolve", return_value="/fake/agent"))
            stack.enter_context(mock.patch.object(worker.host_adapter, "branch_heads", return_value={"main": "head"}))
            cancel = stack.enter_context(mock.patch.object(worker.claude_adapter, "cancel"))
            stop = stack.enter_context(mock.patch.object(worker.process_status, "stop_session"))
            cleanup = stack.enter_context(mock.patch.object(worker.host_adapter, "cleanup"))
            teardown = stack.enter_context(mock.patch.object(worker, "teardown", return_value=()))
            if verification:
                stack.enter_context(mock.patch.object(worker.host_adapter, "start", side_effect=unverified))
                stack.enter_context(mock.patch.object(worker.process_status, "identity", return_value=None))
                stack.enter_context(mock.patch.object(worker.process_status, "carriers", return_value=()))
            if restore_error:
                stack.enter_context(mock.patch.object(self.forge, "add_human_hold", return_value="forge offline"))
            stack.enter_context(mock.patch.object(self, "launch", side_effect=launch))
            self.run_pass()
        if verification:
            cancel.assert_called_once()
            stop.assert_called_once()
            cleanup.assert_called_once()
            teardown.assert_called_once()
        else:
            cancel.assert_not_called()
            self.assertEqual(config.read_text(), '{"mcpServers":{"custom":{}}}')
        attempt = 1 if review else 2
        failed = [s for s in self.db.segments(KEY) if s.stage == stage and s.attempt == attempt]
        self.assertEqual(len(failed), 1, [e.message for e in self.db.log(KEY)])
        seg = failed[0]
        self.assertEqual((seg.runtime, seg.model, seg.effort, seg.agent_type),
                         ("cursor", "composer-2.5", "", "worker-type"))
        self.assertEqual((seg.started_at, seg.ended_at, seg.outcome), (NOW.isoformat(), NOW.isoformat(), "failed"))
        self.assertEqual(slots.running(self.db), 0)
        self.assertTrue(self.db.item(KEY).human_review)
        if restore_error:
            self.assertEqual(self.db.resume(KEY)["pending_hold"], {"stage": stage, "attempt": attempt, "retry": True})
        else:
            self.assertEqual(self.db.resume(KEY)["launcher_hold"], {"stage": stage, "attempt": attempt})
        messages = " ".join(entry.message for entry in self.db.log(KEY))
        self.assertIn("identity mismatched" if verification else "refusing to replace it", messages)
        if restore_error:
            self.assertIn("forge offline", messages)
        # Consumers receive the engine's persisted row, including the earlier host's separate group.
        from performance import table
        from zoneinfo import ZoneInfo
        rows = table(self.db.segments(KEY), since=NOW.date(), until=NOW.date(), by="day", zone=ZoneInfo("UTC"))
        cursor = next(r for r in rows if r.runtime == "cursor")
        self.assertEqual((cursor.model, cursor.effort, cursor.runs, cursor.completed, cursor.median_minutes),
                         ("composer-2.5", "", 1, 0, 0))
        self.assertEqual(next(r for r in rows if r.runtime == "claude").runs, 1)

    def test_cursor_config_refusal_retains_identity_after_previous_claude_attempt(self):
        self.assert_cursor_start_failure()

    def test_cursor_verification_failure_retains_identity_and_cleanup(self):
        self.assert_cursor_start_failure(verification=True)

    def test_review_cursor_failure_retains_identity(self):
        self.assert_cursor_start_failure(review=True)

    def test_cursor_identity_survives_a_second_failure_restoring_the_hold(self):
        self.assert_cursor_start_failure(restore_error=True)

    def test_launch_failure_creates_the_owned_hold_used_by_the_next_pass(self):
        gh = tf.FakeGh(issues=[owned_issue(1, "Retry", ["stage: doing", "bug"])])
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
        gh = tf.FakeGh(issues=[owned_issue(1, "Handoff", ["stage: doing", "bug"])])
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
        gh = tf.FakeGh(issues=[owned_issue(1, "Held merge", ["stage: doing"])],
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
        gh = tf.FakeGh(issues=[owned_issue(1, "Shared stages", ["stage: doing"])])
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
        gh = tf.FakeGh(issues=[owned_issue(1, "Held merge", ["stage: doing"])],
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
        gh = tf.FakeGh(issues=[owned_issue(1, "Moved by hand", ["stage: review"])])
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

    def test_undeclared_agent_type_runs_default_agent(self):
        # Section 9: undeclared persona agents use the default and record an empty agent type.
        self.p = policy.parse({
            "default_workflow": "standard",
            "personas": {"builder": {"role": "implements", "model": "example", "agent_type": "nonexistent-agent"},
                         "critic": {"role": "reviews", "model": "example"}},
            "stages": {"implement": {"persona": "builder"}, "review": {"persona": "critic"}, "merge": {}},
            "workflows": {"standard": {"start": "implement", "edges": [
                {"from": "implement", "to": "review"}, {"from": "review", "to": "merge"},
            ]}},
        })
        self.assertEqual(self.run_pass(), [reconcile.Dispatch(KEY, "implement", "builder", 1)])
        self.assertEqual(self.launched, [(KEY, "implement", 1, "builder")])
        self.assertEqual(self.db.segments(KEY)[0].agent_type, "")

    def decide(self, topic, question, answer=None, by="user", **record):
        """Append a decision row, as trio_gate.record does for every writer (rows are append-only)."""
        self.db.sync_from_forge(self.forge.snapshots())
        return self.db.add_decision(KEY, topic, question, answer, by, record)

    def state(self, row):
        """A decision's current state: its identity's latest row."""
        return decision_actions.state_of(decision_escalation.latest(self.db.decisions(KEY))[decision_escalation.identity(row)])

    def test_a_wait_or_an_escalation_leaves_a_persons_answer_answered(self):
        # §6, #1124: only the decision's own intent failing for good blocks it, never the planner waiting.
        for question, action in (("Approve schema?", reconcile.Wait(KEY, "merge conflicts")),
                                 ("Ship it?", reconcile.Escalate(KEY, "retry limit reached"))):
            with self.subTest(action=action):
                dec = self.decide("design", question, "Yes")
                with mock.patch.object(engine.reconcile, "plan", return_value=[action]):
                    self.run_pass()
                    self.run_pass()
                self.assertEqual((self.state(dec).status, self.state(dec).blocked_reason), ("answered", ""))
                self.assertEqual(self.db.decisions(KEY)[-1].id, dec.id)  # nothing appended

    def test_a_closed_issues_wait_blocked_answers_settle_to_answered_as_new_rows(self):
        # #1124: closed issues get no planned action, so the settling pass reads every issue's decisions.
        self.forge.rows[0].update(forge_state="closed", merge_state="merged")
        blocked = [self.decide("design", q, "Yes", status="blocked", blocked_reason=reason)
                   for q, reason in (("Approve schema?", "merged, awaiting main CI"), ("Ship it?", "human review"))]
        self.run_pass()
        rows = self.db.decisions(KEY)
        self.assertTrue({b.id for b in blocked} <= {r.id for r in rows})  # the blocked rows are kept: append-only
        for b in blocked:
            self.assertEqual((self.state(b).status, self.state(b).blocked_reason), ("answered", ""))
            self.assertGreater(self.state(b).id, b.id)
        self.run_pass()
        self.assertEqual(len(self.db.decisions(KEY)), len(rows))  # settled once

    def test_default_agent_has_no_declared_type(self):
        self.run_pass()
        self.assertEqual(self.db.segments(KEY)[0].agent_type, "")

    def add_intent(self, stage, phase, *, status="re-routed", reason="", question="Back?"):
        """A board intent as the page server leaves it: a decision row carrying its target stage and phase."""
        record = {"status": status, "rerouted_to": stage, "intent": phase,
                  **({"blocked_reason": reason} if reason else {}), **({"reversed": True} if status == "reversed" else {})}
        return self.decide("design", question, None if status == "reversed" else "Back", **record)

    def settled_reroute(self, stage="review"):
        return self.add_intent(stage, "", question="Settled?")  # its own decision, so later intents leave it alone

    def test_a_pending_run_intent_runs_even_after_a_completed_stage_and_ends(self):
        self.db.sync_from_forge(self.forge.snapshots())
        self.db.record_segment(KEY, "implement", 1, (NOW - timedelta(hours=2)).isoformat(),
                               (NOW - timedelta(hours=1)).isoformat(), "done")
        dec = self.add_intent("implement", "run", status="reversed")
        self.run_pass()
        self.assertEqual(self.launched, [(KEY, "implement", 2, "builder")])
        self.assertEqual((self.state(dec).status, self.state(dec).intent), ("reversed", ""))
        # The reversal's done phase is develop's restarted row.
        self.assertEqual(self.state(dec).record["restarted"], "implement")

    def test_a_write_intent_is_written_by_the_engine_then_runs_and_ends(self):
        dec = self.add_intent("review", "write")
        self.run_pass()
        self.assertEqual(self.forge.writes, [(1, "review", "implement", True)])
        self.assertEqual(self.db.item(KEY).stage, "review")
        saved = self.state(dec)
        self.assertEqual((saved.status, saved.blocked_reason, saved.rerouted_to, saved.intent), ("re-routed", "", "review", ""))
        self.assertEqual(self.launched, [(KEY, "review", 1, "critic")])

    def test_a_failed_write_stays_pending_blocks_nothing_and_is_retried(self):
        dec = self.add_intent("review", "write")
        for failure in (SimpleNamespace(ok=False, done=False, error="offline"), RuntimeError("offline")):
            with self.subTest(failure=failure):
                effect = {"side_effect": failure} if isinstance(failure, Exception) else {"return_value": failure}
                with mock.patch.object(self.forge, "write_stage", **effect):
                    actions = self.run_pass()
                saved = self.state(dec)
                self.assertEqual((saved.status, saved.blocked_reason, saved.intent), ("re-routed", "", "write"))
                self.assertEqual(actions, [reconcile.Wait(KEY, "re-route pending")])
                self.assertEqual(self.db.item(KEY).stage, "implement")
        self.assertEqual([e.kind for e in self.db.log(KEY) if "failed" in e.message], [reconcile.OP_STAGE_WRITE])
        self.run_pass()
        self.assertEqual((self.db.item(KEY).stage, self.state(dec).intent), ("review", ""))

    def test_only_the_newest_intent_is_written_and_the_older_one_is_superseded(self):
        old = self.add_intent("review", "write", question="A?")
        newest = self.add_intent("implement", "write", question="B?")
        self.forge.rows[0]["stage"] = "review"
        self.run_pass()
        self.assertEqual(self.forge.writes, [(1, "implement", "review", True)])
        self.assertEqual((self.state(old).status, self.state(old).superseded_by, self.state(old).intent),
                         ("superseded", newest.id, ""))
        self.assertEqual(self.state(newest).status, "re-routed")

    def test_a_reversal_supersedes_a_blocked_intent_and_runs(self):
        blocked = self.add_intent("review", "run", status="blocked", reason="retry limit reached", question="A?")
        reversal = self.add_intent("implement", "run", status="reversed", question="B?")
        self.run_pass()
        self.assertEqual(self.state(blocked).status, "superseded")
        self.assertEqual(self.launched, [(KEY, "implement", 1, "builder")])
        self.assertEqual(self.state(reversal).intent, "")

    def test_a_new_intent_replaces_the_engines_own_pending_move(self):
        self.add_intent("review", "write")
        self.db.set_resume(KEY, {"pending_stage": {"stage": "merge", "expected": "implement"}})
        self.run_pass()
        self.assertEqual([w[1] for w in self.forge.writes], ["review"])
        self.assertNotIn("pending_stage", self.db.resume(KEY))

    def test_waiting_on_an_intent_marks_no_sibling_decision_blocked(self):
        sibling = self.decide("scope", "Ship?", "Yes")
        for phase, status in (("write", "re-routed"), ("run", "blocked")):
            with self.subTest(phase=phase):
                held = self.add_intent("review", phase, status=status, reason="retry limit reached" if status == "blocked" else "")
                with mock.patch.object(self.forge, "write_stage", return_value=SimpleNamespace(ok=False, error="offline")):
                    self.assertEqual(self.run_pass(), [reconcile.Wait(KEY, "re-route pending")])
                    self.assertEqual(self.run_pass(), [reconcile.Wait(KEY, "re-route pending")])
                self.assertEqual((self.state(sibling).status, self.state(sibling).blocked_reason), ("answered", ""))
                self.decide("design", held.question, "Back", status="superseded")

    def test_the_engines_own_stage_move_blocks_no_settled_reroute(self):
        old = self.settled_reroute("review")
        self.run_pass()
        self.finish("implement")
        with mock.patch.object(self.forge, "write_stage", return_value=SimpleNamespace(ok=False, error="offline")):
            self.run_pass()
            self.run_pass()  # the retry of the move is what refuses it again
        self.assertEqual(self.db.resume(KEY)["pending_stage"]["stage"], "review")
        self.assertEqual(self.state(old).status, "re-routed")

    def test_an_answered_decision_a_wait_blocked_is_unblocked_when_the_wait_is_the_decision_hold(self):
        dec = self.decide("design", "Approve schema?", "Yes", status="blocked", blocked_reason="merge is not ready")
        with mock.patch.object(engine.reconcile, "plan", return_value=[reconcile.Wait(KEY, "decision pending")]):
            self.run_pass()
        self.assertEqual((self.state(dec).status, self.state(dec).blocked_reason), ("answered", ""))

    def test_a_decision_hold_wait_blocks_no_answered_decision(self):
        answered = self.decide("design", "Approve schema?", "Yes")
        self.decide("scope", "Still open?", by="zach")
        with mock.patch.object(engine.reconcile, "plan", return_value=[reconcile.Wait(KEY, "decision pending")]):
            self.run_pass()
        self.assertEqual(self.state(answered).status, "answered")

    def test_a_wait_blocked_answer_settles_even_beside_a_pending_sibling(self):
        # #1124: a wait never blocked it for good, so an open sibling is no reason to keep it blocked.
        blocked = self.decide("design", "Approve schema?", "Yes", status="blocked", blocked_reason="merge conflicts")
        self.decide("scope", "Still open?", by="zach")
        with mock.patch.object(engine.reconcile, "plan", return_value=[reconcile.Wait(KEY, "merge conflicts")]):
            self.run_pass()
        self.assertEqual((self.state(blocked).status, self.state(blocked).blocked_reason), ("answered", ""))

    def spent_intent(self, status="reversed"):
        dec = self.add_intent("implement", "run", status=status)
        with mock.patch.object(engine.reconcile, "plan", return_value=[reconcile.Escalate(KEY, "retry limit reached")]):
            self.run_pass()
        return dec

    def test_a_spent_retry_limit_blocks_only_the_intents_own_decision_and_keeps_the_hold(self):
        other = self.settled_reroute("review")
        for status in ("reversed", "re-routed"):
            with self.subTest(status=status):
                dec = self.spent_intent(status)
                saved = self.state(dec)
                self.assertEqual((saved.status, saved.blocked_reason, saved.intent), ("blocked", "retry limit reached", "run"))
                self.assertFalse(decision_actions.is_closed(saved))
                self.assertEqual(self.state(other).status, "re-routed")
                for _ in range(2):
                    self.assertEqual(self.run_pass(), [reconcile.Wait(KEY, "re-route pending")])
                self.assertEqual(self.launched, [])
                self.decide("design", dec.question, "Back", status="superseded")

    def test_a_spent_reversal_waits_after_earlier_completed_work(self):
        self.db.sync_from_forge(self.forge.snapshots())
        self.db.record_segment(KEY, "implement", 1, NOW.isoformat(), NOW.isoformat(), "done")
        dec = self.spent_intent()
        for _ in range(2):
            self.assertIsInstance(self.run_pass()[0], reconcile.Wait)
        self.assertEqual(self.launched, [])
        self.assertEqual(self.state(dec).status, "blocked")

    def test_a_blocked_intent_is_left_alone_by_later_actions_and_decision_holds(self):
        dec = self.spent_intent()
        for action in (reconcile.Wait(KEY, "merge is not ready"), reconcile.Wait(KEY, "decision pending"), reconcile.Merge(KEY)):
            with mock.patch.object(engine.reconcile, "plan", return_value=[action]):
                with mock.patch.object(engine.engine_merge, "execute", return_value=False):
                    self.run_pass()
            saved = self.state(dec)
            self.assertEqual((saved.status, saved.blocked_reason, saved.intent), ("blocked", "retry limit reached", "run"))

    def test_a_failed_stage_write_after_a_dispatch_is_logged_as_a_stage_write(self):
        self.run_pass()
        self.finish("implement")
        with mock.patch.object(self.forge, "write_stage", return_value=SimpleNamespace(ok=False, error="label offline")):
            self.run_pass()
        self.assertEqual([e.kind for e in self.db.log(KEY) if "failed" in e.message], [reconcile.OP_STAGE_WRITE])

    def test_a_failed_merge_hold_write_is_logged_as_a_stage_write_not_a_refused_merge(self):
        self.db.sync_from_forge(self.forge.snapshots())
        self.forge.kanban = SimpleNamespace(holds=lambda stage: stage == "merge", stage_map={}, hold_stages=("merge",))
        with mock.patch.object(engine.reconcile, "plan", return_value=[reconcile.Merge(KEY)]):
            with mock.patch.object(self.forge, "write_stage", return_value=SimpleNamespace(ok=False, error="label offline")):
                self.run_pass()
        self.assertEqual([e.kind for e in self.db.log(KEY) if "failed" in e.message], [reconcile.OP_STAGE_WRITE])

    def test_a_failed_ask_is_not_recorded_as_a_worker_run(self):
        self.db.sync_from_forge(self.forge.snapshots())
        with mock.patch.object(engine.reconcile, "plan", return_value=[reconcile.Ask(KEY, "gate", "Split?")]):
            with mock.patch.object(engine, "_choice", side_effect=RuntimeError("store busy")):
                self.run_pass()
        self.assertEqual([e.kind for e in self.db.log(KEY) if "failed" in e.message], [""])

    def test_comments_board_rows_carry_are_posted_once(self):
        self.decide("design", "Approve schema?", "Yes", status="answered", comment="first")
        self.decide("design", "Approve schema?", "Yes", status="blocked", blocked_reason="wait")  # carries no comment
        posted = []
        self.forge.comment = lambda number, body, commit=False: posted.append(body) or ""
        with mock.patch.object(engine.reconcile, "plan", return_value=[]):
            self.run_pass()
            self.run_pass()
        self.assertEqual(posted, ["first"])

    def test_a_comment_that_fails_is_retried_and_logged_once(self):
        self.decide("design", "Approve schema?", "Yes", status="answered", comment="first")
        self.forge.comment = mock.Mock(return_value="offline")
        with mock.patch.object(engine.reconcile, "plan", return_value=[]):
            self.run_pass()
            self.run_pass()
        self.assertEqual(self.forge.comment.call_count, 2)
        self.assertEqual([e.message for e in self.db.log(KEY)], ["decision comment failed: offline"])
        self.assertNotIn("decision_comments_sent", self.db.resume(KEY) or {})

    def test_the_launch_ends_only_the_intent_it_carried(self):
        carried = self.add_intent("implement", "run", status="reversed", question="A?")
        launch = self.launch

        def board_post_during_launch(key, stage, attempt, persona):
            result = launch(key, stage, attempt, persona)
            self.newer = self.add_intent("review", "write", question="B?")  # appended after planning
            return result

        self.launch = board_post_during_launch
        self.run_pass()
        # The newer intent is current, so the launch does not end the carried one or clear the newer: the next pass supersedes it.
        self.assertEqual((self.state(carried).status, self.state(carried).intent), ("reversed", "run"))
        self.assertEqual((self.state(self.newer).status, self.state(self.newer).intent), ("re-routed", "write"))
        self.run_pass()
        self.assertEqual((self.state(carried).status, self.state(carried).superseded_by), ("superseded", self.newer.id))

    def test_a_worker_is_not_launched_for_an_intent_a_newer_one_has_replaced_since_planning(self):
        old = self.add_intent("implement", "run", status="reversed", question="A?")
        real_plan = engine.reconcile.plan

        def plan(*args, **kwargs):
            actions = real_plan(*args, **kwargs)
            self.add_intent("review", "write", question="B?")  # appended while planning
            return actions

        with mock.patch.object(engine.reconcile, "plan", side_effect=plan):
            self.run_pass()
        self.assertEqual(self.launched, [])
        self.assertEqual(self.state(old).intent, "run")

    def test_a_retry_limit_blocks_only_the_current_intent_not_a_newer_one_that_replaced_it(self):
        old = self.add_intent("implement", "run", question="A?")

        def plan(*args, **kwargs):
            self.newer = self.add_intent("review", "write", question="B?")
            return [reconcile.Escalate(KEY, "retry limit reached")]

        with mock.patch.object(engine.reconcile, "plan", side_effect=plan):
            self.run_pass()
        # The newer intent is current, so the spent limit blocks nothing: the older one is superseded.
        self.assertEqual(self.state(old).status, "re-routed")
        self.assertEqual((self.state(self.newer).status, self.state(self.newer).intent), ("re-routed", "write"))
        self.run_pass()
        self.assertEqual(self.state(old).status, "superseded")

    def test_a_human_answer_during_a_trio_consult_wins_and_leaves_the_persons_row_latest(self):
        # Architect ruling on #758, item 2: run against #750's merged consult path (engine._choice -> trio_gate).
        self._trio()
        self.p.stages["implement"] = policy.Place("implement", "builder", {"zach_gates": ("plan",)})
        self.db.upsert_item(KEY, title="Parser", stage="implement")
        self.db.record_segment(KEY, "implement", 1, NOW.isoformat(), NOW.isoformat(), "done")
        consults = []

        def person_answers(request):
            if not consults:  # the person answers through the board while the first vote is in flight
                self.db.add_decision(KEY, "plan", "gate requires Zach's approval", "hold", "user",
                                     {"decision": {"context": "implement-1", "options": ["approve", "hold"]}})
            consults.append(request)
            return model_port.Reply("", {"vote": "adopt", "choice": "approve", "reasoning": "because"}, "", 0.1, 1)

        self.run_pass(model=person_answers)
        rows = self.db.decisions(KEY)
        self.assertEqual([(r.answer, r.decided_by) for r in rows], [("hold", "user")])  # the trio's approval never landed
        self.assertNotIn("waived_gates", self.db.resume(KEY) or {})
        self.assertFalse([e for e in self.db.log(KEY) if "failed" in e.message])

    def test_the_segment_records_the_agent_the_launcher_uses_from_the_stage_defaults(self):
        (self.root / "CLAUDE.md").write_text(
            "## Coordinator\n- Settings: `chief-of-stuff.toml`\n- Agent: specialist task\n")
        (self.root / "chief-of-stuff.toml").write_text(
            '[lanes.code]\nstages = ["implement"]\n[stages.implement]\nagent = "specialist"\n')
        self.run_pass()
        self.assertEqual(self.db.segments(KEY)[0].agent_type, "specialist")

    def plan_policy(self):
        self.p = policy.parse({
            "default_workflow": "standard",
            "personas": {"builder": {"role": "implements", "model": "build"}, "critic": {"role": "reviews", "model": "review"}},
            "stages": {"plan": {"persona": "builder"}, "implement": {"persona": "builder"},
                       "review": {"persona": "critic"}, "merge": {}},
            "workflows": {"standard": {"start": "plan", "edges": [
                {"from": "plan", "to": "implement"}, {"from": "implement", "to": "review"},
                {"from": "review", "to": "implement"}, {"from": "review", "to": "merge"}]}},
        })

    def test_two_undeclared_plan_stage_issues_dispatch_together(self):
        # #915 (§8): undeclared issues no longer serialize at the plan stage.
        self.plan_policy()
        other = KEY.rpartition("#")[0] + "#2"
        self.forge.rows = [{"issue_key": key, "stage": "plan", "forge_state": "open", "merge_state": "", "body": ""}
                           for key in (KEY, other)]
        self.forge.snapshot = lambda issue_key, **_: {**next(r for r in self.forge.rows if r["issue_key"] == issue_key),
                                                      "stage_mark": None}
        self.run_pass()
        self.assertEqual(sorted((key, stage) for key, stage, *_ in self.launched), [(KEY, "plan"), (other, "plan")])

    def planned_without_scope(self):
        # An issue whose plan ran before #915/#820 asked plans to propose their file ownership.
        self.plan_policy()
        self.forge.rows[0].update(stage="plan", body="")
        self.forge.snapshot = lambda issue_key, **_: {**self.forge.rows[0], "stage_mark": None}
        self.db.sync_from_forge(self.forge.snapshots())
        self.db.record_segment(KEY, "plan", 1, started_at=NOW.isoformat(), ended_at=NOW.isoformat(), outcome="done")

    def test_a_planned_issue_with_no_file_ownership_dispatches_implement(self):
        # #1039 (C29): a plan result without file_ownership goes on to implement; it is never re-planned for scope.
        self.planned_without_scope()
        self.run_pass()
        self.assertEqual([(key, stage, attempt) for key, stage, attempt, *_ in self.launched], [(KEY, "implement", 1)])

    def test_a_stored_scope_proposal_raises_no_scope_decision_and_writes_nothing(self):
        # #1039 (C29): resume data written before the removal stays readable and is never consulted on.
        self._trio()
        self.forge.rows[0]["body"] = "Fix the widget."
        self.db.sync_from_forge(self.forge.snapshots())
        proposal = {"stage": "plan", "attempt": 1, "scopes": [["src/a.py", "write"]]}
        self.db.update_resume(KEY, lambda data: data.__setitem__("scope_proposal", proposal))
        with mock.patch.object(self.forge, "snapshot", lambda issue_key, **_: {**self.forge.rows[0], "stage_mark": None}):
            self.run_pass(model=self._votes([("adopt", "approve")] * 3))
        self.assertFalse(getattr(self.forge, "bodies", []))
        self.assertEqual([r.topic for r in self.db.decisions(KEY) if r.topic == "scope"], [])
        self.assertEqual(self.db.resume(KEY)["scope_proposal"], proposal)

    def test_trio_consults_stop_starting_once_the_pass_budget_is_spent(self):
        # #927: consults run inside the pass, so a slow trio must not hold the pass (and worker collection) for long.
        self._trio()
        other = KEY.rpartition("#")[0] + "#2"
        self.forge.rows.append({**self.forge.rows[0], "issue_key": other})
        for key in (KEY, other):
            self.db.upsert_item(key, title="Parser", stage="implement")
            self.db.add_ask(key, "implement", 1, "session", "Which parser?", ["Split the parser", "Keep the parser"], "")
        votes = self._votes([("adopt", "Split the parser")] * 6)

        def slow(request):
            self.now += timedelta(seconds=engine.TRIO_PASS_BUDGET_S)  # each vote uses up the whole budget
            return votes(request)
        with mock.patch.object(self.forge, "snapshot", lambda issue_key, **_: {
                **next(r for r in self.forge.rows if r["issue_key"] == issue_key), "stage_mark": None}):
            self.run_pass(model=slow)
        self.assertEqual(votes.n, 1)  # the first vote spends the budget: no further call, and the next consult waits
        consulted = [key for key in (KEY, other) if self.db.decisions(key)]
        self.assertEqual(len(consulted), 1)
        self.assertTrue(decision_escalation.retryable_failure(self.db.decisions(consulted[0])[-1].record))

    def _trio(self, **fields):
        self.p = policy.parse({
            "default_workflow": "standard",
            "policy": {"trio": ["builder", "critic", "operator"], **fields},
            "personas": {"builder": {"role": "implements", "model": "build"},
                         "critic": {"role": "reviews", "model": "review"},
                         "operator": {"role": "operates", "model": "ops"}},
            "stages": {"implement": {"persona": "builder"}, "review": {"persona": "critic"}, "merge": {}},
            "workflows": {"standard": {"start": "implement", "edges": [
                {"from": "implement", "to": "review", "gate": "plan"},
                {"from": "review", "to": "implement"}, {"from": "review", "to": "merge"},
            ]}},
        })

    def _votes(self, votes):
        pending = list(votes)

        def call(request):
            vote, choice = pending.pop(0)
            call.n += 1
            return model_port.Reply("", {"vote": vote, "choice": choice, "reasoning": "because"}, "", 0.1, 1)

        call.n = 0
        return call

    def test_a_unanimous_trio_adopts_an_ask_with_no_ping(self):
        self._trio()
        self.db.upsert_item(KEY, title="Parser", stage="implement")
        self.db.add_ask(KEY, "implement", 1, "session", "Which parser?", ["Split the parser", "Keep the parser"], "")
        model = self._votes([("adopt", "Split the parser")] * 3)
        self.run_pass(model=model)
        self.run_pass(model=model)
        self.assertEqual(self.db.asks(KEY)[0].answer, "Split the parser")
        row = self.db.decisions(KEY)[0]
        self.assertEqual((row.answer, row.decided_by, row.record["unanimous"]), ("Split the parser", "trio", True))
        self.assertEqual([v["vote"] for v in row.record["verdicts"]], ["adopt", "adopt", "adopt"])
        self.assertNotIn("decision_interrupts", self.db.resume(KEY))
        self.assertAlmostEqual(self.db.resume(KEY)["spend_usd"], 0.3)
        self.assertEqual(model.n, 3)

    def permission_ask(self, command):
        self.db.upsert_item(KEY, title="Parser", stage="implement")
        context = json.dumps({"tool_name": "Bash", "tool_input": {"command": command}, "why": "an older, narrower rule"})
        return self.db.add_ask(KEY, "implement", 1, "session", "Permission requested for Bash", ["allow", "deny"], context)

    def test_an_open_permission_ask_raised_under_an_older_rule_is_answered_by_todays(self):
        # #890 (3): appended answers close the asks, so the held issue becomes ready; no row is deleted.
        self.permission_ask("git add PLAN-7.md")
        model = mock.Mock()
        self.run_pass(model=model)
        model.assert_not_called()
        self.assertEqual(self.db.asks(KEY)[0].answer, "allow")
        self.assertNotIn("decision_interrupts", self.db.resume(KEY) or {})
        self.assertTrue(any(e.message.startswith("permission allow: Permission requested for Bash") for e in self.db.log(KEY)))

    def test_a_permission_ask_raised_before_874_with_no_options_is_settled_too(self):
        # #890 follow-up: 60 of the 62 open permission asks were raised before the allow/deny form and carry none.
        self.db.upsert_item(KEY, title="Parser", stage="implement")
        context = json.dumps({"tool_name": "Bash", "tool_input": {"command": "git ls-files"}})
        self.db.add_ask(KEY, "plan", 1, "session", "Permission requested for Bash", [], context)
        self.db.add_ask(KEY, "plan", 1, "session", "Permission requested for Bash", [],
                        json.dumps({"tool_name": "Bash", "tool_input": {"command": "cat ../sibling/notes.md"}}))
        self._trio()
        self.run_pass(model=self._votes([("adopt", "allow")] * 3))
        self.assertEqual([a.answer for a in self.db.asks(KEY)], ["allow", "allow"])  # the rule, then the trio's choice
        self.assertNotIn("decision_interrupts", self.db.resume(KEY) or {})

    def test_a_permission_marker_is_settled_by_the_rule_without_the_question_prefix(self):
        # #893: kind marks the ask, so recognition does not depend on the question text.
        self.db.upsert_item(KEY, title="Parser", stage="implement")
        context = json.dumps({"kind": "permission", "tool_name": "Bash",
                              "tool_input": {"command": "git ls-files"}, "cwd": str(self.root)})
        self.db.add_ask(KEY, "implement", 1, "session", "May this run?", ["allow", "deny"], context)
        model = mock.Mock()
        self.run_pass(model=model)
        model.assert_not_called()
        self.assertEqual(self.db.asks(KEY)[0].answer, "allow")
        self.assertTrue(any("the guard's rule" in e.message for e in self.db.log(KEY)))

    def test_a_commit_whose_hooks_are_inside_the_worktree_is_not_settled_by_the_rule(self):
        # #893: re-judging the stored hooks path keeps an in-tree commit from being auto-allowed on the next pass.
        self.db.upsert_item(KEY, title="Parser", stage="implement")
        context = json.dumps({"kind": "permission", "tool_name": "Bash",
                              "tool_input": {"command": "git commit -m x"}, "cwd": str(self.root),
                              "hooks_path": ".husky", "hooks_root": str(self.root)})
        self.db.add_ask(KEY, "implement", 1, "session", "May this run?", ["allow", "deny"], context)
        self.run_pass()
        self.assertEqual(self.db.asks(KEY)[0].answer, "deny")
        messages = [e.message for e in self.db.log(KEY)]
        # No model: the trio is not consulted, so the deny is the trio's, not the guard's rule.
        self.assertTrue(any("the trio did not adopt an answer" in message for message in messages))
        self.assertFalse(any("the guard's rule" in message for message in messages))

    def test_a_commit_with_unset_hooks_is_still_allowed_by_the_rule(self):
        # #893: a null hooks path, and an ask stored before hooks were recorded, stay the #890 allow.
        self.db.upsert_item(KEY, title="Parser", stage="implement")
        marked = {"kind": "permission", "tool_name": "Bash", "tool_input": {"command": "git commit -m x"},
                  "cwd": str(self.root)}
        self.db.add_ask(KEY, "implement", 1, "session", "May this run?", ["allow", "deny"],
                        json.dumps({**marked, "hooks_path": None}))
        self.db.add_ask(KEY, "implement", 1, "session", "Permission requested for Bash", ["allow", "deny"],
                        json.dumps({"tool_name": "Bash", "tool_input": {"command": "git commit -m x"},
                                    "cwd": str(self.root)}))
        model = mock.Mock()
        self.run_pass(model=model)
        model.assert_not_called()
        self.assertEqual([a.answer for a in self.db.asks(KEY)], ["allow", "allow"])

    def test_a_split_trio_denies_a_permission_and_never_asks_the_owner(self):
        # #890 (2): unjudged goes to the trio only; a split denies with the reason on the run.
        self._trio()
        self.permission_ask("cat ../sibling/notes.md")
        self.run_pass(model=self._votes([("adopt", "allow"), ("concern", "deny"), ("adopt", "allow")]))
        self.assertEqual(self.db.asks(KEY)[0].answer, "deny")
        latest = self.db.decisions(KEY)[-1]
        self.assertEqual((latest.answer, latest.decided_by), ("deny", "guard"))
        self.assertEqual(decision_escalation.owner_waits(self.db.decisions(KEY), self.db.segments(KEY), self.db.asks(KEY)), [])
        self.assertNotIn("decision_interrupts", self.db.resume(KEY) or {})
        self.assertTrue(any("the trio did not adopt an answer" in e.message for e in self.db.log(KEY)))

    def test_a_trio_with_nothing_launchable_or_every_entry_exhausted_denies_a_permission(self):
        # Architect on #891: for a permission ask, what goes to the owner elsewhere is a deny here.
        rotation = (engine.settings.ModelEntry("codex", "m1"), engine.settings.ModelEntry("cursor", "m2"))
        quota = lambda request: model_port.Failure("quota", "429 quota")  # noqa: E731
        for name, port in (("nothing launchable", model_port.Port(quota, rotation, launchable=lambda runtime: False)),
                           ("every entry exhausted", model_port.Port(quota, rotation))):
            with self.subTest(name):
                self._trio()
                self.db.engine.dispose()
                self.db = store.Store(self.root / "store.db", clock=lambda: self.now)
                self.addCleanup(self.db.engine.dispose)
                ask_id = self.permission_ask(f"cat ../sibling/{name.replace(' ', '-')}.md")
                self.run_pass(model=port)
                answer = next(a.answer for a in self.db.asks(KEY) if a.id == ask_id)
                self.assertEqual(answer, "deny")
                self.assertEqual(decision_escalation.owner_waits(self.db.decisions(KEY), self.db.segments(KEY), self.db.asks(KEY)), [])
                self.assertNotIn("decision_interrupts", self.db.resume(KEY) or {})

    def test_an_unavailable_trio_denies_a_permission(self):
        self.permission_ask("cat ../sibling/notes.md")
        self.run_pass()  # no model: the trio cannot be consulted
        self.assertEqual(self.db.asks(KEY)[0].answer, "deny")
        self.assertNotIn("decision_interrupts", self.db.resume(KEY) or {})

    def test_a_split_trio_escalates_once(self):
        self._trio()
        self.db.upsert_item(KEY, title="Parser", stage="implement")
        self.db.add_ask(KEY, "implement", 1, "session", "Which parser?", ["Split the parser", "Keep the parser"], "")
        model = self._votes([("adopt", "Split the parser"), ("concern", "Keep the parser"), ("adopt", "Split the parser")])
        self.run_pass(model=model)
        self.run_pass(model=model)
        self.assertIsNone(self.db.asks(KEY)[0].answer)
        self.assertEqual(self.db.resume(KEY)["decision_interrupts"][0]["kind"], "ambiguity")
        self.assertEqual(sum(entry.message.startswith("ambiguity:") for entry in self.db.log(KEY)), 1)
        self.assertEqual(model.n, 3)

    def test_a_unanimous_gate_approval_is_applied_on_the_next_pass(self):
        self._trio()
        self.p.stages["implement"] = policy.Place("implement", "builder", {"zach_gates": ("plan",)})
        self.db.upsert_item(KEY, title="Parser", stage="implement")
        self.db.record_segment(KEY, "implement", 1, NOW.isoformat(), NOW.isoformat(), "done")
        model = self._votes([("adopt", "approve")] * 6)
        self.assertEqual(self.run_pass(model=model), [reconcile.Ask(KEY, "plan", "gate requires Zach's approval")])
        self.assertNotIn("decision_interrupts", self.db.resume(KEY))
        self.assertEqual(self.db.resume(KEY)["waived_gates"], ["plan"])
        self.assertEqual(self.db.decisions(KEY)[0].answer, "approve")
        self.run_pass(model=model)
        self.assertEqual(self.launched[-1][1], "review")
        self.assertEqual(model.n, 3)
        self.assertNotIn("waived_gates", self.db.resume(KEY))
        # A second crossing of the same gate after rework must ask and consult the trio again.
        self.db.record_segment(KEY, "review", 1, (NOW + timedelta(minutes=1)).isoformat(),
                               (NOW + timedelta(minutes=2)).isoformat(), "done")
        self.db.record_segment(KEY, "implement", 2, (NOW + timedelta(minutes=3)).isoformat(),
                               (NOW + timedelta(minutes=4)).isoformat(), "done")
        self.forge.rows = [{"issue_key": KEY, "title": "Parser", "stage": "implement", "forge_state": "open",
                            "review_state": "", "merge_state": "", "human_review": False}]
        self.assertEqual(self.run_pass(model=model), [reconcile.Ask(KEY, "plan", "gate requires Zach's approval")])
        self.assertEqual(model.n, 6)

    def test_a_unanimous_gate_hold_interrupts_once_and_waits(self):
        self._trio()
        self.p.stages["implement"] = policy.Place("implement", "builder", {"zach_gates": ("plan",)})
        self.db.upsert_item(KEY, title="Parser", stage="implement")
        self.db.record_segment(KEY, "implement", 1, NOW.isoformat(), NOW.isoformat(), "done")
        model = self._votes([("adopt", "hold")] * 3)
        self.assertEqual(self.run_pass(model=model), [reconcile.Ask(KEY, "plan", "gate requires Zach's approval")])
        self.assertEqual(self.db.resume(KEY)["decision_interrupts"][0]["kind"], "ambiguity")
        self.assertEqual(self.run_pass(model=model), [reconcile.Wait(KEY, "decision pending")])
        self.assertEqual(sum(entry.message.startswith("ambiguity:") for entry in self.db.log(KEY)), 1)
        self.assertEqual(sum(entry.message == "wait: decision pending" for entry in self.db.log(KEY)), 1)
        self.assertEqual(model.n, 3)

    def test_a_reversed_decision_restarts_once_and_reconsults(self):
        self._trio()
        self.forge.rows = [{"issue_key": KEY, "title": "Parser", "stage": "review", "forge_state": "open",
                            "review_state": "approved", "merge_state": "ready", "human_review": False}]
        self.db.upsert_item(KEY, title="Parser", stage="review")
        self.db.record_segment(KEY, "implement", 1, (NOW - timedelta(hours=2)).isoformat(),
                               (NOW - timedelta(hours=1)).isoformat(), "done")
        self.db.record_segment(KEY, "review", 1, (NOW - timedelta(minutes=30)).isoformat(), NOW.isoformat(), "done")
        self.db.add_decision(KEY, topic="ask", question="Which parser?", answer="Split the parser",
                             decided_by="trio", record={"decision": {"options": ["Split the parser", "Keep the parser"],
                                                                    "context": "parser constraints"}})
        rev = decision_escalation.reverse(self.db.decisions(KEY), "ask", "Which parser?")
        self.db.add_decision(KEY, topic=rev.topic, question=rev.question,
                             answer=rev.answer, decided_by=rev.decided_by, record=rev.record)
        model = self._votes([("adopt", "Keep the parser")] * 3)
        self.assertEqual(self.run_pass(model=model), [reconcile.Dispatch(KEY, "implement", "builder", 2)])
        self.assertEqual(self.db.decisions(KEY)[-1].record.get("restarted"), "implement")
        self.assertEqual(model.n, 0)
        self.run_pass(model=model)
        self.assertEqual(model.n, 0)
        self.finish("implement", 2)
        self.assertEqual(self.run_pass(model=model), [reconcile.Review(KEY, 2)])
        self.assertEqual(model.n, 3)
        self.assertEqual(self.db.decisions(KEY)[-1].answer, "Keep the parser")
        self.finish("review", 2)
        self.findings(2)
        self.assertEqual(self.run_pass(model=model), [reconcile.Merge(KEY)])
        self.assertEqual([stage for _, stage, _, _ in self.launched], ["implement", "review"])

    def test_planner_limit_tracks_review_destination_until_review_can_launch(self):
        self._trio()
        self.p.stages["review"] = policy.Place("review", "critic", {"spend_usd": 1})
        self.db.upsert_item(KEY, stage="implement")
        self.db.record_segment(KEY, "implement", 1, NOW.isoformat(), NOW.isoformat(), "done")
        self.db.set_resume(KEY, {"spend_usd": 1})
        self.run_pass()
        interrupt = self.db.resume(KEY)["interrupt"]
        self.assertEqual(interrupt["stage"], "review")
        self.db.add_ask(KEY, "implement", 1, "s1", "Implementation choice?", ["Yes", "No"], "")
        self.run_pass(model=self._votes([("adopt", "Yes")] * 3))
        self.assertEqual(self.db.resume(KEY)["interrupt"], interrupt)
        self.p.stages["review"] = policy.Place("review", "critic")
        self.assertEqual(self.run_pass(), [reconcile.Review(KEY, 1)])
        self.assertNotIn("interrupt", self.db.resume(KEY))

    def test_planner_retry_exhaustion_tracks_destination_stage(self):
        self._merge_gate()
        self.p.stages["implement"] = policy.Place("implement", "builder", {"retry_limit": 0})
        self.forge.rows[0]["stage"] = "review"
        self.db.record_segment(KEY, "implement", 1, (NOW - timedelta(hours=1)).isoformat(),
                               (NOW - timedelta(minutes=40)).isoformat(), "done")
        self.findings(counts={"important": 1})
        self.assertIsInstance(self.run_pass()[0], reconcile.Escalate)
        self.assertEqual(self.db.resume(KEY)["interrupt"]["stage"], "implement")

    def test_slow_consultations_start_backoff_when_the_failure_is_recorded(self):
        self._trio()
        self.db.upsert_item(KEY, stage="implement")
        self.db.add_ask(KEY, "implement", 1, "s1", "Which parser?", ["Split", "Keep"], "")
        def fail(request):
            self.now += timedelta(seconds=60)
            return model_port.Failure("unknown", "timeout")
        failed = mock.Mock(side_effect=fail)
        for delay in (60, 120):
            self.run_pass(model=failed)
            completed = self.now
            calls = failed.call_count
            retry, = self.db.resume(KEY)["trio_retries"].values()
            self.assertEqual(datetime.fromisoformat(retry["retry_at"]), completed + timedelta(seconds=delay))
            self.now += timedelta(seconds=delay - 1)
            self.run_pass(model=failed)
            self.assertEqual(failed.call_count, calls)
            self.now += timedelta(seconds=1)

    def test_quota_reset_clears_a_limit_stamped_with_the_planned_stage(self):
        self._trio(quota_percent=80)
        self.db.upsert_item(KEY, stage="implement")
        self.db.record_segment(KEY, "implement", 1, NOW.isoformat(), NOW.isoformat(), "done")
        self.db.set_resume(KEY, {"quota_percent": 100,
                                 "quota": {"retry_at": (NOW + timedelta(hours=1)).isoformat()}})
        self.assertEqual(self.run_pass(max_concurrency=0), [reconcile.Limit(KEY, "quota", "reached quota limit")])
        self.assertEqual(self.db.resume(KEY)["interrupt"]["stage"], "review")
        self.now += timedelta(hours=1)
        self.run_pass(max_concurrency=0)
        self.assertNotIn("interrupt", self.db.resume(KEY))

    def test_retryable_trio_failures_escalate_at_a_durable_cap_and_accept_an_answer(self):
        self._trio()
        self.db.upsert_item(KEY, stage="implement")
        self.db.add_ask(KEY, "implement", 1, "s1", "Which parser?", ["Split", "Keep"], "")
        failed = mock.Mock(return_value=model_port.Failure("unknown", "outage"))
        for _ in range(8):
            self.run_pass(model=failed)
            self.now += timedelta(hours=2)
        self.assertEqual(failed.call_count, 24)
        interrupt = self.db.resume(KEY)["decision_interrupts"][0]
        self.assertEqual(interrupt, {"kind": "ambiguity",
            "reason": "trio unavailable after 8 attempts: Which parser?",
            "decision": {"id": ["ask", "Which parser?", "ask-1"], "stage": "implement", "attempt": 1}})
        self.db.engine.dispose()
        self.db = store.Store(self.root / "store.db", clock=lambda: self.now)
        self.addCleanup(self.db.engine.dispose)
        for _ in range(3):
            self.run_pass(model=failed)
            self.now += timedelta(hours=2)
        self.assertEqual(failed.call_count, 24)
        self.assertEqual(self.db.resume(KEY)["decision_interrupts"][0], interrupt)
        self.db.add_ask(KEY, "implement", 1, "s1", "Which backend?", ["Local", "Remote"], "")
        model = self._votes([("adopt", "Local")] * 3)
        self.run_pass(model=model)
        self.assertEqual([a.answer for a in self.db.asks(KEY)], [None, "Local"])
        self.assertEqual(model.n, 3)
        self.db.answer_ask(KEY, self.db.asks(KEY)[0].id, "Split")
        self.run_pass(model=model)
        self.assertEqual(self.db.decisions(KEY)[-1].answer, "Split")
        self.assertEqual(self.db.decisions(KEY)[-1].decided_by, "user")
        self.assertEqual(failed.call_count, 24)
        self.assertEqual(model.n, 3)
        self.assertNotIn("decision_interrupts", self.db.resume(KEY))

    def test_retryable_gate_failures_stop_at_a_cap_without_repeated_interrupts(self):
        self._trio()
        self.p.stages["implement"] = policy.Place("implement", "builder", {"zach_gates": ("plan",)})
        self.db.upsert_item(KEY, stage="implement")
        self.db.record_segment(KEY, "implement", 1, NOW.isoformat(), NOW.isoformat(), "done")
        failed = mock.Mock(return_value=model_port.Failure("unknown", "outage"))
        for _ in range(11):
            self.run_pass(model=failed)
            self.now += timedelta(hours=2)
        self.assertEqual(failed.call_count, 24)
        self.assertEqual(sum(e.message.startswith("ambiguity:") for e in self.db.log(KEY)), 1)
        self.assertEqual(self.db.resume(KEY)["decision_interrupts"][0], {"kind": "ambiguity",
            "reason": "trio unavailable after 8 attempts: gate requires Zach's approval",
            "decision": {"id": ["plan", "gate requires Zach's approval", ""], "stage": "implement", "attempt": 1}})
        self.assertEqual(self.run_pass(model=failed), [reconcile.Wait(KEY, "decision pending")])
        self.assertEqual(failed.call_count, 24)
        self.assertNotIn("waived_gates", self.db.resume(KEY))
        # A later crossing gets a fresh retry budget.
        self.db.record_segment(KEY, "implement", 2, self.now.isoformat(), self.now.isoformat(), "done")
        model = self._votes([("adopt", "approve")] * 3)
        self.run_pass(model=model)
        self.assertEqual(model.n, 3)
        self.assertEqual(self.db.resume(KEY)["waived_gates"], ["plan"])
        self.assertNotIn("decision_interrupts", self.db.resume(KEY))

    def test_a_superseded_capped_gate_does_not_hide_a_later_capped_ask(self):
        self._trio()
        self.p.stages["implement"] = policy.Place("implement", "builder", {"zach_gates": ("plan",)})
        self.db.upsert_item(KEY, stage="implement")
        self.db.record_segment(KEY, "implement", 1, NOW.isoformat(), NOW.isoformat(), "done")
        failed = mock.Mock(return_value=model_port.Failure("unknown", "outage"))
        for _ in range(8):
            self.run_pass(model=failed)
            self.now += timedelta(hours=2)
        stale = self.db.resume(KEY)["decision_interrupts"][0]
        self.assertEqual(stale["decision"]["attempt"], 1)
        self.db.record_segment(KEY, "implement", 2, self.now.isoformat(), self.now.isoformat(), "done")
        ask_id = self.db.add_ask(KEY, "implement", 2, "s2", "Which parser?", ["Split", "Keep"], "ctx")
        for _ in range(8):
            self.run_pass(model=failed)
            self.now += timedelta(hours=2)
        self.assertEqual(failed.call_count, 48)
        interrupt = self.db.resume(KEY)["decision_interrupts"][0]
        self.assertEqual(interrupt["decision"], {"id": ["ask", "Which parser?", f"ask-{ask_id}"],
                                                "stage": "implement", "attempt": 2})
        self.assertEqual(interrupt["reason"], "trio unavailable after 8 attempts: Which parser?")
        # The capped gate remains unanswered while the ask blocks its next consultation.
        gate = [r for r in self.db.decisions(KEY) if r.topic == "plan"][-1]
        self.assertEqual((gate.answer, gate.decided_by, gate.record["decision"]["context"]),
                         (None, "zach", "implement-1"))
        self.assertEqual(gate.record["retry_exhausted"], 8)
        self.db.answer_ask(KEY, ask_id, "Keep")
        model = self._votes([("adopt", "approve")] * 3)
        self.run_pass(model=model)
        self.assertEqual(model.n, 3)
        self.assertEqual({r.record["decision"]["context"] for r in self.db.decisions(KEY)},
                         {"implement-1", "implement-2", "ctx"})
        self.assertNotIn("decision_interrupts", self.db.resume(KEY))

    def test_decision_interrupts_recover_from_committed_rows_and_preserve_other_interrupts(self):
        self._trio()
        self.p.stages["implement"] = policy.Place("implement", "builder", {"zach_gates": ("plan",)})
        self.db.upsert_item(KEY, stage="implement")
        self.db.record_segment(KEY, "implement", 1, NOW.isoformat(), NOW.isoformat(), "done")
        other = {"kind": "ambiguity", "reason": "missing decision payload", "stage": "review"}
        for evidence in ({}, {"recommendation": "hold"}, {"retry_exhausted": 8}):
            with self.subTest(evidence=evidence):
                self.db.add_decision(KEY, "plan", "gate requires Zach's approval", None, "zach", {
                    **evidence, "decision": {"stage": "implement", "attempt": 1, "context": "implement-1",
                                             "options": ["approve", "hold"]}})
                self.db.set_resume(KEY, {"interrupt": other})
                model = mock.Mock()
                self.assertEqual(self.run_pass(model=model), [reconcile.Wait(KEY, "decision pending")])
                expected = self.db.resume(KEY)
                self.assertEqual(expected["interrupt"], other)
                self.assertEqual(len(expected["decision_interrupts"]), 1)
                with mock.patch.object(self.db, "set_resume", wraps=self.db.set_resume) as writes:
                    self.run_pass()
                writes.assert_not_called()
                self.assertEqual(self.db.resume(KEY), expected)
                model.assert_not_called()
        self.db.add_decision(KEY, "plan", "gate requires Zach's approval", "approve", "user", {
            "decision": {"stage": "implement", "attempt": 1, "context": "implement-1"}})
        self.run_pass(max_concurrency=0)
        self.assertNotIn("decision_interrupts", self.db.resume(KEY))
        self.assertEqual(self.db.resume(KEY)["interrupt"], other)

    def test_split_and_hold_survive_a_crash_immediately_after_the_decision_write(self):
        self._trio()
        self.p.stages["implement"] = policy.Place("implement", "builder", {"zach_gates": ("plan",)})
        self.db.upsert_item(KEY, stage="implement")
        write = self.db.add_decision
        def crash(*args, **kwargs):
            write(*args, **kwargs)
            raise KeyboardInterrupt("after committed decision")
        for attempt, vote in enumerate(("concern", "adopt"), 1):
            with self.subTest(vote=vote):
                self.now += timedelta(hours=1)
                self.db.record_segment(KEY, "implement", attempt, self.now.isoformat(), self.now.isoformat(), "done")
                self.db.set_resume(KEY, {})
                model = self._votes([(vote, "hold")] * 3)
                with mock.patch.object(self.db, "add_decision", side_effect=crash), self.assertRaises(KeyboardInterrupt):
                    self.run_pass(model=model)
                row = self.db.decisions(KEY)[-1]
                self.assertEqual((row.answer, row.decided_by), (None, "zach"))
                self.assertNotIn("decision_interrupts", self.db.resume(KEY))
                self.assertEqual(self.run_pass(model=model), [reconcile.Wait(KEY, "decision pending")])
                interrupts = self.db.resume(KEY)["decision_interrupts"]
                self.assertEqual(len(interrupts), 1)
                self.assertEqual(interrupts[0]["decision"]["attempt"], attempt)
                self.run_pass(model=model)
                self.assertEqual(self.db.resume(KEY)["decision_interrupts"], interrupts)
                self.assertEqual(model.n, 3)

    def test_unscoped_replacement_interrupt_survives_with_or_without_a_model(self):
        self._trio()
        self.db.upsert_item(KEY, stage="implement")
        self.db.record_segment(KEY, "implement", 1, NOW.isoformat(), NOW.isoformat(), "done")
        self.db.add_decision(KEY, "design", "Missing payload?", record={
            "restarted": "implement", "restart_attempt": 1})
        for model in (None, mock.Mock()):
            with self.subTest(model=model), mock.patch.object(engine.reconcile, "plan", return_value=[]):
                self.run_pass(model=model)
                self.run_pass(model=model)
                self.assertEqual(self.db.resume(KEY)["interrupt"], {
                    "kind": "ambiguity", "reason": "Missing payload?", "stage": "implement"})
                self.assertEqual(sum(e.message.startswith("ambiguity:") for e in self.db.log(KEY)), 1)
                self.assertNotIn("decision_interrupts", self.db.resume(KEY))

    def test_the_cap_counts_failures_a_killed_pass_did_not_absorb(self):
        self._trio()
        self.db.upsert_item(KEY, stage="implement")
        self.db.add_ask(KEY, "implement", 1, "s1", "Which parser?", ["Split", "Keep"], "")
        failed = mock.Mock(return_value=model_port.Failure("unknown", "outage"))
        for _ in range(7):
            self.run_pass(model=failed)
            self.now += timedelta(hours=2)
        # A kill after the 7th failed row but before its counter write leaves the counter one short.
        data = self.db.resume(KEY)
        retry, = data["trio_retries"].values()
        retry["failures"] -= 1
        self.db.set_resume(KEY, data)
        self.run_pass(model=failed)
        self.assertEqual(failed.call_count, 24)
        self.assertEqual(self.db.resume(KEY)["decision_interrupts"][0]["reason"], "trio unavailable after 8 attempts: Which parser?")
        self.run_pass(model=failed)
        self.assertEqual(failed.call_count, 24)

    def test_two_capped_decisions_each_have_one_interrupt_and_a_bounded_log(self):
        self._trio()
        self.db.upsert_item(KEY, stage="implement")
        first = self.db.add_ask(KEY, "implement", 1, "s1", "Which parser?", ["Split", "Keep"], "")
        second = self.db.add_ask(KEY, "implement", 1, "s1", "Which backend?", ["Local", "Remote"], "")
        failed = mock.Mock(return_value=model_port.Failure("unknown", "outage"))
        for _ in range(8):
            self.run_pass(model=failed)
            self.now += timedelta(hours=2)
        interrupts = self.db.resume(KEY)["decision_interrupts"]
        self.assertEqual([i["decision"] for i in interrupts], [
            {"id": ["ask", "Which parser?", f"ask-{first}"], "stage": "implement", "attempt": 1},
            {"id": ["ask", "Which backend?", f"ask-{second}"], "stage": "implement", "attempt": 1}])
        for _ in range(3):
            self.run_pass(model=failed)
        self.assertEqual(sum(e.message.startswith("ambiguity:") for e in self.db.log(KEY)), 2)
        self.assertEqual(self.db.resume(KEY)["decision_interrupts"], interrupts)
        self.db.answer_ask(KEY, first, "Keep")
        self.run_pass(model=failed)
        self.assertEqual(self.db.resume(KEY)["decision_interrupts"], [interrupts[1]])

    def test_retryable_replacement_failures_escalate_with_an_answerable_ask_at_the_cap(self):
        failed = self._retryable_replacement()
        for _ in range(10):
            self.now += timedelta(hours=2)
            self.run_pass(model=failed)
        self.assertEqual(failed.call_count, 24)
        ask, = self.db.asks(KEY)
        self.assertIsNone(ask.answer)
        self.assertEqual(ask.options, ["Split", "Keep"])
        interrupt = self.db.resume(KEY)["decision_interrupts"][0]
        self.assertEqual(interrupt, {"kind": "ambiguity",
            "reason": "trio unavailable after 8 attempts: Which parser?",
            "decision": {"id": ["design", "Which parser?", "parser"], "stage": "implement", "attempt": 1}})
        self.assertEqual(sum(e.message.startswith("ambiguity:") for e in self.db.log(KEY)), 1)
        self.finish("review")
        self.findings()
        self.db.engine.dispose()
        self.db = store.Store(self.root / "store.db", clock=lambda: self.now)
        self.addCleanup(self.db.engine.dispose)
        self.assertEqual(self.run_pass(model=failed), [reconcile.Wait(KEY, "decision pending")])
        self.assertEqual(failed.call_count, 24)
        self.assertEqual(self.db.resume(KEY)["decision_interrupts"][0], interrupt)
        self.db.answer_ask(KEY, ask.id, "Keep")
        self.run_pass(model=failed)
        row = self.db.decisions(KEY)[-1]
        self.assertEqual((row.answer, row.decided_by), ("Keep", "user"))
        self.assertEqual(self.db.asks(KEY)[0].answer, "Keep")
        self.assertEqual(failed.call_count, 24)
        self.assertNotIn("decision_interrupts", self.db.resume(KEY))

    def test_answered_short_ask_clears_its_ambiguity(self):
        self._trio()
        self.db.upsert_item(KEY, stage="implement")
        ask = self.db.add_ask(KEY, "implement", 1, "s1", "Which credentials?", [], "ctx")
        self.run_pass(model=mock.Mock())
        self.assertEqual(self.db.decisions(KEY)[-1].decided_by, "zach")
        self.db.answer_ask(KEY, ask, "Use the test account")
        self.run_pass()
        self.assertNotIn("decision_interrupts", self.db.resume(KEY))

    def test_a_failed_restart_launch_keeps_its_stage_move_and_hold(self):
        # #750 ruling 2 (rule 11; replaces the rollback test): every failed start keeps the stage move, human review
        # and the hold, and its pending_stage supersedes the restart route, which never drops silently.
        self._merge_gate()
        self.p.stages["review"] = policy.Place("review", "critic")
        self.db.add_decision(KEY, topic="design", question="Which parser?", answer="Split",
                             record={"decision": {"options": ["Split", "Keep"]}})
        rev = decision_escalation.reverse(self.db.decisions(KEY), "design", "Which parser?")
        self.db.add_decision(KEY, topic=rev.topic, question=rev.question, record=rev.record)
        with mock.patch.object(self, "launch", side_effect=RuntimeError("launch failed")):
            self.run_pass()
        self.assertIn((1, "implement", "merge", True), self.forge.writes)
        self.assertEqual(self.forge.rows[0]["stage"], "implement")
        self.assertTrue(next(i for i in self.db.items() if i.issue_key == KEY).human_review)
        failed, = [s for s in self.db.segments(KEY) if s.stage == "implement"]
        self.assertEqual(failed.outcome, "failed")

    def test_retryable_gate_consultation_waits_for_persisted_backoff(self):
        self._trio()
        self.p.stages["implement"] = policy.Place("implement", "builder", {"zach_gates": ("plan",)})
        self.db.upsert_item(KEY, stage="implement")
        self.db.record_segment(KEY, "implement", 1, NOW.isoformat(), NOW.isoformat(), "done")
        failed = mock.Mock(return_value=model_port.Failure("unknown", "temporarily unavailable"))
        self.assertEqual(self.run_pass(model=failed), [reconcile.Ask(KEY, "plan", "gate requires Zach's approval")])
        model = self._votes([("adopt", "approve")] * 3)
        self.assertEqual(self.run_pass(model=model), [reconcile.Ask(KEY, "plan", "gate requires Zach's approval")])
        self.assertEqual(model.n, 0)
        self.db.engine.dispose()
        self.db = store.Store(self.root / "store.db", clock=lambda: self.now)
        self.addCleanup(self.db.engine.dispose)
        self.now += timedelta(seconds=60)
        self.run_pass(model=model)
        self.assertEqual(model.n, 3)
        self.assertEqual(self.db.resume(KEY)["waived_gates"], ["plan"])

    def test_answering_split_asks_directly_releases_only_the_matching_decision(self):
        self._merge_gate()
        self.p.stages["review"] = policy.Place("review", "critic")
        first, second = [self.db.add_ask(KEY, "review", 1, "s1", "Which parser?", ["Split", "Keep"], ctx)
                         for ctx in ("one", "two")]
        model = self._votes([("concern", "Split")] * 6)
        self.run_pass(model=model)
        self.db.answer_ask(KEY, first, "Keep")
        self.assertEqual(self.run_pass(), [reconcile.Wait(KEY, "decision pending")])
        rows = decision_escalation.latest(self.db.decisions(KEY))
        self.assertEqual(rows[("ask", "Which parser?", f"ask-{first}")].answer, "Keep")
        self.assertIsNone(rows[("ask", "Which parser?", f"ask-{second}")].answer)
        self.db.answer_ask(KEY, second, "Split")
        self.assertEqual(self.run_pass(), [reconcile.Merge(KEY)])
        self.assertEqual(len(self.forge.merges), 1)
        self.assertEqual(model.n, 6)

    def test_retryable_ask_consultation_backs_off_without_delaying_siblings(self):
        self._trio()
        self.db.upsert_item(KEY, stage="implement")
        self.db.add_ask(KEY, "implement", 1, "s1", "Which parser?", ["Split", "Keep"], "")
        failed = mock.Mock(return_value=model_port.Failure("unknown", "outage"))
        self.run_pass(model=failed)
        self.assertEqual(failed.call_count, 3)
        self.run_pass(model=failed)
        self.assertEqual(failed.call_count, 3)
        self.db.add_ask(KEY, "implement", 1, "s1", "Which backend?", ["Local", "Remote"], "")
        model = self._votes([("adopt", "Local")] * 3)
        self.run_pass(model=model)
        self.assertEqual([a.answer for a in self.db.asks(KEY)], [None, "Local"])
        self.now += timedelta(seconds=60)
        self.run_pass(model=failed)
        self.assertEqual(failed.call_count, 6)
        self.now += timedelta(seconds=60)
        self.run_pass(model=failed)
        self.assertEqual(failed.call_count, 6)
        self.now += timedelta(seconds=60)
        self.run_pass(model=self._votes([("adopt", "Keep")] * 3))
        self.assertEqual(self.db.asks(KEY)[0].answer, "Keep")

    def test_an_old_ask_answer_does_not_settle_its_split_replacement(self):
        self._trio()
        self.db.clock = lambda: self.now
        self.db.upsert_item(KEY, stage="implement")
        self.db.add_ask(KEY, "implement", 1, "s1", "Which parser?", ["Split", "Keep"], "original")
        self.run_pass(model=self._votes([("adopt", "Split")] * 3))
        self.finish("implement")
        self.run_pass()
        self.finish("review")
        self.findings()
        rev = decision_escalation.reverse(self.db.decisions(KEY), "ask", "Which parser?")
        self.db.add_decision(KEY, topic=rev.topic, question=rev.question, record=rev.record)
        self.run_pass()
        self.finish("implement", 2)
        split = self._votes([("concern", "Keep")] * 6)
        self.run_pass(model=split)
        self.assertIsNone(self.db.decisions(KEY)[-1].answer)
        self.run_pass(model=split)
        self.assertIsNone(self.db.decisions(KEY)[-1].answer)
        self.assertEqual(split.n, 3)
        replacement, = [ask for ask in self.db.asks(KEY) if not ask.answer]
        self.assertIn("Restart completed: implement-2", replacement.context)
        self.db.answer_ask(KEY, replacement.id, "Keep")
        self.run_pass(model=split)
        self.assertEqual(split.n, 3)
        self.assertEqual(self.db.decisions(KEY)[-1].answer, "Keep")
        self.assertNotIn("decision_interrupts", self.db.resume(KEY))
        self.finish("review", 2)
        self.findings(2)
        self.assertEqual(self.run_pass(model=split), [reconcile.Merge(KEY)])
        self.assertEqual(split.n, 3)
        self.assertEqual(len(self.forge.merges), 1)
        self.run_pass(model=split)
        self.assertEqual(split.n, 3)

    def test_short_asks_keep_the_question_in_the_interrupt(self):
        self._trio()
        self.db.upsert_item(KEY, stage="implement")
        model = mock.Mock()
        for options in ([], ["Continue"]):
            with self.subTest(options=options):
                self.db.add_ask(KEY, "implement", 1, "s1", "Which credentials should I use?", options, "")
                self.run_pass(model=model)
                self.assertIn("Which credentials should I use?", self.db.resume(KEY)["decision_interrupts"][0]["reason"])
        model.assert_not_called()

    def test_adopting_a_sibling_ask_does_not_clear_the_split_interrupt(self):
        self._trio()
        self.db.upsert_item(KEY, stage="implement")
        self.db.add_ask(KEY, "implement", 1, "s1", "Which parser?", ["Split", "Keep"], "one")
        first = self._votes([("concern", "Split")] * 3)
        self.run_pass(model=first)
        interrupt = self.db.resume(KEY)["decision_interrupts"][0]
        # Identical text and options still belong to a different persisted ask instance.
        self.db.add_ask(KEY, "implement", 1, "s1", "Which parser?", ["Split", "Keep"], "one")
        self.run_pass(model=self._votes([("adopt", "Keep")] * 3))
        self.assertEqual(self.db.resume(KEY).get("decision_interrupts", [None])[0], interrupt)

    def test_consulting_another_stage_does_not_clear_its_siblings_limit(self):
        self._trio()
        self.p.stages["review"] = policy.Place("review", "critic", {"spend_usd": 1})
        self.db.upsert_item(KEY, stage="implement")
        self.db.set_resume(KEY, {"spend_usd": 1})
        self.db.add_ask(KEY, "review", 1, "s1", "Review choice?", ["Yes", "No"], "")
        self.run_pass(model=mock.Mock())
        interrupt = self.db.resume(KEY)["interrupt"]
        self.db.add_ask(KEY, "implement", 1, "s1", "Implementation choice?", ["Yes", "No"], "")
        # Resolve the blocked ask externally; its stage's limit remains until that stage is unblocked.
        self.db.answer_ask(KEY, self.db.asks(KEY)[0].id, "Yes")
        self.run_pass(model=self._votes([("adopt", "Yes")] * 3))
        self.assertEqual(self.db.resume(KEY).get("interrupt"), interrupt)

    def test_failed_reversal_claim_does_not_leave_a_pending_stage(self):
        self._merge_gate()
        self.p.stages["review"] = policy.Place("review", "critic")
        self.db.add_decision(KEY, topic="design", question="Which parser?", answer="Split",
                             record={"decision": {"options": ["Split", "Keep"]}})
        rev = decision_escalation.reverse(self.db.decisions(KEY), "design", "Which parser?")
        self.db.add_decision(KEY, topic=rev.topic, question=rev.question, record=rev.record)
        for failure in (ValueError("slot taken"), RuntimeError("launch failed")):
            with self.subTest(failure=failure), mock.patch.object(engine.slots, "claim", side_effect=failure):
                self.assertIsInstance(self.run_pass()[0], reconcile.Dispatch)
                self.assertNotIn("pending_stage", self.db.resume(KEY) or {})
                self.assertEqual(self.forge.writes, [])
                self.assertEqual(self.forge.rows[0]["stage"], "merge")
        self.run_pass()
        self.assertEqual(self.launched, [(KEY, "implement", 1, "builder")])
        self.assertEqual(self.forge.rows[0]["stage"], "implement")

    def test_reversal_launch_exit_rolls_back_stage_without_losing_resume(self):
        self._merge_gate()
        self.p.stages["review"] = policy.Place("review", "critic")
        self.db.add_decision(KEY, topic="design", question="Which parser?", answer="Split",
                             record={"decision": {"options": ["Split", "Keep"]}})
        rev = decision_escalation.reverse(self.db.decisions(KEY), "design", "Which parser?")
        self.db.add_decision(KEY, topic=rev.topic, question=rev.question, record=rev.record)
        with mock.patch.object(self, "launch", side_effect=SystemExit("launch exited")):
            with self.assertRaisesRegex(SystemExit, "launch exited"):
                self.run_pass()
        self.assertNotIn("pending_stage", self.db.resume(KEY))
        self.assertIn("review-1", self.db.resume(KEY)["runs"])
        self.assertFalse(any(s.stage == "implement" for s in self.db.segments(KEY)))
        self.assertEqual(self.forge.writes, [])
        self.run_pass()
        self.assertEqual(self.launched, [(KEY, "implement", 1, "builder")])

    def test_permission_asks_context_options_limits_and_failure_spend(self):
        import guard_binding
        import worker_guard
        self._trio()
        self.db.upsert_item(KEY, title="Parser", stage="")
        binding = guard_binding.Binding(KEY, "implement", 1, "session", self.root, NOW)
        worker_guard.handle("PermissionRequest", {"tool_name": "Bash", "tool_input": {"command": "touch /outside"}}, binding)
        prompts = []

        def model(request):
            prompts.append(request.prompt)
            if len(prompts) == 1:
                return model_port.Failure("error", "boom", cost_usd=0.05)
            return model_port.Reply("", {"vote": "adopt", "choice": "allow", "reasoning": "ok"}, "", 0.1, 1)

        self.run_pass(model=model)
        self.assertTrue(any("touch /outside" in p for p in prompts))
        self.assertAlmostEqual(self.db.resume(KEY)["spend_usd"], 0.25)

    def test_a_reached_limit_pings_once_and_does_not_launch(self):
        self._trio(spend_usd=1)
        self.db.upsert_item(KEY, title="Parser", stage="implement")
        self.db.set_resume(KEY, {"spend_usd": 1})
        self.db.add_ask(KEY, "implement", 1, "session", "Which parser?", ["Split", "Keep"], "ctx")
        model = self._votes([("adopt", "Split")] * 3)
        self.run_pass(model=model)
        self.run_pass(model=model)
        self.assertEqual(self.launched, [])
        self.assertEqual(model.n, 0)
        self.assertEqual(self.db.resume(KEY)["interrupt"]["kind"], "limit")
        self.assertEqual(sum(entry.message.startswith("limit:") for entry in self.db.log(KEY)), 1)

    def test_stage_policy_resolution_and_ask_ordering(self):
        from dataclasses import replace
        self._trio()
        self.p = replace(self.p, base=policy.Policy())
        self.p.stages["implement"] = policy.Place("implement", "builder", {"trio": ("builder", "critic", "operator")})
        self.p.stages["review"] = policy.Place("review", "critic", {"trio": ("builder", "critic", "operator"),
                                                                    "zach_gates": ("merge",)})
        self.p.workflows["standard"] = policy.Workflow("standard", "implement", (
            policy.Edge("implement", "review"), policy.Edge("review", "merge", "merge")))
        # Empty item/ask stage resolves workflow.start policy ("implement"); closed items are skipped; decisions precede continuations.
        closed = "github:example/project#2"
        self.forge.rows = [{"issue_key": KEY, "title": "Open", "stage": "", "forge_state": "open",
                            "review_state": "", "merge_state": "", "human_review": False}]
        self.db.upsert_item(KEY, title="Open", stage="")
        self.db.upsert_item(closed, title="Closed", stage="implement", forge_state="closed")
        self.db.add_ask(KEY, "", 1, "s1", "Which parser?", ["Split", "Keep"], "")
        self.db.add_ask(closed, "implement", 1, "s2", "Closed ask?", ["Yes", "No"], "")
        events = []

        def launch(issue_key, stage, attempt, persona):
            events.append(("launch", stage))
            self.launch(issue_key, stage, attempt, persona)
            return "claude", "high"

        def model(request):
            events.append(("model", request.model))
            return model_port.Reply("", {"vote": "adopt", "choice": "Split", "reasoning": "ok"}, "", 0.0, 1)

        engine.run_pass(forge=self.forge, store=self.db, policies=self.p, root=self.root, trees=self.trees,
                        launch=launch, workers=engine.settings.Workers(), clock=lambda: NOW, model=model)
        self.assertEqual(events[-1], ("launch", "implement"))
        self.assertEqual(len([e for e in events if e[0] == "model"]), 3)
        self.assertEqual(self.db.asks(KEY)[0].answer, "Split")
        self.assertIsNone(self.db.asks(closed)[0].answer)
        # Merge gate on an item already at "merge" resolves the incoming edge.source ("review") policy.
        self.forge.rows = [{"issue_key": KEY, "title": "Open", "stage": "merge", "forge_state": "open",
                            "review_state": "approved", "merge_state": "ready", "human_review": False}]
        self.db.upsert_item(KEY, title="Open", stage="merge", review_state="approved", merge_state="ready")
        self.db.end_segment(KEY, "implement", 1, NOW.isoformat(), "done")
        self.db.record_segment(KEY, "review", 1, (NOW + timedelta(minutes=1)).isoformat(),
                               (NOW + timedelta(minutes=2)).isoformat(), "done")
        self.db.set_resume(KEY, {})
        review_report(self.db, KEY)
        gate_model = self._votes([("adopt", "approve")] * 3)
        self.assertEqual(self.run_pass(model=gate_model), [reconcile.Ask(KEY, "merge", "gate requires Zach's approval")])
        self.assertEqual(self.db.resume(KEY)["waived_gates"], ["merge"])

    def test_observed_quota_exhaustion_pings_then_expires_at_reset(self):
        self._trio(quota_percent=80)
        self.db.upsert_item(KEY, stage="implement")
        self.db.set_resume(KEY, {"quota_percent": 100,
                                 "quota": {"reset": "in two hours", "retry_at": (NOW + timedelta(hours=2)).isoformat()}})
        self.assertEqual(self.run_pass(), [reconcile.Limit(KEY, "quota", "reached quota limit")])
        self.assertEqual(self.db.resume(KEY)["interrupt"]["kind"], "limit")
        self.assertEqual(self.launched, [])
        self.now += timedelta(hours=2)
        self.assertEqual(self.run_pass(), [reconcile.Dispatch(KEY, "implement", "builder", 1)])
        self.assertNotIn("quota_percent", self.db.resume(KEY))

    def _merge_gate(self):
        self._trio()
        self.p.stages["review"] = policy.Place("review", "critic", {"zach_gates": ("merge",)})
        self.p.workflows["standard"] = policy.Workflow("standard", "implement", (
            policy.Edge("implement", "review"), policy.Edge("review", "implement"),
            policy.Edge("review", "merge", "merge")))
        self.forge.rows[0].update(stage="merge", review_state="approved")
        self.db.sync_from_forge(self.forge.snapshots())
        self.db.record_segment(KEY, "review", 1, (NOW - timedelta(minutes=2)).isoformat(),
                               (NOW - timedelta(minutes=1)).isoformat(), "done")
        self.findings()

    def test_merge_gate_retries_outages_and_clears_ambiguity(self):
        self._merge_gate()
        failed = mock.Mock(return_value=model_port.Failure("unknown", "outage"))
        self.assertIsInstance(self.run_pass(model=failed)[0], reconcile.Ask)
        self.now += timedelta(seconds=60)
        model = self._votes([("adopt", "approve")] * 3)
        self.assertIsInstance(self.run_pass(model=model)[0], reconcile.Ask)
        self.assertEqual(model.n, 3)
        self.assertNotIn("decision_interrupts", self.db.resume(KEY))
        self.assertEqual(self.run_pass(model=model), [reconcile.Merge(KEY)])

    def _superseded_merge_gate(self, mode):
        self._merge_gate()
        if mode == "exhausted":
            model = mock.Mock(return_value=model_port.Failure("unknown", "outage"))
            for _ in range(8):
                self.run_pass(model=model)
                self.now += timedelta(hours=2)
        else:
            model = self._votes([(mode, "hold")] * 3)
            self.run_pass(model=model)
        self.assertEqual(len(self.db.resume(KEY)["decision_interrupts"]), 1)
        self.now += timedelta(hours=1)
        self.db.record_segment(KEY, "review", 2, self.now.isoformat(), self.now.isoformat(), "done")
        self.findings(attempt=2)
        fresh = self._votes([("adopt", "approve")] * 3)
        self.assertIsInstance(self.run_pass(model=fresh)[0], reconcile.Ask)
        self.assertEqual(fresh.n, 3)
        self.assertNotIn("decision_interrupts", self.db.resume(KEY))
        self.assertEqual(self.run_pass(model=fresh), [reconcile.Merge(KEY)])

    def test_a_superseded_held_merge_gate_consults_the_new_review(self):
        self._superseded_merge_gate("adopt")

    def test_a_superseded_split_merge_gate_consults_the_new_review(self):
        self._superseded_merge_gate("concern")

    def test_a_superseded_capped_merge_gate_consults_the_new_review(self):
        self._superseded_merge_gate("exhausted")

    def test_a_standing_approval_releases_an_unanswered_earlier_gate_at_merge(self):
        self._trio()
        self.p.stages["implement"] = policy.Place("implement", "builder", {"zach_gates": ("plan",)})
        self.db.upsert_item(KEY, stage="implement")
        self.db.record_segment(KEY, "implement", 1, NOW.isoformat(), NOW.isoformat(), "done")
        self.run_pass(model=self._votes([("adopt", "hold")] * 3))
        self.assertIsNone(self.db.decisions(KEY)[-1].answer)
        self.p.stages["implement"] = policy.Place("implement", "builder", {
            "zach_gates": ("plan",), "approvals": (policy.Approval("plan", NOW + timedelta(days=1)),)})
        self.now += timedelta(minutes=1)
        self.assertEqual(self.run_pass(), [reconcile.Review(KEY, 1)])
        self.assertNotIn("decision_interrupts", self.db.resume(KEY))
        self.finish("review")
        self.findings()
        self.assertEqual(self.run_pass(), [reconcile.Merge(KEY)])


    def test_return_gate_owner_interrupt_uses_the_review_context_not_the_policy_source(self):
        self._merge_gate()
        self.p.stages["architect"] = self.p.stages["review"]
        self.p.stages["review"] = policy.Place("review", "critic")
        self.p.workflows["standard"] = policy.Workflow("standard", "implement", (
            policy.Edge("implement", "review"), policy.Edge("review", "architect"),
            policy.Edge("architect", "merge", "merge")))
        self.db.record_segment(KEY, "architect", 2, (NOW - timedelta(hours=1)).isoformat(),
                               (NOW - timedelta(minutes=5)).isoformat(), "done")
        model = self._votes([("concern", "hold")] * 3)
        action, = self.run_pass(model=model)
        self.assertEqual(action.source, "architect")
        self.assertEqual((action.stage, action.attempt), ("review", 1))
        row, = self.db.decisions(KEY)
        self.assertEqual((row.record["decision"]["stage"], row.record["decision"]["attempt"]),
                         ("review", 1))
        interrupt, = self.db.resume(KEY)["decision_interrupts"]
        self.assertEqual((interrupt["decision"]["stage"], interrupt["decision"]["attempt"]), ("review", 1))
        self.assertEqual(self.run_pass(model=model), [reconcile.Wait(KEY, "decision pending")])
        self.assertEqual(self.db.resume(KEY)["decision_interrupts"], [interrupt])
        self.assertEqual(model.n, 3)

    def test_legacy_gate_hold_migrates_to_an_owner_interrupt(self):
        self._merge_gate()
        self.run_pass(model=self._votes([("adopt", "hold")] * 3))
        row = self.db.decisions(KEY)[-1]
        payload = dict(row.record["decision"])
        payload.pop("gate", None)
        self.db.add_decision(KEY, row.topic, row.question, "hold", "trio", {
            **row.record, "decision": payload})
        legacy = self.db.resume(KEY)
        legacy["interrupt"] = legacy.pop("decision_interrupts")[0]
        self.db.set_resume(KEY, legacy)
        model = mock.Mock()
        self.assertEqual(self.run_pass(model=model), [reconcile.Wait(KEY, "decision pending")])
        expected = self.db.resume(KEY)
        self.assertEqual(len(expected["decision_interrupts"]), 1)
        self.assertNotIn("interrupt", expected)
        self.run_pass(model=model)
        self.assertEqual(self.db.resume(KEY), expected)
        model.assert_not_called()

    def test_a_settlement_without_an_attempt_clears_the_owner_interrupt(self):
        self._merge_gate()
        self.run_pass(model=self._votes([("concern", "hold")] * 3))
        row = self.db.decisions(KEY)[-1]
        self.db.add_decision(KEY, row.topic, row.question, "approve", "user", {})
        self.run_pass(max_concurrency=0)
        self.assertNotIn("decision_interrupts", self.db.resume(KEY))

    def _restart_clears_downstream_owner_interrupts(self, start):
        self._merge_gate()
        if start == "plan":
            self.p.stages["plan"] = policy.Place("plan", "builder")
            self.p.workflows["standard"] = policy.Workflow("standard", "plan", (
                policy.Edge("plan", "implement"), *self.p.workflows["standard"].edges))
        self.db.record_segment(KEY, start, 1, (NOW - timedelta(hours=1)).isoformat(),
                               (NOW - timedelta(minutes=5)).isoformat(), "done")
        self.run_pass(model=self._votes([("concern", "hold")] * 3))
        self.assertEqual(len(self.db.resume(KEY)["decision_interrupts"]), 1)
        self.now += timedelta(hours=1)
        self.db.add_decision(KEY, "design", "Rework?", record={"reversed": True})
        self.assertEqual(self.run_pass(), [reconcile.Dispatch(KEY, start, "builder", 2)])
        self.assertNotIn("decision_interrupts", self.db.resume(KEY))

    def test_an_implement_restart_clears_downstream_owner_interrupts_before_review_starts_again(self):
        self._restart_clears_downstream_owner_interrupts("implement")

    def test_a_plan_restart_clears_downstream_owner_interrupts_before_review_starts_again(self):
        self._restart_clears_downstream_owner_interrupts("plan")


    def test_worker_ask_can_adopt_hold_without_gate_semantics(self):
        self._trio()
        self.db.upsert_item(KEY, stage="implement")
        self.db.add_ask(KEY, "implement", 1, "s1", "Continue?", ["approve", "hold"], "ctx")
        model = self._votes([("adopt", "hold")] * 3)
        self.run_pass(model=model)
        self.run_pass(model=model)
        self.assertEqual(self.db.asks(KEY)[0].answer, "hold")
        self.assertEqual(self.db.decisions(KEY)[-1].answer, "hold")
        self.assertEqual(model.n, 3)
        self.assertNotIn("decision_interrupts", self.db.resume(KEY))

    def test_gate_context_excludes_out_of_band_architect_and_is_not_a_wildcard(self):
        self._merge_gate()
        self.db.record_segment(KEY, "architect", 1, NOW.isoformat(), NOW.isoformat(), "done")
        self.db.add_decision(KEY, topic="merge", question="gate requires Zach's approval", answer="hold",
                             record={"decision": {"context": "", "options": ["approve", "hold"]}})
        model = self._votes([("adopt", "hold")] * 3)
        self.assertIsInstance(self.run_pass(model=model)[0], reconcile.Ask)
        self.assertEqual(model.n, 3)
        self.assertEqual(self.db.decisions(KEY)[-1].record["decision"]["context"], "review-1")
        self.assertEqual(self.run_pass(model=model), [reconcile.Wait(KEY, "decision pending")])
        self.assertEqual(model.n, 3)

    def test_gate_consultations_respect_limits_and_backoff(self):
        self._merge_gate()
        self.p.stages["review"] = policy.Place("review", "critic", {"zach_gates": ("merge",), "spend_usd": 1})
        self.db.set_resume(KEY, {**self.db.resume(KEY), "spend_usd": 1})
        model = self._votes([("adopt", "approve")] * 3)
        self.assertEqual(self.run_pass(model=model), [reconcile.Limit(KEY, "spend", "reached spend limit")])
        self.db.set_resume(KEY, {"quota": {"retry_at": (NOW + timedelta(hours=1)).isoformat()}})
        review_report(self.db, KEY)
        self.assertEqual(self.run_pass(model=model), [reconcile.Wait(KEY, "quota reset pending")])
        self.assertEqual(model.n, 0)

    def test_pending_asks_wait_before_continuation_and_do_not_displace_each_other(self):
        self._trio()
        self.db.upsert_item(KEY, stage="implement")
        for context in ("parser one", "parser two"):
            self.db.add_ask(KEY, "implement", 1, "s1", "Which parser?", ["Split", "Keep"], context)
        model = self._votes([("concern", "Split")] * 6)
        self.assertEqual(self.run_pass(model=model), [reconcile.Wait(KEY, "unanswered ask")])
        self.run_pass(model=model)
        self.assertEqual(model.n, 6)
        self.assertEqual(self.launched, [])
        self.assertEqual(len(self.db.decisions(KEY)), 2)

    def test_identical_ask_in_a_later_attempt_consults_again(self):
        self._trio()
        self.db.upsert_item(KEY, stage="implement")
        model = self._votes([("adopt", "Split")] * 6)
        self.db.add_ask(KEY, "implement", 1, "s1", "Which parser?", ["Split", "Keep"], "")
        self.run_pass(model=model)
        self.db.add_ask(KEY, "implement", 2, "s2", "Which parser?", ["Split", "Keep"], "")
        self.run_pass(model=model)
        self.assertEqual(model.n, 6)
        self.assertEqual([a.answer for a in self.db.asks(KEY)], ["Split", "Split"])

    def test_ask_backoff_and_limits_notify_even_with_an_open_worker(self):
        self._trio(quota_percent=80)
        self.run_pass()
        self.db.add_ask(KEY, "implement", 1, "s1", "Which parser?", ["Split", "Keep"], "")
        model = self._votes([("adopt", "Split")] * 3)
        self.db.set_resume(KEY, {**self.db.resume(KEY), "quota": {"retry_at": (NOW + timedelta(hours=1)).isoformat()}})
        self.run_pass(model=model)
        self.assertEqual(model.n, 0)
        self.db.set_resume(KEY, {**self.db.resume(KEY), "quota_percent": 85})
        self.run_pass(model=model)
        self.assertEqual(model.n, 0)
        self.assertEqual(self.db.resume(KEY)["interrupt"],
                         {"kind": "limit", "reason": "reached quota limit", "stage": "implement"})

    def test_repeated_quota_occurrences_each_log_once(self):
        self._trio(quota_percent=80)
        self.db.upsert_item(KEY, stage="implement")
        for _ in range(2):
            self.db.set_resume(KEY, {**(self.db.resume(KEY) or {}), "quota_percent": 100,
                "quota": {"retry_at": (self.now + timedelta(hours=1)).isoformat()}})
            self.run_pass()
            self.run_pass()
            self.now += timedelta(hours=1)
            self.run_pass()
            self.assertNotIn("interrupt", self.db.resume(KEY))
            # End the fake worker so a new exhaustion can block a later launch.
            for seg in self.db.segments(KEY):
                if not seg.ended_at:
                    self.db.end_segment(KEY, seg.stage, seg.attempt, self.now.isoformat(), "unknown")
        self.assertEqual(sum(e.message == "limit: reached quota limit" for e in self.db.log(KEY)), 2)

    def test_reversal_crash_after_claim_recovers_replacement_after_completion(self):
        self._trio()
        self.db.upsert_item(KEY, stage="implement")
        self.db.add_decision(KEY, topic="design", question="Which parser?", answer="Split", record={
            "decision": {"options": ["Split", "Keep"], "context": "original"}})
        rev = decision_escalation.reverse(self.db.decisions(KEY), "design", "Which parser?")
        self.db.add_decision(KEY, topic=rev.topic, question=rev.question, record=rev.record)
        # Durable worker and segment exist; crash prevented appending the restarted row.
        self.launch(KEY, "implement", 1, "builder")
        self.db.record_segment(KEY, "implement", 1, NOW.isoformat())
        model = self._votes([("adopt", "Keep")] * 3)
        self.run_pass(model=model)
        self.assertEqual(model.n, 0)
        self.finish("implement")
        self.run_pass(model=model)
        self.assertEqual(model.n, 3)
        self.assertEqual(self.db.decisions(KEY)[-1].answer, "Keep")
        self.assertIn("implement-1", self.db.decisions(KEY)[-1].record["decision"]["context"])

    def assert_reversal_claim_crash_recovers(self, stage, outcome):
        import tomllib
        # Use the shipped plan -> implement -> review route; isolate the restart
        # from its separate out-of-band architect requirement.
        self.p = policy.parse(tomllib.loads((Path(__file__).resolve().parents[1] / "assets/policy.toml").read_text()))
        self.p.stages["review"] = policy.Place("review", "reviewer", {"architect": False})
        self.forge.rows[0].update(stage=stage, review_state="approved")
        self.db.sync_from_forge(self.forge.snapshots())
        for index, prior in enumerate(("plan", "implement", "review")):
            self.db.record_segment(KEY, prior, 1, (NOW - timedelta(minutes=60 - index * 10)).isoformat(),
                                   (NOW - timedelta(minutes=55 - index * 10)).isoformat(), "done")
        self.findings()
        self.db.add_decision(KEY, topic="design", question="Which parser?", answer="Split", record={
            "decision": {"options": ["Split", "Keep"], "context": "original"}})
        rev = decision_escalation.reverse(self.db.decisions(KEY), "design", "Which parser?")
        self.db.add_decision(KEY, topic=rev.topic, question=rev.question, record=rev.record)
        model = self._votes([("adopt", "Keep")] * 3)
        claim = engine.slots.claim

        def crash_after_claim(*args, **kwargs):
            claim(*args, **kwargs)
            raise SystemExit("crash after durable claim")

        with mock.patch.object(engine.slots, "claim", side_effect=crash_after_claim):
            with self.assertRaisesRegex(SystemExit, "crash after durable claim"):
                self.run_pass(model=model)
        self.assertEqual(self.forge.rows[0]["stage"], stage)
        self.assertNotIn("restarted", self.db.decisions(KEY)[-1].record)
        self.assertEqual(self.run_pass(model=model), [])
        self.assertEqual(model.n, 0)
        self.finish("plan", 2, outcome)
        if outcome != "done":
            self.assertEqual(self.run_pass(model=model), [reconcile.Dispatch(KEY, "plan", "implementer", 3)])
            self.assertEqual(model.n, 0)
            self.finish("plan", 3)
        self.assertEqual(self.run_pass(model=model), [reconcile.Dispatch(KEY, "implement", "implementer", 2)])
        self.assertEqual(model.n, 3)
        self.assertEqual(self.db.decisions(KEY)[-1].answer, "Keep")
        self.finish("implement", 2)
        self.assertEqual(self.run_pass(model=model), [reconcile.Review(KEY, 2)])
        self.finish("review", 2)
        self.findings(2)
        self.assertEqual(self.run_pass(model=model), [reconcile.Merge(KEY)])
        self.assertEqual([s for _, s, _, _ in self.launched],
                         ["plan"] + (["plan"] if outcome != "done" else []) + ["implement", "review"])
        self.assertEqual(len(self.forge.merges), 1)

    def test_reversal_claim_crash_from_review_retries_failed_plan(self):
        self.assert_reversal_claim_crash_recovers("review", "error")

    def test_reversal_claim_crash_from_merge_retries_failed_plan(self):
        self.assert_reversal_claim_crash_recovers("merge", "error")

    def test_reversal_claim_crash_from_review_runs_implementation_after_plan(self):
        self.assert_reversal_claim_crash_recovers("review", "done")

    def test_reversal_claim_crash_from_merge_runs_implementation_after_plan(self):
        self.assert_reversal_claim_crash_recovers("merge", "done")

    def test_reversed_gate_waits_for_reworked_source_attempt(self):
        self._merge_gate()
        model = self._votes([("adopt", "approve")] * 6)
        self.run_pass(model=model)
        rev = decision_escalation.reverse(self.db.decisions(KEY), "merge", "gate requires Zach's approval")
        self.db.add_decision(KEY, topic=rev.topic, question=rev.question, record=rev.record)
        self.run_pass(model=model)
        self.assertEqual(model.n, 3)
        self.finish("implement")
        self.run_pass(model=model)
        self.assertEqual(model.n, 3)
        self.finish("review", 2)
        self.findings(2)
        self.assertIsInstance(self.run_pass(model=model)[0], reconcile.Ask)
        self.assertEqual(model.n, 6)
        self.assertEqual(self.db.decisions(KEY)[-1].record["decision"]["context"], "review-2")

    def test_replacement_split_is_settled_until_a_human_answers(self):
        self._trio()
        self.db.upsert_item(KEY, stage="implement")
        self.db.add_decision(KEY, topic="ask", question="Which parser?", answer="Split", record={
            "decision": {"options": ["Split", "Keep"], "context": "original"}})
        rev = decision_escalation.reverse(self.db.decisions(KEY), "ask", "Which parser?")
        self.db.add_decision(KEY, topic=rev.topic, question=rev.question, record=rev.record)
        model = self._votes([("concern", "Keep")] * 6)
        self.run_pass(model=model)
        self.finish("implement")
        self.run_pass(model=model)
        self.assertEqual(model.n, 3)
        self.run_pass(model=model)
        self.assertEqual(model.n, 3)
        self.assertEqual(self.run_pass(model=model), [reconcile.Wait(KEY, "unanswered ask")])
        self.assertEqual(model.n, 3)
        self.assertFalse(any(s.stage == "review" for s in self.db.segments(KEY)))
        ask, = self.db.asks(KEY)
        self.db.answer_ask(KEY, ask.id, "Keep")
        self.assertEqual(self.run_pass(model=model), [reconcile.Review(KEY, 1)])
        self.assertEqual(model.n, 3)
        self.assertEqual(self.db.decisions(KEY)[-1].answer, "Keep")
        self.finish("review")
        self.findings()
        self.assertEqual(self.run_pass(model=model), [reconcile.Merge(KEY)])
        self.assertEqual(model.n, 3)

    def _retryable_replacement(self):
        self._trio()
        self.db.clock = lambda: self.now
        self.db.upsert_item(KEY, stage="implement")
        self.db.add_decision(KEY, topic="design", question="Which parser?", answer="Split", record={
            "decision": {"options": ["Split", "Keep"], "context": "original", "instance": "parser"}})
        rev = decision_escalation.reverse(self.db.decisions(KEY), "design", "Which parser?")
        self.db.add_decision(KEY, rev.topic, rev.question, rev.answer, rev.decided_by, rev.record)
        self.run_pass()
        self.finish("implement")
        failed = mock.Mock(return_value=model_port.Failure("unknown", "timeout"))
        self.run_pass(model=failed)
        self.assertEqual(failed.call_count, 3)
        return failed

    def _linked_retryable_replacement(self):
        # A durable linked ask can survive a later retryable consultation (including old store data).
        self._retryable_replacement()
        row = self.db.decisions(KEY)[-1]
        asks = self.db.asks(KEY)
        ask_id = asks[0].id if asks else self.db.add_ask(
            KEY, "implement", 1, "", row.question, ["Split", "Keep"], row.record["decision"]["context"])
        self.db.add_decision(KEY, row.topic, row.question, record={**row.record, "answer_ask_id": ask_id})
        self.now += timedelta(seconds=60)
        return ask_id

    def test_retryable_replacement_uses_backoff_without_a_human_ask_or_ping(self):
        failed = self._retryable_replacement()
        self.assertEqual(self.db.asks(KEY), [])
        self.assertNotIn("decision_interrupts", self.db.resume(KEY))
        self.run_pass(model=failed)
        self.assertEqual(failed.call_count, 3)
        self.db.engine.dispose()
        self.db = store.Store(self.root / "store.db", clock=lambda: self.now)
        self.addCleanup(self.db.engine.dispose)
        self.now += timedelta(seconds=59)
        self.run_pass(model=failed)
        self.assertEqual(failed.call_count, 3)
        self.now += timedelta(seconds=1)
        model = self._votes([("adopt", "Keep")] * 3)
        self.run_pass(model=model)
        self.assertEqual(model.n, 3)
        self.assertEqual(self.db.decisions(KEY)[-1].answer, "Keep")
        self.assertEqual(self.db.asks(KEY), [])
        self.assertNotIn("trio_retries", self.db.resume(KEY))

    def test_replacement_adoption_commits_its_link_before_answering_the_ask(self):
        ask_id = self._linked_retryable_replacement()
        model = self._votes([("adopt", "Keep")] * 3)
        with mock.patch.object(self.db, "answer_ask", side_effect=SystemExit("crash before ask answer")):
            with self.assertRaises(SystemExit):
                self.run_pass(model=model)
        row = self.db.decisions(KEY)[-1]
        self.assertEqual(row.answer, "Keep")
        self.assertEqual(row.record.get("answer_ask_id"), ask_id)
        self.db.engine.dispose()
        self.db = store.Store(self.root / "store.db", clock=lambda: self.now)
        self.addCleanup(self.db.engine.dispose)
        self.run_pass()
        ask, = self.db.asks(KEY)
        self.assertEqual(ask.answer, "Keep")
        self.assertEqual(decision_escalation.identity(decision_escalation.ask_decision(self.db.decisions(KEY), ask)),
                         ("design", "Which parser?", "parser"))
        self.assertEqual(model.n, 3)

    def test_recovery_links_a_historical_ask_to_its_committed_adoption_without_a_model(self):
        ask_id = self._linked_retryable_replacement()
        prior = self.db.decisions(KEY)[-1]
        record = {key: value for key, value in prior.record.items() if key != "answer_ask_id"}
        record.update(recommendation="Keep", unanimous=True)
        self.db.add_decision(KEY, prior.topic, prior.question, "Keep", "trio", record)
        self.run_pass()
        ask, = self.db.asks(KEY)
        self.assertEqual((ask.id, ask.answer), (ask_id, "Keep"))
        row = decision_escalation.ask_decision(self.db.decisions(KEY), ask)
        self.assertEqual(row.answer, "Keep")
        self.assertEqual(row.record["answer_ask_id"], ask_id)
        self.assertEqual({decision_escalation.identity(row) for row in self.db.decisions(KEY)},
                         {("design", "Which parser?", "parser")})

    def test_replacement_loop_settles_adoption_when_a_sibling_ask_is_limit_blocked(self):
        ask_id = self._linked_retryable_replacement()
        # Put an earlier, blocked ask ahead of the replacement in the driver's ask snapshot.
        sibling = self.db.add_ask(KEY, "review", 1, "", "Review choice?", ["Yes", "No"], "review")
        self.p.stages["review"] = policy.Place("review", "critic", {"spend_usd": 0.1})
        self.db.set_resume(KEY, {**self.db.resume(KEY), "spend_usd": 1})
        original_asks = self.db.asks
        def sibling_first(key):
            return sorted(original_asks(key), key=lambda ask: ask.id != sibling)
        model = self._votes([("adopt", "Keep")] * 3)
        with mock.patch.object(self.db, "asks", side_effect=sibling_first):
            self.run_pass(model=model)
        answers = {ask.id: ask.answer for ask in self.db.asks(KEY)}
        self.assertEqual(answers, {ask_id: "Keep", sibling: None})
        self.assertEqual(self.db.decisions(KEY)[-1].record.get("answer_ask_id"), ask_id)
        self.assertEqual(model.n, 3)

    def test_unrouted_ask_blocks_merge_even_without_a_model(self):
        self._merge_gate()
        self.p.stages["review"] = policy.Place("review", "critic")
        self.db.add_ask(KEY, "review", 1, "s1", "Which parser?", ["Split", "Keep"], "")
        self.assertEqual(self.run_pass(), [reconcile.Wait(KEY, "decision pending")])
        self.assertEqual(self.forge.merges, [])

    def test_entering_a_merge_hold_preserves_its_approval_until_merge(self):
        self._merge_gate()
        self.forge.rows[0]["stage"] = "review"
        self.forge.kanban = SimpleNamespace(stage_map={}, holds=lambda stage: stage == "merge", hold_stages=())
        model = self._votes([("adopt", "approve")] * 3)
        self.assertIsInstance(self.run_pass(model=model)[0], reconcile.Ask)
        self.assertEqual(self.run_pass(model=model), [reconcile.Merge(KEY)])
        self.assertEqual(self.forge.merges, [])
        self.assertEqual(self.db.resume(KEY)["waived_gates"], ["merge"])
        self.forge.rows[0]["human_review"] = False
        self.assertEqual(self.run_pass(model=model), [reconcile.Merge(KEY)])
        self.assertEqual(len(self.forge.merges), 1)
        self.assertNotIn("waived_gates", self.db.resume(KEY))
        self.assertEqual(model.n, 3)

    def _exit_worker(self, stage: str, attempt: int, stderr: str, exit_code: int = 1) -> None:
        run_dir = Path(self.db.resume(KEY)["runs"][f"{stage}-{attempt}"]["run_dir"])
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "stdout").write_text("")
        (run_dir / "stderr").write_text(stderr)
        (run_dir / "exit.json").write_text(f'{{"exit_code": {exit_code}}}')

    def test_quota_and_outage_wait_with_backoff_persist_across_restart_and_never_ask(self):
        # An unreadable tree is a human hold. This case is the quota backoff, so the tree has to be readable.
        readable = mock.patch.object(engine.engine_facts, "_tree_facts",
                                     return_value=(TreeState("work", 0, 0, 0), False, self.now))
        readable.start()
        self.addCleanup(readable.stop)
        # 1. Four consecutive outages exceed retry_limit (3) yet back off exponentially, persist across store reopen,
        # log wait once, never Ask or Escalate, and dispatch again when retry_at passes.
        self.assertEqual(self.run_pass(), [reconcile.Dispatch(KEY, "implement", "builder", 1)])
        for attempt, minutes in ((1, 1), (2, 2), (3, 4), (4, 8)):
            self._exit_worker("implement", attempt, "503 Service Unavailable: overloaded")
            expected_retry = self.now + timedelta(minutes=minutes)
            self.assertEqual(self.run_pass(), [reconcile.Wait(KEY, "quota reset pending")])
            self.assertEqual(self.db.segments(KEY)[-1].outcome, "unknown")
            self.assertEqual(datetime.fromisoformat(self.db.resume(KEY)["quota"]["retry_at"]), expected_retry)
            self.assertNotIn("interrupt", self.db.resume(KEY))
            # Reopen store to prove the quota wait survives an engine restart.
            self.db.engine.dispose()
            self.db = store.Store(self.root / "store.db", clock=lambda: self.now)
            self.addCleanup(self.db.engine.dispose)
            self.now = expected_retry - timedelta(seconds=1)
            self.assertEqual(self.run_pass(), [reconcile.Wait(KEY, "quota reset pending")])
            self.assertEqual(len(self.launched), attempt)
            self.now = expected_retry
            self.assertEqual(self.run_pass(), [reconcile.Dispatch(KEY, "implement", "builder", attempt + 1)])
            self.assertNotIn("quota", self.db.resume(KEY))

        wait_logs = [entry.message for entry in self.db.log(KEY) if entry.message.startswith("wait: ")]
        self.assertEqual(wait_logs, ["wait: quota reset pending"])
        self.assertFalse(any(entry.message.startswith(("ask:", "escalate:")) for entry in self.db.log(KEY)))

        # 2. Quota with an explicit reset time waits until that reset and then dispatches.
        self.now = datetime(2000, 1, 1, 14, 0, tzinfo=timezone.utc)
        self._exit_worker("implement", 5, "429 quota exceeded; resets at 5pm.")
        self.assertEqual(self.run_pass(), [reconcile.Wait(KEY, "quota reset pending")])
        self.assertEqual(self.db.segments(KEY)[-1].outcome, "quota")
        self.assertEqual(self.db.resume(KEY)["quota"]["retry_at"], "2000-01-01T17:00:00+00:00")
        self.now = datetime(2000, 1, 1, 16, 59, tzinfo=timezone.utc)
        self.assertEqual(self.run_pass(), [reconcile.Wait(KEY, "quota reset pending")])
        self.now = datetime(2000, 1, 1, 17, 0, tzinfo=timezone.utc)
        self.assertEqual(self.run_pass(), [reconcile.Dispatch(KEY, "implement", "builder", 6)])
        self.assertNotIn("quota", self.db.resume(KEY))

        # 3. Completing implement resets retryable backoff: review's first outage backs off 1m, not 32m.
        self.finish("implement", attempt=6, outcome="done")
        self.assertEqual(self.run_pass(), [reconcile.Review(KEY, 1)])
        self._exit_worker("review", 1, "503 Service Unavailable: overloaded")
        self.assertEqual(self.run_pass(), [reconcile.Wait(KEY, "quota reset pending")])
        self.assertEqual(datetime.fromisoformat(self.db.resume(KEY)["quota"]["retry_at"]),
                         self.now + timedelta(minutes=1))


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
             mock.patch.object(engine, "run_pass") as passed, mock.patch.object(engine.time, "sleep", side_effect=[None, RuntimeError("plugin update ignored")]) as sleep:
            self.assertEqual(engine.main(["--root", str(self.root), "--once"]), 0)
        self.assertTrue((self.root / "store.db").is_file())
        self.assertEqual(passed.call_count, 1)
        self.assertIs(passed.call_args.kwargs["forge"], fake)
        self.assertEqual(passed.call_args.kwargs["trees"], self.root / "trees")
        self.assertEqual(passed.call_args.kwargs["workers"].claude_as_profile, "secondary")
        self.assertEqual(passed.call_args.kwargs["teardown_command"], "exit 7")
        self.assertEqual(passed.call_args.kwargs["launch"].keywords["teardown_command"], "exit 7")
        sleep.assert_not_called()

    def test_the_production_pass_consults_the_trio_through_the_claude_adapter(self):
        with mock.patch.object(engine.forge, "for_workspace", return_value=FakeForge()), \
             mock.patch.object(engine, "run_pass") as passed, \
             mock.patch.object(engine.claude_adapter, "call") as call:
            self.assertEqual(engine.main(["--root", str(self.root), "--once"]), 0)
            request = engine.model_port.Request("q")  # #879: a request names its runtime; empty is Claude
            passed.call_args.kwargs["model"](request)
        self.assertEqual(call.call_args.args, (request,))

    def test_engine_passes_workspace_rules_to_its_worker_launch_port(self):
        path = self.root / "settings.toml"
        path.write_text(path.read_text() + '\nrules = ["Keep tests."]\n')
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

    def test_failed_release_preflight_keeps_running_and_retries_before_teardown(self):
        import pages
        install = self.root / "install"
        old, current = install / "versions/old", install / "versions/new"
        old.mkdir(parents=True)
        (current / "scripts").mkdir(parents=True)
        (current / "chief_of_stuff.py").touch()
        (current / "scripts/engine.py").touch()
        (current / "requirements.txt").write_text("example==1\n")
        (install / "current").symlink_to(current)
        data = self.root / "data"
        python = data / "venv-new/bin/python"
        errors, stops = io.StringIO(), []
        serving = threading.Event()
        now = mock.Mock(return_value=1000)
        ticks = iter((1010, 1020, 1060))
        passed = mock.Mock(side_effect=lambda **_kw: self.assertTrue(serving.wait(2)))

        def sleep(_interval):
            now.return_value = next(ticks)

        def serve(_root, stop):
            stops.append(stop)
            serving.set()
            stop.wait()

        def prepare(*_args, **_kwargs):
            with self.assertRaises(supervisor.ServiceError):
                with supervisor.lock(data / "supervisor.lock"):
                    pass
            if passed.call_count < 3:
                raise supervisor.ServiceError("pip offline")
            self.assertFalse(stops[0].is_set(), "preflight must happen before board teardown")
            with self.assertRaises(supervisor.ServiceError):
                with supervisor.lock(supervisor.state_dir(self.root) / "engine.lock"):
                    pass
            return python

        with mock.patch.object(supervisor, "RELEASE", old), \
             mock.patch.object(supervisor, "data_dir", return_value=data), \
             mock.patch.object(supervisor, "ensure_venv", side_effect=prepare) as preflight, \
             mock.patch.object(pages, "supervise", side_effect=serve), \
             mock.patch.object(engine.forge, "for_workspace", return_value=FakeForge()), \
             mock.patch.object(engine, "run_pass", passed), \
             mock.patch.object(engine.time, "sleep", side_effect=sleep), \
             mock.patch.object(engine.time, "time", now), \
             mock.patch.object(engine.os, "execv") as execv, contextlib.redirect_stderr(errors):
            self.assertEqual(engine.main(["--root", str(self.root)]), 0)
        self.assertEqual(passed.call_count, 3, "backoff must leave engine passes running")
        self.assertEqual(preflight.call_args_list, [mock.call(data, release=current, timeout=60)] * 2)
        execv.assert_called_once_with(str(python), [
            str(python), str(current / "scripts/engine.py"), "--root", str(self.root), "--interval", "60"])
        self.assertTrue(stops[0].is_set())
        self.assertIn("pip offline", errors.getvalue())

    def test_failed_preflight_backoff_survives_restart_and_changes_with_pins_or_release(self):
        current = self.root / "next"
        (current / "scripts").mkdir(parents=True)
        (current / "scripts/engine.py").touch()
        requirements = current / "requirements.txt"
        requirements.write_text("example==1\n")
        data = self.root / "data"
        errors = io.StringIO()
        with mock.patch.object(supervisor, "data_dir", return_value=data), \
             mock.patch.object(supervisor, "ensure_venv", side_effect=supervisor.ServiceError("pip offline")) as prepare, \
             mock.patch.object(engine.time, "time") as now, contextlib.redirect_stderr(errors):
            now.return_value = 1000
            self.assertIsNone(engine._prepare_handoff(current))
            # A new interpreter reads the same persisted retry deadline, rather than trying pip again.
            probe = "import engine; from pathlib import Path; from unittest.mock import Mock; " \
                    f"engine.supervisor.data_dir = lambda: Path({str(data)!r}); " \
                    "engine.time.time = lambda: 1010; " \
                    "engine.supervisor.ensure_venv = Mock(side_effect=AssertionError('retried')); " \
                    f"assert engine._prepare_handoff(Path({str(current)!r})) is None; " \
                    "engine.supervisor.ensure_venv.assert_not_called()"
            result = subprocess.run([sys.executable, "-c", probe], cwd=Path(engine.__file__).parent,
                                    capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            now.return_value = 1060
            self.assertIsNone(engine._prepare_handoff(current))
            now.return_value = 1179
            self.assertIsNone(engine._prepare_handoff(current))
            self.assertEqual(prepare.call_count, 2, "second failure backs off for 120 seconds")
            now.return_value = 1180
            self.assertIsNone(engine._prepare_handoff(current))
            requirements.write_text("example==2\n")
            self.assertIsNone(engine._prepare_handoff(current))
            other = self.root / "other"
            (other / "scripts").mkdir(parents=True)
            (other / "scripts/engine.py").touch()
            (other / "requirements.txt").write_bytes(requirements.read_bytes())
            self.assertIsNone(engine._prepare_handoff(other))
            self.assertEqual(prepare.call_count, 5, "new pins and a different release retry immediately")
        self.assertEqual(errors.getvalue().count("pip offline"), 3, "unchanged failures are logged once per target")

    def test_preflight_does_not_wait_for_another_workspace_supervisor_lock(self):
        current = self.root / "next"
        (current / "scripts").mkdir(parents=True)
        (current / "scripts/engine.py").touch()
        (current / "requirements.txt").write_text("example==1\n")
        data = self.root / "data"
        with supervisor.lock(data / "supervisor.lock"), \
             mock.patch.object(supervisor, "data_dir", return_value=data), \
             mock.patch.object(supervisor, "ensure_venv") as prepare, \
             mock.patch.object(supervisor, "lock", wraps=supervisor.lock) as locked:
            # Inspect before entering to make a blocking-lock regression fail without hanging the suite.
            def check_lock(path, wait=False):
                self.assertFalse(wait, "the pass loop must not wait for the global lock")
                return locked._mock_wraps(path, wait=wait)
            locked.side_effect = check_lock
            self.assertIsNone(engine._prepare_handoff(current))
            self.assertFalse(locked.call_args.kwargs.get("wait", False))
        prepare.assert_not_called()

    def test_once_runs_the_working_release_when_update_preflight_fails(self):
        errors = io.StringIO()
        current = self.root / "next"
        (current / "scripts").mkdir(parents=True)
        (current / "scripts/engine.py").touch()
        (current / "requirements.txt").write_text("example==1\n")
        with mock.patch.object(supervisor, "current_release", return_value=current), \
             mock.patch.object(supervisor, "data_dir", return_value=self.root / "data"), \
             mock.patch.object(supervisor, "ensure_venv", side_effect=supervisor.ServiceError("pip offline")) as preflight, \
             mock.patch.object(engine.forge, "for_workspace", return_value=FakeForge()), \
             mock.patch.object(engine, "run_pass") as passed, \
             mock.patch.object(engine.os, "execv") as execv, contextlib.redirect_stderr(errors):
            self.assertEqual(engine.main(["--root", str(self.root), "--once"]), 0)
        passed.assert_called_once()
        self.assertEqual(preflight.call_args.kwargs, {"release": current, "timeout": 60})
        execv.assert_not_called()

    def test_loop_relaunches_the_installed_release_when_the_pointer_moves(self):
        newer = self.root / "install/versions/2.0.0-abc"
        (newer / "scripts").mkdir(parents=True)
        (newer / "scripts/engine.py").touch()
        (newer / "requirements.txt").write_text("example==1\n")
        python = self.root / "data/venv-new/bin/python"
        with mock.patch.object(engine.forge, "for_workspace", return_value=FakeForge()), \
             mock.patch.object(engine, "run_pass") as passed, \
             mock.patch.object(engine.supervisor, "current_release", side_effect=[supervisor.RELEASE, newer]), \
             mock.patch.object(supervisor, "data_dir", return_value=self.root / "data"), \
             mock.patch.object(supervisor, "ensure_venv", return_value=python) as preflight, \
             mock.patch.object(engine.os, "execv") as execv, \
             mock.patch.object(engine.time, "sleep", side_effect=[None, RuntimeError("update ignored")]) as sleep:
            self.assertEqual(engine.main(["--root", str(self.root), "--interval", "5"]), 0)
        self.assertEqual(passed.call_count, 1)
        sleep.assert_called_once_with(5.0)
        preflight.assert_called_once_with(self.root / "data", release=newer, timeout=60)
        execv.assert_called_once_with(str(python), [
            str(python), str(newer / "scripts/engine.py"), "--root", str(self.root), "--interval", "5.0"])

    def test_stale_launch_hands_off_before_opening_the_workspace_or_migrating(self):
        import pages
        install = self.root / "install"
        old, current = install / "versions/old", install / "versions/current"
        old.mkdir(parents=True)
        (current / "scripts").mkdir(parents=True)
        (current / "chief_of_stuff.py").touch()
        (current / "scripts/engine.py").touch()
        (current / "requirements.txt").write_text("example==1\n")
        (install / "current").symlink_to(current)
        python = self.root / "data/venv-new/bin/python"
        with mock.patch.object(supervisor, "RELEASE", old), \
             mock.patch.object(supervisor, "data_dir", return_value=self.root / "data"), \
             mock.patch.object(supervisor, "ensure_venv", return_value=python) as preflight, \
             mock.patch.object(engine.startup, "workspace_forge") as workspace, \
             mock.patch.object(engine.store_access, "migrated_store", side_effect=RuntimeError("unknown revision")) as migrated, \
             mock.patch.object(pages, "supervise") as serving, \
             mock.patch.object(engine, "run_pass") as passed, \
             mock.patch.object(engine.os, "execv") as execv:
            self.assertEqual(engine.main(["--root", str(self.root)]), 0)
        workspace.assert_not_called()
        migrated.assert_not_called()
        serving.assert_not_called()
        passed.assert_not_called()
        preflight.assert_called_once_with(self.root / "data", release=current, timeout=60)
        execv.assert_called_once_with(str(python), [
            str(python), str(current / "scripts/engine.py"), "--root", str(self.root), "--interval", "60"])

    def test_loop_keeps_running_while_the_installed_release_is_its_own(self):
        with mock.patch.object(engine.forge, "for_workspace", return_value=FakeForge()), \
             mock.patch.object(engine, "run_pass") as passed, \
             mock.patch.object(engine.supervisor, "current_release", return_value=engine.supervisor.RELEASE), \
             mock.patch.object(engine.os, "execv") as execv, \
             mock.patch.object(engine.time, "sleep", side_effect=[None, KeyboardInterrupt]):
            self.assertEqual(engine.main(["--root", str(self.root)]), 0)
        self.assertEqual(passed.call_count, 2)
        execv.assert_not_called()

    def test_handoff_waits_for_unrecorded_page_child_cleanup(self):
        import pages
        started = threading.Event()
        children = []
        real_popen = subprocess.Popen
        newer = self.root / "next"
        (newer / "scripts").mkdir(parents=True)
        (newer / "scripts/engine.py").touch()
        (newer / "requirements.txt").write_text("example==1\n")
        board = self.root / "pages"

        def start(*_a, **_kw):
            proc = real_popen([sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True)
            children.append(proc)
            self.addCleanup(lambda: proc.poll() is None and proc.kill())
            started.set()
            return proc

        def passed(**_kw):
            self.assertTrue(started.wait(2), "page server startup never began")

        def handoff(*_args):
            self.assertIsNotNone(children[0].returncode, "exec preceded detached child cleanup")
            self.assertFalse((board / pages.PID).exists())
            with supervisor.lock(supervisor.state_dir(self.root) / "pages-supervisor.lock"):
                pass

        with mock.patch.object(engine.forge, "for_workspace", return_value=FakeForge()), \
             mock.patch.object(engine, "run_pass", side_effect=passed), \
             mock.patch.object(supervisor, "current_release", side_effect=[supervisor.RELEASE, newer]), \
             mock.patch.object(supervisor, "data_dir", return_value=self.root / "data"), \
             mock.patch.object(supervisor, "ensure_venv", return_value=Path(sys.executable)), \
             mock.patch.object(pages, "read_config", return_value=SimpleNamespace(pages_dir=str(board), board_url="http://127.0.0.1:1/")), \
             mock.patch.object(pages, "from_config", return_value=(board, 1)), \
             mock.patch.object(pages.subprocess, "Popen", side_effect=start), \
             mock.patch.object(pages, "_who", side_effect=lambda _port: (threading.Event().wait(0.25), None)[1]), \
             mock.patch.object(engine.time, "sleep"), \
             mock.patch.object(engine.os, "execv", side_effect=handoff) as execv:
            self.assertEqual(engine.main(["--root", str(self.root)]), 0)
        execv.assert_called_once()

    def test_subcommands_delegate_to_supervisor_with_only_the_flags_it_accepts(self):
        for argv, expected in ((["--root", str(self.root), "install"], ["--root", str(self.root), "install"]),
                               (["status"], ["--root", str(Path(".")), "status"]),
                               (["--interval", "5", "--once", "uninstall", "--root", str(self.root)],
                                ["--root", str(self.root), "uninstall"])):
            with self.subTest(argv=argv), \
                 mock.patch.object(engine.supervisor, "main", return_value=0) as sup:
                self.assertEqual(engine.main(argv), 0)
                sup.assert_called_once_with(expected)

    def test_loop_supervises_the_page_server_and_stops_it_on_exit(self):
        import pages
        seen = []
        with mock.patch.object(pages, "supervise", side_effect=lambda root, stop: seen.append((root, stop))), \
             mock.patch.object(engine.forge, "for_workspace", return_value=FakeForge()), \
             mock.patch.object(engine, "run_pass"), \
             mock.patch.object(engine.time, "sleep", side_effect=KeyboardInterrupt):
            self.assertEqual(engine.main(["--root", str(self.root)]), 0)
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0][0], self.root)
        self.assertTrue(seen[0][1].is_set())


class PolicyFingerprintTest(unittest.TestCase):
    """#827: the merged policy's fingerprint is logged when it changes, and only then."""

    def test_a_changed_policy_is_logged_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "store.db"
            store.migrate(path)
            db = store.Store(path)
            try:
                def note(data, overlay):
                    err = io.StringIO()
                    with contextlib.redirect_stderr(err):
                        engine._note_policy(root, data, overlay, db)
                    return err.getvalue()

                first = note({"policy": {"retry_limit": 3}}, "policy.toml")
                rows = db.engine_log()
                self.assertIn("engine: policy changed:", first)
                self.assertIn("overlay policy.toml", first)
                self.assertEqual(len(rows), 1)
                fingerprint = first.split("policy changed:", 1)[1].split()[0]
                self.assertIn(fingerprint, rows[0].message)
                self.assertIn("overlay policy.toml", rows[0].message)
                self.assertEqual(rows[0].kind, "policy_change")

                again = note({"policy": {"retry_limit": 3}}, "policy.toml")
                self.assertEqual(again, "")
                self.assertEqual(len(db.engine_log()), 1)

                second = note({"policy": {"retry_limit": 5}}, "policy.toml")
                self.assertNotEqual(second, "")
                self.assertNotEqual(first, second)
                self.assertEqual(len(db.engine_log()), 2)

                shipped = note({"policy": {"retry_limit": 7}}, "")
                self.assertIn("shipped policy", shipped)
                rows = db.engine_log()
                self.assertEqual(len(rows), 3)
                self.assertIn("shipped policy", rows[-1].message)
                self.assertNotIn("overlay", rows[-1].message)
            finally:
                db.engine.dispose()


class TrioPortTest(unittest.TestCase):
    """#879: the trio's port sends Claude to claude_adapter and other hosts, read-only, to host_adapter."""

    def test_each_request_goes_to_its_hosts_adapter(self):
        with mock.patch.object(engine.claude_adapter, "call", return_value="claude") as claude, \
             mock.patch.object(engine.host_adapter, "judge", return_value="host") as host:
            self.assertEqual(engine._judge(engine.model_port.Request("q"), claude_as_profile="p"), "claude")
            self.assertEqual(engine._judge(engine.model_port.Request("q", runtime="codex")), "host")
        self.assertEqual(claude.call_args.kwargs, {"claude_as_profile": "p"})
        host.assert_called_once()

    def test_only_a_host_with_a_read_only_mode_and_its_cli_can_judge(self):
        engine._judges.cache_clear()
        self.addCleanup(engine._judges.cache_clear)
        with mock.patch.object(engine.host_adapter, "available", side_effect=lambda runtime: runtime != "agy"):
            self.assertTrue(engine._judges("claude"))
            self.assertTrue(engine._judges("codex"))
            self.assertFalse(engine._judges("agy"))
            self.assertFalse(engine._judges("nonesuch"))

    def test_the_spend_meter_keeps_the_trio_rotation(self):
        port = engine.model_port.Port(lambda request: engine.model_port.Reply("", None, "", 0.5, 1), ("x",), "random")
        metered = engine._metered(mock.Mock(), "k", port)
        self.assertEqual((metered.rotation, metered.pick), (("x",), "random"))


class LoginPathTest(unittest.TestCase):
    """#871: under its LaunchAgent the engine ran `gh` against launchd's short PATH and every pass failed."""

    def test_under_its_launch_agent_the_engine_adopts_the_login_path(self):
        env = {"CHIEF_OF_STUFF_VERSION": "0.129.85", "PATH": "/usr/bin:/bin"}
        with mock.patch.object(engine.shell_setup, "login_path", return_value="/opt/homebrew/bin:/usr/bin:/bin"):
            engine.adopt_login_path(env)
        self.assertEqual(env["PATH"], "/opt/homebrew/bin:/usr/bin:/bin")

    def test_outside_the_launch_agent_or_with_an_unreadable_path_nothing_changes(self):
        outside = {"PATH": "/usr/bin"}
        with mock.patch.object(engine.shell_setup, "login_path") as never:
            engine.adopt_login_path(outside)
        never.assert_not_called()
        unreadable = {"CHIEF_OF_STUFF_VERSION": "0.129.85", "PATH": "/usr/bin"}
        with mock.patch.object(engine.shell_setup, "login_path", return_value=""):
            engine.adopt_login_path(unreadable)
        self.assertEqual((outside["PATH"], unreadable["PATH"]), ("/usr/bin", "/usr/bin"))


if __name__ == "__main__":
    unittest.main()
