"""Claims on a tree from the sources that know its writer by process (#43).

The worker registry ties a PID to a worktree; a one-shot launcher's pidfile does the same. Each source
decides liveness itself, so the join in `orphans` only joins. File ownership rows, the third source,
are read where the tracker is parsed.
"""

from __future__ import annotations

from pathlib import Path

from orphans import Claim

Claims = dict[Path, list[Claim]]


def registry_claims(root: Path, registrations, process_exists) -> Claims:
    """`registrations` and `process_exists` come from the driver (`process_status`)."""
    out: Claims = {}
    for pid, record in registrations(root).items():
        live = process_exists(pid)
        absence = "" if live else f"has no running process (pid {pid})"
        out.setdefault((root / record["worktree"]).resolve(), []).append(Claim(record["name"], live, absence))
    return out


def one_shot_claims(trees: Path, running_trees) -> Claims:
    """A dead launcher claims nothing: its task's File ownership row still speaks for the tree.

    `running_trees` comes from the driver (`process_status`).
    """
    return {path.resolve(): [Claim(task, True)] for path, task in running_trees(trees).items()}
