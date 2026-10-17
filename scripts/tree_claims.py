"""Claims on a tree from the sources that know its writer by process (#43).

The worker registry ties a PID to a worktree; a one-shot launcher's pidfile does the same. Each source
decides liveness itself, so the join in `orphans` only joins. File ownership rows, the third source,
are read where the tracker is parsed.
"""

from __future__ import annotations

import json
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


def _same_launch(record: dict, started: dict, tree: Path, started_path: Path) -> bool:
    recorded = record.get("tree")
    try:
        same_tree = isinstance(recorded, str) and Path(recorded).resolve() == tree
    except OSError:
        same_tree = False
    return (all(record.get(field) == started[field] for field in ("session_id", "pid", "runner_pid"))
            and same_tree and record.get("run_dir") == str(started_path.parent.resolve()))


def engine_claims(root: Path, identity, segments) -> Claims:
    """Keep verified engine runs owned until their store segment closes, including teardown.

    `segments` is `(ended_at, resume run entry)` from the store, already carrying `run_dir`.
    An open segment stays claimed when that launch matches, and when `exit.json` is on disk,
    until collection closes it. A closed segment does not hide a later process in the same directory.
    An unavailable process view raises rather than proving the tree unowned.
    """
    located: dict[Path, list[tuple]] = {}
    for ended, record in segments:
        if not isinstance(record, dict):
            continue
        run_dir = record.get("run_dir")
        if isinstance(run_dir, str) and run_dir.strip():
            located.setdefault(Path(run_dir).resolve(), []).append((ended, record))
    out: Claims = {}
    for path in (root / ".chief-of-stuff/runs").glob("*/started.json"):
        try:
            started = json.loads(path.read_text())
            command = json.loads((path.parent / "command.json").read_text())
            if not isinstance(started, dict) or not isinstance(command, dict):
                continue
            session = started["session_id"]
            if (not isinstance(session, str) or not session or session != command["session_id"]
                    or any(type(started.get(field)) is not int or started[field] <= 0
                           for field in ("pid", "runner_pid"))
                    or not isinstance(command.get("cwd"), str) or not command["cwd"].strip()):
                continue
            tree = Path(command["cwd"]).resolve()
            open_records = [record for ended, record in located.get(path.parent.resolve(), []) if ended is None]
            if open_records and any(_same_launch(record, started, tree, path) for record in open_records):
                out.setdefault(tree, []).append(Claim("engine worker", True))
                continue
            # exit.json covers the crash window before resume saves a session, not a different launch.
            if (open_records and (path.parent / "exit.json").exists()
                    and all(not record.get("session_id") for record in open_records)):
                out.setdefault(tree, []).append(Claim("engine worker", True))
                continue
            host = identity(started["pid"], session, host=True)
            runner = identity(started["runner_pid"], session)
            if host is not None or runner is not None:
                out.setdefault(tree, []).append(Claim("engine worker", True))
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return out
