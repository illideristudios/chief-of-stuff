"""M6 store-backed board, Gantt, decisions on the board, issue/source/workers pages, and lead counts (#643)."""

from __future__ import annotations

import http.client
import json
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
import decision_actions  # noqa: E402
import flow_chart as fc  # noqa: E402
import gantt  # noqa: E402
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
KEY_A = "github:example/repo#20"
KEY_B = "github:example/repo#21"
KEY_C = "github:example/repo#22"

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


class StoreBoardOpenChangesTest(unittest.TestCase):
    """#896: the store board counts and draws the forge's open changes, though no tracker row names one."""

    def test_store_board_shows_the_open_changes_the_forge_holds(self) -> None:
        root, db = make_store_workspace()
        self.addCleanup(db.engine.dispose)
        (root / "CLAUDE.md").write_text(CLAUDE_MD + "- Backlog: GitHub issues; repo example/repo\n", encoding="utf-8")
        db.upsert_item(KEY, workflow="build", stage="review", forge_state="open", title="Upload size limit")
        pr = {"number": 21, "url": "https://github.com/example/repo/pull/21", "title": "Upload size limit",
              "state": "OPEN", "baseRefName": "develop", "body": "Fixes #12"}

        def gh(args, input_text=None):  # answers only what the query asks for, as GitHub does
            query = args[args.index("-f") + 1]
            data = {"merged": {"nodes": []}, **({"open": {"nodes": [pr]}} if "is:open" in query else {})}
            return 0, json.dumps({"data": data}), ""

        bs.refresh(root, NOW, gh=gh)
        html = pg.write_board(root, now=NOW)[0].read_text(encoding="utf-8")
        self.assertIn("1 merge request</div>", html)
        self.assertIn(">#21</a>", html)


def _seed_failed_attempt_fixtures(db: store.Store, *, live_worker: bool) -> None:
    fail_at = NOW - timedelta(hours=1)
    db.upsert_item(KEY_A, workflow="build", stage="plan", forge_state="open", title="Failed start")
    db.record_segment(
        KEY_A, "plan", 1,
        started_at=fail_at.isoformat(),
        ended_at=fail_at.isoformat(),
        outcome="failed",
        runtime="claude", model="opus", agent_type="plan",
    )
    t0 = NOW - timedelta(hours=2)
    db.upsert_item(KEY_B, workflow="build", stage="review", forge_state="open", title="Between stages")
    db.record_segment(
        KEY_B, "implement", 1,
        started_at=t0.isoformat(),
        ended_at=(t0 + timedelta(hours=1)).isoformat(),
        outcome="done",
        runtime="claude", model="opus", agent_type="implementer",
    )
    if live_worker:
        live_start = NOW - timedelta(minutes=30)
        db.upsert_item(KEY_C, workflow="build", stage="implement", forge_state="open", title="Live worker")
        db.record_segment(
            KEY_C, "implement", 1,
            started_at=live_start.isoformat(),
            ended_at=None,
            outcome=None,
            runtime="claude", model="opus", agent_type="implementer", runner="worker-c",
        )


