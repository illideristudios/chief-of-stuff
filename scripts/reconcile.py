"""Plan work from snapshots and policy. Use Cases; no I/O or execution."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal, Mapping, Protocol, Sequence

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


def suite_holds(counts: Mapping[str, int | None], *, require_green: bool) -> bool:
    """Hold unknown counts, zero passed, or nonzero failed/skipped/xfailed; opt-out never holds."""
    if not require_green:
        return False
    keys = ("passed", "failed", "skipped", "xfailed")
    if any(counts.get(key) is None for key in keys):
        return True
    return counts["passed"] == 0 or any(counts[key] for key in keys[1:])


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


def review_findings(reports: Sequence[object], history: Sequence[Segment], *,
                    sessions: Mapping[int, str] | None = None) -> list[dict] | None:
    """Open findings from each panel member's latest structured report; absent/failed is unknown.

    Engine callers bind reports to the launched sessions. Reviewer dispositions are never authoritative. Keys bind
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
            content = json.dumps([finding.get("severity"), finding.get("title"), finding.get("evidence"),
                                  finding.get("path"), finding.get("line")])
            finding = {k: v for k, v in finding.items() if k != "disposition"}
            findings.append({**finding, "finding_key": f"{round}/{segment.attempt}/{hashlib.sha1(content.encode()).hexdigest()[:8]}"})
    return findings


@dataclass(frozen=True)
class ArchitectReply:
    reviewed_head: str
    status: str
    created_at: str
    dispositions: Mapping[str, str]
    request_token: str = ""


@dataclass(frozen=True)
class Consult:
    issue_key: str
    head: str
    findings: Sequence[dict]
    reason: str = "architect consultation required"


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


Action = Dispatch | Review | Merge | Ask | Escalate | Wait | Consult


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


def wants_architect(policies: Policies, stages: set[str], completed: Sequence[Segment], room: str | None) -> bool:
    """Whether merging consults the architect: the review stage, a stage that ran, or a panel member sets `architect`."""
    ran = {"review" if "review" in stages else "merge"} | {s.stage for s in completed}
    found = [_policy(policies, stage, room) for stage in sorted(ran) if stage in policies.stages]
    if "review" in stages:
        found += [policies.resolve(name, "review", room) for name in _reviewers(policies, room)]
    return any(p.architect for p in found)


def consults_architect(policies: Policies, history: Sequence[Segment], issue_type: str | None, room: str | None) -> bool:
    """`wants_architect` for an item, so a driver reads the reply exactly when `_merge` would ask for it."""
    workflow = policies.workflow_for(issue_type)
    stages = {workflow.start} | {e.target for e in workflow.edges}
    return wants_architect(policies, stages, [s for s in history if s.started_at and s.ended_at], room)


def _merge(item: WorkItem, workflow: Workflow, history: Sequence[Segment], policies: Policies,
           room: str | None, now: datetime, counts: Sequence[dict] | None, edges: Sequence[Edge],
           decisions: Sequence[Decision], head: str | None, reply: ArchitectReply | None,
           requested: Mapping[str, Sequence[dict]] | Sequence[str]) -> Action:
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
    if counts:  # already filtered by each reporting persona's effective policy
        return Wait(key, "blocking review findings")
    if wants_architect(policies, stages, completed, room):
        if reply is None or reply.status != "APPROVED" or reply.reviewed_head != head:
            return _consult(key, head, (), requested)
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
          decisions: Sequence[Decision], head: str | None, reply: ArchitectReply | None,
          requested: Mapping[str, Sequence[dict]] | Sequence[str], hold_stages: Sequence[str], launcher_hold: Mapping[str, object]) -> Action | None:
    key = item.issue_key
    if item.forge_state == "closed" or any(s.started_at and not s.ended_at for s in history):
        return None
    workflow = policies.workflow_for(issue_type)
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
        return _merge(item, workflow, history, policies, room, now, counts, incoming, decisions, head, reply, requested)
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
        return _merge(item, workflow, history, policies, room, now, counts, (edge,), decisions, head, reply, requested)
    action = _work(key, edge.target, workflow, history, policies, room)
    if isinstance(action, Escalate):
        return action
    return _gate(key, edge, policies, room, now) or action


def _reviewers(policies: Policies, room: str | None) -> tuple[str, ...]:
    resolved = _policy(policies, "review", room)
    return resolved.reviewers or (policies.stages["review"].persona,)


