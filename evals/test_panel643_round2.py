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
import decision_actions as da
import notify
import pages
import reconcile

KEY, NOW = fixtures.KEY, fixtures.NOW


def current(db, row):
    """A decision's state: its identity's latest row (rows are append-only)."""
    return da.state_of(da.latest(db.decisions(row.issue_key))[da.identity(row)])


def decide(db, key, topic, question, answer=None, **record):
    return db.add_decision(key, topic, question, answer, "user", record)


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

    def test_a_reroute_appends_a_write_intent_and_the_page_server_does_nothing_else(self):
        dec = self.db.add_decision(KEY, "design", "Choose")
        fg = mock.Mock()
        self.assertEqual(self.post({"decision_id": dec.id, "action": "reroute", "target_stage": "implement"}, fg), 303)
        fg.write_stage.assert_not_called()
        saved = current(self.db, dec)
        self.assertNotEqual(saved.id, dec.id)  # appended, never updated in place
        self.assertEqual((saved.status, saved.rerouted_to, saved.intent), ("re-routed", "implement", "write"))
        self.assertEqual(self.db.item(KEY).stage, "review")
        self.assertIsNone(self.db.resume(KEY))  # the page server writes only its row

    def test_a_reverse_appends_a_run_intent_and_closes_nothing_else(self):
        held = decide(self.db, KEY, "design", "Choose A", "Back", status="blocked", blocked_reason="retry limit reached",
                      rerouted_to="implement", intent="run")
        answered = decide(self.db, KEY, "scope", "Choose B", "Yes", status="answered")
        self.assertEqual(self.post({"decision_id": answered.id, "action": "reverse", "answer": "No"}, mock.Mock()), 303)
        saved = current(self.db, answered)
        self.assertEqual((saved.status, saved.note, saved.intent), ("reversed", "No", "run"))
        self.assertTrue(saved.rerouted_to)
        # Choosing the one current intent is the engine's job; the page server leaves the blocked one as it was.
        self.assertEqual((current(self.db, held).status, current(self.db, held).intent), ("blocked", "run"))

    def test_a_settled_intent_cannot_be_re_routed_again(self):
        dec = self.db.add_decision(KEY, "design", "Choose")
        self.assertEqual(self.post({"decision_id": dec.id, "action": "reroute", "target_stage": "implement"}, mock.Mock()), 303)
        pending = self.db.decisions(KEY)[-1]
        self.assertEqual(current(self.db, pending).intent, "write")
        # An unapplied intent can still be replaced; once the engine has applied it, it is settled and cannot.
        settled = decide(self.db, KEY, "design", "Choose", pending.answer, status="re-routed", rerouted_to="implement")
        self.assertEqual(self.post({"decision_id": settled.id, "action": "reroute", "target_stage": "review"}, mock.Mock()), 409)
        self.assertEqual(self.post({"decision_id": settled.id, "action": "reverse", "answer": "No"}, mock.Mock()), 409)
        self.assertEqual(self.db.decisions(KEY)[-1].id, settled.id)

    def test_a_target_with_no_worker_is_refused_before_anything_is_written(self):
        dec = self.db.add_decision(KEY, "design", "Choose")
        fg = mock.Mock()
        self.assertEqual(self.post({"decision_id": dec.id, "action": "reroute", "target_stage": "merge"}, fg), 400)
        fg.write_stage.assert_not_called()
        self.assertEqual(current(self.db, dec).status, "open")
        self.assertEqual(len(self.db.decisions(KEY)), 1)
        self.assertNotIn("pending_stage", self.db.resume(KEY) or {})

    def test_comments_are_left_on_the_row_for_the_engine_not_sent_from_the_post(self):
        dec = self.db.add_decision(KEY, "design", "Choose")
        fg = mock.Mock()
        self.assertEqual(self.post({"decision_id": dec.id, "answer": "Yes"}, fg), 303)
        fg.comment.assert_not_called()
        queued = da.pending_comments(self.db.decisions(KEY), ())
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
        decide(w.db, notices.KEY, "design", "Back?", "Yes", status="answered", comment="Decision #1 answered")
        w.db.add_log(notices.KEY, "decision comment failed: offline", notices.NOW.isoformat(), kind=reconcile.OP_DECISION_COMMENT)
        later = notices.NOW + timedelta(minutes=5)
        self.recovered(w, later)
        self.assertEqual(len(self.rows(w, later)), 1)
        w.db.set_resume(notices.KEY, {"decision_comments_sent": ["Decision #1 answered"]})
        self.assertEqual(self.rows(w, later), [])

    def test_a_stage_write_failure_lasts_until_no_write_is_pending(self):
        w = self.ws()
        w.db.upsert_item(notices.KEY)
        w.db.set_resume(notices.KEY, {"pending_stage": {"stage": "fix", "expected": "review"}})
        w.db.add_log(notices.KEY, "stage write refused: write refused", notices.NOW.isoformat(), kind=reconcile.OP_STAGE_WRITE)
        later = notices.NOW + timedelta(minutes=5)
        self.recovered(w, later)
        self.assertEqual(len(self.rows(w, later)), 1)
        w.db.set_resume(notices.KEY, {})
        self.assertEqual(self.rows(w, later), [])

    def test_a_stage_write_failure_lasts_while_a_board_intent_still_has_its_write_to_do(self):
        w = self.ws()
        w.db.upsert_item(notices.KEY)
        w.db.add_log(notices.KEY, "stage write failed: offline", notices.NOW.isoformat(), kind=reconcile.OP_STAGE_WRITE)
        dec = decide(w.db, notices.KEY, "design", "Back?", "Back", status="re-routed", rerouted_to="fix", intent="write")
        later = notices.NOW + timedelta(minutes=5)
        self.recovered(w, later)
        self.assertEqual(len(self.rows(w, later)), 1)
        decide(w.db, notices.KEY, "design", "Back?", "Back", status="re-routed", rerouted_to="fix", intent="run",
               intent_id=dec.id)
        self.assertEqual(len(self.rows(w, later)), 1)  # a run intent whose stage is not written has its write to do too
        w.db.upsert_item(notices.KEY, stage="fix")
        self.assertEqual(self.rows(w, later), [])

    def failure(self, message, kind=""):
        w = self.ws()
        w.db.upsert_item(notices.KEY)
        w.db.add_log(notices.KEY, message, notices.NOW.isoformat(), kind=kind)
        later = notices.NOW + timedelta(minutes=5)
        self.recovered(w, later)
        return len(self.rows(w, later))

    def test_a_refused_merge_is_not_cleared_by_an_unrelated_later_run(self):
        self.assertEqual(self.failure("merge refused: not ready", reconcile.OP_MERGE_REFUSED), 1)

    def test_a_worker_run_failure_is_cleared_by_a_later_success(self):
        self.assertEqual(self.failure("worker fix attempt 1: failed", reconcile.OP_WORKER_RUN), 0)

    def test_a_failure_with_no_recorded_operation_stays_unrecovered(self):
        self.assertEqual(self.failure("dispatch failed: no slot"), 1)

    def test_unknown_operations_never_recover_through_a_later_worker_success(self):
        for message in ("ask failed: offline", "wait failed: offline", "unrecognized failed operation"):
            self.assertEqual(self.failure(message, "unknown"), 1)

    def test_recovery_follows_the_recorded_operation_not_the_wording(self):
        # Worded like a stage write, recorded as a worker run: a later success recovers it.
        self.assertEqual(self.failure("stage write failed: label offline", reconcile.OP_WORKER_RUN), 0)
        # Worded like a worker run, recorded as a stage write with none pending: it recovered.
        self.assertEqual(self.failure("worker fix attempt 1: failed", reconcile.OP_STAGE_WRITE), 0)


if __name__ == "__main__":
    unittest.main()
