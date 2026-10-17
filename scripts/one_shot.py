#!/usr/bin/env python3
"""Run one scoped task in one CLI turn, then reconcile its result."""

from __future__ import annotations

import fcntl
import json
import os
import re
import shlex
import signal
import subprocess
from contextlib import contextmanager, suppress
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, NamedTuple

import backlog
import dispatch_prompt
import forge
import git_trees
import git_view
import kanban
import ownership
import runtimes
import tracker_write
from _vendor.toon_format import ToonDecodeError, decode as toon_decode, encode as toon_encode
from md import cells as _cells, is_separator as _is_separator
from reconcile import suite_holds
from model_port import quota_reset
from notify_service import atomic_write
from one_shot_report import FIELD_MAX, UNREADABLE, read_report
from process_status import PIDFILE, PIDFILE_MAX, pidfile_runs, process_exists, running_trees
from tracker import Task, parse_tracker
from tracker_log import (RELAUNCHED, TIMED_OUT, ended_line, hold_failed_line, relaunch_line,
                         started_line, teardown_failed_line, timed_out_line)
from workspace import settings_path, worktrees_dir
from settings import load as load_settings
from shell_setup import clean_env, login_argv, resolve

RESULT = Path(dispatch_prompt.RESULT_FILE)
REPORT = Path(dispatch_prompt.PROMPT_DIR) / "one-shot-report.toon"  # the tree's copy; `result` reads the launcher's own
NO_COMMITS = "No new commits detected"
BOOTSTRAP = ("Read {dispatch} first. It is your entire one-shot assignment. Work in {cwd}. "
             "Finish in this invocation and write the requested TOON result before exiting.")


def command(runtime: str, binary: str, cwd: Path, dispatch: Path, *, agent_type: str | None,
            model: str, effort: str, lead: list[str] = (), expected_common: Path | None = None) -> list[str]:
    """A single foreground CLI invocation, without interactive plan or mailbox modes."""
    prompt = BOOTSTRAP.format(dispatch=dispatch, cwd=cwd)
    tail: list[str] = []
    if runtime == "claude":
        argv = [binary, "--print", "--permission-mode", "auto", "--permission-prompts", "none",
                "--no-session-persistence", "--plugin-dir", str(Path(__file__).resolve().parent.parent)]
        if agent_type:
            argv += ["--agent", agent_type]
    elif runtime == "codex":
        # `never` returns blocked operations to the model instead of waiting for a human.
        argv = [binary, "-a", "never", "exec", "-C", str(cwd), "--sandbox", "workspace-write",
                # Network for fetch, push, and forge APIs; file writes stay confined.
                "-c", "sandbox_workspace_write.network_access=true", "--ephemeral"]
        argv += git_trees.codex_add_dir_args(cwd, expected_common=expected_common)
    elif runtime == "cursor":
        argv = [binary, "--print", "--force", "--trust", "--workspace", str(cwd)]
    elif runtime == "agy":
        argv = [binary, "--mode", "accept-edits", "--dangerously-skip-permissions"]
        # agy's --print takes the prompt as its value, so it goes last, right before the prompt.
        tail = ["--print"]
    else:
        raise ValueError(f"unknown runtime {runtime}")
    flags = [*(["--model", model] if model else []), *runtimes.get(runtime).effort_tokens(effort)]
    return [argv[0], *lead, *argv[1:], *flags, *tail, prompt]


def worker_result(path: Path, exit_code: int) -> tuple[str, str, str, dict[str, int | None]]:
    """Return (status, reason, changes, counts); never infer success from the CLI exit code or free-form final response."""
    counts = dict.fromkeys(dispatch_prompt.SUITE_KEYS)
    try:
        raw = path.read_text()
    except OSError as exc:
        return "human_review", f"worker did not write a valid TOON result ({exc}); exit {exit_code}", "", counts
    try:
        data = toon_decode(raw)
    except (ToonDecodeError, ValueError, TypeError) as exc:
        try:  # some workers write JSON instead of TOON (#154); same fields either way
            data = json.loads(raw)
        except ValueError:
            return "human_review", f"worker wrote an unreadable TOON result ({exc}); exit {exit_code}: {_brief(raw)}", "", counts
    if isinstance(data, dict):
        counts = dispatch_prompt.suite_counts(data)
    if not isinstance(data, dict) or data.get("status") not in ("done", "human_review", "relaunch") or any(
        not isinstance(data.get(k), str) or not data[k].strip() for k in ("reason", "changes")
    ):
        return "human_review", f"worker result is incomplete or invalid; exit {exit_code}", "", counts
    if exit_code:
        return "human_review", f"worker exited {exit_code}: {data['reason']}", data["changes"], counts
    return data["status"], data["reason"], data["changes"], counts


QUOTA_STOP = "quota stop: "
_QUOTA_TAIL = 65536  # the worker's stderr is read for this one decision only, and only its tail


def quota_marker(runtime: str, model: str) -> str:
    """The launcher's quota-stop marker exactly as the Log line carries it: the writer and the cap both read this text."""
    return _brief(f"{QUOTA_STOP}{runtime} {model}")


