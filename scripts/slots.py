"""Worker concurrency cap (E21): free-slot count for reconcile, and a claim that cannot race.

A segment with `started_at` and no `ended_at` is a running worker. Before the engine launches a
Dispatch or Review worker it claims under an exclusive lock on the Worktrees dir (same flock
pattern as `one_shot.slot`), re-counts inside the lock, refuses a duplicate segment, launches the
worker, and only then records the segment's start — so a failed spawn leaves nothing counting
against the cap, and two concurrent claims for the last slot cannot both win.
"""

from __future__ import annotations

import fcntl
import os
from collections.abc import Callable
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

import clock

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


def claim(trees: Path, store: Any, *, issue_key: str, stage: str, attempt: int,
          max_concurrency: int | None, launch: Callable[[], None],
          clock: clock.Clock = clock.system,
          runtime: str | None = None, model: str | None = None,
          effort: str | None = None, agent_type: str | None = None) -> None:
    """Claim one worker slot: recount, refuse a duplicate, launch, then record the start — or refuse."""
    with _exclusive(trees):
        n = running(store)
        if free_slots(max_concurrency, n) <= 0:
            raise ValueError(f"{n} workers running; [workers] max_concurrency is {max_concurrency}")
        if any(s.stage == stage and s.attempt == attempt for s in store.segments(issue_key)):
            raise ValueError(f"segment {issue_key} {stage} attempt {attempt} already exists")
        launch()
        store.record_segment(issue_key, stage, attempt, started_at=clock().isoformat(),
                             runtime=runtime, model=model, effort=effort, agent_type=agent_type)
