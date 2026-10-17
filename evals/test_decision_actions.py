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
        self.assertEqual((tr.decision.intent, tr.source), ("", None))
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

    def test_reverse_appends_its_own_row_as_a_run_intent_and_closes_the_decision_it_reversed(self) -> None:
        answered = make_decision(status="answered", answer="Yes, split it", decided_by="trio", answered_at=ANSWERED_AT)
        tr = da.reverse(answered, "No, keep one pass", decided_by="user", answered_at="2026-10-09T10:30:00+00:00",
                        stage="implement", row_id=9)
        self.assertEqual((tr.decision.id, tr.decision.status, tr.decision.answer), (9, "reversed", "No, keep one pass"))
        self.assertEqual((tr.decision.rerouted_to, tr.decision.intent), ("implement", "run"))
        self.assertEqual((tr.store_write.decision_id, tr.store_write.status, tr.store_write.intent), (9, "reversed", "run"))
        self.assertEqual((tr.source.decision_id, tr.source.status, tr.source.superseded_by), (7, "superseded", 9))
        self.assertTrue(any(isinstance(a, da.ForgeComment) and "reversed" in a.body.casefold() for a in tr.actions))

    def test_reroute_appends_its_own_row_as_a_write_intent_and_closes_the_decision_it_left(self) -> None:
        answered = make_decision(status="answered", answer="Yes", decided_by="user", answered_at=ANSWERED_AT)
        tr = da.reroute(answered, "plan", answer_text="Re-plan the module boundary first", decided_by="user",
                        answered_at="2026-10-09T10:45:00+00:00", row_id=9)
        self.assertEqual((tr.decision.id, tr.decision.status, tr.decision.rerouted_to, tr.decision.intent),
                         (9, "re-routed", "plan", "write"))
        self.assertEqual((tr.store_write.decision_id, tr.store_write.rerouted_to, tr.store_write.intent), (9, "plan", "write"))
        self.assertEqual((tr.source.decision_id, tr.source.status, tr.source.superseded_by), (7, "superseded", 9))
        self.assertTrue(any(isinstance(a, da.ForgeComment) and "plan" in a.body for a in tr.actions))
        self.assertEqual([a for a in tr.actions if not isinstance(a, da.ForgeComment)], [])

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
                da.reverse(rec, "Again", row_id=9)
            with self.assertRaises(ValueError):
                da.reroute(rec, "plan", row_id=9)

    def test_a_blocked_intent_keeps_its_target_and_phase(self):
        rerouted = make_decision(status="re-routed", answer="Back", rerouted_to="implement", intent="run")
        refused = da.refuse(rerouted, "retry limit reached")
        self.assertEqual((refused.decision.status, refused.decision.rerouted_to, refused.decision.intent),
                         ("blocked", "implement", "run"))
        self.assertEqual((refused.store_write.rerouted_to, refused.store_write.intent), ("implement", "run"))

    def test_a_decision_with_an_intent_can_be_neither_answered_nor_restored_by_a_wait(self):
        for status in ("re-routed", "reversed", "blocked"):
            held = make_decision(status=status, answer="Back", rerouted_to="plan", intent="run")
            self.assertIsNone(da.meet_action(held, reconcile.Dispatch(KEY, "implement", "builder", 1)).store_write)
            with self.assertRaises(ValueError):
                da.answer(held, "Again")

    def test_an_unapplied_or_blocked_intent_can_be_reversed_or_re_routed_and_is_closed_without_its_intent(self):
        for status in ("re-routed", "reversed", "blocked"):
            for phase in ("write", "run"):
                held = make_decision(status=status, answer="Back", rerouted_to="plan", intent=phase)
                for tr in (da.reverse(held, "No", row_id=9), da.reroute(held, "review", row_id=9)):
                    self.assertEqual((tr.source.status, tr.source.intent, tr.source.superseded_by), ("superseded", "", 9))

    def test_an_answered_decision_whose_action_is_blocked_can_still_be_reversed(self):
        blocked = make_decision(status="blocked", answer="Ship", blocked_reason="merge conflicts")
        self.assertEqual(da.reverse(blocked, "No", row_id=9).decision.status, "reversed")