def quota_stop(log: Path, runtime: str, model: str) -> str:
    """The relaunch reason when the worker's stderr shows an exhausted quota (a 429 with RESOURCE_EXHAUSTED or quota), else "".
    A missing or unreadable log grants nothing. The caller applies it only to an unchanged tree with no result (#422)."""
    try:
        with log.open("rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - _QUOTA_TAIL))
            text = f.read().decode(errors="replace")
    except OSError:
        return ""
    reset = quota_reset(text)  # the detector lives with the model port (ledger E5)
    return "" if reset is None else quota_marker(runtime, model) + (f"; {reset}" if reset else "")


def _enclosing_repository(cwd: Path) -> Path | None:
    """The nearest ancestor of cwd holding a `.git` entry, or None (#560): a one-shot working
    in a subfolder of a repository commits and reads there. Filesystem lookups only — the
    resolved location is still read through the sanitised view."""
    try:
        parents = cwd.resolve().parents
    except (OSError, RuntimeError):
        return None
    return next((parent for parent in parents if os.path.lexists(parent / ".git")), None)


def _git(args: list[str], tree: Path, *, expected_common: Path | None = None) -> tuple[subprocess.CompletedProcess[str] | None, Exception | None]:
    """Keep a refused or failed view separate from Git's own exit status. Only an absent
    layout (a plain folder below a repository) resolves to the enclosing repository; an
    unreadable one is never quietly replaced with another tree's view."""
    try:
        options = {"expected_common": expected_common} if expected_common is not None else {}
        return git_view.run(args, tree, env=git_trees.audit_env(), timeout=15, **options), None
    except (git_view.Unviewable, git_view.CacheError, OSError, subprocess.TimeoutExpired) as exc:
        if not isinstance(exc, git_view.NotARepository):
            return None, exc
        root = _enclosing_repository(tree)
        if root is None:
            return None, exc
        try:
            return git_view.run(args, root, env=git_trees.audit_env(), timeout=15, **options), None
        except (git_view.Unviewable, git_view.CacheError, OSError, subprocess.TimeoutExpired) as enclosed:
            return None, enclosed


def changed_files(cwd: Path, *, expected_common: Path | None = None) -> str:
    out, error = _git(["status", "--short"], cwd, expected_common=expected_common)
    if out is None:
        if isinstance(error, git_view.NotARepository):  # a plain folder has no change list to report (#559)
            return ""
        return f"git status failed: {'unreadable: ' if isinstance(error, git_view.Unviewable) else ''}{error}"
    return out.stdout.strip() if out.returncode == 0 else f"git status failed: {out.stderr.strip()}"


def git_head(cwd: Path, *, strict: bool = False, expected_common: Path | None = None) -> str | None:
    out, error = _git(["rev-parse", "HEAD"], cwd, expected_common=expected_common)
    if out is not None and out.returncode == 0:
        return out.stdout.strip()
    if strict:
        if isinstance(error, git_view.NotARepository):  # a plain folder has no HEAD to lose
            return None
        # A missing symbolic branch is unborn; other failures cannot authorize a launch.
        if out is not None:
            ref, error = _git(["symbolic-ref", "-q", "HEAD"], cwd, expected_common=expected_common)
            if ref is not None and ref.returncode == 0 and ref.stdout.strip():
                refs, error = _git(["for-each-ref", "--format=%(refname)", ref.stdout.strip()], cwd, expected_common=expected_common)
                if refs is not None and refs.returncode == 0 and not refs.stdout.strip():
                    return None
        if isinstance(error, git_view.CacheError):
            raise ValueError(f"git view cache configuration error: {error}") from None
        raise ValueError("tree is unreadable")
    return None


def committed_changes(cwd: Path, before: str | None, *, expected_common: Path | None = None) -> str:
    after = git_head(cwd, expected_common=expected_common)
    if not before or before == after:
        return NO_COMMITS
    if after is None:
        return "Could not read commits: tree unreadable"
    out, error = _git(["log", "--format=%h %s", "--stat", f"{before}..{after}"], cwd, expected_common=expected_common)
    if out is None:
        return "Could not read commits: tree unreadable" if isinstance(error, git_view.Unviewable) else f"Could not read commits: {error}"
    return out.stdout.strip()[:12000] if out.returncode == 0 else f"Could not read commits: {out.stderr.strip()}"


def _brief(value: str) -> str:
    return " ".join(value.split())[:500]


def _task_line(lines: list[str], task: str) -> int:
    in_tasks = False
    hits = []
    for i, line in enumerate(lines):
        if line.startswith("## "):
            in_tasks = line.rstrip() in ("## Tasks", "## Lanes")
        if in_tasks and line.startswith("|") and not _is_separator(_cells(line)) and task in _cells(line):
            hits.append(i)
    if len(hits) != 1:
        raise ValueError("could not identify one task table row for reconciliation")
    return hits[0]


def _set_owner_state(line: str, old: re.Pattern, new: str, why: str) -> str:
    # Owner and state are adjacent in every supported tracker table. Replace only that
    # span; leave the item and every other cell byte-for-byte as approved.
    replaced, count = old.subn(lambda _: new, line)
    if count != 1:
        raise ValueError(why)
    return replaced


def _running(name: str) -> re.Pattern:
    return re.compile(rf"\|\s*{re.escape(name)}\s*\|\s*running\s+\d{{1,2}}:\d{{2}}\s*\|", re.I)


def _carried(name: str) -> re.Pattern:
    """The rollover twin's row: same worker, state restarted to open (#576)."""
    return re.compile(rf"\|\s*{re.escape(name)}\s*\|\s*open\s*\|", re.I)


def _tree_note(root: Path, cwd: Path, expected_common: Path | None = None) -> str:
    """How the File ownership row names the tree: relative to the Worktrees dir, as audit reads it."""
    claude_md = (root / "CLAUDE.md").read_text()
    base = (root / worktrees_dir(claude_md)).resolve()
    if expected_common is None:
        settings = load_settings(root, settings_path(claude_md))
        commons = git_trees.trusted_commons([root / path for path in settings.repos.values()])
        expected_common = next((commons[p] for p in (cwd.resolve(), *cwd.resolve().parents) if p in commons), None)
    try:
        tree = str(cwd.resolve().relative_to(base))
    except ValueError:
        tree = str(cwd.resolve())
    out, error = _git(["branch", "--show-current"], cwd, expected_common=expected_common)
    if isinstance(error, git_view.CacheError):
        raise ValueError(f"git view cache configuration error: {error}") from None
    if isinstance(error, git_view.NotARepository):
        branch = ""
    elif out is None or out.returncode:
        raise ValueError("tree is unreadable")
    else:
        branch = out.stdout.strip()
    if any(c in tree + branch for c in "|`()\n"):
        raise ValueError(f"tree {tree!r} on {branch!r} cannot be written into a File ownership row")
    return f"worktree `{tree}` ({branch or 'detached'})"


def record_launch(path: Path, task: str, name: str, tree: str, runtime: str, model: str, at: str,
                  effort: str = "") -> None:
    """Before the worker runs: the row is the worker's and running, File ownership names its tree, and the Log says so."""
    if "|" in name or "\n" in name:
        raise ValueError("worker name cannot be written as a tracker owner")

    def change(text: str) -> str:
        lines = text.splitlines(keepends=True)
        i = _task_line(lines, task)
        dispatch_prompt.refuse_overlap(text, task)  # under the lock, so two launches that share a path cannot both pass
        lines[i] = _set_owner_state(lines[i], re.compile(r"\|\s*unassigned\s*\|\s*open\s*\|", re.I),
                                    f"| {name} | running {at} |", "task owner/state is not an unassigned open row")
        keys = dispatch_prompt.task_keys(task, dispatch_prompt.task_name(text, task))
        section = False
        owned = None
        for j, line in enumerate(lines):
            if line.startswith("## "):
                section = line.rstrip() == ownership.HEADING
            elif section and any(row.context in keys for row in ownership.parse(line)):
                owned = j
                break
        if owned is None:
            raise ValueError(f"no File ownership row for {task!r}")
        body = lines[owned].rstrip("\n")
        if body.lstrip().startswith("|"):
            if len(_cells(body)) != 2:
                raise ValueError("the File ownership row is not a two-cell `| context | paths |` row")
            cut = body.rstrip().rstrip("|").rstrip()
            lines[owned] = f"{cut}; {tree} |\n"
        else:
            lines[owned] = f"{body.rstrip()}; {tree}\n"
        return tracker_write.append_log("".join(lines), started_line(at, name, runtime, model, tree, task, effort), create=True)

    tracker_write.edit(path, change)


def update_tracker(path: Path, task: str, name: str, owner: str, status: str, reason: str, changes: str,
                   at: str, hold_error: str = "", carried: bool = False) -> None:
    """Set the dispatched row to waiting, or back to ready on a relaunch, and append a durable local result note.

    A row the midnight rollover carried over starts open under the worker (#576), not running. A failed
    review hold (#566) rides the same locked edit into the Log, so the audit still sees the hold attempt
    after this run's report is gone.
    """
    if "|" in owner or "\n" in owner:
        raise ValueError("configured user name cannot be written as a tracker owner")

    def change(text: str) -> str:
        lines = text.splitlines(keepends=True)
        i = _task_line(lines, task)
        running = _carried(name) if carried else _running(name)
        why = "task row changed during the one-shot run; tracker needs manual reconciliation"
        if status == "relaunch":
            # Back to unassigned and open: ready, so the coordinator dispatches it again.
            lines[i] = _set_owner_state(lines[i], running, "| unassigned | open |", why)
            note = relaunch_line(at, name, task, _brief(reason))
        else:
            lines[i] = _set_owner_state(lines[i], running, f"| {owner} | waiting |", why)
            note = ended_line(at, name, status, _brief(reason), _brief(changes))
        text = tracker_write.append_log("".join(lines), note, create=True)
        if hold_error:
            text = tracker_write.append_log(text, hold_failed_line(at, name, task, hold_error))
        return text

    tracker_write.edit(path, change)


def launcher_copy(root: Path, cwd: Path) -> Path:
    return root / dispatch_prompt.REPORTS_DIR / f"{cwd.name}.toon"


def write_report(root: Path, cwd: Path, report: dict) -> dict:
    """The launcher's own copy, which `result` reads, then the worker's in the tree; returns what was written. The text is cut
    to what `result` reads and written as UTF-8. A copy that cannot be written is an error in the report, and the old one goes
    so it is not served as this run's; the tree is the worker's, so a failure there costs nothing."""
    report = {**report, **{key: report[key][:FIELD_MAX] for key in ("reason", "changes")}}
    mine = launcher_copy(root, cwd)
    try:
        atomic_write(mine, (toon_encode(report) + "\n").encode("utf-8", errors="replace"))
    except OSError as exc:
        with suppress(OSError):
            mine.unlink(missing_ok=True)
        report["errors"] = [*report["errors"], f"report copy: {exc}"]
    data = (toon_encode(report) + "\n").encode("utf-8", errors="replace")
    if not (cwd / REPORT).parent.is_symlink():
        with suppress(OSError):
            atomic_write(cwd / REPORT, data)
    return report


class _TaskLookup(NamedTuple):
    text: str = ""
    rows: tuple[Task, ...] = ()
    error: OSError | ValueError | None = None

    @property
    def row(self) -> Task | None:
        return self.rows[0] if len(self.rows) == 1 else None


def _lookup_task(path: Path, task: str, *, allow_missing: bool = False) -> _TaskLookup:
    """Read one tracker's task, keeping unreadable evidence separate from clean absence."""
    try:
        text = path.read_text()
    except FileNotFoundError as exc:
        return _TaskLookup(error=None if allow_missing else exc)
    except (OSError, ValueError) as exc:
        return _TaskLookup(error=exc)
    return _TaskLookup(text, tuple(row for row in parse_tracker(text).tasks if row.item.strip() == task))


def _launch_row_running(launch: Path, task: str, name: str) -> bool:
    """The launch-day row the midnight rollover carried over is still running under this worker (#576)."""
    found = _lookup_task(launch, task)
    if found.error is not None:
        raise found.error
    row = found.row
    return row is not None and row.kind == "running" and row.owner.strip().lower() == name.lower()


def _pending_run(root: Path, cfg, day: str, task: str, name: str) -> bool:
    """Whether reconciliation still owns a running row, including its midnight rollover twin."""
    launch = root / cfg.tracker_path(day)
    end = root / cfg.tracker_path(datetime.now(cfg.zone).date().isoformat())
    found = _lookup_task(end, task, allow_missing=end != launch)
    if found.error is not None:
        return True  # an unreadable tracker cannot authorize discarding recovery evidence
    if found.rows:
        row = found.row
        if row is None or row.owner.strip().lower() != name.lower():
            return False
        if row.kind == "running":
            return True
        if end == launch or row.kind != "open":
            return False
    found = _lookup_task(launch, task)
    row = found.row
    return found.error is not None or (row is not None and row.kind == "running"
                                      and row.owner.strip().lower() == name.lower())


def reconcile(root: Path, day: str, task: str, name: str, cwd: Path, exit_code: int,
              before_head: str | None = None, read_only: bool = False, runtime: str = "", model: str = "",
              *, adopted: bool = False, expected_common: Path | None = None,
              teardown_errors: list[str] | None = None) -> dict:
    """Reconcile a worker run against its launch day, task and tree; git violations precede suite gating; return a report with counts and evidence."""
    cfg = dispatch_prompt.config(root)
    settings = load_settings(root, cfg.settings_path)
    errors = list(teardown_errors or [])
    if expected_common is None:
        commons = git_trees.trusted_commons([root / path for path in settings.repos.values()])
        expected_common = next((commons[p] for p in (cwd.resolve(), *cwd.resolve().parents) if p in commons), None)
    status, reason, changes, counts = worker_result(cwd / RESULT, exit_code)
    suite = "Suite: " + ", ".join(f"{key}={value if value is not None else 'missing or invalid'}"
                                  for key, value in counts.items())
    actual = changed_files(cwd, expected_common=expected_common)
    commits = committed_changes(cwd, before_head, expected_common=expected_common)
    # The result lands on the day the run ends, falling back to the launch day's (#93).
    path = root / cfg.tracker_path(day)
    launch = path
    end = root / cfg.tracker_path(datetime.now(cfg.zone).date().isoformat())
    carried = False
    found = _lookup_task(end, task, allow_missing=end != launch)
    if found.error is not None:
        raise found.error
    if found.rows:
        # The midnight rollover carries a still-running launch-day row over as the end day's open twin (#576):
        # the same row, so the run lands on it. The launch day's row tells the twin from a changed row.
        carried = launch != end and _launch_row_running(launch, task, name)
        path = end
    if status == "done" and read_only and (actual or commits != NO_COMMITS):
        # A task that owns nothing changed something: whatever the worker wrote, someone looks (#57).
        left = "\n".join(x for x in (actual, commits) if x and x != NO_COMMITS)
        status, reason = "human_review", f"read-only task left changes: {left}; worker said: {reason}"
    if status == "done" and actual:
        status = "human_review"
        reason = f"worker reported completion but the worktree is not clean: {actual}"
    # Git/worktree violations take precedence over the suite gate.
    if status == "done" and suite_holds(counts, require_green=settings.workers.require_green_tests):
        status, reason = "human_review", f"suite is not green; {suite}; worker said: {reason}"
    quota = ""
    if status == "human_review" and not changes and not actual and commits == NO_COMMITS:
        # No valid result from an unchanged tree: a quota stop returns it to ready rather than holding it (#422).
        quota = quota_stop(cwd / dispatch_prompt.PROMPT_DIR / "worker-stderr.log", runtime, model)
        if quota:
            status, reason = "relaunch", quota
    if status == "relaunch":
        # A relaunch is for a run that changed nothing, once: anything else is someone's to look at.
        # A quota relaunch does not spend that limit; it has its own, once per task and runtime+model a day (#422).
        marker = RELAUNCHED.format(task=task)
        log = path.read_text().splitlines()
        if not quota and _brief(reason).startswith(QUOTA_STOP):  # as the Log folds it, so padding cannot dodge it
            reason = f"worker: {reason}"  # the launcher's marker is the launcher's alone
        if actual or commits != NO_COMMITS:
            status, reason = "human_review", f"worker asked to be relaunched but left changes: {reason}"
        elif quota:
            mine = re.escape(quota_marker(runtime, model))  # then the line goes on with `;` and a snippet, or ends with `.`
            if any(marker in line and re.match(mine + r"(?:;|\.?$)", line.split(marker, 1)[1]) for line in log):
                status, reason = "human_review", f"quota stop repeated: {quota[len(QUOTA_STOP):]}"
        elif any(marker in line and not line.split(marker, 1)[1].startswith(QUOTA_STOP) for line in log):
            status, reason = "human_review", f"already relaunched once today and stopped again: {reason}"
    # Keep launcher evidence ahead of worker prose and reserve room for each part.
    evidence = (f"{suite}\nCommits from this run:\n{commits[:FIELD_MAX // 4]}" +
                (f"\nWorking tree:\n{actual[:FIELD_MAX // 4]}" if actual else "\nWorking tree clean") + "\n")
    summary = evidence + changes[:FIELD_MAX - len(evidence)]
    # The tracker keeps only 500 characters: its git excerpts must leave room for both facts.
    log_summary = (f"{suite}; Commits from this run: {_brief(commits)[:100]}; " +
                   (f"Working tree: {_brief(actual)[:100]}" if actual else "Working tree clean") + f"; {changes}")
    found = _lookup_task(path, task)
    if found.error is not None:
        raise found.error
    row = found.row
    if row is None or row.owner.strip().lower() != name.lower() or (
            # The rollover twin is the same row, open (#576); any other drift from running is a changed row.
            row.kind != "running" and not (carried and row.kind == "open")):
        report = {"status": "human_review", "task": task, "worker": name, "runtime_exit": exit_code,
                  "reason": "task row changed during the one-shot run; reconcile it manually",
                  "changes": summary, **counts,
                  "errors": errors + [f"{dispatch_prompt.TRACKER_ERROR} task row changed; no issue or tracker update was made"]}
        return write_report(root, cwd, report)
    if adopted:
        # #99: the launcher died before reconciling this run; the report, the Log line and any
        # review comment say so, and the tree's evidence is the only word there is.
        reason = f"the launcher died before reconciling this run; {reason}"
    ref = backlog.issue_ref(row.issue, cfg.backlog) if row and row.issue.strip() else None
    hold_error = ""
    if status == "human_review" and ref:
        if settings.kanban:
            error = forge.add_human_hold(ref, cfg.backlog, settings.kanban)
            if error:
                hold_error = error
                errors.append(f"review hold: {error}")
        else:
            errors.append("review hold: workspace has no [kanban] configuration")
    try:
        update_tracker(path, task, name, cfg.user, status, reason, log_summary,
                       tracker_write.stamp(cfg.zone), hold_error=hold_error, carried=row.kind == "open")
    except (OSError, ValueError) as exc:
        errors.append(f"{dispatch_prompt.TRACKER_ERROR} {exc}")
        report = {"status": status, "task": task, "worker": name, "runtime_exit": exit_code,
                  "reason": reason, "changes": summary, **counts, "errors": errors}
        return write_report(root, cwd, report)
    # Publish warnings and review comments only after the row moves. An adoption retry
    # cannot repeat them while a tracker write is failing; setting the review hold is idempotent.
    for error in (teardown_errors or []):
        try:
            tracker_write.edit(path, lambda text: tracker_write.append_log(
                text, teardown_failed_line(tracker_write.stamp(cfg.zone), name, task,
                                           _brief(error.removeprefix('teardown: '))), create=True))
        except (OSError, ValueError) as exc:
            errors.append(f"teardown: could not log cleanup failure: {exc}")
    if status == "human_review" and ref:
        home = (backlog.GitHubBacklog(ref.repo) if ref.host == backlog.GITHUB else
                cfg.backlog.sibling(ref.repo)
                if isinstance(cfg.backlog, backlog.Backlog) else None)
        if home is not None:
            body = f"One-shot worker {name} needs human review.\n\nWhy: {reason}\n\nChanges made:\n{summary}"
            commented = backlog.comment(home, ref.number, body, commit=True)
            if not commented.done:
                errors.append(f"issue comment: {commented.error}")
    report = {"status": status, "task": task, "worker": name, "runtime_exit": exit_code,
              "reason": reason, "changes": summary, **counts, "errors": errors}
    return write_report(root, cwd, report)


@contextmanager
def trees_lock(trees: Path):
    """Hold the Worktrees dir's lock: workers claim their trees under it, and cleanup removes a tree under it."""
    lock = os.open(trees, os.O_RDONLY)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield
    finally:
        os.close(lock)


def _pidfile_line(pidfile: Path) -> str | None:
    """The first line of a regular pid file, or None when it is missing, a symlink, or unreadable."""
    try:
        if pidfile.parent.is_symlink():
            return None
        data = git_view.read_regular(pidfile, PIDFILE_MAX, nofollow=True)
    except OSError:
        return None
    if not data:
        return None
    return (data.decode(errors="replace").splitlines() or [""])[0]


def _pidfile_pid(pidfile: Path) -> int | None:
    """The pid in a regular pid file, or None when it is missing, a symlink, or unreadable."""
    line = _pidfile_line(pidfile)
    if line is None:
        return None
    pid, _, _ = line.partition(" ")
    if pid.isascii() and pid.isdecimal() and (value := int(pid)) > 0:
        return value
    return None


def _write_pidfile(trees: Path, cwd: Path, text: str, replace: set[int]) -> bool:
    """Under the worktrees lock, write only when the pid file is absent or still names `replace`."""
    with trees_lock(trees):
        pidfile = cwd / PIDFILE
        if pidfile.parent.is_symlink() or pidfile.is_symlink():
            return False
        current = _pidfile_pid(pidfile)
        if pidfile.exists() and current not in replace:
            return False
        pidfile.parent.mkdir(exist_ok=True)
        if pidfile.parent.is_symlink() or pidfile.is_symlink():
            return False
        pidfile.write_text(text)
        return True


def _unlink_own_pidfile(trees: Path, cwd: Path, pid: int) -> None:
    with trees_lock(trees):
        with suppress(OSError):
            pidfile = cwd / PIDFILE
            if _pidfile_pid(pidfile) == pid:
                pidfile.unlink()


@contextmanager
def slot(trees: Path, cwd: Path, task: str, cap: int | None):
    """Claim a worker slot under the Worktrees dir's lock, so two sessions launching at once cannot both
    take the last one; release it when the run ends."""
    with trees_lock(trees):
        busy = running_trees(trees)
        if cap is not None and len(busy) >= cap:
            raise ValueError(f"{len(busy)} one-shot workers running; [workers] max_concurrency is {cap} "
                             "(list them: chief-of-stuff processes --root <workspace>)")
        pidfile = cwd / PIDFILE
        line = _pidfile_line(pidfile)
        held = _pidfile_pid(pidfile)
        # A live detached worker's pid is the adoption marker; do not replace it.
        # A dead pid for another task is the same marker, waiting for adopt().
        if line is not None and held is not None and held != os.getpid():
            if process_exists(held):
                raise ValueError(f"{cwd.name} already has a live one-shot worker (pid {held})")
            if line.partition(" ")[2].strip() != task.strip():
                raise ValueError(f"{cwd.name} still holds an unreconciled one-shot run")
        if pidfile.parent.is_symlink() or pidfile.is_symlink():
            raise ValueError(f"{cwd.name} pid file is a symlink")
        pidfile.parent.mkdir(exist_ok=True)
        if pidfile.parent.is_symlink() or pidfile.is_symlink():
            raise ValueError(f"{cwd.name} pid file is a symlink")
        pidfile.write_text(f"{os.getpid()} {task}\n")
    recovery = {"pending": False}
    try:
        yield recovery
    finally:
        if not recovery["pending"]:
            _unlink_own_pidfile(trees, cwd, os.getpid())


def _clear_stale_dispatch(cwd: Path, task: str) -> None:
    """A dispatch whose launcher was stopped mid-run accepts the same task again, keeping the work
    already in the tree (#138). The pid file answers whether a launcher or worker is still live: a
    dead or missing one plus the same task clears the tree's copy of the dispatch and the stopped
    run's result; a live pid, another task's dispatch, or a pid file that cannot be read, refuses
    as ever (write_dispatch, below). Runs before slot() writes its own claim into the pid file."""
    dispatch = cwd / dispatch_prompt.DISPATCH_FILE
    if not dispatch.is_file():
        return
    try:
        data = git_view.read_regular(cwd / PIDFILE, PIDFILE_MAX, nofollow=True)
    except FileNotFoundError:
        pass  # no pid file: the run that wrote this dispatch ended and cleaned its slot
    except OSError:
        return  # a pid file that cannot be read vouches for nothing; the refusal stands
    else:
        pid, _, held = ((data or b"").decode(errors="replace").splitlines() or [""])[0].partition(" ")
        if pid.isascii() and pid.isdecimal() and int(pid) > 0 and process_exists(int(pid)):
            return  # a launcher or worker is live in this tree
        if held.strip() != task.strip():
            return  # another task's tree
    (cwd / RESULT).unlink(missing_ok=True)  # the stopped run's result is not the next run's
    dispatch.unlink(missing_ok=True)


def run(*, root: Path, day: str | None, task: str, cwd: Path, name: str,
        runtime: str, agent_type: str | None, model: str, effort: str, dry_run: bool,
        timeout_minutes: int = 60,
        teardown: Callable[[Path, Path, str, str], list[str]] = lambda *args: []) -> int:
    cfg = dispatch_prompt.config(root)
    chosen_day = day or datetime.now(cfg.zone).date().isoformat()
    settings = load_settings(root, cfg.settings_path)
    body = dispatch_prompt.compose(root, chosen_day, task, worktree=cwd, name=name,
                                   runtime=runtime, one_shot=True, settings=settings)
    workers = settings.workers
    commons = git_trees.trusted_commons([root / path for path in settings.repos.values()])
    common = next((commons[p] for p in (cwd.resolve(), *cwd.resolve().parents) if p in commons), None)
    program, lead = runtimes.program(runtime, workers.claude_as_profile)
    binary = resolve(program)
    if not binary:
        raise ValueError(f"{program} is unavailable in the configured interactive login shell")
    dispatch = cwd / dispatch_prompt.DISPATCH_FILE
    argv = command(runtime, binary, cwd, dispatch, agent_type=agent_type, model=model, effort=effort, lead=lead,
                   expected_common=common)
    if dry_run:
        print("would run one-shot: " + " ".join(shlex.quote(x) for x in argv))
        print(f"would write: {dispatch}")
        return 0
    if not cwd.is_dir():
        raise ValueError(f"no worktree at {cwd}")
    trees = root / worktrees_dir((root / "CLAUDE.md").read_text())
    _clear_stale_dispatch(cwd, task)
    with slot(trees, cwd, task, workers.max_concurrency) as recovery:
        try:
            return _launch(root, cfg, chosen_day, task, cwd, name, runtime, model, effort, body, argv,
                           timeout_minutes, common, teardown, workers.teardown, trees=trees)
        finally:
            recovery["pending"] = _pending_run(root, cfg, chosen_day, task, name)


def _launch(root: Path, cfg, chosen_day: str, task: str, cwd: Path, name: str, runtime: str, model: str,
            effort: str, body: str, argv: list[str], timeout_minutes: int, expected_common: Path | None = None,
            teardown: Callable[[Path, Path, str, str], list[str]] = lambda *args: [], cleanup: str = "",
            *, trees: Path | None = None) -> int:
    # Same clean login-shell path as interactive sessions; auth and user PATH come from shell setup.
    # Everything that can refuse runs before anything is written; a refused row takes its dispatch back.
    trees = trees if trees is not None else root / worktrees_dir((root / "CLAUDE.md").read_text())
    launch_argv, tree = login_argv(argv), _tree_note(root, cwd, expected_common)
    before_head = git_head(cwd, strict=True, expected_common=expected_common)
    written = dispatch_prompt.write_dispatch(cwd, body)
    logs = cwd / dispatch_prompt.PROMPT_DIR
    try:
        record_launch(root / cfg.tracker_path(chosen_day), task, name, tree, runtime, model,
                      tracker_write.stamp(cfg.zone), effort)
    except Exception:
        written.unlink(missing_ok=True)
        raise
    # An earlier run's copy must not read as this run's if the launcher dies before reconciling.
    with suppress(OSError):  # something in the way of the reports folder is no reason to leave the row running
        launcher_copy(root, cwd).unlink(missing_ok=True)
    # A relaunch in a fresh tree leaves the old tree's copy; `result` would pick it by mtime. Other tasks' copies stay.
    for copy in (root / dispatch_prompt.REPORTS_DIR).glob("*.toon"):
        try:
            if read_report(copy, git_trees.read_regular)["task"] == task:
                copy.unlink()
        except UNREADABLE:
            pass
    env = clean_env()
    env["CHIEF_OF_STUFF_WORKSPACE"] = str(root.resolve())
    env["CHIEF_OF_STUFF_WORKER"] = name
    for log in ("worker-stdout.log", "worker-stderr.log"):
        (logs / log).write_text("")  # a fresh run's worker logs start empty; a retried run appends
    worker_pids: list[int] = []
    exit_code = _run_worker(cwd, logs, launch_argv, env, task, timeout_minutes, worker_pids, trees=trees)
    try:
        teardown_errors = teardown(root, cwd, name, cleanup)
        if exit_code == 124 and not (cwd / RESULT).exists():
            # The first timeout relaunches the run once, into the same tree with its partial commits
            # (#110): the Log line is the once-per-row marker the second timeout finds, a result the
            # killed worker wrote mid-window goes so only the second run's evidence is read, and the
            # second wait gets a doubled timeout.
            tracker = root / cfg.tracker_path(chosen_day)
            if not timed_out_before(tracker, name):
                tracker_write.edit(tracker, lambda text: tracker_write.append_log(
                    text, timed_out_line(tracker_write.stamp(cfg.zone), name, timeout_minutes, task)))
                with suppress(OSError):
                    (cwd / RESULT).unlink(missing_ok=True)
                exit_code = _run_worker(cwd, logs, launch_argv, env, task, timeout_minutes * 2, worker_pids, trees=trees)
                teardown_errors.extend(teardown(root, cwd, name, cleanup))
        report = reconcile(root, chosen_day, task, name, cwd, exit_code, before_head, runtime=runtime, model=model,
                           read_only=dispatch_prompt.READ_ONLY in body.splitlines(), expected_common=expected_common,
                           teardown_errors=teardown_errors)  # held to what it was told
    finally:
        # Keep unfinished runs discoverable. With no spawned child, the launcher is
        # the recovery marker and becomes adoptable when it exits. Completed or reassigned rows release it.
        if _pending_run(root, cfg, chosen_day, task, name):
            with suppress(OSError):
                launcher_copy(root, cwd).unlink(missing_ok=True)
            if worker_pids:
                with suppress(OSError):
                    _write_pidfile(trees, cwd, f"{worker_pids[-1]} {task}\n", {os.getpid(), worker_pids[-1]})
    print(toon_encode(report))
    return 0 if report["status"] == "done" and not any(
        not error.startswith("teardown:") for error in report["errors"]) else 1


def timed_out_before(tracker: Path, name: str) -> bool:
    """Whether this tracker's Log already carries `name`'s timed-out marker: the row's once-per-run
    retry is spent, so a run timing out again holds for review instead of relaunching (#110)."""
    if not tracker.is_file():
        return False
    return any((m := TIMED_OUT.match(line)) and m.group(3) == name for line in tracker.read_text().splitlines())


def _run_worker(cwd: Path, logs: Path, launch_argv: list[str], env: dict, task: str, timeout_minutes: int,
                worker_pids: list | None = None, *, trees: Path | None = None) -> int:
    """Run the worker once, to completion or out of time: its exit code, 124 when the wait ran out,
    127 when it could not start. The worker runs in a session of its own: the coordinator's restart
    kills the launcher and the row, not the run, and the restarted coordinator adopts the finished
    run from the tree's evidence (#99). The pid file names the worker, the pid that is actually in
    this tree; `processes` reads the same file either way."""
    trees = trees if trees is not None else cwd.parent
    worker = None
    try:
        with (logs / "worker-stdout.log").open("a") as stdout, (logs / "worker-stderr.log").open("a") as stderr:
            worker = subprocess.Popen(launch_argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                                      stdout=stdout, stderr=stderr, start_new_session=True)
            try:
                if not _write_pidfile(trees, cwd, f"{worker.pid} {task}\n", {os.getpid()}):
                    raise OSError("pid file names another worker")
            except OSError:
                with suppress(ProcessLookupError):
                    os.killpg(worker.pid, signal.SIGKILL)  # no untracked worker may survive
                worker.wait()
                raise
            return worker.wait(timeout=timeout_minutes * 60)
    except subprocess.TimeoutExpired:
        if worker is not None:
            with suppress(ProcessLookupError):
                os.killpg(worker.pid, signal.SIGKILL)
            worker.wait()
        with (logs / "worker-stderr.log").open("a") as stderr:
            stderr.write(f"One-shot run timed out after {timeout_minutes} minutes\n")
        return 124
    except OSError as exc:
        with suppress(OSError), (logs / "worker-stderr.log").open("a") as stderr:
            stderr.write(f"Could not start worker: {exc}\n")
        return 127
    finally:
        if worker_pids is not None and worker is not None:
            worker_pids.append(worker.pid)
        # The launcher owns the tree throughout cleanup, retry setup and reconciliation.
        # A still-running worker keeps its own pid so adoption can find it (#99).
        if worker is None or worker.poll() is not None:
            with suppress(OSError):
                replace = {os.getpid()}
                if worker is not None:
                    replace.add(worker.pid)
                _write_pidfile(trees, cwd, f"{os.getpid()} {task}\n", replace)


ADOPT_LOOKBACK = 7  # days of trackers one adoption sweep scans for a dead run's running row


def _running_row(root: Path, cfg, task: str, cwd: Path, days: list[str]) -> tuple[str, str] | None:
    """The day and worker name of `task`'s running row in the latest of `days` whose tracker holds one
    and whose File ownership names this tree; None when every tracker was readable or absent
    and none says running here. An unreadable tracker raises, preserving recovery evidence (#99)."""
    tree = f"worktree `{cwd.resolve().name}`"
    for day in days:  # latest first
        found = _lookup_task(root / cfg.tracker_path(day), task, allow_missing=True)
        if found.error is not None:
            raise found.error
        row = found.row
        if row is None or row.kind != "running" or not row.owner.strip():
            continue
        owned = ownership.cell(found.text, dispatch_prompt.task_keys(task, dispatch_prompt.task_name(found.text, task)))
        if owned is None or tree not in owned:
            continue  # the row's running work lives in some other tree
        return day, row.owner.strip()
    return None


def adopt(root: Path, day: str | None = None, *,
          teardown: Callable[[Path, Path, str, str], list[str]] = lambda *args: []) -> list[dict]:
    """Reconcile the one-shot runs whose launcher died before reconciling them (#99): a tree whose
    pid file names a worker that is gone, whose task row still says running under that worker, and
    whose launcher report copy was never written, is reconciled from its own evidence - the result
    file, the changed files, the work already in the tree. A worker still alive is left to finish;
    the next sweep adopts it. The day is a tracker day, not the zone's today; with none given,
    adoption looks back through ADOPT_LOOKBACK days. Tracker read failures preserve that tree's
    marker and are raised after the remaining trees have been processed."""
    cfg = dispatch_prompt.config(root)
    settings = load_settings(root, cfg.settings_path)
    commons = git_trees.trusted_commons([root / path for path in settings.repos.values()])
    trees = root / worktrees_dir((root / "CLAUDE.md").read_text())
    days = [day] if day else [(datetime.now(cfg.zone).date() - timedelta(days=n)).isoformat()
                              for n in range(ADOPT_LOOKBACK)]
    adopted = []
    failures = []
    for cwd, pid, task in pidfile_runs(trees):
        if process_exists(pid):
            continue  # a worker still running is still working on its row
        if launcher_copy(root, cwd).exists():
            _unlink_own_pidfile(trees, cwd, pid)
            continue  # a copy is there: a reconcile already answered for this tree's latest run
        try:
            found = _running_row(root, cfg, task, cwd, days)
        except (OSError, ValueError) as exc:
            failures.append(exc)
            continue
        if found is None:
            _unlink_own_pidfile(trees, cwd, pid)
            continue
        # Recheck under the launch/cleanup lock before taking the dead worker's slot.
        pidfile = cwd / PIDFILE
        claimed = False
        row_day, name = found
        try:
            with trees_lock(trees):
                current = next(((held_pid, held_task) for tree, held_pid, held_task in pidfile_runs(trees) if tree == cwd), None)
                if current != (pid, task) or process_exists(pid) or launcher_copy(root, cwd).exists():
                    continue
                if pidfile.parent.is_symlink() or pidfile.is_symlink():
                    continue
                try:
                    pidfile.write_text(f"{os.getpid()} {task}\n")
                except OSError:
                    continue
                claimed = True
            stale = cwd / dispatch_prompt.DISPATCH_FILE
            body = stale.read_text() if stale.is_file() else ""
            # The dispatch is the launcher's own writing, so its Read-only line is trusted (#57).
            report = reconcile(root, row_day, task, name, cwd, 0,
                               read_only=dispatch_prompt.READ_ONLY in body.splitlines(), adopted=True,
                               expected_common=commons.get(cwd.resolve()),
                               teardown_errors=teardown(root, cwd, name, settings.workers.teardown))
            adopted.append(report)
        except BaseException as exc:
            # Preserve the dead worker evidence so a later sweep can retry interrupted adoption.
            if claimed and _pending_run(root, cfg, row_day, task, name):
                with suppress(OSError):
                    _write_pidfile(trees, cwd, f"{pid} {task}\n", {os.getpid(), pid})
            elif claimed:
                _unlink_own_pidfile(trees, cwd, os.getpid())
            if isinstance(exc, (OSError, ValueError)):
                failures.append(exc)
            else:
                raise
        else:
            errors = report["errors"]
            unfinished = _pending_run(root, cfg, row_day, task, name) and (not launcher_copy(root, cwd).exists()
                          or any(str(error).startswith((dispatch_prompt.TRACKER_ERROR, "report copy:"))
                                 for error in errors))
            if unfinished:
                with suppress(OSError):
                    launcher_copy(root, cwd).unlink(missing_ok=True)
                with suppress(OSError):
                    _write_pidfile(trees, cwd, f"{pid} {task}\n", {os.getpid(), pid})
            else:
                _unlink_own_pidfile(trees, cwd, os.getpid())
    if len(failures) == 1:
        raise failures[0]
    if failures:
        raise OSError("adoption tracker failures: " + "; ".join(str(exc) for exc in failures))
    return adopted