class FailedAttemptBoardParityTest(unittest.TestCase):
    """#885: failed or idle attempts draw as open/queued; running counts match dispatch and the RUNNING tile."""

    def _row(self, db: store.Store, key: str) -> tuple[str, gantt.Row]:
        quoted = urllib.parse.quote(key, safe="")
        return next((status, row) for status, row in fc.build_from_store(db, NOW, include_hold_bars=True)
                    if row.href.endswith(quoted))

    def _parity(self, root: Path, db: store.Store) -> None:
        data = task_forge.page_data(db)
        flow = fc.build_from_store(db, NOW)
        counts = decision_actions.tab_counts(data.items(), data.decisions(), data.segments())
        cfg = st.load(root, "cos.toml")
        tasks, *_ = rb._tasks_from_store(data, cfg, cfg.lanes, cfg.kanban, NOW.date())
        running_tasks = [t for t in tasks if t.kind == "running"]
        open_flow = sum(1 for status, _ in flow if status == "open")
        self.assertEqual(counts["dispatch"], open_flow)
        self.assertEqual(len(running_tasks), sum(1 for status, _ in flow if status == "running"))

    def test_build_from_store_status_and_segment_kinds(self) -> None:
        root, db = make_store_workspace()
        self.addCleanup(db.engine.dispose)
        _seed_failed_attempt_fixtures(db, live_worker=True)
        status_a, row_a = self._row(db, KEY_A)
        status_b, row_b = self._row(db, KEY_B)
        status_c, row_c = self._row(db, KEY_C)
        self.assertEqual((status_a, row_a.note), ("open", "retry · plan"))
        self.assertEqual((status_b, row_b.note), ("open", "queued · review"))
        self.assertEqual((status_c, row_c.note), ("running", "implement"))
        self.assertEqual([s.kind for s in row_a.segments], ["failed"])
        self.assertEqual([s.kind for s in row_b.segments], ["done"])
        self.assertEqual([s.kind for s in row_c.segments], ["running"])

    def test_dispatch_and_running_counts_without_live_worker(self) -> None:
        root, db = make_store_workspace()
        self.addCleanup(db.engine.dispose)
        _seed_failed_attempt_fixtures(db, live_worker=False)
        self._parity(root, db)

    def test_dispatch_and_running_counts_with_live_worker(self) -> None:
        root, db = make_store_workspace()
        self.addCleanup(db.engine.dispose)
        _seed_failed_attempt_fixtures(db, live_worker=True)
        self._parity(root, db)
        data = task_forge.page_data(db)
        counts = decision_actions.tab_counts(data.items(), data.decisions(), data.segments())
        flow = fc.build_from_store(db, NOW)
        self.assertEqual(sum(1 for status, _ in flow if status == "running"), 1)
        self.assertEqual(counts["dispatch"], sum(1 for status, _ in flow if status == "open"))

    def test_flow_section_and_issue_page_show_open_not_running(self) -> None:
        root, db = make_store_workspace()
        self.addCleanup(db.engine.dispose)
        _seed_failed_attempt_fixtures(db, live_worker=False)
        rows = fc.build_from_store(db, NOW, include_hold_bars=True)
        html = fc.section(rows, NOW)
        self.assertIn("queued", html)
        self.assertNotRegex(html, r"\brunning\b")
        self.assertIn("gantt-failed", html)
        self.assertRegex(html, r'data-cat="plan"[^>]*title="[^"]*failed"')
        data = task_forge.page_data(db)
        body = issue_page.render(KEY_A, [], bs.EMPTY, NOW, root=root, db=data)
        self.assertEqual(self._row(db, KEY_A)[0], "open")
        self.assertNotIn('class="chip running"', body)

    def test_gantt_legend_names_failed(self) -> None:
        html = gantt.legend(fc.COLOURS)
        self.assertIn("failed", html)


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
        self.db.add_decision(KEY2, "plan", "Use Redis for cache?", "Yes, use Redis", "user", {"status": "answered"})
        self.db.add_decision(KEY2, "plan", "Use Redis for cache?", "Yes, use Redis", "user",
                             {"status": "blocked", "blocked_reason": "merge conflicts"})

    def test_the_board_shows_no_decision_item_and_no_decision_form(self) -> None:
        """Zach: decisions live on /decisions only; the board keeps no inline block (#895)."""
        html = pg.write_board(self.root, now=NOW)[0].read_text(encoding="utf-8")
        for absent in ("decision-item", "decision-form", "board-decisions", "<form", "Split upload parser?"):
            with self.subTest(absent=absent):
                self.assertNotIn(absent, html)

    def test_needs_input_and_the_decisions_tab_link_to_the_decisions_page(self) -> None:
        """#1122: the NEEDS INPUT tile and the Decisions tab both open /decisions."""
        html = pg.write_board(self.root, now=NOW)[0].read_text(encoding="utf-8")
        self.assertRegex(html, r'<a class="panels-tile" data-kind="blocked" href="/decisions"><span class="panels-label">NEEDS INPUT')
        self.assertRegex(html, r'<a href="/decisions"[^>]*>Decisions')

    def test_needs_input_and_the_decisions_badge_count_open_and_blocked_decisions(self) -> None:
        """#1124: a blocked row is a failed intent a person's newer answer clears, so it still needs input."""
        html = pg.write_board(self.root, now=NOW)[0].read_text(encoding="utf-8")
        self.assertIn('<span class="panels-label">NEEDS INPUT</span><b class="panels-count">2</b>', html)
        self.assertIn('<span class="badge" title="2 pending">2</span>', html)
        data = task_forge.page_data(self.db)
        self.assertEqual(decision_actions.tab_counts(data.items(), data.decisions(), data.segments())["decisions"], 2)

    def test_decisions_page_renders_open_and_blocked_decisions_and_blocked_is_never_closed(self) -> None:
        board_path, *_ = pg.write_board(self.root, now=NOW)
        html = (board_path.parent / "decisions.html").read_text(encoding="utf-8")
        self.assertIn("Split upload parser?", html)
        self.assertIn("Use Redis for cache?", html)
        self.assertIn("merge conflicts", html)
        # Blocked decision must show as BLOCKED, never as CLOSED:
        self.assertRegex(html, r"(?i)blocked.*merge conflicts|merge conflicts.*blocked")
        self.assertNotRegex(html, r"(?i)>\s*closed\s*<")

    def test_a_superseded_decision_has_no_card_with_live_buttons(self) -> None:
        self.db.add_decision(KEY, "design", "Replaced question?")
        self.db.add_decision(KEY, "design", "Replaced question?", "Yes", "user",
                             {"status": "superseded", "superseded_by": self.d_open.id})
        html = (pg.write_board(self.root, now=NOW)[0].parent / "decisions.html").read_text(encoding="utf-8")
        self.assertIn("Split upload parser?", html)
        self.assertNotIn("Replaced question?", html)

    def test_pages_post_answer_reverse_and_reroute_only_record_in_the_store(self) -> None:
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
            d1 = self.db.decisions(KEY)[-1]  # appended on the decision's own identity
            self.assertEqual((d1.topic, d1.question, d1.status), (self.d_open.topic, self.d_open.question, "answered"))
            self.assertEqual(d1.answer, "Split into two")
            self.assertEqual(d1.decided_by, "user")

            # 2. Reverse the answered decision, posted from the row the board now shows: a run intent on its next row.
            resp = post("/", {"decision_id": str(self.d_open.id), "action": "reverse", "answer": "Keep single pass"})
            self.assertEqual(resp.status, 409)  # a form from an older row is refused
            resp = post("/", {"decision_id": str(d1.id), "action": "reverse", "answer": "Keep single pass"})
            self.assertEqual(resp.status, 303)
            d2 = self.db.decisions(KEY)[-1]
            self.assertNotEqual(d2.id, d1.id)
            self.assertEqual((d2.status, d2.record["note"], d2.intent), ("reversed", "Keep single pass", "run"))

            # 3. Re-route another decision to another stage: a write intent; the earlier intent is left for the engine.
            reroute_dec = self.db.add_decision(KEY, "plan", "Return to implementation?")
            resp = post("/", {
                "decision_id": str(reroute_dec.id),
                "action": "reroute",
                "target_stage": "implement",
                "answer": "Back to implement",
            })
            self.assertEqual(resp.status, 303)
            d3 = self.db.decisions(KEY)[-1]
            self.assertNotEqual(d3.id, reroute_dec.id)
            self.assertEqual((d3.status, d3.rerouted_to, d3.intent), ("re-routed", "implement", "write"))
            self.assertEqual((self.db.decision(d2.id).status, self.db.decision(d2.id).intent), ("reversed", "run"))
            # The page server records only: no forge call, no card move, no slot.
            fake_forge.write_stage.assert_not_called()
            fake_forge.comment.assert_not_called()
            self.assertEqual(self.db.item(KEY).stage, "review")
            self.assertIsNone(self.db.resume(KEY))  # the page server writes only its rows
            import decision_actions
            self.assertEqual(len(decision_actions.pending_comments(self.db.decisions(KEY), ())), 3)


