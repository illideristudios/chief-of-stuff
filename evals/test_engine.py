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
import policy
import reconcile
import store
import supervisor

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

    def snapshots(self):
        return [dict(row) for row in self.rows]

    def write_stage(self, number, stage, expected=None, commit=False):
        self.writes.append((number, stage, expected, commit))
        next(row for row in self.rows if row["issue_key"].endswith(f"#{number}"))["stage"] = stage
        return SimpleNamespace(ok=True, done=True, error="")

    def closing_request(self, issue_key):
        return SimpleNamespace(number=10)

    def merge(self, number, commit=False, issue_key=None):
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

    def launch(self, key, stage, attempt, persona):
        self.launched.append((key, stage, attempt, persona))
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

    def test_blocking_findings_follow_the_planned_fix_stage(self):
        self.run_pass()
        self.finish("implement")
        self.run_pass()
        self.finish("review")
        self.findings(counts={"minor": 1})
        self.assertEqual(self.run_pass(), [reconcile.Dispatch(KEY, "implement", "builder", 2)])
        self.assertEqual(self.forge.writes[-1], (1, "implement", "review", True))

    def test_stage_write_failure_is_logged_and_retried_before_planning(self):
        self.run_pass()
        self.finish("implement")
        with mock.patch.object(self.forge, "write_stage", return_value=SimpleNamespace(ok=False, error="label offline")):
            self.run_pass()
        self.assertIn("label offline", self.db.log(KEY)[-1].message)
        self.finish("review")
        self.findings()
        self.run_pass()
        self.assertEqual(self.forge.writes[0], (1, "review", "implement", True))
        self.assertEqual(self.forge.writes[-1], (1, "merge", "review", True))

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
        self.assertEqual(planned.call_args.args[5], self.now)
        self.assertEqual(planned.call_args.args[6], {KEY: {}})

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
        (self.root / "CLAUDE.md").write_text("# Workspace\n\n## Coordinator\n\n- Worktrees: `trees/`\n")

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
        errors = io.StringIO()
        with mock.patch.object(engine.forge, "for_workspace", return_value=None), contextlib.redirect_stderr(errors):
            self.assertEqual(engine.main(["--root", str(self.root), "--once"]), 2)
        self.assertFalse((self.root / "store.db").exists())
        self.assertIn("Backlog:", errors.getvalue())

    def test_default_interval_is_sixty_seconds(self):
        with mock.patch.object(engine.forge, "for_workspace", return_value=FakeForge()), \
             mock.patch.object(engine, "run_pass"), \
             mock.patch.object(engine.time, "sleep", side_effect=KeyboardInterrupt) as sleep:
            self.assertEqual(engine.main(["--root", str(self.root)]), 0)
        sleep.assert_called_once_with(60)

    def test_supervisor_default_entry_is_the_engine_driver(self):
        self.assertEqual(supervisor.ENGINE, (str(Path(engine.__file__).resolve()),))


if __name__ == "__main__":
    unittest.main()
