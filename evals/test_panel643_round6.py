"""Round-six lifecycle regressions, exercised through board POSTs and the engine."""
import unittest
from types import SimpleNamespace
from unittest import mock

from evals import test_engine as engines
from evals import test_panel643_round2 as board
from evals import test_decision_actions as decisions
from evals import test_reconcile as planner
import decision_actions as da
import engine
import reconcile


class VocabularyTest(unittest.TestCase):
    def test_state_of_reads_the_same_status_from_a_row_and_a_state(self):
        row = decisions.make_decision(status="reversed", blocked_reason="old data")
        state = da.DecisionState(7, decisions.KEY, "design", "Choose", "reversed", blocked_reason="old data")
        self.assertEqual(da.state_of(row).status, "reversed")
        self.assertEqual(da.state_of(row).status, da.state_of(state).status)
        for record in (row, state):
            with self.assertRaises(ValueError):
                da.answer(record, "Discard reversal")

    def test_a_blocked_reversal_has_one_status_and_cannot_restore_as_an_answer(self):
        row = decisions.make_decision(status="reversed", answer="Redo")
        blocked = da.block([row], row.id, "retry limit reached").decision
        self.assertEqual(blocked.status, "blocked")
        self.assertEqual(da.state_of(blocked).status, "blocked")
        self.assertIsNone(da.meet_action(blocked, reconcile.Dispatch(decisions.KEY, "implement", "builder", 1)).store_write)
        with self.assertRaises(ValueError):
            da.answer(blocked, "Discard reversal")


class ReroutePostTest(unittest.TestCase):
    setUp = board.BoardPostTest.setUp
    post = board.BoardPostTest.post

    def forge(self, ok=True):
        fg = mock.Mock()
        fg.write_stage.return_value = SimpleNamespace(ok=ok, done=ok, error="" if ok else "offline")
        return fg

    def reroute(self, decision_id, stage, fg):
        return self.post({"decision_id": decision_id, "action": "reroute", "target_stage": stage}, fg)

    def test_each_reroute_appends_a_row_and_names_that_row_in_both_slots(self):
        original = self.db.add_decision(board.KEY, "design", "Choose")
        self.assertEqual(self.reroute(original.id, "implement", self.forge(False)), 409)
        first = self.db.decisions(board.KEY)[-1]
        self.assertNotEqual(first.id, original.id)
        self.assertEqual(self.db.decision(original.id).record["superseded_by"], first.id)
        self.assertEqual(self.db.resume(board.KEY)["pending_stage"]["decision_id"], first.id)
        self.assertEqual(self.db.resume(board.KEY)["redispatch"]["decision_id"], first.id)
        self.assertEqual(self.reroute(first.id, "review", self.forge(False)), 409)
        second = self.db.decisions(board.KEY)[-1]
        self.assertNotEqual(first.id, second.id)
        self.assertEqual(self.db.decision(first.id).status, "superseded")
        self.assertEqual(self.db.decision(first.id).record["superseded_by"], second.id)

    def test_a_new_reroute_supersedes_a_blocked_reroute_for_another_question(self):
        original = self.db.add_decision(board.KEY, "design", "Choose A")
        self.reroute(original.id, "implement", self.forge(False))
        first = self.db.decisions(board.KEY)[-1]
        other = self.db.add_decision(board.KEY, "scope", "Choose B")
        self.assertEqual(self.reroute(other.id, "review", self.forge()), 303)
        second = self.db.decisions(board.KEY)[-1]
        self.assertEqual(self.db.decision(first.id).status, "superseded")
        self.assertEqual(self.db.decision(first.id).record["superseded_by"], second.id)
        self.assertTrue(da.is_closed(self.db.decision(first.id)))
        self.assertNotIn("pending_stage", self.db.resume(board.KEY))
        self.assertEqual(self.db.resume(board.KEY)["redispatch"]["decision_id"], second.id)

    def test_a_late_result_of_a_reroute_on_the_same_decision_is_a_no_op(self):
        for ok in (True, False):
            with self.subTest(ok=ok):
                original = self.db.add_decision(board.KEY, "design", f"Choose {ok}")
                old_forge = self.forge()
                saved = {}

                def newer_intent(*args, **kwargs):
                    first = self.db.decisions(board.KEY)[-1]
                    self.assertEqual(self.reroute(first.id, "review", self.forge(False)), 409)
                    saved["resume"] = self.db.resume(board.KEY)
                    saved["rows"] = [(d.id, d.status, d.blocked_reason) for d in self.db.decisions(board.KEY)]
                    saved["logs"] = [e.message for e in self.db.log(board.KEY)]
                    return SimpleNamespace(ok=ok, done=ok, error="late failure")

                old_forge.write_stage.side_effect = newer_intent
                self.reroute(original.id, "implement", old_forge)
                self.assertEqual(self.db.item(board.KEY).stage, "review")
                self.assertEqual(self.db.resume(board.KEY), saved["resume"])
                self.assertEqual([(d.id, d.status, d.blocked_reason) for d in self.db.decisions(board.KEY)], saved["rows"])
                self.assertEqual([e.message for e in self.db.log(board.KEY)], saved["logs"])


