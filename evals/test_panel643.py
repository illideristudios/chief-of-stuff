"""Regressions for the second M6 panel: real store, HTTP, and notification paths."""
import http.client
import sqlite3
import threading
import unittest
from datetime import timedelta
from types import SimpleNamespace
from unittest import mock
from urllib.parse import quote, urlencode

from evals import test_m6_store_board as fixtures
from evals import test_notify as notices
import decision_actions as da
import decision_escalation
import issue_page
import notify
import pages
import reconcile
import render_board
import source_page
import store
import store_access
import task_forge
import trio_gate
import workers_page

KEY, NOW = fixtures.KEY, fixtures.NOW
OTHER = "github:example/other#12"


def current(db, row):
    """A decision's state: its identity's latest row (rows are append-only)."""
    return da.state_of(decision_escalation.latest(db.decisions(row.issue_key))[decision_escalation.identity(row)])


def comments(db):
    return da.pending_comments(db.decisions(KEY), ())


class PanelTest(unittest.TestCase):
    def setUp(self):
        self.root, self.db = fixtures.make_store_workspace()
        self.addCleanup(self.db.engine.dispose)
        self.db.upsert_item(KEY, title="First repository", stage="review")

    def request(self, method, route, data=None):
        server = pages.make_server(self.root / "pages", 0, root=self.root, refresh=lambda *a: None, every=3600)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=10)
        conn.request(method, route, urlencode(data) if data else None)
        response = conn.getresponse()
        body = response.read().decode()
        conn.close()
        return response.status, body

    def test_blank_and_repeated_post_leave_decision_and_comment_queue_unchanged(self):
        dec = self.db.add_decision(KEY, "design", "Choose")
        fg = mock.Mock()
        fg.comment.return_value = ""
        with mock.patch.object(pages.forge, "for_workspace", return_value=fg):
            self.assertEqual(self.request("POST", "/", {"decision_id": dec.id, "answer": "  "})[0], 400)
            self.assertEqual(current(self.db, dec).status, "open")
            self.assertEqual(self.request("POST", "/", {"decision_id": dec.id, "answer": "Yes"})[0], 303)
            self.assertEqual(self.request("POST", "/", {"decision_id": dec.id, "answer": "No"})[0], 409)
        self.assertEqual(current(self.db, dec).answer, "Yes")
        self.assertEqual(len(comments(self.db)), 1)

    def test_refusals_are_navigable_and_preserve_input_without_replaying_stale_actions(self):
        dec = self.db.add_decision(KEY, "design", "Choose a direction")
        status, body = self.request("POST", "/", {"decision_id": dec.id, "answer": " <draft> ",
                                                   "action": "reroute", "target_stage": "invalid"})
        self.assertEqual(status, 400)
        self.assertIn('class="tabs"', body)
        self.assertIn('href="/"', body)
        self.assertIn('&lt;draft&gt;', body)
        self.assertNotIn('<draft>', body)
        self.assertIn('name="answer"', body)
        self.assertIn('name="target_stage"', body)
        self.assertEqual(len(self.db.decisions(KEY)), 1)
        self.assertEqual(self.request("POST", "/", {"decision_id": dec.id, "answer": "Accepted"})[0], 303)
        status, body = self.request("POST", "/", {"decision_id": dec.id, "answer": "My draft"})
        self.assertEqual(status, 409)
        self.assertIn("Choose a direction", body)
        self.assertIn("answered", body.lower())
        self.assertIn("My draft", body)
        self.assertIn('href="/decisions/', body)
        self.assertNotIn('method="post"', body)
        self.assertEqual(len(self.db.decisions(KEY)), 2)

    def test_invalid_answer_can_be_corrected_and_retried(self):
        dec = self.db.add_decision(KEY, "design", "Choose")
        status, body = self.request("POST", "/", {"decision_id": dec.id, "answer": "  ", "action": "answer"})
        self.assertEqual(status, 400)
        self.assertIn('name="action" value="answer"', body)
        self.assertEqual(self.request("POST", "/", {"decision_id": dec.id, "answer": "Corrected", "action": "answer"})[0], 303)
        self.assertEqual(current(self.db, dec).answer, "Corrected")

    def test_missing_decision_keeps_answer_and_unknown_issues_offer_navigation(self):
        status, body = self.request("POST", "/", {"decision_id": 99999999, "answer": "draft"})
        self.assertEqual(status, 404)
        self.assertIn("draft", body)
        self.assertIn('href="/"', body)
        for route in ("/issues/99999", "/issues/foo"):
            status, body = self.request("GET", route)
            self.assertEqual(status, 404)
            self.assertIn('class="tabs"', body)
            self.assertIn('href="/workers"', body)
            self.assertIn('href="/"', body)

    def test_reasked_decision_reports_latest_open_state_and_escapes_it(self):
        dec = self.db.add_decision(KEY, "design", "Old question")
        self.db.add_decision(KEY, "design", "Old question")
        status, body = self.request("POST", "/", {"decision_id": dec.id, "answer": "draft"})
        self.assertEqual(status, 409)
        self.assertIn("Old question", body)
        self.assertIn("Current status: <b>open</b>", body)
        self.assertIn("draft", body)
        self.assertNotIn('method="post"', body)

    def test_simultaneous_answers_have_one_winner_and_one_queued_comment(self):
        dec = self.db.add_decision(KEY, "design", "Choose")
        fg = mock.Mock()
        fg.comment.return_value = ""
        server = pages.make_server(self.root / "pages", 0, root=self.root, refresh=lambda *a: None, every=3600)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        barrier = threading.Barrier(2)
        results = []
        def submit(text):
            conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=10)
            barrier.wait()
            conn.request("POST", "/", urlencode({"decision_id": dec.id, "answer": text}))
            resp = conn.getresponse()
            results.append(resp.status)
            resp.read()
            conn.close()
        with mock.patch.object(pages.forge, "for_workspace", return_value=fg):
            threads = [threading.Thread(target=submit, args=(text,)) for text in ("Yes", "No")]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(15)
        self.assertEqual(sorted(results), [303, 409])
        self.assertEqual(len(comments(self.db)), 1)

    def test_reverse_at_merge_records_the_stage_that_did_the_work(self):
        self.db.upsert_item(KEY, stage="merge")
        self.db.record_segment(KEY, "implement", 1, (NOW - timedelta(hours=2)).isoformat(),
                               (NOW - timedelta(hours=1)).isoformat(), "done")
        dec = self.db.add_decision(KEY, "scope", "Choose", answer="Yes", decided_by="user")
        fg = mock.Mock()
        fg.comment.return_value = ""
        with mock.patch.object(pages.forge, "for_workspace", return_value=fg):
            status, _ = self.request("POST", "/", {"decision_id": dec.id, "action": "reverse", "answer": "No"})
        self.assertEqual(status, 303)
        saved = current(self.db, dec)
        self.assertEqual((saved.status, saved.rerouted_to, saved.intent), ("reversed", "implement", "run"))
        # The reversal is appended on the decision's own identity, in develop's reversal shape.
        self.assertEqual(len(self.db.decisions(KEY)), 2)
        self.assertTrue(decision_escalation.is_reversed(self.db.decisions(KEY)[-1]))

    def test_record_update_supersedes_old_resume_status(self):
        dec = self.db.add_decision(KEY, "design", "Choose")
        self.db.set_resume(KEY, {"decisions": {str(dec.id): {"status": "blocked", "blocked_reason": "old"}}})
        trio_gate.record(self.db, KEY, da.answer(self.db.decision(dec.id), "Yes").update)
        self.assertEqual(current(self.db, dec).status, "answered")
        self.assertEqual(current(self.db, dec).blocked_reason, "")

    def test_reroute_survives_comment_failure_and_persists_its_intent(self):
        dec = self.db.add_decision(KEY, "design", "Choose")
        fg = mock.Mock()
        fg.write_stage.return_value = SimpleNamespace(ok=True, done=True, error="")
        fg.comment.return_value = "offline"
        data = {"decision_id": dec.id, "action": "reroute", "target_stage": "implement"}
        with mock.patch.object(pages.forge, "for_workspace", return_value=fg):
            self.assertEqual(self.request("POST", "/", data)[0], 303)
            self.assertEqual(self.request("POST", "/", data)[0], 409)
        fg.write_stage.assert_not_called()
        # The re-route is the decision's newer row, so the replay of the older form is refused.
        rerouted = current(self.db, dec)
        self.assertEqual((rerouted.status, len(self.db.decisions(KEY))), ("re-routed", 2))
        self.assertEqual((rerouted.rerouted_to, rerouted.intent), ("implement", "write"))

    def test_store_busy_is_an_http_error(self):
        from sqlalchemy.exc import OperationalError
        with mock.patch.object(store_access, "migrated_store", side_effect=OperationalError("busy", {}, Exception())):
            self.assertEqual(self.request("POST", "/", {"decision_id": 1, "answer": "Yes"})[0], 503)

    def test_full_keys_have_distinct_issue_and_source_routes(self):
        self.db.upsert_item(OTHER, title="Other repository", stage="implement")
        from dataclasses import replace
        from backlog_ref import GitHubBacklog
        fixtures.bs._write(self.root / "pages" / fixtures.bs.CACHE, replace(
            fixtures.bs.EMPTY, home=GitHubBacklog("example/repo"), issues={
                "#12": fixtures.bs.Issue("#12", "https://github.com/example/repo/issues/12", "First repository", "open", (), body="First source proof"),
                "example/other#12": fixtures.bs.Issue("example/other#12", "https://github.com/example/other/issues/12", "Other repository", "open", (), body="Other source proof"),
            }))
        for suffix in ("", "/source"):
            for key, title, absent in ((KEY, "First repository", "Other repository"),
                                       (OTHER, "Other repository", "First repository")):
                status, body = self.request("GET", f"/issues/{quote(key, safe='')}{suffix}")
                self.assertEqual(status, 200)
                self.assertIn(title, body)
                self.assertNotIn(absent, body)
                if suffix:
                    self.assertIn("First source proof" if key == KEY else "Other source proof", body)

    def test_closed_holds_are_ignored(self):
        self.db.upsert_item(OTHER, title="Closed repository", forge_state="closed", human_review=True)
        self.db.add_decision(OTHER, "design", "Closed question")
        cfg = fixtures.parse_coordinator(fixtures.CLAUDE_MD, today=NOW.date())
        _, holds, _, _ = render_board._tasks_from_store(self.db, cfg, {}, None, NOW.date())
        self.assertFalse(holds)

    def test_store_items_are_visible_with_real_tracker_rows(self):
        from tracker import parse_tracker
        tracker = "## Tasks\n\n| Item | Owner | State | Since | Due | Checklist |\n|---|---|---|---|---|---|\n| Legacy | unassigned | open | | | |\n"
        self.assertEqual(len(parse_tracker(tracker).tasks), 1)
        cfg = fixtures.parse_coordinator(fixtures.CLAUDE_MD, today=NOW.date())
        body = render_board.render(tracker, cfg, NOW, db=task_forge.page_data(self.db))
        self.assertIn("First repository", body)

    def test_issue_timeline_and_workers_use_store_segments_and_logs(self):
        self.db.record_segment(KEY, "implement", 1, (NOW - timedelta(hours=1)).isoformat(),
                               NOW.isoformat(), "done", runner="engine-run")
        self.db.add_log(KEY, "Engine finished parsing", NOW.isoformat())
        data = task_forge.page_data(self.db)
        body = issue_page.render(12, [], fixtures.bs.EMPTY, NOW, db=data)
        self.assertIn("engine-run", body)
        self.assertIn("Engine finished parsing", body)
        self.assertNotIn("No workers launched", body)

    def test_workers_accept_naive_and_malformed_timestamps(self):
        for n, stamp in enumerate(("bad", NOW.replace(tzinfo=None).isoformat()), 1):
            self.db.record_segment(KEY, "review", n, stamp, runner=f"run-{n}")
        body = workers_page.render("", fixtures.bs.EMPTY, NOW, self.root / "trees", 4, db=self.db)
        self.assertIn("run-1", body)
        self.assertIn("run-2", body)

    def test_store_reads_do_not_take_writer_lock(self):
        # A WAL reader must remain usable while another connection holds RESERVED.
        self.db.engine.dispose()
        from sqlalchemy import event
        event.listen(self.db.engine, "connect", lambda conn, _: conn.execute("PRAGMA busy_timeout=1"))
        with sqlite3.connect(self.root / "store.db") as writer:
            writer.execute("BEGIN IMMEDIATE")
            self.assertEqual(len(self.db.decisions()), 0)
            self.assertEqual(len(self.db.items()), 1)

    def test_store_navigation_counts_from_open_rows_only(self):
        self.db.record_segment(KEY, "review", 1, NOW.isoformat(), runner="running")
        self.db.add_decision(KEY, "design", "Choose")
        self.db.upsert_item(OTHER, forge_state="closed", human_review=True)
        counts = pages._navigation(self.root, self.root / "pages", task_forge.page_data(self.db))
        self.assertEqual(counts["workers"], 0)
        self.assertEqual(counts["dispatch"], 0)
        self.assertEqual(counts["decisions"], 1)
        self.assertNotIn("since", counts)
        self.db.upsert_item("github:example/repo#13", title="Ready", stage="implement")
        self.db.upsert_item("github:example/repo#14", title="Human hold", human_review=True)
        counts = pages._navigation(self.root, self.root / "pages", task_forge.page_data(self.db))
        self.assertEqual((counts["workers"], counts["dispatch"], counts["decisions"]), (1, 1, 1))



