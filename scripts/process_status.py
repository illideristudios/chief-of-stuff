#!/usr/bin/env python3
"""Check worker PIDs in one call without exposing their command lines.

With no PIDs, check every registered worker and list every live one-shot run after them in the same list, each row marked `kind: one-shot`.
PIDs can be space or comma separated.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from datetime import datetime, timedelta

import runtimes
from _vendor.toon_format import encode as toon_encode
from dispatch_prompt import LINE_CAP
from git_trees import read_regular
from tracker import parse_tracker
from workspace import ConfigError, read_config, worktrees_dir

# `<pid> <task>` while a one-shot runs in this tree - the worker's own pid once it has started (the launcher's until
# then); the count behind `[workers] max_concurrency` (#99).
PIDFILE = Path(".chief-of-stuff") / "one-shot.pid"
# The pid file and the tree's name are a worker's to write, so neither is printed raw: `processes` shows a task only when a
# tracker holds it, at most this many characters; it and `result` show a tree name only when it is `[A-Za-z0-9._-]`. The readers below return them raw.
TASK_MAX = 200
TREE_NAME = re.compile(r"[A-Za-z0-9._-]+")
# A pid file holds `<pid> <item>`, and an item launches at up to LINE_CAP characters of up to 4 UTF-8 bytes: read it whole,
# or flow cannot join a long item to its row and orphans a live run (#570).
PIDFILE_MAX = 4 * LINE_CAP + 32


def registrations(root: Path) -> dict[int, dict[str, str]]:
    result = {}
    for path in sorted((root / ".chief-of-stuff" / "sessions").glob("*.json")):
        try:
            record = json.loads(path.read_text())
            pid = int(record["pid"])
            if pid > 0 and all(isinstance(record[key], str) for key in ("name", "runtime", "worktree")):
                result[pid] = record
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return result


def parse_pids(values: list[str]) -> list[int]:
    pids = []
    for value in values:
        for part in value.split(","):
            if not part.isdecimal() or int(part) <= 0:
                raise ValueError(f"invalid PID: {part!r}")
            pid = int(part)
            if pid not in pids:
                pids.append(pid)
    return pids


def process_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except (ProcessLookupError, OverflowError):  # OverflowError: a pid too large for the OS is no process
        return False


def identity(pid: int, session_id: str, *, host: bool = False) -> str | None:
    """A live process's birth time, only when its command carries this run's session id (#53, #551).

    An unavailable process view raises: it must never count as verified or as proof of completion.
    """
    if not process_exists(pid):
        return None
    try:
        probe = subprocess.run(["ps", "-ww", "-p", str(pid), "-o", "stat=,lstart=,command="],
                               capture_output=True, text=True, timeout=3)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("ps timed out verifying worker identity") from exc
    if probe.returncode and not probe.stdout.strip():
        if not process_exists(pid):
            return None
        raise RuntimeError("ps cannot verify worker identity")
    fields = probe.stdout.strip().split(None, 6)
    if len(fields) != 7:
        raise RuntimeError("ps returned no worker identity")
    if fields[0].startswith("Z") or session_id not in fields[6].split():
        return None
    if host and "-lic" in fields[6].split()[:3]:
        return None  # shell setup is not a started host; wait for its exec
    return " ".join(fields[1:6])


def pidfile_runs(trees: Path) -> list[tuple[Path, int, str]]:
    """(worktree, pid, task) from every readable pid file, live pid or not: the one parser of the pid files. `one_shot_runs`
    keeps the live ones; adoption (#99) inspects the rest. The file is untrusted: opened without blocking or following a
    symlink, a regular file only, a bounded read, a pid of ASCII digits. The task is RAW, the first line as written (the
    board joins on it) or the tree's name when empty: print it only through `main`. ponytail: pid reuse can count a dead
    worker as live; add the process start time if it bites."""
    found = []
    for f in sorted(trees.glob(f"*/{PIDFILE}")):
        try:
            data = read_regular(f, PIDFILE_MAX, nofollow=True)
            pid, _, task = ((data or b"").decode(errors="replace").splitlines() or [""])[0].partition(" ")
            if pid.isascii() and pid.isdecimal() and int(pid) > 0:
                found.append((f.parent.parent, int(pid), task.strip() or f.parent.parent.name))
        except OSError:
            continue
    return found


def one_shot_runs(trees: Path) -> list[tuple[Path, int, str]]:
    """(worktree, pid, task) for each one-shot whose worker is alive. A launcher that died, or a restart that killed it,
    holds no slot once the worker is gone too; a finished worker's tree is adopted from its evidence instead (#99)."""
    return [(tree, pid, task) for tree, pid, task in pidfile_runs(trees) if process_exists(pid)]


def tracker_texts(root: Path, warn: bool = True) -> list[str]:
    """Today's and yesterday's tracker (a run can outlive midnight), each one that exists. When the config, the zone or a
    file that is there cannot be read: none, and one stderr line unless `warn` is off."""
    try:
        cfg = read_config(root)
        today = datetime.now(cfg.zone).date()
        texts = []
        for day in (today, today - timedelta(days=1)):
            try:
                texts.append((root / cfg.tracker_path(day.isoformat())).read_text())
            except FileNotFoundError:
                continue
        return texts
    except (OSError, ConfigError, ValueError, KeyError):
        if warn:
            print("one-shot tasks not shown: the tracker could not be read", file=sys.stderr)
        return []


def known_tasks(texts: list[str]) -> set[str]:
    """The item and the name of each row in the Tasks table of these trackers: what a worker's task may be printed as."""
    return {s.strip() for text in texts for t in parse_tracker(text).tasks for s in (t.item, t.name)} - {""}


def shown_tree(name: str) -> str:
    return name if TREE_NAME.fullmatch(name) else ""


def running_trees(trees: Path) -> dict[Path, str]:
    """Each worktree whose one-shot launcher is alive, and its task."""
    return {tree: task for tree, _, task in one_shot_runs(trees)}


def check(root: Path, pids: list[int]) -> list[dict[str, str | int]]:
    registered = registrations(root)
    selected = pids if pids else list(registered)
    present = [pid for pid in selected if process_exists(pid)]
    commands: dict[int, str] = {}
    if present:
        try:
            probe = subprocess.run(["ps", "-ww", "-p", ",".join(map(str, present)),
                                    "-o", "pid=,command="], capture_output=True, text=True,
                                   timeout=3)
            for line in probe.stdout.splitlines():
                head, _, command = line.strip().partition(" ")
                if head.isdecimal():
                    commands[int(head)] = command.strip()
        except (OSError, subprocess.TimeoutExpired):
            pass
    rows = []
    for pid in selected:
        record = registered.get(pid)
        status = "gone"
        if pid in present:
            command = commands.get(pid)
            if command is None:
                status = "unverified"
            elif record is None:
                status = "running"
            else:
                binary = runtimes.binary(record["runtime"])
                status = ("running" if binary in command and record["worktree"] in command
                          else "pid_reused")
        rows.append({"pid": pid, "name": record["name"] if record else "",
                     "runtime": record["runtime"] if record else "", "status": status})
    return rows


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--root", type=Path, required=True, help="workspace containing the worker registry")
    ap.add_argument("pids", nargs="*", help="PIDs, separated by spaces or commas; omit for all workers and one-shot runs")
    args = ap.parse_args(argv)
    try:
        pids = parse_pids(args.pids)
    except ValueError as exc:
        ap.error(str(exc))
    rows: list[dict[str, str | int]] = check(args.root, pids)
    if not pids:
        try:
            runs = one_shot_runs(args.root / worktrees_dir((args.root / "CLAUDE.md").read_text()))
        except (OSError, UnicodeDecodeError):  # no CLAUDE.md, or one that cannot be decoded: no trees dir to look in
            print("one-shot runs not checked: CLAUDE.md could not be read", file=sys.stderr)
            runs = []
        known = known_tasks(tracker_texts(args.root)) if runs else set()  # printed: a task only when a tracker holds it
        rows += [{"pid": pid, "task": task[:TASK_MAX] if task in known else "", "worktree": shown_tree(tree.name), "status": "running",
                  "kind": "one-shot"} for tree, pid, task in runs]
    print(toon_encode(rows) if rows else "[]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
