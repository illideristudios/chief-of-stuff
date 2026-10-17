"""Plan work from snapshots and policy. Use Cases; no I/O or execution."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Literal, Mapping, Protocol, Sequence

import completion
import readiness
import worker_health
from model_port import RETRYABLE
from orphans import Claim
from policy import Edge, Policies, Policy, SEVERITIES, Workflow
from tree_state import TreeState


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


def suite_holds(counts: Mapping[str, int | None], *, require_green: bool) -> bool:
    """Hold unknown counts, zero passed, or nonzero failed/skipped/xfailed; opt-out never holds."""
    if not require_green:
        return False
    keys = ("passed", "failed", "skipped", "xfailed")
    if any(counts.get(key) is None for key in keys):
        return True
    return counts["passed"] == 0 or any(counts[key] for key in keys[1:])


def suite_reason(counts: Mapping[str, int | None], summary: str) -> str:
    shown = ", ".join(
        f"{name}={counts[name] if counts[name] is not None else 'missing or invalid'}"
        for name in ("passed", "failed", "skipped", "xfailed"))
    return f"suite is not green; Suite: {shown}; worker said: {summary}"


def suite_hold_action(item: WorkItem, hold: object, history: Sequence[Segment], *,
                      observed: bool | None) -> str:
    """Judge a persisted hold: retire closed work; accept only explicit removal after publication.

    Invalid open holds raise so the driver can retain the local hold and isolate the item.
    Missing observations never mean release.
    """
    if item.forge_state != "open":
        return "retire"
    if (not isinstance(hold, dict) or not isinstance(hold.get("stage"), str) or not hold["stage"]
            or type(hold.get("attempt")) is not int or hold["attempt"] < 1
            or type(hold.get("published")) is not bool):
        raise ValueError("invalid suite hold record")
    segment = next((s for s in history if s.stage == hold["stage"] and s.attempt == hold["attempt"]), None)
    if segment is None or not segment.ended_at or segment.outcome != "human_review":
        raise ValueError("suite hold must reference a completed human-review segment")
    if not hold["published"]:
        return "publish"
    return "accept" if observed is False else "wait"


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


@dataclass(frozen=True)
class Escalate:
    issue_key: str
    reason: str


@dataclass(frozen=True)
class Wait:
    issue_key: str
    reason: str


@dataclass(frozen=True)
class Close:
    """End an open segment whose worker is gone. The next pass plans from the closed segment."""

    issue_key: str
    stage: str
    attempt: int
    outcome: str
    reason: str
    human_review: bool = False
    suite_hold: bool = False


Action = Dispatch | Review | Merge | Ask | Escalate | Wait | Close


@dataclass(frozen=True)
class Facts:
    """One pass's readiness, completion and stall facts. Empty means the caller did not gather them."""

    claims: tuple[readiness.Claim, ...] = ()
    blockers: tuple[tuple[str, tuple[str, ...] | None], ...] | None = None  # None: the relationship read failed
    asks: tuple[readiness.Ask, ...] = ()
    reports: tuple[readiness.Report, ...] = ()
    alive: tuple[tuple[tuple[str, str, int], bool], ...] = ()  # (issue, stage, attempt) → worker alive
    read_only: tuple[str, ...] = ()
    changed: tuple[tuple[str, bool | None], ...] = ()  # dirty; read-only also checks the launch HEAD
    unreadable: tuple[str, ...] = ()  # git tree/status could not be read; independent of baseline evidence
    on_main: tuple[tuple[str, bool | None], ...] = ()
    suite_on_main: tuple[tuple[str, bool | None], ...] = ()
    landing_heads: tuple[tuple[str, str], ...] = ()  # source head of the forge-verified landing
    landed_trees: tuple[tuple[str, bool], ...] = ()  # recorded HEAD is on main or matches the landed source head
    trees: tuple[tuple[str, TreeState], ...] = ()
    tree_names: tuple[tuple[str, str], ...] = ()
    tree_claims: tuple[tuple[str, tuple], ...] = ()
    progress: tuple[tuple[str, datetime | None], ...] = ()  # present None: no usable progress evidence; absent: not gathered
    landing_errors: tuple[tuple[str, str], ...] = ()  # per-issue forge landing failures
    blockers_error: str = ""  # why `blockers` is None
    stall_after: timedelta = timedelta(minutes=45)
    suites: tuple[tuple[tuple[str, str, int], tuple], ...] = ()  # (issue, stage, attempt) → (counts, summary)
    require_green: bool = False


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
    rounds = attempt - 1 - sum(s.stage == stage and s.outcome in RETRYABLE for s in history)
    if stage == "review":
        # Completed review rounds belong to the fix loop; only failures since the last completed review spend retries.
        rounds = 0
        for s in sorted((s for s in history if s.stage == stage), key=lambda s: s.attempt, reverse=True):
            if s.outcome == "done":
                break
            rounds += s.outcome == "failed"
    elif any(e.target == stage for e in workflow.next("review")):
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
    return Review(key, attempt) if stage == "review" else Dispatch(key, stage, policies.stages[stage].persona, attempt)


