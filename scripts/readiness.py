"""Whether an item may be dispatched now. Use Cases; no I/O.

File ownership is the pass's claim rows (store.Ownership). Dependency blockers are the forge's
native relationships for this pass. Stop records are the engine's Ask and Report rows.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

import ownership


@dataclass(frozen=True)
class Claim:
    issue_key: str
    path: str
    mode: str = "write"  # "write" or "read"; a read claim owns nothing


@dataclass(frozen=True)
class Ask:
    issue_key: str
    answer: str | None = None


@dataclass(frozen=True)
class Report:
    issue_key: str
    outcome: str
    stage: str = ""
    attempt: int = 0


def _cell(paths: Sequence[str]) -> str:
    return ", ".join(paths)


def _conflict(issue_key: str, claims: Sequence[Claim], running: Sequence[str]) -> str | None:
    mine = [c.path for c in claims if c.issue_key == issue_key and c.mode != "read"]
    if not mine:
        return None
    mine_cell = _cell(mine)
    for other in running:
        if other == issue_key:
            continue
        theirs = [c.path for c in claims if c.issue_key == other and c.mode != "read"]
        if theirs and ownership.overlaps(mine_cell, _cell(theirs)):
            return f"file ownership conflict with {other}"
    return None


def why_not(issue_key: str, *, claims: Sequence[Claim], running: Sequence[str],
            blockers: tuple[str, ...] | None, asks: Sequence[Ask], reports: Sequence[Report]) -> str | None:
    """Why this item must not be dispatched, or None when it may."""
    mine_asks = [row for row in asks if row.issue_key == issue_key]
    if any(not (row.answer or "").strip() for row in mine_asks):
        return "unanswered ask"
    if not mine_asks:
        mine = [row for row in reports if row.issue_key == issue_key]
        if mine and mine[-1].outcome == "blocked":
            return "worker stopped"
    if blockers is None:
        return "dependency blockers unknown"
    if blockers:
        return "blocked by " + ", ".join(blockers)
    return _conflict(issue_key, claims, running)
