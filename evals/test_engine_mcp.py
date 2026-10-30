"""Environment-bound report/ask tools, including an SDK stdio client round trip."""
from __future__ import annotations

import asyncio
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import store

KEY = "github:example/repo#12"
FINDING = {"severity": "important", "title": "A defect", "evidence": "A reproduction",
           "path": "src/widget.py", "line": 4, "suggestion": "Fix it"}


class MCPTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        store.migrate(self.root / "store.db")
        self.db = store.Store(self.root / "store.db")
        self.addCleanup(self.db.engine.dispose)
        self.db.upsert_item(KEY, title="A widget", stage="review")
        from datetime import datetime, timezone
        import engine_mcp
        import guard_binding
        self.env = {
            "CHIEF_ISSUE_KEY": KEY, "CHIEF_STAGE": "review", "CHIEF_ATTEMPT": "2",
            "CHIEF_SESSION_ID": "session-two", "CHIEF_ROOT": str(self.root),
            "CHIEF_LAUNCHED_AT": datetime.now(timezone.utc).isoformat(),
            "CHIEF_PYTHON": sys.executable,
        }
        self.tools = engine_mcp.EngineTools(guard_binding.Binding.from_env(self.env), self.db)

    def test_tools_store_environment_identity_and_return_ids(self):
        result = self.tools.report("done", "Reviewed", [FINDING])
        report = self.db.reports(KEY)[0]
        self.assertEqual(result["report_id"], report.id)
        self.assertEqual((report.issue_key, report.stage, report.attempt, report.session_id),
                         (KEY, "review", 2, "session-two"))
        self.assertEqual(report.findings, [FINDING])
        result = self.tools.ask("Choose a widget", ["small", "large"], "Product choice")
        ask = self.db.asks(KEY)[0]
        self.assertEqual(result["ask_id"], ask.id)
        self.assertIn("stop", result["message"].lower())
        self.assertEqual((ask.issue_key, ask.stage, ask.attempt, ask.session_id, ask.answer),
                         (KEY, "review", 2, "session-two", None))

    def test_forged_identity_and_invalid_findings_cannot_write(self):
        with self.assertRaises(TypeError):
            self.tools.report("done", "forged", issue_key="github:example/repo#99")
        with self.assertRaises(TypeError):
            self.tools.ask("forged", [], "", attempt=3)
        for finding in ({**FINDING, "severity": "high"}, {**FINDING, "line": -1},
                        {**FINDING, "session_id": "forged"}):
            with self.subTest(finding=finding), self.assertRaises(ValueError):
                self.tools.report("done", "bad", [finding])
        with self.assertRaises(ValueError):
            self.tools.report("success", "bad")
        self.assertEqual(self.db.reports(KEY), [])
        self.assertEqual(self.db.asks(KEY), [])

    def test_older_attempt_is_append_only_and_cannot_replace_newer(self):
        self.tools.report("done", "new result")
        import engine_mcp
        from guard_binding import Binding
        older = engine_mcp.EngineTools(Binding(KEY, "review", 1, "session-one", self.root,
                                             self.tools.binding.launched_at), self.db)
        older.report("failed", "late old result")
        self.assertEqual([(r.attempt, r.summary) for r in self.db.reports(KEY)],
                         [(1, "late old result"), (2, "new result")])

    def test_store_refuses_missing_issue_and_preserves_ask_answers(self):
        from sqlalchemy.exc import IntegrityError
        with self.assertRaises(IntegrityError):
            self.db.add_report("github:example/repo#99", "review", 1, "s", "done", "x", [])
        with self.assertRaises(IntegrityError):
            self.db.add_ask("github:example/repo#99", "review", 1, "s", "q", [], "c")
        ask_id = self.tools.ask("A question", [], "context")["ask_id"]
        with self.db.session() as session:
            session.get(store.Ask, ask_id).answer = "An answer"
        self.assertEqual(self.db.asks(KEY)[0].answer, "An answer")

    def test_finding_schema_advertises_policy_severities(self):
        from engine_mcp import Finding
        from policy import SEVERITIES
        self.assertEqual(Finding.model_json_schema()["properties"]["severity"]["enum"], list(SEVERITIES))

    def test_stdio_sdk_round_trip(self):
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        async def run():
            params = StdioServerParameters(command=sys.executable, args=[str(ROOT / "scripts/engine_mcp.py")],
                                           env=self.env, cwd=self.tmp.name)
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write, read_timeout_seconds=10) as client:
                    await client.initialize()
                    tools = await client.list_tools()
                    self.assertEqual({t.name for t in tools.tools}, {"report", "ask", "decisions"})
                    for tool in tools.tools:
                        self.assertNotIn("issue_key", tool.input_schema["properties"])
                    report = await client.call_tool("report", {"outcome": "done", "summary": "over stdio",
                                                               "findings": [FINDING], "issue_key": "forged"})
                    # SDK may ignore extra arguments or refuse them; it must never bind to them.
                    if report.is_error:
                        report = await client.call_tool("report", {"outcome": "done", "summary": "over stdio",
                                                                   "findings": [FINDING]})
                    self.assertFalse(report.is_error)
                    bad = await client.call_tool("report", {"outcome": "done", "summary": "invalid",
                                                            "findings": [{**FINDING, "severity": "high"}]})
                    self.assertTrue(bad.is_error)
                    ask = await client.call_tool("ask", {"question": "stdio question", "options": ["yes"],
                                                         "context": "stdio context"})
                    self.assertFalse(ask.is_error)
        asyncio.run(run())
        self.assertEqual([(r.issue_key, r.attempt, r.summary) for r in self.db.reports(KEY)],
                         [(KEY, 2, "over stdio")])
        self.assertEqual(self.db.asks(KEY)[0].question, "stdio question")


