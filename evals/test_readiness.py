"""Ready: file-ownership conflicts, dependency blockers and stop records."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import readiness

KEY = "github:example/project#1"
OTHER = "github:example/project#2"


class ReadinessTest(unittest.TestCase):
    def why(self, **kwargs):
        base = dict(claims=(), running=(), blockers=(), asks=(), reports=())
        base.update(kwargs)
        return readiness.why_not(KEY, **base)

    def test_a_write_path_that_overlaps_a_running_issue_is_not_ready(self):
        claims = (readiness.Claim(KEY, "src/a.py"), readiness.Claim(OTHER, "src/a.py"))
        self.assertEqual(self.why(claims=claims, running=(OTHER,)),
                         f"file ownership conflict with {OTHER}")

    def test_a_read_only_claim_overlaps_nothing(self):
        claims = (readiness.Claim(KEY, "src/a.py"), readiness.Claim(OTHER, "src/a.py", "read"))
        self.assertIsNone(self.why(claims=claims, running=(OTHER,)))

    def test_an_issue_does_not_conflict_with_itself(self):
        claims = (readiness.Claim(KEY, "src/a.py"),)
        self.assertIsNone(self.why(claims=claims, running=(KEY,)))

    def test_an_open_blocker_and_an_unknown_read_are_not_ready(self):
        self.assertEqual(self.why(blockers=(OTHER,)), f"blocked by {OTHER}")
        self.assertEqual(self.why(blockers=None), "dependency blockers unknown")

    def test_an_unanswered_ask_is_not_ready_and_an_answered_one_clears_a_stop(self):
        self.assertEqual(self.why(asks=(readiness.Ask(KEY, None),)), "unanswered ask")
        self.assertIsNone(self.why(asks=(readiness.Ask(KEY, "proceed"),),
                                   reports=(readiness.Report(KEY, "blocked"),)))

    def test_an_answer_clears_only_the_stop_it_answered(self):
        reports = (readiness.Report(KEY, "blocked", "implement", 1),)
        self.assertEqual(self.why(asks=(readiness.Ask(KEY, "proceed", "review", 1),), reports=reports),
                         "worker stopped")
        self.assertIsNone(self.why(asks=(readiness.Ask(KEY, "proceed", "implement", 1),), reports=reports))

    def test_the_latest_report_decides_not_the_highest_attempt(self):
        reports = (readiness.Report(KEY, "done", "implement", 2), readiness.Report(KEY, "blocked", "review", 1))
        self.assertEqual(self.why(reports=reports), "worker stopped")
        self.assertIsNone(self.why(reports=reports[::-1]))

    def test_a_blocked_report_with_no_ask_is_a_stop(self):
        self.assertEqual(self.why(reports=(readiness.Report(KEY, "blocked"),)), "worker stopped")
        self.assertIsNone(self.why(reports=(readiness.Report(KEY, "done"),)))
