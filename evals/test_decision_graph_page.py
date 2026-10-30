"""Owner's read-only view for traversing the decision graph (§17)."""

from __future__ import annotations

import contextlib
from datetime import datetime
from html import escape
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile
import unittest
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(HERE))

from backlog_ref import GitHubBacklog  # noqa: E402
import decision_graph as dg  # noqa: E402
import decision_graph_page as dgp  # noqa: E402
import decision_page as dp  # noqa: E402
from fragment import issue_href  # noqa: E402
import store  # noqa: E402
import task_forge  # noqa: E402
from test_page_theme import assert_carries_the_theme  # noqa: E402

ZONE = ZoneInfo("America/Chicago")
NOW = datetime(2026, 10, 29, 14, 0, tzinfo=ZONE)


class DecisionGraphPresenterTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.pages = self.tmp / "pages"
        self.pages.mkdir()
        self.db_path = self.tmp / "store.db"
        store.migrate(self.db_path)
        self.store = store.Store(self.db_path)
        self.addCleanup(self.store.engine.dispose)

    def page_data(self) -> task_forge.PageData:
        with self.store.transaction(write=False):
            return task_forge.page_data(self.store)

    def test_store_snapshot_reads_graph_nodes_without_logging_grants(self) -> None:
        root_node = self.store.add_graph_decision(
            "Use SQLite in WAL mode for workspace state.",
            "Single-host durable ACID state without an external daemon.",
            topic="storage",
            decided_by="owner",
            issue_key="github:example/repo#10",
            created_at="2026-10-28T09:00:00Z",
        )
        child_node = self.store.add_graph_decision(
            "Use SQLite WAL with short read-only page transactions.",
            "Keeps page renders non-blocking while workers write.",
            parent_ids=(root_node.id,),
            topic="storage",
            decided_by="trio",
            issue_key="github:example/repo#12",
            rejected_alternatives=(
                dg.RejectedAlternative(
                    approach="Shared long-lived write transaction in the page server",
                    reason="Blocks concurrent worker commits under WAL.",
                ),
            ),
            created_at="2026-10-29T10:00:00Z",
        )

        data = self.page_data()
        nodes = data.graph_nodes()
        self.assertEqual([n.id for n in nodes], [root_node.id, child_node.id])
        self.assertEqual(len(nodes[0].rejected_alternatives), 1)
        self.assertEqual(nodes[1].rejected_alternatives, ())
        self.assertEqual(self.store.pending_history_requests(), [])
        self.assertEqual(self.store.history_grants(), [])
        self.assertEqual(self.store.engine_log(), [])
        with contextlib.closing(sqlite3.connect(self.db_path)) as conn:
            req_count = conn.execute("SELECT COUNT(*) FROM decision_history_request").fetchone()[0]
            grant_count = conn.execute("SELECT COUNT(*) FROM decision_history_grant").fetchone()[0]
        self.assertEqual((req_count, grant_count), (0, 0))

    def test_empty_graph_index_carries_theme_and_is_read_only(self) -> None:
        page = dgp.index_page((), dp.Context(NOW))
        assert_carries_the_theme(self, page)
        self.assertIn("Decision graph", page)
        self.assertIn("0 current leaves", page)
        self.assertIn(">none<", page)
        self.assertIn('href="/decisions"', page)
        self.assertIn('href="/"', page)
        self.assertNotIn("<form", page)

    def test_index_lists_only_current_leaves_with_full_text_reason_and_links(self) -> None:
        n1 = self.store.add_graph_decision(
            "Store decisions in daily markdown tables.",
            "Initial human-edited format before the engine store existed.",
            topic="decisions",
            decided_by="owner",
        )
        n2 = self.store.add_graph_decision(
            "Store decisions as a DAG in SQLite.",
            "Lets current decisions stay small while preserving provenance.",
            parent_ids=(n1.id,),
            topic="decisions",
            decided_by="owner",
            rejected_alternatives=(
                dg.RejectedAlternative(
                    approach="Flat append-only text log",
                    reason="Cannot express superseding or multi-parent merges.",
                ),
            ),
        )
        n3 = self.store.add_graph_decision(
            "Render decision graph pages on request from the store.",
            "The owner reads and traverses the graph freely without authored files.",
            topic="pages",
            decided_by="owner",
            issue_key="github:team/app#44",
        )

        ctx = dp.Context(NOW, forge=GitHubBacklog("team/app"))
        page = dgp.index_page(self.page_data().graph_nodes(), ctx)
        assert_carries_the_theme(self, page)
        self.assertIn("2 current leaves", page)
        # Current leaves n2 and n3 appear with full decision text, reason, and per-node links.
        self.assertIn(f'href="/decisions/graph/{n2.id}"', page)
        self.assertIn(f'href="/decisions/graph/{n3.id}"', page)
        self.assertIn("Store decisions as a DAG in SQLite.", page)
        self.assertIn("Lets current decisions stay small while preserving provenance.", page)
        self.assertIn("Render decision graph pages on request from the store.", page)
        self.assertIn("The owner reads and traverses the graph freely without authored files.", page)
        self.assertIn(f'href="{escape(issue_href("github:team/app#44"))}">#44</a>', page)
        # Superseded ancestor n1 and its rejected alternatives do not clutter the current-leaves page.
        self.assertNotIn("Store decisions in daily markdown tables.", page)
        self.assertNotIn("Flat append-only text log", page)
        self.assertNotIn("<form", page)

    def test_node_page_links_parents_and_children_and_shows_rejected_alternatives(self) -> None:
        p1 = self.store.add_graph_decision(
            "Keep worker prompts minimal.",
            "Avoid flooding worker context with historical debates.",
            topic="prompts",
            decided_by="owner",
        )
        p2 = self.store.add_graph_decision(
            "Gate worker history queries behind harsh justification.",
            "Workers only see ancestors when current leaves conflict or fail.",
            topic="history",
            decided_by="architect",
        )
        child = self.store.add_graph_decision(
            "Inject only current leaves and expose a gated history tool.",
            "Combines minimal worker context with audited provenance access.",
            parent_ids=(p1.id, p2.id),
            topic="decisions",
            decided_by="owner",
            issue_key="github:other/repo#8",
            rejected_alternatives=(
                dg.RejectedAlternative(
                    approach="Inject every ancestor and rejected alternative into every worker",
                    reason="Pollutes worker context with dead options.",
                ),
            ),
        )
        self.store.add_rejected_alternative(
            p2.id,
            "Unrestricted history reads for workers",
            "Re-introduces anchoring on rejected approaches.",
        )

        nodes = self.page_data().graph_nodes()
        ctx = dp.Context(NOW, forge=GitHubBacklog("team/app"))

        # Leaf page: links up to both parents, has no children and no rejected alternatives.
        leaf_html = dgp.node_page(nodes, child.id, ctx)
        self.assertIsNotNone(leaf_html)
        assert_carries_the_theme(self, leaf_html)
        self.assertIn("Inject only current leaves and expose a gated history tool.", leaf_html)
        self.assertIn("Combines minimal worker context with audited provenance access.", leaf_html)
        self.assertIn(f'href="/decisions/graph/{p1.id}"', leaf_html)
        self.assertIn(f'href="/decisions/graph/{p2.id}"', leaf_html)
        self.assertIn('href="/decisions/graph"', leaf_html)
        self.assertIn(f'href="{escape(issue_href("github:other/repo#8"))}">other/repo#8</a>', leaf_html)
        self.assertNotIn("<form", leaf_html)

        # Ancestor p1 page: links down to child and shows rejected alternatives recorded on p1.
        p1_html = dgp.node_page(nodes, p1.id, ctx)
        self.assertIsNotNone(p1_html)
        self.assertIn("Keep worker prompts minimal.", p1_html)
        self.assertIn("Avoid flooding worker context with historical debates.", p1_html)
        self.assertIn(f'href="/decisions/graph/{child.id}"', p1_html)
        self.assertIn("Inject every ancestor and rejected alternative into every worker", p1_html)
        self.assertIn("Pollutes worker context with dead options.", p1_html)

        # Ancestor p2 page: links down to child and shows its own rejected alternative + reason.
        p2_html = dgp.node_page(nodes, p2.id, ctx)
        self.assertIsNotNone(p2_html)
        self.assertIn(f'href="/decisions/graph/{child.id}"', p2_html)
        self.assertIn("Unrestricted history reads for workers", p2_html)
        self.assertIn("Re-introduces anchoring on rejected approaches.", p2_html)

        # Unknown id returns None, and write_node removes any stale file.
        self.assertIsNone(dgp.node_page(nodes, 9999, ctx))
        stale = self.pages / "decisions-graph-9999.html"
        stale.write_text("stale", encoding="utf-8")
        self.assertIsNone(dgp.write_node(self.tmp, self.pages, 9999, "2026-10-29", db=self.page_data()))
        self.assertFalse(stale.exists())

    def test_cycles_in_graph_data_never_hang_index_or_node_pages(self) -> None:
        # 1. Synthetic cycle including a self-loop and a 3-node cycle.
        synthetic = (
            dg.AncestorDecision(id=1, decision="Self-linked", reason="Corrupt self edge", parent_ids=(1,)),
            dg.AncestorDecision(id=2, decision="Cycle A", reason="Links to B", parent_ids=(3,)),
            dg.AncestorDecision(id=3, decision="Cycle B", reason="Links to A", parent_ids=(2,)),
            dg.AncestorDecision(id=4, decision="Leaf over cycle", reason="Points into cycle", parent_ids=(2,)),
        )
        idx = dgp.index_page(synthetic, dp.Context(NOW))
        self.assertIn("Leaf over cycle", idx)
        self.assertIn('href="/decisions/graph/4"', idx)
        for nid in (1, 2, 3, 4):
            rendered = dgp.node_page(synthetic, nid, dp.Context(NOW))
            self.assertIsNotNone(rendered)

        # 2. Raw SQLite cycle inserted directly into decision_graph_edge.
        a = self.store.add_graph_decision("First node in cycle", "Reason A")
        b = self.store.add_graph_decision("Second node in cycle", "Reason B", parent_ids=(a.id,))
        leaf = self.store.add_graph_decision("Leaf above cycle", "Reason C", parent_ids=(b.id,))
        with contextlib.closing(sqlite3.connect(self.db_path)) as conn:
            conn.execute(
                "INSERT INTO decision_graph_edge (child_id, parent_id, position) VALUES (?, ?, 0)",
                (a.id, b.id),
            )
            conn.commit()

        nodes = self.page_data().graph_nodes()
        db_index = dgp.index_page(nodes, dp.Context(NOW))
        self.assertIn("Leaf above cycle", db_index)
        for nid in (a.id, b.id, leaf.id):
            page = dgp.node_page(nodes, nid, dp.Context(NOW))
            self.assertIsNotNone(page)

    def test_existing_decisions_index_links_to_decision_graph(self) -> None:
        html, _ = dp.index(self.pages, dp.Context(NOW), "2026-10-29", db=self.page_data())
        self.assertIn('href="/decisions/graph"', html)


if __name__ == "__main__":
    unittest.main()
