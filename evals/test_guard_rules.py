"""Pure guard Use Case rules: no store, no hook I/O."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import sys
import unittest
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import guard_rules  # noqa: E402


class GuardRulesTest(unittest.TestCase):
    def test_stop_allowed_requires_a_report_at_or_after_launch(self):
        launched = datetime.fromisoformat("2026-10-11T12:00:00+00:00")
        before = SimpleNamespace(created_at="2026-10-11T11:59:59+00:00")
        at = SimpleNamespace(created_at="2026-10-11T12:00:00+00:00")
        after = SimpleNamespace(created_at="2026-10-11T12:00:01+00:00")
        self.assertFalse(guard_rules.stop_allowed([], launched))
        self.assertFalse(guard_rules.stop_allowed([before], launched))
        self.assertTrue(guard_rules.stop_allowed([before, at], launched))
        self.assertTrue(guard_rules.stop_allowed([after], launched))

    def test_permission_ask_records_tool_name_and_input(self):
        question, context = guard_rules.permission_ask(
            {"tool_name": "Bash", "tool_input": {"command": "touch /x"}})
        self.assertEqual(question, "Permission requested for Bash")
        self.assertIn("touch /x", context)
        self.assertIn("Bash", context)

    def test_resume_state_assembles_binding_item_asks_and_reports(self):
        binding = SimpleNamespace(issue_key="github:o/r#1", stage="implement", attempt=2,
                                  session_id="s2")
        item = SimpleNamespace(title="Widget", stage="review")
        asks = [SimpleNamespace(id=1, stage="implement", attempt=1, session_id="s1",
                                question="Colour?", options=["red"], context="c", answer="red")]
        reports = [SimpleNamespace(id=2, stage="implement", attempt=1, session_id="s1",
                                   outcome="done", summary="ok", findings=[])]
        state = guard_rules.resume_state(binding, item, asks, reports)
        self.assertEqual(state["issue_key"], "github:o/r#1")
        self.assertEqual(state["title"], "Widget")
        self.assertEqual(state["forge_stage"], "review")
        self.assertEqual(state["asks"][0]["answer"], "red")
        self.assertEqual(state["reports"][0]["summary"], "ok")


if __name__ == "__main__":
    unittest.main()
