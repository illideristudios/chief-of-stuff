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
            "run_dir": str(supervisor.state_dir(self.root) / "runs" / f"{key.rpartition('#')[2]}-{stage}-{attempt}"),
            "tree": str(self.trees / "fake"),
        }}
        self.db.set_resume(key, data)
        return "used-model", "high"

    def run_pass(self, **kwargs):
        return engine.run_pass(forge=self.forge, store=self.db, policies=self.p, root=self.root,
                               trees=self.trees, launch=self.launch, clock=lambda: self.now, **kwargs)

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
        self.assertEqual(self.db.log(KEY)[-1].message, "wait: needs input")

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
        self.assertEqual(self.run_pass(), [reconcile.Wait(KEY, "needs input")])
        self.assertTrue(self.db.items()[0].human_review)
        self.findings(counts={"minor": 1})
        self.assertEqual(self.run_pass(), [reconcile.Wait(KEY, "needs input")])
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
            self.assertEqual(self.run_pass(), [reconcile.Wait(KEY, "needs input")])
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
        self.assertEqual(self.run_pass(), [reconcile.Wait(KEY, "needs input")])
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

    def test_answered_decision_meeting_a_blocked_action_records_blocked_status(self):
        # M6 item 2 & #288: engine calls decision_actions when a planned action meets an answered decision.
        self.db.sync_from_forge(self.forge.snapshots())
        dec = self.db.add_decision(KEY, "design", "Approve schema?")
        self.db.answer_decision(dec.id, "Yes", decided_by="user")
        with mock.patch.object(engine.reconcile, "plan", return_value=[reconcile.Wait(KEY, "merge conflicts")]):
            self.run_pass()
        updated = next(d for d in self.db.decisions(KEY) if d.id == dec.id)
        self.assertEqual(updated.status, "blocked")
        self.assertEqual(updated.blocked_reason, "merge conflicts")

    def test_trio_consults_once_and_updates_one_row_for_consensus_and_split(self):
        import model_port
        for unanimous in (True, False):
            with self.subTest(unanimous=unanimous):
                gate = f"gate-{unanimous}"
                self.p = policy.parse({
                    "policy": {"trio": ["one", "two", "three"]},
                    "personas": {n: {"role": "advises", "model": "example"} for n in ("one", "two", "three")},
                    "stages": {"implement": {}},
                    "default_workflow": "standard",
                    "workflows": {"standard": {"start": "implement"}},
                })
                model = mock.Mock(return_value=model_port.Reply("", {
                    "vote": "adopt" if unanimous else "concern", "choice": "adopt", "reasoning": "Evidence"
                }, "", 0, 1))
                with mock.patch.object(engine.reconcile, "plan", return_value=[reconcile.Ask(KEY, gate, "Split module?")]):
                    self.run_pass(trio_port=model)
                    self.run_pass(trio_port=model)
                decs = [d for d in self.db.decisions(KEY) if d.topic == gate]
                self.assertEqual(len(decs), 1)
                self.assertEqual(model.call_count, 3)
                self.assertEqual(decs[0].status, "answered" if unanimous else "open")
                self.assertEqual(len(decs[0].record["verdicts"]), 3)

    def test_an_ask_without_a_configured_trio_stays_open_and_unconsulted(self):
        model = mock.Mock()
        with mock.patch.object(engine.reconcile, "plan", return_value=[reconcile.Ask(KEY, "gate", "Split module?")]):
            self.run_pass(trio_port=model)
        model.assert_not_called()
        self.assertEqual([d.status for d in self.db.decisions(KEY)], ["open"])
        self.assertFalse([e for e in self.db.log(KEY) if "failed" in e.message])

    def test_default_agent_has_no_declared_type(self):
        self.run_pass()
        self.assertEqual(self.db.segments(KEY)[0].agent_type, "")

    def test_pending_redispatch_runs_even_after_completed_stage(self):
        self.db.sync_from_forge(self.forge.snapshots())
        self.db.record_segment(KEY, "implement", 1, (NOW - timedelta(hours=2)).isoformat(),
                               (NOW - timedelta(hours=1)).isoformat(), "done")
        self.db.set_resume(KEY, {"redispatch": {"stage": "implement", "persona": "builder", "attempt": 2}})
        self.run_pass()
        self.assertEqual(self.launched, [(KEY, "implement", 2, "builder")])
        self.assertNotIn("redispatch", self.db.resume(KEY))

    def blocked_reroute(self, error="write refused"):
        self.db.sync_from_forge(self.forge.snapshots())
        dec = self.db.add_decision(KEY, "design", "Back to review?")
        self.db.answer_decision(dec.id, "Back", status="blocked", blocked_reason=error, rerouted_to="review")
        self.db.set_resume(KEY, {"pending_stage": {"stage": "review", "expected": "implement", "decision_id": dec.id},
                                 "redispatch": {"stage": "review", "persona": "critic", "attempt": 1}})
        return dec

    def settled_reroute(self, stage="review"):
        self.db.sync_from_forge(self.forge.snapshots())
        dec = self.db.add_decision(KEY, "design", "Earlier?")
        self.db.answer_decision(dec.id, "Went back", status="re-routed", rerouted_to=stage)
        return dec

    def test_a_retried_reroute_that_lands_restores_the_decision_and_clears_the_intent(self):
        dec = self.blocked_reroute()
        self.run_pass()
        saved = self.db.decision(dec.id)
        self.assertEqual((saved.status, saved.blocked_reason, saved.rerouted_to), ("re-routed", "", "review"))
        self.assertEqual(self.db.item(KEY).stage, "review")
        self.assertNotIn("pending_stage", self.db.resume(KEY))

    def test_a_retried_reroute_that_is_refused_again_keeps_the_decision_blocked_with_the_new_reason(self):
        dec = self.blocked_reroute()
        with mock.patch.object(self.forge, "write_stage", return_value=SimpleNamespace(ok=False, error="offline")):
            self.run_pass()
        saved = self.db.decision(dec.id)
        self.assertEqual((saved.status, saved.rerouted_to), ("blocked", "review"))
        self.assertIn("offline", saved.blocked_reason)
        self.assertEqual(self.db.resume(KEY)["pending_stage"],
                         {"stage": "review", "expected": "implement", "decision_id": dec.id})

    def test_a_refused_retry_blocks_only_the_decision_its_pending_write_names(self):
        old = self.settled_reroute()
        dec = self.blocked_reroute()
        with mock.patch.object(self.forge, "write_stage", return_value=SimpleNamespace(ok=False, error="offline")):
            self.run_pass()
        self.assertEqual(self.db.decision(old.id).status, "re-routed")
        self.assertEqual(self.db.decision(dec.id).status, "blocked")

    def test_a_landed_write_restores_only_the_decision_its_pending_write_names(self):
        dec = self.blocked_reroute()
        other = self.db.add_decision(KEY, "scope", "Conflicts?")
        self.db.answer_decision(other.id, "Yes", status="blocked", blocked_reason="merge conflicts", rerouted_to="review")
        self.run_pass()
        self.assertEqual(self.db.decision(dec.id).status, "re-routed")
        self.assertEqual((self.db.decision(other.id).status, self.db.decision(other.id).blocked_reason),
                         ("blocked", "merge conflicts"))

    def test_a_pending_write_with_no_decision_id_touches_no_decision(self):
        old = self.settled_reroute()
        dec = self.blocked_reroute()
        self.db.set_resume(KEY, {"pending_stage": {"stage": "review", "expected": "implement"}})
        with mock.patch.object(self.forge, "write_stage", return_value=SimpleNamespace(ok=False, error="offline")):
            self.run_pass()
        self.assertEqual((self.db.decision(old.id).status, self.db.decision(dec.id).blocked_reason),
                         ("re-routed", "write refused"))
        self.run_pass()
        self.assertEqual(self.db.decision(dec.id).status, "blocked")
        self.assertNotIn("pending_stage", self.db.resume(KEY))

    def test_the_engines_own_stage_move_blocks_no_settled_reroute(self):
        old = self.settled_reroute("review")
        self.run_pass()
        self.finish("implement")
        with mock.patch.object(self.forge, "write_stage", return_value=SimpleNamespace(ok=False, error="offline")):
            self.run_pass()
            self.run_pass()  # the retry of the move is what refuses it again
        self.assertEqual(self.db.resume(KEY)["pending_stage"]["stage"], "review")
        self.assertEqual(self.db.decision(old.id).status, "re-routed")

    def test_an_answered_decision_a_wait_blocked_is_unblocked_when_the_wait_is_the_decision_hold(self):
        self.db.sync_from_forge(self.forge.snapshots())
        dec = self.db.add_decision(KEY, "design", "Approve schema?")
        self.db.answer_decision(dec.id, "Yes", status="blocked", blocked_reason="merge is not ready")
        with mock.patch.object(engine.reconcile, "plan", return_value=[reconcile.Wait(KEY, "decision pending")]):
            self.run_pass()
        saved = self.db.decision(dec.id)
        self.assertEqual((saved.status, saved.blocked_reason), ("answered", ""))

    def test_a_decision_hold_wait_blocks_no_answered_decision(self):
        self.db.sync_from_forge(self.forge.snapshots())
        answered = self.db.add_decision(KEY, "design", "Approve schema?")
        self.db.answer_decision(answered.id, "Yes", status="answered")
        self.db.add_decision(KEY, "scope", "Still open?")
        with mock.patch.object(engine.reconcile, "plan", return_value=[reconcile.Wait(KEY, "decision pending")]):
            self.run_pass()
        self.assertEqual(self.db.decision(answered.id).status, "answered")

    def test_comment_delivery_keeps_what_the_board_queued_meanwhile(self):
        self.db.sync_from_forge(self.forge.snapshots())
        self.db.set_resume(KEY, {"decision_comments": ["first"]})

        def comment(number, body, commit=False):
            data = self.db.resume(KEY)  # the board POST commits while the forge call is in flight
            self.db.set_resume(KEY, {**data, "redispatch": {"stage": "implement", "persona": "builder", "attempt": 2},
                                     "decision_comments": [*data["decision_comments"], "second"]})
            return ""

        self.forge.comment = comment
        with mock.patch.object(engine.reconcile, "plan", return_value=[]):
            self.run_pass()
        data = self.db.resume(KEY)
        self.assertEqual(data["decision_comments"], ["second"])
        self.assertEqual(data["redispatch"]["attempt"], 2)

    def test_a_comment_that_fails_stays_queued_and_is_logged_once(self):
        self.db.sync_from_forge(self.forge.snapshots())
        self.db.set_resume(KEY, {"decision_comments": ["first"]})
        self.forge.comment = mock.Mock(return_value="offline")
        with mock.patch.object(engine.reconcile, "plan", return_value=[]):
            self.run_pass()
            self.run_pass()
        self.assertEqual(self.db.resume(KEY)["decision_comments"], ["first"])
        self.assertEqual([e.message for e in self.db.log(KEY)], ["decision comment failed: offline"])

    def trio_policy(self, **policy_table):
        return policy.parse({
            "policy": policy_table,
            "personas": {n: {"role": "advises", "model": "example"} for n in ("one", "two", "three")},
            "stages": {"implement": {}},
            "default_workflow": "standard",
            "workflows": {"standard": {"start": "implement"}},
        })

    def adopting_model(self, side_effect=None):
        import model_port
        reply = model_port.Reply("", {"vote": "adopt", "choice": "adopt", "reasoning": "Evidence"}, "", 0, 1)
        return mock.Mock(return_value=reply, side_effect=side_effect)

    def test_a_trio_consult_keeps_what_the_board_queued_meanwhile(self):
        self.p = self.trio_policy(trio=["one", "two", "three"])

        def board_post(request):
            self.db.set_resume(KEY, {**(self.db.resume(KEY) or {}),
                                     "redispatch": {"stage": "implement", "persona": "builder", "attempt": 2}})
            return mock.DEFAULT

        model = self.adopting_model(side_effect=board_post)
        with mock.patch.object(engine.reconcile, "plan", return_value=[reconcile.Ask(KEY, "gate", "Split module?")]):
            self.run_pass(trio_port=model)
        data = self.db.resume(KEY)
        self.assertEqual(data["redispatch"]["attempt"], 2)
        self.assertEqual(data["interrupt"]["gate"], "gate")

    def test_the_launch_pops_only_the_request_it_carried(self):
        self.db.sync_from_forge(self.forge.snapshots())
        carried = {"stage": "implement", "persona": "builder", "attempt": 2, "decision_id": 8}
        newer = {**carried, "decision_id": 9}
        self.db.set_resume(KEY, {"redispatch": carried})
        launch = self.launch

        def board_post_during_launch(key, stage, attempt, persona):
            result = launch(key, stage, attempt, persona)
            self.db.update_resume(key, lambda data: data.__setitem__("redispatch", newer))  # committed after planning
            return result

        self.launch = board_post_during_launch
        self.run_pass()
        self.assertEqual(self.db.resume(KEY)["redispatch"], newer)

    def test_a_request_past_the_retry_limit_blocks_its_decision_and_clears_only_that_request(self):
        self.db.sync_from_forge(self.forge.snapshots())
        dec = self.db.add_decision(KEY, "design", "Back?")
        self.db.answer_decision(dec.id, "Back", status="re-routed", rerouted_to="implement")
        request = {"stage": "implement", "persona": "builder", "attempt": 9, "decision_id": dec.id}
        self.db.set_resume(KEY, {"redispatch": request})
        with mock.patch.object(engine.reconcile, "plan", return_value=[reconcile.Escalate(KEY, "retry limit reached")]):
            self.run_pass()
        saved = self.db.decision(dec.id)
        self.assertEqual((saved.status, saved.blocked_reason), ("blocked", "retry limit reached"))
        self.assertNotIn("redispatch", self.db.resume(KEY))

    def test_a_request_that_escalates_keeps_a_newer_request(self):
        self.db.sync_from_forge(self.forge.snapshots())
        self.db.set_resume(KEY, {"redispatch": {"stage": "implement", "persona": "builder", "attempt": 9, "decision_id": 3}})
        newer = {"stage": "implement", "persona": "builder", "attempt": 10, "decision_id": 4}

        def plan(*args, **kwargs):
            self.db.update_resume(KEY, lambda data: data.__setitem__("redispatch", newer))
            return [reconcile.Escalate(KEY, "retry limit reached")]

        with mock.patch.object(engine.reconcile, "plan", side_effect=plan):
            self.run_pass()
        self.assertEqual(self.db.resume(KEY)["redispatch"], newer)

    def test_a_human_answer_during_a_trio_consult_wins_and_leaves_one_settled_row(self):
        self.p = self.trio_policy(trio=["one", "two", "three"])
        answered = []

        def person_answers(request):
            if not answered:
                row = self.db.decisions(KEY)[0]
                answered.append(self.db.answer_decision(row.id, "Not that", decided_by="user"))
            return mock.DEFAULT

        model = self.adopting_model(side_effect=person_answers)
        with mock.patch.object(engine.reconcile, "plan", return_value=[reconcile.Ask(KEY, "gate", "Split module?")]):
            self.run_pass(trio_port=model)
        (row,) = self.db.decisions(KEY)
        self.assertEqual((row.status, row.answer, row.decided_by), ("answered", "Not that", "user"))
        self.assertFalse(self.db.resume(KEY).get("decision_comments"))
        self.assertFalse([e for e in self.db.log(KEY) if "failed" in e.message])

    def test_a_policy_with_only_tagged_trios_is_consulted_for_the_gate_tag(self):
        self.p = self.trio_policy(trios={"gate": ["one", "two", "three"]})
        model = self.adopting_model()
        with mock.patch.object(engine.reconcile, "plan", return_value=[reconcile.Ask(KEY, "gate", "Split module?")]):
            self.run_pass(trio_port=model)
        self.assertEqual(model.call_count, 3)
        self.assertFalse([e for e in self.db.log(KEY) if "failed" in e.message])

    def test_a_gate_with_no_matching_trio_and_no_default_stays_open_and_unconsulted(self):
        self.p = self.trio_policy(trios={"other": ["one", "two", "three"]})
        model = self.adopting_model()
        with mock.patch.object(engine.reconcile, "plan", return_value=[reconcile.Ask(KEY, "gate", "Split module?")]):
            self.run_pass(trio_port=model)
        model.assert_not_called()
        self.assertEqual([d.status for d in self.db.decisions(KEY)], ["open"])
        self.assertFalse([e for e in self.db.log(KEY) if "failed" in e.message])

    def test_a_unanimous_trio_answer_queues_its_issue_comment(self):
        self.p = self.trio_policy(trio=["one", "two", "three"])
        with mock.patch.object(engine.reconcile, "plan", return_value=[reconcile.Ask(KEY, "gate", "Split module?")]):
            self.run_pass(trio_port=self.adopting_model())
        [body] = self.db.resume(KEY)["decision_comments"]
        self.assertIn("answered by trio: adopt", body)

    def test_the_segment_records_the_agent_the_launcher_uses_from_the_stage_defaults(self):
        (self.root / "CLAUDE.md").write_text(
            "## Coordinator\n- Settings: `chief-of-stuff.toml`\n- Agent: specialist task\n")
        (self.root / "chief-of-stuff.toml").write_text(
            '[lanes.code]\nstages = ["implement"]\n[stages.implement]\nagent = "specialist"\n')
        self.run_pass()
        self.assertEqual(self.db.segments(KEY)[0].agent_type, "specialist")


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
            '[kanban.map]\nplan = 0\nimplement = 0\nfix = 0\nreview = 1\nmerge = 2\n')

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
        sleep.assert_not_called()

    def test_the_production_pass_consults_the_trio_through_the_claude_adapter(self):
        with mock.patch.object(engine.forge, "for_workspace", return_value=FakeForge()), \
             mock.patch.object(engine, "run_pass") as passed, \
             mock.patch.object(engine.claude_adapter, "call") as call:
            self.assertEqual(engine.main(["--root", str(self.root), "--once"]), 0)
            request = object()
            passed.call_args.kwargs["trio_port"](request)
        self.assertEqual(call.call_args.args, (request,))

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
