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
        self.assertEqual(self.plan(item, [Segment("fix", 3), Segment("review", 3, started_at=NOW)],
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
        self.assertEqual(self.plan(item, history, {}), [reconcile.Merge(KEY)])

    def test_retry_limit_escalates_before_a_fourth_rework_but_allows_its_review(self):
        for stage, expected in (("review", reconcile.Escalate(KEY, "retry limit reached for implement")),
                                ("implement", reconcile.Review(KEY, 5))):
            with self.subTest(stage=stage):
                history = [Segment("implement", 4), Segment("review", 4)]
                history[-1 if stage == "review" else 0] = Segment(stage, 4, started_at=NOW)
                item = replace(Item(), stage=stage, review_state="changes_requested")
                self.assertEqual(self.plan(item, history, {"critical": 1}), [expected])

    def test_retry_limit_is_resolved_for_the_target_persona_stage_and_room(self):
        self.p.personas["builder"] = replace(self.p.personas["builder"], policy={"retry_limit": 0})
        self.assertEqual(self.plan(), [reconcile.Dispatch(KEY, "implement", "builder", 1)])
        self.assertIsInstance(self.plan(history=[Segment("implement", outcome="failed")])[0], reconcile.Escalate)
        self.p.stages["implement"] = policy.Place("implement", "builder", {"retry_limit": 1})
        self.assertEqual(self.plan(history=[Segment("implement", outcome="failed")]),
                         [reconcile.Dispatch(KEY, "implement", "builder", 2)])
        item = replace(Item(), stage="review", review_state="changes_requested")
        self.assertEqual(self.plan(item, [Segment("implement", 4), Segment("review", 4, started_at=NOW)],
                                   {"minor": 1}, rooms={KEY: "lab"}),
                         [reconcile.Dispatch(KEY, "implement", "builder", 5)])

    def test_policy_satisfied_work_merges_without_an_interrupt_or_slot(self):
        item = replace(Item(), stage="review", review_state="approved", merge_state="ready")
        self.assertEqual(self.plan(item, [Segment("review")], {}, slots=0), [reconcile.Merge(KEY)])

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


if __name__ == "__main__":
    unittest.main()
