"""Judgements on a finished or abandoned run. Use Cases; no I/O.

Done means on main, with the suite run on main: unknown does not clear. A read-only run that left
a change goes to review. Unlanded work and orphans come from the tree and its claims. A running
segment whose worker is gone is closed from that evidence instead of suppressing the planner.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from orphans import Claim, judge
from tree_state import TreeState, at_risk


@dataclass(frozen=True)
class Verdict:
    outcome: str
    reason: str
    human_review: bool = False


def done_on_main(on_main: bool | None, suite_on_main: bool | None) -> str | None:
    """None only when both facts are true. Unknown is not landed."""
    if on_main is not True:
        return "not on main" if on_main is False else "on main is unknown"
    if suite_on_main is not True:
        return "suite has not run on main" if suite_on_main is False else "suite on main is unknown"
    return None


def read_only_left_changes(read_only: bool, changed: bool | None) -> str | None:
    """A read-only run that left a commit or a file, or whose tree could not be shown clean."""
    if read_only and changed is not False:
        return "read-only run left changes"
    return None


def unlanded(state: TreeState | None) -> str | None:
    if state is None:
        return None
    reasons = at_risk(state)
    return ", ".join(reasons) if reasons else None


def orphan_reason(name: str, state: TreeState | None, claims: Sequence[Claim]) -> str | None:
    if state is None or not name:
        return None
    found = judge(name, state, list(claims))
    return None if found is None else str(found)


def abandoned(worker_alive: bool | None, *, report: str | None = None, read_only: bool = False,
              changed: bool | None = None, on_main: bool | None = None, suite_on_main: bool | None = None,
              tree: TreeState | None = None, tree_name: str = "",
              tree_claims: Sequence[Claim] = (), tree_unreadable: bool = False) -> Verdict | None:
    """How to close an open segment whose worker is gone. None while the worker may still be running."""
    if worker_alive is not False:
        return None
    why = read_only_left_changes(read_only, changed)
    if why:
        return Verdict("done", why, True)
    left = unlanded(tree)
    # Keep the explicit done outcome behind a human hold, so recovery does not retry and overwrite the delivered work.
    if report == "done" and left and on_main is not True:
        return Verdict("done", left, True)
    orphaned = orphan_reason(tree_name, tree, tree_claims)
    if orphaned:
        return Verdict("failed", orphaned, True)
    if report == "done":
        gate = done_on_main(on_main, suite_on_main)
        if gate:
            return Verdict("done", gate, True)
        return Verdict("done", "")
    if tree_unreadable:
        return Verdict("failed", "worker tree could not be read", True)
    return Verdict("failed", "worker gone")
