"""The board's decision lifecycle on append-only rows (M6 item 2, #643; architect ruling on #758, §6)."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import decision_actions as da  # noqa: E402
import decision_escalation  # noqa: E402
import policy  # noqa: E402
import reconcile  # noqa: E402
import settings  # noqa: E402

KEY = "github:example/repo#12"
ASKED_AT = "2026-10-09T10:00:00+00:00"
ANSWERED_AT = "2026-10-09T10:15:00+00:00"


def make_decision(id=7, topic="design", question="Split the parser into two passes?", answer=None, decided_by="",
                  **record: object) -> SimpleNamespace:
    """A stored row: the lifecycle lives in its record, as the store keeps it."""
    return SimpleNamespace(id=id, issue_key=KEY, topic=topic, question=question, answer=answer,
                           decided_by=decided_by, created_at=ASKED_AT, record=dict(record))


def appended(rows: list, update: decision_escalation.DecisionUpdate) -> SimpleNamespace:
    """Append an update as the next row, as trio_gate.record does."""
    row = SimpleNamespace(id=max((r.id for r in rows), default=0) + 1, issue_key=KEY, topic=update.topic,
                          question=update.question, answer=update.answer, decided_by=update.decided_by,
                          created_at=ANSWERED_AT, record=update.record)
    rows.append(row)
    return row


def answered(**fields) -> SimpleNamespace:
    return make_decision(**{"answer": "Yes, split it", "decided_by": "trio", **fields})


class DecisionActionsTest(unittest.TestCase):
    def test_answer_appends_an_answered_row_and_a_forge_comment(self) -> None:
        tr = da.answer(make_decision(decided_by="zach"), "Yes, split it", decided_by="user", answered_at=ANSWERED_AT)
        self.assertEqual((tr.decision.status, tr.decision.answer, tr.decision.decided_by, tr.decision.answered_at),
                         ("answered", "Yes, split it", "user", ANSWERED_AT))
        self.assertEqual((tr.update.topic, tr.update.question, tr.update.answer, tr.update.decided_by),
                         ("design", "Split the parser into two passes?", "Yes, split it", "user"))
        self.assertEqual(tr.update.record["status"], "answered")
        self.assertTrue(any(isinstance(a, da.ForgeComment) and a.issue_key == KEY for a in tr.actions))
        self.assertEqual(tr.decision.intent, "")
        self.assertTrue(da.is_closed(tr.decision))

    def test_reverse_is_a_run_intent_on_the_decisions_own_row_in_the_reversal_shape(self) -> None:
        tr = da.reverse_intent(answered(), "No, keep one pass", decided_by="user",
                               answered_at="2026-10-09T10:30:00+00:00", stage="implement")
        self.assertEqual((tr.decision.status, tr.decision.rerouted_to, tr.decision.intent, tr.decision.note),
                         ("reversed", "implement", "run", "No, keep one pass"))
        # develop's reversal shape: no answer, so it blocks merge until its replacement is settled
        self.assertIsNone(tr.update.answer)
        self.assertEqual((tr.update.topic, tr.update.question), ("design", "Split the parser into two passes?"))
        self.assertEqual((tr.update.record["reversed"], tr.update.record["recommendation"], tr.update.record["intent"]),
                         (True, "Yes, split it", "run"))
        row = appended([answered()], tr.update)
        self.assertTrue(decision_escalation.is_reversed(row))
        self.assertTrue(any(isinstance(a, da.ForgeComment) and "reversed" in a.body.casefold() for a in tr.actions))

    def test_reroute_is_a_write_intent_on_the_decisions_own_row(self) -> None:
        tr = da.reroute(answered(decided_by="user"), "plan", answer_text="Re-plan the module boundary first",
                        decided_by="user", answered_at="2026-10-09T10:45:00+00:00")
        self.assertEqual((tr.decision.status, tr.decision.rerouted_to, tr.decision.intent),
                         ("re-routed", "plan", "write"))
        self.assertEqual((tr.update.record["rerouted_to"], tr.update.record["intent"], tr.update.answer),
                         ("plan", "write", "Re-plan the module boundary first"))
        self.assertTrue(any(isinstance(a, da.ForgeComment) and "plan" in a.body for a in tr.actions))
        self.assertEqual([a for a in tr.actions if not isinstance(a, da.ForgeComment)], [])

    def test_answered_decision_meeting_blocked_action_becomes_blocked_and_never_closed(self) -> None:
        # #288: an answered decision whose issue is blocked carries the reason the action cannot run,
        # shows on the board as blocked, and is never treated as closed.
        ship = answered(answer="Ship now", decided_by="user")
        for action, reason in (
            (reconcile.Wait(KEY, "merge conflicts"), "merge conflicts"),
            (reconcile.Escalate(KEY, "retry limit"), "retry limit"),
        ):
            with self.subTest(action=action):
                tr = da.meet_action(ship, action)
                self.assertEqual((tr.decision.status, tr.decision.blocked_reason, tr.decision.answer, tr.decision.decided_by),
                                 ("blocked", reason, "Ship now", "user"))
                self.assertEqual(da.board_status(tr.decision), "blocked")
                self.assertFalse(da.is_closed(tr.decision))
                self.assertEqual((tr.update.record["status"], tr.update.record["blocked_reason"], tr.update.answer),
                                 ("blocked", reason, "Ship now"))

    def test_blocked_decision_clears_to_answered_when_action_can_run(self) -> None:
        blocked = answered(answer="Ship now", decided_by="user", status="blocked", blocked_reason="merge conflicts")
        tr = da.meet_action(blocked, reconcile.Dispatch(KEY, "implement", "builder", 1))
        self.assertEqual((tr.decision.status, tr.decision.blocked_reason), ("answered", ""))
        self.assertTrue(da.is_closed(tr.decision))
        self.assertEqual(tr.update.record["status"], "answered")
        self.assertNotIn("blocked_reason", tr.update.record)

    def test_an_unchanged_state_appends_no_row(self) -> None:
        # Rows are append-only, so a pass that changes nothing must write nothing.
        self.assertIsNone(da.meet_action(answered(decided_by="user"), reconcile.Dispatch(KEY, "implement", "builder", 1)).update)
        blocked = answered(decided_by="user", status="blocked", blocked_reason="merge conflicts")
        self.assertIsNone(da.meet_action(blocked, reconcile.Wait(KEY, "merge conflicts")).update)

    def test_a_models_answer_is_not_marked_blocked(self) -> None:
        self.assertIsNone(da.meet_action(answered(decided_by="trio"), reconcile.Wait(KEY, "merge conflicts")).update)

    def test_blank_and_duplicate_answers_are_rejected(self):
        for record, text in ((make_decision(), "  "), (answered(), "Again")):
            with self.assertRaises(ValueError):
                da.answer(record, text)

    def test_settled_reroutes_and_reversals_do_not_change_when_waiting(self):
        for status in ("reversed", "re-routed"):
            rec = make_decision(answer="Change course", status=status)
            self.assertIsNone(da.meet_action(rec, reconcile.Wait(KEY, "no slots")).update)
            with self.assertRaises(ValueError):
                da.reverse_intent(rec, "Again")
            with self.assertRaises(ValueError):
                da.reroute(rec, "plan")

    def test_a_blocked_intent_keeps_its_target_and_phase(self):
        rerouted = make_decision(answer="Back", status="re-routed", rerouted_to="implement", intent="run")
        refused = da.refuse(rerouted, "retry limit reached")
        self.assertEqual((refused.decision.status, refused.decision.rerouted_to, refused.decision.intent),
                         ("blocked", "implement", "run"))
        self.assertEqual((refused.update.record["rerouted_to"], refused.update.record["intent"],
                          refused.update.record["intent_id"]), ("implement", "run", 7))

    def test_a_decision_with_an_intent_can_be_neither_answered_nor_restored_by_a_wait(self):
        for status in ("re-routed", "reversed", "blocked"):
            held = make_decision(answer="Back", status=status, rerouted_to="plan", intent="run")
            self.assertIsNone(da.meet_action(held, reconcile.Dispatch(KEY, "implement", "builder", 1)).update)
            with self.assertRaises(ValueError):
                da.answer(held, "Again")

    def test_an_unapplied_or_blocked_intent_can_be_replaced_by_a_new_intent(self):
        for status in ("re-routed", "reversed", "blocked"):
            for phase in ("write", "run"):
                held = make_decision(answer="Back", status=status, rerouted_to="plan", intent=phase, intent_id=3)
                for tr in (da.reverse_intent(held, "No"), da.reroute(held, "review")):
                    # A new intent begins on its own row, so it does not inherit the old intent's id.
                    self.assertNotIn("intent_id", tr.update.record)
                    rows = [held]
                    appended(rows, tr.update)
                    self.assertEqual(da.current_intent(rows).intent_id, rows[-1].id)

    def test_an_answered_decision_whose_action_is_blocked_can_still_be_reversed(self):
        blocked = answered(answer="Ship", status="blocked", blocked_reason="merge conflicts")
        self.assertEqual(da.reverse_intent(blocked, "No").decision.status, "reversed")


SEGMENT = lambda stage, attempt: SimpleNamespace(stage=stage, attempt=attempt)  # noqa: E731


class RespondTest(unittest.TestCase):
    """The use case chooses the stage, persona and attempt; the page driver only appends the result."""

    def setUp(self) -> None:
        self.policies = settings.load_policy()

    def respond(self, action, text="Because", *, record=None, target="", stage="review", history=(), issue_type=None,
                policies=None):
        return da.respond(record or make_decision(decided_by="zach"), action, text, target_stage=target,
                          current_stage=stage, history=history, policies=policies or self.policies,
                          issue_type=issue_type, answered_at=ANSWERED_AT)

    def test_reroute_runs_the_target_stage(self):
        tr = self.respond("reroute", target="implement")
        self.assertEqual((tr.decision.rerouted_to, tr.decision.intent), ("implement", "write"))

    def test_reroute_refuses_unknown_stages_and_stages_with_no_worker(self):
        for target in ("nowhere", "merge", ""):
            with self.subTest(target=target), self.assertRaises(da.BadRequest):
                self.respond("reroute", target=target)

    def test_reverse_returns_to_the_stage_that_did_the_work_never_to_merge(self):
        history = [SEGMENT("implement", 1), SEGMENT("review", 1), SEGMENT("merge", 1)]
        tr = self.respond("reverse", record=answered(topic="merge"), stage="merge", history=history)
        self.assertEqual((tr.decision.rerouted_to, tr.decision.intent), ("implement", "run"))

    def test_reverse_with_no_history_starts_the_issue_types_own_workflow(self):
        custom = policy.parse({
            "default_workflow": "standard",
            "personas": {"builder": {"role": "builds", "model": "example"}, "writer": {"role": "writes", "model": "example"}},
            "stages": {"implement": {"persona": "builder"}, "draft": {"persona": "writer"}, "merge": {}},
            "workflows": {"standard": {"start": "implement", "edges": [{"from": "implement", "to": "merge"}]},
                          "docs": {"issue_types": ["docs"], "start": "draft", "edges": [{"from": "draft", "to": "merge"}]}},
        })
        tr = self.respond("reverse", record=answered(topic="merge"), stage="merge", issue_type="docs", policies=custom)
        self.assertEqual(tr.decision.rerouted_to, "draft")

    def test_bad_requests_are_told_apart_from_state_conflicts(self):
        with self.assertRaises(da.BadRequest):
            self.respond("answer", "  ")
        with self.assertRaises(da.BadRequest):
            self.respond("shrug")
        with self.assertRaises(ValueError) as closed:
            self.respond("answer", record=answered())
        self.assertNotIsInstance(closed.exception, da.BadRequest)


def intent(id, phase="write", status="re-routed", stage="review", **fields):
    return make_decision(id=id, question=f"q{id}", answer="Back", status=status, rerouted_to=stage, intent=phase,
                         **fields)


class DecisionUpdateTest(unittest.TestCase):
    def test_block_appends_to_only_the_current_intent_and_keeps_its_intent(self) -> None:
        update = da.block([intent(1), intent(2, "run")], 2, "retry limit reached")
        self.assertEqual((update.question, update.record["status"]), ("q2", "blocked"))
        self.assertEqual((update.record["blocked_reason"], update.record["rerouted_to"], update.record["intent"]),
                         ("retry limit reached", "review", "run"))

    def test_block_with_no_matching_intent_appends_nothing(self) -> None:
        for intent_id in (None, 0, 3, 1):
            self.assertIsNone(da.block([intent(1), intent(2)], intent_id, "offline"))

    def test_block_leaves_an_open_or_answered_decision_alone(self) -> None:
        self.assertIsNone(da.block([make_decision(id=1)], 1, "offline"))
        self.assertIsNone(da.block([answered(id=1)], 1, "offline"))

    def test_restore_held_restores_only_blocked_answers_when_no_decision_is_open(self) -> None:
        blocked = answered(id=1, question="a", status="blocked", blocked_reason="merge is not ready")
        held = intent(2, "run", "blocked", blocked_reason="retry limit reached")
        self.assertEqual([(u.question, u.record["status"]) for u in da.restore_held([blocked, held])], [("a", "answered")])
        self.assertEqual(da.restore_held([blocked, make_decision(id=3, question="c")]), [])

    def test_unblock_restores_only_the_decision_whose_latest_row_has_that_id(self) -> None:
        blocked = [answered(id=n, question=f"q{n}", status="blocked", blocked_reason="wait") for n in (1, 2)]
        update = da.unblock(blocked, 1)
        self.assertEqual((update.question, update.record["status"]), ("q1", "answered"))
        self.assertNotIn("blocked_reason", update.record)
        self.assertIsNone(da.unblock(blocked, None))
        self.assertIsNone(da.unblock([intent(1)], 1))


class CurrentIntentTest(unittest.TestCase):
    """One current intent per issue is chosen here, in the Use Cases; no driver picks or supersedes."""

    def test_the_current_intent_is_the_newest_with_an_intent(self):
        rows = [intent(1), answered(id=2, question="x"), intent(3, "run"), intent(4, "", "re-routed")]
        self.assertEqual(da.current_intent(rows).intent_id, 3)
        self.assertIsNone(da.current_intent([rows[1], rows[3]]))
        self.assertIsNone(da.current_intent([]))

    def test_only_a_decisions_latest_row_counts(self):
        # Append-only: a later row of the same decision replaces its earlier intent.
        rows = [intent(1), make_decision(id=2, question="q1", answer="Back", status="superseded")]
        self.assertIsNone(da.current_intent(rows))

    def test_a_blocked_intent_stays_current_until_a_newer_one_replaces_it(self):
        rows = [intent(1, "run", "blocked", blocked_reason="retry limit reached")]
        self.assertEqual(da.current_intent(rows).intent_id, 1)
        self.assertEqual(da.current_intent([*rows, intent(2, "run", "reversed", stage="implement")]).intent_id, 2)

    def test_supersede_closes_every_older_pending_or_blocked_intent_by_the_current_one(self):
        rows = [intent(1, "write"), intent(2, "run", "blocked", blocked_reason="retry limit reached"),
                answered(id=3, question="x"), intent(4, "run", "reversed")]
        updates = da.supersede(rows)
        self.assertEqual([(u.question, u.record["status"], u.record["superseded_by"], u.record.get("intent"),
                           u.record.get("blocked_reason")) for u in updates],
                         [("q1", "superseded", 4, None, None), ("q2", "superseded", 4, None, None)])

    def test_supersede_with_one_intent_or_none_changes_nothing(self):
        self.assertEqual(da.supersede([intent(1)]), [])
        self.assertEqual(da.supersede([make_decision()]), [])

    def test_advance_moves_the_intent_from_its_write_to_its_run_and_then_ends_it(self):
        rows = [intent(1, "write")]
        write = da.advance(rows, 1)
        self.assertEqual((write.record["intent"], write.record["status"], write.record["intent_id"]), ("run", "re-routed", 1))
        appended(rows, write)
        self.assertEqual(da.current_intent(rows).intent_id, 1)  # the same intent, across its phases
        done = da.advance(rows, 1)
        self.assertEqual((done.record.get("intent"), done.record["status"], done.record["rerouted_to"]),
                         (None, "re-routed", "review"))
        appended(rows, done)
        self.assertIsNone(da.advance(rows, 1))
        self.assertIsNone(da.advance([intent(1, "run", "blocked", blocked_reason="retry limit reached")], 1))
        self.assertIsNone(da.advance([intent(1)], 2))

    def test_a_reversals_done_phase_is_the_restarted_row(self):
        rows = [answered()]
        appended(rows, da.reverse_intent(rows[0], "No", stage="implement").update)
        self.assertTrue(decision_escalation.open_reversal(rows))
        done = da.advance(rows, rows[-1].id, "implement", 2)
        self.assertEqual((done.record["restarted"], done.record["restart_attempt"], done.record.get("intent")),
                         ("implement", 2, None))
        appended(rows, done)
        self.assertFalse(decision_escalation.open_reversal(rows))
        self.assertIsNone(da.current_intent(rows))

    def test_advance_and_block_apply_only_to_the_current_intent(self):
        older, newer = intent(1, "write"), intent(2, "run")
        self.assertIsNone(da.advance([older, newer], 1))
        self.assertIsNone(da.block([older, newer], 1, "retry limit reached"))
        self.assertNotIn("intent", da.advance([older, newer], 2).record)

    def test_an_intent_replaces_the_engines_pending_move_only_when_it_writes_a_stage(self):
        stage = "implement"
        self.assertTrue(da.replaces_engine_move(da.state_of(intent(1, "write", stage="implement")), stage))
        self.assertTrue(da.replaces_engine_move(da.state_of(intent(1, "run", stage="review")), stage))
        self.assertFalse(da.replaces_engine_move(da.state_of(intent(1, "run", stage="implement")), stage))
        self.assertFalse(da.replaces_engine_move(da.state_of(intent(1, "write", "blocked", stage="review")), stage))


class HumanAnswerTest(unittest.TestCase):
    """Human answer wins: no model row lands over a person's answer (architect ruling on #758, item 2)."""

    def test_a_persons_latest_answer_is_returned_for_its_decision_only(self):
        rows = [make_decision(id=1, decided_by="zach"), answered(id=2, decided_by="user", answer="Keep it")]
        trio = decision_escalation.DecisionUpdate("design", "Split the parser into two passes?", "Split", "trio", {})
        self.assertEqual(da.human_answer(rows, trio).answer, "Keep it")
        self.assertIsNone(da.human_answer(rows, decision_escalation.DecisionUpdate("design", "Another?", "Split", "trio", {})))
        self.assertIsNone(da.human_answer([answered(decided_by="trio")], trio))


if __name__ == "__main__":
    unittest.main()
