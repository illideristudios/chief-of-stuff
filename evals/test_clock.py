"""The engine's one clock: injectable, defaulting to aware UTC wall time (1.0 plan, M3 item 8)."""

from __future__ import annotations

import json
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
DATETIME_INJECT = {"name": "datetime-inject", "marketplace": "ai-additions", "version": "^1.0"}


class ClockDependencyTest(unittest.TestCase):
    def test_plugin_manifest_declares_datetime_inject(self) -> None:
        manifest = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
        self.assertIn(DATETIME_INJECT, manifest.get("dependencies", []))

    def test_marketplace_manifest_declares_datetime_inject_and_cross_marketplace_allowlist(self) -> None:
        marketplace = json.loads((ROOT / ".claude-plugin" / "marketplace.json").read_text(encoding="utf-8"))
        self.assertIn("ai-additions", marketplace.get("allowCrossMarketplaceDependenciesOn", []))
        plugins = {entry["name"]: entry for entry in marketplace.get("plugins", [])}
        self.assertIn("chief-of-stuff", plugins)
        self.assertIn(DATETIME_INJECT, plugins["chief-of-stuff"].get("dependencies", []))


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
