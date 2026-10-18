"""Round-eight: an unapplied intent can be replaced, the engine re-checks currency around every write, and a
reversal writes its own stage."""
import unittest
from types import SimpleNamespace
from unittest import mock

from evals import test_engine as engines
from evals.test_panel643_round7 import IntentEndToEndTest as Round7
import engine
import reconcile

KEY = engines.KEY


class IntentCurrencyTest(unittest.TestCase):
    setUp = engines.EngineTest.setUp
    run_pass = engines.EngineTest.run_pass
    launch = engines.EngineTest.launch
    post = Round7.post
    asked = Round7.asked
    current = Round7.current

    def add_intent(self, source, action="reroute", stage="review", text="Back"):
        """What the page server appends: the decision's next row, posted from the row the board showed."""
        data = {"decision_id": source.id, "action": action, "target_stage": stage, "answer": text}
        self.assertEqual(self.post(data), 303)
        return self.db.decisions(KEY)[-1]

    def test_a_reroute_whose_write_keeps_failing_can_be_re_routed_again_from_the_board(self):
        first = self.asked("design", "Back?")
        pending = self.add_intent(first)
        with mock.patch.object(self.forge, "write_stage", side_effect=RuntimeError("offline")):
            self.run_pass()
        self.assertEqual(self.current(pending).intent, "write")
        again = self.add_intent(pending, stage="implement")
        # The newer intent is the decision's latest row, so the older one is gone with it.
        self.assertEqual((self.current(pending).rerouted_to, self.current(pending).intent_id), ("implement", again.id))
        self.run_pass()
        self.assertEqual((self.current(again).intent, self.launched), ("", [(KEY, "implement", 1, "builder")]))

    def test_a_blocked_reversal_can_be_reversed_again_from_the_board(self):
        done = self.asked("scope", "Ship?", "Yes")
        reversal = self.add_intent(done, "reverse", text="No")
        blocked = self.db.add_decision(KEY, reversal.topic, reversal.question, None, "user",
                                       {**reversal.record, "status": "blocked", "blocked_reason": "retry limit reached",
                                        "intent_id": reversal.id})
        self.run_pass()
        self.assertEqual(self.launched, [])
        self.add_intent(blocked, "reverse", text="Still no")
        self.run_pass()
        self.assertEqual(self.launched, [(KEY, "implement", 1, "builder")])

    def test_an_intent_that_arrives_while_the_stage_is_written_is_not_resurrected_or_cleared(self):
        first = self.asked("design", "Back?")
        older = self.add_intent(first)
        second = self.asked("scope", "Ship?", "Yes")
        real, newer = self.forge.write_stage, []

        def write_then_a_newer_intent_arrives(*args, **kwargs):
            result = real(*args, **kwargs)
            if not newer:
                newer.append(self.add_intent(second, "reverse", text="No"))
            return result

        with mock.patch.object(self.forge, "write_stage", side_effect=write_then_a_newer_intent_arrives):
            self.run_pass()
        self.assertEqual(self.launched, [])
        self.assertEqual(self.current(older).intent, "write")  # not advanced: it was no longer current
        self.run_pass()
        self.assertEqual(self.launched, [(KEY, "implement", 1, "builder")])
        self.assertEqual((self.current(older).status, self.current(older).intent), ("superseded", ""))
        self.assertEqual((self.current(newer[0]).status, self.current(newer[0]).intent), ("reversed", ""))

    def test_a_first_intent_that_arrives_after_planning_stops_the_obsolete_worker(self):
        self.db.sync_from_forge(self.forge.snapshots())
        done = self.asked("scope", "Ship?", "Yes")
        real = engine.reconcile.plan

        def plan_then_an_intent_arrives(*args, **kwargs):
            actions = real(*args, **kwargs)
            self.add_intent(done, "reroute", "review")
            return actions

        with mock.patch.object(engine.reconcile, "plan", side_effect=plan_then_an_intent_arrives):
            self.run_pass()
        self.assertEqual(self.launched, [])

    def test_a_run_intent_does_not_discard_the_engines_queued_stage_write(self):
        done = self.asked("scope", "Ship?", "Yes")
        reversal = self.add_intent(done, "reverse", "implement", text="No")
        self.assertEqual((reversal.status, reversal.rerouted_to, reversal.intent), ("reversed", "implement", "run"))
        self.db.update_resume(KEY, lambda data: data.__setitem__("pending_stage", {"stage": "review", "expected": "implement"}))
        engine._apply_intent(self.forge, self.db, KEY)
        self.assertEqual(self.db.resume(KEY)["pending_stage"], {"stage": "review", "expected": "implement"})

    def test_a_reversal_from_a_held_stage_writes_its_stage_and_drops_the_hold(self):
        self.forge.kanban = SimpleNamespace(holds=lambda stage: stage == "merge", stage_map={})
        self.forge.rows[0]["stage"] = "merge"
        self.db.sync_from_forge(self.forge.snapshots())
        self.db.upsert_item(KEY, human_review=True)
        done = self.asked("implement", "Ship?", "Yes")
        reversal = self.add_intent(done, "reverse", text="No")
        self.assertEqual((reversal.intent, reversal.rerouted_to), ("run", "implement"))
        self.run_pass()
        self.assertEqual(self.forge.writes, [(1, "implement", "merge", True)])
        # develop's stage-write rule: only the forge's next read clears a hold, so no held issue launches mid-pass.
        self.assertEqual(self.launched, [])
        self.run_pass()
        self.assertFalse(self.db.item(KEY).human_review)
        self.assertEqual(self.launched, [(KEY, "implement", 1, "builder")])


if __name__ == "__main__":
    unittest.main()
