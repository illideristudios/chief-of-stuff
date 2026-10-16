#!/usr/bin/env python3
"""Deterministic flow engine and pipeline state machine for chief-of-stuff.

Automates routine state transitions:
1. Reconciles dead or finished worker processes into the tracker.
2. Dispatches ready one-shot tasks (when [workers] mode is 'one-shot').
3. Detects task stagnation (dead-man's switch).
"""

from __future__ import annotations

import argparse
import os
import re
import shlex
import shutil
import subprocess
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import clock
import dispatch_prompt
import git_trees
import make_worktree
import notify
import ownership
import process_status
import tracker
import task_forge
import tracker_write
from one_shot_report import UNREADABLE, read_report
from settings import Settings, SettingsError, load as load_settings
from tracker_log import ended_line, started_line
from workspace import Config, ConfigError, read_config, worktrees_dir

WORKTREE_RE = re.compile(r"worktrees?\s+`(?P<name>[^`]+)`")


def resolve_chief_bin(root: Path) -> Path:
    """Find the chief-of-stuff launcher executable or entry point."""
    pinned = os.environ.get("CHIEF_OF_STUFF_RELEASE")
    if pinned:
        cand = Path(pinned).resolve() / "chief_of_stuff.py"
        if cand.is_file():
            return cand
    cand = Path(__file__).resolve().parent.parent / "chief_of_stuff.py"
    if cand.is_file():
        return cand
    which = shutil.which("chief-of-stuff")
    if which:
        return Path(which)
    if "chief" in Path(sys.argv[0]).name:
        return Path(sys.argv[0]).resolve()
    return (root / "chief_of_stuff.py").resolve()


def live_one_shots(root: Path) -> dict[str, int]:
    """Map each live one-shot's pid-file text (its task's item) to its launcher's PID."""
    trees_path = (root / worktrees_dir((root / "CLAUDE.md").read_text())).resolve()
    return {task: pid for _, pid, task in process_status.one_shot_runs(trees_path)}


def live_workers(root: Path) -> dict[str, int]:
    """Map running task labels or session names to their live PIDs."""
    result = live_one_shots(root)
    for pid, record in process_status.registrations(root).items():
        if process_status.process_exists(pid):
            result[record["name"]] = pid
    return result


def last_worktree_commit_epoch(tree: Path) -> float | None:
    """Return the UNIX timestamp of the latest commit in the worktree, or None."""
    try:
        out = subprocess.run(["git", "-C", str(tree), "log", "-1", "--format=%ct"],
                             capture_output=True, text=True, timeout=10, check=False)
        if out.returncode == 0 and out.stdout.strip().isdigit():
            return float(out.stdout.strip())
    except (OSError, subprocess.SubprocessError):
        pass
    return None


def ready_one_shot_tasks(root: Path, tracker_text: str, settings: Settings) -> list[tracker.Task]:
    """Find unassigned open tasks eligible for one-shot launch, bounded by concurrency and file overlap."""
    return task_forge.ready_tasks(tracker_text, settings.workers.max_concurrency or 1)


