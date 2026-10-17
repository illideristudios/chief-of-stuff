"""M6 store-backed board, Gantt, decisions on the board, issue/source/workers pages, and lead counts (#643)."""

from __future__ import annotations

import http.client
import re
import sys
import tempfile
import threading
import unittest
import urllib.parse
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "scripts"), str(ROOT / "evals")]

import board_sources as bs  # noqa: E402
import flow_chart as fc  # noqa: E402
import issue_page  # noqa: E402
import pages as pg  # noqa: E402
import render_board as rb  # noqa: E402
import settings as st  # noqa: E402
import source_page  # noqa: E402
import store  # noqa: E402
import store_access  # noqa: E402
import task_forge  # noqa: E402
import workers_page  # noqa: E402
from workspace import parse_coordinator  # noqa: E402

ZONE = ZoneInfo("America/Chicago")
NOW = datetime(2026, 10, 9, 14, 0, tzinfo=ZONE)
KEY = "github:example/repo#12"
KEY2 = "github:example/repo#15"

CLAUDE_MD = """# Workspace

## Coordinator
- User: Robin
- Daily log dir: `daily/`
- Tracker: `daily/<date>-tracker.md`
- Timezone: America/Chicago
- Board: self-hosted; URL http://127.0.0.1:8765/; dir `pages/`
- Settings: `cos.toml`
"""

COS_TOML = """[lanes]
build = { stages = ["implement", "review", "fix", "merge"] }

[kanban]
stages = ["In progress", "In review", "Fixing", "Merging"]
human_review_label = "Needs input"

[kanban.map]
implement = 0
review = 1
fix = 2
merge = 3

[workers]
max_concurrency = 4
"""


def make_store_workspace() -> tuple[Path, store.Store]:
    root = Path(tempfile.mkdtemp()).resolve()
    (root / "CLAUDE.md").write_text(CLAUDE_MD, encoding="utf-8")
    (root / "cos.toml").write_text(COS_TOML, encoding="utf-8")
    (root / "pages").mkdir()
    (root / "trees").mkdir()
    db_path = root / "store.db"
    store.migrate(db_path)
    db = store.Store(db_path, clock=lambda: NOW)
    return root, db


