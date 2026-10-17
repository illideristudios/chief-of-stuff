"""Judgements on a finished or abandoned run. Use Cases; no I/O.

Orphan recovery (#637): the driver reads each leftover worktree and branch into an `OrphanTree`; `recovery`
decides which one to reuse, or why the choice is the user's.
"""

from __future__ import annotations

import re
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


def _label(c: OrphanTree) -> str:
    return c.path or c.branch


def _reuse(c: OrphanTree, head: str) -> Recovery:
    return Recovery(c.path, c.branch, head, c.path is None or c.dirty or c.ahead is None or c.ahead > 0 or head != c.head)


def _preferring(candidates: Sequence[OrphanTree], preferred: str | None) -> OrphanTree:
    return next((c for c in candidates if preferred and c.path == preferred), candidates[0])


def _kept(candidates: Sequence[OrphanTree], answer: str, preferred: str | None) -> Recovery | Conflict:
    """The user chose to keep one: take the one they named, else dirty work, else the preferred, as it is."""
    usable = [c for c in candidates if not (c.unreadable or c.conflicted)]
    if not usable:
        return Conflict("no orphaned worktree or branch is readable and free of merge conflicts")
    named = [c for c in usable if re.search(r"(?<![\w/.-])" + re.escape(c.branch) + r"(?![\w/.-])", answer)]
    pool = named or usable
    trees = [c for c in pool if c.path] or pool
    chosen = _preferring([c for c in trees if c.dirty] or trees, preferred)
    return _reuse(chosen, chosen.head)


def recovery(candidates: Sequence[OrphanTree], *, preferred: str | None = None,
             answer: str | None = None) -> Recovery | Conflict | None:
    """None without candidates. Otherwise the commit to resume from and the tree or branch that holds it, or the
    conflict that needs the user. Uncommitted work is never put behind a fast-forward or beside other uncommitted work."""
    if not candidates:
        return None
    if answer is not None:
        return _kept(candidates, answer, preferred)
    if bad := [c for c in candidates if c.unreadable]:
        return Conflict(f"orphaned worktree {_label(bad[0])} is detached or unreadable")
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