class DecisionPageByIdTest(unittest.TestCase):
    """#945: every store decision has an engine-rendered page at /decisions/<id>, <id> its identity's first row
    (§6). Open or blocked carries the answer form; answered shows the answer and who gave it and keeps only Reverse,
    which appends a reversal intent (#971). Unknown ids are 404; authored /decisions/<slug> pages keep working."""

    TRIO = {"decision": {"issue_key": KEY, "topic": "scope", "question": "Approve this file ownership?",
                         "options": ["approve", "reject"], "context": "write: scripts/cache.py", "stage": "plan",
                         "attempt": 1},
            "verdicts": [{"persona": "architect", "vote": "concern", "choice": None,
                          "reasoning": "model failure (timeout): timed out after 15 seconds"}],
            "recommendation": None, "unanimous": False,
            "error": "trio unavailable after 3 attempts: Approve this file ownership?"}

    def setUp(self) -> None:
        self.root, self.db = make_store_workspace()
        self.addCleanup(self.db.engine.dispose)
        self.db.upsert_item(KEY, workflow="build", stage="review", forge_state="open", title="Upload size limit")
        self.open = self.db.add_decision(KEY, "scope", "Approve this file ownership?", None, "zach", self.TRIO)
        self.asked = self.db.add_decision(KEY, "design", "Split upload parser?")
        self.answered = self.db.add_decision(KEY, "design", "Split upload parser?", "Split into two", "user",
                                             {"status": "answered"})
        self.forge = mock.MagicMock()
        patcher = mock.patch.object(pg.forge, "for_workspace", return_value=self.forge)
        patcher.start()
        self.addCleanup(patcher.stop)
        server = pg.make_server(self.root / "pages", 0, root=self.root, refresh=lambda r, n: None, every=3600)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        self.addCleanup(server.server_close)
        self.port = server.server_address[1]

    def request(self, method: str, path: str, data: dict[str, str] | None = None) -> tuple[int, str, str]:
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        body = urllib.parse.urlencode(data).encode() if data is not None else None
        conn.request(method, path, body=body, headers={"Content-Type": "application/x-www-form-urlencoded"} if body else {})
        resp = conn.getresponse()
        text = resp.read().decode("utf-8")
        conn.close()
        return resp.status, resp.getheader("Location") or "", text

    def test_an_open_decision_page_shows_the_field_set_and_the_answer_form(self) -> None:
        status, _, page = self.request("GET", f"/decisions/{self.open.id}")
        self.assertEqual(status, 200)
        from fragment import issue_href
        for text in ("Approve this file ownership?", "approve", "reject", "write: scripts/cache.py",
                     "trio unavailable after 3 attempts", "timed out after 15 seconds", "Robin",
                     "If you don't answer", "default not recorded", f'href="{issue_href(KEY)}"'):
            with self.subTest(text=text):
                self.assertIn(text, page)
        form = re.search(r'<form method="post" action="/" class="decision-form">.*?</form>', page, re.S)
        self.assertIsNotNone(form)
        self.assertIn(f'name="decision_id" value="{self.open.id}"', form[0])
        self.assertIn(f'name="back" value="/decisions/{self.open.id}"', form[0])
        self.assertIn('value="answer"', form[0])

    def test_an_answered_decision_shows_the_answer_and_who_with_only_reverse_and_its_history(self) -> None:
        for row in (self.asked, self.answered):  # the first row's id is the address; a later row's renders the same
            with self.subTest(row=row.id):
                status, _, page = self.request("GET", f"/decisions/{row.id}")
                self.assertEqual(status, 200)
                self.assertIn("Split upload parser?", page)
                self.assertIn("Split into two", page)
                self.assertIn("user", page)
                form = re.search(r'<form method="post" action="/" class="decision-form">.*?</form>', page, re.S)
                self.assertIsNotNone(form)
                self.assertNotIn('value="answer"', form[0])
                self.assertNotIn('value="reroute"', form[0])
                self.assertIn('value="reverse"', form[0])
                self.assertIn(f'name="decision_id" value="{self.answered.id}"', form[0])
                self.assertIn(f'name="back" value="/decisions/{self.asked.id}"', form[0])
                history = page.split("History", 1)[1]
                self.assertLess(history.index(f"#{self.answered.id}"), history.index(f"#{self.asked.id}"))
                self.assertNotIn(f"#{self.open.id} ", history)

    def test_reverse_from_the_page_appends_a_reversal_intent_and_returns_to_the_page(self) -> None:
        status, where, _ = self.request("POST", "/", {"decision_id": str(self.answered.id), "action": "reverse",
                                                      "answer": "Keep single pass", "back": f"/decisions/{self.asked.id}"})
        self.assertEqual((status, where), (303, f"/decisions/{self.asked.id}"))
        row = self.db.decisions(KEY)[-1]
        self.assertNotEqual(row.id, self.answered.id)
        self.assertEqual((row.question, row.status, row.record["note"], row.intent),
                         ("Split upload parser?", "reversed", "Keep single pass", "run"))
        self.assertEqual(self.db.decision(self.answered.id).answer, "Split into two")  # appended, never overwritten
        self.forge.write_stage.assert_not_called()
        self.forge.comment.assert_not_called()
        status, _, page = self.request("GET", f"/decisions/{self.asked.id}")
        self.assertEqual(status, 200)
        self.assertIn("Keep single pass", page)

    def test_an_answer_from_the_page_returns_to_it_and_a_foreign_back_returns_to_the_list(self) -> None:
        status, where, _ = self.request("POST", "/", {"decision_id": str(self.open.id), "action": "answer",
                                                      "answer": "approve", "back": f"/decisions/{self.open.id}"})
        self.assertEqual((status, where), (303, f"/decisions/{self.open.id}"))
        later = self.db.add_decision(KEY, "plan", "Return to implementation?")
        status, where, _ = self.request("POST", "/", {"decision_id": str(later.id), "action": "answer",
                                                      "answer": "yes", "back": "http://elsewhere/x"})
        self.assertEqual((status, where), (303, "/decisions"))

    def test_an_unknown_id_is_404_and_an_authored_page_still_serves(self) -> None:
        self.assertEqual(self.request("GET", "/decisions/9999")[0], 404)
        (self.root / "pages" / "decision-pick-db.json").write_text(json.dumps({
            "headline": "Pick a database", "ask": "Which one?", "recommended": "a", "why": "simpler",
            "default": "a", "options": [{"key": "a", "title": "SQLite"}, {"key": "b", "title": "Postgres"}]}))
        status, _, page = self.request("GET", "/decisions/pick-db")
        self.assertEqual(status, 200)
        self.assertIn("Pick a database", page)

    def test_the_decisions_list_and_the_task_page_link_each_decision_to_its_page(self) -> None:
        status, _, listing = self.request("GET", "/decisions")
        self.assertEqual(status, 200)
        self.assertIn(f'href="/decisions/{self.open.id}"', listing)
        status, _, task = self.request("GET", "/issues/12")
        self.assertEqual(status, 200)
        for d in (self.open, self.asked):
            with self.subTest(decision=d.id):
                self.assertIn(f'href="/decisions/{d.id}"', task)
        self.assertIn("Split upload parser?", task)


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

    def test_live_workers_and_pending_decision_are_visible_on_every_surface(self):
        self.db.add_decision(KEY, "design", "Choose a parser")
        self.db.record_segment(KEY, "plan", 1, (NOW - timedelta(minutes=5)).isoformat(),
                               runner="plan-a1", runtime="cursor", model="composer")
        # A cache entry for the first worker must not count that same worker twice.
        sources = replace(bs.EMPTY, workers=(
            bs.Worker("impl-a1", "one-shot", "claude", "opus", "Upload size limit", "impl-a1", NOW, None),
            bs.Worker("idle", "session", "claude", "", "", "", NOW, None),
        ))
        data = task_forge.page_data(self.db)
        cfg = parse_coordinator((self.root / "CLAUDE.md").read_text(), NOW.date())
        settings = st.load(self.root, cfg.settings_path)
        board = rb.render("", cfg, NOW, settings.lanes, kanban=settings.kanban, sources=sources, db=data)
        workers = workers_page.render("", sources, NOW, self.root / "trees", 4, db=data)
        self.assertIn('<span class="panels-label">RUNNING</span><b class="panels-count">2</b>', board)
        self.assertIn('<h2>Running · 2</h2>', workers)
        card = re.search(r'<div class="columns-card columns-flagged"[^>]*>.*?NEEDS INPUT</div></div>', board, re.S)[0]
        self.assertIn('data-cat="running">running', card)
        header = re.search(r'<header\b.*?</header>', issue_page.render(KEY, [], sources, NOW, db=data), re.S)[0]
        self.assertIn('class="chip needs-input">NEEDS INPUT', header)
        self.assertIn('class="chip running">RUNNING', header)
        self.assertEqual(header.count('>RUNNING</span>'), 1)
        # The count comes from store segments even when the worker cache is empty.
        no_cache = rb.render("", cfg, NOW, settings.lanes, kanban=settings.kanban, db=data)
        self.assertIn('<span class="panels-label">RUNNING</span><b class="panels-count">2</b>', no_cache)

    def test_counts_include_distinct_cached_and_store_workers_only_once(self):
        self.db.record_segment(KEY, "plan", 1, NOW.isoformat(), runner="plan-a1", outcome="mystery")
        sources = replace(bs.EMPTY, workers=(
            bs.Worker("impl-a1", "one-shot", "claude", "opus", "Upload size limit", "impl-a1", NOW, None),
            bs.Worker("impl-a1", "one-shot", "claude", "opus", "Upload size limit", "impl-a1", NOW, None),
            bs.Worker("extra", "session", "claude", "", "Cache warmup", "", NOW, None),
            bs.Worker("idle", "session", "claude", "", "", "", NOW, None),
        ))
        data = task_forge.page_data(self.db)
        cfg = parse_coordinator((self.root / "CLAUDE.md").read_text(), NOW.date())
        settings = st.load(self.root, cfg.settings_path)
        board = rb.render("", cfg, NOW, settings.lanes, kanban=settings.kanban, sources=sources, db=data)
        workers = workers_page.render("", sources, NOW, self.root / "trees", 4, db=data)
        self.assertIn('<span class="panels-label">RUNNING</span><b class="panels-count">3</b>', board)
        self.assertIn('<h2>Running · 3</h2>', workers)
        self.assertIn('<b>2 of 4</b><span class="cap">SLOTS FREE', workers)
        body = issue_page.render(KEY, [], sources, NOW, db=data)
        self.assertIn('>running · 0m</span>', body)

    def test_ended_and_unknown_outcomes_do_not_imply_a_live_worker(self):
        self.db.end_segment(KEY, "implement", 1, NOW.isoformat(), "mystery")
        self.db.record_segment(KEY, "plan", 1, (NOW - timedelta(minutes=2)).isoformat(), NOW.isoformat(),
                               runner="plan-ended")
        self.db.add_decision(KEY, "design", "Choose a parser")
        data = task_forge.page_data(self.db)
        body = issue_page.render(KEY, [], bs.EMPTY, NOW, db=data)
        header = re.search(r'<header\b.*?</header>', body, re.S)[0]
        self.assertIn('class="chip needs-input">NEEDS INPUT', header)
        self.assertNotIn('class="chip running"', header)
        self.assertIn('>ended · 2m</span>', body)
        workers = workers_page.render("", bs.EMPTY, NOW, self.root / "trees", 4, db=data)
        self.assertIn('<h2>Running · 0</h2>', workers)
        cfg = parse_coordinator((self.root / "CLAUDE.md").read_text(), NOW.date())
        board = rb.render("", cfg, NOW, st.load(self.root, cfg.settings_path).lanes, db=data)
        self.assertIn('<span class="panels-label">RUNNING</span><b class="panels-count">0</b>', board)

    def test_unnamed_live_runs_with_the_same_stage_and_attempt_are_distinct(self):
        self.db.record_segment(KEY, "plan", 1, NOW.isoformat())
        self.db.record_segment(KEY2, "plan", 1, NOW.isoformat())
        sources = replace(bs.EMPTY, workers=(
            bs.Worker("plan-1", "session", "claude", "", "Other task", "", NOW, None),
        ))
        data = task_forge.page_data(self.db)
        cfg = parse_coordinator((self.root / "CLAUDE.md").read_text(), NOW.date())
        board = rb.render("", cfg, NOW, st.load(self.root, cfg.settings_path).lanes, sources=sources, db=data)
        workers = workers_page.render("", sources, NOW, self.root / "trees", 4, db=data)
        self.assertIn('<span class="panels-label">RUNNING</span><b class="panels-count">4</b>', board)
        self.assertIn('<h2>Running · 4</h2>', workers)

    def test_unknown_decision_state_does_not_hide_a_live_run_or_invent_a_hold(self):
        self.db.add_decision(KEY, "design", "Choose a parser", record={"status": "unrecognized"})
        data = task_forge.page_data(self.db)
        cfg = parse_coordinator((self.root / "CLAUDE.md").read_text(), NOW.date())
        board = rb.render("", cfg, NOW, st.load(self.root, cfg.settings_path).lanes, db=data)
        card = re.search(r'<div class="columns-card">.*?Upload size limit.*?</div></div>', board, re.S)[0]
        self.assertIn('data-cat="running">running', card)
        self.assertNotIn("NEEDS INPUT", card)
        header = re.search(r'<header\b.*?</header>', issue_page.render(KEY, [], bs.EMPTY, NOW, db=data), re.S)[0]
        self.assertIn('class="chip running">RUNNING', header)
        self.assertNotIn("NEEDS INPUT", header)

    def test_server_routes_full_issue_key_and_workers_from_store(self) -> None:
        # Reopen before the HTTP route reads failed-start evidence: it must survive a restart.
        for attempt, end, outcome in ((1, NOW, "failed"), (2, NOW + timedelta(seconds=20), "failed"),
                                       (3, NOW + timedelta(minutes=2), "done")):
            self.db.record_segment(KEY, "plan", attempt, NOW.isoformat(), end.isoformat(), outcome,
                                   runtime="cursor", model="composer-2.5")
        self.db.engine.dispose()
        self.db = store.Store(self.root / "store.db")
        self.addCleanup(self.db.engine.dispose)
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
            self.assertIn('cursor composer-2.5 · plan · attempt 1</span><span>failed to start · 0m', body)
            self.assertIn('cursor composer-2.5 · plan · attempt 2</span><span>failed · 0m', body)
            self.assertIn('cursor composer-2.5 · plan · attempt 3</span><span>done · 2m', body)

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
        self.location = resp.getheader("Location")
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
        import trio_gate
        transition = decision_actions.answer(dec, "Split", answered_at=NOW.isoformat())
        trio_gate.record(self.db, KEY, transition.update)
        transition = decision_actions.refuse(self.db.decisions(KEY)[-1], "conflict")
        trio_gate.record(self.db, KEY, transition.update)
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

    def test_a_segment_that_starts_and_ends_at_one_instant_lists_started_first(self):
        t = (NOW - timedelta(minutes=10)).isoformat()
        t2 = (NOW - timedelta(minutes=3)).isoformat()
        self.db.record_segment(KEY, "plan", 2, started_at=t, ended_at=t, outcome="failed", runner="plan-2")
        self.db.record_segment(KEY, "plan", 3, started_at=t2, ended_at=t2, outcome="failed", runner="plan-3")
        self.db.add_log(KEY, "dispatch failed: could not verify worker start", at=t)
        body = issue_page.render(12, [], bs.EMPTY, NOW, root=self.root, db=task_forge.page_data(self.db))
        history = body.split("How it got here", 1)[1]
        self.assertLess(history.index("plan started · plan-2"), history.index("plan failed · plan-2"))
        self.assertLess(history.index("plan failed · plan-2"), history.index("dispatch failed: could not verify worker start"))
        self.assertLess(history.index("plan started · plan-3"), history.index("plan failed · plan-3"))

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

    def test_a_reroute_post_records_an_intent_and_never_reaches_the_forge(self):
        dec = self.db.add_decision(KEY, "design", "Choose parser")
        with mock.patch.object(pg.forge, "for_workspace", side_effect=AssertionError("the page server never writes")):
            status, _ = self.request("POST", "/", {"decision_id": dec.id, "action": "reroute",
                                                     "target_stage": "review", "answer": "Review again"})
        self.assertEqual(status, 303)
        self.assertEqual(self.db.item(KEY).stage, "implement")
        saved = self.db.decisions(KEY)[-1]
        self.assertEqual((saved.status, saved.rerouted_to, saved.intent, saved.blocked_reason), ("re-routed", "review", "write", ""))

    def test_board_command_uses_migrated_driver(self):
        import contextlib
        import io
        with mock.patch.object(store_access, "migrated_store", wraps=store_access.migrated_store) as opened:
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(pg.main(["--board", "--root", str(self.root)]), 0)
        opened.assert_called_once_with(self.root)
        board = next((self.root / "pages").glob("*-board.html")).read_text()
        self.assertIn("Upload size limit", board)

    def test_store_decisions_post_only_through_the_store_answer_path(self):
        dec = self.db.add_decision(KEY, "design", "Choose parser")
        status, _ = self.request("POST", "/decisions", {"decision_id": dec.id, "answer": "Split"})
        self.assertEqual(status, 404)
        self.assertEqual(self.db.decision(dec.id).status, "open")

    def test_a_decisions_page_form_records_its_answer_in_the_store(self):
        """#895: GET /decisions, post the open store decision's own form, and the store holds the person's answer;
        the server records it only, and sends the person back to /decisions."""
        status, page = self.request("GET", "/decisions")
        self.assertEqual(status, 200)
        row = next(r for r in page.split('<div class="row pend"')[1:] if "Approve cache eviction policy?" in r)
        form = re.search(r'<form method="post" action="([^"]*)" class="decision-form">(.*?)</form>', row, re.S)
        fields = dict(re.findall(r'<input type="hidden" name="(\w+)" value="([^"]*)"', form[2]))
        with mock.patch.object(pg.forge, "for_workspace", side_effect=AssertionError("the page server never writes")):
            status, _ = self.request("POST", form[1], {**fields, "action": "answer", "answer": "Evict least recent"})
        self.assertEqual((status, self.location), (303, "/decisions"))
        saved = self.db.decisions(KEY2)[-1]
        self.assertEqual((saved.question, saved.status, saved.answer, saved.decided_by),
                         ("Approve cache eviction policy?", "answered", "Evict least recent", "user"))
        self.assertEqual(self.db.item(KEY2).stage, "review")

    def test_an_answered_decision_on_the_decisions_page_has_no_live_form(self):
        """#924: a COMPLETED row is read-only; only an open or blocked decision carries a form."""
        dec = self.db.decisions(KEY2)[-1]
        self.request("POST", "/", {"decision_id": dec.id, "action": "answer", "answer": "Evict least recent"})
        page = self.request("GET", "/decisions")[1]
        pending, done = page.split("COMPLETED")
        self.assertIn("Approve cache eviction policy?", done)
        self.assertNotIn("Approve cache eviction policy?", pending)
        self.assertNotIn("<form", done)


