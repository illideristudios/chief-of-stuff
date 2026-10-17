"""The reconcile planner uses snapshots only; no forge, store or model."""

import sys
import tomllib
import unittest
from dataclasses import FrozenInstanceError, dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import policy
import reconcile

NOW = datetime(2000, 1, 1, tzinfo=timezone.utc)
KEY = "github:example/project#1"


@dataclass(frozen=True)
class Item:
    issue_key: str = KEY
    stage: str = "implement"
    forge_state: str = "open"
    review_state: str = ""
    merge_state: str = ""
    human_review: bool = False


@dataclass(frozen=True)
class Segment:
    stage: str
    attempt: int = 1
    started_at: datetime = NOW - timedelta(hours=2)
    ended_at: datetime | None = NOW - timedelta(hours=1)
    outcome: str | None = "done"


@dataclass(frozen=True)
class Decision:
    answer: str | None = None
    topic: str = "scope"
    question: str = "Split the parser?"


def policies():
    return policy.parse({
        "default_workflow": "standard",
        "personas": {"builder": {"role": "implements", "model": "example"},
                     "critic": {"role": "reviews", "model": "example"}},
        "stages": {"implement": {"persona": "builder"}, "review": {"persona": "critic"}, "merge": {}},
        "rooms": {"lab": {"policy": {"retry_limit": 5}}},
        "workflows": {"standard": {"start": "implement", "edges": [
            {"from": "implement", "to": "review", "gate": "plan"},
            {"from": "review", "to": "implement"},
            {"from": "review", "to": "merge", "gate": "merge"},
        ]}},
    })