SEGMENT = lambda stage, attempt: SimpleNamespace(stage=stage, attempt=attempt)  # noqa: E731


class RespondTest(unittest.TestCase):
    """The use case chooses the stage, persona and attempt; the page driver only executes the result."""

    def setUp(self) -> None:
        self.policies = settings.load_policy()

    def respond(self, action, text="Because", *, record=None, target="", stage="review", history=(), issue_type=None,
                policies=None):
        return da.respond(record or make_decision(), action, text, target_stage=target, current_stage=stage,
                          history=history, policies=policies or self.policies, issue_type=issue_type,
                          answered_at=ANSWERED_AT, row_id=9)

    def test_reroute_runs_the_target_stage_from_its_own_row(self):
        tr = self.respond("reroute", target="implement")
        self.assertEqual((tr.decision.id, tr.decision.rerouted_to, tr.decision.intent), (9, "implement", "write"))

    def test_reroute_refuses_unknown_stages_and_stages_with_no_worker(self):
        for target in ("nowhere", "merge", ""):
            with self.subTest(target=target), self.assertRaises(da.BadRequest):
                self.respond("reroute", target=target)

    def test_reverse_returns_to_the_stage_that_did_the_work_never_to_merge(self):
        history = [SEGMENT("implement", 1), SEGMENT("review", 1), SEGMENT("merge", 1)]
        tr = self.respond("reverse", record=make_decision(status="answered", answer="Yes", gate="merge"),
                          stage="merge", history=history)
        self.assertEqual((tr.decision.rerouted_to, tr.decision.intent), ("implement", "run"))

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
        self.assertEqual(tr.decision.rerouted_to, "draft")

    def test_bad_requests_are_told_apart_from_state_conflicts(self):
        with self.assertRaises(da.BadRequest):
            self.respond("answer", "  ")
        with self.assertRaises(da.BadRequest):
            self.respond("shrug")
        with self.assertRaises(ValueError) as closed:
            self.respond("answer", record=make_decision(status="answered", answer="Yes"))
        self.assertNotIsInstance(closed.exception, da.BadRequest)


def intent(id, phase="write", status="re-routed", stage="review", **fields):
    return make_decision(id=id, question=f"q{id}", status=status, answer="Back", rerouted_to=stage, intent=phase, **fields)


class DecisionUpdateTest(unittest.TestCase):
    def test_block_updates_only_the_decision_with_that_id_and_keeps_its_intent(self) -> None:
        update = da.block([intent(1), intent(2, "run")], 2, "retry limit reached")
        self.assertEqual((update.store_write.decision_id, update.store_write.status), (2, "blocked"))
        self.assertEqual((update.store_write.blocked_reason, update.store_write.rerouted_to, update.store_write.intent),
                         ("retry limit reached", "review", "run"))

    def test_block_with_no_matching_id_updates_nothing(self) -> None:
        for decision_id in (None, 0, 3):
            self.assertIsNone(da.block([intent(1), intent(2)], decision_id, "offline"))

    def test_block_leaves_an_open_or_answered_decision_alone(self) -> None:
        for status in ("open", "answered"):
            self.assertIsNone(da.block([make_decision(id=1, status=status)], 1, "offline"))

    def test_restore_held_restores_only_blocked_answers_when_no_decision_is_open(self) -> None:
        blocked = make_decision(id=1, question="a", status="blocked", answer="Yes", blocked_reason="merge is not ready")
        held = intent(2, "run", "blocked", blocked_reason="retry limit reached")
        self.assertEqual([u.store_write.decision_id for u in da.restore_held([blocked, held])], [1])
        self.assertEqual(da.restore_held([blocked, make_decision(id=3, question="c")]), [])

    def test_unblock_restores_only_the_decision_with_that_id(self) -> None:
        blocked = [make_decision(id=n, question=f"q{n}", status="blocked", answer="Yes", blocked_reason="wait") for n in (1, 2)]
        update = da.unblock(blocked, 1)
        self.assertEqual((update.store_write.decision_id, update.store_write.status, update.store_write.blocked_reason),
                         (1, "answered", ""))
        self.assertIsNone(da.unblock(blocked, None))
        self.assertIsNone(da.unblock([intent(1)], 1))


