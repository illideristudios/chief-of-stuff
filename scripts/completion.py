"""Judgements on a finished or abandoned run. Use Cases; no I/O.

Landing means on main, with the suite run there: unknown does not clear. Stage completion can
precede landing. Before landing, dirty files and commits absent from remote refs need recovery;
inherited, published PR commits do not. A verified landing of this tree's HEAD and a clean tree
clear squash history even after branch deletion. Missing tree evidence requires a human hold without
changing the reported outcome. Retryable and blocked runs keep their outcome and tree unless the tree is
unreadable. Final completion also judges orphans.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from model_port import RETRYABLE
from orphans import Claim, judge
from tree_state import TreeState, at_risk


@dataclass(frozen=True)
class Verdict:
    outcome: str
    reason: str
    human_review: bool = False
    suite_hold: bool = False


def done_on_main(on_main: bool | None, suite_on_main: bool | None) -> str | None:
    """None only when both facts are true. Unknown is not landed."""
    if on_main is not True:
        return "not on main" if on_main is False else "on main is unknown"
    if suite_on_main is not True:
        return "suite has not run on main" if suite_on_main is False else "suite on main is unknown"
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


def abandoned(worker_alive: bool | None, *, report: str | None = None,
              on_main: bool | None = None, suite_on_main: bool | None = None,
              tree: TreeState | None = None, tree_name: str = "",
              tree_claims: Sequence[Claim] = (), tree_unreadable: bool = False,
              landing_required: bool = True, tree_landed: bool | None = None) -> Verdict | None:
    """Close a gone worker using the module contract; holds preserve its report or failed outcome."""
    if worker_alive is not False:
        return None
    outcome = report or "failed"
    if tree_unreadable:
        return Verdict(outcome, "worker tree could not be read", True)
    if report in (*RETRYABLE, "blocked"):
        return Verdict(report, "")
    if not landing_required:
        left = []
        if tree is not None:
            if tree.dirty:
                left.append(f"{tree.dirty} uncommitted")
            if tree.unpushed is None:
                left.append("unpushed commits unknown")
            elif tree.unpushed:
                left.append(f"{tree.unpushed} unpushed")
        reason = ", ".join(left) if left else ("" if report == "done" else "worker gone")
        return Verdict(outcome, reason, bool(left))
    left = unlanded(tree)
    landed = (tree_landed is True and tree is not None and tree.dirty == 0
              and done_on_main(on_main, suite_on_main) is None)
    # Preserve explicit completion behind a hold; retrying could overwrite the delivered work.
    if report == "done" and not landed and (left or tree_landed is False):
        return Verdict("done", left or "tree HEAD is outside the verified landing", True)
    orphaned = None if landed and report == "done" else orphan_reason(tree_name, tree, tree_claims)
    if orphaned:
        return Verdict("failed", orphaned, True)
    if report == "done":
        gate = done_on_main(on_main, suite_on_main)
        if gate:
            return Verdict("done", gate, True)
        return Verdict("done", "")
    return Verdict("failed", "worker gone")
