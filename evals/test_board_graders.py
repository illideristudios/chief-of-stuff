"""The board graders, reading the board the renderer wrote into the case's pages dir, and the runner's
pages wiring (Zach, 2026-09-24 14:07: "we should host our own webserver"; the mock publish tool is gone)."""

from __future__ import annotations

import json

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import run

EVALS = Path(__file__).parent
CT = ZoneInfo("America/Chicago")


class PagesWiringTest(unittest.TestCase):
    def test_pages_py_is_on_the_allowlist(self) -> None:
        self.assertIn(f"Bash(python3 {run.PLUGIN_ROOT / 'scripts' / 'pages.py'}:*)", run.ALLOWED)

    def test_each_run_gets_its_own_pages_port(self) -> None:
        a, b = run.context("America/Chicago", datetime.now(CT)), run.context("America/Chicago", datetime.now(CT))
        self.assertTrue(a["pages_port"].isdigit())
        self.assertNotEqual(a["pages_port"], b["pages_port"])

    def test_stop_pages_ends_what_a_case_started(self) -> None:
        work = Path(tempfile.mkdtemp())
        (work / "pages").mkdir()
        (work / "pages.py").write_text("import time; time.sleep(60)\n")  # stop_pages signals only a `pages.py serve` pid
        proc = subprocess.Popen([sys.executable, str(work / "pages.py"), "serve"], start_new_session=True)
        (work / "pages" / ".pid").write_text(str(proc.pid))
        run.stop_pages(work)
        self.assertIsNotNone(proc.wait(timeout=5))
        run.stop_pages(Path(tempfile.mkdtemp()))  # nothing started: nothing to stop


TRACKER = "# Tracker\n\nBoard: board-1.\n\n## Tasks\n\n| item | owner | state | since | due | checklist |\n|---|---|---|---|---|---|\n| Audit | coordinator | done 10:00 | 09:00 |  | x |\n| Notes | Robin | open | 09:00 |  | y |\n\n## Log\n\n- 09:00 opened\n"


