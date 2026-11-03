#!/usr/bin/env python3
"""Print a finished one-shot's report, so the coordinator never opens a file under the worker's tree.

With no `--task`, one TOON list of the runs: task, worker, worktree, status, and whether the tracker took the run
(`reconciled`). With `--task <task>`, that task's newest report, exit 1 when there is none. It reads only the launcher's own
copies, `<root>/.chief-of-stuff/reports/<tree>.toon`, which nothing in a worker's tree can change.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from _vendor.toon_format import encode as toon_encode
from dispatch_prompt import CONTROL, REPORT_KEYS, REPORTS_DIR, TRACKER_ERROR
from git_trees import read_regular
from one_shot_report import UNREADABLE, read_report
from process_status import shown_tree, tracker_texts
from tracker import parse_tracker

def clean(value: object) -> object:
    """A value with control characters (but newlines and tabs) removed; a mapping is not the launcher's shape."""
    if isinstance(value, str):
        return CONTROL.sub("", value)
    if isinstance(value, list):
        return [clean(v) for v in value]
    return None if isinstance(value, dict) else value


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--root", type=Path, required=True, help="workspace whose launcher kept the reports")
    ap.add_argument("--task", help="the task whose report to print: its item, or its name in the Tasks table")
    args = ap.parse_args(argv)
    runs, skipped = [], 0  # (mtime, tree name, report) of each report that can be read
    for path in sorted((args.root / REPORTS_DIR).glob("*.toon")):
        try:
            runs.append((path.stat().st_mtime, path.stem, read_report(path, read_regular)))
        except UNREADABLE:
            skipped += 1
    if skipped:
        print(f"one-shot reports skipped: {skipped} could not be read", file=sys.stderr)

    if args.task is not None:
        task = args.task.strip()
        items = {t.item.strip() for text in tracker_texts(args.root, warn=False)
                 for t in parse_tracker(text).tasks if task and t.name.strip() == task} - {""}  # a name two rows share maps to nothing
        found = [r for r in runs if r[2]["task"] == task] or [r for r in runs if len(items) == 1 and r[2]["task"] in items]
        if not found:
            print(f"no one-shot report for task '{task}'", file=sys.stderr)
            return 1
        _, tree, data = max(found, key=lambda r: r[0])
        print(toon_encode({**{key: clean(data.get(key)) for key in REPORT_KEYS}, "worktree": shown_tree(tree)}))
        return 0
    rows = [{"task": clean(data["task"]), "worker": clean(data["worker"]), "worktree": shown_tree(tree),
             "status": clean(data["status"]),
             "reconciled": isinstance(data.get("errors"), list)
             and not any(str(e).startswith(TRACKER_ERROR) for e in data["errors"])} for _, tree, data in runs]
    print(toon_encode(rows) if rows else "[]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
