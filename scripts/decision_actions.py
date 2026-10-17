"""Pure decision lifecycle transitions (Use Cases).

Covers open, answered, reversed, re-routed, and answered-but-blocked decisions.
Callers in Drivers (pages, engine) pass a decision record and the issue's state
in and execute the returned store writes, forge writes, and re-dispatch actions.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import Any

from policy import Policies

OPEN_STATES = frozenset({"open", "blocked"})
CLOSED_STATES = frozenset({"answered", "reversed", "re-routed", "superseded"})


class BadRequest(ValueError):
    """The request itself is wrong; any other ValueError is a decision whose state cannot take it."""


@dataclass(frozen=True)
class DecisionState:
    id: int
    issue_key: str
    gate: str
    prompt: str
    status: str
    answer: str = ""
    decided_by: str = ""
    asked_at: str = ""
    answered_at: str | None = None
    blocked_reason: str = ""
    rerouted_to: str = ""
    blocked_from: str = ""
    superseded_by: int = 0


@dataclass(frozen=True)
class StoreDecisionWrite:
    decision_id: int
    issue_key: str
    status: str
    answer: str
    decided_by: str
    answered_at: str | None
    blocked_reason: str = ""
    rerouted_to: str = ""
    blocked_from: str = ""
    superseded_by: int = 0


@dataclass(frozen=True)
class ForgeComment:
    issue_key: str
    body: str


@dataclass(frozen=True)
class ForgeStageWrite:
    issue_key: str
    stage: str
    previous_stage: str = ""


@dataclass(frozen=True)
class Redispatch:
    issue_key: str
    stage: str
    persona: str
    attempt: int
    reason: str
    decision_id: int = 0


@dataclass(frozen=True)
class DecisionUpdate:
    """One decision's store write, keyed by its id."""
    decision: DecisionState
    store_write: StoreDecisionWrite


@dataclass(frozen=True)
class Transition:
    decision: DecisionState
    store_write: StoreDecisionWrite | None
    actions: tuple[ForgeComment | ForgeStageWrite, ...] = ()
    redispatch: Redispatch | None = None


def state_of(record: Any, *, blocked_reason: str = "", rerouted_to: str = "") -> DecisionState:
    if isinstance(record, DecisionState):
        if blocked_reason or rerouted_to:
            return replace(
                record,
                blocked_reason=blocked_reason or record.blocked_reason,
                rerouted_to=rerouted_to or record.rerouted_to,
            )
        return record
    rec_blocked = str(getattr(record, "blocked_reason", "") or blocked_reason or "")
    rec_rerouted = str(getattr(record, "rerouted_to", "") or rerouted_to or "")
    status = str(getattr(record, "status", "answered" if getattr(record, "answer", None) else "open") or "open")
    return DecisionState(
        id=int(getattr(record, "id", getattr(record, "decision_id", 0)) or 0),
        issue_key=str(getattr(record, "issue_key", "") or ""),
        gate=str(getattr(record, "gate", getattr(record, "topic", getattr(record, "stage", ""))) or ""),
        prompt=str(getattr(record, "prompt", getattr(record, "question", "")) or ""),
        status=status,
        answer=str(getattr(record, "answer", "") or ""),
        decided_by=str(getattr(record, "decided_by", "") or ""),
        asked_at=str(getattr(record, "asked_at", "") or ""),
        answered_at=getattr(record, "answered_at", None),
        blocked_reason=rec_blocked,
        rerouted_to=rec_rerouted,
        blocked_from=str(getattr(record, "blocked_from", "") or ""),
        superseded_by=int(getattr(record, "superseded_by", 0) or 0),
    )


def board_status(record: Any, *, blocked_reason: str = "") -> str:
    st = state_of(record, blocked_reason=blocked_reason)
    return st.status


def is_closed(record: Any, *, blocked_reason: str = "") -> bool:
    st = state_of(record, blocked_reason=blocked_reason)
    return st.status in CLOSED_STATES


