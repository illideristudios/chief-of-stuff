"""Decision graph entities, history judgement rules, precedent seam, store and MCP tool (#1181)."""

from __future__ import annotations

import sys
import tempfile
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


class DecisionGraphStoreAndAdapterTest(unittest.TestCase):
    def setUp(self) -> None:
        import decision_history as dh
        import store
        self.dh = dh
        self.store_mod = store
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = Path(self.tmp.name) / "store.db"
        store.migrate(self.db_path)
        self.store = store.Store(self.db_path)
        self.addCleanup(self.store.engine.dispose)

    def _seed_diamond_graph(self) -> tuple[dg.DecisionLeaf, dg.DecisionLeaf, dg.DecisionLeaf, dg.DecisionLeaf]:
        d1 = self.store.add_graph_decision(
            "Keep decisions in markdown tables.",
            "No database existed in early prototypes.",
            topic="precedent",
            decided_by="owner",
        )
        d2 = self.store.add_graph_decision(
            "Store decisions in SQLite behind SQLAlchemy ORM.",
            "Concurrent workers require transactional writes.",
            parent_ids=(d1.id,),
            topic="precedent-storage",
            decided_by="owner",
            rejected_alternatives=(
                dg.RejectedAlternative(
                    approach="Poll verdict in a model turn loop",
                    reason="Burns model tokens waiting on the coordinator.",
                ),
                dg.RejectedAlternative(
                    approach="Flat JSONL ledger without ORM",
                    reason="The owner requires an ORM over store.db.",
                ),
            ),
        )
        d3 = self.store.add_graph_decision(
            "Expose only current leaves to workers by default.",
            "Superseded decisions in context cause decision poisoning.",
            parent_ids=(d1.id,),
            topic="precedent-visibility",
            decided_by="owner",
        )
        d4 = self.store.add_graph_decision(
            "Serve current decision leaves through the engine MCP tool backed by SQLAlchemy ORM.",
            "Combines ACID storage with single-tool history gating.",
            parent_ids=(d2.id, d3.id),
            topic="decision-graph",
            decided_by="owner",
        )
        return d1, d2, d3, d4

    def test_store_persists_dag_exposes_only_leaves_and_forbids_rejected_alternatives_on_leaves(self) -> None:
        d1, d2, d3, d4 = self._seed_diamond_graph()
        leaves = self.store.decision_leaves()
        self.assertEqual([leaf.id for leaf in leaves], [d4.id])
        self.assertEqual(leaves[0].parent_ids, (d2.id, d3.id))
        self.assertIn("SQLAlchemy ORM", leaves[0].decision)
        self.assertIn("single-tool history gating", leaves[0].reason)

        # Attaching a rejected alternative to a current leaf is refused.
        with self.assertRaisesRegex(ValueError, "ancestors.*never in a leaf"):
            self.store.add_rejected_alternative(d4.id, "Direct SQLite reads", "Bypasses the approved tool.")

        # Creating a root leaf with rejected_alternatives (no parent ancestor) is also refused.
        with self.assertRaisesRegex(ValueError, "ancestors.*never in a leaf"):
            self.store.add_graph_decision(
                "Standalone leaf",
                "Standalone reason",
                rejected_alternatives=[{"approach": "Bad leaf alt", "reason": "Cannot live on leaf"}],
            )

        # Attaching a rejected alternative to an ancestor (d2) succeeds.
        added = self.store.add_rejected_alternative(
            d2.id, "Direct SQLite reads from workers", "Workers must reach history only via the approved tool."
        )
        self.assertEqual(added.approach, "Direct SQLite reads from workers")

    def test_free_readers_traverse_freely_while_worker_role_is_refused(self) -> None:
        d1, d2, d3, d4 = self._seed_diamond_graph()
        with self.assertRaises(PermissionError):
            self.store.read_decision_graph(role="worker", leaf_id=d4.id)

        for role in ("coordinator", "architect", "owner"):
            view = self.store.read_decision_graph(role=role, leaf_id=d4.id)
            self.assertEqual([leaf["id"] for leaf in view["leaves"]], [d4.id])
            self.assertEqual([a["id"] for a in view["ancestors"]], [d2.id, d3.id, d1.id])

    def test_approved_ancestors_grant_bounds_depth_logs_grant_and_reports_code_mismatch_to_architect(self) -> None:
        d1, d2, d3, d4 = self._seed_diamond_graph()

        # Unjustified request is refused before queueing.
        with self.assertRaisesRegex(ValueError, "justify"):
            self.store.submit_history_request(
                self.dh.HistoryRequest(kind="ancestors", leaf_id=d4.id, justification="")
            )

        # Justified code-mismatch request asking for depth 1 (immediate parents only, not grandparent d1).
        req_row = self.store.submit_history_request(
            self.dh.HistoryRequest(
                kind="ancestors",
                leaf_id=d4.id,
                requested_depth=1,
                issue_key="github:example/repo#1181",
                worker_id="session-abc",
                justification=(
                    f"Decision #{d4.id} requires serving leaves through the engine MCP tool, which does not match "
                    "what the code shows in scripts/engine_mcp.py:20 where only report and ask are registered."
                ),
            ),
            stage="implement",
            attempt=1,
        )
        self.assertEqual(req_row.status, "pending")
        # Pending queue carries only the request metadata and justification, never ancestor text.
        pending = self.store.pending_history_requests()
        self.assertEqual([r.id for r in pending], [req_row.id])
        self.assertFalse(hasattr(pending[0], "ancestors"))

        decided = self.store.judge_history_request(req_row.id)
        self.assertEqual(decided.status, "approved")
        self.assertEqual(decided.grant_case, "code_mismatch")
        self.assertEqual(decided.approved_depth, 1)
        self.assertTrue(decided.report_to_architect)

        payload = self.store.fulfill_history_request(req_row.id)
        self.assertEqual(payload["status"], "approved")
        self.assertEqual(payload["approved_depth"], 1)
        # Depth 1 returns immediate parents (d2, d3) and does NOT leak grandparent d1.
        self.assertEqual([a["id"] for a in payload["ancestors"]], [d2.id, d3.id])

        # Grant is durably logged in decision_history_grant and engine_log, plus architect mismatch logged.
        grants = self.store.history_grants()
        self.assertEqual(len(grants), 1)
        self.assertEqual(grants[0].request_id, req_row.id)
        self.assertEqual(grants[0].returned_ids, [d2.id, d3.id])

        kinds = [entry.kind for entry in self.store.engine_log()]
        self.assertIn("decision_history_grant", kinds)
        self.assertIn("architect_mismatch", kinds)

    def test_was_rejected_grant_returns_only_matching_alternative_and_reason_never_ancestors(self) -> None:
        self._seed_diamond_graph()
        req_row = self.store.submit_history_request(
            self.dh.HistoryRequest(
                kind="was_rejected",
                approach="poll verdict in a model turn loop",
                issue_key="github:example/repo#1181",
                worker_id="session-xyz",
                justification=(
                    "I am about to build worker waiting by polling verdict in a model turn loop; "
                    "the answer is not obvious because the current leaf covers MCP leaf reads "
                    "without stating how blocked history requests await verdicts."
                ),
            )
        )
        decided = self.store.judge_history_request(req_row.id)
        self.assertEqual(decided.status, "approved")

        payload = self.store.fulfill_history_request(req_row.id)
        self.assertEqual(payload["status"], "approved")
        self.assertTrue(payload["matched"])
        self.assertEqual(
            payload["rejected_alternative"],
            {
                "approach": "Poll verdict in a model turn loop",
                "reason": "Burns model tokens waiting on the coordinator.",
            },
        )
        self.assertNotIn("ancestors", payload)
        # Verify neither ancestor decision text nor sibling rejected alternative leaked anywhere in payload.
        serialized = repr(payload)
        self.assertNotIn("Keep decisions in markdown tables", serialized)
        self.assertNotIn("Flat JSONL ledger without ORM", serialized)

        grants = self.store.history_grants()
        self.assertEqual(len(grants), 1)
        self.assertEqual(grants[0].returned_ids, [])
        self.assertEqual(grants[0].matched_approach, "Poll verdict in a model turn loop")

    def test_workspace_precedent_source_replaces_tracker_decisions_with_graph_leaves_behind_seam(self) -> None:
        import precedent_source as ps

        ws = Path(self.tmp.name) / "ws"
        (ws / "days").mkdir(parents=True)
        (ws / "CLAUDE.md").write_text(
            "# Workspace\n\n## Coordinator\n- User: the owner\n- Daily log dir: `logs`\n- Tracker: `days/<date>.md`\n- Timezone: `UTC`\n",
            encoding="utf-8",
        )
        (ws / "days" / "2026-10-29.md").write_text(
            "# Tracker\n\n## Decisions\n"
            "| time | item | words |\n"
            "| --- | --- | --- |\n"
            "| 08:00 | legacy-precedent | Read Decisions rows from the markdown tracker. |\n",
            encoding="utf-8",
        )

        # Before any graph decisions exist, the workspace seam reads the tracker's Decisions rows.
        before = ps.workspace_precedent_source(ws, store_path=self.db_path, date="2026-10-29")
        ans_before = self.dh.lookup_precedent(before, "markdown tracker")
        self.assertEqual(len(ans_before), 1)
        self.assertIn("markdown tracker", ans_before[0].decision)

        # Once the decision graph has leaves, the exact same coordinator lookup answers from current leaves only.
        self._seed_diamond_graph()
        after = ps.workspace_precedent_source(ws, store_path=self.db_path, date="2026-10-29")
        ans_after = self.dh.lookup_precedent(after, "engine MCP tool")
        self.assertEqual(len(ans_after), 1)
        self.assertIn("SQLAlchemy ORM", ans_after[0].decision)
        # Superseded ancestor d1 ("Keep decisions in markdown tables.") is absent from precedent.
        self.assertEqual(self.dh.lookup_precedent(after, "early prototypes markdown tables"), ())


