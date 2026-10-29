"""Durable, attempt-bound operator messages for autonomous workers."""
from __future__ import annotations

import sys
import tempfile
import threading
import unittest
from pathlib import Path

from alembic import command

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import store

KEY = "github:example/project#42"
SESSION = "worker-session-1"


class WorkerMessagesTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "store.db"
        store.migrate(self.path)
        self.db = store.Store(self.path)
        self.addCleanup(self.db.engine.dispose)
        self.db.upsert_item(KEY, title="A task")
        self.db.record_segment(KEY, "implement", 1, "start")
        self.db.set_resume(KEY, {"runs": {"implement-1": {"session_id": SESSION}}})

    def queue(self, body: str = "Please check the timeout.", request_id: str = "client-1"):
        return self.db.queue_worker_message(KEY, "implement", 1, SESSION, body, request_id=request_id)

    def test_queue_is_durable_and_bound_to_a_single_worker_attempt(self) -> None:
        message = self.queue()
        self.assertEqual((message.status, message.body, message.session_id),
                         ("queued", "Please check the timeout.", SESSION))
        other = store.Store(self.path)
        self.addCleanup(other.engine.dispose)
        row = other.worker_messages(KEY, stage="implement", attempt=1, session_id=SESSION)[0]
        self.assertEqual((row.id, row.status, row.created_at), (message.id, "queued", message.created_at))
        self.assertEqual(other.worker_messages(KEY, session_id="different"), [])

    def test_queue_rejects_missing_or_ended_workers_and_invalid_text(self) -> None:
        with self.assertRaises(ValueError):
            self.queue("   ")
        with self.assertRaises(ValueError):
            self.db.queue_worker_message(KEY, "implement", 1, "wrong", "hello", request_id="wrong")
        self.db.end_segment(KEY, "implement", 1, "end", "done")
        with self.assertRaises(ValueError):
            self.queue()

    def test_duplicate_request_id_is_idempotent_only_for_the_same_message(self) -> None:
        first = self.queue()
        second = self.queue()
        self.assertEqual(first.id, second.id)
        with self.assertRaises(ValueError):
            self.queue("Changed content")
        self.assertEqual(len(self.db.worker_messages(KEY)), 1)

    def test_a_queued_message_is_not_retargeted_when_an_attempt_ends(self) -> None:
        original = self.queue()
        self.db.end_segment(KEY, "implement", 1, "end", "failed")
        self.db.record_segment(KEY, "implement", 2, "second start")
        self.db.set_resume(KEY, {"runs": {"implement-1": {"session_id": SESSION},
                                           "implement-2": {"session_id": "new-session"}}})
        self.assertEqual(self.queue().id, original.id)  # a repeated intake remains safe after the run ended
        with self.assertRaises(ValueError):
            self.db.queue_worker_message(KEY, "implement", 2, "new-session", original.body, request_id="client-1")
        with self.assertRaises(ValueError):
            self.db.begin_worker_message_delivery(original.id, KEY, "implement", 1, SESSION)
        self.assertEqual(self.db.worker_messages(KEY, attempt=2), [])
        self.assertEqual(self.db.worker_messages(KEY, attempt=1)[0].status, "queued")

    def test_delivery_requires_evidence_and_the_exact_worker_identity(self) -> None:
        message = self.queue()
        with self.assertRaises(ValueError):
            self.db.ack_worker_message(message.id, KEY, "implement", 1, SESSION,
                                       delivery_id="unknown", evidence="host receipt")
        claim = self.db.begin_worker_message_delivery(message.id, KEY, "implement", 1, SESSION)
        with self.assertRaises(ValueError):
            self.db.ack_worker_message(message.id, KEY, "implement", 1, SESSION,
                                       delivery_id=claim.delivery_id, evidence="")
        with self.assertRaises(ValueError):
            self.db.ack_worker_message(message.id, KEY, "implement", 1, "other",
                                       delivery_id=claim.delivery_id, evidence="host receipt")
        with self.assertRaises(ValueError):
            self.db.ack_worker_message(message.id, KEY, "implement", 1, SESSION,
                                       delivery_id="other", evidence="host receipt")
        self.assertEqual(self.db.worker_messages(KEY)[0].status, "sending")
        self.db.ack_worker_message(message.id, KEY, "implement", 1, SESSION,
                                   delivery_id=claim.delivery_id, evidence="host receipt 9")
        row = self.db.worker_messages(KEY)[0]
        self.assertEqual((row.status, row.evidence), ("delivered", "host receipt 9"))
        self.assertTrue(row.resolved_at)
        self.db.ack_worker_message(message.id, KEY, "implement", 1, SESSION,
                                   delivery_id=claim.delivery_id, evidence="host receipt 9")
        with self.assertRaises(ValueError):
            self.db.fail_worker_message(message.id, KEY, "implement", 1, SESSION, reason="not sent")

    def test_failed_message_never_becomes_delivered_without_an_acknowledgment(self) -> None:
        msg = self.queue()
        with self.assertRaises(ValueError):
            self.db.fail_worker_message(msg.id, KEY, "implement", 1, SESSION, reason=" ")
        self.db.fail_worker_message(msg.id, KEY, "implement", 1, SESSION, reason="delivery unsupported")
        row = self.db.worker_messages(KEY)[0]
        self.assertEqual((row.status, row.evidence), ("failed", "delivery unsupported"))
        with self.assertRaises(ValueError):
            self.db.ack_worker_message(msg.id, KEY, "implement", 1, SESSION, delivery_id="late", evidence="late")

    def test_crash_after_claim_leaves_delivery_unconfirmed_and_cannot_retry_automatically(self) -> None:
        message = self.queue()
        claim = self.db.begin_worker_message_delivery(message.id, KEY, "implement", 1, SESSION)
        self.assertEqual(claim.status, "sending")
        self.assertTrue(claim.delivery_id)
        self.assertTrue(claim.claimed_at)
        reopened = store.Store(self.path)
        self.addCleanup(reopened.engine.dispose)
        self.assertEqual(reopened.worker_messages(KEY)[0].status, "sending")
        with self.assertRaises(ValueError):
            reopened.begin_worker_message_delivery(message.id, KEY, "implement", 1, SESSION)

    def test_duplicate_concurrent_intake_creates_one_message(self) -> None:
        start = threading.Barrier(3)
        ids: list[int] = []
        errors: list[Exception] = []

        def submit() -> None:
            other = store.Store(self.path)
            try:
                start.wait(timeout=5)
                ids.append(other.queue_worker_message(KEY, "implement", 1, SESSION, "Same", request_id="stable").id)
            except Exception as exc:
                errors.append(exc)
            finally:
                other.engine.dispose()

        threads = [threading.Thread(target=submit) for _ in range(2)]
        for thread in threads:
            thread.start()
        start.wait(timeout=5)
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        self.assertEqual(len(set(ids)), 1)
        self.assertEqual(len(self.db.worker_messages(KEY)), 1)

    def test_conflicting_delivery_results_cannot_both_succeed(self) -> None:
        message = self.queue()
        claim = self.db.begin_worker_message_delivery(message.id, KEY, "implement", 1, SESSION)
        start = threading.Barrier(3)
        successes: list[str] = []
        rejected: list[str] = []

        def resolve(status: str) -> None:
            other = store.Store(self.path)
            try:
                start.wait(timeout=5)
                if status == "delivered":
                    other.ack_worker_message(message.id, KEY, "implement", 1, SESSION,
                                             delivery_id=claim.delivery_id, evidence="receipt")
                else:
                    other.fail_worker_message(message.id, KEY, "implement", 1, SESSION, reason="not delivered")
                successes.append(status)
            except ValueError:
                rejected.append(status)
            finally:
                other.engine.dispose()

        threads = [threading.Thread(target=resolve, args=(status,)) for status in ("delivered", "failed")]
        for thread in threads:
            thread.start()
        start.wait(timeout=5)
        for thread in threads:
            thread.join()
        self.assertEqual(len(successes), 1)
        self.assertEqual(len(rejected), 1)
        self.assertEqual(self.db.worker_messages(KEY)[0].status, successes[0])

    def test_only_one_concurrent_sender_can_claim_a_message(self) -> None:
        message = self.queue()
        start = threading.Barrier(3)
        claims: list[str] = []
        rejected: list[Exception] = []

        def claim() -> None:
            other = store.Store(self.path)
            try:
                start.wait(timeout=5)
                claims.append(other.begin_worker_message_delivery(message.id, KEY, "implement", 1,
                                                                  SESSION).delivery_id)
            except ValueError as exc:
                rejected.append(exc)
            finally:
                other.engine.dispose()

        threads = [threading.Thread(target=claim) for _ in range(2)]
        for thread in threads:
            thread.start()
        start.wait(timeout=5)
        for thread in threads:
            thread.join()
        self.assertEqual(len(claims), 1)
        self.assertEqual(len(rejected), 1)
        self.assertEqual(self.db.worker_messages(KEY)[0].delivery_id, claims[0])

    def test_migration_preserves_existing_engine_data(self) -> None:
        old_path = Path(self.tmp.name) / "old.db"
        command.upgrade(store._config(old_path), "0009")
        old = store.Store(old_path)
        old.upsert_item(KEY, title="Existing task")
        old.engine.dispose()
        store.migrate(old_path)
        new = store.Store(old_path)
        self.addCleanup(new.engine.dispose)
        self.assertEqual(new.item(KEY).title, "Existing task")
        self.assertEqual(new.worker_messages(KEY), [])


if __name__ == "__main__":
    unittest.main()
