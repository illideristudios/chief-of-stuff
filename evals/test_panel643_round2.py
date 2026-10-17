"""Regressions for the third M6 panel round: the board POST records intent and engine/notify keep it."""
import http.client
import sqlite3
import threading
import unittest
from datetime import timedelta
from types import SimpleNamespace
from unittest import mock
from urllib.parse import urlencode

from evals import test_m6_store_board as fixtures
from evals import test_notify as notices
import notify
import pages
import store

KEY, NOW = fixtures.KEY, fixtures.NOW


class BoardPostTest(unittest.TestCase):
    def setUp(self):
        self.root, self.db = fixtures.make_store_workspace()
        self.addCleanup(self.db.engine.dispose)
        self.db.upsert_item(KEY, title="First repository", stage="review")

    def post(self, data, forge):
        server = pages.make_server(self.root / "pages", 0, root=self.root, refresh=lambda *a: None, every=3600)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=10)
        with mock.patch.object(pages.forge, "for_workspace", return_value=forge):
            conn.request("POST", "/", urlencode(data))
            response = conn.getresponse()
            response.read()
        conn.close()
        return response.status

    def test_a_refused_reroute_keeps_its_intent_for_the_engine_and_logs_the_refusal(self):
        dec = self.db.add_decision(KEY, "design", "Choose")
        fg = mock.Mock()
        fg.write_stage.return_value = SimpleNamespace(ok=False, done=False, error="write refused")
        status = self.post({"decision_id": dec.id, "action": "reroute", "target_stage": "implement"}, fg)
        self.assertEqual(status, 409)
        saved = self.db.decision(dec.id)
        self.assertEqual((saved.status, saved.rerouted_to), ("blocked", "implement"))
        resume = self.db.resume(KEY)
        self.assertEqual(resume["pending_stage"], {"stage": "implement", "expected": "review", "decision_id": dec.id})
        self.assertEqual((resume["redispatch"]["stage"], resume["redispatch"]["decision_id"]), ("implement", dec.id))
        self.assertEqual(self.db.item(KEY).stage, "review")
        self.assertIn("stage write refused: write refused", [e.message for e in self.db.log(KEY)])

    def test_a_landed_reroute_leaves_no_pending_write(self):
        dec = self.db.add_decision(KEY, "design", "Choose")
        fg = mock.Mock()
        fg.write_stage.return_value = SimpleNamespace(ok=True, done=True, error="")
        self.assertEqual(self.post({"decision_id": dec.id, "action": "reroute", "target_stage": "implement"}, fg), 303)
        self.assertEqual(self.db.decision(dec.id).status, "re-routed")
        self.assertEqual(self.db.item(KEY).stage, "implement")
        resume = self.db.resume(KEY)
        self.assertNotIn("pending_stage", resume)
        self.assertEqual(resume["redispatch"]["stage"], "implement")

    def test_forge_io_runs_outside_the_store_write_lock(self):
        dec = self.db.add_decision(KEY, "design", "Choose")
        seen = []

        def write_stage(*args, **kwargs):
            try:
                with sqlite3.connect(self.root / "store.db", timeout=0.2) as other:
                    other.execute("BEGIN IMMEDIATE")
                    other.rollback()
                seen.append("free")
            except sqlite3.OperationalError as exc:
                seen.append(str(exc))
            return SimpleNamespace(ok=True, done=True, error="")

        fg = mock.Mock()
        fg.write_stage.side_effect = write_stage
        self.assertEqual(self.post({"decision_id": dec.id, "action": "reroute", "target_stage": "implement"}, fg), 303)
        self.assertEqual(seen, ["free"])

    def test_a_target_with_no_worker_is_refused_before_anything_is_written(self):
        dec = self.db.add_decision(KEY, "design", "Choose")
        fg = mock.Mock()
        self.assertEqual(self.post({"decision_id": dec.id, "action": "reroute", "target_stage": "merge"}, fg), 400)
        fg.write_stage.assert_not_called()
        self.assertEqual(self.db.decision(dec.id).status, "open")
        self.assertNotIn("pending_stage", self.db.resume(KEY) or {})

    def test_comments_are_queued_for_the_engine_not_sent_from_the_post(self):
        dec = self.db.add_decision(KEY, "design", "Choose")
        fg = mock.Mock()
        self.assertEqual(self.post({"decision_id": dec.id, "answer": "Yes"}, fg), 303)
        fg.comment.assert_not_called()
        queued = self.db.resume(KEY)["decision_comments"]
        self.assertEqual(len(queued), 1)
        self.assertIn("answered by user: Yes", queued[0])


