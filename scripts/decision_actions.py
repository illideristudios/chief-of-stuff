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
CLOSED_STATES = frozenset({"answered", "reversed", "re-routed"})


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
    if rec_blocked and status in CLOSED_STATES:
        status = "blocked"
    return DecisionState(
        id=int(getattr(record, "id", getattr(record, "decision_id", 0)) or 0),
        issue_key=str(getattr(record, "issue_key", "") or ""),
        gate=str(getattr(record, "gate", getattr(record, "stage", "")) or ""),
        prompt=str(getattr(record, "prompt", getattr(record, "question", "")) or ""),
        status=status,
        answer=str(getattr(record, "answer", "") or ""),
        decided_by=str(getattr(record, "decided_by", "") or ""),
        asked_at=str(getattr(record, "asked_at", "") or ""),
        answered_at=getattr(record, "answered_at", None),
        blocked_reason=rec_blocked,
        rerouted_to=rec_rerouted,
    )


def board_status(record: Any, *, blocked_reason: str = "") -> str:
    st = state_of(record, blocked_reason=blocked_reason)
    if st.status == "blocked" or st.blocked_reason:
        return "blocked"
    return st.status


def is_closed(record: Any, *, blocked_reason: str = "") -> bool:
    st = state_of(record, blocked_reason=blocked_reason)
    if st.status == "blocked" or st.blocked_reason:
        return False
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
    if prev.status not in OPEN_STATES or prev.rerouted_to:
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
    if prev.status not in {"answered", "blocked"} or prev.rerouted_to:
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
) -> Transition:
    prev = state_of(record)
    if prev.status not in OPEN_STATES | {"answered"}:
        raise ValueError("decision is already closed")
    if not target_stage.strip():
        raise ValueError("a target stage is required")
    cleaned_stage = target_stage.strip()
    cleaned_answer = answer_text.strip() or f"Re-routed to {cleaned_stage}"
    nxt = replace(
        prev,
        status="re-routed",
        answer=cleaned_answer,
        decided_by=decided_by or "user",
        answered_at=answered_at if answered_at is not None else prev.answered_at,
        blocked_reason="",
        rerouted_to=cleaned_stage,
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
    nxt = replace(state_of(record), status="blocked", blocked_reason=reason)
    return Transition(decision=nxt, store_write=_write_for(nxt))


def _update(records: Sequence[Any], decision_id: int | None, statuses: frozenset[str]) -> DecisionState | None:
    """The one decision with this id, if it is in a state the update applies to; an absent id matches nothing."""
    found = next((r for r in records if decision_id and getattr(r, "id", None) == decision_id), None)
    prev = state_of(found) if found is not None else None
    return prev if prev is not None and prev.status in statuses else None


def block(records: Sequence[Any], decision_id: int | None, reason: str) -> DecisionUpdate | None:
    """The decision a refused write or a spent retry limit holds up shows as blocked with the reason; it keeps its target."""
    prev = _update(records, decision_id, frozenset({"re-routed", "reversed", "blocked"}))
    if prev is None:
        return None
    nxt = refuse(prev, reason).decision
    return DecisionUpdate(nxt, _write_for(nxt))


def unblock(records: Sequence[Any], decision_id: int | None) -> DecisionUpdate | None:
    """The decision whose block has cleared is restored."""
    prev = _update(records, decision_id, frozenset({"blocked"}))
    if prev is None:
        return None
    tr = meet_action(prev, None)
    return DecisionUpdate(tr.decision, tr.store_write)


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
            answered_at: str | None = None) -> Transition:
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
                   persona=persona, attempt=attempt)


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
    if prev.status not in {"answered", "blocked"}:
        return Transition(decision=prev, store_write=None, actions=())
    reason = _action_block_reason(action)
    if reason:
        nxt = replace(prev, status="blocked", blocked_reason=reason)
        return Transition(decision=nxt, store_write=_write_for(nxt), actions=())
    restored = ("re-routed" if prev.rerouted_to else "answered") if prev.status == "blocked" else prev.status
    nxt = replace(prev, status=restored, blocked_reason="")
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
