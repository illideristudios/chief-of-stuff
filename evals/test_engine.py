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
import model_port
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
        import decision_actions
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
        rev = decision_actions.reverse(self.db.decisions(KEY), "ask", "Which parser?")
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
        self.db.set_resume(KEY, {"quota_percent": 100, "review_findings": {"attempt": 1, "counts": {}},
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
        self.db.answer_ask(self.db.asks(KEY)[0].id, "Split")
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
        # The old gate row survives while the ask prevents any new gate consultation.
        self.assertEqual(self.db.decisions(KEY)[0].record["decision"]["context"], "implement-1")
        self.assertIsNone(self.db.decisions(KEY)[0].answer)
        self.db.answer_ask(ask_id, "Keep")
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
        self.db.answer_ask(first, "Keep")
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
        self.db.answer_ask(ask.id, "Keep")
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
        self.db.answer_ask(ask, "Use the test account")
        self.run_pass()
        self.assertNotIn("decision_interrupts", self.db.resume(KEY))

    def test_failed_launch_crash_before_resume_rollback_drops_route_on_recovery(self):
        import decision_actions
        self._merge_gate()
        self.p.stages["review"] = policy.Place("review", "critic")
        self.db.add_decision(KEY, topic="design", question="Which parser?", answer="Split",
                             record={"decision": {"options": ["Split", "Keep"]}})
        rev = decision_actions.reverse(self.db.decisions(KEY), "design", "Which parser?")
        self.db.add_decision(KEY, topic=rev.topic, question=rev.question, record=rev.record)
        real_set = self.db.set_resume
        def crash_on_rollback(key, data):
            if "pending_stage" not in data:
                raise SystemExit("crash between segment deletion and rollback")
            real_set(key, data)
        with mock.patch.object(self, "launch", side_effect=RuntimeError("launch failed")), \
                mock.patch.object(self.db, "set_resume", side_effect=crash_on_rollback):
            with self.assertRaises(SystemExit):
                self.run_pass()
        self.assertIn("pending_stage", self.db.resume(KEY))
        self.assertFalse(any(s.stage == "implement" for s in self.db.segments(KEY)))
        with mock.patch.object(engine.slots, "claim", side_effect=ValueError("slot unavailable")):
            self.run_pass()
        self.assertEqual(self.forge.writes, [])
        self.assertEqual(self.forge.rows[0]["stage"], "merge")
        self.assertNotIn("pending_stage", self.db.resume(KEY))
        self.run_pass()
        self.assertEqual(self.launched, [(KEY, "implement", 1, "builder")])

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
        import decision_actions
        self._merge_gate()
        self.p.stages["review"] = policy.Place("review", "critic")
        first, second = [self.db.add_ask(KEY, "review", 1, "s1", "Which parser?", ["Split", "Keep"], ctx)
                         for ctx in ("one", "two")]
        model = self._votes([("concern", "Split")] * 6)
        self.run_pass(model=model)
        self.db.answer_ask(first, "Keep")
        self.assertEqual(self.run_pass(), [reconcile.Wait(KEY, "decision pending")])
        rows = decision_actions.latest(self.db.decisions(KEY))
        self.assertEqual(rows[("ask", "Which parser?", f"ask-{first}")].answer, "Keep")
        self.assertIsNone(rows[("ask", "Which parser?", f"ask-{second}")].answer)
        self.db.answer_ask(second, "Split")
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
        import decision_actions
        self._trio()
        self.db.clock = lambda: self.now
        self.db.upsert_item(KEY, stage="implement")
        self.db.add_ask(KEY, "implement", 1, "s1", "Which parser?", ["Split", "Keep"], "original")
        self.run_pass(model=self._votes([("adopt", "Split")] * 3))
        self.finish("implement")
        self.run_pass()
        self.finish("review")
        self.findings()
        rev = decision_actions.reverse(self.db.decisions(KEY), "ask", "Which parser?")
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
        self.db.answer_ask(replacement.id, "Keep")
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
        self.db.answer_ask(self.db.asks(KEY)[0].id, "Yes")
        self.run_pass(model=self._votes([("adopt", "Yes")] * 3))
        self.assertEqual(self.db.resume(KEY).get("interrupt"), interrupt)

    def test_failed_reversal_claim_does_not_leave_a_pending_stage(self):
        import decision_actions
        self._merge_gate()
        self.p.stages["review"] = policy.Place("review", "critic")
        self.db.add_decision(KEY, topic="design", question="Which parser?", answer="Split",
                             record={"decision": {"options": ["Split", "Keep"]}})
        rev = decision_actions.reverse(self.db.decisions(KEY), "design", "Which parser?")
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
        import decision_actions
        self._merge_gate()
        self.p.stages["review"] = policy.Place("review", "critic")
        self.db.add_decision(KEY, topic="design", question="Which parser?", answer="Split",
                             record={"decision": {"options": ["Split", "Keep"]}})
        rev = decision_actions.reverse(self.db.decisions(KEY), "design", "Which parser?")
        self.db.add_decision(KEY, topic=rev.topic, question=rev.question, record=rev.record)
        with mock.patch.object(self, "launch", side_effect=SystemExit("launch exited")):
            with self.assertRaisesRegex(SystemExit, "launch exited"):
                self.run_pass()
        self.assertNotIn("pending_stage", self.db.resume(KEY))
        self.assertEqual(self.db.resume(KEY)["review_findings"], {"attempt": 1, "counts": {}})
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
        self.db.set_resume(KEY, {"review_findings": {"attempt": 1, "counts": {}}})
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
        self.db.set_resume(KEY, {"review_findings": {"attempt": 1, "counts": {}},
                                 "quota": {"retry_at": (NOW + timedelta(hours=1)).isoformat()}})
        self.assertEqual(self.run_pass(model=model), [reconcile.Wait(KEY, "quota reset pending")])
        self.assertEqual(model.n, 0)

    def test_pending_asks_wait_before_continuation_and_do_not_displace_each_other(self):
        self._trio()
        self.db.upsert_item(KEY, stage="implement")
        for context in ("parser one", "parser two"):
            self.db.add_ask(KEY, "implement", 1, "s1", "Which parser?", ["Split", "Keep"], context)
        model = self._votes([("concern", "Split")] * 6)
        self.assertEqual(self.run_pass(model=model), [reconcile.Wait(KEY, "decision pending")])
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
        import decision_actions
        self._trio()
        self.db.upsert_item(KEY, stage="implement")
        self.db.add_decision(KEY, topic="design", question="Which parser?", answer="Split", record={
            "decision": {"options": ["Split", "Keep"], "context": "original"}})
        rev = decision_actions.reverse(self.db.decisions(KEY), "design", "Which parser?")
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
        import decision_actions
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
        rev = decision_actions.reverse(self.db.decisions(KEY), "design", "Which parser?")
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
        import decision_actions
        self._merge_gate()
        model = self._votes([("adopt", "approve")] * 6)
        self.run_pass(model=model)
        rev = decision_actions.reverse(self.db.decisions(KEY), "merge", "gate requires Zach's approval")
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
        import decision_actions
        self._trio()
        self.db.upsert_item(KEY, stage="implement")
        self.db.add_decision(KEY, topic="ask", question="Which parser?", answer="Split", record={
            "decision": {"options": ["Split", "Keep"], "context": "original"}})
        rev = decision_actions.reverse(self.db.decisions(KEY), "ask", "Which parser?")
        self.db.add_decision(KEY, topic=rev.topic, question=rev.question, record=rev.record)
        model = self._votes([("concern", "Keep")] * 6)
        self.run_pass(model=model)
        self.finish("implement")
        self.run_pass(model=model)
        self.assertEqual(model.n, 3)
        self.run_pass(model=model)
        self.assertEqual(model.n, 3)
        self.assertEqual(self.run_pass(model=model), [reconcile.Wait(KEY, "decision pending")])
        self.assertEqual(model.n, 3)
        self.assertFalse(any(s.stage == "review" for s in self.db.segments(KEY)))
        ask, = self.db.asks(KEY)
        self.db.answer_ask(ask.id, "Keep")
        self.assertEqual(self.run_pass(model=model), [reconcile.Review(KEY, 1)])
        self.assertEqual(model.n, 3)
        self.assertEqual(self.db.decisions(KEY)[-1].answer, "Keep")
        self.finish("review")
        self.findings()
        self.assertEqual(self.run_pass(model=model), [reconcile.Merge(KEY)])
        self.assertEqual(model.n, 3)

    def _retryable_replacement(self):
        import decision_actions
        self._trio()
        self.db.clock = lambda: self.now
        self.db.upsert_item(KEY, stage="implement")
        self.db.add_decision(KEY, topic="design", question="Which parser?", answer="Split", record={
            "decision": {"options": ["Split", "Keep"], "context": "original", "instance": "parser"}})
        rev = decision_actions.reverse(self.db.decisions(KEY), "design", "Which parser?")
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
        import decision_actions
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
        self.assertEqual(decision_actions.identity(decision_actions.ask_decision(self.db.decisions(KEY), ask)),
                         ("design", "Which parser?", "parser"))
        self.assertEqual(model.n, 3)

    def test_recovery_links_a_historical_ask_to_its_committed_adoption_without_a_model(self):
        import decision_actions
        ask_id = self._linked_retryable_replacement()
        prior = self.db.decisions(KEY)[-1]
        record = {key: value for key, value in prior.record.items() if key != "answer_ask_id"}
        record.update(recommendation="Keep", unanimous=True)
        self.db.add_decision(KEY, prior.topic, prior.question, "Keep", "trio", record)
        self.run_pass()
        ask, = self.db.asks(KEY)
        self.assertEqual((ask.id, ask.answer), (ask_id, "Keep"))
        row = decision_actions.ask_decision(self.db.decisions(KEY), ask)
        self.assertEqual(row.answer, "Keep")
        self.assertEqual(row.record["answer_ask_id"], ask_id)
        self.assertEqual({decision_actions.identity(row) for row in self.db.decisions(KEY)},
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
        self.forge.kanban = SimpleNamespace(stage_map={}, holds=lambda stage: stage == "merge")
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
