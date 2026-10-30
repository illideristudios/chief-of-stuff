"""The engine's one clock: injectable, defaulting to aware UTC wall time (1.0 plan, M3 item 8)."""

from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import clock
import store

KEY = "github:example/repo#12"
PINNED = datetime(2026, 10, 11, 19, 0, 0, tzinfo=timezone.utc)


class ClockTest(unittest.TestCase):
    def test_system_returns_aware_utc(self) -> None:
        now = clock.system()
        self.assertIsNotNone(now.tzinfo)
        self.assertEqual(now.utcoffset(), timedelta(0))

    def test_store_timestamps_come_from_the_injected_clock(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "store.db"
            store.migrate(path)
            s = store.Store(path, clock=lambda: PINNED)
            self.addCleanup(s.engine.dispose)
            s.upsert_item(KEY, title="x")
            self.assertEqual(s.items()[0].updated_at, PINNED.isoformat())
            s.set_human_review(KEY, True)
            self.assertEqual(s.items()[0].updated_at, PINNED.isoformat())
            s.add_decision(KEY, topic="t", question="q?")
            self.assertEqual(s.decisions(KEY)[0].created_at, PINNED.isoformat())


if __name__ == "__main__":
    unittest.main()
