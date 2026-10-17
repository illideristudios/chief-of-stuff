"""Completion: done-on-main, the read-only rule, unlanded work, orphans, and a gone worker."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import completion
import orphans
from tree_state import TreeState

CLEAN = TreeState("feature", 0, 0, 0)
RISK = TreeState("feature", 1, 2, None)


class CompletionTest(unittest.TestCase):
    def test_done_means_on_main_with_the_suite_run_there(self):
        self.assertIsNone(completion.done_on_main(True, True))
        self.assertEqual(completion.done_on_main(False, True), "not on main")
        self.assertEqual(completion.done_on_main(True, False), "suite has not run on main")
        self.assertEqual(completion.done_on_main(None, True), "on main is unknown")
        self.assertEqual(completion.done_on_main(True, None), "suite on main is unknown")

    def test_a_read_only_run_that_left_a_change_goes_to_review(self):
        self.assertEqual(completion.read_only_left_changes(True, True), "read-only run left changes")
        self.assertEqual(completion.read_only_left_changes(True, None), "read-only run left changes")
        self.assertIsNone(completion.read_only_left_changes(True, False))
        self.assertIsNone(completion.read_only_left_changes(False, True))

    def test_unlanded_work_and_an_orphan_are_named(self):
        self.assertIsNone(completion.unlanded(CLEAN))
        self.assertIn("uncommitted", completion.unlanded(RISK))
        gone = orphans.Claim("worker", False, "has no process")
        self.assertIsNone(completion.orphan_reason("tree", RISK, (orphans.Claim("worker", True),)))
        self.assertIn("orphaned", completion.orphan_reason("tree", RISK, (gone,)).lower())

    def test_a_live_or_unknown_worker_is_not_abandoned(self):
        self.assertIsNone(completion.abandoned(True))
        self.assertIsNone(completion.abandoned(None))

    def test_a_gone_worker_is_closed_from_its_evidence(self):
        self.assertEqual(completion.abandoned(False), completion.Verdict("failed", "worker gone"))
        self.assertEqual(completion.abandoned(False, read_only=True, changed=True),
                         completion.Verdict("done", "read-only run left changes", True))
        self.assertEqual(completion.abandoned(False, report="done", on_main=True, suite_on_main=True),
                         completion.Verdict("done", ""))
        self.assertEqual(completion.abandoned(False, report="done", on_main=False, suite_on_main=True),
                         completion.Verdict("done", "not on main", True))