def _merge(item: WorkItem, workflow: Workflow, history: Sequence[Segment], policies: Policies,
           room: str | None, now: datetime, counts: Mapping[str, int] | None, edges: Sequence[Edge],
           decisions: Sequence[Decision]) -> Action:
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
    return _gate(key, edges[0], policies, room, now) or Merge(key)


def _next(workflow: Workflow, stage: str, blocking: bool) -> Edge | None:
    edges = workflow.next(stage)
    if stage == "review":
        edges = tuple(e for e in edges if (e.target != "merge") == blocking)
    return edges[0] if len(edges) == 1 else None


def _report_outcome(facts: Facts, key: str, segment: Segment) -> str | None:
    mine = [row for row in facts.reports
            if row.issue_key == key and (not row.stage or (row.stage == segment.stage and row.attempt == segment.attempt))]
    return mine[-1].outcome if mine else None


def _running_segment(history: Sequence[Segment]) -> Segment | None:
    return next((segment for segment in history if segment.started_at and not segment.ended_at), None)


def completion_wait(key: str, facts: Facts) -> Wait | None:
    """Forge closure alone cannot complete a merged item or release its tree."""
    gate = completion.done_on_main(dict(facts.on_main).get(key), dict(facts.suite_on_main).get(key))
    if gate:
        error = dict(facts.landing_errors).get(key)
        reason = f"merged, awaiting main CI: {gate}" + (f": {error}" if error else "")
        return Wait(key, reason)
    return None


def completion_verdict(key: str, segment: Segment, facts: Facts, workflow: Workflow, *,
                       report: str | None, worker_alive: bool | None) -> completion.Verdict | None:
    """One completion input mapping for collected exits and abandoned workers."""
    claims = dict(facts.tree_claims).get(key, ())
    if worker_alive is False:
        claims = tuple(Claim(claim.owner, False, "has no process", claim.ref) if claim.owner == key else claim for claim in claims)
    verdict = completion.abandoned(
        worker_alive, report=report,
        read_only=key in facts.read_only, changed=dict(facts.changed).get(key),
        on_main=dict(facts.on_main).get(key), suite_on_main=dict(facts.suite_on_main).get(key),
        tree_unreadable=key in facts.unreadable,
        tree=dict(facts.trees).get(key), tree_name=dict(facts.tree_names).get(key, ""),
        tree_claims=claims, landing_required=not workflow.next(segment.stage),
        tree_landed=dict(facts.landed_trees).get(key))
    if verdict is None or verdict.human_review or report != "done":
        return verdict
    found = dict(facts.suites).get((key, segment.stage, segment.attempt),
                                  (dict.fromkeys(("passed", "failed", "skipped", "xfailed")), ""))
    if not suite_holds(found[0], require_green=facts.require_green):
        return verdict
    return completion.Verdict("human_review", suite_reason(found[0], found[1]), True, True)


def _abandoned(item: WorkItem, segment: Segment, facts: Facts, workflow: Workflow) -> Close | None:
    verdict = completion_verdict(item.issue_key, segment, facts, workflow,
        report=_report_outcome(facts, item.issue_key, segment),
        worker_alive=dict(facts.alive).get((item.issue_key, segment.stage, segment.attempt)))
    if verdict is None:
        return None
    return Close(item.issue_key, segment.stage, segment.attempt, verdict.outcome, verdict.reason,
                 verdict.human_review, verdict.suite_hold)