class StoreGanttAndBoardTest(unittest.TestCase):
    """M6 item 1 & Exit S2: Gantt and board read from the store and forge adapter; rework on one issue shows one
    line with several (issue, stage, attempt) segments."""

    def setUp(self) -> None:
        self.root, self.db = make_store_workspace()
        class FrozenDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                return NOW.astimezone(tz)
        frozen = mock.patch.object(workers_page, "datetime", FrozenDatetime)
        frozen.start()
        self.addCleanup(frozen.stop)
        self.addCleanup(self.db.engine.dispose)
        self.db.upsert_item(KEY, workflow="build", stage="review", forge_state="open", title="Upload size limit")
        self.db.upsert_item(KEY2, workflow="build", stage="fix", forge_state="open", title="Cache warmup")
        # Rework on KEY across multiple stage attempts on the same day:
        t0 = NOW - timedelta(hours=5)
        self.db.record_segment(
            KEY, "implement", 1,
            started_at=t0.isoformat(),
            ended_at=(t0 + timedelta(hours=1)).isoformat(),
            outcome="done",
            runtime="claude", model="opus", agent_type="implementer", runner="impl-a1",
        )
        self.db.record_segment(
            KEY, "review", 1,
            started_at=(t0 + timedelta(hours=1)).isoformat(),
            ended_at=(t0 + timedelta(hours=2)).isoformat(),
            outcome="done",
            runtime="codex", model="gpt-5.4", agent_type="reviewer", runner="rev-a1",
        )
        self.db.record_segment(
            KEY, "fix", 1,
            started_at=(t0 + timedelta(hours=2)).isoformat(),
            ended_at=(t0 + timedelta(hours=3)).isoformat(),
            outcome="done",
            runtime="claude", model="opus", agent_type="implementer", runner="fix-a1",
        )
        self.db.record_segment(
            KEY, "review", 2,
            started_at=(t0 + timedelta(hours=3)).isoformat(),
            ended_at=None,
            outcome=None,
            runtime="codex", model="gpt-5.4", agent_type="reviewer", runner="rev-a2",
        )
        # KEY2 enters fix twice (looping):
        self.db.record_segment(
            KEY2, "implement", 1,
            started_at=(t0 + timedelta(minutes=30)).isoformat(),
            ended_at=(t0 + timedelta(hours=1, minutes=30)).isoformat(),
            outcome="done",
            runtime="claude", model="sonnet", runner="impl-b1",
        )
        self.db.record_segment(
            KEY2, "fix", 1,
            started_at=(t0 + timedelta(hours=1, minutes=30)).isoformat(),
            ended_at=(t0 + timedelta(hours=2, minutes=15)).isoformat(),
            outcome="done",
            runtime="claude", model="sonnet", runner="fix-b1",
        )
        self.db.record_segment(
            KEY2, "review", 1,
            started_at=(t0 + timedelta(hours=2, minutes=15)).isoformat(),
            ended_at=(t0 + timedelta(hours=3)).isoformat(),
            outcome="done",
            runtime="claude", model="sonnet", runner="rev-b1",
        )
        self.db.record_segment(
            KEY2, "fix", 2,
            started_at=(t0 + timedelta(hours=3)).isoformat(),
            ended_at=None,
            outcome=None,
            runtime="claude", model="sonnet", runner="fix-b2",
        )

    def test_rework_on_one_issue_shows_one_gantt_row_with_several_segments(self) -> None:
        rows = fc.from_store(self.db, NOW - timedelta(hours=24), NOW)
        by_key = [r for r in rows if KEY in r.href or "Upload size limit" in r.label]
        self.assertEqual(len(by_key), 1, f"expected one Gantt row for {KEY}, got {by_key}")
        row = by_key[0]
        self.assertEqual([s.category for s in row.segments], ["implement", "review", "fix", "review"])
        self.assertEqual([s.kind for s in row.segments], ["done", "done", "done", "running"])
        self.assertIn(f"/issues/{urllib.parse.quote(KEY, safe="")}", row.href)

    def test_board_write_renders_from_store_without_any_tracker_file(self) -> None:
        self.db.add_decision(KEY2, "design", "Keep cache warmup pool?")
        sources = replace(
            bs.EMPTY,
            changes={
                "!21": bs.Change("!21", "https://github.com/example/repo/pull/21", "Upload size limit", "open",
                                 False, "passed", True, 1, None, "main", ("src/upload.py",), (KEY, "#12")),
                "!22": bs.Change("!22", "https://github.com/example/repo/pull/22", "Cache warmup", "open",
                                 False, "passed", False, 0, None, "main", ("src/upload.py",), (KEY2, "#15")),
            },
        )
        bs._write(self.root / "pages" / bs.CACHE, sources)
        board_path, *_ = pg.write_board(self.root, now=NOW)
        html = board_path.read_text(encoding="utf-8")
        # Full issue key routes on the board:
        self.assertIn(f'href="/issues/{urllib.parse.quote(KEY, safe="")}"', html)
        self.assertIn(f'href="/issues/{urllib.parse.quote(KEY2, safe="")}"', html)
        # Rework issue appears once in Build and has its multi-segment Gantt bars in Flow:
        self.assertIn("Upload size limit", html)
        self.assertIn("Flow · 24 hours", html)
        # Card marks from store & forge: queue 1, shares file with !22, decision pending, looping:
        self.assertIn("queue 1", html)
        self.assertIn("shares file with !22", html)
        self.assertIn("decision pending", html)
        self.assertIn("looping", html)


