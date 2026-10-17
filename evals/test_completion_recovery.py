"""Orphan recovery: which tree and branch to reuse, or why to ask (architecture §4, completion.py)."""

import sys
import unittest
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from completion import Conflict, OrphanTree, Recovery, recovery
import completion


def tree(path, head="a", branch="work/issue-1-implement-1", **kw):
    return OrphanTree(path=path, branch=branch, head=head, **kw)


class RecoveryTest(unittest.TestCase):
    def test_retirement_requires_matching_readable_nonempty_snapshots(self):
        saved = completion.RetirementSnapshot("work/issue-1-implement-1", "a", "digest")
        self.assertTrue(completion.may_retire(saved, saved))
        for changed in (replace(saved, branch="work/other"), replace(saved, head="b"),
                        replace(saved, contents="different"), replace(saved, unreadable=True),
                        replace(saved, branch=""), replace(saved, head=""), replace(saved, contents="")):
            with self.subTest(changed=changed):
                self.assertFalse(completion.may_retire(saved, changed))
                self.assertFalse(completion.may_retire(changed, saved))
                if changed.unreadable or not changed.branch or not changed.head or not changed.contents:
                    self.assertFalse(completion.may_retire(changed, changed))

    def test_no_candidates_recovers_nothing(self):
        self.assertIsNone(recovery([]))

    def test_one_tree_is_reused_in_place_and_dirty_work_marks_recovery(self):
        self.assertEqual(recovery([tree("/t/a", dirty=True)]),
                         Recovery("/t/a", "work/issue-1-implement-1", "a", True))
        self.assertFalse(recovery([tree("/t/a", ahead=0)]).recovered)

    def test_unknown_commit_count_still_counts_as_recovery(self):
        self.assertTrue(recovery([tree("/t/a", ahead=None)]).recovered)

    def test_a_surviving_branch_without_its_directory_is_recreated(self):
        got = recovery([tree(None, head="b")])
        self.assertEqual((got.path, got.head, got.recovered), (None, "b", True))

    def test_the_tree_holding_the_newest_commit_wins_and_an_older_one_fast_forwards(self):
        old, new = tree("/t/old", head="a"), tree("/t/new", head="b", branch="work/issue-1-implement-2",
                                                  ancestors=frozenset({"a"}))
        self.assertEqual(recovery([old, new]).path, "/t/new")
        self.assertEqual(recovery([old, new], preferred="/t/old").path, "/t/new")
        only_old = recovery([old, tree(None, head="b", ancestors=frozenset({"a"}))])
        self.assertEqual((only_old.path, only_old.head), ("/t/old", "b"))

    def test_fast_forwarding_a_saved_base_tree_counts_as_recovery(self):
        self.assertTrue(recovery([tree("/t/base", head="a"),
                                  tree(None, head="b", ancestors=frozenset({"a"}))]).recovered)

    def test_the_only_dirty_tree_wins_over_preferred_clean_tree_at_same_tip(self):
        clean, dirty = tree("/t/clean"), tree("/t/dirty", dirty=True)
        self.assertEqual(recovery([clean, dirty], preferred=clean.path).path, dirty.path)

    def test_the_preferred_tree_wins_among_equal_heads(self):
        a, b = tree("/t/a"), tree("/t/b", branch="work/issue-1-implement-2")
        self.assertEqual(recovery([a, b], preferred="/t/b").path, "/t/b")

    def test_conflicts_name_their_reason(self):
        cases = {
            "detached or unreadable": [tree("/t/a", unreadable=True)],
            "unresolved merge conflicts": [tree("/t/a", conflicted=True)],
            "multiple orphaned worktrees have uncommitted changes": [tree("/t/a", dirty=True),
                                                                      tree("/t/b", dirty=True)],
            "diverged commits": [tree("/t/a", head="a"), tree("/t/b", head="b")],
            "behind newer commits": [tree("/t/a", head="a", dirty=True),
                                     tree(None, head="b", ancestors=frozenset({"a"}))],
        }
        for reason, trees in cases.items():
            with self.subTest(reason=reason):
                got = recovery(trees)
                self.assertIsInstance(got, Conflict)
                self.assertIn(reason, got.reason)

if __name__ == "__main__":
    unittest.main()
