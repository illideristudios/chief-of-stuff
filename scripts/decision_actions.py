"""Decision escalation, resolution, and reversal judgements (Use Cases, §4)."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any, Collection, Sequence

from model_port import RETRYABLE

MAX_TRIO_RETRIES = 8


def retryable_failure(record: Any) -> bool:
    """A model outage or exhausted quota is not a settled disagreement."""
    if isinstance(record, dict) and record.get("retry_exhausted"):
        return False
    verdicts = record.get("verdicts", ()) if isinstance(record, dict) else ()
    return isinstance(verdicts, (list, tuple)) and any(
        isinstance(v, dict) and v.get("choice") is None
        and any(str(v.get("reasoning", "")).startswith(f"model failure ({kind}):") for kind in RETRYABLE)
        for v in verdicts)


@dataclass(frozen=True)
class DecisionUpdate:
    topic: str
    question: str
    answer: str | None
    decided_by: str
    record: dict[str, Any]


def exhaust_retry(row: Any, failures: int) -> DecisionUpdate | None:
    """A capped model outage becomes an answerable human decision, keeping the votes as evidence."""
    if row is None or row.answer or failures < MAX_TRIO_RETRIES or not retryable_failure(row.record):
        return None
    reason = f"trio unavailable after {failures} attempts: {row.question}"
    return DecisionUpdate(row.topic, row.question, None, "zach",
                          {**row.record, "retry_exhausted": failures, "error": reason})


def stamped_failures(row: Any, context: str, options: Sequence[str], stage: str = "", attempt: int = 0) -> int:
    """Failed consultations a retryable row recorded in its own commit, for this context and these options."""
    record = row.record if row is not None and isinstance(row.record, dict) else {}
    prior = record.get("decision") if isinstance(record.get("decision"), dict) else {}
    if (row is None or row.answer or not retryable_failure(record) or prior.get("context", "") != context
            or tuple(prior.get("options") or ()) != tuple(options)
            or (prior.get("stage", ""), prior.get("attempt", 0)) != (stage, attempt)):
        return 0
    return int(record.get("failures", 0))


def _time(value: datetime | str) -> datetime:
    when = datetime.fromisoformat(value) if isinstance(value, str) else value
    if when.utcoffset() is None:
        raise ValueError("timestamps must include a timezone")
    return when


def is_reversed(row: Any) -> bool:
    record = getattr(row, "record", None)
    return not (getattr(row, "answer", None) or "").strip() and isinstance(record, dict) and bool(record.get("reversed"))


def is_restarted(row: Any, history: Sequence[Any] = (), start: str = "implement") -> bool:
    record = getattr(row, "record", None)
    if isinstance(record, dict) and record.get("restarted"):
        return True
    if not is_reversed(row):
        return False
    created = getattr(row, "created_at", None)
    if created:
        try:
            when = _time(created)
            return any(s.stage == _restarts(record, start) and s.started_at and _time(s.started_at) >= when
                       for s in history)
        except ValueError:
            pass
    return False


def identity(row: Any) -> tuple[str, str, str]:
    record = row.record if isinstance(getattr(row, "record", None), dict) else {}
    decision = record.get("decision") if isinstance(record.get("decision"), dict) else {}
    return row.topic, row.question, decision.get("instance", "")


def latest(decisions: Sequence[Any]) -> dict[tuple[str, str, str], Any]:
    """Append-only state, independently for every decision instance."""
    return {identity(d): d for d in decisions}


@dataclass(frozen=True)
class NeedsOwner:
    decision_id: tuple[str, str, str]
    stage: str
    attempt: int
    reason: str


def _is_gate(row: Any, decision: dict[str, Any]) -> bool:
    # Rows written before `gate` was explicit are recognised by their shape.
    return bool(decision.get("gate", row.topic != "ask" and not decision.get("instance")
                             and tuple(decision.get("options") or ()) == ("approve", "hold")))


def needs_owner(rows: Sequence[Any]) -> NeedsOwner | None:
    """The latest row of one decision alone says whether it waits on the owner."""
    if not rows:
        return None
    row = rows[-1]
    record = row.record if isinstance(row.record, dict) else {}
    decision = record.get("decision") if isinstance(record.get("decision"), dict) else {}
    gate = _is_gate(row, decision)
    # Older gate holds committed an answer before their separate interrupt write.
    legacy_hold = row.answer == "hold" and row.decided_by == "trio" and gate
    if not legacy_hold and ((row.answer or "").strip() or row.decided_by != "zach"):
        return None
    stage, attempt = decision.get("stage", ""), decision.get("attempt", 0)
    # A gate is judged on the segment it consulted, which older rows stored as the policy source.
    scoped = re.fullmatch(r"(.+)-(\d+)", str(decision.get("context", ""))) if gate else None
    if scoped:
        stage, attempt = scoped[1], int(scoped[2])
    return NeedsOwner(identity(row), stage, attempt, str(record.get("error") or row.question))


def _when(value: Any) -> datetime | None:
    try:
        return _time(value) if value else None
    except (TypeError, ValueError):
        return None


def _restarts(record: dict, start: str) -> str:
    """The stage a reversal restarts: the one it ran, else the board's target (§6), else `workflow.start`."""
    return str(record.get("restarted") if isinstance(record.get("restarted"), str) else record.get("rerouted_to") or start)


