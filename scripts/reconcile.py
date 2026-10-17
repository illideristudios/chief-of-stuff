"""Plan work from snapshots and policy. Use Cases; no I/O or execution."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Mapping, Protocol, Sequence

from model_port import RETRYABLE
from policy import DISPOSITIONS, Edge, Policies, Policy, SEVERITIES, Workflow


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
    persona: str
    reviewed_head: str
    review_round: int
    finding_key: str


class Decision(Protocol):
    topic: str
    question: str
    answer: str | None


def _panel_history(history: Sequence[Segment]) -> list[Segment]:
    reviews = [s for s in history if s.stage == "review"]
    round = max((getattr(s, "review_round", 0) for s in reviews), default=0)
    if round:
        reviews = [s for s in reviews if getattr(s, "review_round", 0) == round]
    else:
        reviews = sorted(reviews, key=lambda s: s.attempt)[-1:]
    # A retry supersedes only that persona's earlier attempt, never another panel member.
    latest = {}
    for segment in sorted(reviews, key=lambda s: s.attempt):
        latest[getattr(segment, "persona", "")] = segment
    return list(latest.values())


def _dispositions(reports: Sequence[object], history: Sequence[Segment], reviews: Sequence[Segment],
                  consults: Mapping[int, str] | None) -> dict[str, str]:
    """The architect's fix/file/keep/discard per finding key, from its latest done consult of this panel."""
    start = min(_time(s.started_at) for s in reviews)
    consult = max((s for s in history if s.stage == "architect" and s.outcome == "done" and s.ended_at
                   and _time(s.started_at) >= start), key=lambda s: _time(s.started_at), default=None)
    matching = [r for r in reports if consult and getattr(r, "stage", None) == "architect"
                and r.attempt == consult.attempt and (consults is None or consults.get(r.attempt) == r.session_id)]
    if not matching or matching[-1].outcome != "done":
        return {}
    return {f["finding_key"]: f["disposition"] for f in matching[-1].findings
            if isinstance(f, dict) and f.get("finding_key") and f.get("disposition") in DISPOSITIONS}


def review_findings(reports: Sequence[object], history: Sequence[Segment], *,
                    sessions: Mapping[int, str] | None = None,
                    consults: Mapping[int, str] | None = None) -> list[dict] | None:
    """Open findings from each panel member's latest structured report; absent/failed is unknown.

    Engine callers bind reports to the launched sessions (`consults` binds the architect's). Finding keys bind
    fixes to one panel round and the finding's content; a later review must confirm the fixes.
    """
    reviews = _panel_history(history)
    if not reviews:
        return None
    findings = []
    for segment in reviews:
        if not segment.ended_at or segment.outcome != "done":
            return None
        matching = [r for r in reports if getattr(r, "stage", None) == "review" and r.attempt == segment.attempt
                    and (sessions is None or sessions.get(r.attempt) == r.session_id)]
        if not matching or matching[-1].outcome != "done":
            return None
        for finding in matching[-1].findings:
            if not isinstance(finding, dict) or finding.get("severity") not in SEVERITIES:
                return None
            round = getattr(segment, "review_round", 0) or segment.attempt
            content = json.dumps([finding.get("severity"), finding.get("title"), finding.get("evidence")])
            findings.append({**finding, "finding_key": f"{round}/{segment.attempt}/{hashlib.sha1(content.encode()).hexdigest()[:8]}"})
    disposed = _dispositions(reports, history, reviews, consults)
    return [{**f, "disposition": disposed[f["finding_key"]]} if f["finding_key"] in disposed else f for f in findings]


@dataclass(frozen=True)
class Dispatch:
    issue_key: str
    stage: str
    persona: str | None
    attempt: int
    finding_key: str = ""
    finding: dict | None = None


@dataclass(frozen=True)
class Review:
    issue_key: str
    attempt: int
    persona: str | None = None
    review_round: int = 0


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
        return Escalate(key, f"retry limit reached for {stage}")
    return Dispatch(key, stage, policies.stages[stage].persona, attempt)


