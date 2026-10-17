"""Agent performance rows from stage segments (architecture §9). Reads no file; callers pass the rows in."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Iterable
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class Row:
    period: date
    runtime: str
    model: str
    effort: str
    agent_type: str
    runs: int
    completed: int
    human_review: int
    relaunched: int
    median_minutes: int | None


def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts)


def _median_minutes(durations: list[int]) -> int | None:
    if not durations:
        return None
    xs = sorted(durations)
    return xs[(len(xs) - 1) // 2]  # lower middle when even


def _run(seg: Any) -> tuple:
    return tuple(getattr(seg, name, default) for name, default in (
        ("issue_key", ""), ("stage", ""), ("persona", ""), ("review_round", 0), ("finding_key", "")))


def table(segments: Iterable[Any], since: date, until: date, by: str = "day",
          zone: ZoneInfo = ZoneInfo("UTC")) -> list[Row]:
    """Per period and runtime/model/effort/agent_type: runs, outcomes, and median whole minutes."""
    if by not in ("day", "week"):
        raise ValueError(f"by must be 'day' or 'week', not {by!r}")
    groups: dict[tuple[date, str, str, str, str], list[Any]] = defaultdict(list)
    segments = list(segments)
    # A panel numbers attempts across personas, so only an earlier attempt of the same review or fix is a relaunch.
    first: dict[tuple, int] = {}
    for seg in segments:
        first[_run(seg)] = min(seg.attempt, first.get(_run(seg), seg.attempt))
    for seg in segments:
        start_day = _parse(seg.started_at).astimezone(zone).date()
        if start_day < since or start_day > until:
            continue
        period = start_day if by == "day" else start_day - timedelta(days=start_day.weekday())
        key = (period, seg.runtime or "", seg.model or "", seg.effort or "", seg.agent_type or "")
        groups[key].append(seg)

    rows: list[Row] = []
    for (period, runtime, model, effort, agent_type), segs in sorted(groups.items()):
        durations: list[int] = []
        completed = human_review = relaunched = 0
        for seg in segs:
            if seg.outcome == "done":
                completed += 1
            if seg.outcome == "human_review" or getattr(seg, "required_human_review", False):
                human_review += 1
            if seg.attempt > first[_run(seg)]:
                relaunched += 1
            if seg.ended_at:
                minutes = int((_parse(seg.ended_at) - _parse(seg.started_at)).total_seconds() // 60)
                durations.append(minutes)
        rows.append(Row(period=period, runtime=runtime, model=model, effort=effort, agent_type=agent_type,
                        runs=len(segs), completed=completed, human_review=human_review,
                        relaunched=relaunched, median_minutes=_median_minutes(durations)))
    return rows