class NoScopeProposalTest(unittest.TestCase):
    """#1039 (C29): report takes no file ownership, so no plan run proposes a scope."""

    def tools(self, stage):
        from datetime import datetime, timezone
        import engine_mcp
        import guard_binding
        env = {"CHIEF_ISSUE_KEY": KEY, "CHIEF_STAGE": stage, "CHIEF_ATTEMPT": "1", "CHIEF_SESSION_ID": "s",
               "CHIEF_ROOT": str(self.root), "CHIEF_LAUNCHED_AT": datetime.now(timezone.utc).isoformat(),
               "CHIEF_PYTHON": sys.executable}
        return engine_mcp.EngineTools(guard_binding.Binding.from_env(env), self.db)

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        store.migrate(self.root / "store.db")
        self.db = store.Store(self.root / "store.db")
        self.addCleanup(self.db.engine.dispose)
        self.db.upsert_item(KEY, title="A widget", stage="plan")

    def test_a_plan_report_takes_no_file_ownership_and_keeps_no_proposal(self):
        with self.assertRaises(TypeError):
            self.tools("plan").report("done", "Planned", file_ownership=[{"path": "src/widget.py", "mode": "write"}])
        self.tools("plan").report("done", "Planned")
        self.assertEqual([r.summary for r in self.db.reports(KEY)], ["Planned"])
        self.assertNotIn("scope_proposal", self.db.resume(KEY) or {})


class ChannelMigrationTest(unittest.TestCase):
    def test_upgrade_preserves_existing_store_and_adds_worker_channel(self):
        from alembic import command
        from alembic.config import Config
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "store.db"
            cfg = Config()
            cfg.set_main_option("script_location", str(ROOT / "scripts/migrations"))
            cfg.set_main_option("sqlalchemy.url", f"sqlite:///{path}")
            command.upgrade(cfg, "0002")
            db = store.Store(path)
            try:
                db.upsert_item(KEY, title="Before upgrade", stage="review")
                # Seed the historical schema without asking today's ORM to write newer columns.
                with db.engine.begin() as conn:
                    conn.exec_driver_sql("INSERT INTO segment (issue_key, stage, attempt, started_at, runtime) "
                                         "VALUES (?, 'review', 1, 'before', 'claude')", (KEY,))
                store.migrate(path)
                self.assertEqual(db.items()[0].title, "Before upgrade")
                self.assertEqual(db.segments(KEY)[0].runtime, "claude")
                db.add_report(KEY, "review", 1, "s", "done", "After upgrade", [])
                db.add_ask(KEY, "review", 1, "s", "After upgrade?", [], "")
                self.assertEqual(len(db.reports(KEY)), 1)
                self.assertEqual(len(db.asks(KEY)), 1)
            finally:
                db.engine.dispose()


if __name__ == "__main__":
    unittest.main()
