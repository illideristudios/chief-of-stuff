"""The issue-keyed store: one row per forge issue, upsert-only writes, schema owned by migrations (1.0 plan, M1)."""

from __future__ import annotations

import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from contextlib import closing
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, select
from sqlalchemy.exc import IntegrityError

import store

KEY = "github:example/repo#12"


class StoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "store.db"
        store.migrate(self.path)
        self.store = store.Store(self.path)
        self.addCleanup(self.store.engine.dispose)

    def test_a_second_write_for_one_issue_updates_it_and_never_inserts(self) -> None:
        self.store.upsert_item(KEY, title="first", stage="ready")
        self.store.upsert_item(KEY, title="second")
        items = self.store.items()
        self.assertEqual([(i.issue_key, i.title, i.stage) for i in items], [(KEY, "second", "ready")])

    def test_the_database_itself_refuses_a_duplicate_issue_row(self) -> None:
        # Not just the upsert helper: a raw insert of the same key must fail too (S2 at the data layer).
        self.store.upsert_item(KEY, title="x")
        with self.assertRaises(IntegrityError), self.store.session() as s:
            s.add(store.WorkItem(issue_key=KEY, title="dup", stage="", forge_state="open", review_state="",
                                 merge_state="", human_review=False, updated_at=""))
            s.flush()

    def test_concurrent_writers_cannot_create_a_duplicate(self) -> None:
        errors: list[BaseException] = []

        def write(n: int) -> None:
            try:
                other = store.Store(self.path)
                self.addCleanup(other.engine.dispose)
                for i in range(10):
                    other.upsert_item(KEY, title=f"w{n}-{i}")
                    other.record_segment(KEY, "review", 1, started_at=f"t{n}-{i}")
            except BaseException as exc:  # noqa: BLE001 - the test reports whatever a writer hit
                errors.append(exc)

        threads = [threading.Thread(target=write, args=(n,)) for n in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        self.assertEqual(len(self.store.items()), 1)
        self.assertEqual(len(self.store.segments(KEY)), 1)

    def test_concurrent_fresh_writers_wait_for_an_existing_write_lock(self) -> None:
        # Use a raw connection so no Store has initialized the journal mode before the race.
        blocker = sqlite3.connect(self.path)
        self.addCleanup(blocker.close)
        blocker.execute("BEGIN IMMEDIATE")
        writers = [store.Store(self.path) for _ in range(32)]
        for writer in writers:
            self.addCleanup(writer.engine.dispose)
        start = threading.Barrier(len(writers) + 1)
        errors: list[BaseException] = []

        def write(n: int) -> None:
            try:
                start.wait(timeout=5)
                for i in range(10):
                    writers[n].upsert_item(KEY, title=f"w{n}-{i}")
                    writers[n].record_segment(KEY, "review", 1, started_at=f"t{n}-{i}")
            except BaseException as exc:  # noqa: BLE001 - the test reports whatever a writer hit
                errors.append(exc)

        threads = [threading.Thread(target=write, args=(n,)) for n in range(len(writers))]
        try:
            for thread in threads:
                thread.start()
            start.wait(timeout=5)
            # Fresh connections must wait at BEGIN IMMEDIATE, even while another writer holds the lock.
            time.sleep(0.1)
        finally:
            blocker.rollback()
            for thread in threads:
                thread.join()
        self.assertEqual(errors, [])
        self.assertEqual(len(self.store.items()), 1)
        self.assertEqual(len(self.store.segments(KEY)), 1)

    def test_concurrent_read_then_write_sessions_wait_for_the_lock(self) -> None:
        self.store.upsert_item(KEY, title="x")
        for message in ("first", "second"):
            self.store.add_log(KEY, message, at="09:00")
        writers = [store.Store(self.path) for _ in range(8)]
        for writer in writers:
            self.addCleanup(writer.engine.dispose)
        start = threading.Barrier(len(writers))
        errors: list[BaseException] = []

        def write(n: int) -> None:
            try:
                start.wait(timeout=5)
                with writers[n].session() as s:
                    rows = s.execute(select(store.LogEntry.id).execution_options(yield_per=1))
                    rows.fetchone()
                    # Leave a row pending: even legacy sqlite3 mode retains this WAL read snapshot.
                    # Give another writer time to commit before trying to upgrade it to a write.
                    time.sleep(0.05)
                    s.add(store.LogEntry(issue_key=KEY, message=f"w{n}", at="10:00"))
                    s.flush()
                    rows.close()
            except BaseException as exc:  # noqa: BLE001 - the test reports whatever a writer hit
                errors.append(exc)

        threads = [threading.Thread(target=write, args=(n,)) for n in range(len(writers))]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        self.assertEqual({entry.message for entry in self.store.log(KEY)},
                         {"first", "second", *(f"w{n}" for n in range(len(writers)))})

    def test_file_claim_replacement_keeps_every_open_segments_scope(self) -> None:
        self.store.upsert_item(KEY)
        self.store.replace_file_ownership(KEY, [("src", "write")])
        self.store.set_ownership(KEY, "/tmp/worker-tree", "tree")
        self.store.record_segment(KEY, "implement", 1, started_at="09:00")
        self.store.record_segment(KEY, "review", 1, started_at="09:01", ended_at="09:02", outcome="done")
        for pending in ([], [("docs", "read")]):
            self.store.replace_file_ownership(KEY, pending)
            self.assertEqual({(r.path, r.mode) for r in self.store.ownership(KEY)},
                             {("src", "write"), ("/tmp/worker-tree", "tree")})
        self.store.end_segment(KEY, "implement", 1, "09:03", "done")
        self.store.replace_file_ownership(KEY, [("docs", "read")])
        self.assertEqual({(r.path, r.mode) for r in self.store.ownership(KEY)},
                         {("docs", "read"), ("/tmp/worker-tree", "tree")})

    def test_scope_sync_waits_for_a_worker_start_transaction_before_replacing(self) -> None:
        self.store.upsert_item(KEY)
        self.store.replace_file_ownership(KEY, [("src", "write")])
        other = store.Store(self.path)
        self.addCleanup(other.engine.dispose)
        started = threading.Event()
        errors = []

        def replace() -> None:
            started.set()
            try:
                other.replace_file_ownership(KEY, [])
            except BaseException as exc:
                errors.append(exc)

        with self.store.session() as session:
            session.add(store.Segment(issue_key=KEY, stage="implement", attempt=1, started_at="09:00"))
            session.flush()  # reserve the worker while another connection attempts the scope sync
            thread = threading.Thread(target=replace)
            thread.start()
            self.assertTrue(started.wait(timeout=5))
        thread.join(timeout=5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual([(r.path, r.mode) for r in self.store.ownership(KEY)], [("src", "write")])

    def test_rework_is_more_segments_on_the_one_item_not_more_items(self) -> None:
        self.store.upsert_item(KEY, title="x")
        self.store.record_segment(KEY, "implement", 1, started_at="09:00", ended_at="10:00", outcome="done")
        self.store.record_segment(KEY, "review", 1, started_at="10:00", ended_at="10:30", outcome="findings")
        self.store.record_segment(KEY, "implement", 2, started_at="10:30")
        self.store.record_segment(KEY, "implement", 2, started_at="10:30", ended_at="11:00", outcome="done")
        self.assertEqual(len(self.store.items()), 1)
        self.assertEqual([(s.stage, s.attempt, s.outcome) for s in self.store.segments(KEY)],
                         [("implement", 1, "done"), ("review", 1, "findings"), ("implement", 2, "done")])

    def test_a_retried_start_never_reopens_a_finished_segment(self) -> None:
        self.store.upsert_item(KEY, title="x")
        self.store.record_segment(KEY, "review", 1, started_at="10:00", ended_at="10:30", outcome="findings")
        self.store.record_segment(KEY, "review", 1, started_at="10:00")
        self.assertEqual([(s.ended_at, s.outcome) for s in self.store.segments(KEY)], [("10:30", "findings")])

    def test_a_segment_records_who_ran_it(self) -> None:
        self.store.upsert_item(KEY, title="x")
        self.store.record_segment(KEY, "implement", 1, started_at="09:00",
                                  runtime="claude", model="opus", effort="high", agent_type="implementer")
        got = self.store.segments(KEY)[0]
        self.assertEqual((got.runtime, got.model, got.effort, got.agent_type),
                         ("claude", "opus", "high", "implementer"))

    def test_a_segment_defaults_who_ran_it_to_empty_strings(self) -> None:
        self.store.upsert_item(KEY, title="x")
        self.store.record_segment(KEY, "implement", 1, started_at="09:00")
        got = self.store.segments(KEY)[0]
        self.assertEqual((got.runtime, got.model, got.effort, got.agent_type), ("", "", "", ""))

    def test_a_retried_start_never_clears_who_ran_it(self) -> None:
        self.store.upsert_item(KEY, title="x")
        self.store.record_segment(KEY, "implement", 1, started_at="09:00",
                                  runtime="claude", model="opus", effort="high", agent_type="implementer",
                                  ended_at="10:00", outcome="done")
        self.store.record_segment(KEY, "implement", 1, started_at="09:00")
        got = self.store.segments(KEY)[0]
        self.assertEqual((got.runtime, got.model, got.effort, got.agent_type, got.ended_at, got.outcome),
                         ("claude", "opus", "high", "implementer", "10:00", "done"))

    def test_a_human_review_flag_for_an_issue_with_no_row_is_refused(self) -> None:
        with self.assertRaises(KeyError):
            self.store.set_human_review("github:example/repo#404", True)
        self.assertEqual(self.store.items(), [])

    def test_sync_updates_reported_human_review_and_preserves_it_when_omitted(self) -> None:
        self.store.sync_from_forge([{"issue_key": KEY, "human_review": True}])
        self.assertTrue(self.store.items()[0].human_review)
        self.store.sync_from_forge([{"issue_key": KEY, "title": "no kanban"}])
        self.assertTrue(self.store.items()[0].human_review)
        self.store.sync_from_forge([{"issue_key": KEY, "human_review": False}])
        self.assertFalse(self.store.items()[0].human_review)

    def test_a_segment_for_an_issue_with_no_row_is_refused(self) -> None:
        with self.assertRaises(IntegrityError):
            self.store.record_segment("github:example/repo#404", "review", 1, started_at="09:00")

    def test_ownership_decisions_and_log_hang_off_the_item(self) -> None:
        self.store.upsert_item(KEY, title="x")
        self.store.set_ownership(KEY, "scripts/store.py", "write")
        self.store.set_ownership(KEY, "scripts/store.py", "read")
        self.store.add_decision(KEY, topic="architecture", question="Split store?", answer="yes",
                                decided_by="trio", record={"votes": ["yes", "yes", "yes"]})
        self.store.add_log(KEY, "launched", at="09:00")
        self.assertEqual([(o.path, o.mode) for o in self.store.ownership(KEY)], [("scripts/store.py", "read")])
        self.assertEqual([(d.topic, d.decided_by, d.record) for d in self.store.decisions(KEY)],
                         [("architecture", "trio", {"votes": ["yes", "yes", "yes"]})])
        self.assertEqual([(e.at, e.message) for e in self.store.log(KEY)], [("09:00", "launched")])

    def test_update_resume_changes_only_its_keys_from_the_current_value(self) -> None:
        self.store.upsert_item(KEY, title="x")
        self.store.set_resume(KEY, {"note": {"stage": "fix"}, "decision_comments": ["a"]})
        seen = []

        def drop_comments(data):
            seen.append(dict(data))
            data["decision_comments"] = []
            data["interrupt"] = {"kind": "Ask"}

        self.store.update_resume(KEY, drop_comments)
        self.assertEqual(seen, [{"note": {"stage": "fix"}, "decision_comments": ["a"]}])
        self.assertEqual(self.store.resume(KEY), {"note": {"stage": "fix"}, "decision_comments": [],
                                                  "interrupt": {"kind": "Ask"}})

    def test_resume_state_round_trips(self) -> None:
        self.store.upsert_item(KEY, title="x")
        self.assertIsNone(self.store.resume(KEY))
        self.store.set_resume(KEY, {"next": "review", "waiting_on": ["ci"]})
        self.store.set_resume(KEY, {"next": "merge", "waiting_on": []})
        self.assertEqual(self.store.resume(KEY), {"next": "merge", "waiting_on": []})

    def test_answer_ask_validates_options_and_single_answer(self) -> None:
        self.store.upsert_item(KEY, title="x")
        ask_id = self.store.add_ask(KEY, "implement", 1, "s1", "Which parser?", ["Split", "Keep"], "ctx")
        with self.assertRaises(ValueError):
            self.store.answer_ask(KEY, ask_id, "Rewrite")
        self.assertIsNone(self.store.asks(KEY)[0].answer)
        self.store.answer_ask(KEY, ask_id, "Split")
        self.assertEqual(self.store.asks(KEY)[0].answer, "Split")
        # House rule 11: the architect's ruling on #758 makes the same answer again a no-op (an idempotent retry);
        # a different second answer is still refused, as #750 requires.
        self.store.answer_ask(KEY, ask_id, "Split")
        with self.assertRaises(KeyError):
            self.store.answer_ask(KEY, ask_id, "Keep")
        self.assertEqual(self.store.asks(KEY)[0].answer, "Split")

    def test_the_store_rebuilds_from_the_forge_and_sync_never_touches_engine_data(self) -> None:
        snapshots = [{"issue_key": KEY, "title": "x", "stage": "review", "forge_state": "open",
                      "review_state": "changes_requested", "merge_state": "blocked"}]
        self.store.sync_from_forge(snapshots)
        self.store.set_human_review(KEY, True)
        self.store.record_segment(KEY, "review", 1, started_at="10:00")
        self.store.sync_from_forge([{**snapshots[0], "stage": "merge", "merge_state": "ready"}])
        item = self.store.items()[0]
        self.assertEqual((item.stage, item.merge_state, item.human_review), ("merge", "ready", True))
        self.assertEqual(len(self.store.segments(KEY)), 1)
        # Drop the store; the forge-owned columns come back, the engine-owned data is lost.
        self.store.engine.dispose()
        self.path.unlink()
        for suffix in ("-wal", "-shm"):
            Path(str(self.path) + suffix).unlink(missing_ok=True)
        store.migrate(self.path)
        rebuilt = store.Store(self.path)
        self.addCleanup(rebuilt.engine.dispose)
        rebuilt.sync_from_forge(snapshots)
        item = rebuilt.items()[0]
        self.assertEqual((item.issue_key, item.stage, item.review_state, item.merge_state, item.human_review),
                         (KEY, "review", "changes_requested", "blocked", False))
        self.assertEqual(rebuilt.segments(KEY), [])


class MigrationTest(unittest.TestCase):
    def test_migration_enables_wal_before_any_store_connects(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "store.db"
            store.migrate(path)
            conn = sqlite3.connect(path)
            try:
                self.assertEqual(conn.execute("PRAGMA journal_mode").fetchone(), ("wal",))
            finally:
                conn.close()

    def test_migration_enables_wal_for_an_existing_database_at_head(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "store.db"
            store.migrate(path)
            conn = sqlite3.connect(path)
            try:
                self.assertEqual(conn.execute("PRAGMA journal_mode=DELETE").fetchone(), ("delete",))
            finally:
                conn.close()
            store.migrate(path)
            conn = sqlite3.connect(path)
            try:
                self.assertEqual(conn.execute("PRAGMA journal_mode").fetchone(), ("wal",))
            finally:
                conn.close()

    def test_migrations_build_exactly_the_schema_the_models_declare(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "store.db"
            store.migrate(path)
            engine = create_engine(f"sqlite:///{path}")
            with engine.connect() as conn:
                drift = compare_metadata(MigrationContext.configure(conn), store.Base.metadata)
            engine.dispose()
            self.assertEqual(drift, [])

    def test_the_log_operation_migration_backfills_kind_from_the_old_wording(self) -> None:
        old = {"stage write failed: x": "stage_write", "stage write refused: x": "stage_write",
               "decision comment failed: x": "decision_comment", "merge refused: x": "merge_refused",
               "merged #1": "merge", "escalate: retry limit reached": "worker_run",
               "dispatch failed: x": "worker_run", "wait: merge is not ready": "unknown",
               "ask failed: x": "unknown", "wait failed: x": "unknown", "merge failed: x": "merge_refused",
               "unrecognized worker failed: x": "unknown", "arbitrary refused operation": "unknown",
               "worker implement attempt 1: failed": "worker_run", "review failed: x": "worker_run"}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "store.db"
            command.upgrade(store._config(path), "0005")  # the revision before 0006_log_operation
            conn = sqlite3.connect(path)
            conn.execute("insert into work_item (issue_key, title, stage, forge_state, review_state, merge_state, human_review, "
                         "updated_at) values ('o/r#1', '', '', 'open', '', '', 0, '')")
            for message in old:
                conn.execute("insert into log_entry (issue_key, at, message) values ('o/r#1', 'now', ?)", (message,))
            conn.commit()
            conn.close()
            store.migrate(path)
            conn = sqlite3.connect(path)
            kinds = dict(conn.execute("select message, kind from log_entry").fetchall())
            conn.close()
            self.assertEqual(kinds, old)
    def test_existing_human_review_segments_keep_history_when_schema_is_upgraded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "store.db"
            store.command.upgrade(store._config(path), "0004")
            with closing(sqlite3.connect(path)) as conn, conn:
                conn.execute("INSERT INTO work_item VALUES (?, '', '', 'open', '', '', 0, '')", (KEY,))
                for attempt, outcome in enumerate(("human_review", "done"), start=1):
                    conn.execute("INSERT INTO segment (issue_key, stage, attempt, started_at, ended_at, outcome) "
                                 "VALUES (?, 'implement', ?, '09:00', '10:00', ?)", (KEY, attempt, outcome))
            store.migrate(path)
            db = store.Store(path)
            self.addCleanup(db.engine.dispose)
            self.assertEqual([s.required_human_review for s in db.segments(KEY)], [True, False])

    def test_migrating_twice_is_a_no_op_and_records_the_revision(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "store.db"
            store.migrate(path)
            store.migrate(path)
            conn = sqlite3.connect(path)
            rows = conn.execute("select version_num from alembic_version").fetchall()
            conn.close()
            self.assertEqual(len(rows), 1)


if __name__ == "__main__":
    unittest.main()