def reconcile_dead_workers(root: Path, tracker_path: Path, day: str) -> list[str]:
    """Reconcile tasks in running state whose worker process is gone."""
    if not tracker_path.is_file():
        return []
    tracker_text = tracker_path.read_text()
    parsed = tracker.parse_tracker(tracker_text)
    running_tasks = [t for t in parsed.tasks if t.kind == "running"]
    if not running_tasks:
        return []

    # #570: a one-shot's pid file holds its item, not the row's name; only that text is joined to a row, never a
    # session's name, which may happen to equal some task's item (#571 review).
    row_of = tracker.launcher(parsed.tasks)
    live = {*live_workers(root), *(row_of(k)[1] for k in live_one_shots(root))}
    reconciled: list[str] = []

    for t in running_tasks:
        identifier = t.label
        if identifier in live or t.name in live or t.owner in live:
            continue

        # Process is dead. Check for report in .chief-of-stuff/reports/
        reports_dir = root / dispatch_prompt.REPORTS_DIR
        report_found = None
        if reports_dir.is_dir():
            matching_reports: list[tuple[float, dict]] = []
            for p in reports_dir.glob("*.toon"):
                try:
                    data = read_report(p, git_trees.read_regular)
                    if data.get("task") in (t.label, t.name, t.item):
                        matching_reports.append((p.stat().st_mtime, data))
                except UNREADABLE:
                    continue
            if matching_reports:
                matching_reports.sort(key=lambda item: item[0], reverse=True)
                report_found = matching_reports[0][1]

        cfg = read_config(root)
        at = tracker_write.stamp(cfg.zone)
        status = report_found.get("status") if report_found else None

        def change(text: str) -> str:
            lines = text.splitlines(keepends=True)
            matched = False
            for i, line in enumerate(lines):
                row_match = (f"| {t.name} |" in line or f"| {t.label} |" in line) and "running" in line
                if line.startswith("|") and row_match:
                    if status == "relaunch":
                        lines[i] = re.sub(r"\|\s*[^|]+\|\s*running\s+[^|]+\|", "| unassigned | open |", line)
                    elif status == "done":
                        lines[i] = re.sub(r"\|\s*[^|]+\|\s*running\s+[^|]+\|", f"| {cfg.user} | waiting |", line)
                    else:
                        lines[i] = re.sub(r"running(?:\s+\d{1,2}:\d{2})?", "orphaned", line)
                    matched = True
                    break
            if not matched:
                return text
            if status == "relaunch":
                note = f"- {at} worker {t.owner} requested relaunch: task {t.label} set to unassigned open"
            elif status == "done":
                note = f"- {at} worker {t.owner} completed: task {t.label} reconciled to waiting"
            else:
                note = f"- {at} worker {t.owner} disappeared: task {t.label} marked orphaned"
            return tracker_write.append_log("".join(lines), note, create=True)

        tracker_write.edit(tracker_path, change)
        action_desc = f"reconciled to {status or 'orphaned'}"
        reconciled.append(f"task '{t.label}' {action_desc} (worker {t.owner} dead)")

    return reconciled


def check_stagnant_tasks(root: Path, tracker_text: str, now: datetime, day: str,
                         settings: Settings, stale_minutes: int = 45) -> list[str]:
    """Detect running tasks with no progress for longer than stale_minutes."""
    parsed = tracker.parse_tracker(tracker_text)
    running = [t for t in parsed.tasks if t.kind == "running"]
    warnings: list[str] = []
    trees_path = (root / worktrees_dir((root / "CLAUDE.md").read_text())).resolve()

    for t in running:
        if not t.state_time:
            continue
        try:
            start_time = clock.hhmm(t.state_time, date.fromisoformat(day), now.tzinfo)
            if not start_time:
                continue
            ran = now - start_time
            if ran > timedelta(minutes=stale_minutes):
                # Check worktree commit freshness
                cell = ownership.cell(tracker_text, {t.label, t.name, t.item}) or ""
                m = WORKTREE_RE.search(cell)
                tree_name = m.group("name") if m else t.label
                tree_dir = trees_path / tree_name
                last_commit = last_worktree_commit_epoch(tree_dir)
                if last_commit:
                    commit_age = (now.timestamp() - last_commit) / 60
                    if commit_age >= stale_minutes:
                        warnings.append(f"stale: task '{t.label}' running {int(ran.total_seconds() // 60)}m with no commits in {int(commit_age)}m")
                else:
                    warnings.append(f"stale: task '{t.label}' running {int(ran.total_seconds() // 60)}m with no progress")
        except Exception:
            continue

    return warnings


