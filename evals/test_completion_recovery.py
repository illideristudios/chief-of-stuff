"""Orphan recovery: which tree and branch to reuse, or why to ask (architecture §4, completion.py)."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from completion import Conflict, OrphanTree, Recovery, recovery
import completion


def tree(path, head="a", branch="work/issue-1-implement-1", **kw):
    return OrphanTree(path=path, branch=branch, head=head, **kw)


class RecoveryTest(unittest.TestCase):
    def test_unreadable_or_quarantined_work_is_not_reused(self):
        for bad in (tree("/t/a", unreadable=True), tree("/t/a", quarantined=True),
                    tree("/t/a", unreadable=True, quarantined=True)):
            with self.subTest(bad=bad):
                self.assertIsInstance(recovery([bad]), completion.Quarantine)
        self.assertIsInstance(recovery([tree("/t/a"), tree("/t/b", unreadable=True)]), completion.Quarantine)

    def test_holds_are_judged_from_latest_answers_and_current_conflict(self):
        decisions = [("conflict", "implement\nold", "hold"),
                     ("conflict", "implement\nold", "abort"),
                     ("conflict", "fix\ncurrent", "hold"),
                     ("other", "review\ncurrent", "hold")]
        self.assertEqual(completion.held_stages(decisions, conflict="current", readable=True), {"fix"})
        self.assertEqual(completion.held_stages(decisions, conflict=None, readable=True), set())
        self.assertEqual(completion.held_stages(decisions, conflict=None, readable=False), {"fix"})

    def test_recovery_fact_gatherer_returns_decisions_without_judging_them(self):
        from unittest import mock
        from types import SimpleNamespace
        import engine_facts
        rows = [SimpleNamespace(topic="conflict", question="implement\na", answer="abort"),
                SimpleNamespace(topic="conflict", question="review\nb", answer="hold")]
        store = SimpleNamespace(decisions=lambda key: rows)
        with mock.patch.object(engine_facts.worker, "recovery_conflict", return_value="a"):
            facts = engine_facts.recovery(store, "repo#1", trees=Path("/trees"), repo=Path("/repo"))
        self.assertEqual(facts["decisions"], [(r.topic, r.question, r.answer) for r in rows])
        self.assertEqual(facts["conflict"], "a")
        self.assertTrue(facts["readable"])

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
