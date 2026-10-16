#!/usr/bin/env python3
"""Preview or sync a tracked issue's Kanban stage without replacing unrelated labels."""

from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path
import sys

import backlog
import forge
import kanban
import tracker_log
from _vendor.toon_format import encode as toon_encode
from tracker import parse_tracker
from workspace import ConfigError, read_config
from settings import SettingsError, load as load_settings


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--root", type=Path, required=True, help="workspace holding CLAUDE.md and the tracker")
    ap.add_argument("--date", help="tracker date, YYYY-MM-DD; defaults to today in the workspace timezone")
    ap.add_argument("--issue", help="one tracked issue reference, required with --commit")
    ap.add_argument("--task", help="with --issue and several rows on it: the tracker row's name or item number (#69)")
    ap.add_argument("--from-stage", help="stage last observed on the issue or project")
    ap.add_argument("--initialize", action="store_true", help="start an unlabeled issue or project item")
    ap.add_argument("--release-hold", action="store_true", help="release a launcher hold; retain stage-required holds")
    ap.add_argument("--commit", action="store_true", help="apply one verified move; default is preview")
    args = ap.parse_args(argv)
    if args.release_hold and (not args.issue or not args.from_stage or args.initialize):
        print("kanban: --release-hold requires --issue and --from-stage; forbids --initialize", file=sys.stderr)
        return 2
    if args.commit and not args.issue:
        print("kanban: --commit requires --issue; bulk updates are preview only", file=sys.stderr)
        return 2
    if args.commit and not (args.from_stage or args.initialize):
        print("kanban: --commit requires --from-stage or --initialize", file=sys.stderr)
        return 2
    if args.from_stage and args.initialize:
        print("kanban: choose --from-stage or --initialize", file=sys.stderr)
        return 2
    if args.task and not args.issue:
        print("kanban: --task needs --issue; it names one row of that issue", file=sys.stderr)
        return 2
    try:
        cfg = read_config(args.root)
        settings = load_settings(args.root, cfg.settings_path)
        if settings.kanban is None:
            print("kanban: no [kanban] section in the workspace settings", file=sys.stderr)
            return 2
        day = args.date or datetime.now(cfg.zone).date().isoformat()
        text = (args.root / cfg.tracker_path(day)).read_text()
        tracker = parse_tracker(text)
    except (OSError, ConfigError, SettingsError) as exc:
        print(f"kanban: {exc}", file=sys.stderr)
        return 2
    wanted = backlog.parse_issue_arg(args.issue, cfg.backlog) if args.issue else None
    if args.issue and wanted is None:
        print(f"kanban: invalid issue reference {args.issue!r}", file=sys.stderr)
        return 2
    selected = []
    for task in tracker.tasks:
        if task.standing or (task.kind == "done" and not wanted) or not task.lane.strip() or not task.issue.strip():
            continue
        ref = backlog.issue_ref(task.issue, cfg.backlog)
        if ref is None or (wanted and ref != wanted):
            continue
        selected.append((task, ref))
    if wanted and not selected:
        print(f"kanban: {args.issue} is not a tracked task with a lane", file=sys.stderr)
        return 2
    if args.task:
        # #69: a --task names one row of the issue, by the row's name or its tracker item number.
        picked = [(task, ref) for task, ref in selected
                  if task.name.strip() == args.task
                  or task.item.strip().lstrip("#") == args.task.strip().lstrip("#")]
        if not picked:
            print(f"kanban: no task named {args.task!r} on {args.issue}", file=sys.stderr)
            return 2
        selected = picked
    elif len({ref for _, ref in selected}) != len(selected):
        # #69: rows sharing an issue cannot be auto-picked; an issue carries one stage label.
        seen = set()
        for task, ref in selected:
            if ref in seen:
                names = ", ".join(f"{t.label!r} (item {t.item.strip()})" for t, r in selected if r == ref)
                print(f"kanban: multiple tracker tasks point to {ref.label(cfg.backlog)}: {names}; "
                      f"name one with --task", file=sys.stderr)
                return 2
            seen.add(ref)
    rows = []
    for task, ref in selected:
        keep_hold = False
        if args.release_hold:
            shared, keep_hold = kanban.shared_issue(settings.kanban, tracker.tasks, cfg.backlog, ref,
                completed=tracker_log.completed_tasks(text), releasing=task)
            task = shared or task
        stage = task.stage.strip()
        if not stage:
            result = kanban.Sync(ref, stage, error="tracker stage is blank")
        else:
            result = forge.sync_stage(ref, cfg.backlog, settings.kanban, stage,
                                      expected=args.from_stage, initialize=args.initialize, commit=args.commit,
                                      release_hold=args.release_hold, keep_hold=keep_hold)
        changed = result.delta.changed or result.project_add or result.current_status != result.project_status
        rows.append({"issue": ref.label(cfg.backlog), "stage": stage,
                     "add": ", ".join(result.delta.add), "remove": ", ".join(result.delta.remove),
                     "project_status": result.project_status or "",
                     "action": ("error" if result.error else "unchanged" if not changed else
                                "updated" if result.done else "would update"),
                     "error": result.error})
    print(toon_encode(rows) if rows else "[]")
    return 1 if any(row["error"] for row in rows) else 0


if __name__ == "__main__":
    raise SystemExit(main())
