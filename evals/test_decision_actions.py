"""Unit tests for scripts/decision_actions.py: pure decision lifecycle transitions (M6 item 2, #643)."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import decision_actions as da  # noqa: E402
import reconcile  # noqa: E402

KEY = "github:example/repo#12"
ASKED_AT = "2026-10-09T10:00:00+00:00"
ANSWERED_AT = "2026-10-09T10:15:00+00:00"


def make_decision(**fields: object) -> SimpleNamespace:
    base = dict(
        id=7,
        issue_key=KEY,
        gate="design",
        prompt="Split the parser into two passes?",
        status="open",
        answer="",
        decided_by="",
        asked_at=ASKED_AT,
        answered_at=None,
    )
    base.update(fields)
    return SimpleNamespace(**base)


class DecisionActionsTest(unittest.TestCase):
    def test_answer_transitions_open_to_answered_and_emits_store_and_forge_writes(self) -> None:
        tr = da.answer(make_decision(), "Yes, split it", decided_by="user", answered_at=ANSWERED_AT)
        self.assertEqual(tr.decision.status, "answered")
        self.assertEqual(tr.decision.answer, "Yes, split it")
        self.assertEqual(tr.decision.decided_by, "user")
        self.assertEqual(tr.decision.answered_at, ANSWERED_AT)
        self.assertEqual(tr.decision.blocked_reason, "")
        self.assertIsNotNone(tr.store_write)
        self.assertEqual(tr.store_write.status, "answered")
        self.assertEqual(tr.store_write.answer, "Yes, split it")
        self.assertEqual(tr.store_write.decided_by, "user")
        self.assertTrue(any(isinstance(a, da.ForgeComment) and a.issue_key == KEY for a in tr.actions))
        self.assertIsNone(tr.redispatch)
        self.assertTrue(da.is_closed(tr.decision))

    def test_from_trio_decided_enters_lifecycle_as_answered_by_trio(self) -> None:
        outcome = SimpleNamespace(status="decided", rationale="Two of three agreed to split.", escalated_reason=None)
        tr = da.from_trio(make_decision(), outcome, answered_at=ANSWERED_AT)
        self.assertEqual(tr.decision.status, "answered")
        self.assertEqual(tr.decision.decided_by, "trio")
        self.assertEqual(tr.decision.answer, "Two of three agreed to split.")
        self.assertEqual(tr.decision.answered_at, ANSWERED_AT)
        self.assertEqual(tr.store_write.decided_by, "trio")
        self.assertEqual(tr.store_write.status, "answered")

    def test_from_trio_escalated_leaves_decision_open_for_user(self) -> None:
        outcome = SimpleNamespace(status="escalated", rationale=None, escalated_reason="gate timed out")
        tr = da.from_trio(make_decision(), outcome, answered_at=ANSWERED_AT)
        self.assertEqual(tr.decision.status, "open")
        self.assertEqual(tr.decision.decided_by, "")
        self.assertFalse(da.is_closed(tr.decision))

    def test_reverse_transitions_answered_to_reversed_and_requests_redispatch(self) -> None:
        answered = make_decision(status="answered", answer="Yes, split it", decided_by="trio", answered_at=ANSWERED_AT)
        tr = da.reverse(
            answered,
            "No, keep one pass",
            decided_by="user",
            answered_at="2026-10-09T10:30:00+00:00",
            stage="implement",
            persona="builder",
            attempt=2,
        )
        self.assertEqual(tr.decision.status, "reversed")
        self.assertEqual(tr.decision.answer, "No, keep one pass")
        self.assertEqual(tr.decision.decided_by, "user")
        self.assertEqual(tr.store_write.status, "reversed")
        self.assertEqual(tr.store_write.answer, "No, keep one pass")
        self.assertTrue(any(isinstance(a, da.ForgeComment) and "reversed" in a.body.casefold() for a in tr.actions))
        self.assertEqual(tr.redispatch, da.Redispatch(KEY, "implement", "builder", 2, "decision reversed"))

    def test_reroute_transitions_to_rerouted_writes_stage_and_requests_redispatch(self) -> None:
        answered = make_decision(status="answered", answer="Yes", decided_by="user", answered_at=ANSWERED_AT)
        tr = da.reroute(
            answered,
            "plan",
            answer_text="Re-plan the module boundary first",
            decided_by="user",
            answered_at="2026-10-09T10:45:00+00:00",
            current_stage="review",
            persona="architect",
            attempt=1,
        )
        self.assertEqual(tr.decision.status, "re-routed")
        self.assertEqual(tr.decision.rerouted_to, "plan")
        self.assertEqual(tr.store_write.status, "re-routed")
        self.assertEqual(tr.store_write.rerouted_to, "plan")
        self.assertIn(da.ForgeStageWrite(KEY, "plan", "review"), tr.actions)
        self.assertTrue(any(isinstance(a, da.ForgeComment) and "plan" in a.body for a in tr.actions))
        self.assertEqual(tr.redispatch, da.Redispatch(KEY, "plan", "architect", 1, "decision re-routed to plan"))

    def test_answered_decision_meeting_blocked_action_becomes_blocked_and_never_closed(self) -> None:
        # #288: an answered decision whose issue is blocked carries the reason the action cannot run,
        # shows on the board as blocked, and is never treated as closed.
        answered = make_decision(status="answered", answer="Ship now", decided_by="user", answered_at=ANSWERED_AT)
        for action, reason in (
            (reconcile.Wait(KEY, "merge conflicts"), "merge conflicts"),
            (reconcile.Escalate(KEY, "retry limit"), "retry limit"),
        ):
            with self.subTest(action=action):
                tr = da.meet_action(answered, action)
                self.assertEqual(tr.decision.status, "blocked")
                self.assertEqual(tr.decision.blocked_reason, reason)
                self.assertEqual(tr.decision.answer, "Ship now")
                self.assertEqual(tr.decision.decided_by, "user")
                self.assertEqual(da.board_status(tr.decision), "blocked")
                self.assertFalse(da.is_closed(tr.decision))
                self.assertEqual(tr.store_write.status, "blocked")
                self.assertEqual(tr.store_write.blocked_reason, reason)

    def test_blocked_decision_clears_to_answered_when_action_can_run(self) -> None:
        blocked = make_decision(status="blocked", answer="Ship now", decided_by="user", answered_at=ANSWERED_AT)
        tr = da.meet_action(blocked, reconcile.Dispatch(KEY, "implement", "builder", 1), blocked_reason="merge conflicts")
        self.assertEqual(tr.decision.status, "answered")
        self.assertEqual(tr.decision.blocked_reason, "")
        self.assertEqual(da.board_status(tr.decision), "answered")
        self.assertTrue(da.is_closed(tr.decision))
        self.assertEqual(tr.store_write.status, "answered")
        self.assertEqual(tr.store_write.blocked_reason, "")


if __name__ == "__main__":
    unittest.main()
