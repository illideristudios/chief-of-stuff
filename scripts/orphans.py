"""Whose is a tree at risk, and are they still here (#43). Pure: the join, and nothing that asks.

Each claim source (the Log's started lines, the worker registry, one-shot launchers) decides its own
claim's liveness, because only it knows how. Any live claim holds the tree: a false orphan sends the
user after a session that is working. A tree no source claims is `Unclaimed` — owner unknown is a
different fact from owner gone, and the tree may be the user's own.
"""

from __future__ import annotations

from dataclasses import dataclass

from findings import ORPHANED, UNCLAIMED
from tree_state import TreeState, at_risk


@dataclass(frozen=True)
class Claim:
    owner: str
    live: bool
    absence: str = ""  # how the owner is gone: "is not in ## Sessions", "has no process (pid 4821)"
    ref: str = ""


@dataclass(frozen=True)
class Orphan:
    """Work at risk whose every claimant is gone."""

    worktree: str
    owner: str
    why: str
    absence: str
    ref: str = ""

    def __str__(self) -> str:
        who = f"{self.owner!r} [{self.ref}]" if self.ref else f"{self.owner!r}"
        return f"{ORPHANED} {self.worktree} — {self.why}, and its owner {who} {self.absence}"


@dataclass(frozen=True)
class Unclaimed:
    """Work at risk in a tree nothing names."""

    worktree: str
    why: str

    def __str__(self) -> str:
        return (f"{UNCLAIMED} {self.worktree} — {self.why}; "
                "no started line, worker registration or one-shot names this tree")


def judge(name: str, state: TreeState, claims: list[Claim]) -> Orphan | Unclaimed | None:
    reasons = at_risk(state)
    if not reasons or any(c.live for c in claims):
        return None
    tree, why = f"{name} ({state.branch})", ", ".join(reasons)
    if not claims:
        return Unclaimed(tree, why)
    gone = claims[0]
    return Orphan(tree, gone.owner, why, gone.absence, gone.ref)
