"""Runtime limits: unbilled sources, account-wide history and board warnings (§10)."""
from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import limits
import limits_cli
import limits_history
import limits_probe
import render_board
import settings
import store

NOW = datetime(2026, 10, 16, 12, tzinfo=timezone.utc)


def reading(used=40, **kw):
    return replace(limits.Reading("codex", "", "codex/primary/300m", used, 100, "%",
                                  NOW + timedelta(hours=2), NOW, ""), **kw)


class Judgements(unittest.TestCase):
    def test_burn_and_projection_use_elapsed_hours(self):
        rows = [reading(20, read_at=NOW - timedelta(hours=1)), reading(40)]
        self.assertEqual(limits.burn_rate(rows[::-1]), 20)
        self.assertTrue(limits.near_cap(rows, NOW, .8))
        self.assertFalse(limits.near_cap(rows, NOW, .9))

    def test_reset_account_limit_and_unknown_are_never_mixed(self):
        rows = [reading(99, resets_at=NOW - timedelta(minutes=1), read_at=NOW - timedelta(hours=1)),
                reading(10)]
        self.assertIsNone(limits.burn_rate(rows))
        self.assertIsNone(limits.burn_rate([reading(10, account="other"), reading(20)]))
        self.assertIsNone(limits.burn_rate([reading(10, limit="weekly"), reading(20)]))
        self.assertFalse(limits.near_cap([reading(99, resets_at=NOW)], NOW, .8))
        self.assertFalse(limits.near_cap([reading(99), reading(None, unknown="unreadable",
                                            read_at=NOW + timedelta(seconds=1))], NOW, .8))
        self.assertTrue(limits.near_cap([reading(80)], NOW, .8))
        self.assertIsNone(limits.burn_rate([reading(50), reading(40, read_at=NOW + timedelta(minutes=1))]))


