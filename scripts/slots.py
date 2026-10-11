"""Worker concurrency cap (E21): free-slot count for reconcile, and a claim that cannot race.

A segment with `started_at` and no `ended_at` is a running worker. Before the engine launches a
Dispatch or Review worker it claims under an exclusive lock on the Worktrees dir (same flock
pattern as `one_shot.slot`), re-counts inside the lock, records the new segment's start, and
refuses when full so two concurrent claims for the last slot cannot both win.
"""

from __future__ import annotations

import fcntl
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

# No `[workers] max_concurrency`: treat as this many free slots so reconcile can dispatch freely.
UNBOUNDED = 10**9


def free_slots(max_concurrency: int | None, running: int) -> int:
    """How many workers may still start. No cap means UNBOUNDED; never negative."""
    cap = UNBOUNDED if max_concurrency is None else max_concurrency
    return max(0, cap - running)


def running(store: Any) -> int:
    """Open segments in the store: started, not ended."""
    return sum(1 for item in store.items()
               for seg in store.segments(item.issue_key)
               if seg.ended_at is None)


@contextmanager
def _exclusive(path: Path) -> Iterator[None]:
    """Hold an exclusive flock on a directory (the Worktrees dir), same shape as one_shot.slot."""
    lock = os.open(path, os.O_RDONLY)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield
    finally:
        os.close(lock)


def claim(trees: Path, store: Any, *, issue_key: str, stage: str, attempt: int, started_at: str,
          max_concurrency: int | None) -> None:
    """Claim one worker slot: recount and record the segment start under the Worktrees flock, or refuse."""
    with _exclusive(trees):
        n = running(store)
        if free_slots(max_concurrency, n) <= 0:
            raise ValueError(f"{n} workers running; [workers] max_concurrency is {max_concurrency}")
        store.record_segment(issue_key, stage, attempt, started_at=started_at)