def _seed_exhausted(db: store.Store, key: str, *, title: str, failure: str,
                    live: bool = False, clear_interrupt: bool = False) -> None:
    """An issue whose retries burned out: failed segments, exhausted interrupt, optional live recovery."""
    fail_at = NOW - timedelta(minutes=10)
    db.upsert_item(key, workflow="build", stage="plan", forge_state="open", title=title)
    db.record_segment(key, "plan", 1, fail_at.isoformat(), fail_at.isoformat(), "failed",
                      runtime="claude", model="opus", agent_type="plan", runner="plan-1")
    db.record_segment(key, "plan", 2, (fail_at + timedelta(minutes=1)).isoformat(),
                      (fail_at + timedelta(minutes=1)).isoformat(), "failed",
                      runtime="claude", model="opus", agent_type="plan", runner="plan-2")
    db.add_log(key, failure, (fail_at + timedelta(minutes=1)).isoformat())
    db.add_log(key, "exhausted: retry limit reached for plan",
               (fail_at + timedelta(minutes=2)).isoformat())
    resume = {"interrupt": {"kind": "exhausted", "reason": "retry limit reached for plan", "stage": "plan"}}
    if clear_interrupt:
        resume = {}
    if live:
        db.record_segment(key, "plan", 3, (NOW - timedelta(minutes=2)).isoformat(), None, None,
                          runtime="claude", model="opus", agent_type="plan", runner="plan-3")
        if clear_interrupt:
            resume = {}
    db.set_resume(key, resume)


