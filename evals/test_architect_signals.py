"""Architect signals: unassigned compliance gaps reach an issue-filing worker (#1180).

Design §6 / ARCHITECTURE §16: the architect writes unassigned gap rows; the coordinator keeps its own
ledger and spawns a short-lived issue-filing worker; a repeated signal key files nothing twice; only
the architect writes compliance.md.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "evals"))
sys.path.insert(0, str(ROOT / "scripts"))

from evals.rules_text import rules_text, sections  # noqa: E402

COMPLIANCE = """# Compliance

| ID | Section | Severity | File and line | Gap | Owner | Status |
|---|---|---|---|---|---|---|
| C1 | 5 | P1 | `a.py:1` | closed gap | #1 | closed |
| C4 | 3 | P2 | `b.py:2` | shared page module missing | unassigned | open |
| C5 | 1 | P2 | `c.py:3` | use case imports gateway | unassigned | open |
| C9 | 4 | P2 | `d.py:4` | closed again | #9 | closed |
| C10 | 1 | P3 | `e.py:5` | assigned work | #853 | open |
"""


class GapRowsTest(unittest.TestCase):
    def test_unassigned_open_rows_are_the_intake(self):
        import architect_signals as sig
        rows = sig.unassigned_gaps(COMPLIANCE)
        self.assertEqual([r.id for r in rows], ["C4", "C5"])
        self.assertEqual(rows[0].section, "3")
        self.assertIn("shared page", rows[0].gap)

    def test_a_row_owned_by_an_issue_is_not_intake(self):
        import architect_signals as sig
        self.assertEqual([r.id for r in sig.unassigned_gaps(COMPLIANCE) if r.id == "C10"], [])


class LedgerTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        (self.root / ".chief-of-stuff").mkdir()

    def test_pending_excludes_gap_ids_already_in_the_ledger(self):
        import architect_signals as sig
        sig.record_acted(self.root, "C4", issue_key="github:o/app#12", signal_key="architect-signal:C4@abc")
        pending = sig.pending(self.root, COMPLIANCE)
        self.assertEqual([r.id for r in pending], ["C5"])

    def test_recording_does_not_edit_compliance_md(self):
        import architect_signals as sig
        path = self.root / "architecture" / "compliance.md"
        path.parent.mkdir()
        path.write_text(COMPLIANCE)
        before = path.read_text()
        sig.record_acted(self.root, "C4", issue_key="github:o/app#12", signal_key="architect-signal:C4@abc")
        self.assertEqual(path.read_text(), before)
        self.assertTrue((self.root / ".chief-of-stuff" / "architect-signals.json").is_file())

    def test_a_repeated_record_for_the_same_gap_is_idempotent(self):
        import architect_signals as sig
        sig.record_acted(self.root, "C4", issue_key="github:o/app#12", signal_key="architect-signal:C4@abc")
        sig.record_acted(self.root, "C4", issue_key="github:o/app#99", signal_key="architect-signal:C4@abc")
        ledger = json.loads((self.root / ".chief-of-stuff" / "architect-signals.json").read_text())
        self.assertEqual(list(ledger["acted"].keys()), ["C4"])
        self.assertEqual(ledger["acted"]["C4"]["issue_key"], "github:o/app#12")


class SignalKeyTest(unittest.TestCase):
    def test_key_names_the_gap_and_the_commit(self):
        import architect_signals as sig
        self.assertEqual(sig.signal_key("C4", "abc1234"), "architect-signal:C4@abc1234")

    def test_body_embeds_the_key_on_its_own_line(self):
        import architect_signals as sig
        body = sig.issue_body(
            gap=sig.GapRow("C4", "3", "P2", "`b.py:2`", "shared page module missing", "unassigned", "open"),
            against="abc1234",
            source="architecture/compliance.md",
        )
        self.assertIn("architect-signal:C4@abc1234", body.splitlines())
        self.assertIn("shared page module missing", body)

    def test_existing_issue_with_the_same_key_means_file_nothing(self):
        import architect_signals as sig
        issues = [
            {"number": 10, "body": "other\narchitect-signal:C4@abc1234\n", "title": "old"},
            {"number": 11, "body": "unrelated", "title": "other"},
        ]
        found = sig.find_existing(issues, "architect-signal:C4@abc1234")
        self.assertEqual(found, 10)
        self.assertIsNone(sig.find_existing(issues, "architect-signal:C5@abc1234"))


class AgentRulesTest(unittest.TestCase):
    """Agent-file acceptance for #1180. Behaviour starts red; the agent text turns it green."""

    @classmethod
    def setUpClass(cls):
        cls.agent = (ROOT / "agents" / "chief-of-stuff.md").read_text()
        cls.rules = rules_text()
        cls.filing = ROOT / "agents" / "issue-filing.md"
        cls.architect = ROOT / "agents" / "frank-lloyd-aight.md"

    def test_issue_filing_worker_agent_exists(self):
        self.assertTrue(self.filing.is_file(), "agents/issue-filing.md is the short-lived filing worker")

    def test_architect_agent_exists_beside_the_plugin(self):
        # The project's architect rules live in agents/ and bind the frank-lloyd-aight plugin role.
        self.assertTrue(self.architect.is_file(), "agents/frank-lloyd-aight.md carries the project architect rules")

    def test_coordinator_spawns_issue_filing_for_unassigned_gaps(self):
        text = self.rules
        self.assertIn("issue-filing", text)
        self.assertIn("unassigned", text)
        self.assertIn("architect-signals", text)
        self.assertIn("compliance.md", text)

    def test_coordinator_keeps_its_own_ledger_and_never_edits_compliance(self):
        text = self.rules
        self.assertIn("architect-signals.json", text)
        self.assertRegex(text, r"(?i)never edits? `?architecture/compliance\.md`?")

    def test_architect_never_files_forge_issues(self):
        text = self.architect.read_text()
        self.assertRegex(text, r"(?i)never files? (forge )?issues?")
        self.assertNotRegex(text, r"(?i)gh issue create")

    def test_architect_grades_after_merge_and_on_mismatch(self):
        text = self.architect.read_text()
        self.assertRegex(text, r"(?i)after each merge to main")
        self.assertRegex(text, r"(?i)mismatch")

    def test_issue_filing_worker_dedupes_by_signal_key_and_ends(self):
        text = self.filing.read_text()
        self.assertIn("architect-signal:", text)
        self.assertRegex(text, r"(?i)files? nothing|do not file|already exists")
        self.assertRegex(text, r"(?i)\bends?\b|\bexit\b|\bstop\b")


class CirclesTest(unittest.TestCase):
    def test_architect_signals_sits_in_use_cases(self):
        from evals.test_architecture import CIRCLES
        self.assertIn("architect_signals", CIRCLES["use_cases"])


if __name__ == "__main__":
    unittest.main()
