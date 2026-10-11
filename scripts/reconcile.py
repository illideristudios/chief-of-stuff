"""Plan work from snapshots and policy. Use Cases; no I/O or execution."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Mapping, Protocol, Sequence

from policy import Edge, Policies, Policy, SEVERITIES, Workflow


class WorkItem(Protocol):
    issue_key: str
    stage: str
    forge_state: str
    review_state: str
    merge_state: str
    human_review: bool


class Segment(Protocol):
    stage: str
    attempt: int
    started_at: datetime | str
    ended_at: datetime | str | None
    outcome: str | None


@dataclass(frozen=True)
class Dispatch:
    issue_key: str
    stage: str
    persona: str | None
    attempt: int


@dataclass(frozen=True)
class Review:
    issue_key: str
    attempt: int


@dataclass(frozen=True)
class Merge:
    issue_key: str


@dataclass(frozen=True)
class Ask:
    issue_key: str
    gate: str
    reason: str


@dataclass(frozen=True)
class Escalate:
    issue_key: str
    reason: str


@dataclass(frozen=True)
class Wait:
    issue_key: str
    reason: str


Action = Dispatch | Review | Merge | Ask | Escalate | Wait


def _time(value: datetime | str) -> datetime:
    when = datetime.fromisoformat(value) if isinstance(value, str) else value
    if when.utcoffset() is None:
        raise ValueError("timestamps must include a timezone")
    return when


def _order(item: WorkItem, created_at: Mapping[str, datetime]) -> tuple:
    namespace, _, number = item.issue_key.rpartition("#")
    return (item.issue_key not in created_at,
            _time(created_at[item.issue_key]) if item.issue_key in created_at else datetime.min.replace(tzinfo=timezone.utc),
            namespace, int(number) if number.isdecimal() else 0, item.issue_key)


def _policy(policies: Policies, stage: str, room: str | None) -> Policy:
    return policies.resolve(policies.stages[stage].persona, stage, room)


def _gate(key: str, edge: Edge, policies: Policies, room: str | None, now: datetime) -> Ask | None:
    resolved = _policy(policies, edge.source, room)
    if (edge.gate in resolved.zach_gates
            and not any(a.gate == edge.gate and a.expires > now for a in resolved.approvals)):
        return Ask(key, edge.gate, "gate requires Zach's approval")
    return None


def _blocking(counts: Mapping[str, int], resolved: Policy) -> bool:
    blocking = SEVERITIES[:SEVERITIES.index(resolved.blocking_severity) + 1]
    return any(counts.get(severity, 0) > 0 for severity in blocking)


def _work(key: str, stage: str, workflow: Workflow, history: Sequence[Segment], policies: Policies, room: str | None) -> Action:
    attempt = max((s.attempt for s in history if s.stage == stage), default=0) + 1
    if stage == "review":
        return Review(key, attempt)
    # Every attempt in a review's rework stage counts; other stages get an initial attempt before their retries.
    rounds = attempt if any(e.target == stage for e in workflow.next("review")) else attempt - 1
    if rounds > _policy(policies, stage, room).retry_limit:
        return Escalate(key, f"retry limit reached for {stage}")
    return Dispatch(key, stage, policies.stages[stage].persona, attempt)


def _merge(item: WorkItem, workflow: Workflow, history: Sequence[Segment], policies: Policies,
           room: str | None, now: datetime, counts: Mapping[str, int] | None, edges: Sequence[Edge]) -> Action:
    key = item.issue_key
    stages = {workflow.start} | {e.target for e in workflow.edges}
    if item.review_state == "changes_requested":
        return Wait(key, "changes requested on the forge")
    if "review" in stages:
        completed = tuple(s for s in history if s.started_at and s.ended_at)
        review = max((s for s in completed if s.stage == "review" and s.outcome != "failed"),
                     key=lambda s: _time(s.started_at), default=None)
        if review is None or any(s.stage not in ("review", "merge")
                                 and _time(s.started_at) >= _time(review.started_at) for s in completed):
            return _work(key, "review", workflow, history, policies, room)
    elif item.review_state != "approved":
        return Wait(key, "review has not run")
    if counts is None:
        return Wait(key, "review findings are unknown")
    review_stage = "review" if "review" in stages else "merge"
    if _blocking(counts, _policy(policies, review_stage, room)):
        return Wait(key, "blocking review findings")
    if item.merge_state != "ready":
        return Wait(key, "merge is not ready")
    if workflow.start == "merge" and not any(e.target == "merge" for e in workflow.edges):
        return Merge(key)
    if len(edges) != 1:
        return Wait(key, "no unique incoming merge edge")
    return _gate(key, edges[0], policies, room, now) or Merge(key)


def _next(workflow: Workflow, stage: str, blocking: bool) -> Edge | None:
    edges = workflow.next(stage)
    if stage == "review":
        edges = tuple(e for e in edges if (e.target != "merge") == blocking)
    return edges[0] if len(edges) == 1 else None


def _item(item: WorkItem, history: Sequence[Segment], policies: Policies, issue_type: str | None,
          room: str | None, now: datetime, counts: Mapping[str, int] | None) -> Action | None:
    key = item.issue_key
    if item.forge_state == "closed" or any(s.started_at and not s.ended_at for s in history):
        return None
    if item.human_review:
        return Wait(key, "human review")
    workflow = policies.workflow_for(issue_type)
    stage = item.stage or workflow.start
    stages = {workflow.start} | {e.target for e in workflow.edges}
    if stage not in stages:
        return Wait(key, "stage is outside the workflow")
    completed = tuple(s for s in history if s.started_at and s.ended_at and (stage != "merge" or s.stage != "merge"))
    latest = max(completed, key=lambda s: (_time(s.started_at), s.attempt), default=None)
    if latest and len({s.stage for s in completed if _time(s.started_at) == _time(latest.started_at)}) > 1:
        return Wait(key, "segments tie at the latest start")
    if stage == "merge":
        incoming = tuple(e for e in workflow.edges if e.target == stage and latest and e.source == latest.stage)
        return _merge(item, workflow, history, policies, room, now, counts, incoming)
    # A segment from an earlier visit to this stage must not skip the next fix round.
    if latest is None or latest.stage != stage:
        return _work(key, stage, workflow, history, policies, room)
    if latest.outcome == "failed":
        return _work(key, stage, workflow, history, policies, room)
    blocking = False
    if stage == "review":
        if counts is None:
            return Wait(key, "review findings are unknown")
        blocking = _blocking(counts, _policy(policies, stage, room))
    edge = _next(workflow, stage, blocking)
    if edge is None:
        return Wait(key, "no unique workflow edge")
    if edge.target == "merge":
        return _merge(item, workflow, history, policies, room, now, counts, (edge,))
    action = _work(key, edge.target, workflow, history, policies, room)
    if isinstance(action, Escalate):
        return action
    return _gate(key, edge, policies, room, now) or action


def plan(items: Sequence[WorkItem], segments: Mapping[str, Sequence[Segment]], policies: Policies,
         issue_types: Mapping[str, str | None], free_slots: int, now: datetime,
         open_findings: Mapping[str, Mapping[str, int]], *, created_at: Mapping[str, datetime] | None = None,
         rooms: Mapping[str, str] | None = None) -> list[Action]:
    """One action per eligible issue, oldest first; dispatch and review each take one slot.

    Findings are complete severity counts from the latest review; an absent item is unknown.
    Gates use their source stage's policy; rework limits use the destination stage's policy, excluding reviews.
    Without creation times, issue numbers order work within each forge namespace.
    """
    _time(now)
    if type(free_slots) is not int or free_slots < 0:
        raise ValueError("free_slots must be a non-negative integer")
    actions = []
    for item in sorted(items, key=lambda item: _order(item, created_at or {})):
        action = _item(item, segments.get(item.issue_key, ()), policies, issue_types.get(item.issue_key),
                       (rooms or {}).get(item.issue_key), now, open_findings.get(item.issue_key))
        if isinstance(action, (Dispatch, Review)):
            if free_slots:
                free_slots -= 1
            else:
                action = Wait(item.issue_key, "no free worker slots")
        if action is not None:
            actions.append(action)
    return actions