class StoreDecisionsOnBoardAndPagesTest(unittest.TestCase):
    """M6 item 2 & #288: decisions as board items; answer, reverse, re-route POSTs via pages.py; blocked never closed."""

    def setUp(self) -> None:
        self.root, self.db = make_store_workspace()
        class FrozenDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                return NOW.astimezone(tz)
        frozen = mock.patch.object(workers_page, "datetime", FrozenDatetime)
        frozen.start()
        self.addCleanup(frozen.stop)
        self.addCleanup(self.db.engine.dispose)
        self.db.upsert_item(KEY, workflow="build", stage="review", forge_state="open", title="Upload size limit")
        self.db.upsert_item(KEY2, workflow="build", stage="implement", forge_state="open", title="Cache warmup")
        self.d_open = self.db.add_decision(KEY, "design", "Split upload parser?")
        self.d_blocked = self.db.add_decision(KEY2, "plan", "Use Redis for cache?")
        self.db.answer_decision(self.d_blocked.id, "Yes, use Redis", decided_by="user")
        self.db.answer_decision(self.d_blocked.id, "Yes, use Redis", status="blocked", blocked_reason="merge conflicts")

    def test_board_renders_open_and_blocked_decisions_and_blocked_is_never_closed(self) -> None:
        board_path, *_ = pg.write_board(self.root, now=NOW)
        html = board_path.read_text(encoding="utf-8")
        self.assertIn("Split upload parser?", html)
        self.assertIn("Use Redis for cache?", html)
        self.assertIn("merge conflicts", html)
        # Blocked decision must show as BLOCKED, never as CLOSED:
        self.assertRegex(html, r"(?i)blocked.*merge conflicts|merge conflicts.*blocked")
        self.assertNotRegex(html, r"(?i)>\s*closed\s*<")

    def test_pages_post_answer_reverse_and_reroute_write_store_and_forge(self) -> None:
        fake_forge = mock.MagicMock()
        fake_forge.write_stage.return_value = SimpleNamespace(ok=True, done=True, error="")
        fake_forge.comment.return_value = ""
        server = pg.make_server(self.root / "pages", 0, root=self.root, refresh=lambda r, n: None, every=3600)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        self.addCleanup(server.server_close)
        port = server.server_address[1]

        def post(path: str, data: dict[str, str]) -> http.client.HTTPResponse:
            body = urllib.parse.urlencode(data).encode("utf-8")
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
            conn.request("POST", path, body=body, headers={"Content-Type": "application/x-www-form-urlencoded"})
            resp = conn.getresponse()
            resp.read()
            conn.close()
            return resp

        with mock.patch.object(pg.forge, "for_workspace", return_value=fake_forge):
            # 1. Answer the open decision:
            resp = post("/", {"decision_id": str(self.d_open.id), "action": "answer", "answer": "Split into two"})
            self.assertEqual(resp.status, 303)
            d1 = next(d for d in self.db.decisions(KEY) if d.id == self.d_open.id)
            self.assertEqual(d1.status, "answered")
            self.assertEqual(d1.answer, "Split into two")
            self.assertEqual(d1.decided_by, "user")

            # 2. Reverse the answered decision:
            resp = post("/", {"decision_id": str(self.d_open.id), "action": "reverse", "answer": "Keep single pass"})
            self.assertEqual(resp.status, 303)
            d2 = next(d for d in self.db.decisions(KEY) if d.id == self.d_open.id)
            self.assertEqual(d2.status, "reversed")
            self.assertEqual(d2.answer, "Keep single pass")

            # 3. Re-route another decision to another stage:
            reroute_dec = self.db.add_decision(KEY, "plan", "Return to implementation?")
            resp = post("/", {
                "decision_id": str(reroute_dec.id),
                "action": "reroute",
                "target_stage": "implement",
                "answer": "Back to implement",
            })
            self.assertEqual(resp.status, 303)
            d3 = next(d for d in self.db.decisions(KEY) if d.id == reroute_dec.id)
            self.assertEqual(d3.status, "re-routed")
            self.assertEqual(self.db.item(KEY).stage, "implement")
            fake_forge.write_stage.assert_called_once_with(12, "implement", expected="review", commit=True)
            fake_forge.comment.assert_not_called()
            self.assertEqual(len(self.db.resume(KEY)["decision_comments"]), 3)


