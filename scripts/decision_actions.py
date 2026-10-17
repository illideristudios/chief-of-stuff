"""Decision escalation, resolution, and reversal judgements (Use Cases, §4)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Sequence

from model_port import RETRYABLE


def retryable_failure(record: Any) -> bool:
    """A model outage or exhausted quota is not a settled disagreement."""
    verdicts = record.get("verdicts", ()) if isinstance(record, dict) else ()
    return isinstance(verdicts, (list, tuple)) and any(
        isinstance(v, dict) and v.get("choice") is None
        and any(str(v.get("reasoning", "")).startswith(f"model failure ({kind}):") for kind in RETRYABLE)
        for v in verdicts)


@dataclass(frozen=True)
class Reversal:
    topic: str
    question: str
    answer: None
    decided_by: str
    record: dict[str, Any]


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
            return any(s.stage == start and s.started_at and _time(s.started_at) >= when for s in history)
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


def open_reversal(decisions: Sequence[Any], history: Sequence[Any] = (), start: str = "implement") -> bool:
    """True when the latest row for any question is a reversal not yet restarted at `start`."""
    rows = latest(decisions)
    return any(is_reversed(d) and not is_restarted(d, history, start) for d in rows.values())


def reverse(decisions: Sequence[Any], topic: str, question: str, *, instance: str | None = None) -> Reversal:
    """Board reversal: reopen an adopted decision so the planner routes the work again."""
    rows = [row for row in decisions if row.topic == topic and row.question == question
            and (instance is None or identity(row)[2] == instance)]
    if not rows or not (rows[-1].answer or "").strip():
        raise ValueError("only an adopted decision can be reversed")
    prior = rows[-1].record if isinstance(rows[-1].record, dict) else {}
    return Reversal(topic=topic, question=question, answer=None, decided_by="",
                    record={"reversed": True, "recommendation": rows[-1].answer,
                            "decision": prior.get("decision")})


def restarted(decisions: Sequence[Any], stage: str, attempt: int | None = None) -> list[Reversal]:
    """Resolving rows for open reversals once `stage` (`workflow.start`) is dispatched."""
    rows = latest(decisions)
    return [Reversal(topic=d.topic, question=d.question, answer=None, decided_by="",
                     record={"restarted": stage, "restart_attempt": attempt, "recommendation": d.record.get("recommendation"),
                             "decision": d.record.get("decision")})
            for d in rows.values() if is_reversed(d)]


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
        candidates = [s for s in history if s.stage == start and s.started_at and (
            s.attempt >= attempt if attempt is not None else
            not origin.created_at or _time(s.started_at) >= _time(origin.created_at))]
        segment = min((s for s in candidates if s.ended_at and s.outcome == "done"),
                      key=lambda s: (_time(s.started_at), s.attempt), default=None)
        if segment is not None:
            result.append((origin, segment))
    return result