def _stalled(item: WorkItem, segment: Segment, facts: Facts, now: datetime) -> Wait | None:
    alive = dict(facts.alive).get((item.issue_key, segment.stage, segment.attempt))
    progress = dict(facts.progress)
    if alive is not True or item.issue_key not in progress:
        return None
    if worker_health.silence(_time(segment.started_at), progress[item.issue_key], now, facts.stall_after) == "act":
        return Wait(item.issue_key, "stalled")
    return None


def _item(item: WorkItem, history: Sequence[Segment], policies: Policies, issue_type: str | None,
          room: str | None, now: datetime, counts: Mapping[str, int] | None,
          decisions: Sequence[Decision], hold_stages: Sequence[str], launcher_hold: Mapping[str, object], facts: Facts | None = None) -> Action | None:
    key = item.issue_key
    workflow = policies.workflow_for(issue_type)
    if item.forge_state == "closed" and item.merge_state == "merged" and facts is not None:
        waiting = completion_wait(key, facts)
        if waiting:
            return waiting
    running = _running_segment(history)
    if running is not None:
        if facts is None:
            return None
        return _abandoned(item, running, facts, workflow) or _stalled(item, running, facts, now)
    if item.forge_state == "closed":
        return None
    stage = item.stage or workflow.start
    stages = {workflow.start} | {e.target for e in workflow.edges}
    if stage not in stages:
        return Wait(key, "human review" if item.human_review else "stage is outside the workflow")
    # An architect segment is an out-of-band consultation unless the workflow names architect as a stage.
    completed = tuple(s for s in history if s.started_at and s.ended_at and (s.stage != "architect" or "architect" in stages)
                      and (stage != "merge" or s.stage != "merge"))
    latest = max(completed, key=lambda s: (_time(s.started_at), s.attempt), default=None)
    if latest and len({s.stage for s in completed if _time(s.started_at) == _time(latest.started_at)}) > 1:
        return Wait(key, "human review" if item.human_review else "segments tie at the latest start")
    if item.human_review:
        # Only a failed launch, whose hold the engine restored itself, can release an extra hold.
        # Quota and unknown exits, human outcomes, completed work and stage-required holds need a human.
        if (item.stage and stage not in hold_stages and latest and latest.stage == stage
                and launcher_hold.get("stage") == stage and launcher_hold.get("attempt") == latest.attempt
                and latest.outcome == "failed"):
            if launcher_hold.get("release_failures", 0) > _policy(policies, stage, room).retry_limit:
                return Escalate(key, f"hold release retry limit reached for {stage}")
            return _work(key, stage, workflow, history, policies, room)
        return Wait(key, "human review")
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
        return _merge(item, workflow, history, policies, room, now, counts, incoming, decisions)
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
        return _merge(item, workflow, history, policies, room, now, counts, (edge,), decisions)
    action = _work(key, edge.target, workflow, history, policies, room)
    if isinstance(action, Escalate):
        return action
    return _gate(key, edge, policies, room, now) or action


HoldRecovery = Literal["restore", "drop_hold", "drop_release", "replace", "keep", "drop_owner", "drop_stage", "none"]


def hold_recovery(item: WorkItem, history: Sequence[Segment], *, pending_hold: Mapping[str, object] | None,
                  pending_release: Mapping[str, object] | None, owned: Mapping[str, object] | None,
                  pending_stage: Mapping[str, object] | None = None) -> HoldRecovery:
    """What to do with an issue's saved hold records before planning.

    restore: retry a pending hold restoration. drop_hold: forget one that no longer applies.
    drop_release: forget a release the board has moved past. replace: a release without current
    ownership or a drifted failed handoff returns to a normal human hold.
    keep: leave a verified release to planning.
    drop_owner: ownership the hold no longer backs. none: nothing saved applies.
    drop_stage: an ordinary pending stage write whose card a person has since moved to a third stage.
    """
    if pending_hold:
        return "restore" if item.forge_state == "open" and item.stage == pending_hold["stage"] else "drop_hold"
    if pending_release:
        if (item.forge_state != "open" or not item.stage or item.stage != pending_release["expected"]
                or item.stage != pending_release["stage"]):
            return "drop_release"
        newest = max((s.attempt for s in history if s.stage == item.stage), default=0)
        if not owned or owned.get("stage") != item.stage or owned.get("attempt") != newest:
            # Older engines saved no provenance, and a kill inside a release can leave an older attempt.
            return "replace"
        return "keep"
    if pending_stage and item.stage not in (pending_stage["expected"], pending_stage["stage"]):
        if pending_stage.get("restore_hold") and item.forge_state == "open" and bool(item.stage):
            return "replace"
        return "drop_stage"
    if owned and (not item.human_review or item.stage != owned.get("stage")):
        return "drop_owner"
    return "none"