class NotificationRecoveryTest(unittest.TestCase):
    ws = notices.NotifyTest.ws

    def rows(self, w, now=notices.NOW):
        cfg, settings = notify.configuration(w.root)
        return notify.store_rows(w.root, cfg, settings, now)

    def recovered(self, w, later):
        w.db.record_segment(notices.KEY, "fix", 2, later.isoformat(), later.isoformat(), "done")

    def test_closed_issues_report_no_failure_even_when_nothing_recovered(self):
        w = self.ws()
        w.db.upsert_item(notices.KEY)
        w.db.add_log(notices.KEY, "dispatch failed: no slot", notices.NOW.isoformat())
        w.db.record_segment(notices.KEY, "fix", 1, notices.NOW.isoformat(), notices.NOW.isoformat(), "failed")
        self.assertEqual(len(self.rows(w)), 2)
        w.db.upsert_item(notices.KEY, forge_state="closed")
        self.assertEqual(self.rows(w), [])

    def test_a_later_worker_success_does_not_hide_a_queued_comment_failure(self):
        w = self.ws()
        w.db.upsert_item(notices.KEY)
        w.db.set_resume(notices.KEY, {"decision_comments": ["Decision #1 answered"]})
        w.db.add_log(notices.KEY, "decision comment failed: offline", notices.NOW.isoformat(), kind=store.OP_DECISION_COMMENT)
        later = notices.NOW + timedelta(minutes=5)
        self.recovered(w, later)
        self.assertEqual(len(self.rows(w, later)), 1)
        w.db.set_resume(notices.KEY, {})
        self.assertEqual(self.rows(w, later), [])

    def test_a_stage_write_failure_lasts_until_no_write_is_pending(self):
        w = self.ws()
        w.db.upsert_item(notices.KEY)
        w.db.set_resume(notices.KEY, {"pending_stage": {"stage": "fix", "expected": "review"}})
        w.db.add_log(notices.KEY, "stage write refused: write refused", notices.NOW.isoformat(), kind=store.OP_STAGE_WRITE)
        later = notices.NOW + timedelta(minutes=5)
        self.recovered(w, later)
        self.assertEqual(len(self.rows(w, later)), 1)
        w.db.set_resume(notices.KEY, {})
        self.assertEqual(self.rows(w, later), [])

    def failure(self, message, kind=""):
        w = self.ws()
        w.db.upsert_item(notices.KEY)
        w.db.add_log(notices.KEY, message, notices.NOW.isoformat(), kind=kind)
        later = notices.NOW + timedelta(minutes=5)
        self.recovered(w, later)
        return len(self.rows(w, later))

    def test_a_refused_merge_is_not_cleared_by_an_unrelated_later_run(self):
        self.assertEqual(self.failure("merge refused: not ready", store.OP_MERGE_REFUSED), 1)

    def test_a_worker_run_failure_is_cleared_by_a_later_success(self):
        self.assertEqual(self.failure("worker fix attempt 1: failed", store.OP_WORKER_RUN), 0)

    def test_a_failure_with_no_recorded_operation_stays_unrecovered(self):
        self.assertEqual(self.failure("dispatch failed: no slot"), 1)

    def test_recovery_follows_the_recorded_operation_not_the_wording(self):
        # Worded like a stage write, recorded as a worker run: a later success recovers it.
        self.assertEqual(self.failure("stage write failed: label offline", store.OP_WORKER_RUN), 0)
        # Worded like a worker run, recorded as a stage write with none pending: it recovered.
        self.assertEqual(self.failure("worker fix attempt 1: failed", store.OP_STAGE_WRITE), 0)


if __name__ == "__main__":
    unittest.main()
