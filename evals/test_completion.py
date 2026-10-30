"""Completion: done-on-main, unlanded work, orphans, and a gone worker."""

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
        self.assertEqual(completion.abandoned(False, report="done", on_main=True, suite_on_main=True),
                         completion.Verdict("done", ""))
        self.assertEqual(completion.abandoned(False, report="done", on_main=False, suite_on_main=True),
                         completion.Verdict("done", "not on main", True))

    def test_a_done_report_with_unlanded_work_stays_done_for_review_not_an_orphan(self):
        gone = (orphans.Claim("worker", False, "has no process"),)
        verdict = completion.abandoned(False, report="done", on_main=False, tree=RISK, tree_name="tree", tree_claims=gone)
        self.assertEqual((verdict.outcome, verdict.human_review), ("done", True))

    def test_final_done_holds_work_at_risk_even_when_head_and_suite_are_on_main(self):
        gone = (orphans.Claim("worker", False, "has no process"),)
        for tree in (TreeState("feature", 1, 0, 0), TreeState("feature", 0, 2, 1),
                     TreeState("feature", None, 0, 0), TreeState("feature", 0, None, None)):
            for tree_name in ("", "tree"):
                with self.subTest(tree=tree, tree_name=tree_name):
                    verdict = completion.abandoned(
                        False, report="done", on_main=True, suite_on_main=True, tree=tree,
                        tree_name=tree_name, tree_claims=gone, landing_required=True)
                    self.assertEqual(verdict, completion.Verdict("done", completion.unlanded(tree), True))
        # engine_facts.gather sets tree_landed when local HEAD is on main; dirty still holds.
        for tree in (TreeState("feature", 1, 0, 0), TreeState("feature", 1, 2, None),
                     TreeState("feature", None, 0, 0)):
            with self.subTest(tree=tree, tree_landed=True):
                verdict = completion.abandoned(
                    False, report="done", on_main=True, suite_on_main=True, tree=tree,
                    tree_name="tree", tree_claims=gone, landing_required=True, tree_landed=True)
                self.assertEqual(verdict, completion.Verdict("done", completion.unlanded(tree), True))
        outside = TreeState("feature", 0, 2, 0)
        verdict = completion.abandoned(
            False, report="done", on_main=True, suite_on_main=True, tree=outside,
            tree_name="tree", tree_claims=gone, landing_required=True, tree_landed=False)
        self.assertEqual(verdict, completion.Verdict("done", "tree HEAD is outside the verified landing", True))

    def test_final_done_clears_a_clean_tree_with_no_work_at_risk(self):
        for tree in (CLEAN, TreeState("feature", 0, 0, None), TreeState("feature", 0, 2, 0)):
            with self.subTest(tree=tree):
                verdict = completion.abandoned(
                    False, report="done", on_main=True, suite_on_main=True, tree=tree,
                    tree_name="tree", tree_claims=(orphans.Claim("worker", False),), landing_required=True)
                self.assertEqual(verdict, completion.Verdict("done", ""))
        squash = TreeState("feature", 0, 2, None)
        verdict = completion.abandoned(
            False, report="done", on_main=True, suite_on_main=True, tree=squash,
            tree_name="tree", tree_claims=(orphans.Claim("worker", False),),
            landing_required=True, tree_landed=True)
        self.assertEqual(verdict, completion.Verdict("done", ""))

    def test_a_gone_worker_whose_tree_could_not_be_read_is_held(self):
        self.assertEqual(completion.abandoned(False, tree_unreadable=True),
                         completion.Verdict("failed", "worker tree could not be read", True))

    def test_premerge_done_does_not_certify_an_unreadable_tree(self):
        verdict = completion.abandoned(False, report="done", landing_required=False, tree_unreadable=True)
        self.assertEqual(verdict, completion.Verdict("done", "worker tree could not be read", True))

    def test_only_final_completion_requires_landing(self):
        for final, held in ((False, False), (True, True)):
            with self.subTest(final=final):
                verdict = completion.abandoned(False, report="done", tree=CLEAN, on_main=False,
                                               suite_on_main=False, landing_required=final)
                self.assertEqual(verdict.human_review, held)


class StageCompletionRegressionTest(unittest.TestCase):
    def test_clean_detached_or_untracked_premerge_done_is_not_an_orphan(self):
        for branch in ("feature", "(detached)"):
            with self.subTest(branch=branch):
                verdict = completion.abandoned(
                    False, report="done", tree=TreeState(branch, 0, 3, 0), on_main=False,
                    tree_name="review-tree", tree_claims=(orphans.Claim("worker", False, "exited"),),
                    landing_required=False)
                self.assertEqual(verdict, completion.Verdict("done", ""))

    def test_untouched_published_premerge_tree_can_retry_after_a_missing_or_failed_report(self):
        for report in (None, "failed"):
            verdict = completion.abandoned(False, report=report, tree=TreeState("HEAD", 0, 3, 0),
                tree_name="review", tree_claims=(orphans.Claim("worker", False, "exited"),), landing_required=False)
            self.assertEqual(verdict, completion.Verdict("failed", "worker gone"))

    def test_unknown_push_evidence_does_not_certify_premerge_done(self):
        verdict = completion.abandoned(False, report="done", tree=TreeState("HEAD", 0, 3, None), landing_required=False)
        self.assertTrue(verdict.human_review)
        self.assertIn("unknown", verdict.reason)

    def test_premerge_done_still_holds_uncommitted_or_unpushed_work(self):
        for tree in (TreeState("feature", 1, 3, None), TreeState("feature", 0, 3, 1)):
            with self.subTest(tree=tree):
                verdict = completion.abandoned(False, report="done", tree=tree, landing_required=False)
                self.assertEqual((verdict.outcome, verdict.human_review), ("done", True))

    def test_unreadable_trees_hold_every_outcome_without_turning_failure_into_done(self):
        for report in (None, "failed", "done", "quota", "unknown", "blocked"):
            with self.subTest(report=report):
                verdict = completion.abandoned(False, report=report, tree_unreadable=True, landing_required=False)
                self.assertEqual(verdict, completion.Verdict(report or "failed", "worker tree could not be read", True))
