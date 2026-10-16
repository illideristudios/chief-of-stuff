#!/usr/bin/env python3
"""Run one scoped task in one CLI turn, then reconcile its result."""

from __future__ import annotations

import fcntl
import json
import os
import re
import shlex
import subprocess
from contextlib import contextmanager, suppress
from datetime import datetime, timedelta
from pathlib import Path

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
from model_port import quota_reset
from notify_service import atomic_write
from dispatch_prompt import OUTCOMES
from one_shot_report import FIELD_MAX, UNREADABLE, read_report
from process_status import PIDFILE, PIDFILE_MAX, pidfile_runs, process_exists, running_trees
from tracker import parse_tracker
from tracker_log import RELAUNCHED, TIMED_OUT, ended_line, hold_failed_line, relaunch_line, started_line, timed_out_line
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
    report = {**report, "outcome": OUTCOMES.get(report["status"], report["status"]),
              **{key: report[key][:FIELD_MAX] for key in ("reason", "changes")}}
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


def _launch_row_running(launch: Path, task: str, name: str) -> bool:
    """The launch-day row the midnight rollover carried over is still running under this worker (#576)."""
    try:
        rows = [row for row in parse_tracker(launch.read_text()).tasks if row.item.strip() == task]
    except OSError:
        return False
    return len(rows) == 1 and rows[0].kind == "running" and rows[0].owner.strip().lower() == name.lower()


def reconcile(root: Path, day: str, task: str, name: str, cwd: Path, exit_code: int,
              before_head: str | None = None, read_only: bool = False, runtime: str = "", model: str = "",
              *, adopted: bool = False, expected_common: Path | None = None) -> dict:
    """Reconcile a worker run against its launch day, task and tree; git violations precede suite gating; return a report with counts and evidence."""
    cfg = dispatch_prompt.config(root)
    settings = load_settings(root, cfg.settings_path)
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
    if end.exists() and any(row.item.strip() == task for row in parse_tracker(end.read_text()).tasks):
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
    if status == "done" and settings.workers.require_green_tests and (
            any(value is None for value in counts.values()) or counts["passed"] == 0
            or any(counts[key] for key in ("failed", "skipped", "xfailed"))):
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
    rows = [row for row in parse_tracker(path.read_text()).tasks if row.item.strip() == task]
    row = rows[0] if len(rows) == 1 else None
    errors = []
    if row is None or row.owner.strip().lower() != name.lower() or (
            # The rollover twin is the same row, open (#576); any other drift from running is a changed row.
            row.kind != "running" and not (carried and row.kind == "open")):
        report = {"status": "human_review", "task": task, "worker": name, "runtime_exit": exit_code,
                  "reason": "task row changed during the one-shot run; reconcile it manually",
                  "changes": summary, **counts,
                  "errors": [f"{dispatch_prompt.TRACKER_ERROR} task row changed; no issue or tracker update was made"]}
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
        home = (backlog.GitHubBacklog(ref.repo) if ref.host == backlog.GITHUB else
                cfg.backlog.sibling(ref.repo)
                if isinstance(cfg.backlog, backlog.Backlog) else None)
        if home is not None:
            body = f"One-shot worker {name} needs input.\n\nWhy: {reason}\n\nChanges made:\n{summary}"
            commented = backlog.comment(home, ref.number, body, commit=True)
            if not commented.done:
                errors.append(f"issue comment: {commented.error}")
    try:
        update_tracker(path, task, name, cfg.user, status, reason, log_summary,
                       tracker_write.stamp(cfg.zone), hold_error=hold_error, carried=row.kind == "open")
    except (OSError, ValueError) as exc:
        errors.append(f"{dispatch_prompt.TRACKER_ERROR} {exc}")
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


@contextmanager
def slot(trees: Path, cwd: Path, task: str, cap: int | None):
    """Claim a worker slot under the Worktrees dir's lock, so two sessions launching at once cannot both
    take the last one; release it when the run ends."""
    with trees_lock(trees):
        busy = running_trees(trees)
        if cap is not None and len(busy) >= cap:
            raise ValueError(f"{len(busy)} one-shot workers running; [workers] max_concurrency is {cap} "
                             "(list them: chief-of-stuff processes --root <workspace>)")
        (cwd / PIDFILE).parent.mkdir(exist_ok=True)
        (cwd / PIDFILE).write_text(f"{os.getpid()} {task}\n")
    try:
        yield
    finally:
        (cwd / PIDFILE).unlink(missing_ok=True)


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
        timeout_minutes: int = 60) -> int:
    cfg = dispatch_prompt.config(root)
    chosen_day = day or datetime.now(cfg.zone).date().isoformat()
    body = dispatch_prompt.compose(root, chosen_day, task, worktree=cwd, name=name,
                                   runtime=runtime, one_shot=True)
    settings = load_settings(root, cfg.settings_path)
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
    with slot(trees, cwd, task, workers.max_concurrency):
        return _launch(root, cfg, chosen_day, task, cwd, name, runtime, model, effort, body, argv, timeout_minutes, common)


