"""Unit tests for scripts/decision_actions.py: pure decision lifecycle transitions (M6 item 2, #643)."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import decision_actions as da  # noqa: E402
import policy  # noqa: E402
import reconcile  # noqa: E402
import settings  # noqa: E402

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
        outcome = SimpleNamespace(unanimous=True, recommendation="Split")
        tr = da.from_trio(make_decision(), outcome, answered_at=ANSWERED_AT)
        self.assertEqual(tr.decision.status, "answered")
        self.assertEqual(tr.decision.decided_by, "trio")
        self.assertEqual(tr.decision.answer, "Split")
        self.assertEqual(tr.decision.answered_at, ANSWERED_AT)
        self.assertEqual(tr.store_write.decided_by, "trio")
        self.assertEqual(tr.store_write.status, "answered")

    def test_from_trio_escalated_leaves_decision_open_for_user(self) -> None:
        outcome = SimpleNamespace(unanimous=False, recommendation=None)
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
        self.assertEqual(tr.redispatch, da.Redispatch(KEY, "implement", "builder", 2, "decision reversed", 7))

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
        self.assertEqual(tr.redispatch, da.Redispatch(KEY, "plan", "architect", 1, "decision re-routed to plan", 7))

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

    def test_blank_and_duplicate_answers_are_rejected(self):
        for record, text in ((make_decision(), "  "), (make_decision(status="answered"), "Again")):
            with self.assertRaises(ValueError):
                da.answer(record, text)

    def test_closed_reroutes_and_reversals_do_not_change_when_waiting(self):
        for status in ("reversed", "re-routed"):
            rec = make_decision(status=status, answer="Change course")
            self.assertIsNone(da.meet_action(rec, reconcile.Wait(KEY, "no slots")).store_write)
            with self.assertRaises(ValueError):
                da.reverse(rec, "Again")
            with self.assertRaises(ValueError):
                da.reroute(rec, "plan")

    def test_a_refused_reroute_keeps_its_target_and_returns_to_rerouted_when_the_write_lands(self):
        rerouted = make_decision(status="re-routed", answer="Back", rerouted_to="implement")
        refused = da.refuse(rerouted, "write refused")
        self.assertEqual((refused.decision.status, refused.decision.rerouted_to), ("blocked", "implement"))
        self.assertEqual(refused.store_write.rerouted_to, "implement")
        restored = da.meet_action(refused.decision, None)
        self.assertEqual((restored.decision.status, restored.decision.blocked_reason), ("re-routed", ""))
        self.assertEqual(restored.store_write.rerouted_to, "implement")

    def test_an_answered_decision_whose_action_is_blocked_can_still_be_reversed(self):
        blocked = make_decision(status="blocked", answer="Ship", blocked_reason="merge conflicts")
        self.assertEqual(da.reverse(blocked, "No").decision.status, "reversed")
        rerouted = make_decision(status="blocked", answer="Back", blocked_reason="refused", rerouted_to="plan")
        with self.assertRaises(ValueError):
            da.reverse(rerouted, "No")


SEGMENT = lambda stage, attempt: SimpleNamespace(stage=stage, attempt=attempt)  # noqa: E731


class RespondTest(unittest.TestCase):
    """The use case chooses the stage, persona and attempt; the page driver only executes the result."""

    def setUp(self) -> None:
        self.policies = settings.load_policy()

    def respond(self, action, text="Because", *, record=None, target="", stage="review", history=(), issue_type=None,
                policies=None):
        return da.respond(record or make_decision(), action, text, target_stage=target, current_stage=stage,
                          history=history, policies=policies or self.policies, issue_type=issue_type,
                          answered_at=ANSWERED_AT)

    def test_reroute_picks_the_persona_and_next_attempt_of_the_target_stage(self):
        tr = self.respond("reroute", target="implement", history=[SEGMENT("implement", 1), SEGMENT("implement", 2)])
        self.assertEqual(tr.redispatch, da.Redispatch(KEY, "implement", "implementer", 3, "decision re-routed to implement", 7))
        self.assertIn(da.ForgeStageWrite(KEY, "implement", "review"), tr.actions)

    def test_reroute_refuses_unknown_stages_and_stages_with_no_worker(self):
        for target in ("nowhere", "merge", ""):
            with self.subTest(target=target), self.assertRaises(da.BadRequest):
                self.respond("reroute", target=target)

    def test_reverse_returns_to_the_stage_that_did_the_work_never_to_merge(self):
        history = [SEGMENT("implement", 1), SEGMENT("review", 1), SEGMENT("merge", 1)]
        tr = self.respond("reverse", record=make_decision(status="answered", answer="Yes", gate="merge"),
                          stage="merge", history=history)
        self.assertEqual((tr.redispatch.stage, tr.redispatch.attempt, tr.redispatch.persona), ("implement", 2, "implementer"))

    def test_reverse_with_no_history_starts_the_issue_types_own_workflow(self):
        custom = policy.parse({
            "default_workflow": "standard",
            "personas": {"builder": {"role": "builds", "model": "example"}, "writer": {"role": "writes", "model": "example"}},
            "stages": {"implement": {"persona": "builder"}, "draft": {"persona": "writer"}, "merge": {}},
            "workflows": {"standard": {"start": "implement", "edges": [{"from": "implement", "to": "merge"}]},
                          "docs": {"issue_types": ["docs"], "start": "draft", "edges": [{"from": "draft", "to": "merge"}]}},
        })
        tr = self.respond("reverse", record=make_decision(status="answered", answer="Yes", gate="merge"),
                          stage="merge", issue_type="docs", policies=custom)
        self.assertEqual(tr.redispatch.stage, "draft")

    def test_bad_requests_are_told_apart_from_state_conflicts(self):
        with self.assertRaises(da.BadRequest):
            self.respond("answer", "  ")
        with self.assertRaises(da.BadRequest):
            self.respond("shrug")
        with self.assertRaises(ValueError) as closed:
            self.respond("answer", record=make_decision(status="answered", answer="Yes"))
        self.assertNotIsInstance(closed.exception, da.BadRequest)


class DecisionUpdateTest(unittest.TestCase):
    def reroutes(self):
        return [make_decision(id=n, status="re-routed", answer="Back", rerouted_to="review") for n in (1, 2)]

    def test_block_updates_only_the_decision_with_that_id(self) -> None:
        update = da.block(self.reroutes(), 2, "offline")
        self.assertEqual((update.store_write.decision_id, update.store_write.status), (2, "blocked"))
        self.assertEqual((update.store_write.blocked_reason, update.store_write.rerouted_to), ("offline", "review"))

    def test_block_with_no_matching_id_updates_nothing(self) -> None:
        for decision_id in (None, 0, 3):
            self.assertIsNone(da.block(self.reroutes(), decision_id, "offline"))

    def test_block_leaves_an_open_or_answered_decision_alone(self) -> None:
        for status in ("open", "answered"):
            self.assertIsNone(da.block([make_decision(id=1, status=status)], 1, "offline"))

    def test_block_takes_a_reversed_decision_past_the_retry_limit(self) -> None:
        update = da.block([make_decision(id=1, status="reversed", answer="Redo")], 1, "retry limit reached")
        self.assertEqual((update.store_write.status, update.store_write.blocked_reason), ("blocked", "retry limit reached"))

    def test_unblock_restores_only_the_decision_with_that_id(self) -> None:
        blocked = [make_decision(id=n, status="blocked", answer="Back", blocked_reason="offline", rerouted_to="review")
                   for n in (1, 2)]
        update = da.unblock(blocked, 1)
        self.assertEqual((update.store_write.decision_id, update.store_write.status, update.store_write.blocked_reason),
                         (1, "re-routed", ""))
        self.assertIsNone(da.unblock(blocked, None))
        self.assertIsNone(da.unblock(self.reroutes(), 1))

    def test_a_reroute_and_a_reversal_name_their_decision_in_the_redispatch(self) -> None:
        rerouted = da.reroute(make_decision(), "implement", persona="builder")
        reversed_ = da.reverse(make_decision(status="answered", answer="Yes"), "No", stage="implement", persona="builder")
        self.assertEqual((rerouted.redispatch.decision_id, reversed_.redispatch.decision_id), (7, 7))


if __name__ == "__main__":
    unittest.main()
