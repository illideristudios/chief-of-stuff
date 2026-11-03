"""Owner's read-only view for traversing the decision graph (§17)."""

from __future__ import annotations

import contextlib
from datetime import datetime
from html import escape
from pathlib import Path
import re
import shutil
import sqlite3
import sys
import tempfile
import threading
import unittest
from unittest import mock
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
import pages as pg  # noqa: E402
import store  # noqa: E402
import task_forge  # noqa: E402
from test_page_theme import assert_carries_the_theme  # noqa: E402
from test_pages import LANES, TRACKER, ZONE as PAGES_ZONE, get, post  # noqa: E402

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


class DecisionGraphHttpServerTest(unittest.TestCase):
    def setUp(self) -> None:
        parent = tempfile.TemporaryDirectory()
        self.addCleanup(parent.cleanup)
        self.root = Path(parent.name).resolve() / "ws"
        self.root.mkdir()
        self.day = datetime.now(ZoneInfo(PAGES_ZONE)).date().isoformat()
        (self.root / "CLAUDE.md").write_text(
            "## Coordinator\n- User: the owner\n- Daily log dir: `daily/`\n- Tracker: `daily/<date>-tracker.md`\n"
            f"- Timezone: {PAGES_ZONE}\n- Board: self-hosted; URL http://127.0.0.1:8765/; dir `pages/`\n"
            "- Settings: `cos.toml`\n",
            encoding="utf-8",
        )
        (self.root / "daily").mkdir()
        (self.root / "daily" / f"{self.day}-tracker.md").write_text(
            TRACKER.format(day=self.day, task="Security audit"),
            encoding="utf-8",
        )
        self.pages = self.root / "pages"
        self.pages.mkdir()
        (self.root / "cos.toml").write_text(LANES, encoding="utf-8")
        self.refreshed = threading.Event()
        self.server = pg.make_server(self.pages, 0, root=self.root, refresh=self._fake_refresh, every=3600)
        self.port = self.server.server_address[1]
        self.origin = f"http://127.0.0.1:{self.port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.assertTrue(self.refreshed.wait(10))

    def _fake_refresh(self, root: Path, now: datetime):
        import board_sources
        try:
            return board_sources.refresh(root, now)
        finally:
            self.refreshed.set()

    def _open_store(self) -> store.Store:
        db_path = self.root / "store.db"
        store.migrate(db_path)
        return store.Store(db_path)

    def test_decisions_links_to_graph_and_empty_workspace_serves_empty_graph(self) -> None:
        decisions = get(self.port, "/decisions")
        self.assertEqual(decisions.status, 200)
        self.assertIn('href="/decisions/graph"', decisions.body.decode())

        empty_graph = get(self.port, "/decisions/graph")
        self.assertEqual(empty_graph.status, 200)
        body = empty_graph.body.decode()
        self.assertIn("0 current leaves", body)
        self.assertIn(">none<", body)
        self.assertFalse((self.root / "store.db").exists())

        # Without a store, any node id returns the same 404 refusal page as /decisions/<id>.
        missing = get(self.port, "/decisions/graph/1")
        neighbour = get(self.port, "/decisions/1")
        self.assertEqual(missing.status, 404)
        self.assertEqual(missing.body, neighbour.body)

    def test_traverse_up_and_down_from_store_without_grants_writes_or_history_tool(self) -> None:
        db = self._open_store()
        try:
            db.upsert_item("github:example/repo#42", title="Engine task", stage="implement", forge_state="open")
            db.add_decision("github:example/repo#42", "scope", "Pending ask for nav badge", decided_by="owner")
            db.add_decision("github:example/repo#42", "scope", "Engine retry awaiting models")
            ancestor = db.add_graph_decision(
                "Keep decision records in markdown files.",
                "Bootstrapped before the SQLite store.",
                topic="decisions",
                decided_by="owner",
            )
            leaf = db.add_graph_decision(
                "Record decisions as a provenance DAG in SQLite.",
                "Workers receive only current leaves; the owner walks the full graph.",
                parent_ids=(ancestor.id,),
                topic="decisions",
                decided_by="owner",
                issue_key="github:example/repo#42",
                rejected_alternatives=(
                    dg.RejectedAlternative(
                        approach="Expose all rejected alternatives in every worker prompt",
                        reason="Wastes context and anchors workers on dead designs.",
                    ),
                ),
            )
        finally:
            db.engine.dispose()

        import decisions_cli
        with (
            mock.patch.object(store.Store, "submit_history_request", side_effect=AssertionError("history tool called")),
            mock.patch.object(store.Store, "fulfill_history_request", side_effect=AssertionError("history grant called")),
            mock.patch.object(decisions_cli, "main", side_effect=AssertionError("decisions cli called")),
        ):
            graph_resp = get(self.port, "/decisions/graph")
            self.assertEqual(graph_resp.status, 200)
            graph_html = graph_resp.body.decode()
            self.assertIn("1 current leaf", graph_html)
            self.assertIn("Record decisions as a provenance DAG in SQLite.", graph_html)
            self.assertIn("Workers receive only current leaves; the owner walks the full graph.", graph_html)
            self.assertIn(f'href="/decisions/graph/{leaf.id}"', graph_html)
            self.assertNotIn("Keep decision records in markdown files.", graph_html)
            # Carries shared navigation counts from the store.
            self.assertIn('title="1 pending">1', graph_html)
            decisions_html = get(self.port, "/decisions").body.decode()
            self.assertIn('title="1 pending">1', decisions_html)
            self.assertIn("Pending ask for nav badge", decisions_html)
            self.assertNotIn("Engine retry awaiting models", decisions_html)

            # Walk to the leaf, then up to its parent, then see the link back down to the child.
            leaf_resp = get(self.port, f"/decisions/graph/{leaf.id}")
            self.assertEqual(leaf_resp.status, 200)
            leaf_html = leaf_resp.body.decode()
            self.assertIn('title="1 pending">1', leaf_html)
            self.assertIn(f'href="/decisions/graph/{ancestor.id}"', leaf_html)
            self.assertNotIn("Expose all rejected alternatives in every worker prompt", leaf_html)

            head_resp = get(self.port, f"/decisions/graph/{leaf.id}", method="HEAD")
            self.assertEqual(head_resp.status, 200)
            self.assertEqual(int(head_resp.getheader("Content-Length")), len(leaf_resp.body))

            parent_resp = get(self.port, f"/decisions/graph/{ancestor.id}")
            self.assertEqual(parent_resp.status, 200)
            parent_html = parent_resp.body.decode()
            self.assertIn('title="1 pending">1', parent_html)
            self.assertIn("Keep decision records in markdown files.", parent_html)
            self.assertIn("Bootstrapped before the SQLite store.", parent_html)
            self.assertIn(f'href="/decisions/graph/{leaf.id}"', parent_html)
            self.assertIn("Expose all rejected alternatives in every worker prompt", parent_html)
            self.assertIn("Wastes context and anchors workers on dead designs.", parent_html)

        # Updating the store re-renders /decisions/graph on the next request.
        db = self._open_store()
        try:
            newer = db.add_graph_decision(
                "Add a read-only /decisions/graph view on the page server.",
                "Lets the owner walk parents, children, and rejected alternatives.",
                parent_ids=(leaf.id,),
                topic="pages",
                decided_by="owner",
            )
            self.assertEqual(db.pending_history_requests(), [])
            self.assertEqual(db.history_grants(), [])
            self.assertEqual(db.engine_log(), [])
        finally:
            db.engine.dispose()

        refreshed = get(self.port, "/decisions/graph").body.decode()
        self.assertIn(f'href="/decisions/graph/{newer.id}"', refreshed)
        self.assertIn("Add a read-only /decisions/graph view on the page server.", refreshed)
        self.assertNotIn("Record decisions as a provenance DAG in SQLite.", refreshed)

        with contextlib.closing(sqlite3.connect(self.root / "store.db")) as conn:
            req_count = conn.execute("SELECT COUNT(*) FROM decision_history_request").fetchone()[0]
            grant_count = conn.execute("SELECT COUNT(*) FROM decision_history_grant").fetchone()[0]
        self.assertEqual((req_count, grant_count), (0, 0))

        # Rendered HTML files are routes only (404 by filename, omitted from /all), and POST is refused.
        for direct in ("/decisions-graph.html", f"/decisions-graph-{leaf.id}.html", "/Decisions-Graph.html"):
            with self.subTest(direct=direct):
                self.assertEqual(get(self.port, direct).status, 404)
        listing = get(self.port, "/all").body.decode()
        self.assertNotIn("decisions-graph", listing)

        (self.pages / "decision-graph.json").write_text('{"bad": true}', encoding="utf-8")
        for target in ("/decisions/graph", f"/decisions/graph/{leaf.id}"):
            with self.subTest(post_target=target):
                resp = post(self.port, target, b"key=A&words=attempt", origin=self.origin)
                self.assertEqual(resp.status, 405)

    def test_unknown_and_invalid_graph_ids_match_neighbour_not_found_page(self) -> None:
        db = self._open_store()
        try:
            node = db.add_graph_decision("Single root decision.", "Initial reason.")
        finally:
            db.engine.dispose()

        neighbour_404 = get(self.port, "/decisions/999")
        self.assertEqual(neighbour_404.status, 404)
        self.assertIn(b"No such decision. Return to the decisions list or board.", neighbour_404.body)

        for bad in ("/decisions/graph/999", "/decisions/graph/0", "/decisions/graph/01",
                    "/decisions/graph/nope", f"/decisions/graph/{node.id}/extra"):
            with self.subTest(bad=bad):
                resp = get(self.port, bad)
                self.assertEqual(resp.status, 404)
                self.assertEqual(resp.body, neighbour_404.body)

    def test_cycle_in_store_edges_does_not_hang_http_routes(self) -> None:
        db = self._open_store()
        try:
            n1 = db.add_graph_decision("Cycle node one.", "First reason.")
            n2 = db.add_graph_decision("Cycle node two.", "Second reason.", parent_ids=(n1.id,))
            n3 = db.add_graph_decision("Leaf pointing into cycle.", "Third reason.", parent_ids=(n2.id,))
        finally:
            db.engine.dispose()

        with contextlib.closing(sqlite3.connect(self.root / "store.db")) as conn:
            conn.execute(
                "INSERT INTO decision_graph_edge (child_id, parent_id, position) VALUES (?, ?, 0)",
                (n1.id, n2.id),
            )
            conn.commit()

        idx = get(self.port, "/decisions/graph")
        self.assertEqual(idx.status, 200)
        self.assertIn("Leaf pointing into cycle.", idx.body.decode())

        for nid in (n1.id, n2.id, n3.id):
            with self.subTest(node_id=nid):
                resp = get(self.port, f"/decisions/graph/{nid}")
                self.assertEqual(resp.status, 200)
        self.assertTrue(re.search(rf'href="/decisions/graph/{n2.id}"', get(self.port, f"/decisions/graph/{n1.id}").body.decode()))


if __name__ == "__main__":
    unittest.main()
