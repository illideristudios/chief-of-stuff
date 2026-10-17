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


def open_reversal(decisions: Sequence[Any], history: Sequence[Any] = (), start: str = "implement") -> bool:
    """True when the latest row for any question is a reversal not yet restarted at `start`."""
    latest = {(d.topic, d.question): d for d in decisions}
    return any(is_reversed(d) and not is_restarted(d, history, start) for d in latest.values())


def reverse(decisions: Sequence[Any], topic: str, question: str) -> Reversal:
    """Board reversal: reopen an adopted decision so the planner routes the work again."""
    rows = [row for row in decisions if row.topic == topic and row.question == question]
    if not rows or not (rows[-1].answer or "").strip():
        raise ValueError("only an adopted decision can be reversed")
    prior = rows[-1].record if isinstance(rows[-1].record, dict) else {}
    return Reversal(topic=topic, question=question, answer=None, decided_by="",
                    record={"reversed": True, "recommendation": rows[-1].answer,
                            "decision": prior.get("decision")})


def restarted(decisions: Sequence[Any], stage: str) -> list[Reversal]:
    """Resolving rows for open reversals once `stage` (`workflow.start`) is dispatched."""
    latest = {(d.topic, d.question): d for d in decisions}
    return [Reversal(topic=d.topic, question=d.question, answer=None, decided_by="",
                     record={"restarted": stage, "recommendation": d.record.get("recommendation"),
                             "decision": d.record.get("decision")})
            for d in latest.values() if is_reversed(d)]


def replacements(decisions: Sequence[Any]) -> list[Any]:
    """Restarted decisions still requiring a replacement consultation."""
    latest = {(d.topic, d.question): d for d in decisions}
    restarted_keys = set()
    for row in decisions:
        key = (row.topic, row.question)
        if is_reversed(row):
            restarted_keys.discard(key)
        elif isinstance(row.record, dict) and row.record.get("restarted"):
            restarted_keys.add(key)
    return [d for key, d in latest.items() if key in restarted_keys and not d.answer]
