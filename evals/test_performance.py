"""Agent performance as a store query over stage segments (plan M6 item 4, ledger A3, architecture §9)."""

from __future__ import annotations

import io
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import performance  # noqa: E402
import performance_report  # noqa: E402
import store  # noqa: E402

UTC = ZoneInfo("UTC")
KEY = "github:example/repo#12"

COORD = """# Workspace

## Coordinator

- User: Robin
- Daily log dir: `logs`
- Tracker: `logs/{{date}}.md`
- Timezone: UTC
"""


def seg(**fields: object) -> SimpleNamespace:
    base = dict(runtime="", model="", effort="", agent_type="", attempt=1,
                started_at="2026-10-09T10:00:00+00:00", ended_at=None, outcome=None)
    base.update(fields)
    return SimpleNamespace(**base)


class PerformanceTableTest(unittest.TestCase):
    def test_failed_start_is_attributed_to_its_host_model_and_effort(self) -> None:
        rows = performance.table([
            seg(runtime="claude", model="opus", effort="high", ended_at="2026-10-09T10:01:00+00:00", outcome="done"),
            seg(runtime="cursor", model="composer-2.5", effort="medium", attempt=2,
                ended_at="2026-10-09T10:00:00+00:00", outcome="failed"),
        ], since=date(2026, 10, 9), until=date(2026, 10, 9), by="day", zone=UTC)
        self.assertEqual([(r.runtime, r.model, r.effort, r.runs, r.completed, r.median_minutes) for r in rows],
                         [("claude", "opus", "high", 1, 1, 1), ("cursor", "composer-2.5", "medium", 1, 0, 0)])

    def test_counts_runs_completed_human_review_and_relaunched_per_day_and_key(self) -> None:
        rows = performance.table([
            seg(runtime="claude", model="opus", effort="high", agent_type="implementer",
                started_at="2026-10-09T09:00:00+00:00", ended_at="2026-10-09T09:30:00+00:00", outcome="done"),
            seg(runtime="claude", model="opus", effort="high", agent_type="implementer", attempt=2,
                started_at="2026-10-09T10:00:00+00:00", ended_at="2026-10-09T10:20:00+00:00",
                outcome="human_review"),
            seg(runtime="codex", model="gpt", effort="", agent_type="",
                started_at="2026-10-09T11:00:00+00:00", ended_at="2026-10-09T11:10:00+00:00", outcome="done"),
        ], since=date(2026, 10, 9), until=date(2026, 10, 9), by="day", zone=UTC)
        self.assertEqual(
            [(r.period, r.runtime, r.model, r.effort, r.agent_type,
              r.runs, r.completed, r.human_review, r.relaunched, r.median_minutes) for r in rows],
            [
                (date(2026, 10, 9), "claude", "opus", "high", "implementer", 2, 1, 1, 1, 20),
                (date(2026, 10, 9), "codex", "gpt", "", "", 1, 1, 0, 0, 10),
            ],
        )

    def test_median_minutes_is_whole_minutes_and_takes_the_lower_middle_when_even(self) -> None:
        # Durations 10, 20, 30, 40 → lower middle is 20.
        rows = performance.table([
            seg(runtime="claude", started_at="2026-10-09T09:00:00+00:00",
                ended_at="2026-10-09T09:10:00+00:00", outcome="done"),
            seg(runtime="claude", started_at="2026-10-09T09:00:00+00:00",
                ended_at="2026-10-09T09:20:00+00:00", outcome="done"),
            seg(runtime="claude", started_at="2026-10-09T09:00:00+00:00",
                ended_at="2026-10-09T09:30:00+00:00", outcome="done"),
            seg(runtime="claude", started_at="2026-10-09T09:00:00+00:00",
                ended_at="2026-10-09T09:40:00+00:00", outcome="done"),
        ], since=date(2026, 10, 9), until=date(2026, 10, 9), by="day", zone=UTC)
        self.assertEqual([r.median_minutes for r in rows], [20])

    def test_a_week_row_uses_that_weeks_monday_and_since_until_are_inclusive(self) -> None:
        # Thursday 2026-10-08 and Monday 2026-10-12 fall in different weeks.
        rows = performance.table([
            seg(runtime="claude", started_at="2026-10-08T12:00:00+00:00",
                ended_at="2026-10-08T12:15:00+00:00", outcome="done"),
            seg(runtime="claude", started_at="2026-10-12T12:00:00+00:00",
                ended_at="2026-10-12T12:15:00+00:00", outcome="done"),
        ], since=date(2026, 10, 8), until=date(2026, 10, 12), by="week", zone=UTC)
        self.assertEqual([(r.period, r.runs) for r in rows],
                         [(date(2026, 10, 5), 1), (date(2026, 10, 12), 1)])

    def test_an_open_segment_counts_as_a_run_but_not_in_the_median(self) -> None:
        rows = performance.table([
            seg(runtime="claude", started_at="2026-10-09T09:00:00+00:00",
                ended_at="2026-10-09T09:30:00+00:00", outcome="done"),
            seg(runtime="claude", started_at="2026-10-09T10:00:00+00:00"),
        ], since=date(2026, 10, 9), until=date(2026, 10, 9), by="day", zone=UTC)
        self.assertEqual([(r.runs, r.completed, r.median_minutes) for r in rows], [(2, 1, 30)])

    def test_segments_outside_the_window_are_left_out(self) -> None:
        rows = performance.table([
            seg(runtime="claude", started_at="2026-10-08T12:00:00+00:00",
                ended_at="2026-10-08T12:05:00+00:00", outcome="done"),
            seg(runtime="claude", started_at="2026-10-10T12:00:00+00:00",
                ended_at="2026-10-10T12:05:00+00:00", outcome="done"),
        ], since=date(2026, 10, 9), until=date(2026, 10, 9), by="day", zone=UTC)
        self.assertEqual(rows, [])


class PerformanceReportTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "CLAUDE.md").write_text(COORD)
        store.migrate(self.root / "store.db")
        self.db = store.Store(self.root / "store.db")
        self.addCleanup(self.db.engine.dispose)
        self.db.upsert_item(KEY, title="x")
        self.db.record_segment(
            KEY, "implement", 1, started_at="2026-10-09T09:00:00+00:00",
            ended_at="2026-10-09T09:25:00+00:00", outcome="done",
            runtime="claude", model="opus", effort="high", agent_type="implementer",
        )

    def test_main_prints_a_table_from_the_store_and_exits_zero(self) -> None:
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = performance_report.main([
                "--root", str(self.root), "--since", "2026-10-09", "--until", "2026-10-09",
            ])
        self.assertEqual(code, 0)
        out = buf.getvalue()
        self.assertIn("claude", out)
        self.assertIn("opus", out)
        self.assertIn("implementer", out)
        self.assertRegex(out, r"\b1\b")  # runs
        self.assertIn("25", out)  # median minutes

    def test_a_store_at_an_older_revision_is_brought_up_before_the_query(self) -> None:
        old = self.root / "old"
        old.mkdir()
        (old / "CLAUDE.md").write_text(COORD)
        cfg = store.Config()
        cfg.set_main_option("script_location", str(ROOT / "scripts" / "migrations"))
        cfg.set_main_option("sqlalchemy.url", f"sqlite:///{old / 'store.db'}")
        cfg.attributes["metadata"] = store.Base.metadata
        store.command.upgrade(cfg, "0001")
        with redirect_stdout(io.StringIO()):
            self.assertEqual(performance_report.main(["--root", str(old), "--since", "2026-10-09"]), 0)

    def test_the_router_runs_it_as_performance(self) -> None:
        sys.path.insert(0, str(ROOT))
        import chief_of_stuff
        self.assertEqual(chief_of_stuff.COMMANDS.get("performance"), "scripts/performance_report.py")


if __name__ == "__main__":
    unittest.main()
