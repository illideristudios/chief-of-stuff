"""Decision lifecycle transitions: answer, reverse, re-route and supersede a board intent (Use Cases, §4)."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Collection, Sequence

from decision_escalation import (  # noqa: F401  re-exported: importers keep reaching these through here
    MAX_TRIO_RETRIES, retryable_failure, DecisionUpdate, exhaust_retry, stamped_failures, is_reversed, is_restarted,
    identity, latest, NeedsOwner, needs_owner, owner_waits, pending_decisions, open_reversal, reverse, restarted,
    replacements, link_ask, preserve_ask_link, ask_decision, recover_ask_link, committed_ask_answer, answer_ask,
    replacement_context, replacement_ask,
)

OPEN_STATES = frozenset({"open", "blocked"})
CLOSED_STATES = frozenset({"answered", "reversed", "re-routed", "superseded"})
# A comment rides only on the row that asked for it; later rows of the decision do not repeat it.
_LIFECYCLE = ("status", "answered_at", "blocked_reason", "rerouted_to", "intent", "intent_id", "superseded_by", "note",
              "comment")


class BadRequest(ValueError):
    """The request itself is wrong; any other ValueError is a decision whose state cannot take it."""


@dataclass(frozen=True)
class DecisionState:
    id: int  # the identity's latest row: the board posts it, so a stale form is refused
    issue_key: str
    gate: str
    prompt: str
    status: str
    answer: str = ""
    decided_by: str = ""
    asked_at: str = ""
    answered_at: str | None = None
    blocked_reason: str = ""
    rerouted_to: str = ""  # the stage a re-route or reversal runs
    intent: str = ""  # "write": its stage is not yet written; "run": its worker is not yet launched; "": done
    intent_id: int = 0  # the row the intent began on, stable across its phases
    superseded_by: int = 0
    note: str = ""  # a reversal's words; its answer stays empty so the reversal blocks until replaced
    record: Any = None


@dataclass(frozen=True)
class ForgeComment:
    issue_key: str
    body: str


@dataclass(frozen=True)
class Transition:
    decision: DecisionState
    update: DecisionUpdate | None
    actions: tuple[ForgeComment, ...] = ()


def _field(row: Any, record: dict, name: str, default: Any = "") -> Any:
    """A lifecycle field from the row's record, else the row's own attribute (the store exposes both)."""
    value = record.get(name)
    return value if value not in (None, "") else (getattr(row, name, None) or default)


def _status(row: Any, record: dict) -> str:
    if record.get("status"):
        return str(record["status"])
    if record.get("reversed") or record.get("restarted"):
        return "reversed"
    return str(getattr(row, "status", None) or ("answered" if (getattr(row, "answer", None) or "").strip() else "open"))


def state_of(row: Any) -> DecisionState:
    """One row's lifecycle state; `states` takes the latest row of every decision."""
    if isinstance(row, DecisionState):
        return row
    record = row.record if isinstance(getattr(row, "record", None), dict) else {}
    row_id = int(getattr(row, "id", 0) or 0)
    intent = str(_field(row, record, "intent"))
    answer = str(getattr(row, "answer", "") or "")
    return DecisionState(
        id=row_id, issue_key=str(getattr(row, "issue_key", "") or ""),
        gate=str(getattr(row, "topic", None) or getattr(row, "gate", "") or ""),
        prompt=str(getattr(row, "question", None) or getattr(row, "prompt", "") or ""), status=_status(row, record),
        answer=answer, decided_by=str(getattr(row, "decided_by", "") or ""),
        asked_at=str(getattr(row, "created_at", None) or getattr(row, "asked_at", "") or ""),
        answered_at=record.get("answered_at", getattr(row, "answered_at", None) or (getattr(row, "created_at", None) if answer else None)),
        blocked_reason=str(_field(row, record, "blocked_reason")), rerouted_to=str(_field(row, record, "rerouted_to")),
        intent=intent, intent_id=int(_field(row, record, "intent_id", 0) or 0) or (row_id if intent else 0),
        superseded_by=int(_field(row, record, "superseded_by", 0) or 0), note=str(_field(row, record, "note")),
        record=record)


def current_rows(records: Sequence[Any]) -> list[Any]:
    """Every decision's latest row, oldest decision first, across one issue or many: what the board shows."""
    return list({(getattr(r, "issue_key", ""), identity(r)): r for r in records}.values())


def states(records: Sequence[Any]) -> list[DecisionState]:
    """Every decision's current state."""
    return [state_of(row) for row in current_rows(records)]


def board_status(record: Any) -> str:
    return state_of(record).status


def is_closed(record: Any) -> bool:
    return state_of(record).status in CLOSED_STATES


def _row(st: DecisionState) -> DecisionUpdate:
    """The row that records `st`, keeping the identity and the trio's evidence of the row it follows."""
    record = {k: v for k, v in (st.record or {}).items() if k not in _LIFECYCLE}
    fields = {"status": st.status, "answered_at": st.answered_at, "blocked_reason": st.blocked_reason,
              "rerouted_to": st.rerouted_to, "intent": st.intent,
              "intent_id": st.intent_id if st.intent else 0,  # 0 for a new intent: it begins on the row it appends
              "superseded_by": st.superseded_by, "note": st.note}
    record.update({k: v for k, v in fields.items() if v})
    return DecisionUpdate(st.gate, st.prompt, st.answer or None, st.decided_by, record)