class CurrentIntentTest(unittest.TestCase):
    """One current intent per issue is chosen here, in the Use Cases; no driver picks or supersedes."""

    def test_the_current_intent_is_the_newest_row_with_an_intent(self):
        rows = [intent(1), make_decision(id=2, question="x", status="answered", answer="Yes"), intent(3, "run"),
                intent(4, "", "re-routed")]
        self.assertEqual(da.current_intent(rows).id, 3)
        self.assertIsNone(da.current_intent([rows[1], rows[3]]))
        self.assertIsNone(da.current_intent([]))

    def test_a_blocked_intent_stays_current_until_a_newer_one_replaces_it(self):
        rows = [intent(1, "run", "blocked", blocked_reason="retry limit reached")]
        self.assertEqual(da.current_intent(rows).id, 1)
        self.assertEqual(da.current_intent([*rows, intent(2, "run", "reversed", stage="implement")]).id, 2)

    def test_supersede_closes_every_older_pending_or_blocked_intent_by_the_current_one(self):
        rows = [intent(1, "write"), intent(2, "run", "blocked", blocked_reason="retry limit reached"),
                make_decision(id=3, question="x", status="answered", answer="Yes"), intent(4, "run", "reversed")]
        updates = da.supersede(rows)
        self.assertEqual([(u.store_write.decision_id, u.store_write.status, u.store_write.superseded_by,
                           u.store_write.intent, u.store_write.blocked_reason) for u in updates],
                         [(1, "superseded", 4, "", ""), (2, "superseded", 4, "", "")])

    def test_supersede_with_one_intent_or_none_changes_nothing(self):
        self.assertEqual(da.supersede([intent(1)]), [])
        self.assertEqual(da.supersede([make_decision()]), [])

    def test_advance_moves_the_intent_from_its_write_to_its_run_and_then_ends_it(self):
        write = da.advance([intent(1, "write")], 1)
        self.assertEqual((write.store_write.intent, write.store_write.status), ("run", "re-routed"))
        done = da.advance([write.decision], 1)
        self.assertEqual((done.store_write.intent, done.store_write.status, done.store_write.rerouted_to), ("", "re-routed", "review"))
        self.assertIsNone(da.advance([done.decision], 1))
        self.assertIsNone(da.advance([intent(1, "run", "blocked", blocked_reason="retry limit reached")], 1))
        self.assertIsNone(da.advance([intent(1)], 2))

    def test_advance_and_block_apply_only_to_the_current_intent(self):
        older, newer = intent(1, "write"), intent(2, "run")
        self.assertIsNone(da.advance([older, newer], 1))
        self.assertIsNone(da.block([older, newer], 1, "retry limit reached"))
        self.assertEqual(da.advance([older, newer], 2).store_write.intent, "")

    def test_an_intent_replaces_the_engines_pending_move_only_when_it_writes_a_stage(self):
        stage = "implement"
        self.assertTrue(da.replaces_engine_move(intent(1, "write", stage="implement"), stage))
        self.assertTrue(da.replaces_engine_move(intent(1, "run", stage="review"), stage))
        self.assertFalse(da.replaces_engine_move(intent(1, "run", stage="implement"), stage))
        self.assertFalse(da.replaces_engine_move(intent(1, "write", "blocked", stage="review"), stage))


if __name__ == "__main__":
    unittest.main()
