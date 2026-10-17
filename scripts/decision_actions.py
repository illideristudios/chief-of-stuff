"""Pure decision lifecycle transitions (Use Cases).

Covers open, answered, reversed, re-routed, and answered-but-blocked decisions.
Callers in Drivers (pages, engine) pass a decision record and the issue's state
in and execute the returned store writes, forge writes, and re-dispatch actions.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

OPEN_STATES = frozenset({"open", "blocked"})
CLOSED_STATES = frozenset({"answered", "reversed", "re-routed"})


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
    status = str(getattr(record, "status", "open") or "open")
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
    status = str(getattr(outcome, "status", "") or "")
    unanimous = bool(getattr(outcome, "unanimous", False))
    if status == "decided" or unanimous:
        rationale = str(
            getattr(outcome, "rationale", None)
            or getattr(outcome, "recommendation", None)
            or ""
        ).strip()
        return answer(prev, rationale, decided_by="trio", answered_at=answered_at)
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
        Redispatch(nxt.issue_key, target_stage, persona, attempt, "decision reversed")
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
        Redispatch(nxt.issue_key, cleaned_stage, persona, attempt, f"decision re-routed to {cleaned_stage}")
        if cleaned_stage
        else None
    )
    return Transition(
        decision=nxt,
        store_write=_write_for(nxt),
        actions=(stage_write, comment),
        redispatch=redisp,
    )


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
    if prev.status == "open":
        return Transition(decision=prev, store_write=None, actions=())
    reason = _action_block_reason(action)
    if reason:
        nxt = replace(prev, status="blocked", blocked_reason=reason)
        return Transition(decision=nxt, store_write=_write_for(nxt), actions=())
    restored = "answered" if prev.status == "blocked" else prev.status
    nxt = replace(prev, status=restored, blocked_reason="")
    return Transition(decision=nxt, store_write=_write_for(nxt), actions=())
