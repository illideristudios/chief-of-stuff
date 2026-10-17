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

    def test_the_limit_must_be_positive(self):
        with self.assertRaises(ValueError):
            worker_health.silence(NOW, None, NOW, timedelta(0))
