"""Worker concurrency cap (E21): free-slot count and a claim that cannot race for the last slot."""

from __future__ import annotations

import multiprocessing as mp
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import slots  # noqa: E402
import store  # noqa: E402

KEY = "github:example/repo#12"
PINNED = datetime(2026, 10, 11, 9, 0, 0, tzinfo=timezone.utc)
POST_LAUNCH = datetime(2026, 10, 11, 9, 5, 0, tzinfo=timezone.utc)


class FreeSlotsTest(unittest.TestCase):
    def test_free_slots_are_the_cap_minus_running(self) -> None:
        self.assertEqual(slots.free_slots(5, 2), 3)
        self.assertEqual(slots.free_slots(1, 0), 1)
        self.assertEqual(slots.free_slots(1, 1), 0)

    def test_no_cap_means_a_large_number(self) -> None:
        self.assertEqual(slots.free_slots(None, 0), slots.UNBOUNDED)
        self.assertEqual(slots.free_slots(None, 12), slots.UNBOUNDED - 12)

    def test_free_slots_never_negative(self) -> None:
        self.assertEqual(slots.free_slots(2, 5), 0)
        self.assertEqual(slots.free_slots(0, 1), 0)
        self.assertEqual(slots.free_slots(None, slots.UNBOUNDED + 1), 0)

    def test_a_closed_segment_frees_its_slot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "store.db"
            store.migrate(path)
            db = store.Store(path)
            self.addCleanup(db.engine.dispose)
            db.upsert_item(KEY, title="x")
            db.record_segment(KEY, "implement", 1, started_at="09:00")
            self.assertEqual(slots.free_slots(2, slots.running(db)), 1)
            db.record_segment(KEY, "implement", 1, started_at="09:00", ended_at="10:00", outcome="done")
            self.assertEqual(slots.free_slots(2, slots.running(db)), 2)

    def test_running_counts_open_segments_in_one_store_query(self) -> None:
        # Many items must not N+1: items() plus segments(key) per item under the claim flock.
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "store.db"
            store.migrate(path)
            db = store.Store(path)
            self.addCleanup(db.engine.dispose)
            keys = [f"github:example/repo#{n}" for n in range(3)]
            for k in keys:
                db.upsert_item(k, title="x")
                db.record_segment(k, "implement", 1, started_at="09:00")
            db.record_segment(keys[0], "implement", 1, started_at="09:00", ended_at="10:00", outcome="done")
            db.record_segment(keys[0], "review", 1, started_at="10:00")
            calls: list[tuple[str, object]] = []
            real_segments, real_items = db.segments, db.items

            def spy_segments(issue_key: str | None = None):
                calls.append(("segments", issue_key))
                return real_segments(issue_key)

            def spy_items():
                calls.append(("items", None))
                return real_items()

            with mock.patch.object(db, "segments", spy_segments), mock.patch.object(db, "items", spy_items):
                self.assertEqual(slots.running(db), 3)
            self.assertEqual(calls, [("segments", None)])


def _noop() -> None:
    pass


def _claim_child(barrier, out: str, db_path: str, trees: str, i: int, cap: int) -> None:
    barrier.wait(30)
    db = store.Store(db_path)
    outcome = "refused"
    try:
        slots.claim(Path(trees), db, issue_key=KEY, stage="implement", attempt=i + 1,
                    max_concurrency=cap, launch=_noop)
        outcome = "in"
    except ValueError:
        pass
    finally:
        db.engine.dispose()
    (Path(out) / str(i)).write_text(outcome)


class ClaimTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.trees = self.root / "trees"
        self.trees.mkdir()
        self.db_path = self.root / "store.db"
        store.migrate(self.db_path)
        self.db = store.Store(self.db_path)
        self.addCleanup(self.db.engine.dispose)
        self.db.upsert_item(KEY, title="x")
        self.out = self.root / "out"
        self.out.mkdir()

    def test_claim_stamps_started_at_after_launch(self) -> None:
        # Wait-for-lock / prior-worker launch must not charge the new segment; stamp after launch.
        now = [PINNED]

        def advancing_launch() -> None:
            now[0] = POST_LAUNCH

        slots.claim(self.trees, self.db, issue_key=KEY, stage="implement", attempt=1,
                    max_concurrency=1, launch=advancing_launch, clock=lambda: now[0])
        self.assertEqual(self.db.segments(KEY)[0].started_at, POST_LAUNCH.isoformat())

    def test_claim_records_a_running_segment_when_a_slot_is_free(self) -> None:
        slots.claim(self.trees, self.db, issue_key=KEY, stage="implement", attempt=1,
                    max_concurrency=1, launch=_noop, clock=lambda: PINNED)
        segs = self.db.segments(KEY)
        self.assertEqual([(s.stage, s.attempt, s.started_at, s.ended_at) for s in segs],
                         [("implement", 1, PINNED.isoformat(), None)])
        self.assertEqual(slots.running(self.db), 1)

    def test_claim_persists_worker_identity(self) -> None:
        slots.claim(self.trees, self.db, issue_key=KEY, stage="implement", attempt=1,
                    max_concurrency=1, launch=_noop, clock=lambda: PINNED,
                    runtime="claude", model="opus", effort="high", agent_type="implementer")
        got = self.db.segments(KEY)[0]
        self.assertEqual((got.runtime, got.model, got.effort, got.agent_type),
                         ("claude", "opus", "high", "implementer"))

    def test_claim_records_model_effort_returned_by_launch(self) -> None:
        # Rotation picks a fallback during launch; record that pair, not the pre-launch choice.
        slots.claim(self.trees, self.db, issue_key=KEY, stage="implement", attempt=1,
                    max_concurrency=1, launch=lambda: ("sonnet", "medium"), clock=lambda: PINNED,
                    runtime="claude", model="opus", effort="high", agent_type="implementer")
        got = self.db.segments(KEY)[0]
        self.assertEqual((got.model, got.effort), ("sonnet", "medium"))

    def test_claim_records_passed_model_effort_when_launch_returns_none(self) -> None:
        slots.claim(self.trees, self.db, issue_key=KEY, stage="implement", attempt=1,
                    max_concurrency=1, launch=lambda: None, clock=lambda: PINNED,
                    runtime="claude", model="opus", effort="high", agent_type="implementer")
        got = self.db.segments(KEY)[0]
        self.assertEqual((got.model, got.effort), ("opus", "high"))

    def test_claim_refuses_when_full(self) -> None:
        slots.claim(self.trees, self.db, issue_key=KEY, stage="implement", attempt=1,
                    max_concurrency=1, launch=_noop, clock=lambda: PINNED)
        with self.assertRaises(ValueError) as caught:
            slots.claim(self.trees, self.db, issue_key=KEY, stage="review", attempt=1,
                        max_concurrency=1, launch=_noop, clock=lambda: PINNED)
        self.assertIn("max_concurrency is 1", str(caught.exception))
        self.assertEqual(slots.running(self.db), 1)

    def test_claim_refuses_a_duplicate_segment_even_with_spare_capacity(self) -> None:
        slots.claim(self.trees, self.db, issue_key=KEY, stage="implement", attempt=1,
                    max_concurrency=2, launch=_noop, clock=lambda: PINNED)
        with self.assertRaises(ValueError):
            slots.claim(self.trees, self.db, issue_key=KEY, stage="implement", attempt=1,
                        max_concurrency=2, launch=_noop, clock=lambda: PINNED)
        self.assertEqual(slots.running(self.db), 1)
        self.db.record_segment(KEY, "implement", 1, started_at="09:00", ended_at="10:00", outcome="done")
        with self.assertRaises(ValueError):
            slots.claim(self.trees, self.db, issue_key=KEY, stage="implement", attempt=1,
                        max_concurrency=2, launch=_noop, clock=lambda: PINNED)
        self.assertEqual(self.db.segments(KEY)[0].ended_at, "10:00")

    def test_failed_launch_records_nothing_and_frees_the_slot(self) -> None:
        def boom() -> None:
            raise RuntimeError("spawn failed")

        with self.assertRaises(RuntimeError):
            slots.claim(self.trees, self.db, issue_key=KEY, stage="implement", attempt=1,
                        max_concurrency=1, launch=boom, clock=lambda: PINNED)
        self.assertEqual(self.db.segments(KEY), [])
        self.assertEqual(slots.running(self.db), 0)
        slots.claim(self.trees, self.db, issue_key=KEY, stage="implement", attempt=1,
                    max_concurrency=1, launch=_noop, clock=lambda: PINNED)
        self.assertEqual(slots.running(self.db), 1)

    def test_launch_runs_outside_the_lock_but_its_slot_stays_reserved(self) -> None:
        in_launch = threading.Event()
        release = threading.Event()

        def slow_launch() -> None:
            in_launch.set()
            self.assertTrue(release.wait(5))

        holder = threading.Thread(
            target=slots.claim,
            kwargs=dict(trees=self.trees, store=self.db, issue_key=KEY, stage="implement",
                        attempt=1, max_concurrency=2, launch=slow_launch),
        )
        holder.start()
        self.assertTrue(in_launch.wait(5))

        # A contender gets past the flock while the holder is still launching, and sees the reserved slot.
        contender = threading.Thread(
            target=slots.claim,
            kwargs=dict(trees=self.trees, store=self.db, issue_key=KEY, stage="review",
                        attempt=1, max_concurrency=2, launch=_noop),
        )
        contender.start()
        contender.join(5)
        self.assertFalse(contender.is_alive())
        self.assertEqual(slots.running(self.db), 2)
        with self.assertRaises(ValueError):  # cap 2 is full: holder's reservation still counts
            slots.claim(self.trees, self.db, issue_key=KEY, stage="merge", attempt=1,
                        max_concurrency=2, launch=_noop)

        release.set()
        holder.join(5)
        self.assertFalse(holder.is_alive())
        self.assertEqual(slots.running(self.db), 2)

    def test_two_claims_for_the_last_slot_exactly_one_wins(self) -> None:
        # Cap 1, two processes: the flock + recount inside it means exactly one records a start.
        cap, claimants = 1, 2
        barrier = mp.Barrier(claimants)
        procs = [mp.Process(target=_claim_child,
                            args=(barrier, str(self.out), str(self.db_path), str(self.trees), i, cap))
                 for i in range(claimants)]
        for p in procs:
            p.start()
        for p in procs:
            p.join(timeout=60)
            self.assertEqual(p.exitcode, 0, f"claim {p.pid} exited {p.exitcode}")
        outcomes = [f.read_text() for f in self.out.iterdir()]
        self.assertEqual((outcomes.count("in"), outcomes.count("refused")), (1, 1), outcomes)
        self.assertEqual(slots.running(self.db), 1)


if __name__ == "__main__":
    unittest.main()
