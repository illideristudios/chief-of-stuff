"""A worker's stall. Use Cases; no I/O.

Docs PR #731 places the six health signals here. Stall and stagnation are the silence signal:
no progress for the limit. `watch` is the same rule, halfway there. `flow.py` still runs its own
copy on the retiring tick. The other five signals belong to #236.
"""

from __future__ import annotations

from datetime import datetime, timedelta


def silence(started_at: datetime, last_progress_at: datetime | None, now: datetime, limit: timedelta) -> str:
    """`ok`, `watch` or `act`. Recent progress is ok. Past half the limit with none is watch; past the limit is act."""
    if limit <= timedelta(0):
        raise ValueError("limit must be positive")
    for value in (started_at, now, last_progress_at):
        if value is not None and value.utcoffset() is None:
            raise ValueError("timestamps must include a timezone")
    if last_progress_at is not None and now - last_progress_at < limit:
        return "ok"
    ran = now - started_at
    if ran < limit / 2:
        return "ok"
    if ran < limit:
        return "watch"
    return "act"