class DecisionGraphMCPAndCLITest(unittest.TestCase):
    def setUp(self) -> None:
        from datetime import datetime, timezone
        import engine_mcp
        import guard_binding
        import store

        self.store_mod = store
        self.engine_mcp = engine_mcp
        self.guard_binding = guard_binding
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        store.migrate(self.root / "store.db")
        self.db = store.Store(self.root / "store.db")
        self.addCleanup(self.db.engine.dispose)
        self.issue_key = "github:example/repo#1181"
        self.db.upsert_item(self.issue_key, title="Decision graph", stage="implement")

        self.d1 = self.db.add_graph_decision(
            "Keep decisions in markdown tables.",
            "No database existed in early prototypes.",
            topic="precedent",
            decided_by="owner",
        )
        self.d2 = self.db.add_graph_decision(
            "Store decisions in SQLite behind SQLAlchemy ORM.",
            "Concurrent workers require transactional writes.",
            parent_ids=(self.d1.id,),
            topic="precedent-storage",
            decided_by="owner",
            rejected_alternatives=(
                dg.RejectedAlternative(
                    approach="Poll verdict in a model turn loop",
                    reason="Burns model tokens waiting on the coordinator.",
                ),
            ),
        )
        self.d3 = self.db.add_graph_decision(
            "Expose only current leaves to workers by default.",
            "Superseded decisions in context cause decision poisoning.",
            parent_ids=(self.d1.id,),
            topic="precedent-visibility",
            decided_by="owner",
        )
        self.d4 = self.db.add_graph_decision(
            "Serve current decision leaves through the engine MCP tool backed by SQLAlchemy ORM.",
            "Combines ACID storage with single-tool history gating.",
            parent_ids=(self.d2.id, self.d3.id),
            topic="decision-graph",
            decided_by="owner",
        )

        self.env = {
            "CHIEF_ISSUE_KEY": self.issue_key,
            "CHIEF_STAGE": "implement",
            "CHIEF_ATTEMPT": "1",
            "CHIEF_SESSION_ID": "worker-1181",
            "CHIEF_ROOT": str(self.root),
            "CHIEF_LAUNCHED_AT": datetime.now(timezone.utc).isoformat(),
            "CHIEF_PYTHON": sys.executable,
        }
        self.binding = guard_binding.Binding.from_env(self.env)
        self.tools = engine_mcp.EngineTools(self.binding, self.db)

    @staticmethod
    def _run_coordinator_cli(argv: list[str]) -> int:
        import os
        from unittest import mock
        import decisions_cli

        clean_env = {k: v for k, v in os.environ.items() if k not in ("CHIEF_ISSUE_KEY", "CHIEF_SESSION_ID")}
        with mock.patch.dict("os.environ", clean_env, clear=True):
            return decisions_cli.main(argv)

    def test_mcp_decisions_returns_only_current_leaves_by_default_and_refuses_unjustified_history(self) -> None:
        import asyncio

        leaves_res = asyncio.run(self.tools.decisions())
        self.assertEqual([leaf["id"] for leaf in leaves_res["leaves"]], [self.d4.id])
        self.assertEqual(leaves_res["leaves"][0]["parent_ids"], [self.d2.id, self.d3.id])
        self.assertNotIn("rejected_alternatives", leaves_res["leaves"][0])
        self.assertNotIn("ancestors", leaves_res)

        refused = asyncio.run(self.tools.decisions(action="ancestors", leaf_id=self.d4.id))
        self.assertEqual(refused["status"], "refused")
        self.assertIn("justify", refused["message"].lower())
        self.assertEqual(self.db.pending_history_requests(), [])

        with self.assertRaises(TypeError):
            asyncio.run(self.tools.decisions(role="coordinator"))  # type: ignore[call-arg]

    def test_verdict_wait_handles_pre_recorded_and_mid_registration_verdicts_without_polling_or_lost_wakeup(self) -> None:
        import asyncio
        from unittest import mock
        import decision_history as dh
        import inbox

        justification = (
            f"Decision #{self.d4.id} requires the engine MCP tool to serve leaves, "
            "which does not match what the code shows in scripts/engine_mcp.py:20."
        )

        # Race 1: Coordinator records verdict BEFORE the worker opens the FIFO or starts waiting.
        pre_row = self.db.submit_history_request(
            dh.HistoryRequest(
                kind="ancestors",
                leaf_id=self.d4.id,
                requested_depth=1,
                issue_key=self.issue_key,
                worker_id="worker-1181",
                justification=justification,
            ),
            stage="implement",
            attempt=1,
        )
        self.assertFalse(self.store_mod.verdict_fifo_path(self.root, pre_row.id).exists())
        self.db.judge_history_request(pre_row.id)
        asyncio.run(
            asyncio.wait_for(self.tools._await_verdict(pre_row.id, fallback_interval=None), timeout=0.5)
        )
        self.assertEqual(self.db.fulfill_history_request(pre_row.id)["status"], "approved")

        # Race 2: Coordinator records verdict while `inbox.send_message` is still in flight (before wait() suspends),
        # with fallback polling disabled so wakeup relies strictly on the pre-opened FIFO + pre-check.
        orig_send = inbox.send_message

        def fast_coordinator_during_send(*args, **kwargs):
            result = orig_send(*args, **kwargs)
            req_id = kwargs["payload"]["request_id"]
            self.db.judge_history_request(req_id)
            return result

        self.tools._verdict_fallback_interval = None
        try:
            with mock.patch.object(inbox, "send_message", side_effect=fast_coordinator_during_send):
                mid_payload = asyncio.run(
                    asyncio.wait_for(
                        self.tools.decisions(
                            action="ancestors",
                            leaf_id=self.d4.id,
                            requested_depth=1,
                            justification=justification,
                        ),
                        timeout=0.5,
                    )
                )
        finally:
            self.tools._verdict_fallback_interval = 1.0
        self.assertEqual(mid_payload["status"], "approved")

        # Race 3: Coordinator commits and signals right after `_await_verdict` observes `status == "pending"`
        # and right before the event loop suspends on the FIFO reader (with fallback polling disabled).
        race_row = self.db.submit_history_request(
            dh.HistoryRequest(
                kind="ancestors",
                leaf_id=self.d4.id,
                requested_depth=1,
                issue_key=self.issue_key,
                worker_id="worker-1181",
                justification=justification,
            ),
            stage="implement",
            attempt=1,
        )
        orig_history_request = self.db.history_request
        triggered = False

        def commit_right_after_pending_read(request_id: int):
            nonlocal triggered
            row = orig_history_request(request_id)
            if request_id == race_row.id and not triggered and row is not None and row.status == "pending":
                triggered = True
                self.db.judge_history_request(request_id)
            return row

        with mock.patch.object(self.db, "history_request", side_effect=commit_right_after_pending_read):
            asyncio.run(
                asyncio.wait_for(self.tools._await_verdict(race_row.id, fallback_interval=None), timeout=0.5)
            )
        self.assertTrue(triggered)
        self.assertEqual(self.db.fulfill_history_request(race_row.id)["status"], "approved")

    def _run_stdio_ancestors_round_trip(self, server_env: dict[str, str]) -> dict:
        import asyncio
        import json
        import inbox
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        mailbox_dir = self.root / ".chief-of-stuff" / "mailbox"
        coord_incoming = mailbox_dir / "coordinator" / "incoming"

        async def run() -> dict:
            params = StdioServerParameters(
                command=sys.executable,
                args=[str(ROOT / "scripts/engine_mcp.py")],
                env=server_env,
                cwd=str(self.root),
            )
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write, read_timeout_seconds=10) as client:
                    await client.initialize()

                    async def coordinator_side() -> None:
                        pending = []
                        msg_files: list[Path] = []
                        for _ in range(300):
                            pending = self.db.pending_history_requests()
                            msg_files = list(coord_incoming.glob("msg_*"))
                            if pending and msg_files:
                                break
                            await asyncio.sleep(0.02)
                        self.assertEqual(len(pending), 1)
                        # Verify request reached coordinator mailbox without any ancestor history attached.
                        self.assertEqual(len(msg_files), 1)
                        msg = inbox.Message.from_file(msg_files[0])
                        self.assertNotIn("Keep decisions in markdown tables", repr(msg.to_dict()))
                        # Coordinator CLI judges pending requests without reading history.
                        rc = await asyncio.to_thread(
                            self._run_coordinator_cli, ["--root", str(self.root), "judge"]
                        )
                        self.assertEqual(rc, 0)

                    call_res, _ = await asyncio.gather(
                        client.call_tool(
                            "decisions",
                            {
                                "action": "ancestors",
                                "leaf_id": self.d4.id,
                                "requested_depth": 1,
                                "justification": (
                                    f"Decision #{self.d4.id} requires the engine MCP tool to serve leaves, "
                                    "which does not match what the code shows in scripts/engine_mcp.py:20."
                                ),
                            },
                        ),
                        coordinator_side(),
                    )
                    self.assertFalse(call_res.is_error, call_res)
                    return json.loads(call_res.content[0].text)

        return asyncio.run(run())

    def test_stdio_mcp_blocks_worker_until_coordinator_judges_and_delivers_bounded_slice_without_model_polling(self) -> None:
        mailbox_dir = self.root / ".chief-of-stuff" / "mailbox"
        payload = self._run_stdio_ancestors_round_trip(self.env)
        self.assertEqual(payload["status"], "approved")
        self.assertEqual(payload["approved_depth"], 1)
        self.assertEqual([a["id"] for a in payload["ancestors"]], [self.d2.id, self.d3.id])
        self.assertEqual(len(self.db.history_grants()), 1)

        # Code mismatch was also reported to the architect mailbox.
        arch_files = list((mailbox_dir / "architect" / "incoming").glob("msg_*"))
        self.assertEqual(len(arch_files), 1)

    def test_stdio_mcp_survives_slow_mailbox_fsync_without_losing_coordinator_or_timing_out(self) -> None:
        import os

        with tempfile.TemporaryDirectory() as shim:
            Path(shim, "sitecustomize.py").write_text(
                "import os, time\n"
                "_orig_fsync = os.fsync\n"
                "def _slow_fsync(fd):\n"
                "    time.sleep(0.04)\n"
                "    return _orig_fsync(fd)\n"
                "os.fsync = _slow_fsync\n",
                encoding="utf-8",
            )
            existing_pp = os.environ.get("PYTHONPATH", "")
            slow_env = {
                **self.env,
                "PYTHONPATH": f"{shim}{os.pathsep}{existing_pp}" if existing_pp else shim,
            }
            payload = self._run_stdio_ancestors_round_trip(slow_env)
        self.assertEqual(payload["status"], "approved")
        self.assertEqual([a["id"] for a in payload["ancestors"]], [self.d2.id, self.d3.id])

    def test_stdio_mcp_was_rejected_and_denial_wake_blocked_worker_with_narrow_or_denied_verdict(self) -> None:
        import asyncio
        import json
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        async def run() -> tuple[dict, dict]:
            params = StdioServerParameters(
                command=sys.executable,
                args=[str(ROOT / "scripts/engine_mcp.py")],
                env=self.env,
                cwd=str(self.root),
            )
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write, read_timeout_seconds=10) as client:
                    await client.initialize()

                    async def approve_narrow() -> None:
                        for _ in range(300):
                            reqs = self.db.pending_history_requests()
                            if reqs:
                                rc = await asyncio.to_thread(
                                    self._run_coordinator_cli,
                                    [
                                        "--root", str(self.root),
                                        "decide",
                                        "--request", str(reqs[0].id),
                                        "--approve",
                                        "--reason", "Specific non-obvious approach named.",
                                    ],
                                )
                                self.assertEqual(rc, 0)
                                return
                            await asyncio.sleep(0.02)
                        self.fail("pending was_rejected request never arrived")

                    res1, _ = await asyncio.gather(
                        client.call_tool(
                            "decisions",
                            {
                                "action": "was_rejected",
                                "approach": "poll verdict in a model turn loop",
                                "justification": (
                                    "I am about to build worker waiting by polling verdict in a model turn loop; "
                                    "not obvious because current leaf #4 does not state the waiting mechanism."
                                ),
                            },
                        ),
                        approve_narrow(),
                    )
                    self.assertFalse(res1.is_error, res1)

                    async def deny_fishing() -> None:
                        for _ in range(300):
                            reqs = self.db.pending_history_requests()
                            if reqs:
                                rc = await asyncio.to_thread(
                                    self._run_coordinator_cli,
                                    ["--root", str(self.root), "judge", "--request", str(reqs[0].id)],
                                )
                                self.assertEqual(rc, 0)
                                return
                            await asyncio.sleep(0.02)
                        self.fail("pending fishing request never arrived")

                    res2, _ = await asyncio.gather(
                        client.call_tool(
                            "decisions",
                            {
                                "action": "ancestors",
                                "leaf_id": self.d4.id,
                                "justification": "Need more context on earlier decisions.",
                            },
                        ),
                        deny_fishing(),
                    )
                    self.assertFalse(res2.is_error, res2)
                    return json.loads(res1.content[0].text), json.loads(res2.content[0].text)

        narrow, denied = asyncio.run(run())
        self.assertEqual(narrow["status"], "approved")
        self.assertTrue(narrow["matched"])
        self.assertEqual(narrow["rejected_alternative"]["approach"], "Poll verdict in a model turn loop")
        self.assertNotIn("ancestors", narrow)

        self.assertEqual(denied["status"], "denied")
        self.assertIn("context", denied["reason"].lower())
        # Only the 1 approved grant was logged; the denied request logged no grant.
        self.assertEqual(len(self.db.history_grants()), 1)

    def test_decisions_cli_allows_free_readers_and_refuses_worker_bound_shell(self) -> None:
        import io
        import json
        from contextlib import redirect_stderr, redirect_stdout
        from unittest import mock
        import decisions_cli

        out = io.StringIO()
        with mock.patch.dict("os.environ", {}, clear=False):
            # Ensure no worker env vars are set in coordinator/owner shell.
            clean_env = {k: v for k, v in __import__("os").environ.items() if not k.startswith("CHIEF_")}
            with mock.patch.dict("os.environ", clean_env, clear=True), redirect_stdout(out):
                rc = decisions_cli.main(
                    ["--root", str(self.root), "--json", "read", "--role", "owner", "--leaf", str(self.d4.id)]
                )
        self.assertEqual(rc, 0)
        parsed = json.loads(out.getvalue())
        self.assertEqual([a["id"] for a in parsed["ancestors"]], [self.d2.id, self.d3.id, self.d1.id])

        # Inside a worker-bound environment, decisions_cli refuses so workers cannot bypass the MCP tool.
        err = io.StringIO()
        with mock.patch.dict("os.environ", self.env, clear=False), redirect_stderr(err):
            rc_worker = decisions_cli.main(
                ["--root", str(self.root), "read", "--role", "coordinator"]
            )
        self.assertEqual(rc_worker, 1)
        self.assertIn("refused", err.getvalue().lower())

if __name__ == "__main__":
    unittest.main()