def _transition(prev: DecisionState, nxt: DecisionState, actions: tuple[ForgeComment, ...] = ()) -> Transition:
    return Transition(nxt, None if nxt == prev else _row(nxt), actions)


def answer(record: Any, answer_text: str, *, decided_by: str = "user", answered_at: str | None = None) -> Transition:
    prev = state_of(record)
    cleaned = answer_text.strip()
    if prev.status not in OPEN_STATES or prev.intent:
        raise ValueError("decision is already closed")
    if not cleaned:
        raise ValueError("an answer must contain text")
    nxt = replace(prev, status="answered", answer=cleaned, decided_by=decided_by or "user",
                  answered_at=answered_at if answered_at is not None else prev.answered_at, blocked_reason="")
    comment = ForgeComment(nxt.issue_key, f"Decision #{nxt.id} ({nxt.gate}) answered by {nxt.decided_by}: {nxt.answer}")
    return Transition(nxt, _row(nxt), (comment,))


def _intent(prev: DecisionState, status: str, verb: str, stage: str, text: str, decided_by: str,
            answered_at: str | None, phase: str) -> Transition:
    """A reversal or re-route is a new intent on the decision's own row; an older intent there is replaced."""
    reversal = status == "reversed"
    nxt = replace(prev, status=status, answer="" if reversal else text, note=text if reversal else "",
                  decided_by=decided_by or "user", answered_at=answered_at if answered_at is not None else prev.answered_at,
                  blocked_reason="", rerouted_to=stage, intent=phase, intent_id=0, superseded_by=0)
    comment = ForgeComment(nxt.issue_key, f"Decision #{prev.id} ({nxt.gate}) {verb} by {nxt.decided_by}: {text}")
    if reversal:  # develop's reversal shape, so restarts, replacements and merge holds read it unchanged
        nxt = replace(nxt, record={**(prev.record or {}), "reversed": True, "recommendation": prev.answer or None})
    return Transition(nxt, _row(nxt), (comment,))


def reverse_intent(record: Any, answer_text: str, *, decided_by: str = "user", answered_at: str | None = None,
                   stage: str = "") -> Transition:
    prev = state_of(record)
    # An answered decision whose action is blocked is still answered; an intent not yet applied can be replaced,
    # and a settled one (no intent left) cannot.
    if prev.status not in {"answered", "blocked"} and not prev.intent:
        raise ValueError("only an answered decision can be reversed")
    if not answer_text.strip():
        raise ValueError("a reversal must contain text")
    target_stage = stage or prev.gate
    return _intent(prev, "reversed", "reversed", target_stage, answer_text.strip(), decided_by, answered_at,
                   "run" if target_stage else "")


def reroute(record: Any, target_stage: str, *, answer_text: str = "", decided_by: str = "user",
            answered_at: str | None = None) -> Transition:
    prev = state_of(record)
    if prev.status not in OPEN_STATES | {"answered"} and not prev.intent:
        raise ValueError("decision is already closed")
    if not target_stage.strip():
        raise ValueError("a target stage is required")
    stage = target_stage.strip()
    return _intent(prev, "re-routed", f"re-routed to {stage}", stage, answer_text.strip() or f"Re-routed to {stage}",
                   decided_by, answered_at, "write")


def refuse(record: Any, reason: str) -> Transition:
    """A write the decision needs was refused: it shows as blocked with the reason, keeping its target and intent."""
    prev = state_of(record)
    return _transition(prev, replace(prev, status="blocked", blocked_reason=reason))


def current_intent(records: Sequence[Any]) -> DecisionState | None:
    """The issue's one current intent: the newest still pending or blocked."""
    return max((st for st in states(records) if st.intent), key=lambda st: st.intent_id, default=None)


def _current(records: Sequence[Any], intent_id: int | None) -> DecisionState | None:
    current = current_intent(records)
    return current if current is not None and intent_id and current.intent_id == intent_id else None


def supersede(records: Sequence[Any]) -> list[DecisionUpdate]:
    """Every older intent, pending or blocked, is closed as superseded by the current one."""
    current = current_intent(records)
    return [_row(replace(st, status="superseded", blocked_reason="", intent="", superseded_by=current.intent_id))
            for st in states(records) if current and st.intent and st.intent_id != current.intent_id]


def block(records: Sequence[Any], intent_id: int | None, reason: str) -> DecisionUpdate | None:
    """The intent a spent retry limit holds up shows as blocked with the reason; it keeps its target and intent,
    which hold the issue until a newer intent supersedes it."""
    found = _current(records, intent_id)
    if found is None or found.status not in {"re-routed", "reversed", "blocked"}:
        return None
    return refuse(found, reason).update


