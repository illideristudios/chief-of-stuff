"""Plan work from snapshots and policy. Use Cases; no I/O or execution."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Mapping, Protocol, Sequence

import decision_actions
from model_port import RETRYABLE
from policy import Edge, Policies, Policy, SEVERITIES, Workflow, breach


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


class Decision(Protocol):
    topic: str
    question: str
    answer: str | None


def review_findings(state: Mapping[str, object], history: Sequence[Segment]) -> dict[str, int] | None:
    """Severity counts from the latest completed review, or None when unknown or stale."""
    latest = max((s for s in history if s.stage == "review" and s.ended_at),
                 key=lambda s: (datetime.fromisoformat(str(s.started_at)), s.attempt), default=None)
    record = state.get("review_findings")
    # TODO #639: replace this attempt-keyed resume bridge with structured findings from the latest review.
    # Forge approval and threads have no severity counts; neither means a worker reported zero findings.
    if latest is None or latest.outcome != "done" or not isinstance(record, dict):
        return None
    counts = record.get("counts")
    if (record.get("attempt") != latest.attempt or not isinstance(counts, dict)
            or any(k not in SEVERITIES or type(v) is not int or v < 0 for k, v in counts.items())):
        return None
    return counts


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
    source: str = field(default="", compare=False)
    context: str = field(default="", compare=False)
    stage: str = field(default="", compare=False)
    attempt: int = field(default=0, compare=False)


@dataclass(frozen=True)
class Escalate:
    issue_key: str
    reason: str
    stage: str = field(default="", compare=False)


@dataclass(frozen=True)
class Limit:
    issue_key: str
    kind: str
    reason: str
    stage: str = field(default="", compare=False)


@dataclass(frozen=True)
class Wait:
    issue_key: str
    reason: str


Action = Dispatch | Review | Merge | Ask | Escalate | Limit | Wait


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


def _approved(edge: Edge, policies: Policies, room: str | None, now: datetime) -> bool:
    return any(a.gate == edge.gate and a.expires > now for a in _policy(policies, edge.source, room).approvals)


def passed_gates(workflow: Workflow, policies: Policies, room: str | None, now: datetime,
                 waived: Sequence[str] = ()) -> set[str]:
    """Gates the owner no longer decides: waived, or under a live standing approval on every edge."""
    edges = [e for e in workflow.edges if e.gate]
    return {e.gate for e in edges
            if e.gate in waived or all(_approved(x, policies, room, now) for x in edges if x.gate == e.gate)}


def _gate(key: str, edge: Edge, policies: Policies, room: str | None, now: datetime,
          waived: Sequence[str] = (), decisions: Sequence[Decision] = (),
          latest: Segment | None = None) -> Ask | Wait | None:
    if edge.gate in waived:
        return None
    resolved = _policy(policies, edge.source, room)
    if edge.gate in resolved.zach_gates and not _approved(edge, policies, room, now):
        context = f"{latest.stage}-{latest.attempt}" if latest is not None else ""
        row = next((d for d in reversed(decisions)
                    if d.topic == edge.gate and d.question == "gate requires Zach's approval"), None)
        if row is not None and not decision_actions.is_reversed(row):
            record = getattr(row, "record", None)
            prior = record.get("decision") if isinstance(record, dict) and isinstance(record.get("decision"), dict) else None
            if not decision_actions.retryable_failure(record) and not (isinstance(record, dict) and record.get("restarted")) and (
                    prior is None or prior.get("context", "") == context):
                if (getattr(row, "answer", None) or "").strip() != "approve":
                    return Wait(key, "decision pending")
        return Ask(key, edge.gate, "gate requires Zach's approval", edge.source, context,
                   latest.stage if latest is not None else "", latest.attempt if latest is not None else 0)
    return None


def _blocking(counts: Mapping[str, int], resolved: Policy) -> bool:
    blocking = SEVERITIES[:SEVERITIES.index(resolved.blocking_severity) + 1]
    return any(counts.get(severity, 0) > 0 for severity in blocking)


def _work(key: str, stage: str, workflow: Workflow, history: Sequence[Segment], policies: Policies, room: str | None) -> Action:
    attempt = max((s.attempt for s in history if s.stage == stage), default=0) + 1
    if stage == "review":
        return Review(key, attempt)
    rounds = attempt - 1 - sum(s.stage == stage and s.outcome in RETRYABLE for s in history)
    if any(e.target == stage for e in workflow.next("review")):
        review = min((_time(s.ended_at) for s in history if s.stage == "review" and s.started_at and s.ended_at),
                     default=None)
        if review is not None:
            # Attempts started at or after the first completed review are rework.
            initial = max((s.attempt for s in history if s.stage == stage
                           and (not s.started_at or _time(s.started_at) < review)), default=0)
            rounds = attempt - initial - sum(s.stage == stage and s.outcome in RETRYABLE and s.started_at
                                             and _time(s.started_at) >= review for s in history)
    if rounds > _policy(policies, stage, room).retry_limit:
        return Escalate(key, f"retry limit reached for {stage}", stage=stage)
    return Dispatch(key, stage, policies.stages[stage].persona, attempt)


def _merge(item: WorkItem, workflow: Workflow, history: Sequence[Segment], policies: Policies,
           room: str | None, now: datetime, counts: Mapping[str, int] | None, edges: Sequence[Edge],
           decisions: Sequence[Decision], waived: Sequence[str] = (),
           latest_segment: Segment | None = None) -> Action:
    key = item.issue_key
    stages = {workflow.start} | {e.target for e in workflow.edges}
    if decision_actions.pending_decisions(decisions, tuple(e.gate for e in workflow.edges if e.gate), history, workflow.start):
        return Wait(key, "decision pending")
    if item.review_state == "changes_requested":
        return Wait(key, "changes requested on the forge")
    completed = tuple(s for s in history if s.started_at and s.ended_at)
    # An out-of-band architect consultation changes no code; an architect workflow stage is work to review.
    unreviewed = {"review", "merge"} | ({"architect"} if "architect" not in stages else set())
    if "review" in stages:
        review = max((s for s in completed if s.stage == "review" and s.outcome == "done"),
                     key=lambda s: _time(s.started_at), default=None)
        # Work that ended after the review started was not all reviewed, even if it started first.
        if review is None or any(s.stage not in unreviewed
                                 and _time(s.ended_at) > _time(review.started_at) for s in completed):
            return _work(key, "review", workflow, history, policies, room)
    elif item.review_state != "approved":
        return Wait(key, "review has not run")
    if counts is None:
        return Wait(key, "review findings are unknown")
    review_stage = "review" if "review" in stages else "merge"
    resolved = _policy(policies, review_stage, room)
    if _blocking(counts, resolved):
        return Wait(key, "blocking review findings")
    work_stages = {review_stage} | {s.stage for s in completed if s.stage in policies.stages}
    if any(_policy(policies, stage, room).architect for stage in work_stages):
        architect = max((s for s in completed if s.stage == "architect" and s.outcome == "done"),
                        key=lambda s: _time(s.started_at), default=None)
        if architect is None or any(s.stage not in ("review", "merge", "architect")
                                    and _time(s.ended_at) > _time(architect.started_at) for s in completed):
            return Wait(key, "architect consultation is required")
    if item.merge_state != "ready":
        return Wait(key, "merge is not ready")
    if workflow.start == "merge" and not any(e.target == "merge" for e in workflow.edges):
        return Merge(key)
    if len(edges) != 1:
        return Wait(key, "no unique incoming merge edge")
    return _gate(key, edges[0], policies, room, now, waived, decisions, latest_segment) or Merge(key)


def _next(workflow: Workflow, stage: str, blocking: bool) -> Edge | None:
    edges = workflow.next(stage)
    if stage == "review":
        edges = tuple(e for e in edges if (e.target != "merge") == blocking)
    return edges[0] if len(edges) == 1 else None


def _item(item: WorkItem, history: Sequence[Segment], policies: Policies, issue_type: str | None,
          room: str | None, now: datetime, counts: Mapping[str, int] | None,
          decisions: Sequence[Decision], waived: Sequence[str] = ()) -> Action | None:
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
    if decision_actions.open_reversal(decisions, history, workflow.start):
        return _work(key, workflow.start, workflow, history, policies, room)
    # An architect segment is an out-of-band consultation unless the workflow names architect as a stage.
    completed = tuple(s for s in history if s.started_at and s.ended_at and (s.stage != "architect" or "architect" in stages)
                      and (stage != "merge" or s.stage != "merge"))
    latest = max(completed, key=lambda s: (_time(s.started_at), s.attempt), default=None)
    if latest and len({s.stage for s in completed if _time(s.started_at) == _time(latest.started_at)}) > 1:
        return Wait(key, "segments tie at the latest start")
    return_edge = None
    if latest and latest.stage == "review":
        work = tuple(s for s in completed if s.stage not in ("review", "merge"))
        prior = max(work, key=lambda s: (_time(s.started_at), s.attempt), default=None)
        # The review injected before architect -> merge must return to that same gated route.
        if (prior and prior.stage == "architect" and prior.outcome == "done"
                and len({s.stage for s in work if _time(s.started_at) == _time(prior.started_at)}) == 1):
            edge = _next(workflow, prior.stage, False)
            if edge and edge.target == "merge":
                return_edge = edge
    if stage == "merge":
        incoming = ((return_edge,) if return_edge else
                    tuple(e for e in workflow.edges if e.target == stage and latest and e.source == latest.stage))
        return _merge(item, workflow, history, policies, room, now, counts, incoming, decisions, waived, latest)
    # A segment from an earlier visit to this stage must not skip the next fix round.
    if latest is None or latest.stage != stage:
        return _work(key, stage, workflow, history, policies, room)
    if latest.outcome != "done":
        return _work(key, stage, workflow, history, policies, room)
    blocking = False
    if stage == "review":
        if counts is None:
            return Wait(key, "review findings are unknown")
        blocking = _blocking(counts, _policy(policies, stage, room))
    edge = return_edge if stage == "review" and not blocking and return_edge else _next(workflow, stage, blocking)
    if edge is None:
        return Wait(key, "no unique workflow edge")
    if edge.target == "merge":
        return _merge(item, workflow, history, policies, room, now, counts, (edge,), decisions, waived, latest)
    action = _work(key, edge.target, workflow, history, policies, room)
    if isinstance(action, Escalate):
        return action
    return _gate(key, edge, policies, room, now, waived, decisions, latest) or action


def plan(items: Sequence[WorkItem], segments: Mapping[str, Sequence[Segment]], policies: Policies,
         issue_types: Mapping[str, str | None], free_slots: int, now: datetime,
         open_findings: Mapping[str, Mapping[str, int]], *, created_at: Mapping[str, datetime] | None = None,
         rooms: Mapping[str, str] | None = None,
         decisions: Mapping[str, Sequence[Decision]] | None = None,
         quota_resets: Mapping[str, datetime] | None = None,
         usage: Mapping[str, Mapping[str, float]] | None = None,
         waived_gates: Mapping[str, Sequence[str]] | None = None,
         pending_asks: Mapping[str, bool] | None = None) -> list[Action]:
    """One action per eligible issue, oldest first; dispatch and review each take one slot.

    Findings are complete severity counts from the latest review; an absent item is unknown.
    Gates use their source stage's policy; rework limits use the destination stage's policy, excluding reviews.
    Merge requires answered decisions and, where policy requires it, a done architect segment for the current work.
    Without creation times, issue numbers order work within each forge namespace.
    """
    _time(now)
    if type(free_slots) is not int or free_slots < 0:
        raise ValueError("free_slots must be a non-negative integer")
    actions = []
    for item in sorted(items, key=lambda item: _order(item, created_at or {})):
        reset = (quota_resets or {}).get(item.issue_key)
        room = (rooms or {}).get(item.issue_key)
        action = _item(item, segments.get(item.issue_key, ()), policies, issue_types.get(item.issue_key),
                       room, now, open_findings.get(item.issue_key),
                       (decisions or {}).get(item.issue_key, ()),
                       tuple((waived_gates or {}).get(item.issue_key) or ()))
        if isinstance(action, Merge) and (pending_asks or {}).get(item.issue_key):
            action = Wait(item.issue_key, "decision pending")
        if isinstance(action, (Dispatch, Review, Ask)):
            target = action.source if isinstance(action, Ask) else action.stage if isinstance(action, Dispatch) else "review"
            kind = breach(_policy(policies, target, room), (usage or {}).get(item.issue_key)) if target in policies.stages else None
            if kind:
                action = Limit(item.issue_key, kind, f"reached {kind} limit", stage=target)
            elif reset is not None and _time(reset) > now:
                action = Wait(item.issue_key, "quota reset pending")
            elif (pending_asks or {}).get(item.issue_key):
                action = Wait(item.issue_key, "decision pending")
            elif not isinstance(action, Ask):
                if free_slots:
                    free_slots -= 1
                else:
                    action = Wait(item.issue_key, "no free worker slots")
        if action is not None:
            actions.append(action)
    return actions
