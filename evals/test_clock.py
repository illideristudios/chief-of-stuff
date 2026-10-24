"""The engine's one clock: injectable, defaulting to aware UTC wall time (1.0 plan, M3 item 8)."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import clock
import store
from evals.live import clock_delivery
from model_port import Reply

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

    def test_user_clock_matches_only_enabled_user_scoped_datetime_inject(self) -> None:
        fixture = json.loads((ROOT / "evals" / "fixtures" / "claude-plugin-list-user.json").read_text(encoding="utf-8"))
        matched = clock.user_clock(fixture)
        self.assertIsNotNone(matched)
        self.assertEqual(matched["id"], "datetime-inject@ai-additions")
        for bad in (
            None,
            {},
            [],
            ["not-a-dict"],
            [{"id": "datetime-inject@ai-additions", "scope": "user", "enabled": False}],
            [{"id": "datetime-inject@ai-additions", "scope": "user", "enabled": 1}],
            [{"id": "datetime-inject@ai-additions", "scope": "project", "enabled": True}],
            [{"id": "datetime-inject@other", "scope": "user", "enabled": True}],
        ):
            with self.subTest(bad=bad):
                self.assertIsNone(clock.user_clock(bad))

    def test_live_clock_delivery_harness_validates_advancing_stamps_and_requires_opt_in(self) -> None:
        case = clock_delivery.ClockDeliveryLiveTest("test_worker_receives_advancing_datetime_inject_timestamps")
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("CHIEF_CLOCK_LIVE", None)
            with self.assertRaisesRegex(AssertionError, "CHIEF_CLOCK_LIVE=1"):
                case.test_worker_receives_advancing_datetime_inject_timestamps()

        t1 = datetime(2026, 10, 24, 14, 30, 0, tzinfo=timezone.utc)
        t2 = datetime(2026, 10, 24, 14, 30, 2, tzinfo=timezone.utc)
        replies = [
            Reply(
                text="",
                data={"lines": ["[Current TimeStamp: Saturday | 2026-10-24 14:30:00 UTC (UTC+00:00)]"]},
                session_id="s1",
                cost_usd=0.0,
                num_turns=1,
            ),
            Reply(
                text="",
                data={"lines": ["[Current TimeStamp: Saturday | 2026-10-24 14:30:02 UTC (UTC+00:00)]"]},
                session_id="s2",
                cost_usd=0.0,
                num_turns=1,
            ),
        ]
        clocks = iter([t1, t1, t1, t2, t2, t2])
        with mock.patch.dict(os.environ, {"CHIEF_CLOCK_LIVE": "1", "CHIEF_CLOCK_PROFILE": "work"}), \
             mock.patch.object(clock_delivery.clock, "system", side_effect=lambda: next(clocks)), \
             mock.patch.object(clock_delivery.time, "sleep"), \
             mock.patch.object(clock_delivery.claude_adapter, "require_worker_clock") as check_clock, \
             mock.patch.object(clock_delivery.claude_adapter, "call", side_effect=replies) as call, \
             mock.patch.object(clock_delivery.claude_adapter, "forget_session"):
            case.test_worker_receives_advancing_datetime_inject_timestamps()
            case.doCleanups()
        check_clock.assert_called_once()
        self.assertEqual(check_clock.call_args.args[0], "work")
        self.assertEqual(call.call_count, 2)
        self.assertTrue(call.call_args.args[0].worker)


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
