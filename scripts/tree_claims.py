"""Claims on a tree from the sources that know its writer by process (#43).

The worker registry ties a PID to a worktree; a one-shot launcher's pidfile does the same. Each source
decides liveness itself, so the join in `orphans` only joins. File ownership rows, the third source,
are read where the tracker is parsed.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
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


def engine_claims(root: Path, identity) -> Claims:
    """Keep verified engine runs owned until their store segment closes, including teardown.

    Read the engine's durable segment snapshot without loading its optional ORM into base-Python
    audit callers. Runs not yet saved by launch still have the adapter's process identities.
    An unavailable process view raises rather than proving the tree unowned.
    """
    segments = {}
    database = root / "store.db"
    if database.exists():
        with closing(sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)) as db:
            for key, stage, attempt, ended, data in db.execute(
                    "SELECT s.issue_key, s.stage, s.attempt, s.ended_at, r.data "
                    "FROM segment s LEFT JOIN resume r ON r.issue_key = s.issue_key"):
                record = (json.loads(data) if data else {}).get("runs", {}).get(f"{stage}-{attempt}", {})
                directory = root / ".chief-of-stuff/runs" / f"{int(key.rpartition('#')[2])}-{stage}-{attempt}"
                segments[directory.resolve()] = (ended, record)
    out: Claims = {}
    for path in (root / ".chief-of-stuff/runs").glob("*/started.json"):
        try:
            started = json.loads(path.read_text())
            command = json.loads((path.parent / "command.json").read_text())
            session = started["session_id"]
            if (not isinstance(session, str) or not session or session != command["session_id"]
                    or any(type(started.get(field)) is not int or started[field] <= 0
                           for field in ("pid", "runner_pid"))
                    or not isinstance(command.get("cwd"), str) or not command["cwd"].strip()):
                continue
            tree = Path(command["cwd"]).resolve()
            segment = segments.get(path.parent.resolve())
            if segment is not None:
                ended, record = segment
                if ended is not None:
                    continue
                # The engine verified both identities before saving this launch. Its open segment
                # retains ownership when the host/runner exits and while collect runs teardown.
                if (all(record.get(field) == started[field] for field in ("session_id", "pid", "runner_pid"))
                        and record.get("tree") == str(tree) and record.get("run_dir") == str(path.parent.resolve())):
                    out.setdefault(tree, []).append(Claim("engine worker", True))
                    continue
            host = identity(started["pid"], session, host=True)
            runner = identity(started["runner_pid"], session)
            if host is not None or runner is not None:
                out.setdefault(tree, []).append(Claim("engine worker", True))
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return out