def _merge(item: WorkItem, workflow: Workflow, history: Sequence[Segment], policies: Policies,
           room: str | None, now: datetime, counts: Sequence[dict] | None, edges: Sequence[Edge],
           decisions: Sequence[Decision], head: str | None) -> Action:
    key = item.issue_key
    stages = {workflow.start} | {e.target for e in workflow.edges}
    # Decision rows are append-only, oldest first: the latest row for a question is its state.
    latest = {(d.topic, d.question): d for d in decisions}
    if any(not d.answer or not d.answer.strip() for d in latest.values()):
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
        if not head:
            return Wait(key, "change head is unknown")
        required = _reviewers(policies, room)
        reviewed = {getattr(s, "persona", ""): s for s in _panel_history(history)}
        if any(name not in reviewed or reviewed[name].outcome != "done"
               or not reviewed[name].ended_at or getattr(reviewed[name], "reviewed_head", "") != head
               for name in required):
            return _work(key, "review", workflow, history, policies, room)
    elif item.review_state != "approved":
        return Wait(key, "review has not run")
    if counts is None:
        return Wait(key, "review findings are unknown")
    review_stage = "review" if "review" in stages else "merge"
    if counts:  # already filtered by each reporting persona's effective policy
        return Wait(key, "blocking review findings")
    work_stages = {review_stage} | {s.stage for s in completed if s.stage in policies.stages}
    consult_policies = [_policy(policies, stage, room) for stage in sorted(work_stages)]
    if "review" in stages:
        consult_policies += [policies.resolve(name, "review", room) for name in _reviewers(policies, room)]
    consult = next((p for p in consult_policies if p.architect), None)
    if consult is not None:
        architect = max((s for s in completed if s.stage == "architect" and s.outcome == "done"),
                        key=lambda s: _time(s.started_at), default=None)
        if architect is None or any(s.stage not in ("review", "merge", "architect")
                                    and _time(s.ended_at) > _time(architect.started_at) for s in completed):
            return _consult(key, history, policies, room, consult)
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
          room: str | None, now: datetime, counts: Sequence[dict] | None,
          decisions: Sequence[Decision], head: str | None) -> Action | None:
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
        return _merge(item, workflow, history, policies, room, now, counts, incoming, decisions, head)
    # A segment from an earlier visit to this stage must not skip the next fix round.
    if latest is None or latest.stage != stage:
        return _work(key, stage, workflow, history, policies, room)
    if latest.outcome != "done":
        return _work(key, stage, workflow, history, policies, room)
    blocking = False
    if stage == "review":
        if counts is None:
            return Wait(key, "review findings are unknown")
        blocking = bool(counts)
    edge = return_edge if stage == "review" and not blocking and return_edge else _next(workflow, stage, blocking)
    if edge is None:
        return Wait(key, "no unique workflow edge")
    if edge.target == "merge":
        return _merge(item, workflow, history, policies, room, now, counts, (edge,), decisions, head)
    action = _work(key, edge.target, workflow, history, policies, room)
    if isinstance(action, Escalate):
        return action
    return _gate(key, edge, policies, room, now) or action


def _reviewers(policies: Policies, room: str | None) -> tuple[str, ...]:
    resolved = _policy(policies, "review", room)
    return resolved.reviewers or (policies.stages["review"].persona,)


def _consult(key: str, history: Sequence[Segment], policies: Policies, room: str | None,
             resolved: Policy) -> Action:
    persona = resolved.architect_persona
    if persona is None and "architect" in policies.stages:
        persona = policies.stages["architect"].persona
    if persona is None:
        return Wait(key, "architect consultation is required: no architect persona configured")
    attempt = max((s.attempt for s in history if s.stage == "architect"), default=0) + 1
    failures = sum(s.stage == "architect" and s.outcome not in (None, "done", *RETRYABLE) for s in history)
    if failures > resolved.retry_limit:
        return Escalate(key, "retry limit reached for architect")
    return Dispatch(key, "architect", persona, attempt)


def _panel(key: str, history: Sequence[Segment], policies: Policies, room: str | None,
           head: str | None, *, fresh: bool = False) -> list[Action]:
    if not head:
        return [Wait(key, "change head is unknown")]
    reviews = _panel_history(history)
    round = max((getattr(s, "review_round", 0) for s in history), default=0)
    # Only a launched review pins a head; a headless failure stays in its round so its retries are counted.
    if fresh or not round or any(getattr(s, "reviewed_head", "") not in ("", head) for s in reviews):
        round += 1
        reviews = []
    attempt = max((s.attempt for s in history if s.stage == "review"), default=0)
    running = {getattr(s, "persona", "") for s in history if s.stage == "review" and s.started_at and not s.ended_at}
    actions = []
    for persona in _reviewers(policies, room):
        previous = next((s for s in reviews if getattr(s, "persona", "") == persona), None)
        if persona in running or (previous and previous.outcome == "done"):
            continue
        failures = sum(s.stage == "review" and getattr(s, "review_round", 0) == round
                       and getattr(s, "persona", "") == persona and s.outcome not in RETRYABLE
                       for s in history)
        if failures > policies.resolve(persona, "review", room).retry_limit:
            return [Escalate(key, f"retry limit reached for review ({persona})")]
        attempt += 1
        actions.append(Review(key, attempt, persona, round))
    return actions