def _running_keys(items: Sequence[WorkItem], segments: Mapping[str, Sequence[Segment]], facts: Facts | None) -> list[str]:
    alive = dict(facts.alive) if facts is not None else {}
    found = []
    for item in items:
        segment = _running_segment(segments.get(item.issue_key, ()))
        if segment is None:
            continue
        if alive.get((item.issue_key, segment.stage, segment.attempt)) is False:
            continue
        found.append(item.issue_key)
    return found


def _with_facts(action: Action | None, item: WorkItem, history: Sequence[Segment], facts: Facts,
                running: Sequence[str]) -> Action | None:
    # Completion writes a forge hold before closure. A human's release must survive retained tree evidence.
    if not isinstance(action, (Dispatch, Review)):
        return action
    blockers = None if facts.blockers is None else dict(facts.blockers).get(item.issue_key, ())
    reason = readiness.why_not(item.issue_key, claims=facts.claims, running=running, blockers=blockers,
                               asks=facts.asks, reports=facts.reports)
    if reason == "dependency blockers unknown" and facts.blockers_error:
        reason = f"{reason}: {facts.blockers_error}"
    return Wait(item.issue_key, reason) if reason else action


def plan(items: Sequence[WorkItem], segments: Mapping[str, Sequence[Segment]], policies: Policies,
         issue_types: Mapping[str, str | None], free_slots: int, now: datetime,
         open_findings: Mapping[str, Mapping[str, int]], *, created_at: Mapping[str, datetime] | None = None,
         rooms: Mapping[str, str] | None = None,
         decisions: Mapping[str, Sequence[Decision]] | None = None,
         quota_resets: Mapping[str, datetime] | None = None,
         hold_stages: Sequence[str] = (),
         launcher_holds: Mapping[str, Mapping[str, object]] | None = None,
         facts: Facts | None = None) -> list[Action]:
    """One action per eligible issue, oldest first; dispatch and review each take one slot.

    Findings are complete severity counts from the latest review; an absent item is unknown.
    Gates use their source stage's policy; retries use the destination stage's policy. Completed reviews do not spend retries.
    Merge requires answered decisions and, where policy requires it, a done architect segment for the current work.
    Without creation times, issue numbers order work within each forge namespace.
    Attempt-owned extra holds on failed current-stage work can be released for relaunch; hold_stages always wait for a human.
    `facts` are this pass's ownership, blockers, stops, tree and stall readings. Without them a running segment
    still suppresses actions.
    """
    _time(now)
    if type(free_slots) is not int or free_slots < 0:
        raise ValueError("free_slots must be a non-negative integer")
    actions = []
    running = _running_keys(items, segments, facts)
    accepted: list[str] = []
    for item in sorted(items, key=lambda item: _order(item, created_at or {})):
        history = segments.get(item.issue_key, ())
        action = _item(item, history, policies, issue_types.get(item.issue_key),
                       (rooms or {}).get(item.issue_key), now, open_findings.get(item.issue_key),
                       (decisions or {}).get(item.issue_key, ()), hold_stages,
                       (launcher_holds or {}).get(item.issue_key, {}), facts)
        if facts is not None and action is not None:
            action = _with_facts(action, item, history, facts, (*running, *accepted))
        if isinstance(action, (Dispatch, Review)):
            reset = (quota_resets or {}).get(item.issue_key)  # quota holds launches only; merges and gates go on
            if reset is not None and _time(reset) > now:
                action = Wait(item.issue_key, "quota reset pending")
            elif free_slots:
                free_slots -= 1
                accepted.append(item.issue_key)
            else:
                action = Wait(item.issue_key, "no free worker slots")
        if action is not None:
            actions.append(action)
    return actions