def _write_for(st: DecisionState) -> StoreDecisionWrite:
    return StoreDecisionWrite(
        decision_id=st.id,
        issue_key=st.issue_key,
        status=st.status,
        answer=st.answer,
        decided_by=st.decided_by,
        answered_at=st.answered_at,
        blocked_reason=st.blocked_reason,
        rerouted_to=st.rerouted_to,
        blocked_from=st.blocked_from,
        superseded_by=st.superseded_by,
    )


def answer(
    record: Any,
    answer_text: str,
    *,
    decided_by: str = "user",
    answered_at: str | None = None,
) -> Transition:
    prev = state_of(record)
    cleaned = answer_text.strip()
    if prev.status not in OPEN_STATES or prev.rerouted_to or prev.blocked_from == "reversed":
        raise ValueError("decision is already closed")
    if not cleaned:
        raise ValueError("an answer must contain text")
    nxt = replace(
        prev,
        status="answered",
        answer=cleaned,
        decided_by=decided_by or "user",
        answered_at=answered_at if answered_at is not None else prev.answered_at,
        blocked_reason="",
    )
    comment = ForgeComment(
        nxt.issue_key,
        f"Decision #{nxt.id} ({nxt.gate}) answered by {nxt.decided_by}: {nxt.answer}",
    )
    return Transition(decision=nxt, store_write=_write_for(nxt), actions=(comment,))


def from_trio(
    record: Any,
    outcome: Any,
    *,
    answered_at: str | None = None,
) -> Transition:
    prev = state_of(record)
    if outcome.unanimous:
        return answer(prev, outcome.recommendation or "", decided_by="trio", answered_at=answered_at)
    nxt = replace(prev, status="open", decided_by="", blocked_reason="")
    return Transition(decision=nxt, store_write=None, actions=())


def reverse(
    record: Any,
    answer_text: str,
    *,
    decided_by: str = "user",
    answered_at: str | None = None,
    stage: str = "",
    persona: str = "",
    attempt: int = 1,
) -> Transition:
    prev = state_of(record)
    # An answered decision whose action is blocked is still answered; a refused re-route is not.
    if prev.status not in {"answered", "blocked"} or prev.rerouted_to or prev.blocked_from == "reversed":
        raise ValueError("only an answered decision can be reversed")
    if not answer_text.strip():
        raise ValueError("a reversal must contain text")
    cleaned = answer_text.strip()
    nxt = replace(
        prev,
        status="reversed",
        answer=cleaned,
        decided_by=decided_by or "user",
        answered_at=answered_at if answered_at is not None else prev.answered_at,
        blocked_reason="",
    )
    comment = ForgeComment(
        nxt.issue_key,
        f"Decision #{nxt.id} ({nxt.gate}) reversed by {nxt.decided_by}: {nxt.answer}",
    )
    target_stage = stage or nxt.gate
    redisp = (
        Redispatch(nxt.issue_key, target_stage, persona, attempt, "decision reversed", nxt.id)
        if target_stage
        else None
    )
    return Transition(decision=nxt, store_write=_write_for(nxt), actions=(comment,), redispatch=redisp)


