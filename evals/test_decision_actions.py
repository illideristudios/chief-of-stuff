"""Owner waits and merge blocking across restarts, standing approvals, malformed times and rescoped gates."""

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace as NS

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import decision_escalation

T0 = datetime(2000, 1, 1, tzinfo=timezone.utc)


def at(minutes):
    return (T0 + timedelta(minutes=minutes)).isoformat()


OWNER = "zach"  # the persisted decided_by value; #809 renames it


def row(topic, question="Q?", answer=None, by=OWNER, minutes=0, **record):
    return NS(topic=topic, question=question, answer=answer, decided_by=by, created_at=at(minutes), record=record)


def seg(stage, attempt, minutes):
    return NS(stage=stage, attempt=attempt, started_at=at(minutes))


def hold(stage="implement", attempt=1, context="implement-1", minutes=1):
    return row("plan", "Gate?", minutes=minutes, recommendation="hold", decision={
        "stage": stage, "attempt": attempt, "context": context, "options": ["approve", "hold"], "gate": True})


class RestartSupersession(unittest.TestCase):
    def setUp(self):
        self.split = row("design", "Split?", minutes=1, decision={"stage": "review", "attempt": 1, "context": "x"})
        self.reversal = row("plan", "Rework?", by="", minutes=5, reversed=True)
        self.restart = row("plan", "Rework?", by="", minutes=7, restarted="implement", restart_attempt=2)
        self.settled = row("plan", "Rework?", answer="redo", by="trio", minutes=9)
        self.segments = [seg("review", 1, 0), seg("implement", 2, 6)]

    def test_a_restart_supersedes_an_older_non_gate_wait_for_merge(self):
        self.assertTrue(decision_escalation.pending_decisions([self.split], (), self.segments))
        rows = [self.split, self.reversal, self.restart, self.settled]
        self.assertEqual(decision_escalation.owner_waits(rows, self.segments, []), [])
        self.assertFalse(decision_escalation.pending_decisions(rows, (), self.segments))

    def test_the_reversed_decision_itself_blocks_merge_until_replaced(self):
        self.assertTrue(decision_escalation.pending_decisions([self.split, self.reversal], (), self.segments))
        self.assertTrue(decision_escalation.pending_decisions([self.split, self.reversal, self.restart], (), self.segments))

    def test_a_wait_raised_after_the_restart_still_blocks(self):
        later = row("design", "Split again?", minutes=10, decision={"stage": "implement", "attempt": 2})
        self.assertTrue(decision_escalation.pending_decisions(
            [self.split, self.reversal, self.restart, self.settled, later], (), self.segments))


class StandingApproval(unittest.TestCase):
    def test_a_passed_gate_no_longer_waits_on_the_owner(self):
        rows = [hold()]
        self.assertEqual(len(decision_escalation.owner_waits(rows, [seg("implement", 1, 0)], [])), 0)
        self.assertEqual(decision_escalation.owner_waits(rows, [seg("implement", 1, 0)], [], passed=("plan",)), [])


class MalformedTimes(unittest.TestCase):
    def test_a_naive_timestamp_skips_the_restart_candidate_instead_of_raising(self):
        naive = row("plan", "Rework?", by="", reversed=True)
        naive.created_at = "2000-01-01T00:00:00"
        segments = [NS(stage="implement", attempt=2, started_at="2000-01-01T00:10:00"), seg("implement", 1, 0)]
        waiting = hold(stage="review", attempt=1, context="review-1")
        waiting.record["decision"]["gate"] = False
        rows = [waiting, naive]
        self.assertEqual(len(decision_escalation.owner_waits(rows, segments, [])), 1)
        self.assertTrue(decision_escalation.pending_decisions(rows, (), segments))

    def test_a_naive_segment_start_never_supersedes_a_wait(self):
        restart = row("plan", "Rework?", by="", restarted="implement", restart_attempt=2, minutes=7)
        segments = [NS(stage="review", attempt=1, started_at="2000-01-01T00:00:00"), seg("implement", 2, 6)]
        waiting = hold(stage="review", attempt=1, context="review-1")
        waiting.record["decision"]["gate"] = False
        self.assertEqual(len(decision_escalation.owner_waits([waiting, restart], segments, [])), 1)


class RescopedGate(unittest.TestCase):
    def test_a_stored_return_edge_scope_follows_its_context(self):
        stored = hold(stage="architect", attempt=1, context="review-1")
        segments = [seg("review", 1, 0), seg("architect", 1, 0), seg("architect", 2, 5)]
        self.assertEqual(decision_escalation.owner_waits([stored], segments, []), [])

    def test_an_ordinary_ask_keeps_its_stored_scope(self):
        ask = row("ask", "Which?", minutes=1, decision={"stage": "implement", "attempt": 1, "context": "review-1",
                                                       "instance": "ask-1", "options": ["a", "b"]})
        wait, = decision_escalation.owner_waits([ask], [seg("implement", 1, 0)], [])
        self.assertEqual((wait.stage, wait.attempt), ("implement", 1))


if __name__ == "__main__":
    unittest.main()