class Sources(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.env = patch.dict(os.environ, {"HOME": str(self.home), "CODEX_HOME": str(self.home / ".codex"),
                                          "CLAUDE_CONFIG_DIR": str(self.home / ".claude"),
                                          "CLAUDE_AS_PROFILES_DIR": str(self.home / "profiles")})
        self.env.start()
        self.addCleanup(self.env.stop)

    def write(self, path, events):
        path = self.home / path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{broken\n" + "\n".join(json.dumps(e) for e in events) + "\n{partial")
        return path

    def codex(self, at, used, name="codex"):
        return {"type": "event_msg", "timestamp": at.isoformat(), "payload": {
            "type": "token_count", "rate_limits": {"limit_id": name,
                "primary": {"used_percent": used, "window_minutes": 300,
                            "resets_at": (NOW + timedelta(hours=2)).timestamp()},
                "secondary": {"used_percent": 31, "window_minutes": 10080,
                              "resets_at": (NOW + timedelta(days=3)).timestamp()}}}}

    def test_codex_latest_event_per_model_window_not_file_order(self):
        self.write(".codex/sessions/older.jsonl", [self.codex(NOW, 40)])
        self.write(".codex/sessions/2026/10/newer.jsonl", [self.codex(NOW - timedelta(hours=1), 10),
                                                          self.codex(NOW, 55, "model-x")])
        with patch("limits_probe._now", return_value=NOW):
            rows = limits_probe.read("codex", "")
        values = {r.limit: r for r in rows}
        self.assertEqual(values["codex/primary/300m"].used, 40)
        self.assertEqual(values["model-x/primary/300m"].used, 55)
        self.assertEqual(values["codex/secondary/10080m"].cap, 100)
        self.assertEqual(values["codex/primary/300m"].read_at, NOW)
        self.assertTrue(all(r.account == "" for r in rows))

    def test_claude_structured_transcript_and_profile_isolation(self):
        event = {"type": "rate_limit_event", "timestamp": NOW.isoformat(), "rate_limit_info": {
            "rateLimitType": "five_hour", "utilization": .85,
            "resetsAt": (NOW + timedelta(hours=2)).timestamp()}}
        self.write(".claude/projects/default/session.jsonl", [event])
        self.write("profiles/secondary/projects/work/session.jsonl", [dict(event, rate_limit_info={
            "rateLimitType": "seven_day_opus", "utilization": .42,
            "resetsAt": (NOW + timedelta(days=3)).timestamp()})])
        with patch("limits_probe._now", return_value=NOW):
            rows = limits_probe.read("claude", "secondary")
            main = limits_probe.read("claude", "")
        self.assertEqual([(r.limit, r.used, r.account) for r in rows], [("seven_day_opus", 42, "secondary")])
        self.assertEqual(main[0].used, 85)
        self.assertTrue(limits_probe.read("claude", "missing")[0].unknown)
        self.assertTrue(limits_probe.read("claude", "../secondary")[0].unknown)

    def test_unknown_never_invents_zero_or_reads_prompt_text(self):
        self.write(".claude/projects/work/session.jsonl", [{"type": "assistant", "timestamp": NOW.isoformat(),
                   "message": {"usage": {"input_tokens": 100}, "content": "rate_limit_info utilization .4"}}])
        for runtime in ("claude", "codex", "cursor", "agy"):
            r = limits_probe.read(runtime, "")[0]
            self.assertIsNone(r.used)
            self.assertIsNone(r.cap)
            self.assertTrue(r.unknown)
            if runtime in ("cursor", "agy"):
                self.assertIn("spike", r.unknown.lower())

    def test_claude_native_cap_hit_uses_error_timestamp_and_local_reset_clock(self):
        stamp = NOW.replace(hour=1)
        event = {"type": "assistant", "isApiErrorMessage": True, "error": "rate_limit",
                 "timestamp": stamp.isoformat(), "message": {"content": [{"type": "text",
                     "text": "You've hit your session limit · resets 9:40pm (America/Chicago)"}]}}
        self.write(".claude/projects/work/session.jsonl", [event])
        with patch("limits_probe._now", return_value=stamp + timedelta(minutes=1)):
            r = limits_probe.read("claude", "")[0]
        self.assertEqual((r.limit, r.used, r.cap), ("session", 100, 100))
        self.assertEqual(r.read_at, stamp)
        self.assertEqual(r.resets_at, stamp.replace(hour=2, minute=40))
        with patch("limits_probe._now", return_value=stamp + timedelta(days=1)):
            r = limits_probe.read("claude", "")[0]
        self.assertIsNone(r.used)
        self.assertTrue(r.unknown)

    def test_expired_and_malformed_readings_are_unknown(self):
        bad = self.codex(NOW, float("nan"))
        old = self.codex(NOW - timedelta(days=8), 60)
        for window in old["payload"]["rate_limits"].values():
            if isinstance(window, dict): window["resets_at"] = (NOW - timedelta(days=1)).timestamp()
        self.write(".codex/sessions/session.jsonl", [old, bad])
        with patch("limits_probe._now", return_value=NOW):
            rows = limits_probe.read("codex", "")
        self.assertTrue(any(r.unknown and r.used is None for r in rows))


class History(unittest.TestCase):
    def test_separate_migrations_account_queries_and_source_dedup(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state/limits.sqlite"
            a = limits_history.LimitsHistory(path)
            b = limits_history.LimitsHistory(path)
            self.addCleanup(a.engine.dispose)
            self.addCleanup(b.engine.dispose)
            self.assertEqual(a.readings("codex", "", NOW), [])
            a.record([reading(10, read_at=NOW - timedelta(hours=1)), reading(20),
                      reading(90, account="other"), reading(15, runtime="claude")])
            b.record([reading(20)])
            self.assertEqual([r.used for r in b.readings("codex", "", NOW - timedelta(days=1))], [10, 20])
            self.assertEqual([r.used for r in a.readings("codex", "other", NOW)], [90])
            self.assertEqual([r.used for r in a.readings("claude", "", NOW)], [15])
            from sqlalchemy import inspect
            self.assertNotIn("work_item", inspect(a.engine).get_table_names())
            issue_path = Path(tmp) / "store.db"
            store.migrate(issue_path)
            db = store.Store(issue_path)
            self.addCleanup(db.engine.dispose)
            self.assertNotIn("limit_reading", inspect(db.engine).get_table_names())

    def test_default_path_uses_xdg_state(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"XDG_STATE_HOME": tmp}):
            self.assertEqual(limits_history.history_path(), Path(tmp) / "chief-of-stuff/limits.sqlite")

    def test_since_is_inclusive_and_normalizes_timezones(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = limits_history.LimitsHistory(Path(tmp) / "limits.sqlite")
            self.addCleanup(db.engine.dispose)
            db.record([reading(30, read_at=NOW.astimezone(timezone(timedelta(hours=-5))))])
            self.assertEqual(db.readings("codex", "", NOW), [reading(30)])
            self.assertEqual(db.readings("codex", "", NOW + timedelta(seconds=1)), [])


class Integration(unittest.TestCase):
    def test_settings_default_valid_and_invalid_thresholds(self):
        self.assertEqual(settings.Settings().limits.warn_at, .8)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for value in ("-0.1", "1.1", "true", '"0.8"', "nan", "inf"):
                (root / "settings.toml").write_text("[limits]\nwarn_at = " + value)
                with self.assertRaises(settings.SettingsError): settings.load(root, "settings.toml")
            (root / "settings.toml").write_text("[limits]\nwarn_at = 0.6")
            self.assertEqual(settings.load(root, "settings.toml").limits.warn_at, .6)

    def test_board_shows_unknown_reset_daily_burn_and_warning(self):
        rows = [reading(20, read_at=NOW - timedelta(hours=1)), reading(40),
                reading(None, runtime="cursor", limit="limits", cap=None, unknown="spike looking for source")]
        panel = render_board.limits_panel(rows, NOW, .8)
        html = render_board.panels.panels([panel])
        for text in ("RUNTIME LIMITS", "codex", "40", "100", "20", "reset", "near cap", "cursor", "unknown", "spike"):
            self.assertIn(text, html)

    def test_board_warning_uses_setting_and_escapes_unknown_reason(self):
        rows = [reading(70), reading(None, runtime="cursor", limit="limits", cap=None, unknown="<spike>")]
        low = render_board.panels.panels([render_board.limits_panel(rows, NOW, .6)])
        high = render_board.panels.panels([render_board.limits_panel(rows, NOW, .9)])
        self.assertIn("near cap", low)
        self.assertNotIn("near cap", high)
        self.assertNotIn("<spike>", high)
        self.assertIn("&lt;spike&gt;", high)

    def test_source_failure_and_recovery_supersede_stale_board_values(self):
        failure = reading(None, limit="limits", cap=None, unknown="unreadable source", read_at=NOW)
        older = reading(90, read_at=NOW - timedelta(minutes=1))
        panel = render_board.limits_panel([older, failure], NOW, .8)
        codex = [r for r in panel.rows if "codex" in r.name]
        self.assertEqual(len(codex), 1)
        self.assertIn("unreadable source", codex[0].facts)
        panel = render_board.limits_panel([failure, reading(40, read_at=NOW + timedelta(minutes=1))],
                                         NOW + timedelta(minutes=2), .8)
        codex = [r for r in panel.rows if "codex" in r.name]
        self.assertEqual(len(codex), 1)
        self.assertIn("40/100", codex[0].facts)

    def test_board_write_queries_history_for_workspace_account_without_probing(self):
        from test_flow import make_workspace
        with tempfile.TemporaryDirectory() as tmp:
            root = make_workspace(Path(tmp), "## Tasks\n", '[workers]\nclaude_as_profile = "secondary"\n[limits]\nwarn_at = 0.6\n')
            with patch("limits_history.LimitsHistory") as history, patch("limits_probe.read") as probe:
                history.return_value.readings.return_value = []
                path, *_ = render_board.write(root, "2026-10-08")
            probe.assert_not_called()
            self.assertEqual([(c.args[0], c.args[1]) for c in history.return_value.readings.call_args_list],
                             [(r, "secondary" if r == "claude" else "") for r in ("claude", "codex", "cursor", "agy")])
            self.assertIn("RUNTIME LIMITS", path.read_text())

    def test_cli_collects_every_runtime_and_records_profile(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            from test_flow import SAMPLE_CLAUDE_MD
            (root / "CLAUDE.md").write_text(SAMPLE_CLAUDE_MD.replace("chief-of-stuff.toml", "settings.toml"))
            (root / "settings.toml").write_text('[workers]\nclaude_as_profile = "secondary"\n')
            output = io.StringIO()
            with patch("limits_cli.read", side_effect=lambda runtime, account: [
                    reading(None, runtime=runtime, account=account, cap=None, unknown="no local event")]) as probe, \
                    patch("limits_cli.LimitsHistory") as history, redirect_stdout(output):
                self.assertEqual(limits_cli.main(["--root", tmp]), 0)
            self.assertEqual(probe.call_args_list, [unittest.mock.call(r, "secondary" if r == "claude" else "")
                                                   for r in ("claude", "codex", "cursor", "agy")])
            self.assertEqual(len(history.return_value.record.call_args.args[0]), 4)
            self.assertIn("unknown", output.getvalue())

    def test_tick_records_all_accounts_and_survives_failure(self):
        import flow
        from test_flow import make_workspace
        with tempfile.TemporaryDirectory() as tmp:
            root = make_workspace(Path(tmp), "## Tasks\n", '[workers]\nclaude_as_profile = "secondary"\n')
            with patch("limits_probe.read", side_effect=[OSError("bad log"), [reading()], [], []]) as probe, \
                    patch("limits_history.LimitsHistory") as history, \
                    patch("flow.reconcile_dead_workers", return_value=["reconciled"]):
                events = flow.tick(root, NOW)
            self.assertIn("reconciled", events)
            self.assertEqual(probe.call_count, 4)
            self.assertEqual(probe.call_args_list[0], unittest.mock.call("claude", "secondary"))
            recorded = history.return_value.record.call_args.args[0]
            self.assertTrue(any(r.runtime == "claude" and r.unknown for r in recorded))
            with patch("limits_history.LimitsHistory", side_effect=OSError("database unavailable")), \
                    patch("flow.reconcile_dead_workers", return_value=["still reconciled"]):
                self.assertIn("still reconciled", flow.tick(root, NOW))


if __name__ == "__main__":
    unittest.main()
