"""Decision graph entities, history judgement rules, precedent seam, store and MCP tool (#1181)."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import decision_graph as dg


class DecisionGraphEntityTest(unittest.TestCase):
    def test_leaves_expose_only_unreplaced_decisions_with_full_text_reason_and_parent_links(self) -> None:
        n1 = dg.AncestorDecision(id=1, topic="storage", decision="Store tasks in markdown tables.",
                                 reason="Plain files need no daemon.")
        n2 = dg.AncestorDecision(id=2, topic="storage", decision="Store tasks in SQLite behind the ORM.",
                                 reason="Concurrent writers need ACID transactions.", parent_ids=(1,))
        n3 = dg.AncestorDecision(id=3, topic="transport", decision="Use stdio MCP for worker reports.",
                                 reason="Launch environment binds identity.")
        parents = {1: (), 2: (1,), 3: ()}

        self.assertEqual(dg.leaf_ids(parents), frozenset({2, 3}))
        self.assertEqual(dg.ancestor_ids(parents), frozenset({1}))

        leaves = dg.current_leaves((n1, n2, n3), parents)
        self.assertEqual([leaf.id for leaf in leaves], [2, 3])
        self.assertEqual(leaves[0].decision, "Store tasks in SQLite behind the ORM.")
        self.assertEqual(leaves[0].reason, "Concurrent writers need ACID transactions.")
        self.assertEqual(leaves[0].parent_ids, (1,))
        self.assertFalse(hasattr(leaves[0], "rejected_alternatives"))

    def test_provenance_is_a_dag_not_a_strict_tree_and_forbids_cycles(self) -> None:
        # Diamond / multi-parent merge: 1 -> (2, 3) -> 4 (4 replaces both 2 and 3; 2 and 3 both share parent 1).
        parents: dict[int, tuple[int, ...]] = {1: ()}
        parents[2] = dg.validate_parent_links(parents, 2, [1])
        parents[3] = dg.validate_parent_links(parents, 3, [1])
        parents[4] = dg.validate_parent_links(parents, 4, [2, 3, 2])  # deduplicates preserving order
        self.assertEqual(parents[4], (2, 3))
        self.assertEqual(dg.leaf_ids(parents), frozenset({4}))
        self.assertEqual(dg.ancestor_ids(parents), frozenset({1, 2, 3}))

        # Self-parent is refused.
        with self.assertRaisesRegex(ValueError, "self"):
            dg.validate_parent_links(parents, 5, [5])

        # Unknown parent is refused.
        with self.assertRaisesRegex(ValueError, "unknown parent"):
            dg.validate_parent_links(parents, 5, [99])

        # Cycle (making 1 depend on 4 when 4 already reaches 1) is refused.
        with self.assertRaisesRegex(ValueError, "cycle"):
            dg.validate_parent_links(parents, 1, [4])

    def test_rejected_alternatives_live_only_in_ancestors_never_in_a_leaf(self) -> None:
        parents = {1: (), 2: (1,)}
        # Node 1 is an ancestor (replaced by 2): allowed.
        dg.validate_rejected_target(parents, 1)

        # Node 2 is a current leaf: refused.
        with self.assertRaisesRegex(ValueError, "ancestors.*never in a leaf"):
            dg.validate_rejected_target(parents, 2)

        # Unknown node: refused.
        with self.assertRaisesRegex(ValueError, "unknown"):
            dg.validate_rejected_target(parents, 99)

    def test_ancestor_slice_bounds_depth_and_deduplicates_dag_paths(self) -> None:
        alt = dg.RejectedAlternative(approach="Flat CSV files", reason="No locking or schema enforcement.")
        nodes = {
            1: dg.AncestorDecision(id=1, decision="D1", reason="R1", rejected_alternatives=(alt,)),
            2: dg.AncestorDecision(id=2, decision="D2", reason="R2", parent_ids=(1,)),
            3: dg.AncestorDecision(id=3, decision="D3", reason="R3", parent_ids=(1,)),
            4: dg.AncestorDecision(id=4, decision="D4", reason="R4", parent_ids=(2, 3)),
        }

        depth_one = dg.ancestor_slice(nodes, 4, max_depth=1)
        self.assertEqual([n.id for n in depth_one], [2, 3])

        depth_two = dg.ancestor_slice(nodes, 4, max_depth=2)
        self.assertEqual([n.id for n in depth_two], [2, 3, 1])
        self.assertEqual(depth_two[-1].rejected_alternatives, (alt,))

        unbounded = dg.ancestor_slice(nodes, 4, max_depth=None)
        self.assertEqual([n.id for n in unbounded], [2, 3, 1])

        with self.assertRaises(ValueError):
            dg.ancestor_slice(nodes, 4, max_depth=0)
        with self.assertRaises(KeyError):
            dg.ancestor_slice(nodes, 999, max_depth=1)

    def test_find_rejected_alternative_searches_only_ancestors_and_returns_narrow_match(self) -> None:
        wanted = dg.RejectedAlternative(
            approach="Poll verdict in a model loop",
            reason="Wastes model turns while waiting on the coordinator.",
        )
        other = dg.RejectedAlternative(
            approach="Shared HTTP server per worker",
            reason="Requires port allocation and teardown.",
        )
        # Even if an in-memory caller constructed a leaf with rejected_alternatives, leaf entries are ignored.
        leaf_leak = dg.RejectedAlternative(
            approach="Leaf-attached approach",
            reason="Must never be readable from a leaf.",
        )
        nodes = {
            1: dg.AncestorDecision(id=1, decision="Secret ancestor decision", reason="Secret ancestor reason",
                                   rejected_alternatives=(wanted, other)),
            2: dg.AncestorDecision(id=2, decision="Current leaf decision", reason="Current leaf reason",
                                   parent_ids=(1,), rejected_alternatives=(leaf_leak,)),
        }
        parents = {1: (), 2: (1,)}

        hit = dg.find_rejected_alternative(nodes, parents, "poll verdict in a model loop")
        self.assertIsNotNone(hit)
        ancestor_id, matched = hit
        self.assertEqual(ancestor_id, 1)
        self.assertEqual(matched, wanted)

        self.assertIsNone(dg.find_rejected_alternative(nodes, parents, "Leaf-attached approach"))
        self.assertIsNone(dg.find_rejected_alternative(nodes, parents, "Unrelated approach"))


if __name__ == "__main__":
    unittest.main()
