"""The engine's heartbeat (#1179): one status line, one summary, and the report, delivered through sinks."""

import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import engine
import heartbeat
import heartbeat_page
import heartbeat_store
import store
from heartbeat import Facts

NOW = datetime(2000, 1, 1, 12, 0, tzinfo=timezone.utc)
TICK = NOW - timedelta(seconds=30)
LIMIT = timedelta(minutes=45)
LINK = "t3code://threads/env-1/thread-9"
A, B, C, D, E, F, G = (f"github:example/project#{n}" for n in range(1, 8))


def facts(**kwargs):
    return Facts(**{"key": A, "title": "Work", **kwargs})


def states(found):
    return {entry.key: entry.state for entry in found.entries}


class StatusTest(unittest.TestCase):
    def status(self, rows, progress=None):
        return heartbeat.status(rows, NOW, TICK, progress or {}, LIMIT)

    def test_each_item_is_in_exactly_one_state(self):
        rows = [facts(key=A, done=True), facts(key=B, done=True, trio_decided=True),
                facts(key=C, park="park: two failures on this issue"), facts(key=D, split_trio=True),
                facts(key=E, park="needs the architect"), facts(key=F, started_at=NOW - timedelta(minutes=5)),
                facts(key=G, started_at=NOW - timedelta(hours=2))]
        found = self.status(rows, {F: NOW - timedelta(minutes=1), G: NOW - timedelta(hours=1)})
        self.assertEqual(states(found), {A: "done", B: "assumed", C: "parked", D: "parked", E: "parked",
                                         F: "running", G: "stalled"})

    def test_a_park_names_its_reason(self):
        found = self.status([facts(key=A, park="park: two failures on this issue"), facts(key=B, split_trio=True),
                             facts(key=C, park="park: three fix rounds ended with findings still open"),
                             facts(key=D, park="architect consultation is required")])
        self.assertEqual({e.key: e.why for e in found.entries},
                         {A: "two failures", B: "split trio", C: "failures", D: "architectural decision"})

    def test_a_worker_with_no_known_progress_is_not_stalled(self):
        found = self.status([facts(started_at=NOW - timedelta(hours=9))])
        self.assertEqual(states(found), {A: "running"})

    def test_done_wins_over_a_stale_park_and_running_over_a_park(self):
        found = self.status([facts(key=A, done=True, park="park: two failures on this issue"),
                             facts(key=B, park="park: two failures on this issue", started_at=NOW)])
        self.assertEqual(states(found), {A: "done", B: "running"})

    def test_an_item_with_nothing_to_report_is_left_out(self):
        self.assertEqual(self.status([facts()]).entries, ())

    def test_the_line_is_short_and_carries_the_counts_and_the_tick(self):
        found = self.status([facts(key=A, done=True), facts(key=B, done=True, trio_decided=True),
                             facts(key=C, split_trio=True), facts(key=D, started_at=NOW)])
        self.assertEqual(found.line, "chief · 1 running · 0 stalled · 1 parked · 1 done · 1 assumed · tick 11:59")
        self.assertLess(len(found.line), 80)

    def test_the_line_without_a_tick_says_so(self):
        self.assertTrue(heartbeat.status([], NOW, None, {}, LIMIT).line.endswith("no tick yet"))

    def test_the_summary_lists_the_parks_and_stalls_and_the_tick_age(self):
        found = self.status([facts(key=A, split_trio=True), facts(key=B, park="park: two failures on this issue"),
                             facts(key=C, started_at=NOW - timedelta(hours=2))], {C: NOW - timedelta(hours=1)})
        text = found.summary
        for part in ("1 stalled", "2 parked", "split trio", "two failures", C, "last tick 30s ago"):
            self.assertIn(part, text)

    def test_a_naive_clock_is_refused(self):
        with self.assertRaises(ValueError):
            heartbeat.status([], datetime(2000, 1, 1), None, {}, LIMIT)


class Recorder:
    def __init__(self, name, fail=False):
        self.name, self.fail, self.seen = name, fail, []

    def deliver(self, found):
        if self.fail:
            raise OSError("the T3 title could not be set")
        self.seen.append(found.line)