class RetryExhaustionDisplayTest(unittest.TestCase):
    """#963: exhausted retries read as needs-input with evidence; live or recovered runs are not mislabelled."""

    def test_latest_exhaustion_uses_the_latest_failure(self):
        import readiness
        evidence = readiness.retry_exhaustion(
            {"interrupt": {"kind": "exhausted", "reason": "retry limit reached"}}, (),
            ["dispatch failed: first", "exhausted: retry limit reached",
             "dispatch failed: second", "exhausted: retry limit reached"])
        self.assertEqual(evidence.failure, "dispatch failed: second")

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

    def test_exhausted_issue_flags_board_flow_task_and_workers(self) -> None:
        failure = "dispatch failed: existing worktree is missing or no longer on the work branch"
        _seed_exhausted(self.db, KEY_A, title="Classification metrics", failure=failure)
        data = task_forge.page_data(self.db)
        evidence = data.exhaustion(KEY_A)
        self.assertIsNotNone(evidence)
        self.assertEqual(evidence.failure, failure)
        status, row = next((s, r) for s, r in fc.build_from_store(self.db, NOW, include_hold_bars=True)
                           if r.href.endswith(urllib.parse.quote(KEY_A, safe="")))
        self.assertEqual(status, "needs input")
        self.assertIn(task_forge.EXHAUSTED_CHIP, row.note)
        self.assertIn("hold", [g.kind for g in row.segments])
        counts = decision_actions.tab_counts(data.items(), data.decisions(), data.segments(), data.resumes)
        self.assertEqual(counts["dispatch"], 0)
        self.assertGreaterEqual(counts["workers"], 1)
        board = pg.write_board(self.root, now=NOW)[0].read_text(encoding="utf-8")
        self.assertIn(task_forge.EXHAUSTED_CHIP, board)
        self.assertIn("NEEDS INPUT", board)
        self.assertRegex(board, r'data-cat="hold"[^>]*>NEEDS INPUT')
        body = issue_page.render(KEY_A, [], bs.EMPTY, NOW, root=self.root, db=data)
        self.assertIn(f'class="chip retries-exhausted">{task_forge.EXHAUSTED_CHIP.upper()}', body)
        self.assertIn(failure, body)
        self.assertIn(task_forge.EXHAUSTED_UNBLOCK, body)
        self.assertNotIn('class="chip running"', body)
        workers = workers_page.render("", bs.EMPTY, NOW, self.root / "trees", 4, db=data)
        self.assertIn(task_forge.EXHAUSTED_CHIP, workers)
        self.assertNotIn("re-dispatch", workers)
        self.assertIn(failure, workers)

    def test_a_live_segment_wins_over_a_stale_exhausted_interrupt(self) -> None:
        _seed_exhausted(self.db, KEY_B, title="Live after exhaustion",
                        failure="dispatch failed: worktree missing", live=True)
        data = task_forge.page_data(self.db)
        self.assertIsNone(data.exhaustion(KEY_B))
        status, row = next((s, r) for s, r in fc.build_from_store(self.db, NOW, include_hold_bars=True)
                           if r.href.endswith(urllib.parse.quote(KEY_B, safe="")))
        self.assertEqual(status, "running")
        self.assertNotIn(task_forge.EXHAUSTED_CHIP, row.note)
        body = issue_page.render(KEY_B, [], bs.EMPTY, NOW, root=self.root, db=data)
        self.assertNotIn(task_forge.EXHAUSTED_CHIP, body)
        workers = workers_page.render("", bs.EMPTY, NOW, self.root / "trees", 4, db=data)
        self.assertIn("plan-3", workers)
        self.assertNotIn("re-dispatch", workers)  # no inert or unverified retry action
        self.assertIn("/issues/", workers)

    def test_a_recovered_issue_is_not_labelled_from_stale_exhausted_log_lines(self) -> None:
        _seed_exhausted(self.db, KEY_C, title="Recovered after raise",
                        failure="dispatch failed: worktree missing", clear_interrupt=True)
        # A fresh attempt after the interrupt was cleared: queued/open, not exhausted.
        self.db.record_segment(KEY_C, "plan", 3, (NOW - timedelta(minutes=3)).isoformat(),
                               (NOW - timedelta(minutes=1)).isoformat(), "done",
                               runtime="claude", model="opus", runner="plan-3")
        data = task_forge.page_data(self.db)
        self.assertIsNone(data.exhaustion(KEY_C))
        self.assertTrue(any(e.message.startswith("exhausted:") for e in data.log(KEY_C)))
        status, row = next((s, r) for s, r in fc.build_from_store(self.db, NOW, include_hold_bars=True)
                           if r.href.endswith(urllib.parse.quote(KEY_C, safe="")))
        self.assertEqual(status, "open")
        self.assertNotIn(task_forge.EXHAUSTED_CHIP, row.note)
        body = issue_page.render(KEY_C, [], bs.EMPTY, NOW, root=self.root, db=data)
        self.assertNotIn('class="chip retries-exhausted"', body)
        self.assertNotIn(task_forge.EXHAUSTED_UNBLOCK, body)
        workers = workers_page.render("", bs.EMPTY, NOW, self.root / "trees", 4, db=data)
        # The previous exhaustion no longer applies, and ended rows offer task links.
        self.assertNotIn(task_forge.EXHAUSTED_CHIP, workers)


if __name__ == "__main__":
    unittest.main()
