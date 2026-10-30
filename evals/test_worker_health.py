"""Stall and stagnation are the silence signal."""

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import worker_health

NOW = datetime(2000, 1, 1, tzinfo=timezone.utc)
LIMIT = timedelta(minutes=45)


class SilenceTest(unittest.TestCase):
    def signal(self, started, progress):
        return worker_health.silence(started, progress, NOW, LIMIT)

    def test_recent_work_is_ok_and_a_quiet_approach_is_watch(self):
        self.assertEqual(self.signal(NOW - timedelta(minutes=10), None), "ok")
        self.assertEqual(self.signal(NOW - timedelta(minutes=30), None), "watch")

    def test_no_progress_past_the_limit_is_a_stall_and_a_fresh_commit_is_not(self):
        start = NOW - timedelta(hours=2)
        self.assertEqual(self.signal(start, None), "act")
        self.assertEqual(self.signal(start, NOW - timedelta(minutes=5)), "ok")

    def test_a_commit_from_before_the_run_or_after_now_is_not_progress(self):
        start = NOW - timedelta(hours=2)
        self.assertEqual(self.signal(start, start - timedelta(minutes=5)), "act")
        self.assertEqual(self.signal(NOW - timedelta(minutes=30), NOW - timedelta(minutes=31)), "watch")
        self.assertEqual(self.signal(start, NOW + timedelta(hours=3)), "act")

    def test_the_limit_must_be_positive(self):
        with self.assertRaises(ValueError):
            worker_health.silence(NOW, None, NOW, timedelta(0))


class EvidenceTest(unittest.TestCase):
    def judge(self, **kwargs):
        return worker_health.stall(NOW - timedelta(minutes=10), NOW, timedelta(minutes=3),
                                   worker_health.Evidence(**kwargs))

    def test_silence_is_suspected_and_needs_verified_liveness(self):
        self.assertEqual(self.judge(alive=True), "suspected")
        self.assertEqual(self.judge(alive=None), "unknown")
        self.assertEqual(self.judge(alive=False), "gone")

    def test_startup_grace_recent_transcript_and_thread_updates_prevent_stalls(self):
        recent = NOW - timedelta(minutes=1)
        evidence = worker_health.Evidence(alive=True, last_activity_at=recent)
        self.assertEqual(worker_health.stall(NOW - timedelta(minutes=2), NOW,
                         timedelta(minutes=3), worker_health.Evidence(alive=True)), "ok")
        self.assertEqual(self.judge(alive=True, last_activity_at=recent), "ok")
        self.assertEqual(worker_health.stall(NOW - timedelta(minutes=10), NOW,
                         timedelta(minutes=3), evidence), "ok")

    def test_pending_requests_wait_then_suspect_but_protected_decisions_always_wait(self):
        self.assertEqual(self.judge(alive=True, pending_requests=1, pending_since=NOW), "waiting")
        self.assertEqual(self.judge(alive=True, pending_requests=1,
                                   pending_since=NOW-timedelta(minutes=4)), "suspected")
        self.assertEqual(self.judge(alive=True, pending_requests=1,
                                   pending_since=NOW-timedelta(days=1), decision_wait=True), "waiting")
        self.assertEqual(self.judge(alive=True, decision_wait=True), "waiting")

    def test_failed_t3_read_is_unknown_and_not_a_process_based_stall(self):
        self.assertEqual(self.judge(alive=True, launcher="t3", error="offline"), "unknown")