def request_token(head: str, findings: Sequence[dict]) -> str:
    """Names one architect request: the forge head plus the findings it asks about, so no clock is compared."""
    keys = "\n".join(sorted({f["finding_key"] for f in findings}))
    return f"{head}@{hashlib.sha256(keys.encode()).hexdigest()}"


def request_records(requested: Mapping[str, Sequence[dict]] | Sequence[str]) -> dict[str, list[dict]]:
    """Persisted requests keep their original findings; older token-only records keep deduplicating."""
    return {token: list(findings) for token, findings in requested.items()} if isinstance(requested, Mapping) else {
        token: [] for token in requested}


def _requested(head: str, findings: Sequence[dict], requested: Mapping[str, Sequence[dict]] | Sequence[str]) -> bool:
    if request_token(head, findings) in requested:
        return True
    keys = {f["finding_key"] for f in findings}
    covered = {f["finding_key"] for token, asked in request_records(requested).items()
               if token.split("@", 1)[0] == head for f in asked}
    return bool(keys) and keys <= covered


def _consult(key: str, head: str | None, findings: Sequence[dict],
             requested: Mapping[str, Sequence[dict]] | Sequence[str]) -> Action:
    if not head:
        return Wait(key, "change head is unknown")
    if _requested(head, findings, requested):
        return Wait(key, "waiting for the architect")
    return Consult(key, head, list(findings))


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
               policies: Policies, room: str | None, head: str | None, workflow: Workflow, now: datetime,
               reply: ArchitectReply | None, requested: Mapping[str, Sequence[dict]] | Sequence[str]) -> list[Action] | None:
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
    if not head:
        return [Wait(item.issue_key, "change head is unknown")]
    edge = _next(workflow, "review", True)
    if edge is None:
        return [Wait(item.issue_key, "no unique workflow edge")]
    if item.stage not in ("review", edge.target):
        return None
    keys = {f["finding_key"] for f in findings}
    fixes = [s for s in history if getattr(s, "finding_key", "") in keys]
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
        # Ask about new findings once. A reply dispatches every fix or undisposed finding;
        # later dispositions never rename the request that already asked about them.
        undisposed = [f for f in findings if "disposition" not in f]
        answered = request_records(requested).get(reply.request_token, []) if reply else []
        reply_covers = bool(reply and reply.status in ("APPROVED", "CHANGES")
                            and reply.reviewed_head == head and reply.request_token in requested
                            and reply.request_token.split("@", 1)[0] == head
                            and {f["finding_key"] for f in undisposed} <= {f["finding_key"] for f in answered})
        if reply is None or (undisposed and not reply_covers):
            return [_consult(item.issue_key, head, undisposed, requested)]
    finding = pending[0]
    tries = [s for s in fixes if s.finding_key == finding["finding_key"] and s.outcome not in RETRYABLE]
    if len(tries) > _policy(policies, edge.target, room).retry_limit:
        return [Escalate(item.issue_key, "retry limit reached for finding " + finding["finding_key"])]
    attempt = max((s.attempt for s in history if s.stage == edge.target), default=0) + 1
    return [Dispatch(item.issue_key, edge.target, policies.stages[edge.target].persona, attempt,
                     finding["finding_key"], dict(finding))]


def _unexplained(item: WorkItem, history: Sequence[Segment], panel: Sequence[Segment], head: str | None) -> bool:
    """The forge head moved off the panel at the review stage, and no done fix in this round recorded that forge head."""
    if not head or not panel or item.stage != "review" or item.human_review or item.forge_state == "closed":
        return False
    if any(s.started_at and not s.ended_at for s in history):
        return False
    if not any(getattr(s, "reviewed_head", "") not in ("", head) for s in panel):
        return False
    prefixes = {str(getattr(s, "review_round", 0) or s.attempt) for s in panel}
    return not any(getattr(s, "finding_key", "") and s.outcome == "done" and s.ended_at and s.reviewed_head == head
                   and s.finding_key.split("/")[0] in prefixes for s in history)


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
        return "restore" if item.forge_state == "open" and (
            item.stage == pending_hold["stage"] or pending_hold.get("retry") is False) else "drop_hold"
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


