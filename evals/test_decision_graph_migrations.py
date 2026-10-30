"""Migration correctness for rotation records (0011) and the decision graph (0012)."""

from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import store
from alembic import command


class DecisionGraphMigrationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "store.db"

    def _tables(self) -> set[str]:
        with sqlite3.connect(self.path) as conn:
            return {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}

    def _columns(self, table: str) -> set[str]:
        with sqlite3.connect(self.path) as conn:
            return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}

    def test_0011_and_0012_upgrade_and_downgrade(self) -> None:
        cfg = store._config(self.path)
        command.upgrade(cfg, "0012")
        self.assertIn("rotation_cursor", self._tables())
        self.assertIn("workflow", self._columns("segment"))
        for name in (
            "decision_graph_node",
            "decision_graph_edge",
            "decision_graph_rejected",
            "decision_history_request",
            "decision_history_grant",
        ):
            self.assertIn(name, self._tables())

        command.downgrade(cfg, "0011")
        tables = self._tables()
        self.assertIn("rotation_cursor", tables)
        self.assertIn("workflow", self._columns("segment"))
        for name in (
            "decision_graph_node",
            "decision_graph_edge",
            "decision_graph_rejected",
            "decision_history_request",
            "decision_history_grant",
        ):
            self.assertNotIn(name, tables)

        command.downgrade(cfg, "0010")
        tables = self._tables()
        self.assertNotIn("rotation_cursor", tables)
        self.assertNotIn("workflow", self._columns("segment"))
        self.assertIn("worker_message", tables)

        command.upgrade(cfg, "0012")
        self.assertIn("decision_graph_node", self._tables())
        self.assertIn("workflow", self._columns("segment"))


if __name__ == "__main__":
    unittest.main()
