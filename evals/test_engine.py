"""Engine passes with a fake forge and launch port, and a real issue-keyed store."""

import contextlib
import io
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
import store
import supervisor
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

    def snapshots(self):
        return [self.snapshot(row["issue_key"]) for row in self.rows]

    def snapshot(self, issue_key, *, issue=None, request=None):
        row = next(r for r in self.rows if r["issue_key"] == issue_key)
        return {**dict(row), "stage_mark": None}

    def mark(self, number):
        return f"#{number}"

    def mark_for_stage(self, stage):
        return None

    def write_stage(self, number, stage, expected=None, commit=False, initialize=False):
        self.writes.append((number, stage, expected, commit))
        next(row for row in self.rows if row["issue_key"].endswith(f"#{number}"))["stage"] = stage
        return SimpleNamespace(ok=True, done=True, error="")

    def closing_request(self, issue_key):
        return SimpleNamespace(number=10, verdict="ready", approval="", head=tf.tma.HEAD)

    def issue(self, number):
        row = next(row for row in self.rows if row["issue_key"].endswith(f"#{number}"))
        return SimpleNamespace(ok=True, issue=SimpleNamespace(is_open=row["forge_state"] == "open", labels=[]))

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
            "run_dir": str(supervisor.state_dir(self.root) / "runs" / f"{key.rpartition('#')[2]}-{stage}-{attempt}"),
            "tree": str(self.trees / "fake"),
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
        self.cleanup.assert_called_with(self.root, self.trees, self.db, remove=True)

    def test_no_clone_does_not_bypass_a_recovery_hold(self):
        self.db.upsert_item(KEY, title="Build a thing", stage="implement")
        self.db.add_decision(KEY, "conflict", "implement\ndiverged commits", answer="hold")
        with mock.patch.object(engine.worker.make_worktree, "git", side_effect=AssertionError("git during planning")):
            self.assertEqual(self.run_pass(), [reconcile.Wait(KEY, "orphan recovery held")])
        self.assertEqual(self.launched, [])

    def test_one_issues_recovery_facts_failing_does_not_stop_the_pass(self):
        self.forge.rows.append(
            {"issue_key": OTHER, "stage": "implement", "forge_state": "open", "merge_state": "ready"})

        def facts(store, key, *, trees, repo):
            if key == KEY:
                raise RuntimeError("clone lock")
            return {"decisions": [], "conflict": None, "readable": False}

        with mock.patch.object(engine.engine_facts, "recovery", side_effect=facts):
            actions = self.run_pass()
        self.assertEqual(actions, [
            reconcile.Dispatch(KEY, "implement", "builder", 1),
            reconcile.Dispatch(OTHER, "implement", "builder", 1)])
        self.assertTrue(any("recovery facts failed: clone lock" in entry.message for entry in self.db.log(KEY)))
        self.assertEqual([item[0] for item in self.launched], [KEY, OTHER])

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
        self.forge.rows.append({"issue_key": OTHER, "stage": "implement", "forge_state": "open"})
        self.launch = mock.Mock(side_effect=[RuntimeError("spawn failed"), ("model", "low")])
        self.run_pass()
        self.assertEqual(self.launch.call_args_list[-1].args, (OTHER, "implement", 1, "builder"))
        self.assertEqual([s.issue_key for s in self.db.segments()], [OTHER])
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
        self.forge.rows.append({"issue_key": OTHER, "stage": "implement", "forge_state": "open"})
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

    def test_pending_decision_waits_at_any_stage_until_answered(self):
        self.db.sync_from_forge(self.forge.snapshots())
        self.db.add_decision(KEY, "conflict", "Diverged branches on #1")
        self.assertEqual(self.run_pass(), [reconcile.Wait(KEY, "decision pending")])
        self.assertEqual(self.launched, [])
        self.db.add_decision(KEY, "conflict", "Diverged branches on #1", answer="answered")
        self.assertEqual(self.run_pass(), [reconcile.Dispatch(KEY, "implement", "builder", 1)])
        self.assertEqual(self.launched, [(KEY, "implement", 1, "builder")])

    def test_killed_launcher_reconciles_started_tasks_and_pending_stages_on_restart(self):
        self.run_pass(max_concurrency=1)
        self.finish("implement")
        # Pass 2 plans Review(KEY, 1) (transitioning KEY from implement -> review).
        # Simulate the launcher being killed mid-pass after starting KEY's review worker, before runs or stage write land.
        def killed_launch(key, stage, attempt, persona):
            run = engine.worker._run_dir(self.root, key, stage, attempt)
            run.mkdir(parents=True, exist_ok=True)
            tree = self.trees / f"issue-{key.rpartition('#')[2]}-{stage}-{attempt}"
            tree.mkdir(exist_ok=True)
            data = self.db.resume(key) or {}
            data["review_trees"] = {"1": {"tree": str(tree), "branch": "feature/existing", "head": tf.tma.HEAD}}
            self.db.set_resume(key, data)
            (run / "started.json").write_text('{"pid": 301, "runner_pid": 302, "session_id": "sess-1"}')
            raise KeyboardInterrupt("engine killed mid-launch")

        self.launch = killed_launch
        with self.assertRaises(KeyboardInterrupt):
            self.run_pass()
        # Pass 3: engine restarts while KEY's review worker is still live; it adopts the worker and flushes pending_stage.
        self.launch = mock.Mock(side_effect=AssertionError("started workers must be reconciled, not relaunched"))
        self.assertEqual(self.run_pass(), [])
        self.assertEqual(self.forge.writes, [(1, "review", "implement", True)])
        # Pass 4: KEY's review worker finishes (with no exit.json because its runner died) and advances to Merge.
        key_run = engine.worker._run_dir(self.root, KEY, "review", 1)
        (key_run / "stdout").write_text('{"is_error": false, "result": "reviewed"}')
        (key_run / "stderr").write_text("")
        self.db.add_report(KEY, "review", 1, "sess-1", "done", "clean review", [])
        self.findings(attempt=1, counts={})
        self.now += timedelta(minutes=1)
        with mock.patch.object(engine.worker.process_status, "identity", return_value=None):
            actions = self.run_pass()
        self.assertEqual(actions, [reconcile.Merge(KEY)])
        self.assertEqual(self.forge.writes, [(1, "review", "implement", True), (1, "merge", "review", True)])
        self.assertEqual(self.forge.merges, [(10, True, KEY)])

    def test_s1_s4_eval_kill_engine_mid_run_and_hit_fake_quota_resumes_with_no_prompt(self):
        import json as _json
        import os as _os
        import shutil
        from evals import git_taint as taint, make_repo

        with taint.private_git_views(self.root / "private-views"):
            shutil.rmtree(self.trees)
            clone = make_repo.build(self.root, {})
            self.forge.rows.append(
                {"issue_key": OTHER, "stage": "implement", "forge_state": "open", "merge_state": "ready"})
            heads = {KEY: tf.tma.HEAD, OTHER: tf.tma.HEAD}
            branches = {KEY: "work/issue-1-implement-1", OTHER: "work/issue-2-implement-1"}

            def issue(number):
                row = next(r for r in self.forge.rows if r["issue_key"].endswith(f"#{number}"))
                return SimpleNamespace(ok=True, issue=SimpleNamespace(
                    title=f"Issue {number}", body=f"Implement #{number}",
                    is_open=row["forge_state"] == "open", labels=[]))

            def closing_request(issue_key):
                return SimpleNamespace(number=10 if issue_key == KEY else 20, verdict="ready",
                                       approval="", branch=branches[issue_key], head=heads[issue_key])

            self.forge.issue = issue
            self.forge.closing_request = closing_request
            self.forge.protected_branches = lambda: ("main",)

            fake = Path(__file__).parent / "fixtures" / "fake_claude.py"
            cli = self.root / "fake-claude-cli"
            cli.write_text(
                f"#!{sys.executable}\n"
                "import json, os, subprocess, sys, time, runpy\n"
                "from pathlib import Path\n"
                f"sys.path.insert(0, {str((Path(__file__).resolve().parents[1] / 'scripts'))!r})\n"
                "import store\n"
                "key = os.environ['CHIEF_ISSUE_KEY']\n"
                "stage = os.environ['CHIEF_STAGE']\n"
                "attempt = int(os.environ['CHIEF_ATTEMPT'])\n"
                "session = os.environ['CHIEF_SESSION_ID']\n"
                "root = Path(os.environ['CHIEF_ROOT'])\n"
                "cwd = Path.cwd()\n"
                "env = {**os.environ, 'GIT_AUTHOR_NAME': 'Worker', 'GIT_AUTHOR_EMAIL': 'w@example.test',\n"
                "       'GIT_COMMITTER_NAME': 'Worker', 'GIT_COMMITTER_EMAIL': 'w@example.test'}\n"
                "if key.endswith('#1') and stage == 'implement' and attempt == 1:\n"
                "    (cwd / 'committed.txt').write_text('from attempt 1\\n')\n"
                "    subprocess.run(['git', 'add', 'committed.txt'], cwd=cwd, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)\n"
                "    subprocess.run(['git', 'commit', '-m', 'Inherit: attempt 1 (#1)'], cwd=cwd, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)\n"
                "    (cwd / 'partial.txt').write_text('uncommitted before quota\\n')\n"
                "    deadline = time.monotonic() + 10\n"
                "    while not (root / 'release-1').exists() and time.monotonic() < deadline:\n"
                "        time.sleep(0.02)\n"
                "    idx = sys.argv.index('--') + 1\n"
                "    sys.argv[idx] = 'QUOTA ' + sys.argv[idx]\n"
                "    runpy.run_path(" + repr(str(fake.resolve())) + ", run_name='__main__')\n"
                "if stage == 'implement':\n"
                "    if key.endswith('#1'):\n"
                "        assert (cwd / 'committed.txt').read_text() == 'from attempt 1\\n'\n"
                "        assert (cwd / 'partial.txt').read_text() == 'uncommitted before quota\\n'\n"
                "    (cwd / f'done-{attempt}.txt').write_text('finished\\n')\n"
                "    subprocess.run(['git', 'add', '-A'], cwd=cwd, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)\n"
                "    subprocess.run(['git', 'commit', '-m', f'Green: finish {key}'], cwd=cwd, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)\n"
                "    branch = subprocess.run(['git', 'symbolic-ref', '--short', 'HEAD'], cwd=cwd, capture_output=True, text=True, check=True).stdout.strip()\n"
                "    subprocess.run(['git', 'push', '-u', 'origin', branch], cwd=cwd, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)\n"
                "db = store.Store(root / 'store.db')\n"
                "db.add_report(key, stage, attempt, session, 'done', f'{stage} complete', [])\n"
                "db.engine.dispose()\n"
                "runpy.run_path(" + repr(str(fake.resolve())) + ", run_name='__main__')\n"
            )
            cli.chmod(0o755)
            self.p = policy.replace(self.p, base=policy.Policy(retry_limit=0))

            real_launch = lambda k, s, a, p: engine.worker.launch(
                k, s, a, p, store=self.db, root=self.root, trees=self.trees,
                policies=self.p, forge=self.forge, clone=clone, clock=lambda: self.now)

            self.addCleanup(lambda: (self.root / "release-1").write_text("go\n")
                            if self.root.exists() else None)
            with mock.patch.object(engine.worker.claude_adapter, "resolve", return_value=str(cli)), \
                 mock.patch.object(engine.worker.make_worktree.git_trees, "audit_env",
                                   side_effect=engine.worker.make_worktree.git_trees.audit_env), \
                 mock.patch.object(engine.worker.process_status, "identity",
                                   side_effect=lambda pid, sid, host=False:
                                   "fake-start" if engine.worker.process_status.process_exists(pid) else None), \
                 mock.patch.dict(_os.environ, {"SHELL": "/bin/sh"}):
                engine.worker._runners.clear()
                self.launch = real_launch
                # Pass 1: starts real detached workers for #1 and #2.
                self.assertEqual(self.run_pass(), [
                    reconcile.Dispatch(KEY, "implement", "builder", 1),
                    reconcile.Dispatch(OTHER, "implement", "builder", 1),
                ])
                for r in list(engine.worker._runners.values()):
                    self.addCleanup(lambda proc=r: engine.worker.claude_adapter.cancel(proc)
                                    if hasattr(proc, "wait") and proc.poll() is None else None)

                # Simulate killing the engine mid-run: drop in-memory runners and unsaved run/worktree records.
                run_1 = Path(self.db.resume(KEY)["runs"]["implement-1"]["run_dir"])
                run_2 = Path(self.db.resume(OTHER)["runs"]["implement-1"]["run_dir"])
                runner_1 = engine.worker._runners.pop(str(run_1))
                runner_2 = engine.worker._runners.pop(str(run_2))
                runner_2.wait(timeout=5)
                # Simulate #2's runner dying before writing exit.json and before saving its run record (#99, #375).
                (run_2 / "exit.json").unlink(missing_ok=True)
                data_2 = self.db.resume(OTHER)
                data_2.pop("runs", None)
                self.db.set_resume(OTHER, data_2)
                data_1 = self.db.resume(KEY)
                data_1.pop("runs", None)
                self.db.set_resume(KEY, data_1)
                heads[OTHER] = make_repo.git(["rev-parse", "HEAD"], self.trees / "issue-2-implement-1")

                # Pass 2 (restart while #1 is still live, #2 finished with nobody answering):
                # #1's live worker is adopted without relaunching; #2 is collected and advances to review!
                self.now += timedelta(minutes=1)
                with mock.patch.object(engine.worker.claude_adapter, "start",
                                       wraps=engine.worker.claude_adapter.start) as spy_start:
                    actions = self.run_pass()
                self.assertEqual(actions, [reconcile.Review(OTHER, 1)])
                self.assertEqual(spy_start.call_count, 1)  # only OTHER's review started; KEY was adopted

                # Release #1's attempt 1 to hit fake quota, and wait for it and #2's review 1 to exit.
                (self.root / "release-1").write_text("go\n")
                runner_1.wait(timeout=5)
                for r in list(engine.worker._runners.values()):
                    r.wait(timeout=5)
                self.db.set_resume(OTHER, {**(self.db.resume(OTHER) or {}),
                                           "review_findings": {"attempt": 1, "counts": {}}})

                # Pass 3: #1's quota stop is collected and waits until 5pm with no prompt; #2 merges!
                self.now += timedelta(minutes=1)
                actions = self.run_pass()
                self.assertEqual(actions, [
                    reconcile.Wait(KEY, "quota reset pending"),
                    reconcile.Merge(OTHER),
                ])
                self.assertEqual(self.db.segments(KEY)[-1].outcome, "quota")
                self.assertEqual(self.db.decisions(KEY), [])
                self.assertEqual(self.db.asks(KEY), [])
                self.assertNotIn("interrupt", self.db.resume(KEY) or {})

                # Simulate orphaned worktree state on disk (worktree/work_branch lost from resume store, #725).
                resume_1 = self.db.resume(KEY)
                resume_1.pop("worktree", None)
                resume_1.pop("work_branch", None)
                self.db.set_resume(KEY, resume_1)

                # Pass 4: advance clock past 5pm quota reset; #1 recovers orphaned tree/branch/commits and resumes!
                self.now = NOW + timedelta(hours=17, minutes=1)
                actions = self.run_pass()
                self.assertEqual(actions, [reconcile.Dispatch(KEY, "implement", "builder", 2)])
                for r in list(engine.worker._runners.values()):
                    r.wait(timeout=5)
                tree_1 = self.trees / "issue-1-implement-1"
                heads[KEY] = make_repo.git(["rev-parse", "HEAD"], tree_1)
                log_1 = make_repo.git(["log", "--oneline"], tree_1)
                self.assertIn("Inherit: attempt 1 (#1)", log_1)
                self.assertIn("Green: finish github:example/project#1", log_1)

                # Pass 5 & 6: #1 collects implement-2, runs review-1, and merges with zero prompts!
                self.now += timedelta(minutes=1)
                self.assertEqual(self.run_pass(), [reconcile.Review(KEY, 1)])
                for r in list(engine.worker._runners.values()):
                    r.wait(timeout=5)
                self.findings(attempt=1, counts={})
                self.now += timedelta(minutes=1)
                self.assertEqual(self.run_pass(), [reconcile.Merge(KEY)])
                self.assertEqual(self.forge.merges, [(20, True, OTHER), (10, True, KEY)])
                for issue_key in (KEY, OTHER):
                    self.assertEqual(self.db.decisions(issue_key), [])
                    self.assertEqual(self.db.asks(issue_key), [])
                    self.assertNotIn("interrupt", self.db.resume(issue_key) or {})


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
        fake = FakeForge()
        with mock.patch.object(engine.forge, "for_workspace", return_value=fake), \
             mock.patch.object(engine, "run_pass") as passed, mock.patch.object(engine.time, "sleep") as sleep:
            self.assertEqual(engine.main(["--root", str(self.root), "--once"]), 0)
        self.assertTrue((self.root / "store.db").is_file())
        self.assertEqual(passed.call_count, 1)
        self.assertIs(passed.call_args.kwargs["forge"], fake)
        self.assertEqual(passed.call_args.kwargs["trees"], self.root / "trees")
        self.assertEqual(passed.call_args.kwargs["workers"].claude_as_profile, "secondary")
        sleep.assert_not_called()

    def test_engine_passes_workspace_rules_to_its_worker_launch_port(self):
        path = self.root / "settings.toml"
        path.write_text(path.read_text() + 'rules = ["Keep tests."]\n')
        with mock.patch.object(engine.forge, "for_workspace", return_value=FakeForge()), \
             mock.patch.object(engine, "run_pass") as passed:
            self.assertEqual(engine.main(["--root", str(self.root), "--once"]), 0)
        self.assertEqual(passed.call_args.kwargs["launch"].keywords["rules"], ("Keep tests.",))

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
