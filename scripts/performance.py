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
    workflow: str = ""


@dataclass(frozen=True)
class Record:
    issue_key: str
    stage: str
    attempt: int
    runtime: str
    model: str
    effort: str
    agent_type: str
    workflow: str
    started_at: str
    ended_at: str | None
    outcome: str | None
    completed: bool
    human_review: bool
    relaunched: bool
    minutes: int | None


def records(segments: Iterable[Any]) -> list[Record]:
    """Raw attempt records; no success definition, rate or routing recommendation.

    Start/end timestamps and the original outcome remain available alongside the whole-minute duration.
    An older segment's unknown workflow stays empty instead of being inferred from today's issue or policy.
    """
    return [Record(issue_key=getattr(seg, "issue_key", ""), stage=getattr(seg, "stage", ""),
                   attempt=seg.attempt, runtime=seg.runtime or "", model=seg.model or "",
                   effort=seg.effort or "", agent_type=seg.agent_type or "", workflow=getattr(seg, "workflow", "") or "",
                   started_at=seg.started_at, ended_at=seg.ended_at, outcome=seg.outcome,
                   completed=seg.outcome == "done",
                   human_review=seg.outcome == "human_review" or getattr(seg, "required_human_review", False),
                   relaunched=seg.attempt > 1,
                   minutes=int((_parse(seg.ended_at) - _parse(seg.started_at)).total_seconds() // 60)
                   if seg.ended_at else None)
            for seg in segments]


def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts)


def _median_minutes(durations: list[int]) -> int | None:
    if not durations:
        return None
    xs = sorted(durations)
    return xs[(len(xs) - 1) // 2]  # lower middle when even


def table(segments: Iterable[Any], since: date, until: date, by: str = "day",
          zone: ZoneInfo = ZoneInfo("UTC")) -> list[Row]:
    """Per period, entry, declared agent type and workflow: raw counts and median whole minutes."""
    if by not in ("day", "week"):
        raise ValueError(f"by must be 'day' or 'week', not {by!r}")
    groups: dict[tuple[date, str, str, str, str, str], list[Record]] = defaultdict(list)
    for seg in records(segments):
        start_day = _parse(seg.started_at).astimezone(zone).date()
        if start_day < since or start_day > until:
            continue
        period = start_day if by == "day" else start_day - timedelta(days=start_day.weekday())
        key = (period, seg.runtime, seg.model, seg.effort, seg.agent_type, seg.workflow)
        groups[key].append(seg)

    rows: list[Row] = []
    for (period, runtime, model, effort, agent_type, workflow), segs in sorted(groups.items()):
        rows.append(Row(period=period, runtime=runtime, model=model, effort=effort, agent_type=agent_type,
                        workflow=workflow, runs=len(segs), completed=sum(seg.completed for seg in segs),
                        human_review=sum(seg.human_review for seg in segs), relaunched=sum(seg.relaunched for seg in segs),
                        median_minutes=_median_minutes([seg.minutes for seg in segs if seg.minutes is not None])))
    return rows