class ReconcileTest(unittest.TestCase):
    def setUp(self):
        self.p = policies()

    def test_drifted_pending_stage_replaces_a_hold_only_on_an_open_card_with_a_stage(self):
        for stage in ("merge", ""):
            for state in ("open", "closed"):
                for restore in (False, True):
                    with self.subTest(stage=stage, state=state, restore=restore):
                        item = replace(Item(), stage=stage, forge_state=state)
                        pending = {"stage": "review", "expected": "implement", "restore_hold": restore}
                        action = reconcile.hold_recovery(item, (), pending_hold=None, pending_release=None,
                                                         owned=None, pending_stage=pending)
                        expected = "replace" if restore and state == "open" and stage else "drop_stage"
                        self.assertEqual(action, expected)

    def test_suite_hold_lifecycle_is_decided_from_facts(self):
        hold = {"stage": "implement", "attempt": 1, "published": True}
        history = [Segment("implement", outcome="human_review")]
        decide = reconcile.suite_hold_action
        self.assertEqual(decide(Item(), hold, history, observed=False), "accept")
        for observed in (True, None):
            self.assertEqual(decide(Item(), hold, history, observed=observed), "wait")
        self.assertEqual(decide(Item(), {**hold, "published": False}, history, observed=False), "publish")
        self.assertEqual(decide(Item(forge_state="closed"), hold, history, observed=False), "retire")
        for invalid_history in ([], [Segment("implement", ended_at=None, outcome="human_review")],
                                [Segment("implement", outcome="done")]):
            with self.subTest(history=invalid_history):
                with self.assertRaisesRegex(ValueError, "completed human-review segment"):
                    decide(Item(), hold, invalid_history, observed=False)

    def plan(self, item=Item(), history=(), findings=None, slots=4, **kwargs):
        return reconcile.plan([item], {item.issue_key: history}, self.p, {}, slots, NOW,
                              {} if findings is None else {item.issue_key: findings}, **kwargs)

    def test_closed_items_and_any_running_segment_get_no_actions(self):
        self.assertEqual(self.plan(replace(Item(), forge_state="closed")), [])
        self.assertEqual(self.plan(history=[Segment("review", ended_at=None)]), [])

    def test_completed_stage_follows_its_edge_and_launches_review(self):
        self.assertEqual(self.plan(history=[Segment("implement")]), [reconcile.Review(KEY, 1)])

    def test_gated_edge_asks_only_when_its_resolved_policy_requires_zach(self):
        self.p.stages["implement"] = policy.Place("implement", "builder", {"zach_gates": ("plan",)})
        self.assertEqual(self.plan(history=[Segment("implement")]),
                         [reconcile.Ask(KEY, "plan", "gate requires Zach's approval")])
        self.p.stages["implement"] = policy.Place("implement", "builder")
        self.assertEqual(self.plan(history=[Segment("implement")]), [reconcile.Review(KEY, 1)])

    def test_approval_must_match_gate_and_expire_strictly_after_now(self):
        for gate, expires, allowed in (("plan", NOW + timedelta(seconds=1), True),
                                       ("plan", NOW, False), ("plan", NOW - timedelta(seconds=1), False),
                                       ("merge", NOW + timedelta(days=1), False)):
            with self.subTest(gate=gate, expires=expires):
                self.p = replace(self.p, base=policy.Policy(zach_gates=("plan",),
                                 approvals=(policy.Approval(gate, expires),)))
                action = self.plan(history=[Segment("implement")])[0]
                self.assertIsInstance(action, reconcile.Review if allowed else reconcile.Ask)

    def test_blocking_findings_route_review_back_to_implementation(self):
        item = replace(Item(), stage="review", review_state="changes_requested")
        self.assertEqual(self.plan(item, [Segment("implement"), Segment("review", started_at=NOW)], {"minor": 1}),
                         [reconcile.Dispatch(KEY, "implement", "builder", 2)])

    def test_shared_implementation_stage_gets_three_rework_rounds_after_initial_work(self):
        item = replace(Item(), stage="review", review_state="changes_requested")
        for initial_attempts in (1, 2):
            history = [Segment("implement", attempt, started_at=NOW - timedelta(hours=8 - attempt),
                               ended_at=NOW - timedelta(hours=7 - attempt))
                       for attempt in range(1, initial_attempts + 1)]
            history.append(Segment("review", started_at=NOW - timedelta(hours=4),
                                   ended_at=NOW - timedelta(hours=3)))
            for round in range(1, 4):
                attempt = initial_attempts + round
                for stage in ("review", "implement"):
                    for order in (history, history[::-1]):
                        with self.subTest(initial_attempts=initial_attempts, round=round, stage=stage, history=order):
                            self.assertEqual(self.plan(replace(item, stage=stage), order, {"minor": 1}),
                                             [reconcile.Dispatch(KEY, "implement", "builder", attempt)])
                start = NOW - timedelta(minutes=10 - 2 * round)
                history.append(Segment("implement", attempt, started_at=start, ended_at=start + timedelta(seconds=30)))
                self.assertEqual(self.plan(replace(item, stage="implement"), history, {"minor": 1}),
                                 [reconcile.Review(KEY, round + 1)])
                start += timedelta(minutes=1)
                history.append(Segment("review", round + 1, started_at=start, ended_at=start + timedelta(seconds=30)))
            self.assertEqual(self.plan(item, history, {"minor": 1}),
                             [reconcile.Escalate(KEY, "retry limit reached for implement")])

    def test_review_target_gets_initial_work_before_any_completed_review(self):
        self.p = replace(self.p, base=policy.Policy(retry_limit=0))
        for history in ([], [replace(Segment("review"), started_at="", ended_at=None)]):
            with self.subTest(history=history):
                self.assertEqual(self.plan(history=history), [reconcile.Dispatch(KEY, "implement", "builder", 1)])
        self.assertEqual(self.plan(history=[Segment("implement", outcome="failed")]),
                         [reconcile.Escalate(KEY, "retry limit reached for implement")])

    def test_quota_outcomes_do_not_consume_the_retry_limit_the_way_failures_do(self):
        self.p = replace(self.p, base=policy.Policy(retry_limit=1))
        initial = Segment("implement", started_at=NOW - timedelta(hours=6), ended_at=NOW - timedelta(hours=5))
        review = Segment("review", started_at=NOW - timedelta(hours=4), ended_at=NOW - timedelta(hours=3))
        rework = Segment("implement", 2, started_at=NOW - timedelta(hours=2), ended_at=NOW - timedelta(hours=1))
        again = Segment("review", 2, started_at=NOW - timedelta(minutes=50), ended_at=NOW - timedelta(minutes=40))
        item = replace(Item(), stage="review", review_state="changes_requested")
        for outcome, expected in (("quota", reconcile.Dispatch(KEY, "implement", "builder", 3)),
                                  ("unknown", reconcile.Dispatch(KEY, "implement", "builder", 3)),
                                  ("failed", reconcile.Escalate(KEY, "retry limit reached for implement"))):
            with self.subTest(outcome=outcome, rework=True):
                self.assertEqual(self.plan(item, [initial, review, replace(rework, outcome=outcome), again], {"minor": 1}),
                                 [expected])
        self.p = replace(self.p, base=policy.Policy(retry_limit=0))
        for outcome in ("quota", "unknown"):
            with self.subTest(outcome=outcome, rework=False):
                self.assertEqual(self.plan(history=[Segment("implement", outcome=outcome)]),
                                 [reconcile.Dispatch(KEY, "implement", "builder", 2)])

    def test_work_starting_at_first_review_end_counts_as_rework(self):
        item = replace(Item(), stage="review", review_state="changes_requested")
        history = [Segment("implement", started_at=NOW - timedelta(hours=6),
                           ended_at=NOW - timedelta(hours=5)),
                   Segment("review", started_at=NOW - timedelta(hours=4),
                           ended_at=NOW - timedelta(hours=3))]
        for round in range(1, 4):
            self.assertEqual(self.plan(item, history, {"minor": 1}),
                             [reconcile.Dispatch(KEY, "implement", "builder", round + 1)])
            start = history[-1].ended_at
            history.append(Segment("implement", round + 1, started_at=start,
                                   ended_at=start + timedelta(minutes=30)))
            start += timedelta(minutes=30)
            history.append(Segment("review", round + 1, started_at=start,
                                   ended_at=start + timedelta(minutes=30)))
        for stage in ("review", "implement"):
            for order in (history, history[::-1]):
                with self.subTest(stage=stage, history=order):
                    self.assertEqual(self.plan(replace(item, stage=stage), order, {"minor": 1}),
                                     [reconcile.Escalate(KEY, "retry limit reached for implement")])

    def test_separate_fix_stage_loops_to_review_until_its_retry_limit(self):
        self.p.stages["fix"] = policy.Place("fix", "builder")
        self.p.workflows["standard"] = replace(self.p.workflows["standard"], edges=(
            policy.Edge("implement", "review"), policy.Edge("review", "fix"),
            policy.Edge("fix", "review"), policy.Edge("review", "merge", "merge")))
        item = replace(Item(), stage="review", review_state="changes_requested")
        self.assertEqual(self.plan(item, [Segment("review")], {"minor": 1}),
                         [reconcile.Dispatch(KEY, "fix", "builder", 1)])
        self.assertEqual(self.plan(replace(item, stage="fix"), [Segment("review"),
                                   Segment("fix", started_at=NOW)], {"minor": 1}), [reconcile.Review(KEY, 2)])
        self.assertEqual(self.plan(item, [Segment("review", started_at=NOW - timedelta(hours=4),
                                           ended_at=NOW - timedelta(hours=3)),
                                   Segment("fix", 3), Segment("review", 3, started_at=NOW)],
                                   {"minor": 1}), [reconcile.Escalate(KEY, "retry limit reached for fix")])

    def test_shipped_workflow_reviews_all_three_fix_rounds_before_escalating(self):
        path = Path(__file__).resolve().parent.parent / "assets" / "policy.toml"
        self.p = policy.parse(tomllib.loads(path.read_text()))
        item = replace(Item(), stage="review", review_state="none", merge_state="ready")
        history = [Segment(stage, started_at=NOW - timedelta(hours=6 - i))
                   for i, stage in enumerate(("plan", "implement", "review"))]
        for attempt in range(1, 4):
            with self.subTest(attempt=attempt):
                self.assertEqual(self.plan(item, history, {"minor": 1}),
                                 [reconcile.Dispatch(KEY, "fix", "implementer", attempt)])
                start = NOW - timedelta(minutes=10 - 2 * attempt)
                history.append(Segment("fix", attempt, started_at=start, ended_at=start + timedelta(seconds=30)))
                self.assertEqual(self.plan(replace(item, stage="fix"), history, {"minor": 1}),
                                 [reconcile.Review(KEY, attempt + 1)])
                start += timedelta(minutes=1)
                history.append(Segment("review", attempt + 1, started_at=start, ended_at=start + timedelta(seconds=30)))
        self.assertEqual(self.plan(item, history, {"minor": 1}),
                         [reconcile.Escalate(KEY, "retry limit reached for fix")])
        history.append(Segment("architect", started_at=history[-1].started_at, ended_at=NOW))
        self.assertEqual(self.plan(item, history, {}), [reconcile.Merge(KEY)])

    def test_review_edge_counts_every_repair_attempt_against_the_retry_limit(self):
        self.p.stages["repair"] = policy.Place("repair", "builder")
        self.p.workflows["standard"] = replace(self.p.workflows["standard"], edges=(
            policy.Edge("implement", "review"), policy.Edge("review", "repair"),
            policy.Edge("repair", "review"), policy.Edge("review", "merge")))
        for attempts in range(4):
            for stage in ("review", "repair"):
                with self.subTest(attempts=attempts, stage=stage):
                    history = ([Segment("review", started_at=NOW - timedelta(hours=4),
                                        ended_at=NOW - timedelta(hours=3)),
                                Segment("repair", attempts)] if attempts else []) + [Segment("review", started_at=NOW)]
                    expected = (reconcile.Dispatch(KEY, "repair", "builder", attempts + 1) if attempts < 3
                                else reconcile.Escalate(KEY, "retry limit reached for repair"))
                    self.assertEqual(self.plan(replace(Item(), stage=stage), history, {"minor": 1}), [expected])
        self.assertEqual(self.plan(replace(Item(), stage="repair"),
                                   [Segment("review", started_at=NOW - timedelta(hours=4),
                                            ended_at=NOW - timedelta(hours=3)),
                                    Segment("repair", 3, outcome="failed")]),
                         [reconcile.Escalate(KEY, "retry limit reached for repair")])
        self.assertEqual(self.plan(replace(Item(), stage="repair"), [Segment("repair", 3)]),
                         [reconcile.Review(KEY, 1)])

    def test_fix_without_a_review_edge_gets_an_initial_attempt(self):
        self.p.stages["fix"] = policy.Place("fix", "builder", {"retry_limit": 0})
        self.p.workflows["standard"] = policy.Workflow("standard", "fix", (policy.Edge("fix", "merge"),))
        item = replace(Item(), stage="fix")
        self.assertEqual(self.plan(item), [reconcile.Dispatch(KEY, "fix", "builder", 1)])
        self.assertEqual(self.plan(item, [Segment("fix", outcome="failed")]),
                         [reconcile.Escalate(KEY, "retry limit reached for fix")])

    def test_retry_limit_escalates_before_a_fifth_attempt_but_allows_its_review(self):
        for stage, expected in (("review", reconcile.Escalate(KEY, "retry limit reached for implement")),
                                ("implement", reconcile.Review(KEY, 5))):
            with self.subTest(stage=stage):
                history = [Segment("implement", started_at=NOW - timedelta(hours=6),
                                   ended_at=NOW - timedelta(hours=5)),
                           Segment("review", started_at=NOW - timedelta(hours=4),
                                   ended_at=NOW - timedelta(hours=3)),
                           Segment("implement", 4), Segment("review", 4)]
                history[-1 if stage == "review" else -2] = Segment(stage, 4, started_at=NOW)
                item = replace(Item(), stage=stage, review_state="changes_requested")
                self.assertEqual(self.plan(item, history, {"critical": 1}), [expected])

    def test_retry_limit_is_resolved_for_the_target_persona_stage_and_room(self):
        self.p.personas["builder"] = replace(self.p.personas["builder"], policy={"retry_limit": 0})
        self.assertEqual(self.plan(), [reconcile.Dispatch(KEY, "implement", "builder", 1)])
        self.assertIsInstance(self.plan(history=[Segment("implement", outcome="failed")])[0], reconcile.Escalate)
        self.p.stages["implement"] = policy.Place("implement", "builder", {"retry_limit": 1})
        self.assertEqual(self.plan(), [reconcile.Dispatch(KEY, "implement", "builder", 1)])
        self.assertEqual(self.plan(history=[Segment("implement", outcome="failed")]),
                         [reconcile.Dispatch(KEY, "implement", "builder", 2)])
        item = replace(Item(), stage="review", review_state="changes_requested")
        self.assertEqual(self.plan(item, [Segment("implement", 4), Segment("review", 4, started_at=NOW)],
                                   {"minor": 1}, rooms={KEY: "lab"}),
                         [reconcile.Dispatch(KEY, "implement", "builder", 5)])

    def test_policy_satisfied_work_merges_without_an_interrupt_or_slot(self):
        item = replace(Item(), stage="review", review_state="approved", merge_state="ready")
        self.assertEqual(self.plan(item, [Segment("review")], {}, slots=0), [reconcile.Merge(KEY)])

    def test_pending_decisions_block_merge_but_answered_decisions_do_not(self):
        item = replace(Item(), stage="merge", merge_state="ready")
        for answer in (None, "", "   ", "Split the parser"):
            with self.subTest(answer=answer):
                actions = self.plan(item, [Segment("review")], {}, decisions={KEY: [Decision(answer)]})
                self.assertEqual(actions, [reconcile.Merge(KEY)] if answer and answer.strip() else
                                 [reconcile.Wait(KEY, "decision pending")])

    def test_a_later_answer_resolves_an_earlier_pending_row_for_the_same_question(self):
        item = replace(Item(), stage="merge", merge_state="ready")
        answered = [Decision(None), Decision("Split it")]
        self.assertEqual(self.plan(item, [Segment("review")], {}, decisions={KEY: answered}), [reconcile.Merge(KEY)])
        other = [Decision("Split it"), Decision(None, question="Rename it?")]
        self.assertEqual(self.plan(item, [Segment("review")], {}, decisions={KEY: other}),
                         [reconcile.Wait(KEY, "decision pending")])

    def test_a_review_that_started_before_the_work_ended_does_not_count(self):
        item = replace(Item(), stage="merge", merge_state="ready")
        implement = Segment("implement", started_at=NOW - timedelta(hours=3), ended_at=NOW - timedelta(minutes=30))
        review = Segment("review", started_at=NOW - timedelta(hours=1), ended_at=NOW)
        self.assertNotIn(reconcile.Merge(KEY), self.plan(item, [implement, review], {}))

    def test_required_architect_consultation_must_be_done_for_the_current_work(self):
        self.p.stages["review"] = policy.Place("review", "critic", {"architect": True})
        item = replace(Item(), stage="merge", merge_state="ready")
        implement = Segment("implement", started_at=NOW - timedelta(hours=3))
        review = Segment("review", started_at=NOW - timedelta(hours=1), ended_at=NOW)
        for architect in (None, Segment("architect", outcome="failed"),
                          Segment("architect", ended_at=None),
                          Segment("architect", started_at=NOW - timedelta(hours=4)),
                          # overlaps implement (ends NOW-1h): it could not see the final work
                          Segment("architect", started_at=NOW - timedelta(hours=2), ended_at=NOW)):
            with self.subTest(architect=architect):
                history = [implement, review, *([architect] if architect else [])]
                self.assertNotIn(reconcile.Merge(KEY), self.plan(item, history, {}))
        for started_at in (NOW - timedelta(hours=1), NOW):
            with self.subTest(started_at=started_at):
                architect = Segment("architect", started_at=started_at, ended_at=NOW)
                self.assertEqual(self.plan(item, [implement, review, architect], {}), [reconcile.Merge(KEY)])

    def test_implement_stage_architect_policy_requires_consultation(self):
        self.p.stages["implement"] = policy.Place("implement", "builder", {"architect": True})
        review = Segment("review", started_at=NOW - timedelta(minutes=30), ended_at=NOW)
        for stage in ("review", "merge"):
            with self.subTest(stage=stage):
                item = replace(Item(), stage=stage, merge_state="ready")
                self.assertEqual(self.plan(item, [review], {}), [reconcile.Merge(KEY)])
                history = [Segment("implement"), review]
                self.assertEqual(self.plan(item, history, {}),
                                 [reconcile.Wait(KEY, "architect consultation is required")])
                self.assertEqual(self.plan(item, [*history, Segment("architect", started_at=NOW, ended_at=NOW)], {}),
                                 [reconcile.Merge(KEY)])

    def test_implementer_persona_architect_policy_requires_consultation(self):
        self.p.personas["builder"] = replace(self.p.personas["builder"], policy={"architect": True})
        review = Segment("review", started_at=NOW - timedelta(minutes=30), ended_at=NOW)
        for stage in ("review", "merge"):
            with self.subTest(stage=stage):
                item = replace(Item(), stage=stage, merge_state="ready")
                self.assertEqual(self.plan(item, [review], {}), [reconcile.Merge(KEY)])
                history = [Segment("implement"), review]
                self.assertEqual(self.plan(item, history, {}),
                                 [reconcile.Wait(KEY, "architect consultation is required")])
                self.assertEqual(self.plan(item, [*history, Segment("architect", started_at=NOW, ended_at=NOW)], {}),
                                 [reconcile.Merge(KEY)])

    def test_an_architect_workflow_stage_follows_its_edge_after_a_done_segment(self):
        self.p = policy.parse({
            "default_workflow": "designed",
            "personas": {"builder": {"role": "implements", "model": "example"},
                         "critic": {"role": "reviews", "model": "example"}},
            "stages": {"architect": {"persona": "critic"}, "implement": {"persona": "builder"}},
            "workflows": {"designed": {"start": "architect", "edges": [{"from": "architect", "to": "implement"}]}},
        })
        item = replace(Item(), stage="architect")
        self.assertEqual(self.plan(item, [Segment("architect")], {}), [reconcile.Dispatch(KEY, "implement", "builder", 1)])

    def test_an_architect_workflow_stage_after_review_is_reviewed_again(self):
        self.p = policy.parse({
            "default_workflow": "designed",
            "personas": {"builder": {"role": "implements", "model": "example"},
                         "critic": {"role": "reviews", "model": "example"}},
            "stages": {"implement": {"persona": "builder"}, "review": {"persona": "critic"},
                       "architect": {"persona": "critic"}, "merge": {}},
            "workflows": {"designed": {"start": "implement", "edges": [
                {"from": "implement", "to": "review"}, {"from": "review", "to": "architect"},
                {"from": "architect", "to": "merge", "gate": "merge"}]}},
        })
        item = replace(Item(), stage="merge", review_state="none", merge_state="ready")
        history = [Segment("implement", started_at=NOW - timedelta(hours=6), ended_at=NOW - timedelta(hours=5)),
                   Segment("review", started_at=NOW - timedelta(hours=4), ended_at=NOW - timedelta(hours=3)),
                   Segment("architect")]
        self.assertEqual(self.plan(item, history, {}), [reconcile.Review(KEY, 2)])

    def test_review_architect_merge_reaches_merge_after_the_post_architect_review(self):
        self.p.stages["architect"] = policy.Place("architect", "builder")
        self.p.workflows["standard"] = policy.Workflow("standard", "review", (
            policy.Edge("review", "architect"), policy.Edge("architect", "merge", "merge")))
        item = replace(Item(), stage="review", review_state="none", merge_state="ready")
        self.assertEqual(self.plan(item), [reconcile.Review(KEY, 1)])
        history = [Segment("review", started_at=NOW - timedelta(hours=4),
                           ended_at=NOW - timedelta(hours=3))]
        self.assertEqual(self.plan(item, history, {"minor": 1}),
                         [reconcile.Dispatch(KEY, "architect", "builder", 1)])
        item = replace(item, stage="architect")
        history.append(Segment("architect"))
        self.assertEqual(self.plan(item, history, {}), [reconcile.Review(KEY, 2)])
        item = replace(item, stage="review")  # The engine records the dispatched review's stage.
        history.append(Segment("review", 2, started_at=NOW - timedelta(minutes=30), ended_at=NOW))
        for stage in ("review", "merge"):
            for order in (history, history[::-1]):
                with self.subTest(stage=stage, history=order):
                    self.assertEqual(self.plan(replace(item, stage=stage), order, {}, slots=0),
                                     [reconcile.Merge(KEY)])
        self.p.stages["architect"] = policy.Place("architect", "builder", {"zach_gates": ("merge",)})
        self.assertEqual(self.plan(item, history, {}),
                         [reconcile.Ask(KEY, "merge", "gate requires Zach's approval")])

    def test_architect_requirement_obeys_room_policy(self):
        self.p.stages["review"] = policy.Place("review", "critic", {"architect": True})
        self.p.rooms["lab"] = policy.Place("lab", policy={"architect": False})
        item = replace(Item(), stage="merge", merge_state="ready")
        self.assertEqual(self.plan(item, [Segment("review")], {}, rooms={KEY: "lab"}), [reconcile.Merge(KEY)])

    def test_work_that_ended_after_the_review_started_was_not_reviewed(self):
        # Overlap: the segment started before the review but ended after it began, so the review never saw it.
        item = replace(Item(), stage="review", review_state="approved", merge_state="ready")
        history = [Segment("implement", started_at=NOW - timedelta(hours=3), ended_at=NOW - timedelta(minutes=30)),
                   Segment("review", started_at=NOW - timedelta(hours=1), ended_at=NOW - timedelta(minutes=10))]
        self.assertEqual(self.plan(item, history, {}), [reconcile.Review(KEY, 2)])
        history[0] = replace(history[0], ended_at=history[1].started_at)
        self.assertEqual(self.plan(item, history, {}), [reconcile.Merge(KEY)])

    def test_completed_review_without_forge_approval_merges(self):
        history = [Segment("implement"), Segment("review", started_at=NOW - timedelta(minutes=30), ended_at=NOW)]
        for stage in ("review", "merge"):
            for order in (history, history[::-1]):
                with self.subTest(stage=stage, history=order):
                    item = replace(Item(), stage=stage, review_state="none", merge_state="ready")
                    self.assertEqual(self.plan(item, order, {}, slots=0), [reconcile.Merge(KEY)])

    def test_work_completed_after_review_requires_another_review_even_with_forge_approval(self):
        item = replace(Item(), stage="merge", review_state="approved", merge_state="ready")
        for stage in ("plan", "implement", "fix"):
            history = [Segment("review"), Segment(stage, started_at=NOW - timedelta(minutes=30), ended_at=NOW)]
            for order in (history, history[::-1]):
                with self.subTest(stage=stage, history=order):
                    self.assertEqual(self.plan(item, order, {}), [reconcile.Review(KEY, 2)])

    def test_forge_changes_requested_waits_even_after_a_clean_review(self):
        for stage in ("review", "merge"):
            with self.subTest(stage=stage):
                item = replace(Item(), stage=stage, review_state="changes_requested", merge_state="ready")
                self.assertEqual(self.plan(item, [Segment("review")], {}),
                                 [reconcile.Wait(KEY, "changes requested on the forge")])

    def test_a_failed_review_is_not_review_evidence(self):
        item = replace(Item(), stage="merge", merge_state="ready")
        self.assertEqual(self.plan(item, [Segment("review", outcome="failed")], {}), [reconcile.Review(KEY, 2)])

    def test_review_evidence_requires_a_done_outcome(self):
        for outcome in (None, "", "human_review", "findings", "unknown"):
            for stage in ("review", "merge"):
                with self.subTest(outcome=outcome, stage=stage):
                    item = replace(Item(), stage=stage, review_state="approved", merge_state="ready")
                    self.assertEqual(self.plan(item, [Segment("review", outcome=outcome)], {}),
                                     [reconcile.Review(KEY, 2)])

    def test_merge_requires_ready_state_completed_review_and_known_findings(self):
        item = replace(Item(), stage="merge", review_state="approved", merge_state="ready")
        history = [Segment("review")]
        self.assertEqual(self.plan(replace(item, merge_state="blocked"), history, {}),
                         [reconcile.Wait(KEY, "merge is not ready")])
        self.assertEqual(self.plan(item, history), [reconcile.Wait(KEY, "review findings are unknown")])
        self.assertEqual(self.plan(item, findings={}), [reconcile.Review(KEY, 1)])
        self.assertEqual(self.plan(item, history, {"critical": 1}), [reconcile.Wait(KEY, "blocking review findings")])

    def test_merge_gate_is_rechecked_for_items_already_at_merge(self):
        self.p.stages["review"] = policy.Place("review", "critic", {"zach_gates": ("merge",)})
        item = replace(Item(), stage="merge", review_state="none", merge_state="ready")
        self.assertEqual(self.plan(item, [Segment("review")], {}),
                         [reconcile.Ask(KEY, "merge", "gate requires Zach's approval")])
        self.p.stages["review"] = policy.Place("review", "critic", {"zach_gates": ("merge",),
                                         "approvals": (policy.Approval("merge", NOW + timedelta(hours=1)),)})
        self.assertEqual(self.plan(item, [Segment("review")], {}), [reconcile.Merge(KEY)])

    def test_workflow_without_review_keeps_its_forge_review_requirement(self):
        self.p.workflows["standard"] = replace(self.p.workflows["standard"], edges=(policy.Edge("implement", "merge"),))
        for state, expected in (("approved", reconcile.Merge(KEY)),
                                ("none", reconcile.Wait(KEY, "review has not run"))):
            with self.subTest(state=state):
                item = replace(Item(), stage="merge", review_state=state, merge_state="ready")
                self.assertEqual(self.plan(item, [Segment("implement")], {}), [expected])

    def test_start_merge_without_edges_merges(self):
        self.p = policy.parse({
            "default_workflow": "merge_only",
            "stages": {"merge": {}},
            "workflows": {"merge_only": {"start": "merge", "edges": []}},
        })
        for stage in ("", "merge"):
            with self.subTest(stage=stage):
                item = replace(Item(), stage=stage, review_state="approved", merge_state="ready")
                self.assertEqual(self.plan(item, findings={}, slots=0), [reconcile.Merge(KEY)])

    def test_merge_with_incoming_edges_without_a_traversed_edge_waits(self):
        self.p.workflows["standard"] = replace(self.p.workflows["standard"], edges=(policy.Edge("implement", "merge"),))
        item = replace(Item(), stage="merge", review_state="approved", merge_state="ready")
        self.assertEqual(self.plan(item, findings={}), [reconcile.Wait(KEY, "no unique incoming merge edge")])

    def test_merge_rechecks_only_the_route_taken_into_merge(self):
        self.p.stages["plan"] = policy.Place("plan", "builder", {"zach_gates": ("merge",)})
        self.p.workflows["standard"] = replace(self.p.workflows["standard"], edges=(
            policy.Edge("implement", "plan"), policy.Edge("implement", "merge"),
            policy.Edge("plan", "merge", "merge")))
        item = replace(Item(), stage="merge", review_state="approved", merge_state="ready")
        history = [Segment("plan"), Segment("implement", started_at=NOW - timedelta(minutes=30), ended_at=NOW)]
        for order in (history, history[::-1]):
            with self.subTest(history=order):
                self.assertEqual(self.plan(item, order, {}), [reconcile.Merge(KEY)])

    def test_merge_waits_when_the_traversed_edge_cannot_be_determined(self):
        item = replace(Item(), stage="merge", review_state="approved", merge_state="ready")
        self.p.stages["plan"] = policy.Place("plan", "builder")
        history = [Segment("implement"), Segment("review", started_at=NOW - timedelta(minutes=30), ended_at=NOW)]
        self.p.workflows["standard"] = replace(self.p.workflows["standard"], edges=(
            policy.Edge("implement", "review"), policy.Edge("review", "plan"), policy.Edge("plan", "merge")))
        self.assertEqual(self.plan(item, history, {}), [reconcile.Wait(KEY, "no unique incoming merge edge")])
        self.p.workflows["standard"] = replace(self.p.workflows["standard"], edges=(
            policy.Edge("implement", "review"), policy.Edge("review", "merge"),
            policy.Edge("review", "merge", "merge")))
        self.assertEqual(self.plan(item, [Segment("review")], {}),
                         [reconcile.Wait(KEY, "no unique incoming merge edge")])

    def test_latest_start_tied_between_stages_waits_at_merge(self):
        self.p.workflows["standard"] = replace(self.p.workflows["standard"], edges=(
            policy.Edge("implement", "review"), policy.Edge("implement", "merge"),
            policy.Edge("review", "merge", "merge")))
        self.p.stages["review"] = policy.Place("review", "critic", {"zach_gates": ("merge",)})
        item = replace(Item(), stage="merge", review_state="approved", merge_state="ready")
        for attempt in (1, 2):
            history = [Segment("implement", attempt), Segment("review")]
            for order in (history, history[::-1]):
                with self.subTest(history=order):
                    self.assertEqual(self.plan(item, order, {}),
                                     [reconcile.Wait(KEY, "segments tie at the latest start")])

    def test_latest_start_tied_between_stages_waits_outside_merge(self):
        for attempt in (1, 2):
            history = [Segment("implement", attempt), Segment("review")]
            for order in (history, history[::-1]):
                with self.subTest(history=order):
                    self.assertEqual(self.plan(history=order),
                                     [reconcile.Wait(KEY, "segments tie at the latest start")])

    def test_latest_start_tied_within_a_stage_uses_the_highest_attempt(self):
        history = [Segment("review", started_at=NOW - timedelta(hours=3)),
                   Segment("implement"), Segment("implement", 2, outcome="failed")]
        for order in (history, history[::-1]):
            with self.subTest(history=order):
                self.assertEqual(self.plan(history=order), [reconcile.Dispatch(KEY, "implement", "builder", 3)])

    def test_blocking_threshold_comes_from_review_policy(self):
        self.p.stages["review"] = policy.Place("review", "critic", {"blocking_severity": "important"})
        item = replace(Item(), stage="review", review_state="none", merge_state="ready")
        self.assertEqual(self.plan(item, [Segment("review")], {"minor": 2}), [reconcile.Merge(KEY)])
        for severity in ("critical", "important"):
            with self.subTest(severity=severity):
                self.assertIsInstance(self.plan(item, [Segment("review")], {severity: 1})[0], reconcile.Dispatch)

    def test_ready_merge_state_does_not_skip_implementation(self):
        item = replace(Item(), review_state="approved", merge_state="ready")
        self.assertEqual(self.plan(item, findings={}), [reconcile.Dispatch(KEY, "implement", "builder", 1)])

    def test_human_review_items_wait_without_consuming_a_slot(self):
        self.assertEqual(self.plan(replace(Item(), human_review=True)), [reconcile.Wait(KEY, "human review")])

    def test_retry_releases_an_extra_hold_only_for_the_current_failed_stage(self):
        for stage, action in (("implement", reconcile.Dispatch(KEY, "implement", "builder", 2)),
                              ("review", reconcile.Review(KEY, 2))):
            # The engine owns a hold only after a failed launch, which records `failed`; a quota or
            # unknown exit never carries that ownership, so a person's hold after one is theirs.
            for outcome in ("quota", "unknown"):
                with self.subTest(stage=stage, outcome=outcome):
                    item = replace(Item(), stage=stage, human_review=True)
                    self.assertEqual(self.plan(item, [Segment(stage, outcome=outcome)],
                                               launcher_holds={KEY: {"stage": stage, "attempt": 1}}),
                                     [reconcile.Wait(KEY, "human review")])
            for outcome in ("failed",):
                with self.subTest(stage=stage, outcome=outcome):
                    item = replace(Item(), stage=stage, human_review=True)
                    history = [Segment(stage, outcome=outcome)]
                    self.assertEqual(self.plan(item, history, launcher_holds={KEY: {"stage": stage, "attempt": 1}}), [action])
                    self.assertEqual(self.plan(item, history, launcher_holds={KEY: {"stage": stage, "attempt": 1}}, hold_stages=(stage,)),
                                     [reconcile.Wait(KEY, "human review")])
                    self.assertEqual(self.plan(item, history, launcher_holds={KEY: {"stage": stage, "attempt": 1}}, slots=0),
                                     [reconcile.Wait(KEY, "no free worker slots")])
                    self.assertEqual(self.plan(item, history, launcher_holds={KEY: {"stage": stage, "attempt": 1}}, quota_resets={KEY: NOW + timedelta(hours=1)}),
                                     [reconcile.Wait(KEY, "quota reset pending")])

    def test_unowned_blank_and_stale_holds_wait_for_a_human(self):
        history = [Segment("implement", outcome="quota")]
        for stage, holds in (("implement", {}), ("", {KEY: {"stage": "implement", "attempt": 1}}),
                             ("implement", {KEY: {"stage": "implement", "attempt": 0}})):
            with self.subTest(stage=stage, holds=holds):
                self.assertEqual(self.plan(replace(Item(), stage=stage, human_review=True), history,
                                           launcher_holds=holds), [reconcile.Wait(KEY, "human review")])

    def test_failed_reviews_obey_retry_limit_but_completed_rounds_do_not_spend_it(self):
        self.p = replace(self.p, base=policy.Policy(retry_limit=1))
        item = replace(Item(), stage="review", human_review=True)
        history = [Segment("review", attempt=n, outcome="failed") for n in (1, 2)]
        self.assertEqual(self.plan(item, history, launcher_holds={KEY: {"stage": "review", "attempt": 2}}),
                         [reconcile.Escalate(KEY, "retry limit reached for review")])
        history = [Segment("review", attempt=n) for n in (1, 2)] + [Segment("review", attempt=3, outcome="failed")]
        self.assertEqual(self.plan(replace(item, human_review=False), history), [reconcile.Review(KEY, 4)])

    def test_a_failed_review_followed_by_a_completed_one_does_not_spend_a_later_allowance(self):
        self.p = replace(self.p, base=policy.Policy(retry_limit=1))
        item = replace(Item(), stage="review")
        history = [Segment("review", attempt=1, outcome="failed"), Segment("review", attempt=2),
                   Segment("review", attempt=3, outcome="failed")]
        self.assertEqual(self.plan(item, history), [reconcile.Review(KEY, 4)])
        history.append(Segment("review", attempt=4, outcome="failed"))
        self.assertEqual(self.plan(item, history), [reconcile.Escalate(KEY, "retry limit reached for review")])

    def test_extra_holds_remain_for_completion_human_outcomes_and_other_stages(self):
        item = replace(Item(), human_review=True)
        for history in ([Segment("implement", outcome=outcome)]
                        for outcome in ("done", "human_review", "blocked", None)):
            self.assertEqual(self.plan(item, history), [reconcile.Wait(KEY, "human review")])
        self.assertEqual(self.plan(item, [Segment("review", outcome="failed")]),
                         [reconcile.Wait(KEY, "human review")])

    def test_dispatches_and_parallel_reviews_share_free_slots(self):
        items = [replace(Item(), issue_key=f"github:example/project#{n}", stage=stage)
                 for n, stage in ((1, "review"), (2, "review"), (3, "implement"))]
        for slots in (0, 1, 2, 3):
            with self.subTest(slots=slots):
                actions = reconcile.plan(items, {}, self.p, {}, slots, NOW, {})
                self.assertEqual(sum(isinstance(a, (reconcile.Dispatch, reconcile.Review)) for a in actions), slots)
        actions = reconcile.plan(items, {}, self.p, {}, 2, NOW, {})
        self.assertEqual(actions[:2], [reconcile.Review(items[0].issue_key, 1), reconcile.Review(items[1].issue_key, 1)])

    def test_deterministic_order_uses_creation_time_then_issue_key(self):
        items = [replace(Item(), issue_key=f"github:example/project#{n}") for n in (10, 2, 1)]
        ages = {i.issue_key: NOW - timedelta(days=n) for i, n in zip(items, (3, 1, 2))}
        for order in (items, items[::-1]):
            actions = reconcile.plan(order, {}, self.p, {}, 1, NOW, {}, created_at=ages)
            self.assertEqual(actions[0].issue_key, items[0].issue_key)
            actions = reconcile.plan(order, {}, self.p, {}, 3, NOW, {})
            self.assertEqual([a.issue_key for a in actions], [items[2].issue_key, items[1].issue_key, items[0].issue_key])

    def test_issue_type_selects_workflow_and_empty_stage_uses_its_start(self):
        self.p.workflows["short"] = policy.Workflow("short", "review", (policy.Edge("review", "merge", "merge"),), ("bug",))
        self.assertEqual(reconcile.plan([replace(Item(), stage="")], {}, self.p, {KEY: "bug"}, 1, NOW, {}),
                         [reconcile.Review(KEY, 1)])

    def test_old_stage_segment_does_not_skip_a_new_fix_round(self):
        history = [Segment("implement"), Segment("review", started_at=NOW - timedelta(minutes=30))]
        self.assertEqual(self.plan(history=history), [reconcile.Dispatch(KEY, "implement", "builder", 2)])

    def test_segment_order_and_timestamp_offsets_do_not_change_the_plan(self):
        implement = replace(Segment("implement"), started_at=(NOW - timedelta(hours=2)).isoformat())
        review = replace(Segment("review"), started_at=(NOW - timedelta(hours=1)).astimezone(
                         timezone(timedelta(hours=-6))).isoformat())
        for history in ([implement, review], [review, implement]):
            with self.subTest(history=history):
                self.assertEqual(self.plan(history=history), [reconcile.Dispatch(KEY, "implement", "builder", 2)])

    def test_segment_without_a_start_does_not_count_as_completed_work(self):
        self.assertEqual(self.plan(history=[replace(Segment("implement"), started_at="", ended_at=None)]),
                         [reconcile.Dispatch(KEY, "implement", "builder", 2)])

    def test_missing_findings_after_review_waits_instead_of_assuming_zero(self):
        item = replace(Item(), stage="review", review_state="approved", merge_state="ready")
        self.assertEqual(self.plan(item, [Segment("review")]), [reconcile.Wait(KEY, "review findings are unknown")])

    def test_asks_and_escalations_do_not_need_slots(self):
        self.p = replace(self.p, base=policy.Policy(zach_gates=("plan",)))
        self.assertIsInstance(self.plan(history=[Segment("implement")], slots=0)[0], reconcile.Ask)
        self.p = replace(self.p, base=policy.Policy(retry_limit=0))
        self.assertIsInstance(self.plan(history=[Segment("implement", outcome="failed")], slots=0)[0], reconcile.Escalate)

    def test_failed_segment_retries_current_stage_instead_of_advancing(self):
        self.assertEqual(self.plan(history=[Segment("implement", outcome="failed")]),
                         [reconcile.Dispatch(KEY, "implement", "builder", 2)])

    def test_human_review_segment_retries_current_stage_after_hold_is_cleared(self):
        self.assertEqual(self.plan(history=[Segment("implement", outcome="human_review")]),
                         [reconcile.Dispatch(KEY, "implement", "builder", 2)])

    def test_segment_without_outcome_retries_current_stage_instead_of_advancing(self):
        self.assertEqual(self.plan(history=[Segment("implement", outcome=None)]),
                         [reconcile.Dispatch(KEY, "implement", "builder", 2)])

    def test_unknown_stage_ambiguous_branch_and_terminal_stage_wait(self):
        self.assertIsInstance(self.plan(replace(Item(), stage="unknown"))[0], reconcile.Wait)
        self.p.stages["done"] = policy.Place("done")
        self.assertIsInstance(self.plan(replace(Item(), stage="done"), [Segment("done")])[0], reconcile.Wait)
        self.p.workflows["standard"] = replace(self.p.workflows["standard"], edges=(
            policy.Edge("implement", "review"), policy.Edge("implement", "merge")))
        self.assertIsInstance(self.plan(history=[Segment("implement")])[0], reconcile.Wait)

    def test_actions_are_frozen_and_planning_does_not_mutate_inputs(self):
        items, history, counts = [Item()], {KEY: [Segment("implement")]}, {}
        before = (items[:], {KEY: history[KEY][:]}, counts.copy())
        a = reconcile.plan(items, history, self.p, {}, 1, NOW, counts)
        self.assertEqual(a, reconcile.plan(items, history, self.p, {}, 1, NOW, counts))
        self.assertEqual((items, history, counts), before)
        with self.assertRaises(FrozenInstanceError):
            a[0].issue_key = "changed"

    def test_naive_clock_and_negative_slots_are_refused(self):
        for slots, now in ((-1, NOW), (1, NOW.replace(tzinfo=None))):
            with self.subTest(slots=slots, now=now), self.assertRaises(ValueError):
                reconcile.plan([Item()], {}, self.p, {}, slots, now, {})

    def test_an_item_that_waits_for_a_slot_claims_nothing_for_the_next(self):
        import readiness
        keys = [f"github:example/project#{n}" for n in (1, 2, 3)]
        items = [replace(Item(), issue_key=key) for key in keys]
        claims = (readiness.Claim(keys[1], "src/a.py"), readiness.Claim(keys[2], "src/a.py"))
        got = reconcile.plan(items, {}, self.p, {}, 1, NOW, {}, facts=reconcile.Facts(claims=claims, blockers=()))
        self.assertEqual([type(a).__name__ for a in got], ["Dispatch", "Wait", "Wait"])
        self.assertEqual([a.reason for a in got[1:]], ["no free worker slots"] * 2)

    def test_unknown_blockers_wait_with_their_reason(self):
        facts = reconcile.Facts(blockers=None, blockers_error="auth required")
        self.assertEqual(self.plan(facts=facts),
                         [reconcile.Wait(KEY, "dependency blockers unknown: auth required")])

    def test_a_running_segment_whose_worker_is_gone_is_closed_instead_of_suppressed(self):
        import readiness
        other = "github:example/project#2"
        open_seg = Segment("implement", ended_at=None, outcome=None)
        gone = reconcile.Facts(alive=(((KEY, "implement", 1), False),))
        self.assertEqual(self.plan(history=[open_seg], facts=gone),
                         [reconcile.Close(KEY, "implement", 1, "failed", "worker gone")])
        live = reconcile.Facts(alive=(((KEY, "implement", 1), True),))
        self.assertEqual(self.plan(history=[open_seg], facts=live), [])
        stalled = reconcile.Facts(alive=(((KEY, "implement", 1), True),),
                                  progress=((KEY, NOW - timedelta(hours=2)),),
                                  stall_after=timedelta(minutes=45))
        self.assertEqual(self.plan(history=[replace(open_seg, started_at=NOW - timedelta(hours=2))], facts=stalled),
                         [reconcile.Wait(KEY, "stalled")])
        claims = (readiness.Claim(KEY, "src/a.py"), readiness.Claim(other, "src/a.py"))
        conflict = reconcile.Facts(claims=claims, blockers=(), alive=(((other, "implement", 1), True),))
        actions = reconcile.plan([Item(), replace(Item(), issue_key=other)],
                                 {KEY: (), other: [open_seg]}, self.p, {}, 4, NOW, {}, facts=conflict)
        self.assertEqual(actions, [reconcile.Wait(KEY, f"file ownership conflict with {other}")])

    def test_a_finished_read_only_run_that_left_changes_waits(self):
        facts = reconcile.Facts(read_only=(KEY,), changed=((KEY, True),))
        self.assertEqual(self.plan(history=[Segment("implement")], facts=facts),
                         [reconcile.Wait(KEY, "read-only run left changes")])

    def test_unknown_finished_tree_does_not_reinstate_a_released_read_only_hold(self):
        facts = reconcile.Facts(read_only=(KEY,), changed=((KEY, None),), blockers=())
        self.assertEqual(self.plan(history=[Segment("implement")], facts=facts), [reconcile.Review(KEY, 1)])


class SuiteHoldTest(unittest.TestCase):
    def test_opt_in_holds_unknown_zero_passed_or_nonzero_failure_counts(self):
        green = {"passed": 4, "failed": 0, "skipped": 0, "xfailed": 0}
        self.assertFalse(reconcile.suite_holds(green, require_green=True))
        self.assertFalse(reconcile.suite_holds({**green, "passed": 0}, require_green=False))
        self.assertFalse(reconcile.suite_holds({}, require_green=False))
        for counts in ({**green, "passed": 0}, {**green, "failed": 1}, {**green, "skipped": 2},
                       {**green, "xfailed": 1}, {**green, "passed": None}, {}):
            with self.subTest(counts=counts):
                self.assertTrue(reconcile.suite_holds(counts, require_green=True))


if __name__ == "__main__":
    unittest.main()