def _restart_cutoff(decisions: Sequence[Any], segments: Sequence[Any], start: str) -> datetime | None:
    """When the latest reversal restarted its stage; a malformed time skips that candidate."""
    restarts = []
    for row in decisions:
        record = row.record if isinstance(row.record, dict) else {}
        if not (is_reversed(row) or record.get("restarted")):
            continue
        attempt = record.get("restart_attempt")
        created = _when(getattr(row, "created_at", None))
        starts = [_when(s.started_at) for s in segments if s.stage == _restarts(record, start) and s.started_at
                  and (s.attempt == attempt if attempt is not None else created is not None)]
        starts = [t for t in starts if t is not None and (attempt is not None or t >= created)]
        if starts:
            restarts.append(min(starts))
    return max(restarts, default=None)


def _superseded(scope: tuple[str, int], created: Any, segments: Sequence[Any], cutoff: datetime | None) -> bool:
    """True when the decision's attempt began before the restart, so the restart redoes it."""
    source = next((s for s in segments if (s.stage, s.attempt) == scope), None)
    started = _when(source.started_at if source is not None else created)
    return cutoff is not None and started is not None and started < cutoff


def owner_waits(decisions: Sequence[Any], segments: Sequence[Any], asks: Sequence[Any],
                start: str = "implement", passed: Collection[str] = ()) -> list[NeedsOwner]:
    """Active owner decisions, after settlements, newer attempts, reversal restarts and passed gates."""
    attempts = {}
    for source in (*segments, *asks):
        attempts[source.stage] = max(attempts.get(source.stage, 0), source.attempt)
    cutoff = _restart_cutoff(decisions, segments, start)
    result = []
    # A later settlement applies to its identity even when it omits the old scope.
    for row in latest(decisions).values():
        pending = needs_owner([row])
        if pending is None or row.topic in passed or pending.attempt < attempts.get(pending.stage, 0):
            continue
        if _superseded((pending.stage, pending.attempt), getattr(row, "created_at", None), segments, cutoff):
            continue
        result.append(pending)
    return result


def pending_decisions(decisions: Sequence[Any], gates: Sequence[str], segments: Sequence[Any] = (),
                      start: str = "implement") -> bool:
    """Ordinary decisions block merge; workflow gates are evaluated on their own edge.

    A decision from before a reversal restart is redone by the restart, but a reversed decision
    itself stays blocking until its replacement is settled.
    """
    cutoff = _restart_cutoff(decisions, segments, start)

    def blocks(row: Any) -> bool:
        record = row.record if isinstance(row.record, dict) else {}
        decision = record.get("decision") if isinstance(record.get("decision"), dict) else {}
        scope = (decision.get("stage", ""), decision.get("attempt", 0))
        return (is_reversed(row) or bool(record.get("restarted"))
                or not _superseded(scope, getattr(row, "created_at", None), segments, cutoff))

    return any(not (row.answer or "").strip() and row.topic not in gates and blocks(row)
               for row in latest(decisions).values())