class BoardGraderTest(unittest.TestCase):
    def setUp(self) -> None:
        self.before = Path(tempfile.mkdtemp())
        self.after = Path(tempfile.mkdtemp())
        for root in (self.before, self.after):
            (root / "daily").mkdir()
            (root / "daily" / "t.md").write_text(TRACKER)
        sha = hashlib.sha256(TRACKER.encode()).hexdigest()
        self.html = (
            f'<title>Board</title>\n<meta name="tracker-sha256" content="{sha}">\n'
            '<div class="board" data-deadline="2026-09-16T23:59:00-05:00">\n'
            '<div class="bar done" data-item="Audit" data-start="2026-09-16T09:00:00-05:00" data-end="2026-09-16T10:00:00-05:00" data-start-src="since" data-end-src="state"></div>\n'
            '<div class="bar open open-end" data-item="Notes" data-start="2026-09-16T09:00:00-05:00" data-end="2026-09-16T23:59:00-05:00" data-start-src="since" data-end-src="deadline" data-label="no estimate"></div>\n'
            '<div class="dline" data-deadline-name="Final"></div></div>\n'
        )
        (self.after / "pages").mkdir()
        (self.after / "pages" / "2026-09-15-board.html").write_text("<p>yesterday</p>")
        (self.after / "pages" / "2026-09-16-board.html").write_text(self.html)
        stream = run.load_stream(EVALS / "fixtures" / "stream-denied-commit.jsonl")
        self.rec = run.RunRecord(
            stream=stream,
            t_start=datetime(2026, 9, 16, 16, 0, tzinfo=CT),
            t_end=datetime(2026, 9, 16, 16, 5, tzinfo=CT),
            tz="America/Chicago",
            fixture_dir=self.after,
            before_dir=self.before,
        )

    def grade(self, g: dict) -> tuple[bool, str]:
        return run.grade(g, self.rec)

    def test_board_published(self) -> None:
        ok, detail = self.grade({"type": "board_published", "tasks": ["Audit", "Notes"], "deadline": "2026-09-16T23:59:00-05:00"})
        self.assertTrue(ok, detail)
        ok, detail = self.grade({"type": "board_published", "tasks": ["Missing task"]})
        self.assertFalse(ok)
        self.assertIn("Missing task", detail)
        ok, _ = self.grade({"type": "board_published", "html_match": "bar done[^>]*data-item=\"Audit\""})
        self.assertTrue(ok)
        for board in (self.after / "pages").glob("*-board.html"):
            board.unlink()
        self.assertFalse(self.grade({"type": "board_published"})[0])

    def test_board_matches_tracker(self) -> None:
        g = {"type": "board_matches_tracker", "tracker": "daily/t.md"}
        self.assertTrue(self.grade(g)[0], self.grade(g)[1])
        (self.after / "daily" / "t.md").write_text(TRACKER + "- 10:05 edited after the render\n")
        ok, detail = self.grade(g)
        self.assertFalse(ok)
        self.assertIn("stale", detail)

    def test_the_graded_board_is_the_one_the_server_renders(self) -> None:
        # The agent runs no render step; the grader reads what a GET of `/` would serve after a tracker edit.
        day = datetime.now(CT).date().isoformat()
        (self.after / "CLAUDE.md").write_text(
            "## Coordinator\n- User: Robin\n- Daily log dir: `daily/`\n- Tracker: `daily/<date>-tracker.md`\n"
            "- Timezone: America/Chicago\n- Board: self-hosted; URL http://127.0.0.1:8765/; dir `pages/`\n")
        (self.after / "daily" / f"{day}-tracker.md").write_text(TRACKER)
        (self.after / "pages" / f"{day}-board.html").write_text("<p>stale</p>")
        (self.after / "daily" / f"{day}-tracker.md").write_text(TRACKER + "- 10:05 edited after the render\n")
        ok, detail = self.grade({"type": "board_matches_tracker", "tracker": f"daily/{day}-tracker.md"})
        self.assertTrue(ok, detail)

    def test_a_graded_decision_page_is_the_one_the_server_renders(self) -> None:
        # The agent writes only the JSON; a pages/ grader reads the page a GET would serve.
        day = datetime.now(CT).date().isoformat()
        (self.after / "CLAUDE.md").write_text(
            "## Coordinator\n- User: Robin\n- Daily log dir: `daily/`\n- Tracker: `daily/<date>-tracker.md`\n"
            "- Timezone: America/Chicago\n- Board: self-hosted; URL http://127.0.0.1:8765/; dir `pages/`\n")
        (self.after / "daily" / f"{day}-tracker.md").write_text(TRACKER)
        (self.after / "pages" / "decision-pool.json").write_text(json.dumps({
            "headline": "Pool size", "ask": "Which size?", "recommended": "A", "why": "Fits.", "default": "A.",
            "options": [{"key": "A", "title": "Ten", "text": "Fits."}, {"key": "B", "title": "Twenty", "text": "Over."}]}))
        ok, detail = self.grade({"type": "file_matches", "glob": "pages/decision-*.html", "pattern": "Pool size"})
        self.assertTrue(ok, detail)

    REQS = "# Final\n\n## Submission\n\n- [ ] Security audit of the upload endpoint\n- [ ] Demo video\n  - [ ] Multi-turn question\n\n## Engineering\n\n- [x] Deployed URL — evidence: https://example.test\n"

    def write_reqs(self, before: str, after: str) -> None:
        (self.before / "daily" / "reqs.md").write_text(before)
        (self.after / "daily" / "reqs.md").write_text(after)

    def test_checklist_ticks_only(self) -> None:
        g = {"type": "checklist_ticks_only", "path": "daily/reqs.md", "ticked": ["Security audit of the upload endpoint"], "evidence_match": "abc1234"}
        ticked = self.REQS.replace("- [ ] Security audit of the upload endpoint", "- [x] Security audit of the upload endpoint — evidence: commit abc1234")
        self.write_reqs(self.REQS, ticked)
        ok, detail = self.grade(g)
        self.assertTrue(ok, detail)
        self.write_reqs(self.REQS, self.REQS.replace("- [ ] Security audit of the upload endpoint", "- [x] Security audit of the upload endpoint"))
        ok, detail = self.grade(g)
        self.assertFalse(ok)
        self.assertIn("evidence", detail)
        self.write_reqs(self.REQS, ticked.replace("- [ ] Demo video", "- [x] Demo video"))
        ok, detail = self.grade(g)
        self.assertFalse(ok)
        self.assertIn("Demo video", detail)
        self.write_reqs(self.REQS, ticked.replace("- [x] Deployed URL", "- [ ] Deployed URL"))
        self.assertFalse(self.grade(g)[0])
        self.write_reqs(self.REQS, ticked.replace("Demo video", "Demo video, 3 min"))
        ok, detail = self.grade(g)
        self.assertFalse(ok)
        self.assertIn("reworded", detail)
        self.write_reqs(self.REQS, ticked + "- [ ] New item\n")
        self.assertFalse(self.grade(g)[0])
        self.write_reqs(self.REQS, self.REQS)
        ok, detail = self.grade({"type": "checklist_ticks_only", "path": "daily/reqs.md", "ticked": []})
        self.assertTrue(ok, detail)


class CaseMatchesTheRendererTest(unittest.TestCase):
    """A case's `html_match` is the renderer's markup, so the board grader fails on the agent, never on a stale pattern."""

    def test_the_done_card_pattern_matches_a_rendered_done_task(self) -> None:
        # #1153: a done task leaves every Build stage column but main, so the grader looks for the
        # audit's absence from Build (one card left; review empty), not a done chip on the card.
        case = EVALS / "cases" / "board-republish-on-task-change"
        spec = json.loads((case / "case.json").read_text())
        pattern = next(g["html_match"] for g in spec["graders"] if g.get("html_match"))
        work = Path(tempfile.mkdtemp())
        run.render_tree(case / "fixture", work, run.context(spec["tz"], datetime.now(ZoneInfo(spec["tz"]))))
        tracker = next((work / "daily").glob("*-tracker.md"))
        board = run.PLUGIN_ROOT / "scripts" / "render_board.py"
        subprocess.run([sys.executable, str(board), "--root", str(work)], check=True, capture_output=True)
        open_html = next((work / "pages").glob("*-board.html")).read_text()
        self.assertNotRegex(open_html, pattern)
        tracker.write_text(tracker.read_text().replace(
            "| Security audit | Security audit | coordinator | open |",
            "| Security audit | Security audit | coordinator | done 10:00 |"))
        subprocess.run([sys.executable, str(board), "--root", str(work)], check=True, capture_output=True)
        done_html = next((work / "pages").glob("*-board.html")).read_text()
        self.assertRegex(done_html, pattern)
        self.assertNotIn('<div class="columns-card-name">Security audit</div>', done_html)


if __name__ == "__main__":
    unittest.main()