def dispatch_ready_tasks(root: Path, day: str, tracker_path: Path, settings: Settings) -> list[str]:
    """Automatically cut worktree and spawn one-shot worker for ready tasks."""
    if not tracker_path.is_file():
        return []
    tracker_text = tracker_path.read_text()
    ready = ready_one_shot_tasks(root, tracker_text, settings)
    if not ready:
        return []

    cfg = read_config(root)
    trees_base = worktrees_dir((root / "CLAUDE.md").read_text())
    dispatched: list[str] = []

    for t in ready:
        # Generate stable tree and branch name
        safe_name = re.sub(r"[^A-Za-z0-9_-]", "-", t.label).strip("-")[:32]
        tree_name = f"flow-{safe_name}"
        branch_name = f"flow/{safe_name}"
        clone_repo = root
        if settings.repos:
            first_repo = next(iter(settings.repos.values()))
            clone_repo = root / first_repo

        try:
            declared = make_worktree.agent_types((root / "CLAUDE.md").read_text())
            # A configured profile wins; the legacy implementer fallback must be a task type.
            defaults = settings.stages.get(t.stage.strip())
            if defaults and defaults.agent:
                cut_type = defaults.agent if defaults.agent in declared else ""
            else:
                cut_type = "implementer" if declared.get("implementer") == "task" else ""
            make_worktree.build(root, clone_repo, trees_base, tree_name, branch_name, agent_type=cut_type)
        except make_worktree.RefusedError as exc:
            dispatched.append(f"refused worktree for {t.label}: {exc}")
            continue

        tree_path = (root / trees_base / tree_name).resolve()
        chief_bin = resolve_chief_bin(root)

        cmd = [sys.executable, str(chief_bin), "worker", "--one-shot", "--root", str(root),
               "--task", t.label, "--cwd", str(tree_path), "--date", day, "--name", tree_name]
        if cut_type in declared:
            cmd += ["--type", cut_type]

        try:
            subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True)
            dispatched.append(f"dispatched one-shot worker for '{t.label}' in {tree_name}")
        except OSError as exc:
            dispatched.append(f"failed to launch worker for {t.label}: {exc}")

    return dispatched


def tick(root: Path, now: datetime | None = None, day: str | None = None) -> list[str]:
    """Execute one full deterministic reconciliation and flow pass."""
    cfg = read_config(root)
    zone = cfg.zone
    current_now = now or datetime.now(zone)
    current_day = day or current_now.date().isoformat()
    tracker_path = root / cfg.tracker_path(current_day)

    settings = load_settings(root, cfg.settings_path)
    events: list[str] = []

    # 1. Reconcile dead or gone worker processes
    events.extend(reconcile_dead_workers(root, tracker_path, current_day))

    # 2. In one-shot mode, auto-dispatch ready tasks up to concurrency cap
    if settings.workers.mode == "one-shot":
        events.extend(dispatch_ready_tasks(root, current_day, tracker_path, settings))

    # 3. Check for stagnant/hung tasks
    if tracker_path.is_file():
        tracker_text = tracker_path.read_text()
        warnings = check_stagnant_tasks(root, tracker_text, current_now, current_day, settings)
        events.extend(warnings)
        if warnings:
            try:
                notifier = notify.adapter_for(settings, root)
                for w in warnings:
                    notifier.add(notify.Row("awaiting", "Stagnant task alert", w))
            except Exception:
                pass

    return events


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--root", type=Path, default=Path("."), help="workspace root holding CLAUDE.md")
    ap.add_argument("--date", help="YYYY-MM-DD; default today in workspace timezone")
    args = ap.parse_args(argv)

    root = args.root.resolve()
    configured = os.environ.get("CHIEF_OF_STUFF_WORKSPACE")
    if (root == Path(".").resolve() or not (root / "CLAUDE.md").is_file()) and configured:
        root = Path(configured).resolve()
    claude_md = root / "CLAUDE.md"
    if not claude_md.is_file() or not re.search(r"^## Coordinator\s*$", claude_md.read_text(), re.M):
        return 0
    try:
        events = tick(root, day=args.date)
        if events:
            for ev in events:
                print(ev)
        else:
            print("flow: all tasks current; no actions needed")
        return 0
    except (ConfigError, SettingsError, OSError) as exc:
        print(f"flow error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