def open_reversal(decisions: Sequence[Any], history: Sequence[Any] = (), start: str = "implement") -> bool:
    """True when the latest row for any question is a reversal not yet restarted at `start`."""
    rows = latest(decisions)
    return any(is_reversed(d) and not is_restarted(d, history, start) for d in rows.values())


def reverse(decisions: Sequence[Any], topic: str, question: str, *, instance: str | None = None) -> DecisionUpdate:
    """Board reversal: reopen an adopted decision so the planner routes the work again."""
    rows = [row for row in decisions if row.topic == topic and row.question == question
            and (instance is None or identity(row)[2] == instance)]
    if not rows or not (rows[-1].answer or "").strip():
        raise ValueError("only an adopted decision can be reversed")
    prior = rows[-1].record if isinstance(rows[-1].record, dict) else {}
    return DecisionUpdate(topic=topic, question=question, answer=None, decided_by="",
                          record={"reversed": True, "recommendation": rows[-1].answer,
                                  "decision": prior.get("decision")})


def restarted(decisions: Sequence[Any], stage: str, attempt: int | None = None) -> list[DecisionUpdate]:
    """Resolving rows for open reversals once `stage` (`workflow.start`) is dispatched."""
    rows = latest(decisions)
    return [DecisionUpdate(topic=d.topic, question=d.question, answer=None, decided_by="",
                           record={"restarted": stage, "restart_attempt": attempt,
                                   "recommendation": d.record.get("recommendation"),
                                   "decision": d.record.get("decision")})
            for d in rows.values() if is_reversed(d) and not d.record.get("intent")]  # a board intent ends by `advance`


def replacements(decisions: Sequence[Any], history: Sequence[Any], start: str,
                 gates: Sequence[str] = ()) -> list[tuple[Any, Any]]:
    """Recover replacements from restart evidence, only after that worker completes successfully.

    The segment is durable before a restarted marker, so a missing marker must not lose a reversal.
    Gate replacements are routed by the planner at the reworked gate instead.
    """
    origins = {}
    for row in decisions:
        key = identity(row)
        if is_reversed(row):
            origins[key] = row
        elif isinstance(row.record, dict) and row.record.get("restarted"):
            if row.record.get("restart_attempt") is not None or key not in origins:
                origins[key] = row
    result = []
    for key, row in latest(decisions).items():
        origin = origins.get(key)
        if origin is None or row.answer or row.topic in gates:
            continue
        record = origin.record
        attempt = record.get("restart_attempt")
        candidates = [s for s in history if s.stage == _restarts(record, start) and s.started_at and (
            s.attempt >= attempt if attempt is not None else
            not origin.created_at or _time(s.started_at) >= _time(origin.created_at))]
        segment = min((s for s in candidates if s.ended_at and s.outcome == "done"),
                      key=lambda s: (_time(s.started_at), s.attempt), default=None)
        if segment is not None:
            result.append((origin, segment))
    return result


def _ask_link(row: Any, decisions: Sequence[Any]) -> int | None:
    """A consultation inherits a link only within the same unreversed decision context."""
    record = row.record if isinstance(row.record, dict) else {}
    if is_reversed(row) or is_restarted(row):
        return None
    if record.get("answer_ask_id") is not None:
        return record["answer_ask_id"]
    prior = record.get("decision") or {}
    for earlier in reversed(decisions):
        if identity(earlier) != identity(row):
            continue
        earlier_record = earlier.record if isinstance(earlier.record, dict) else {}
        payload = earlier_record.get("decision") or {}
        if (is_reversed(earlier) or is_restarted(earlier)
                or payload.get("context", "") != prior.get("context", "")
                or tuple(payload.get("options") or ()) != tuple(prior.get("options") or ())):
            return None
        if earlier_record.get("answer_ask_id") is not None:
            return earlier_record["answer_ask_id"]
    return None


