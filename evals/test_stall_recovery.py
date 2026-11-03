"""Stall recovery and failure budgets use the durable engine attempts, not another job store."""
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
import json
import os
import subprocess
import sys
import uuid
import unittest
from unittest import mock

from evals import test_engine as te
import engine
import engine_facts
import issue_gate
import reconcile
import settings
import store
import t3_adapter
import worker_health

KEY, NOW = te.KEY, te.NOW


class RecoveryTest(unittest.TestCase):
    setUp = te.EngineTest.setUp
    launch = te.EngineTest.launch
    finish = te.EngineTest.finish

    def tick(self, **kwargs):
        return engine.run_pass(forge=self.forge, store=self.db, policies=self.p, root=self.root,
            trees=self.trees, launch=self.launch, workers=self.workers, clock=lambda: self.now, **kwargs)

    def start(self, **kwargs):
        self.workers = settings.Workers(stall=settings.Stall(action="restart", **kwargs))
        self.tick()
        self.now += timedelta(minutes=4)

    def reopen(self):
        self.db.engine.dispose()
        self.db = store.Store(self.root / "store.db", clock=lambda: self.now)
        self.addCleanup(self.db.engine.dispose)

    def test_wait_policy_preserves_open_attempt_and_deduplicates_reason(self):
        self.workers = settings.Workers()
        self.tick()
        self.now += timedelta(minutes=4)
        self.tick()
        self.reopen()
        self.tick()
        self.assertIsNone(self.db.segments(KEY)[0].ended_at)
        self.assertEqual(len(self.launched), 1)
        self.assertEqual(sum("suspected stall" in row.message for row in self.db.log(KEY)), 1)

    def test_restart_verifies_stop_preserves_evidence_and_counts_only_after_relaunch(self):
        self.start(max_restarts=1)
        old = self.db.resume(KEY)["runs"]["implement-1"]
        run_dir = Path(old["run_dir"])
        run_dir.mkdir(parents=True)
        (run_dir / "stdout").write_text("previous transcript")
        os.utime(run_dir / "stdout", (self.now.timestamp()-240,)*2)
        with mock.patch.object(engine.worker, "stop", return_value=None) as stop:
            self.tick()
        stop.assert_called_once()
        self.assertEqual(self.db.segments(KEY)[0].outcome, "stopped")
        self.assertEqual((run_dir / "stdout").read_text(), "previous transcript")
        self.reopen()
        self.tick()
        self.assertEqual(self.db.segments(KEY)[0].outcome, "stalled")
        self.assertEqual(len(self.launched), 2)
        self.assertEqual(self.db.resume(KEY)["runs"]["implement-1"]["tree"], old["tree"])
        self.now += timedelta(minutes=4)
        with mock.patch.object(engine.worker, "stop") as stop:
            self.tick()
        stop.assert_not_called()
        self.assertIn("max_restarts", self.db.log(KEY)[-1].message)

    def test_unverified_stop_never_ends_attempt_or_launches_replacement_after_restart(self):
        self.start()
        with mock.patch.object(engine.worker, "stop", side_effect=RuntimeError("still alive")):
            self.tick()
            self.reopen()
            self.tick()
        self.assertIsNone(self.db.segments(KEY)[0].ended_at)
        self.assertEqual(len(self.launched), 1)
        self.assertIn("still alive", " ".join(row.message for row in self.db.log(KEY)))

    def test_t3_restart_waits_with_cancellation_reason_and_does_not_spend_budget(self):
        self.start()
        self.db.update_resume(KEY, lambda d: d["runs"]["implement-1"].update(launcher="t3", thread_id="thread"))
        evidence = worker_health.Evidence(alive=True, launcher="t3", status="running")
        with mock.patch.object(t3_adapter, "observe", return_value=evidence), \
             mock.patch.object(engine.worker, "stop") as stop:
            self.tick()
            self.reopen()
            self.tick()
        stop.assert_not_called()
        self.assertIsNone(self.db.segments(KEY)[0].ended_at)
        self.assertEqual(len(self.launched), 1)
        self.assertIn("T3 cannot cancel a thread", " ".join(row.message for row in self.db.log(KEY)))

    def test_fresh_transcript_prevents_a_stall_without_a_commit(self):
        self.start()
        record = self.db.resume(KEY)["runs"]["implement-1"]
        run_dir = Path(record["run_dir"])
        run_dir.mkdir(parents=True)
        (run_dir / "stdout").write_text("working")
        os.utime(run_dir / "stdout", (self.now.timestamp(),)*2)
        with mock.patch.object(engine.worker, "stop") as stop:
            self.assertEqual(self.tick(), [])
        stop.assert_not_called()

    def test_configurable_failure_budget_across_agents_and_recovery_keeps_attempts(self):
        self.workers = settings.Workers(failure_limit=3)
        self.db.upsert_item(KEY, stage="implement", forge_state="open")
        for n, (stage, attempt, runtime, outcome) in enumerate((("implement", 1, "claude", "failed"),
                ("review", 1, "codex", "failed"), ("implement", 2, "cursor", "stalled"))):
            at = (NOW-timedelta(minutes=3-n)).isoformat()
            self.db.record_segment(KEY, stage, attempt, at, at, outcome, runtime=runtime)
        self.db.add_log(KEY, "worker failed: concrete failure", NOW.isoformat())
        self.reopen()
        action, = self.tick()
        self.assertIsInstance(action, reconcile.Escalate)
        self.assertIn("3", action.reason)
        prep = self.db.resume(KEY)["park"]
        self.assertEqual(len(prep["research"]["attempts"]), 3)
        self.assertTrue(prep["options"])
        self.assertTrue(prep["recommendation"])
        self.assertFalse(self.launched)
        issue_gate.recover(self.forge, self.db, KEY)
        self.reopen()
        self.tick()
        self.assertEqual(len(self.db.segments(KEY)), 4)
        self.assertEqual(len(self.launched), 1)
        self.assertNotIn("park", self.db.resume(KEY))

    def test_raised_limit_allows_retry_and_lowered_limit_parks(self):
        self.workers = settings.Workers(failure_limit=3)
        self.db.upsert_item(KEY, stage="implement")
        for attempt in (1, 2):
            self.db.record_segment(KEY, "implement", attempt, NOW.isoformat(), NOW.isoformat(), "failed")
        self.assertIsInstance(self.tick()[0], reconcile.Dispatch)
        self.finish("implement", attempt=3, outcome="failed")
        self.workers = replace(self.workers, failure_limit=2)
        self.assertIsInstance(self.tick()[0], reconcile.Escalate)

    def test_recovery_rechecks_liveness_after_a_forge_hold_release(self):
        self.workers = settings.Workers()
        self.db.upsert_item(KEY, stage="implement", forge_state="open")
        self.db.set_resume(KEY, {"park": {"reason": "failed"}})
        self.forge.kanban = object()
        def raced(*args, **kwargs):
            self.db.record_segment(KEY, "implement", 1, NOW.isoformat())
            return te.SimpleNamespace(ok=True)
        with mock.patch.object(self.forge, "write_stage", side_effect=raced):
            with self.assertRaisesRegex(ValueError, "still running"):
                issue_gate.recover(self.forge, self.db, KEY)
        self.assertIn("park", self.db.resume(KEY))

    def test_pending_request_age_survives_engine_restart(self):
        self.start()
        self.db.update_resume(KEY, lambda d: d["runs"]["implement-1"].update(launcher="t3", thread_id="thread"))
        evidence = worker_health.Evidence(alive=True, launcher="t3", pending_requests=1)
        with mock.patch.object(t3_adapter, "observe", return_value=evidence):
            self.assertIn("waiting", self.tick()[0].reason)
            self.reopen()
            self.now += timedelta(minutes=4)
            self.tick()
        self.assertIn("T3 cannot cancel a thread", self.db.log(KEY)[-1].message)

    def test_architectural_and_split_trio_decisions_never_trigger_recovery(self):
        for topic in ("architecture", "ask"):
            with self.subTest(topic=topic):
                if not self.db.segments(KEY):
                    self.start()
                self.db.add_decision(KEY, topic, "Decision needed", None, "owner", {
                    "decision": {"stage": "implement", "attempt": 1, "options": ["one", "two"]}})
                self.now += timedelta(days=1)
                with mock.patch.object(engine.worker, "stop") as stop:
                    actions = self.tick()
                stop.assert_not_called()
                self.assertIn("waiting", actions[0].reason)

    def test_interrupted_stop_resumes_without_counting_a_failure_until_a_real_start(self):
        self.start()
        with mock.patch.object(engine.worker, "stop", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.tick()
        self.reopen()
        with mock.patch.object(engine.worker, "stop"):
            self.tick()
        self.assertEqual(len(self.launched), 2)
        self.assertEqual(self.db.segments(KEY)[0].outcome, "stalled")
        self.assertNotIn("stall_recovery", self.db.resume(KEY))

    def test_renewed_activity_or_decision_wait_cancels_an_unfinished_stop(self):
        self.start()
        with mock.patch.object(engine.worker, "stop", side_effect=RuntimeError("unverified")):
            self.tick()
        self.db.add_decision(KEY, "architecture", "Decision needed", None, "owner", {
            "decision": {"stage": "implement", "attempt": 1, "options": ["one", "two"]}})
        self.reopen()
        with mock.patch.object(engine.worker, "stop") as stop:
            self.tick()
        stop.assert_not_called()
        self.assertIsNone(self.db.segments(KEY)[0].ended_at)
        self.assertNotIn("stall_recovery", self.db.resume(KEY))

    def test_recovery_refuses_a_live_worker_and_unsafe_t3_evidence_does_not_close_it(self):
        self.start()
        with self.assertRaisesRegex(ValueError, "still running"):
            issue_gate.recover(self.forge, self.db, KEY)
        self.db.update_resume(KEY, lambda d: d["runs"]["implement-1"].update(launcher="t3"))
        with mock.patch.object(t3_adapter, "observe", return_value=worker_health.Evidence(launcher="t3", error="offline")), \
             mock.patch.object(engine.worker.process_status, "identity", return_value=None):
            self.tick()
        self.assertIsNone(self.db.segments(KEY)[0].ended_at)
        self.assertEqual(len(self.launched), 1)

    def test_a_dead_t3_polling_process_does_not_close_a_live_thread(self):
        self.start()
        record = self.db.resume(KEY)["runs"]["implement-1"]
        run_dir = Path(record["run_dir"])
        run_dir.mkdir(parents=True)
        (run_dir / "exit.json").write_text('{"exit_code": 1}')
        self.db.update_resume(KEY, lambda d: d["runs"]["implement-1"].update(launcher="t3"))
        evidence = worker_health.Evidence(alive=True, launcher="t3", last_activity_at=self.now)
        with mock.patch.object(t3_adapter, "observe", return_value=evidence):
            self.tick()
        self.assertIsNone(self.db.segments(KEY)[0].ended_at)
        self.assertEqual(len(self.launched), 1)


class StopTest(unittest.TestCase):
    def test_real_local_process_is_verified_gone_before_stop_returns(self):
        session = str(uuid.uuid4())
        process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)", session],
                                   start_new_session=True)
        def cleanup():
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)
        self.addCleanup(cleanup)
        identity = engine.worker.process_status.identity(process.pid, session)
        self.assertTrue(identity)
        engine.worker.stop({"session_id": session, "pid": process.pid, "identity": identity,
                            "runner_pid": process.pid, "runner_identity": identity})
        process.wait(timeout=5)
        self.assertIsNotNone(process.returncode)

    def test_stop_refuses_t3_and_reused_identity_and_verifies_survivors(self):
        record = {"session_id": "session", "pid": 11, "identity": "host",
                  "runner_pid": 12, "runner_identity": "runner"}
        with mock.patch.object(engine.worker.process_status, "stop_session") as stop:
            with self.assertRaisesRegex(RuntimeError, "T3 cannot cancel a thread"):
                engine.worker.stop({**record, "launcher": "t3"})
            stop.assert_not_called()
            with mock.patch.object(engine.worker.process_status, "identity", return_value="other"):
                with self.assertRaisesRegex(RuntimeError, "identity"):
                    engine.worker.stop(record)
            stop.assert_not_called()
            with mock.patch.object(engine.worker.process_status, "identity", side_effect=["host", "runner", "host"]):
                with self.assertRaisesRegex(RuntimeError, "ended"):
                    engine.worker.stop(record)


class T3EvidenceTest(unittest.TestCase):
    def test_show_reads_status_update_and_pending_and_refuses_errors(self):
        import tempfile
        with tempfile.TemporaryDirectory() as temp:
            run = Path(temp)
            (run / "t3.json").write_text(json.dumps({"cli": "/t3", "home": ""}))
            row = {"threadId": "thread", "status": "running", "activeRunId": "run",
                   "updatedAt": NOW.isoformat(), "pendingRequestCount": 1, "itemCount": 12}
            record = {"run_dir": str(run), "thread_id": "thread"}
            with mock.patch.object(t3_adapter, "_t3", return_value=te.SimpleNamespace(
                    returncode=0, stdout=json.dumps({"thread": row}), stderr="")) as cli:
                evidence = t3_adapter.observe(record)
            self.assertEqual((evidence.status, evidence.last_activity_at, evidence.pending_requests),
                             ("running", NOW, 1))
            self.assertIn("show", cli.call_args.args)
            with mock.patch.object(t3_adapter, "_t3", return_value=te.SimpleNamespace(
                    returncode=1, stdout=json.dumps({"thread": row}), stderr="offline")):
                self.assertTrue(t3_adapter.observe(record).error)