class DeliverTest(unittest.TestCase):
    def test_a_failing_sink_is_reported_and_the_others_still_run(self):
        title, board = Recorder("title", fail=True), Recorder("board")
        found = heartbeat.status([], NOW, TICK, {}, LIMIT)
        failures = heartbeat.deliver((title, board), found)
        self.assertEqual(board.seen, [found.line])
        self.assertEqual(failures, ["title: the T3 title could not be set"])

    def test_all_sinks_well_means_no_failures(self):
        self.assertEqual(heartbeat.deliver((Recorder("board"),), heartbeat.status([], NOW, TICK, {}, LIMIT)), [])


class StoreTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        path = self.root / "store.db"
        store.migrate(path)
        self.db = store.Store(path, clock=lambda: NOW)
        self.addCleanup(self.db.engine.dispose)

    def rows(self):
        return {f.key: f for f in heartbeat_store.facts(self.db)}

    def test_done_is_closed_and_merged(self):
        self.db.upsert_item(A, forge_state="closed", merge_state="merged")
        self.db.upsert_item(B, forge_state="closed", merge_state="")
        self.assertEqual([k for k, f in self.rows().items() if f.done], [A])

    def test_a_trio_answer_makes_the_work_assumed(self):
        self.db.upsert_item(A, forge_state="closed", merge_state="merged")
        self.db.add_decision(A, "naming", "Which name?", answer="the short one", decided_by="trio")
        self.db.upsert_item(B, forge_state="closed", merge_state="merged")
        self.db.add_decision(B, "naming", "Which name?", answer="the short one", decided_by="user")
        self.assertEqual({k: f.trio_decided for k, f in self.rows().items()}, {A: True, B: False})

    def test_an_unanswered_owner_decision_from_a_split_trio_is_a_split(self):
        self.db.upsert_item(A)
        self.db.add_decision(A, "naming", "Which name?", decided_by="owner", record={"unanimous": False})
        self.db.upsert_item(B)
        self.db.add_decision(B, "naming", "Which name?", decided_by="owner", record={"unanimous": True})
        self.db.upsert_item(C)
        self.db.add_decision(C, "naming", "Which name?", decided_by="trio", record={"unanimous": False})
        self.db.upsert_item(D)
        self.db.add_decision(D, "naming", "Which name?", decided_by="owner", answer="Use the short name",
                             record={"unanimous": False})
        self.assertEqual({k: f.split_trio for k, f in self.rows().items()}, {A: True, B: False, C: False, D: False})

    def test_the_park_reason_and_the_open_segment_come_from_the_store(self):
        self.db.upsert_item(A)
        self.db.set_resume(A, {"park": {"reason": "park: two failures on this issue"}})
        self.db.upsert_item(B)
        self.db.record_segment(B, "implement", 1, started_at=NOW.isoformat())
        rows = self.rows()
        self.assertEqual(rows[A].park, "park: two failures on this issue")
        self.assertEqual(rows[B].started_at, NOW)

    def test_the_latest_thread_link_comes_from_the_run_record(self):
        self.db.upsert_item(A)
        self.db.set_resume(A, {"runs": {"implement-1": {"thread_link": "t3code://threads/env-1/old"},
                                        "implement-2": {"thread_link": LINK}}})
        self.db.upsert_item(B)
        self.db.set_resume(B, {"runs": {"implement-1": {"thread_link": "javascript:alert(1)"}}})
        self.db.upsert_item(C)
        self.assertEqual({k: f.thread_link for k, f in self.rows().items()}, {A: LINK, B: "", C: ""})


class SinkTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.found = heartbeat.status([facts(key=A, done=True, thread_link=LINK)], NOW, TICK, {}, LIMIT)

    def test_the_file_sink_writes_the_status_for_the_page_server(self):
        heartbeat_store.FileSink(self.root).deliver(self.found)
        saved = json.loads((self.root / ".chief-of-stuff" / "heartbeat.json").read_text())
        self.assertEqual(heartbeat_store.read(self.root), saved)
        self.assertEqual(saved["line"], self.found.line)
        self.assertEqual(saved["summary"], self.found.summary)
        self.assertEqual(saved["entries"], [{"key": A, "title": "Work", "state": "done", "why": "", "link": LINK}])

    def test_beat_delivers_through_every_sink_and_returns_the_failures(self):
        db_path = self.root / "store.db"
        store.migrate(db_path)
        db = store.Store(db_path, clock=lambda: NOW)
        self.addCleanup(db.engine.dispose)
        db.upsert_item(A, forge_state="closed", merge_state="merged")
        bad = Recorder("title", fail=True)
        failures = heartbeat_store.beat(db, NOW, TICK, {}, LIMIT, sinks=(bad, heartbeat_store.FileSink(self.root)))
        self.assertEqual(failures, ["title: the T3 title could not be set"])
        saved = json.loads((self.root / ".chief-of-stuff" / "heartbeat.json").read_text())
        self.assertEqual([e["state"] for e in saved["entries"]], ["done"])


class EngineBeatTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        store.migrate(self.root / "store.db")
        self.db = store.Store(self.root / "store.db", clock=lambda: NOW)
        self.addCleanup(self.db.engine.dispose)
        self.db.upsert_item(A, forge_state="closed", merge_state="merged")
        self.workers = SimpleNamespace(stall=SimpleNamespace(after_minutes=45))

    def beat(self, sinks):
        with mock.patch.object(engine.engine_facts, "gather", return_value=SimpleNamespace(progress=())), \
                mock.patch.object(heartbeat_store, "sinks", return_value=sinks):
            engine._beat(self.db, None, self.root, self.root, self.workers, TICK)

    def test_a_pass_delivers_the_heartbeat_to_the_sinks(self):
        sink = Recorder("board")
        self.beat((sink,))
        self.assertEqual(len(sink.seen), 1)

    def test_a_failing_sink_is_logged_and_the_engine_goes_on(self):
        good = Recorder("board")
        self.beat((Recorder("title", fail=True), good))
        self.assertEqual(len(good.seen), 1)
        self.assertEqual([(e.kind, e.message) for e in self.db.engine_log()],
                         [("heartbeat", "title: the T3 title could not be set")])

    def test_a_failure_to_gather_is_logged_not_raised(self):
        with mock.patch.object(engine.engine_facts, "gather", side_effect=OSError("no git")):
            engine._beat(self.db, None, self.root, self.root, self.workers, TICK)
        self.assertEqual([e.message for e in self.db.engine_log()], ["heartbeat: no git"])


class PageTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.pages = self.root / "pages"
        self.pages.mkdir()

    def render(self, rows):
        found = heartbeat.status(rows, NOW, TICK, {}, LIMIT)
        heartbeat_store.FileSink(self.root).deliver(found)
        return heartbeat_page.render(heartbeat_store.read(self.root))

    def test_the_report_has_the_three_sections_and_links_each_item(self):
        page = self.render([facts(key=A, title="Done one", done=True, thread_link=LINK),
                            facts(key=B, title="Assumed one", done=True, trio_decided=True),
                            facts(key=C, title="Parked one", split_trio=True)])
        for heading in ("Done", "Done on assumptions", "Parked"):
            self.assertIn(f"<h2>{heading}", page)
        self.assertIn(f'href="{LINK}"', page)
        self.assertIn('href="/issues/', page)  # no thread: the issue page
        self.assertIn("split trio", page)

    def test_a_title_is_escaped(self):
        page = self.render([facts(title="<script>alert(1)</script>", done=True)])
        self.assertNotIn("<script>alert", page)

    def test_no_heartbeat_yet_renders_an_empty_report(self):
        self.assertIn("No heartbeat yet", heartbeat_page.render(heartbeat_store.read(self.root)))

    def test_write_puts_report_html_in_the_pages_directory(self):
        self.render([facts(done=True)])
        out = heartbeat_page.write(self.root, self.pages)
        self.assertEqual(out, self.pages / "report.html")
        self.assertIn("Done on assumptions", out.read_text())


if __name__ == "__main__":
    unittest.main()
