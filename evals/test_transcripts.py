"""Generic worker records stay with their exact attempt across restarts and damaged logs."""
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path

from alembic import command
from sqlalchemy import inspect

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import store
import transcripts
import claude_adapter

KEY = "github:example/repo#12"


class TranscriptsTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.path = self.root / "store.db"
        store.migrate(self.path)
        self.db = store.Store(self.path)
        self.addCleanup(self.db.engine.dispose)
        self.db.upsert_item(KEY)
        for stage, attempt in (("implement", 1), ("implement", 2), ("review", 1)):
            self.db.record_segment(KEY, stage, attempt, "start")
        self.log = self.root / "session.jsonl"

    def line(self, kind="assistant", content="A generic reply", session="session-one", **extra):
        return json.dumps({"type": kind, "sessionId": session, "timestamp": "time",
                           "message": {"content": content}, **extra}) + "\n"

    def capture(self, **kwargs):
        transcripts.capture(self.db, KEY, "implement", 1, "session-one", self.log, **kwargs)
        return self.db.transcript(KEY, "implement", 1)

    def test_ordered_events_survive_exit_restart_and_source_removal(self):
        self.log.write_text(self.line("user", "Generic request") + self.line(content=[
            {"type": "text", "text": "Working"}, {"type": "tool_use", "id": "tool-one", "name": "Read", "input": {}}])
            + self.line("user", [{"type": "tool_result", "tool_use_id": "tool-one", "content": "Generic result"}])
            + self.line("assistant", "Generic failure", isApiErrorMessage=True))
        got = self.capture(ended=True)
        self.assertEqual([e["kind"] for e in got["events"]], ["user", "worker", "tool", "tool", "error"])
        self.assertEqual([e["sequence"] for e in got["events"]], list(range(1, 6)))
        self.db.end_segment(KEY, "implement", 1, "end", "failed")
        self.log.unlink()
        other = store.Store(self.path)
        self.addCleanup(other.engine.dispose)
        self.assertEqual(other.transcript(KEY, "implement", 1), got)
        self.assertIsNone(other.transcript(KEY, "implement", 2))
        self.assertIsNone(other.transcript(KEY, "review", 1))

    def test_partial_line_is_retried_after_restart_without_duplicates(self):
        line = self.line(content="Second reply")
        self.log.write_text(self.line() + line[:25])
        self.assertEqual(len(self.capture()["events"]), 1)
        self.db.engine.dispose()
        self.db = store.Store(self.path)
        self.addCleanup(self.db.engine.dispose)
        with self.log.open("a") as f:
            f.write(line[25:])
        self.assertEqual(len(self.capture()["events"]), 2)
        self.assertEqual(len(self.capture()["events"]), 2)

    def test_missing_log_marks_one_gap_and_recovers_only_the_bound_source(self):
        got = self.capture()
        self.assertIn("unavailable", got["last_gap"])
        self.assertEqual(self.capture()["gap_count"], 1)
        self.log.write_text(self.line())
        self.assertEqual([e["kind"] for e in self.capture()["events"]], ["gap", "worker"])

    def test_rotation_truncation_and_same_inode_rewrite_halt_capture(self):
        for damage in ("replace", "truncate", "rewrite"):
            with self.subTest(damage=damage):
                self.log.write_text(self.line())
                self.capture()
                if damage == "replace":
                    self.log.rename(self.root / "old.jsonl")
                self.log.write_text("" if damage == "truncate" else self.line(content="Other generic text"))
                got = self.capture()
                self.assertTrue(got["halted"])
                self.assertIn("changed", got["last_gap"])
                self.assertEqual(sum(e["kind"] == "worker" for e in got["events"]), 1)
                with self.db.session() as s:
                    s.query(store.Transcript).delete()

    def test_foreign_session_and_rebound_attempt_never_capture_its_content(self):
        self.log.write_text(self.line() + self.line(content="Foreign reply", session="session-two"))
        got = self.capture()
        self.assertTrue(got["halted"])
        self.assertNotIn("Foreign reply", json.dumps(got))
        transcripts.capture(self.db, KEY, "implement", 1, "session-two", self.root / "other.jsonl")
        self.assertEqual(self.db.transcript(KEY, "implement", 1)["session_id"], "session-one")
        with self.assertRaisesRegex(ValueError, "already bound"):
            transcripts.capture(self.db, KEY, "implement", 2, "session-two", self.log)

    def test_malformed_and_final_partial_records_have_explicit_gaps(self):
        self.log.write_text("bad json\n" + self.line() + '{"type":')
        got = self.capture(ended=True)
        self.assertEqual([e["kind"] for e in got["events"]], ["gap", "worker", "gap"])
        self.assertIn("partial", got["last_gap"])
        self.assertEqual(self.capture(ended=True)["gap_count"], 2)

    def test_metadata_without_a_session_does_not_hide_worker_or_system_errors(self):
        self.log.write_text(json.dumps({"type": "file-history-snapshot", "snapshot": {}}) + "\n"
                            + self.line() + self.line("system", "", error="Generic transport failure"))
        got = self.capture()
        self.assertFalse(got["halted"])
        self.assertEqual([e["kind"] for e in got["events"]], ["worker", "error"])

    def test_symlink_is_an_explicit_unreadable_source(self):
        target = self.root / "other.jsonl"
        target.write_text(self.line(content="Other content"))
        self.log.symlink_to(target)
        got = self.capture()
        self.assertIn("unavailable", got["last_gap"])
        self.assertNotIn("Other content", json.dumps(got))

    def test_repeated_missing_source_after_cap_does_not_inflate_drops_or_gaps(self):
        self.log.write_text(self.line() * 10)
        self.capture(max_bytes=200)
        self.log.unlink()
        got = self.capture()
        repeated = self.capture()
        self.assertEqual(repeated["gap_count"], got["gap_count"])
        self.assertEqual(repeated["dropped_events"], got["dropped_events"])

    def test_concurrent_captures_commit_events_and_cursor_together(self):
        self.log.write_text(self.line() * 10)
        errors = []

        def run():
            db = store.Store(self.path)
            try:
                transcripts.capture(db, KEY, "implement", 1, "session-one", self.log)
            except Exception as exc:
                errors.append(exc)
            finally:
                db.engine.dispose()

        threads = [threading.Thread(target=run) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        self.assertEqual(len(self.db.transcript(KEY, "implement", 1)["events"]), 10)

    def test_cap_keeps_prefix_counts_dropped_events_and_bounds_oversized_lines(self):
        self.log.write_text(self.line() * 5 + self.line(content="x" * 100_000))
        got = self.capture(max_bytes=300)
        self.assertLessEqual(got["retained_bytes"], 300)
        self.assertGreater(got["dropped_events"], 0)
        self.assertTrue(got["capped"])
        self.assertEqual(got["dropped_events"], self.capture(max_bytes=300)["dropped_events"])
        self.assertIn("retention", got["last_gap"])

    def test_cap_accounts_for_the_orm_json_encoding_and_is_pinned_per_attempt(self):
        self.log.write_text(self.line(content="Generic \u2603 text") * 10)
        got = self.capture(max_bytes=600)
        actual = len(json.dumps(got["events"]).encode("utf-8"))
        self.assertLessEqual(actual, 600)
        self.assertEqual(got["retained_bytes"], actual)
        self.assertEqual(self.capture(max_bytes=10000)["max_bytes"], 600)

    def test_oversized_records_have_a_separate_count_when_event_count_is_unknown(self):
        self.log.write_text(self.line(content="x" * 10000))
        got = self.capture(max_bytes=1024)
        self.assertEqual(got["dropped_records"], 1)
        self.assertEqual(got["dropped_events"], 0)
        self.assertEqual(got["events"], [])

    def test_final_partial_can_complete_later_and_does_not_consume_the_record(self):
        line = self.line()
        self.log.write_text(line[:-2])
        got = self.capture(ended=True)
        self.assertEqual(got["cursor"]["offset"], 0)
        with self.log.open("a") as f:
            f.write(line[-2:])
        got = self.capture(ended=True)
        self.assertEqual([e["kind"] for e in got["events"]], ["gap", "worker"])

    def test_events_without_session_identity_halt_instead_of_guessing(self):
        self.log.write_text(json.dumps({"type": "assistant", "message": {"content": "Unbound reply"}}) + "\n")
        got = self.capture()
        self.assertTrue(got["halted"])
        self.assertNotIn("Unbound reply", json.dumps(got))

    def test_numbered_migration_downgrades_and_upgrades_without_losing_segments(self):
        self.log.write_text(self.line())
        self.capture()
        self.db.engine.dispose()
        command.downgrade(store._config(self.path), "0012")
        self.assertNotIn("transcript", inspect(self.db.engine).get_table_names())
        self.assertEqual(len(self.db.segments(KEY)), 3)
        store.migrate(self.path)
        self.assertIn("transcript", inspect(self.db.engine).get_table_names())

    def test_adapter_uses_saved_launch_config_and_checks_exact_session_and_tree(self):
        run = self.root / "run"
        run.mkdir()
        tree = self.root / "tree"
        record = {"run_dir": str(run), "tree": str(tree), "session_id": "session-one"}
        (run / "command.json").write_text(json.dumps({"session_id": "session-one", "cwd": str(tree),
            "env": {"CLAUDE_CONFIG_DIR": str(self.root / "profile")}}))
        path = claude_adapter.transcript_path(record)
        self.assertEqual(path.name, "session-one.jsonl")
        self.assertTrue(path.is_relative_to(self.root / "profile" / "projects"))
        record["session_id"] = "session-two"
        with self.assertRaisesRegex(ValueError, "identity"):
            claude_adapter.transcript_path(record)

    def test_adapter_handles_new_config_dir_field_and_malformed_launch_records(self):
        run = self.root / "run"
        run.mkdir()
        record = {"run_dir": str(run), "tree": str(self.root / "tree"), "session_id": "session-one"}
        prepared = {"session_id": "session-one", "cwd": record["tree"], "config_dir": str(self.root / "saved")}
        (run / "command.json").write_text(json.dumps(prepared))
        self.assertTrue(claude_adapter.transcript_path(record).is_relative_to(self.root / "saved"))
        for value in (None, [], {**prepared, "cwd": None}, {**prepared, "config_dir": None}):
            (run / "command.json").write_text(json.dumps(value))
            with self.subTest(value=value), self.assertRaises((ValueError, KeyError, TypeError)):
                claude_adapter.transcript_path(record)
