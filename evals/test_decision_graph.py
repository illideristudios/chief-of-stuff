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


class DecisionHistoryUseCaseTest(unittest.TestCase):
    def setUp(self) -> None:
        import decision_history as dh
        self.dh = dh
        self.leaves = (
            dg.DecisionLeaf(
                id=2,
                topic="writes",
                decision="Every worker state change must write synchronously to store.db inside the tool call.",
                reason="Crash recovery requires durable state before returning.",
                parent_ids=(1,),
            ),
            dg.DecisionLeaf(
                id=3,
                topic="loop",
                decision="Async MCP handlers must forbid synchronous disk writes on the event loop thread.",
                reason="Blocking SQLite calls stall concurrent stdio notifications.",
                parent_ids=(1,),
            ),
        )

    def test_unjustified_request_is_refused_and_told_to_justify(self) -> None:
        refusal = self.dh.validate_worker_request(
            self.dh.HistoryRequest(kind="ancestors", leaf_id=2, justification="   ")
        )
        self.assertIsNotNone(refusal)
        self.assertIn("justify", refusal.lower())

        refusal_rj = self.dh.validate_worker_request(
            self.dh.HistoryRequest(kind="was_rejected", approach="unix pipe", justification="")
        )
        self.assertIsNotNone(refusal_rj)
        self.assertIn("justify", refusal_rj.lower())

    def test_free_readers_are_only_coordinator_architect_and_owner(self) -> None:
        for role in ("coordinator", "architect", "owner"):
            self.assertTrue(self.dh.reads_freely(role))
        for role in ("worker", "implementer", "reviewer", ""):
            self.assertFalse(self.dh.reads_freely(role))

    def test_coordinator_judges_contradiction_and_code_mismatch_without_reading_history(self) -> None:
        # Grant case 1: two current leaves contradict, stating what each requires that cannot both hold.
        contradiction = self.dh.HistoryRequest(
            kind="ancestors",
            leaf_id=2,
            other_leaf_id=3,
            requested_depth=1,
            justification=(
                "Decision #2 requires synchronous store.db writes inside every tool call, whereas "
                "decision #3 forbids synchronous disk writes inside async MCP handlers; both cannot hold "
                "when the tool handler is async."
            ),
        )
        verdict = self.dh.judge_justification(contradiction, self.leaves)
        self.assertTrue(verdict.approved, verdict.reason)
        self.assertEqual(verdict.grant_case, "contradiction")
        self.assertEqual(verdict.approved_depth, 1)
        self.assertFalse(verdict.report_to_architect)

        # Bare claim of contradiction without stating what each requires is denied.
        bare = self.dh.HistoryRequest(
            kind="ancestors",
            leaf_id=2,
            other_leaf_id=3,
            justification="Decision #2 and decision #3 contradict each other.",
        )
        self.assertFalse(self.dh.judge_justification(bare, self.leaves).approved)

        # Grant case 2: current leaf does not match concrete code evidence, reported to the architect.
        mismatch = self.dh.HistoryRequest(
            kind="ancestors",
            leaf_id=3,
            requested_depth=1,
            justification=(
                "Decision #3 forbids synchronous disk writes in async MCP handlers, which does not match "
                "what the code shows in scripts/engine_mcp.py:67 where Store.record_worker_report runs synchronously."
            ),
        )
        m_verdict = self.dh.judge_justification(mismatch, self.leaves)
        self.assertTrue(m_verdict.approved, m_verdict.reason)
        self.assertEqual(m_verdict.grant_case, "code_mismatch")
        self.assertTrue(m_verdict.report_to_architect)
        self.assertEqual(m_verdict.approved_depth, 1)

    def test_coordinator_harshly_denies_banned_and_uncovered_rationales(self) -> None:
        denied_texts = (
            "A reviewer finding contradicts decision #2 on scripts/engine_mcp.py:67.",
            "Need more context on why decision #2 replaced #1.",
            "Decision #1 looks related to the store migration I am writing.",
            "The decision is unclear about how transactions commit.",
            "Double-checking my own work against earlier storage rules.",
            "I want to understand the history of scripts/store.py before refactoring.",
        )
        for text in denied_texts:
            with self.subTest(text=text):
                req = self.dh.HistoryRequest(kind="ancestors", leaf_id=2, justification=text)
                verdict = self.dh.judge_justification(req, self.leaves)
                self.assertFalse(verdict.approved, f"expected harsh denial for: {text}")

        # Requesting history on an id that is not a current leaf is denied.
        non_leaf = self.dh.HistoryRequest(
            kind="ancestors",
            leaf_id=99,
            justification=(
                "Decision #99 requires X whereas scripts/engine_mcp.py:10 does not match what the code shows."
            ),
        )
        self.assertFalse(self.dh.judge_justification(non_leaf, self.leaves).approved)

    def test_was_rejected_judgement_grants_specific_non_obvious_approach_and_denies_vague_queries(self) -> None:
        valid = self.dh.HistoryRequest(
            kind="was_rejected",
            approach="polling store.db in a model turn loop",
            justification=(
                "I am about to build verdict delivery by polling store.db in a model turn loop; "
                "the answer is not obvious because current leaves #2 and #3 cover write durability "
                "and event-loop blocking without stating how a blocked worker waits for a verdict."
            ),
        )
        verdict = self.dh.judge_justification(valid, self.leaves)
        self.assertTrue(verdict.approved, verdict.reason)
        self.assertEqual(verdict.grant_case, "rejected_approach")
        self.assertIsNone(verdict.approved_depth)

        vague_cases = (
            ("", "Has anything like this been tried before in the engine?"),
            ("has anything like this been tried", "Checking whether anything like this has been tried."),
            ("sqlite", "Need more context before building."),
            ("polling loop", "Just curious."),
        )
        for approach, justification in vague_cases:
            with self.subTest(approach=approach, justification=justification):
                req = self.dh.HistoryRequest(
                    kind="was_rejected",
                    approach=approach,
                    justification=justification,
                )
                self.assertFalse(self.dh.judge_justification(req, self.leaves).approved)

    def test_precedent_seam_swaps_tracker_decisions_for_graph_leaves_without_changing_consumer(self) -> None:
        tracker_md = (
            "# Day Tracker\n\n"
            "## Decisions\n"
            "| time | item | words |\n"
            "| --- | --- | --- |\n"
            "| 09:15 | worker transport | Use stdio MCP for worker reports. |\n"
            "| 10:30 | store ORM | All store access goes through SQLAlchemy models. |\n"
        )

        class StaticSource:
            def __init__(self, entries: tuple[dg.PrecedentEntry, ...]) -> None:
                self._entries = entries

            def current_precedent(self) -> tuple[dg.PrecedentEntry, ...]:
                return self._entries

        # Same coordinator lookup function answers from tracker rows today and graph leaves tomorrow.
        tracker_source = StaticSource(self.dh.tracker_precedent_entries(tracker_md))
        graph_source = StaticSource(self.dh.graph_precedent_entries(self.leaves))

        from_tracker = self.dh.lookup_precedent(tracker_source, "worker reports transport")
        self.assertEqual(len(from_tracker), 1)
        self.assertIn("stdio MCP", from_tracker[0].decision)

        from_graph = self.dh.lookup_precedent(graph_source, "crash recovery store.db")
        self.assertEqual(len(from_graph), 1)
        self.assertEqual(from_graph[0].id, "decision:2")
        self.assertIn("Crash recovery", from_graph[0].reason)

        ranked = self.dh.lookup_precedent(graph_source, "synchronous store.db writes")
        self.assertEqual([e.id for e in ranked], ["decision:2", "decision:3"])


if __name__ == "__main__":
    unittest.main()