class NotificationPanelTest(unittest.TestCase):
    ws = notices.NotifyTest.ws
    def rows(self, w, now=notices.NOW):
        cfg, settings = notify.configuration(w.root)
        return notify.store_rows(w.root, cfg, settings, now)

    def test_links_encode_fragment(self):
        w = self.ws()
        cfg, settings = notify.configuration(w.root)
        self.assertIn("%23", notify.board_item_url(w.root, settings, notices.KEY))

    def test_human_hold_identity_is_stable_across_forge_refresh(self):
        w = self.ws()
        w.db.upsert_item(notices.KEY, title="Choose", human_review=True)
        before = self.rows(w)
        later = notices.NOW + timedelta(minutes=2)
        w.db.clock = lambda: later
        w.db.sync_from_forge([{"issue_key": notices.KEY, "human_review": True}])
        self.assertEqual([r.title for r in before], [r.title for r in self.rows(w, later)])

    def test_retry_stop_is_notified_and_a_recovered_failure_is_not(self):
        w = self.ws()
        w.db.upsert_item(notices.KEY)
        w.db.add_log(notices.KEY, "escalate: retry limit reached for fix", notices.NOW.isoformat(), kind=reconcile.OP_WORKER_RUN)
        self.assertEqual(len(self.rows(w)), 1)
        w.db.record_segment(notices.KEY, "fix", 1, notices.NOW.isoformat(), notices.NOW.isoformat(), "failed")
        later = notices.NOW + timedelta(minutes=5)
        w.db.record_segment(notices.KEY, "fix", 2, later.isoformat(), later.isoformat(), "done")
        self.assertEqual(self.rows(w, later), [])