def _launch(root: Path, cfg, chosen_day: str, task: str, cwd: Path, name: str, runtime: str, model: str,
            effort: str, body: str, argv: list[str], timeout_minutes: int, expected_common: Path | None = None) -> int:
    # Same clean login-shell path as interactive sessions; auth and user PATH come from shell setup.
    # Everything that can refuse runs before anything is written; a refused row takes its dispatch back.
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
    for log in ("worker-stdout.log", "worker-stderr.log"):
        (logs / log).write_text("")  # a fresh run's worker logs start empty; a retried run appends
    exit_code = _run_worker(cwd, logs, launch_argv, env, task, timeout_minutes)
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
            exit_code = _run_worker(cwd, logs, launch_argv, env, task, timeout_minutes * 2)
    report = reconcile(root, chosen_day, task, name, cwd, exit_code, before_head, runtime=runtime, model=model,
                       read_only=dispatch_prompt.READ_ONLY in body.splitlines(), expected_common=expected_common)  # held to what it was told
    print(toon_encode(report))
    return 0 if report["status"] == "done" and not report["errors"] else 1


def timed_out_before(tracker: Path, name: str) -> bool:
    """Whether this tracker's Log already carries `name`'s timed-out marker: the row's once-per-run
    retry is spent, so a run timing out again holds for review instead of relaunching (#110)."""
    if not tracker.is_file():
        return False
    return any((m := TIMED_OUT.match(line)) and m.group(3) == name for line in tracker.read_text().splitlines())


def _run_worker(cwd: Path, logs: Path, launch_argv: list[str], env: dict, task: str, timeout_minutes: int) -> int:
    """Run the worker once, to completion or out of time: its exit code, 124 when the wait ran out,
    127 when it could not start. The worker runs in a session of its own: the coordinator's restart
    kills the launcher and the row, not the run, and the restarted coordinator adopts the finished
    run from the tree's evidence (#99). The pid file names the worker, the pid that is actually in
    this tree; `processes` reads the same file either way."""
    worker = None
    try:
        with (logs / "worker-stdout.log").open("a") as stdout, (logs / "worker-stderr.log").open("a") as stderr:
            worker = subprocess.Popen(launch_argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                                      stdout=stdout, stderr=stderr, start_new_session=True)
            try:
                (cwd / PIDFILE).write_text(f"{worker.pid} {task}\n")
            except OSError:
                worker.kill()  # a live worker with no pid file would race a fresh launch into this tree
                worker.wait()
                raise
            return worker.wait(timeout=timeout_minutes * 60)
    except subprocess.TimeoutExpired:
        if worker is not None:
            worker.kill()
            worker.wait()
        with (logs / "worker-stderr.log").open("a") as stderr:
            stderr.write(f"One-shot run timed out after {timeout_minutes} minutes\n")
        return 124
    except OSError as exc:
        (logs / "worker-stderr.log").open("a").write(f"Could not start worker: {exc}\n")
        return 127


ADOPT_LOOKBACK = 7  # days of trackers one adoption sweep scans for a dead run's running row


def _running_row(root: Path, cfg, task: str, cwd: Path, days: list[str]) -> tuple[str, str] | None:
    """The day and worker name of `task`'s running row in the latest of `days` whose tracker holds one
    and whose File ownership names this tree; None when no recent tracker says running here (#99)."""
    tree = f"worktree `{cwd.resolve().name}`"
    for day in days:  # latest first
        try:
            text = (root / cfg.tracker_path(day)).read_text()
        except OSError:
            continue
        rows = [row for row in parse_tracker(text).tasks if row.item.strip() == task]
        if len(rows) != 1 or rows[0].kind != "running" or not rows[0].owner.strip():
            continue
        owned = ownership.cell(text, dispatch_prompt.task_keys(task, dispatch_prompt.task_name(text, task)))
        if owned is None or tree not in owned:
            continue  # the row's running work lives in some other tree
        return day, rows[0].owner.strip()
    return None


def adopt(root: Path, day: str | None = None) -> list[dict]:
    """Reconcile the one-shot runs whose launcher died before reconciling them (#99): a tree whose
    pid file names a worker that is gone, whose task row still says running under that worker, and
    whose launcher report copy was never written, is reconciled from its own evidence - the result
    file, the changed files, the work already in the tree. A worker still alive is left to finish;
    the next sweep adopts it. The day is a tracker day, not the zone's today; with none given,
    adoption looks back through ADOPT_LOOKBACK days."""
    cfg = dispatch_prompt.config(root)
    settings = load_settings(root, cfg.settings_path)
    commons = git_trees.trusted_commons([root / path for path in settings.repos.values()])
    trees = root / worktrees_dir((root / "CLAUDE.md").read_text())
    days = [day] if day else [(datetime.now(cfg.zone).date() - timedelta(days=n)).isoformat()
                              for n in range(ADOPT_LOOKBACK)]
    adopted = []
    for cwd, pid, task in pidfile_runs(trees):
        if process_exists(pid):
            continue  # a worker still running is still working on its row
        if launcher_copy(root, cwd).exists():
            continue  # a copy is there: a reconcile already answered for this tree's latest run
        found = _running_row(root, cfg, task, cwd, days)
        if found is None:
            continue
        row_day, name = found
        stale = cwd / dispatch_prompt.DISPATCH_FILE
        body = stale.read_text() if stale.is_file() else ""
        # The dispatch is the launcher's own writing, so its Read-only line is trusted (#57).
        adopted.append(reconcile(root, row_day, task, name, cwd, 0,
                                 read_only=dispatch_prompt.READ_ONLY in body.splitlines(), adopted=True,
                                 expected_common=commons.get(cwd.resolve())))
    return adopted
