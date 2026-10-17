"""Judgements on a finished or abandoned run. Use Cases; no I/O.

Orphan recovery (#637): the driver reads each leftover worktree and branch into an `OrphanTree`; `recovery`
decides which one to reuse, or why the choice is the user's.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence


@dataclass(frozen=True)
class OrphanTree:
    """One candidate: a worktree (`path`), or a branch whose directory is gone (`path` None)."""
    path: str | None
    branch: str
    head: str
    dirty: bool = False
    conflicted: bool = False
    unreadable: bool = False  # detached, unreadable, or its history could not be compared
    quarantined: bool = False  # already under the quarantine namespace or directory
    ahead: int | None = 0  # commits past the base; None when unknown
    ancestors: frozenset[str] = frozenset()  # the other candidates' heads in this head's history


@dataclass(frozen=True)
class Recovery:
    path: str | None  # None: recreate the worktree from `branch`
    branch: str
    head: str  # the commit the tree must be at; a tree behind it fast-forwards
    recovered: bool  # the work already holds something to keep


@dataclass(frozen=True)
class Conflict:
    reason: str


@dataclass(frozen=True)
class Quarantine:
    """Not reusable. The driver quarantines the candidates and starts fresh."""
    reason: str


def _label(c: OrphanTree) -> str:
    return c.path or c.branch


def _reuse(c: OrphanTree, head: str) -> Recovery:
    return Recovery(c.path, c.branch, head, c.path is None or c.dirty or c.ahead is None or c.ahead > 0 or head != c.head)


def _preferring(candidates: Sequence[OrphanTree], preferred: str | None) -> OrphanTree:
    return next((c for c in candidates if preferred and c.path == preferred), candidates[0])


def recovery(candidates: Sequence[OrphanTree], *, preferred: str | None = None) -> Recovery | Conflict | None:
    """None without candidates. Otherwise the commit to resume from and the tree or branch that holds it, or the
    conflict that needs the user. Uncommitted work is never put behind a fast-forward or beside other uncommitted work."""
    if not candidates:
        return None
    if any(c.unreadable or c.quarantined for c in candidates):
        return Quarantine("orphaned work is unreadable or quarantined")
    if bad := [c for c in candidates if c.conflicted]:
        return Conflict(f"orphaned worktree {_label(bad[0])} has unresolved merge conflicts")
    dirty = [c for c in candidates if c.dirty]
    if len(dirty) > 1:
        return Conflict("multiple orphaned worktrees have uncommitted changes")
    heads = {c.head for c in candidates}
    tips = [c for c in candidates if heads - {c.head} <= c.ancestors]
    if not tips:
        return Conflict("orphaned work has diverged commits across worktrees or branches")
    tip = tips[0].head
    if dirty and dirty[0].head != tip:
        return Conflict("orphaned worktree has uncommitted changes behind newer commits")
    # A surviving tree is fast-forwarded before a branch is re-cut.
    trees = [c for c in candidates if c.path]
    at_tip = [c for c in trees if c.head == tip] or trees or [c for c in candidates if c.head == tip]
    return _reuse(_preferring(dirty or at_tip, preferred), tip)