def link_ask(row: Any, ask_id: int) -> DecisionUpdate:
    """Attach an ask without changing the recorded answer or the trio's verdicts."""
    return DecisionUpdate(row.topic, row.question, row.answer, row.decided_by,
                          {**row.record, "answer_ask_id": ask_id})


def preserve_ask_link(decisions: Sequence[Any], update: DecisionUpdate) -> DecisionUpdate:
    """Carry the link into the same durable write as a consultation's new answer."""
    ask_id = _ask_link(update, decisions)
    return link_ask(update, ask_id) if ask_id is not None else update


def ask_decision(decisions: Sequence[Any], ask: Any) -> Any | None:
    """Find only the decision this ask can answer, including a replacement's explicit link."""
    for row in reversed(tuple(latest(decisions).values())):
        record = row.record if isinstance(row.record, dict) else {}
        prior = record.get("decision")
        if not isinstance(prior, dict):
            continue
        linked = _ask_link(row, decisions)
        matches = linked == ask.id if linked is not None else identity(row) == ("ask", ask.question, f"ask-{ask.id}")
        if (matches and row.question == ask.question and prior.get("context", "") == ask.context
                and tuple(prior.get("options") or ()) == tuple(ask.options)):
            return row
    return None


def recover_ask_link(decisions: Sequence[Any], ask: Any) -> DecisionUpdate | None:
    """Repair a historical link hidden by an already committed consultation update."""
    row = ask_decision(decisions, ask)
    if row is not None and row.record.get("answer_ask_id") != ask.id and _ask_link(row, decisions) == ask.id:
        return link_ask(row, ask.id)
    return None


def committed_ask_answer(decisions: Sequence[Any], ask: Any) -> str | None:
    """An unanswered ask receives its associated decision's already committed answer."""
    row = ask_decision(decisions, ask)
    return row.answer if not ask.answer and row is not None and row.answer else None


def answer_ask(decisions: Sequence[Any], ask: Any) -> DecisionUpdate | None:
    """Settle an answered ask without reviving an old context or changing the trio's votes."""
    row = ask_decision(decisions, ask)
    if (not ask.answer or row is None or row.answer or is_reversed(row) or is_restarted(row)):
        return None
    return DecisionUpdate(row.topic, row.question, ask.answer, "user",
                          {**row.record, "recommendation": ask.answer})


def replacement_context(row: Any, segment: Any) -> str:
    prior = row.record.get("decision") or {}
    return (f"{prior.get('context', '')}\nRestart completed: {segment.stage}-{segment.attempt}"
            f"\nDecision instance: {json.dumps(identity(row))}")


def replacement_ask(row: Any, segment: Any, decisions: Sequence[Any], asks: Sequence[Any]
                    ) -> tuple[dict[str, Any], Any, DecisionUpdate] | None:
    """Describe a human ask and its decision update only for a settled disagreement."""
    pending = latest(decisions).get(identity(row))
    if (pending is None or pending.answer or is_reversed(pending) or is_restarted(pending)
            or retryable_failure(pending.record)):
        return None
    prior = pending.record.get("decision") or {}
    context = replacement_context(row, segment)
    if prior.get("context") != context:
        return None
    existing = next((ask for ask in asks if ask.question == pending.question and ask.context == context
                     and tuple(ask.options) == tuple(prior.get("options") or ())), None)
    spec = {"stage": segment.stage, "attempt": segment.attempt, "session_id": "",
            "question": pending.question, "options": list(prior.get("options") or ()), "context": context}
    update = DecisionUpdate(pending.topic, pending.question, pending.answer, pending.decided_by, pending.record)
    return spec, existing, update


# The board lifecycle (§6): answer, reverse, re-route, and an answered decision whose action is blocked.
# Rows stay append-only: a decision's state is its identity's latest row, and every transition returns the row to
# append. A reversal or re-route is an intent carried on that row: "write" (its stage is not yet written), "run" (its
# worker is not yet launched), then done. The page server only appends one; the engine alone applies it.

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
