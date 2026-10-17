"""Runtime limits: Claude readings, account-wide history, and the engine pass (§10)."""
from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
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
import session_logs
import settings
import store

NOW = datetime(2026, 10, 16, 12, tzinfo=timezone.utc)


def reading(used=40, **kw):
    return replace(limits.Reading("codex", "", "codex/primary/300m", used, 100, "%",
                                  NOW + timedelta(hours=2), NOW, ""), **kw)


class Judgements(unittest.TestCase):
    def test_any_limit_for_one_account_can_be_near_cap(self):
        rows = [reading(30, limit="five_hour", read_at=NOW - timedelta(hours=1)),
                reading(60, limit="five_hour"), reading(10, limit="seven_day"),
                reading(None, limit="limits", cap=None, unknown="unreadable")]
        self.assertTrue(limits.near_cap(rows, NOW, .8))
        self.assertTrue(limits.near_cap([reading(85, limit="five_hour"),
                                        reading(10, limit="seven_day")], NOW, .8))
        self.assertFalse(limits.near_cap(rows + [reading(90, account="other")], NOW, .8))
        self.assertFalse(limits.near_cap(rows + [reading(90, runtime="claude")], NOW, .8))
        self.assertFalse(limits.near_cap([reading(10, limit="five_hour"),
                                         reading(10, limit="seven_day")], NOW, .8))

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
        self.env = patch.dict(os.environ, {"HOME": str(self.home),
                                          "CLAUDE_CONFIG_DIR": str(self.home / ".claude"),
                                          "CLAUDE_AS_PROFILES_DIR": str(self.home / "profiles")})
        self.env.start()
        self.addCleanup(self.env.stop)

    def write(self, path, events):
        path = self.home / path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{broken\n" + "\n".join(json.dumps(e) for e in events) + "\n{partial")
        return path

    def test_codex_cursor_and_agy_stay_unread_until_1_1(self):
        self.write(".codex/sessions/session.jsonl", [{"type": "event_msg", "payload": {
            "type": "token_count", "rate_limits": {"primary": {"used_percent": 40}}}}])
        for runtime in ("codex", "cursor", "agy"):
            row = limits_probe.read(runtime, "acct")[0]
            self.assertEqual((row.runtime, row.account, row.unknown, row.used, row.cap),
                             (runtime, "acct", "read in 1.1", None, None))

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

    def test_an_unchanged_transcript_is_not_mapped_again(self):
        event = {"type": "rate_limit_event", "timestamp": NOW.isoformat(), "rate_limit_info": {
            "rateLimitType": "five_hour", "utilization": .85,
            "resetsAt": (NOW + timedelta(hours=2)).timestamp()}}
        self.write(".claude/projects/work/session.jsonl", [event])
        import mmap
        with patch("limits_probe._now", return_value=NOW), \
                patch("session_logs.mmap.mmap", wraps=mmap.mmap) as mapped:
            first = limits_probe.read("claude", "")
            scans = mapped.call_count
            second = limits_probe.read("claude", "")
            self.assertEqual(scans, 1)
            self.assertEqual(mapped.call_count, scans)
        self.assertEqual(first[0].used, second[0].used)
        later = dict(event, rate_limit_info={
            "rateLimitType": "five_hour", "utilization": .1,
            "resetsAt": (NOW + timedelta(hours=2)).timestamp()})
        self.write(".claude/projects/work/session.jsonl", [later])
        with patch("limits_probe._now", return_value=NOW), \
                patch("session_logs.mmap.mmap", wraps=mmap.mmap) as mapped:
            third = limits_probe.read("claude", "")
            self.assertEqual(mapped.call_count, 1)
        self.assertEqual(third[0].used, 10)

    def test_limits_and_workers_share_decoded_transcript_records(self):
        import workers_page
        tree = Path.home() / "work"
        import re
        project = re.sub(r"[^A-Za-z0-9]", "-", str(tree.resolve()))
        self.write(f".claude/projects/{project}/session.jsonl", [
            {"type": "assistant", "timestamp": NOW.isoformat(), "message": {
                "id": "message", "usage": {"input_tokens": 12}}},
            {"type": "rate_limit_event", "timestamp": NOW.isoformat(), "rate_limit_info": {
                "rateLimitType": "five_hour", "utilization": .85,
                "resetsAt": (NOW + timedelta(hours=2)).timestamp()}}])
        import json
        with patch("limits_probe._now", return_value=NOW), patch("json.loads", wraps=json.loads) as decoded:
            self.assertEqual(limits_probe.read("claude", "")[0].used, 85)
            scans = decoded.call_count
            self.assertEqual(workers_page.activity(tree), (NOW, 12))
            self.assertEqual(decoded.call_count, scans)

    def test_unknown_event_keeps_an_unexpired_numeric_limit(self):
        known = {"type": "rate_limit_event", "timestamp": (NOW - timedelta(minutes=2)).isoformat(),
                 "rate_limit_info": {"rateLimitType": "five_hour", "utilization": .85,
                                     "resetsAt": (NOW + timedelta(hours=2)).timestamp()}}
        unknown = dict(known, timestamp=NOW.isoformat(), rate_limit_info={"rateLimitType": "five_hour"})
        self.write(".claude/projects/work/session.jsonl", [known, unknown])
        for events in ([known, unknown], [unknown, known]):
            self.write(".claude/projects/work/session.jsonl", events)
            with patch("limits_probe._now", return_value=NOW):
                rows = limits_probe.read("claude", "")
            self.assertEqual(rows[0].used, 85)
            self.assertEqual(rows[0].read_at, NOW - timedelta(minutes=2))
            self.assertTrue(limits.near_cap(rows, NOW, .8))
        with patch("limits_probe._now", return_value=NOW + timedelta(hours=3)):
            rows = limits_probe.read("claude", "")
        self.assertTrue(rows[0].unknown)
        self.assertFalse(limits.near_cap(rows, NOW + timedelta(hours=3), .8))

    def test_unknown_never_invents_zero_or_reads_prompt_text(self):
        self.write(".claude/projects/work/session.jsonl", [{"type": "assistant", "timestamp": NOW.isoformat(),
                   "rate_limit_info": {"rateLimitType": "five_hour", "utilization": .4,
                                       "resetsAt": (NOW + timedelta(hours=2)).timestamp()},
                   "message": {"usage": {"input_tokens": 100}, "content": "rate_limit_info utilization .4"}}])
        for runtime in ("claude", "codex", "cursor", "agy"):
            with patch("limits_probe._now", return_value=NOW):
                r = limits_probe.read(runtime, "")[0]
            self.assertIsNone(r.used)
            self.assertIsNone(r.cap)
            self.assertTrue(r.unknown)
            if runtime != "claude":
                self.assertEqual(r.unknown, "read in 1.1")

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

    def test_read_all_uses_one_account_and_keeps_going_after_a_failed_runtime(self):
        workers = settings.Workers(claude_as_profile="secondary")

        def fake(runtime, account):
            if runtime == "claude":
                raise OSError("bad log")
            return [reading(runtime=runtime, account=account, used=None, cap=None, unknown="read in 1.1")]

        with patch("limits_probe.read", side_effect=fake) as probe:
            rows = limits_probe.read_all(workers)
        self.assertEqual([c.args for c in probe.call_args_list],
                         [(runtime, "secondary" if runtime == "claude" else "")
                          for runtime in ("claude", "codex", "cursor", "agy")])
        claude = next(row for row in rows if row.runtime == "claude")
        self.assertIsNone(claude.used)
        self.assertIn("bad log", claude.unknown)
        self.assertEqual({row.runtime for row in rows}, {"claude", "codex", "cursor", "agy"})


class History(unittest.TestCase):
    def test_repeated_unknown_reason_is_one_observation_until_status_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = limits_history.LimitsHistory(Path(tmp) / "limits.sqlite")
            self.addCleanup(db.engine.dispose)
            unknown = reading(None, cap=None, resets_at=None, unknown="read in 1.1")
            db.record([unknown])
            db.record([replace(unknown, read_at=NOW + timedelta(minutes=1))])
            self.assertEqual(db.readings("codex", "", NOW), [unknown])
            changed = replace(unknown, unknown="unreadable", read_at=NOW + timedelta(minutes=2))
            known = reading(20, read_at=NOW + timedelta(minutes=3))
            returned = replace(unknown, read_at=NOW + timedelta(minutes=4))
            db.record([changed, known, returned, replace(returned, read_at=NOW + timedelta(minutes=5))])
            self.assertEqual(db.readings("codex", "", NOW), [unknown, changed, known, returned])
            other_account = replace(unknown, account="other")
            other_limit = replace(unknown, limit="weekly")
            other_runtime = replace(unknown, runtime="claude")
            db.record([other_account, other_limit, other_runtime])
            self.assertEqual(db.readings("codex", "other", NOW), [other_account])
            self.assertIn(other_limit, db.readings("codex", "", NOW))
            self.assertEqual(db.readings("claude", "", NOW), [other_runtime])

    def test_a_skipped_unknown_leaves_no_write_lock_for_another_writer(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "limits.sqlite"
            a, b = limits_history.LimitsHistory(path), limits_history.LimitsHistory(path)
            self.addCleanup(a.engine.dispose)
            self.addCleanup(b.engine.dispose)
            unknown = reading(None, cap=None, resets_at=None, unknown="read in 1.1")
            a.record([unknown])
            a.record([replace(unknown, read_at=NOW + timedelta(minutes=1))])
            with b.engine.connect() as conn:
                conn.connection.driver_connection.execute("PRAGMA busy_timeout=100")
            known = reading(20, read_at=NOW + timedelta(minutes=2))
            b.record([known])
            self.assertEqual(b.readings("codex", "", NOW), [unknown, known])

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
            with self.assertRaises(ValueError):
                db.readings("codex", "", NOW.replace(tzinfo=None))
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

    def test_cli_records_one_reading_for_the_workspace_account(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "CLAUDE.md").write_text("## Coordinator\n- Settings: `settings.toml`\n")
            (root / "settings.toml").write_text('[workers]\nclaude_as_profile = "secondary"\n')
            rows = [reading(None, runtime=runtime, account="secondary" if runtime == "claude" else "",
                            cap=None, unknown="read in 1.1")
                    for runtime in ("claude", "codex", "cursor", "agy")]
            output = io.StringIO()
            with patch("limits_cli.read_all", return_value=rows) as probe, \
                    patch("limits_cli.LimitsHistory") as history, redirect_stdout(output):
                self.assertEqual(limits_cli.main(["--root", tmp]), 0)
            probe.assert_called_once()
            self.assertEqual(probe.call_args.args[0].claude_as_profile, "secondary")
            self.assertEqual(history.return_value.record.call_args.args[0], rows)
            self.assertIn("unknown", output.getvalue())

    def test_engine_pass_records_one_reading_and_a_failure_does_not_stop_it(self):
        import engine
        from test_engine import FakeForge, policies
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "CLAUDE.md").write_text("## Coordinator\n- Settings: `settings.toml`\n")
            config_path = root / "settings.toml"
            config_path.write_text('[workers]\nclaude_as_profile = "secondary"\n')
            workers = settings.load(root, "settings.toml").workers
            # Collection must use the same frozen account as worker launch, even after an edit.
            config_path.write_text('[workers]\nclaude_as_profile = "other"\n')
            db_path = root / "store.db"
            store.migrate(db_path)
            db = store.Store(db_path)
            self.addCleanup(db.engine.dispose)
            trees = root / "trees"
            trees.mkdir()
            rows = [reading(runtime="claude", account="secondary")]

            def run():
                with patch.object(engine.worker, "collect"), patch.object(engine.cleanup, "execute", return_value=0):
                    return engine.run_pass(forge=FakeForge(), store=db, policies=policies(), root=root,
                                           trees=trees, workers=workers, launch=lambda *_args: ("model", "high"))

            with patch("limits_probe.read_all", return_value=rows) as probe, \
                    patch("limits_history.LimitsHistory") as history:
                actions = run()
            probe.assert_called_once()
            self.assertEqual(probe.call_args.args[0].claude_as_profile, "secondary")
            history.return_value.record.assert_called_once_with(rows)
            self.assertIsInstance(actions, list)
            errors = io.StringIO()
            with patch("limits_history.LimitsHistory", side_effect=OSError("database unavailable")), redirect_stderr(errors):
                self.assertIsInstance(run(), list)
            self.assertIn("engine: limits reading failed: database unavailable", errors.getvalue())


if __name__ == "__main__":
    unittest.main()