def unblock(records: Sequence[Any], decision_id: int | None) -> DecisionUpdate | None:
    """An answer a wait blocked is restored once the wait is gone; an intent is never restored this way."""
    found = next((st for st in states(records) if decision_id and st.id == decision_id), None)
    if found is None or found.status != "blocked" or found.intent:
        return None
    return _row(replace(found, status="answered", blocked_reason=""))


def latest_questions(records: Sequence[Any]) -> list[DecisionState]:
    return states(records)


def unanswered(records: Sequence[Any]) -> bool:
    return any(st.status != "superseded" and (st.status == "open" or not st.answer.strip()) for st in states(records))


def replaces_engine_move(intent: DecisionState, stage: str) -> bool:
    """Whether applying this intent moves the card, and so replaces a stage write the engine queued for itself."""
    return intent.status != "blocked" and (intent.intent == "write" or (intent.intent == "run" and intent.rerouted_to != stage))


def advance(records: Sequence[Any], intent_id: int, stage: str = "", attempt: int | None = None) -> DecisionUpdate | None:
    """An intent's next phase: its stage written, it runs; its worker launched, it is done. A reversal's done phase is
    the restarted row. Only the current intent advances, and a blocked one stays."""
    found = _current(records, intent_id)
    if found is None or found.status == "blocked":
        return None
    nxt = replace(found, intent="run" if found.intent == "write" else "")
    if found.intent == "run" and found.status == "reversed":
        kept = {k: v for k, v in (found.record or {}).items() if k != "reversed"}
        nxt = replace(nxt, record={**kept, "restarted": stage or found.rerouted_to, "restart_attempt": attempt})
    return _row(nxt)


def restore_held(records: Sequence[Any]) -> list[DecisionUpdate]:
    """A decision-pending hold that no open decision explains is held by answers a wait blocked: restore them."""
    if unanswered(records):
        return []
    return [u for u in (unblock(records, st.id) for st in states(records)) if u is not None]


def _reverse_target(dec: DecisionState, current_stage: str, history: Sequence[Any], policies: Any,
                    issue_type: str | None) -> str:
    """The stage a reversal re-runs: the gate's own stage, else the last stage that did work; never merge."""
    if dec.gate in policies.stages and dec.gate != "merge":
        return dec.gate
    affected = next((s.stage for s in reversed(history) if s.stage not in {"review", "merge"}), None)
    stage = affected or current_stage or dec.gate
    return policies.workflow_for(issue_type).start if stage == "merge" else stage


def respond(record: Any, action: str, text: str, *, target_stage: str = "", current_stage: str = "",
            history: Sequence[Any] = (), policies: Any, issue_type: str | None = None,
            answered_at: str | None = None) -> Transition:
    """A person's answer, reversal or re-route from the board; the last two are intents."""
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
    if not policies.stages[stage].persona:
        raise BadRequest(f"{stage} has no worker to re-run")
    if action == "reverse":
        return reverse_intent(record, text, answered_at=answered_at, stage=stage)
    return reroute(record, stage, answer_text=text, answered_at=answered_at)


def meet_action(record: Any, action: Any) -> Transition:
    """A person's answer shows blocked while the planner's action waits or escalates, and answered otherwise (#288).
    A model's answer is not tracked this way, so trio rows do not grow with every wait."""
    prev = state_of(record)
    if prev.status not in {"answered", "blocked"} or prev.intent or prev.decided_by != "user":
        return Transition(prev, None)
    kind = type(action).__name__ if action is not None else ""
    reason = str(getattr(action, "reason", "") or kind.casefold()) if kind in {"Wait", "Escalate"} else ""
    if reason:
        return refuse(prev, reason)
    return _transition(prev, replace(prev, status="answered", blocked_reason=""))


def pending_comments(records: Sequence[Any], sent: Collection[str]) -> list[str]:
    """The issue comments board rows carry that are not yet posted, oldest first; each is posted once, by its words."""
    bodies = (row.record.get("comment") for row in records if isinstance(getattr(row, "record", None), dict))
    return [body for body in dict.fromkeys(b for b in bodies if isinstance(b, str) and b) if body not in sent]


def human_answer(decisions: Sequence[Any], update: DecisionUpdate) -> Any | None:
    """Human answer wins: the decision's latest row when a person answered it, which no model result replaces."""
    row = latest(decisions).get(identity(update))
    return row if row is not None and row.decided_by == "user" and (row.answer or "").strip() else None


def tab_counts(items, decisions, segments) -> dict:
    """Lead counts from engine rows. Tabs without a store source omit their count."""
    opened = {i.issue_key: i for i in items if i.forge_state != "closed"}
    pending = [d for d in states([d for d in decisions if d.issue_key in opened]) if d.status in OPEN_STATES]
    held = {d.issue_key for d in pending}
    last = {s.issue_key: s for s in segments}
    waiting = {i.issue_key for i in opened.values() if i.human_review}
    waiting.update(key for key, seg in last.items() if key in opened and seg.outcome == "human_review")
    held.update(waiting)
    running = {s.issue_key for s in segments if s.issue_key in opened and not s.ended_at}
    return {"workers": len(waiting), "dispatch": len(opened.keys() - held - running), "decisions": len(pending)}