class StorePagesAndRoutesTest(unittest.TestCase):
    """M6 item 1: issue_page, source_page, workers_page, and pages.py serve from the store with full issue keys."""

    def setUp(self) -> None:
        self.root, self.db = make_store_workspace()
        class FrozenDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                return NOW.astimezone(tz)
        frozen = mock.patch.object(workers_page, "datetime", FrozenDatetime)
        frozen.start()
        self.addCleanup(frozen.stop)
        self.addCleanup(self.db.engine.dispose)
        self.db.upsert_item(KEY, workflow="build", stage="implement", forge_state="open", title="Upload size limit")
        t0 = NOW - timedelta(minutes=25)
        self.db.record_segment(
            KEY, "implement", 1,
            started_at=t0.isoformat(),
            ended_at=None,
            outcome=None,
            runtime="claude", model="opus", agent_type="implementer", runner="impl-a1",
        )
        self.db.upsert_item(KEY2, workflow="build", stage="review", forge_state="open", title="Cache warmup", human_review=True)
        self.db.record_segment(
            KEY2, "review", 1,
            started_at=(NOW - timedelta(minutes=50)).isoformat(),
            ended_at=(NOW - timedelta(minutes=10)).isoformat(),
            outcome="human_review",
            runtime="claude", model="sonnet", agent_type="reviewer", runner="rev-b2",
        )
        self.db.record_report(
            KEY2, "review", 1, status="human_review",
            reason="Edge case needs human input", changes="src/cache.py",
            recorded_at=(NOW - timedelta(minutes=10)).isoformat(),
        )
        self.db.add_decision(KEY2, "design", "Approve cache eviction policy?")

    def test_server_routes_full_issue_key_and_workers_from_store(self) -> None:
        sources = replace(
            bs.EMPTY,
            issues={
                KEY: bs.Issue("#12", "https://github.com/example/repo/issues/12", "Upload size limit", "open",
                              ("In progress",), body="Limit uploads to 10MB."),
                "#12": bs.Issue("#12", "https://github.com/example/repo/issues/12", "Upload size limit", "open",
                                ("In progress",), body="Limit uploads to 10MB."),
            },
        )
        bs._write(self.root / "pages" / bs.CACHE, sources)
        server = pg.make_server(self.root / "pages", 0, root=self.root, refresh=lambda r, n: None, every=3600)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        self.addCleanup(server.server_close)
        port = server.server_address[1]

        def fetch(path: str) -> tuple[int, str]:
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
            conn.request("GET", path)
            resp = conn.getresponse()
            body = resp.read().decode("utf-8")
            conn.close()
            return resp.status, body

        encoded_key = urllib.parse.quote(KEY, safe=":/")
        for route in (f"/issues/{encoded_key}", "/issues/12"):
            status, body = fetch(route)
            self.assertEqual(status, 200, route)
            self.assertIn("Upload size limit", body)
            self.assertIn("implement", body)

        for route in (f"/issues/{encoded_key}/source", "/issues/12/source"):
            status, body = fetch(route)
            self.assertEqual(status, 200, route)
            self.assertIn("Limit uploads to 10MB.", body)

        status, w_body = fetch("/workers")
        self.assertEqual(status, 200)
        self.assertIn("impl-a1", w_body)
        self.assertIn("rev-b2", w_body)
        self.assertIn("NEEDS INPUT", w_body)
        self.assertIn("Edge case needs human input", w_body)


