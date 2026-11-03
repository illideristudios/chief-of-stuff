"""A worker's stall. Use Cases; no I/O.

Docs PR #731 places the six health signals here. Stall and stagnation are the silence signal:
no progress for the limit. `watch` is the same rule, halfway there. `flow.py` still runs its own
copy on the retiring tick. The other five signals belong to #236.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from dataclasses import dataclass


def silence(started_at: datetime, last_progress_at: datetime | None, now: datetime, limit: timedelta) -> str:
    """`ok`, `watch` or `act`. Recent progress is ok. Past half the limit with none is watch; past the limit is act."""
    if limit <= timedelta(0):
        raise ValueError("limit must be positive")
    for value in (started_at, now, last_progress_at):
        if value is not None and value.utcoffset() is None:
            raise ValueError("timestamps must include a timezone")
    # A commit from before the run (its base) or after now (a skewed clock) is no sign of life.
    if last_progress_at is not None and started_at <= last_progress_at <= now and now - last_progress_at < limit:
        return "ok"
    ran = now - started_at
    if ran < limit / 2:
        return "ok"
    if ran < limit:
        return "watch"
    return "act"


@dataclass(frozen=True)
class Evidence:
    """Plain observations. Missing liveness or a failed thread read never proves a stall."""
    alive: bool | None = None
    last_activity_at: datetime | None = None
    launcher: str = "cli"
    status: str = ""
    pending_requests: int = 0
    pending_since: datetime | None = None
    decision_wait: bool = False
    error: str = ""


def stall(started_at: datetime, now: datetime, after: timedelta, evidence: Evidence) -> str:
    """Silence is suspected, never proof. Architectural and split-trio waits do not expire.

    Ordinary requests get a full threshold to be answered. Drivers retain their first-seen
    time across passes and restarts; a missing first observation is conservatively waiting.
    """
    signal = silence(started_at, evidence.last_activity_at, now, after)
    if evidence.decision_wait:
        return "waiting"
    if evidence.error or evidence.alive is None:
        return "unknown"
    if not evidence.alive:
        return "gone"
    if now - started_at < after:
        return "ok"
    if evidence.pending_requests:
        if evidence.pending_since is None or silence(evidence.pending_since, None, now, after) != "act":
            return "waiting"
    return "suspected" if signal == "act" else signal


FAILURES = ("failed", "human_review", "stalled")


def failures(history, acknowledged=()) -> int:
    """One budget per issue, across stages and agents. A stopped attempt is not yet a relaunch."""
    return sum(bool(s.ended_at) and s.outcome in FAILURES and f"{s.stage}-{s.attempt}" not in acknowledged
               for s in history)


def failure_reason(count: int, limit: int) -> str:
    return "park: two failures on this issue" if count == limit == 2 else f"park: {count} failures on this issue (limit {limit})"
