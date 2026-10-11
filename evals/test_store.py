"""The issue-keyed store: one row per forge issue, upsert-only writes, schema owned by migrations (1.0 plan, M1)."""

from __future__ import annotations

import sqlite3
import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import create_engine
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

    def test_a_human_review_flag_for_an_issue_with_no_row_is_refused(self) -> None:
        with self.assertRaises(KeyError):
            self.store.set_human_review("github:example/repo#404", True)
        self.assertEqual(self.store.items(), [])

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

    def test_resume_state_round_trips(self) -> None:
        self.store.upsert_item(KEY, title="x")
        self.assertIsNone(self.store.resume(KEY))
        self.store.set_resume(KEY, {"next": "review", "waiting_on": ["ci"]})
        self.store.set_resume(KEY, {"next": "merge", "waiting_on": []})
        self.assertEqual(self.store.resume(KEY), {"next": "merge", "waiting_on": []})

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
    def test_migrations_build_exactly_the_schema_the_models_declare(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "store.db"
            store.migrate(path)
            engine = create_engine(f"sqlite:///{path}")
            with engine.connect() as conn:
                drift = compare_metadata(MigrationContext.configure(conn), store.Base.metadata)
            engine.dispose()
            self.assertEqual(drift, [])

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
