"""Whether an item may be dispatched now. Use Cases; no I/O.

Dependency blockers are the forge's native relationships for this pass. Stop records are the engine's Ask and
Report rows, in the order written. File ownership never holds an item (#1039, C29): overlapping edits surface as
merge conflicts at review.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence


@dataclass(frozen=True)
class Ask:
    issue_key: str
    answer: str | None = None
    stage: str = ""
    attempt: int = 0


@dataclass(frozen=True)
class Report:
    issue_key: str
    outcome: str
    stage: str = ""
    attempt: int = 0


def why_not(issue_key: str, *, blockers: tuple[str, ...] | None, asks: Sequence[Ask],
            reports: Sequence[Report]) -> str | None:
    """Why this item must not be dispatched now, or None when it may."""
    mine_asks = [row for row in asks if row.issue_key == issue_key]
    if any(not (row.answer or "").strip() for row in mine_asks):
        return "unanswered ask"
    mine = [row for row in reports if row.issue_key == issue_key]
    # Reports arrive in the order written, so the last is the latest stop; an answer clears only its own run's stop.
    if mine and mine[-1].outcome == "blocked" and not any(
            row.stage == mine[-1].stage and row.attempt == mine[-1].attempt for row in mine_asks):
        return "worker stopped"
    if blockers is None:
        return "dependency blockers unknown"
    if blockers:
        return "blocked by " + ", ".join(blockers)
    return None