def plan(items: Sequence[WorkItem], segments: Mapping[str, Sequence[Segment]], policies: Policies,
         issue_types: Mapping[str, str | None], free_slots: int, now: datetime,
         open_findings: Mapping[str, Sequence[dict]], *, created_at: Mapping[str, datetime] | None = None,
         rooms: Mapping[str, str] | None = None,
         decisions: Mapping[str, Sequence[Decision]] | None = None,
         quota_resets: Mapping[str, datetime] | None = None,
         heads: Mapping[str, str | None] | None = None,
         architect_replies: Mapping[str, ArchitectReply] | None = None,
         consult_requests: Mapping[str, Mapping[str, Sequence[dict]] | Sequence[str]] | None = None,
         hold_stages: Sequence[str] = (),
         launcher_holds: Mapping[str, Mapping[str, object]] | None = None) -> list[Action]:
    """Actions oldest issue first; each panel reviewer and each fix consumes one slot.

    Findings are structured records from the latest panel; an absent item is unknown.
    A finding the architect disposed of as keep or discard no longer blocks, and that disposition
    stays with the finding key after the head moves. Only an approval is bound to the forge head.
    Gates use their source stage's policy; rework limits use the destination stage's policy, excluding reviews.
    Merge requires answered decisions and, where policy requires it, an architect approval for the current forge head.
    Without creation times, issue numbers order work within each forge namespace.
    Attempt-owned extra holds on failed current-stage work can be released for relaunch; hold_stages always wait for a human.
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
        panel = _panel_history(history)
        requested = (consult_requests or {}).get(key, ())
        reply = (architect_replies or {}).get(key)
        dispositions = reply.dispositions if reply else {}
        # Approval counts only for this forge head. Dispositions apply by finding key whatever head they came from.
        verdict = reply if (reply and head and reply.reviewed_head == head
                            and reply.status in ("APPROVED", "CHANGES")) else None
        if findings is not None:
            findings = [{**{k: v for k, v in f.items() if k != "disposition"},
                         **({"disposition": dispositions[f["finding_key"]]}
                            if dispositions.get(f["finding_key"]) in DISPOSITIONS else {})}
                        for f in findings]
        counts = findings
        planned = None
        if findings is not None:
            actors = {s.attempt: s.persona for s in history if s.stage == "review"}
            required = _reviewers(policies, room) if "review" in policies.stages else ()
            counts = []
            for finding in findings:
                if finding.get("disposition", "fix") in ("keep", "discard"):
                    continue
                persona = actors.get(int(finding["finding_key"].split("/")[1]))
                if persona not in required:
                    continue
                threshold = policies.resolve(persona, "review", room).blocking_severity
                if SEVERITIES.index(finding["severity"]) <= SEVERITIES.index(threshold):
                    counts.append(finding)
            if planned is None and not item.human_review and item.forge_state != "closed":
                workflow = policies.workflow_for(issue_types.get(key))
                planned = _burn_down(item, history, counts, policies, room, head, workflow, now, reply, requested)
        # A partly launched panel must keep filling available slots while its peers run.
        latest = {getattr(s, "persona", ""): s for s in panel}
        action = None
        legacy = bool(panel) and not any(getattr(s, "review_round", 0) for s in panel)
        if planned is None:
            action = _item(item, history, policies, issue_types.get(key), room, now, counts,
                           (decisions or {}).get(key, ()), head, verdict, requested, hold_stages,
                           (launcher_holds or {}).get(key, {}))
        if (planned is None and panel and item.stage == "review" and not item.human_review
                and item.forge_state != "closed" and not (legacy and isinstance(action, Escalate))  # pre-round reviews spend the old retry budget
                and (not getattr(panel[0], "persona", "") or any(name not in latest or (latest[name].ended_at and latest[name].outcome != "done")
                        for name in _reviewers(policies, room)))):
            planned = _panel(key, history, policies, room, head)
        if planned is None:
            if isinstance(action, Review):
                complete = (panel and all(s.ended_at and s.outcome == "done" for s in panel)
                            and all(name in latest for name in _reviewers(policies, room)))
                planned = _panel(key, history, policies, room, head, fresh=bool(complete))
            else:
                planned = [action] if action is not None else []
        # Stage and gate rules have already had their say. An unexplained forge-head move is re-reviewed;
        # a gate or a hold is not. A done fix explains the move only when it recorded this forge head.
        if (_unexplained(item, history, panel, head) and planned
                and any(isinstance(a, (Dispatch, Consult, Merge)) for a in planned)):
            planned = _panel(key, history, policies, room, head, fresh=True)
        for action in planned:
            if isinstance(action, (Dispatch, Review)):
                actor = policies.personas.get(action.persona)
                if actor is not None and actor.agent == "frank-lloyd-aight":
                    actions.append(Wait(key, "architect runs require Zach's request"))
                    continue
                if free_slots:
                    free_slots -= 1
                else:
                    action = Wait(key, "no free worker slots")
            actions.append(action)
    return actions