class ReviewRegressionsTest(StorePagesAndRoutesTest):
    def request(self, method, route, data=None):
        server = pg.make_server(self.root / "pages", 0, root=self.root, refresh=lambda r, n: None, every=3600)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        self.addCleanup(server.server_close)
        conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=10)
        conn.request(method, route, body=urllib.parse.urlencode(data) if data else None)
        resp = conn.getresponse()
        body = resp.read().decode()
        conn.close()
        return resp.status, body

    def test_wal_commit_invalidates_board_without_checkpoint(self):
        pg.RENDERED_AT.clear()
        self.db.engine.dispose()
        import sqlite3
        with sqlite3.connect(self.root / "store.db") as writer:
            writer.execute("PRAGMA journal_mode=WAL")
            writer.execute("PRAGMA wal_autocheckpoint=0")
            self.assertEqual(self.request("GET", "/")[0], 200)
            before = (self.root / "store.db").read_bytes()
            writer.execute("UPDATE work_item SET title=? WHERE issue_key=?", ("Changed only in WAL", KEY))
            writer.commit()
            self.assertEqual((self.root / "store.db").read_bytes(), before)
            status, body = self.request("GET", "/")
            self.assertEqual(status, 200)
            self.assertIn("Changed only in WAL", body)

    def test_blocked_decision_holds_gantt(self):
        dec = self.db.add_decision(KEY, "design", "Choose parser")
        import decision_actions
        transition = decision_actions.answer(dec, "Split", answered_at=NOW.isoformat())
        self.db.update_decision(transition.store_write)
        import reconcile
        transition = decision_actions.meet_action(self.db.decision(dec.id), reconcile.Wait(KEY, "conflict"))
        self.db.update_decision(transition.store_write)
        row = next((status, row) for status, row in fc.build_from_store(self.db, NOW, include_hold_bars=True)
                   if row.href.endswith(urllib.parse.quote(KEY, safe="")))
        self.assertEqual(row[0], "needs input")
        self.assertIn("hold", [s.kind for s in row[1].segments])

    def test_workers_only_count_local_today_including_midnight_boundary(self):
        self.db.record_segment(KEY, "review", 2, started_at=(NOW - timedelta(days=2)).isoformat(),
                               ended_at=(NOW - timedelta(days=1)).isoformat(), outcome="human_review", runner="old-run")
        # UTC next day, still today in Chicago.
        end = NOW.replace(hour=23, minute=30).astimezone(ZoneInfo("UTC"))
        self.db.record_segment(KEY, "fix", 1, started_at=NOW.isoformat(), ended_at=end.isoformat(),
                               outcome="done", runner="late-local")
        body = workers_page.render("", bs.EMPTY, NOW, self.root / "trees", 4, db=self.db)
        self.assertNotIn("old-run", body)
        self.assertIn("late-local", body)
        self.assertIn("Ended today · 2", body)
        self.assertRegex(body, r'<b>1</b><span class="cap">NEEDS INPUT')

    def test_presenters_render_detached_use_case_data(self):
        data = task_forge.page_data(self.db)
        self.db.engine.dispose()
        with mock.patch.object(store, "Store", side_effect=AssertionError("presenter opened store")):
            body = issue_page.render(12, [], bs.EMPTY, NOW, root=self.root, db=data)
            self.assertIn("Upload size limit", body)
            body = source_page.render(12, [], bs.EMPTY, NOW, root=self.root, db=data)
            self.assertIn("Upload size limit", body)
            self.assertIn("impl-a1", workers_page.render("", bs.EMPTY, NOW, self.root / "trees", 4, db=data))

    def test_render_driver_opens_migrated_store_once_per_page(self):
        for name in (f"{NOW.date()}-board.html", "issue-12.html", "issue-12-source.html", "workers.html"):
            pg.RENDERED_AT.clear()
            with self.subTest(page=name), mock.patch.object(store_access, "migrated_store", wraps=store_access.migrated_store) as opened:
                self.assertEqual(pg.fresh(self.root, self.root / "pages", name), "")
                opened.assert_called_once_with(self.root)

    def test_reroute_refusal_keeps_stage_and_blocks_answer(self):
        dec = self.db.add_decision(KEY, "design", "Choose parser")
        fg = mock.Mock()
        fg.write_stage.return_value = SimpleNamespace(ok=False, done=False, error="write refused")
        with mock.patch.object(pg.forge, "for_workspace", return_value=fg):
            status, body = self.request("POST", "/", {"decision_id": dec.id, "action": "reroute",
                                                         "target_stage": "review", "answer": "Review again"})
        self.assertEqual(status, 409)
        self.assertEqual(self.db.item(KEY).stage, "implement")
        self.assertEqual(self.db.decision(dec.id).status, "blocked")
        self.assertIn("write refused", self.db.decision(dec.id).blocked_reason)
        fg.write_stage.assert_called_once_with(12, "review", expected="implement", commit=True)

    def test_board_command_uses_migrated_driver(self):
        import contextlib
        import io
        with mock.patch.object(store_access, "migrated_store", wraps=store_access.migrated_store) as opened:
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(pg.main(["--board", "--root", str(self.root)]), 0)
        opened.assert_called_once_with(self.root)
        board = next((self.root / "pages").glob("*-board.html")).read_text()
        self.assertIn("Upload size limit", board)

    def test_reroute_preview_does_not_close_decision(self):
        dec = self.db.add_decision(KEY, "design", "Choose parser")
        fg = mock.Mock()
        fg.write_stage.return_value = SimpleNamespace(ok=True, done=False, error="")
        with mock.patch.object(pg.forge, "for_workspace", return_value=fg):
            status, _ = self.request("POST", "/", {"decision_id": dec.id, "action": "reroute",
                                                     "target_stage": "review", "answer": "Review again"})
        self.assertEqual(status, 409)
        self.assertEqual(self.db.item(KEY).stage, "implement")
        self.assertEqual(self.db.decision(dec.id).status, "blocked")
        self.assertIn("not committed", self.db.decision(dec.id).blocked_reason)

    def test_store_decisions_have_only_board_post(self):
        dec = self.db.add_decision(KEY, "design", "Choose parser")
        status, _ = self.request("POST", "/decisions", {"decision_id": dec.id, "answer": "Split"})
        self.assertEqual(status, 404)
        self.assertEqual(self.db.decision(dec.id).status, "open")
        body = self.request("GET", "/")[1]
        self.assertRegex(body, r'<form method="post" action="/" class="decision-form">')


if __name__ == "__main__":
    unittest.main()