def reroute(
    record: Any,
    target_stage: str,
    *,
    answer_text: str = "",
    decided_by: str = "user",
    answered_at: str | None = None,
    current_stage: str = "",
    persona: str = "",
    attempt: int = 1,
    row_id: int | None = None,
) -> Transition:
    prev = state_of(record)
    allowed = OPEN_STATES | {"answered"} | ({"re-routed"} if row_id is not None else set())
    if prev.status not in allowed:
        raise ValueError("decision is already closed")
    if not target_stage.strip():
        raise ValueError("a target stage is required")
    cleaned_stage = target_stage.strip()
    cleaned_answer = answer_text.strip() or f"Re-routed to {cleaned_stage}"
    nxt = replace(
        prev,
        id=row_id if row_id is not None else prev.id,
        status="re-routed",
        answer=cleaned_answer,
        decided_by=decided_by or "user",
        answered_at=answered_at if answered_at is not None else prev.answered_at,
        blocked_reason="",
        rerouted_to=cleaned_stage,
        blocked_from="",
        superseded_by=0,
    )
    stage_write = ForgeStageWrite(nxt.issue_key, cleaned_stage, current_stage or prev.gate)
    comment = ForgeComment(
        nxt.issue_key,
        f"Decision #{nxt.id} ({nxt.gate}) re-routed to {cleaned_stage} by {nxt.decided_by}: {nxt.answer}",
    )
    redisp = (
        Redispatch(nxt.issue_key, cleaned_stage, persona, attempt, f"decision re-routed to {cleaned_stage}", nxt.id)
        if cleaned_stage
        else None
    )
    return Transition(
        decision=nxt,
        store_write=_write_for(nxt),
        actions=(stage_write, comment),
        redispatch=redisp,
    )


def refuse(record: Any, reason: str) -> Transition:
    """A write the decision needs was refused: it shows as blocked with the reason, keeping its re-route target."""
    prev = state_of(record)
    nxt = replace(prev, status="blocked", blocked_reason=reason,
                  blocked_from=prev.blocked_from if prev.status == "blocked" else prev.status)
    return Transition(decision=nxt, store_write=_write_for(nxt))


def _update(records: Sequence[Any], decision_id: int | None, statuses: frozenset[str]) -> Any | None:
    """The one decision with this id, if its stored status is one the update applies to; an absent id matches nothing."""
    found = next((r for r in records if decision_id and getattr(r, "id", None) == decision_id), None)
    return found if found is not None and state_of(found).status in statuses else None


def block(records: Sequence[Any], decision_id: int | None, reason: str) -> DecisionUpdate | None:
    """The decision a refused write or a spent retry limit holds up shows as blocked with the reason; it keeps its target.
    Its original state is kept separately, so a blocked reversal never becomes an answer."""
    found = _update(records, decision_id, frozenset({"re-routed", "reversed", "blocked"}))
    if found is None:
        return None
    nxt = refuse(found, reason).decision
    return DecisionUpdate(nxt, _write_for(nxt))


def unblock(records: Sequence[Any], decision_id: int | None) -> DecisionUpdate | None:
    """The decision whose block has cleared is restored."""
    found = _update(records, decision_id, frozenset({"blocked"}))
    if found is None:
        return None
    prev = state_of(found)
    nxt = replace(prev, status=prev.blocked_from or ("re-routed" if prev.rerouted_to else "answered"),
                  blocked_reason="", blocked_from="")
    return DecisionUpdate(nxt, _write_for(nxt))


def latest_questions(records: Sequence[Any]) -> list[DecisionState]:
    """The newest row for each question; a superseded row is closed even without an answer."""
    return list({(st.gate, st.prompt): st for st in map(state_of, records)}.values())


def unanswered(records: Sequence[Any]) -> bool:
    return any(st.status != "superseded" and (st.status == "open" or not st.answer.strip())
               for st in latest_questions(records))


def held_reroute(records: Sequence[Any]) -> bool:
    """A refused re-route or reversal holds every workflow transition, even after its request is dropped."""
    return any(st.status == "blocked" and (st.rerouted_to or st.blocked_from == "reversed")
               for st in map(state_of, records))


def supersede(records: Sequence[Any], row_id: int) -> list[DecisionUpdate]:
    """Accepting a new intent explicitly closes the source and every earlier intent it replaces."""
    updates = []
    for prev in map(state_of, records):
        if prev.id == row_id or prev.status == "superseded":
            continue
        nxt = replace(prev, status="superseded", blocked_reason="", blocked_from="", superseded_by=row_id)
        updates.append(DecisionUpdate(nxt, _write_for(nxt)))
    return updates