def _burn_down(item: WorkItem, history: Sequence[Segment], findings: Sequence[dict],
               policies: Policies, room: str | None, head: str | None, workflow: Workflow, now: datetime) -> list[Action] | None:
    """Serialize fixes in the writable checkout; reviewers use independent detached trees."""
    reviews = _panel_history(history)
    if (not reviews or "review" not in policies.stages or not workflow.next("review")
            or any(s.started_at and not s.ended_at for s in history)):
        return None
    resolved = _policy(policies, "review", room)
    if any(name not in {getattr(s, "persona", "") for s in reviews} for name in _reviewers(policies, room)):
        return None
    if not findings:
        return None
    edge = _next(workflow, "review", True)
    if edge is None:
        return [Wait(item.issue_key, "no unique workflow edge")]
    if item.stage not in ("review", edge.target):
        return None
    keys = {f["finding_key"] for f in findings}
    fixes = [s for s in history if getattr(s, "finding_key", "") in keys]
    # Once a fix has run, head moves are our own pushes; before that, a moved head makes these findings stale.
    if not fixes and any(getattr(s, "reviewed_head", "") not in ("", head) for s in reviews):
        return _panel(item.issue_key, history, policies, room, head, fresh=True)
    gate = _gate(item.issue_key, edge, policies, room, now)
    if gate is not None:
        return [gate]
    pending = [f for f in findings
               if not any(s.finding_key == f["finding_key"] and s.outcome == "done" for s in fixes)]
    if not pending:
        return _panel(item.issue_key, history, policies, room, head, fresh=True)
    prior_rounds = {s.finding_key.split("/")[0] for s in history
                    if getattr(s, "finding_key", "") and s.finding_key not in keys and s.outcome not in RETRYABLE}
    if len(prior_rounds) >= resolved.retry_limit:
        return [Escalate(item.issue_key, "retry limit reached for review fix rounds")]
    consult = next((p for name in _reviewers(policies, room)
                    if (p := policies.resolve(name, "review", room)).architect), None)
    if consult is not None:
        start = min(_time(s.started_at) for s in reviews)
        if not any(s.stage == "architect" and s.outcome == "done" and s.ended_at
                   and _time(s.started_at) >= start for s in history):
            return [_consult(item.issue_key, history, policies, room, consult)]
        # The consult disposed each finding; plan() already set aside file, keep and discard.
        if any("disposition" not in f for f in pending):
            return [Escalate(item.issue_key, "architect left a review finding undisposed")]
    finding = pending[0]
    tries = [s for s in fixes if s.finding_key == finding["finding_key"] and s.outcome not in RETRYABLE]
    if len(tries) > _policy(policies, edge.target, room).retry_limit:
        return [Escalate(item.issue_key, "retry limit reached for finding " + finding["finding_key"])]
    attempt = max((s.attempt for s in history if s.stage == edge.target), default=0) + 1
    return [Dispatch(item.issue_key, edge.target, policies.stages[edge.target].persona, attempt,
                     finding["finding_key"], dict(finding))]


def plan(items: Sequence[WorkItem], segments: Mapping[str, Sequence[Segment]], policies: Policies,
         issue_types: Mapping[str, str | None], free_slots: int, now: datetime,
         open_findings: Mapping[str, Sequence[dict]], *, created_at: Mapping[str, datetime] | None = None,
         rooms: Mapping[str, str] | None = None,
         decisions: Mapping[str, Sequence[Decision]] | None = None,
         quota_resets: Mapping[str, datetime] | None = None,
         heads: Mapping[str, str | None] | None = None) -> list[Action]:
    """Actions oldest issue first; each panel reviewer and each fix consumes one slot.

    Findings are structured records from the latest panel; an absent item is unknown.
    A finding the architect disposed of as file, keep or discard no longer blocks.
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
        if reset is not None and _time(reset) > now and item.forge_state != "closed":
            actions.append(Wait(item.issue_key, "quota reset pending"))
            continue
        key = item.issue_key
        history = segments.get(key, ())
        room = (rooms or {}).get(key)
        head = (heads or {}).get(key)
        findings = open_findings.get(key)
        counts = findings
        planned = None
        if findings is not None:
            actors = {s.attempt: s.persona for s in history if s.stage == "review"}
            required = _reviewers(policies, room) if "review" in policies.stages else ()
            counts = []
            for finding in findings:
                if finding.get("disposition", "fix") != "fix":
                    continue
                persona = actors.get(int(finding["finding_key"].split("/")[1]))
                if persona not in required:
                    continue
                threshold = policies.resolve(persona, "review", room).blocking_severity
                if SEVERITIES.index(finding["severity"]) <= SEVERITIES.index(threshold):
                    counts.append(finding)
            if not item.human_review and item.forge_state != "closed":
                workflow = policies.workflow_for(issue_types.get(key))
                planned = _burn_down(item, history, counts, policies, room, head, workflow, now)
        # A partly launched panel must keep filling available slots while its peers run.
        panel = _panel_history(history)
        latest = {getattr(s, "persona", ""): s for s in panel}
        if (planned is None and panel and item.stage == "review" and not item.human_review
                and item.forge_state != "closed" and getattr(panel[0], "review_round", 0)
                and any(name not in latest or (latest[name].ended_at and latest[name].outcome != "done")
                        for name in _reviewers(policies, room))):
            planned = _panel(key, history, policies, room, head)
        if planned is None:
            action = _item(item, history, policies, issue_types.get(key), room, now, counts,
                           (decisions or {}).get(key, ()), head)
            if isinstance(action, Review):
                complete = (panel and all(s.ended_at and s.outcome == "done" for s in panel)
                            and all(name in latest for name in _reviewers(policies, room)))
                planned = _panel(key, history, policies, room, head, fresh=bool(complete))
            else:
                planned = [action] if action is not None else []
        for action in planned:
            if isinstance(action, (Dispatch, Review)):
                if free_slots:
                    free_slots -= 1
                else:
                    action = Wait(key, "no free worker slots")
            actions.append(action)
    return actions
