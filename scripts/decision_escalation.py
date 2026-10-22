"""Decision escalation and ask-link judgements: trio retries, owner waits, reversals and replacement asks (Use Cases, §4)."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
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
    """Settle an answered ask without reviving an old context or changing the trio's votes.

    An answer that landed before any consultation is itself the user's row. A user row, a reversed
    row, and an answer the latest row already records stay as they are.
    """
    if not ask.answer:
        return None
    row = ask_decision(decisions, ask)
    if row is None:
        ident = ("ask", ask.question, f"ask-{ask.id}")
        if any(identity(d) == ident for d in decisions):
            return None
        return DecisionUpdate("ask", ask.question, ask.answer, "user", {
            "recommendation": ask.answer,
            "decision": {"options": list(ask.options), "context": ask.context,
                         "stage": getattr(ask, "stage", ""), "attempt": getattr(ask, "attempt", 0),
                         "instance": f"ask-{ask.id}"}})
    if is_reversed(row) or is_restarted(row) or row.decided_by == "user" or row.answer == ask.answer:
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