def restore_held(records: Sequence[Any]) -> list[DecisionUpdate]:
    """A decision-pending hold that no open decision explains is held by answers a wait blocked: restore them.
    A re-route (restored by its stage write) and a reversal (blocked by a spent retry limit) stay as they are."""
    if unanswered(records):
        return []
    updates = (unblock(records, r.id) for r in map(state_of, records)
               if not r.rerouted_to and r.blocked_from != "reversed")
    return [u for u in updates if u is not None]


def _reverse_target(dec: DecisionState, current_stage: str, history: Sequence[Any], policies: Policies,
                    issue_type: str | None) -> str:
    """The stage a reversal re-runs: the gate's own stage, else the last stage that did work; never merge."""
    if dec.gate in policies.stages and dec.gate != "merge":
        return dec.gate
    affected = next((s.stage for s in reversed(history) if s.stage not in {"review", "merge"}), None)
    stage = affected or current_stage or dec.gate
    return policies.workflow_for(issue_type).start if stage == "merge" else stage


def respond(record: Any, action: str, text: str, *, target_stage: str = "", current_stage: str = "",
            history: Sequence[Any] = (), policies: Policies, issue_type: str | None = None,
            answered_at: str | None = None, reroute_id: int | None = None) -> Transition:
    """A person's answer, reversal or re-route from the board, with the stage, persona and attempt it re-runs."""
    if action == "answer":
        if not text.strip():
            raise BadRequest("an answer must contain text")
        return answer(record, text, answered_at=answered_at)
    if action == "reverse":
        stage = _reverse_target(state_of(record), current_stage, history, policies, issue_type)
    elif action in ("reroute", "re-route"):
        stage = target_stage.strip()
    else:
        raise BadRequest("unknown decision action")
    if action == "reverse" and not text.strip():
        raise BadRequest("a reversal must contain text")
    if stage not in policies.stages:
        raise BadRequest("unknown target stage")
    persona = policies.stages[stage].persona
    if not persona:
        raise BadRequest(f"{stage} has no worker to re-run")
    attempt = max((s.attempt for s in history if s.stage == stage), default=0) + 1
    if action == "reverse":
        return reverse(record, text, answered_at=answered_at, stage=stage, persona=persona, attempt=attempt)
    return reroute(record, stage, answer_text=text, answered_at=answered_at, current_stage=current_stage,
                   persona=persona, attempt=attempt, row_id=reroute_id)


def _action_block_reason(action: Any) -> str:
    if action is None:
        return ""
    kind = type(action).__name__
    if kind in {"Wait", "Escalate"}:
        return str(getattr(action, "reason", "") or kind.casefold())
    return ""


def meet_action(
    record: Any,
    action: Any,
    *,
    blocked_reason: str = "",
) -> Transition:
    prev = state_of(record, blocked_reason=blocked_reason)
    if prev.status not in {"answered", "blocked"} or prev.rerouted_to or prev.blocked_from == "reversed":
        return Transition(decision=prev, store_write=None, actions=())
    reason = _action_block_reason(action)
    if reason:
        return refuse(prev, reason)
    restored = "answered" if prev.status == "blocked" else prev.status
    nxt = replace(prev, status=restored, blocked_reason="", blocked_from="")
    return Transition(decision=nxt, store_write=_write_for(nxt), actions=())


def tab_counts(items, decisions, segments) -> dict:
    """Lead counts from engine rows. Tabs without a store source omit their count."""
    opened = {i.issue_key: i for i in items if i.forge_state != "closed"}
    pending = [d for d in decisions if d.issue_key in opened and board_status(d) in OPEN_STATES]
    held = {d.issue_key for d in pending}
    latest = {s.issue_key: s for s in segments}
    waiting = {i.issue_key for i in opened.values() if i.human_review}
    waiting.update(key for key, seg in latest.items() if key in opened and seg.outcome == "human_review")
    held.update(waiting)
    running = {s.issue_key for s in segments if s.issue_key in opened and not s.ended_at}
    return {"workers": len(waiting), "dispatch": len(opened.keys() - held - running), "decisions": len(pending)}
