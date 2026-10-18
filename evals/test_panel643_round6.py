"""Round-six lifecycle regressions: one status vocabulary, read only by `state_of`."""
import unittest

from evals import test_board_decisions as decisions
import decision_actions as da
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

    def test_a_blocked_intent_has_one_status_and_cannot_restore_as_an_answer(self):
        rows = [decisions.make_decision(status="reversed", answer="Redo", rerouted_to="implement", intent="run")]
        blocked = da.state_of(decisions.appended(rows, da.block(rows, rows[0].id, "retry limit reached")))
        self.assertEqual((blocked.status, da.state_of(blocked).status), ("blocked", "blocked"))
        self.assertIsNone(da.meet_action(blocked, reconcile.Dispatch(decisions.KEY, "implement", "builder", 1)).update)
        self.assertEqual(da.restore_held(rows), [])
        with self.assertRaises(ValueError):
            da.answer(blocked, "Discard reversal")


if __name__ == "__main__":
    unittest.main()