class PlanningHoldTest(unittest.TestCase):
    def test_blocked_or_pending_reroutes_hold_every_stage_before_item_planning(self):
        for stage in ("implement", "review", "merge"):
            for status in ("blocked", "re-routed"):
                with self.subTest(stage=stage, status=status):
                    dec = SimpleNamespace(id=1, topic="scope", question="Back", answer="Redo", status=status,
                                          rerouted_to="implement", blocked_reason="retry limit reached" if status == "blocked" else "")
                    actions = reconcile.plan([planner.Item(stage=stage)], {planner.KEY: [planner.Segment(stage)]},
                                             planner.policies(), {}, 1, planner.NOW, {}, decisions={planner.KEY: [dec]},
                                             pending_stages={planner.KEY: {"decision_id": 1}} if status == "re-routed" else {})
                    self.assertEqual(len(actions), 1)
                    self.assertIsInstance(actions[0], reconcile.Wait)
                    self.assertIn("re-route", actions[0].reason)


class EngineCompletionTest(unittest.TestCase):
    setUp = engines.EngineTest.setUp
    run_pass = engines.EngineTest.run_pass
    launch = engines.EngineTest.launch

    def test_a_late_engine_stage_completion_does_not_update_the_item(self):
        self.db.sync_from_forge(self.forge.snapshots())
        old = {"stage": "review", "expected": "implement", "decision_id": 1}
        newer = {"stage": "implement", "expected": "review", "decision_id": 2}
        self.db.set_resume(engines.KEY, {"pending_stage": old})

        def replace_intent(*args, **kwargs):
            self.db.set_resume(engines.KEY, {"pending_stage": newer})
            return SimpleNamespace(ok=True, done=True, error="")

        with mock.patch.object(self.forge, "write_stage", side_effect=replace_intent):
            engine._write_stage(self.forge, self.db, engines.KEY, old)
        self.assertEqual(self.db.item(engines.KEY).stage, "implement")
        self.assertEqual(self.db.resume(engines.KEY)["pending_stage"], newer)

    def test_a_late_engine_refusal_does_not_log_or_block_a_newer_intent(self):
        self.db.sync_from_forge(self.forge.snapshots())
        old = {"stage": "review", "expected": "implement", "decision_id": 1}
        newer = {"stage": "implement", "expected": "review", "decision_id": 2}
        self.db.set_resume(engines.KEY, {"pending_stage": old})

        def replace_intent(*args, **kwargs):
            self.db.set_resume(engines.KEY, {"pending_stage": newer})
            return SimpleNamespace(ok=False, done=False, error="stale refusal")

        with mock.patch.object(self.forge, "write_stage", side_effect=replace_intent):
            self.run_pass()
        self.assertEqual(self.db.resume(engines.KEY)["pending_stage"], newer)
        self.assertFalse(any("stale refusal" in entry.message for entry in self.db.log(engines.KEY)))

    def test_a_launch_completion_keeps_the_newer_board_stage_intent(self):
        self.forge.rows[0]["stage"] = "review"
        self.db.sync_from_forge(self.forge.snapshots())
        old = {"stage": "implement", "persona": "builder", "attempt": 1, "decision_id": 1}
        newer = {"stage": "review", "expected": "implement", "decision_id": 2}
        self.db.set_resume(engines.KEY, {"redispatch": old})
        launch = self.launch

        def newer_intent(*args):
            result = launch(*args)
            self.db.update_resume(engines.KEY, lambda data: data.update(
                pending_stage=newer, redispatch={**old, "stage": "review", "decision_id": 2}))
            return result

        self.launch = newer_intent
        self.run_pass()
        self.assertEqual(self.db.resume(engines.KEY)["pending_stage"], newer)

    def test_a_spent_reversal_waits_after_earlier_completed_work(self):
        self.db.sync_from_forge(self.forge.snapshots())
        self.db.record_segment(engines.KEY, "implement", 1, engines.NOW.isoformat(), engines.NOW.isoformat(), "done")
        dec = self.db.add_decision(engines.KEY, "design", "Back?")
        self.db.answer_decision(dec.id, "Redo", status="reversed")
        request = {"stage": "implement", "persona": "builder", "attempt": 9, "decision_id": dec.id}
        self.db.set_resume(engines.KEY, {"redispatch": request})
        with mock.patch.object(reconcile, "plan", return_value=[reconcile.Escalate(engines.KEY, "retry limit reached")]):
            self.run_pass()
        self.assertNotIn("redispatch", self.db.resume(engines.KEY))
        for _ in range(2):
            self.assertIsInstance(self.run_pass()[0], reconcile.Wait)
        self.assertEqual(self.launched, [])
        self.assertEqual(self.db.decision(dec.id).status, "blocked")
