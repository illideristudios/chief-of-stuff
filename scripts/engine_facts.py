"""Gather one pass's git, worktree, process and forge facts. Frameworks and Drivers.

The store is the one `store_access` opened for the pass. Judgements stay in the use cases;
this module only reads.
"""

from __future__ import annotations

import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any

import git_trees
import git_view
import process_status
import readiness
from forge_port import ForgeError
from orphans import Claim
from reconcile import Facts


def _blockers(forge: Any) -> tuple[dict[str, tuple[str, ...]] | None, str]:
    """The blockers and, when unknown, why."""
    read = getattr(forge, "blocked_by", None)
    if read is None:
        return None, "the forge cannot read them"
    try:
        got = read()
    except (ForgeError, OSError, ValueError, TypeError) as exc:
        return None, str(exc)
    return (got, "") if isinstance(got, dict) else (None, "the forge did not answer with a mapping")


def _record(store: Any, issue_key: str, stage: str, attempt: int) -> dict | None:
    data = store.resume(issue_key)
    runs = data.get("runs") if isinstance(data, dict) else None
    record = runs.get(f"{stage}-{attempt}") if isinstance(runs, dict) else None
    return record if isinstance(record, dict) else None


def _alive(record: dict | None) -> bool | None:
    if record is None:
        return None
    run_dir = record.get("run_dir")
    if isinstance(run_dir, str) and run_dir and (Path(run_dir) / "exit.json").exists():
        return None  # the adapter finished: `worker.collect` classifies its exit, not a liveness guess
    pid, session, identity = record.get("pid"), record.get("session_id"), record.get("identity")
    if type(pid) is not int or pid <= 0 or not isinstance(session, str) or not session or not isinstance(identity, str) or not identity:
        return None
    try:
        if process_status.identity(pid, session, host=True) == identity:
            return True
        runner, runner_identity = record.get("runner_pid"), record.get("runner_identity")
        if (type(runner) is int and runner > 0 and isinstance(runner_identity, str) and runner_identity
                and process_status.identity(runner, session) == runner_identity):
            return True
        if isinstance(run_dir, str) and run_dir and (Path(run_dir) / "exit.json").exists():
            return None  # an exit published during the probes still belongs to worker.collect
        return False
    except (OSError, RuntimeError):
        return None


def _on_main(tree: Path, view: git_view.View) -> bool | None:
    code, out = git_trees.git(["for-each-ref", "--format=%(refname) %(objectname)", git_trees.REF, git_trees.LOCAL_REF], tree, view)
    if code:
        return None
    mains = [oid for name, _, oid in (line.partition(" ") for line in out.splitlines())
             if name in (git_trees.REF, git_trees.LOCAL_REF) and oid]
    if not mains:
        return None
    for oid in mains:
        code = git_trees.git(["merge-base", "--is-ancestor", "HEAD", oid], tree, view)[0]
        if code == 0:
            return True
        if code != 1:
            return None
    return False


def _progress(tree: Path, view: git_view.View) -> datetime | None | object:
    code, out = git_trees.git(["log", "-1", "--format=%cI"], tree, view)
    if code != 0:
        return object()
    if not out:
        return None
    try:
        when = datetime.fromisoformat(out)
    except ValueError:
        return object()
    return when if when.utcoffset() is not None else object()


def _changed(state: Any, tree: Path, starting_head: str | None) -> bool | None:
    """Read-only changes belong to this run, not to the unmerged branch it checked out."""
    if state is None or state.dirty is None:
        return None
    if state.dirty:
        return True
    if not isinstance(starting_head, str) or not starting_head:
        return None
    try:
        code, head = git_trees.git(["rev-parse", "HEAD"], tree)
    except (OSError, git_view.CacheError, subprocess.SubprocessError):
        return None
    return head != starting_head if code == 0 and head else None


def _tree_facts(tree: Path) -> tuple[Any, bool | None, datetime | None | object]:
    try:
        with git_view.opened(tree, git_trees.audit_env()) as view:
            return git_trees.read_state(tree, view), _on_main(tree, view), _progress(tree, view)
    except (OSError, git_view.Unviewable, git_view.CacheError, subprocess.SubprocessError, ValueError, RuntimeError):
        return None, None, object()


def _in_trees(root: Path, trees: Path, path: str) -> bool:
    if not Path(path).is_absolute():
        return False  # file scopes remain scopes even when Worktrees: .
    try:
        return (root / path).resolve().is_relative_to(trees.resolve())
    except OSError:
        return False


def gather(*, forge: Any, store: Any, root: Path, trees: Path, now: datetime) -> Facts:
    """Read recorded worker trees directly; discovery is not evidence that a recorded tree is unreadable."""
    if now.utcoffset() is None:
        raise ValueError("timestamps must include a timezone")
    claims: list[readiness.Claim] = []
    asks: list[readiness.Ask] = []
    reports: list[readiness.Report] = []
    read_only: list[str] = []
    for item in store.items():
        # worker.launch also records the worktree path here; that is a tree claim, not a file scope.
        rows = [row for row in store.ownership(item.issue_key)
                if row.mode != "tree" and not _in_trees(root, trees, row.path)]
        claims.extend(readiness.Claim(item.issue_key, row.path, row.mode) for row in rows)
        if rows and all(row.mode == "read" for row in rows):
            read_only.append(item.issue_key)
        asks.extend(readiness.Ask(row.issue_key, row.answer, row.stage, row.attempt) for row in store.asks(item.issue_key))
    alive: list[tuple[tuple[str, str, int], bool]] = []
    changed: list[tuple[str, bool | None]] = []
    on_main: list[tuple[str, bool | None]] = []
    tree_rows: list[tuple[str, Any]] = []
    names: list[tuple[str, str]] = []
    tree_claims: list[tuple[str, tuple]] = []
    progress: list[tuple[str, datetime | None]] = []
    suite_on_main: list[tuple[str, bool | None]] = []
    pipeline = None
    try:
        if callable(getattr(forge, "branch_head", None)):
            pipeline = forge.branch_head("main")
    except (ForgeError, OSError, ValueError, TypeError):
        pass  # unknown CI never certifies completion
    latest = {}
    for segment in store.segments():
        latest[segment.issue_key] = segment
    for segment in latest.values():
        record = _record(store, segment.issue_key, segment.stage, segment.attempt)
        flag = _alive(record) if segment.ended_at is None else False
        if flag is not None:
            alive.append(((segment.issue_key, segment.stage, segment.attempt), flag))
        raw = record.get("tree") if record else None
        tree = Path(raw) if isinstance(raw, str) and raw else None
        tree = tree if tree is None or any(tree.resolve().is_relative_to(base.resolve()) for base in (root, trees)) else None
        state, landed, stamp = (None, None, None) if tree is None else _tree_facts(tree)
        if state is not None:
            tree_rows.append((segment.issue_key, state))
            names.append((segment.issue_key, tree.name))
            tree_claims.append((segment.issue_key, (Claim(segment.issue_key, flag is not False,
                                                         "" if flag is not False else "has no process"),)))
        changes = None if state is None or state.dirty is None else bool(state.dirty)
        if segment.issue_key in read_only:
            changes = _changed(state, tree, record.get("starting_head") if record else None)
        changed.append((segment.issue_key, changes))
        if landed is not None:
            on_main.append((segment.issue_key, landed))
        # Unknown progress is no evidence of progress; the bounded silence rule must still run.
        progress.append((segment.issue_key, stamp if isinstance(stamp, datetime) else None))
        suite = None
        if state is not None and pipeline is not None and isinstance(pipeline.sha, str) and pipeline.sha:
            code = git_trees.git(["merge-base", "--is-ancestor", "HEAD", pipeline.sha], tree)[0]
            if code == 0:
                suite = pipeline.status == "passed"
        suite_on_main.append((segment.issue_key, suite))
    # Read after liveness, in insertion order, limited to the latest segment's stage and attempt.
    for item in store.items():
        segment = latest.get(item.issue_key)
        reports.extend(readiness.Report(row.issue_key, row.outcome, row.stage, row.attempt)
                       for row in sorted(store.reports(item.issue_key), key=lambda row: row.id)
                       if segment is None or (row.stage, row.attempt) == (segment.stage, segment.attempt))
    blockers, why = _blockers(forge)
    return Facts(claims=tuple(claims), blockers=None if blockers is None else tuple(blockers.items()), blockers_error=why,
                 asks=tuple(asks), reports=tuple(reports), alive=tuple(alive), read_only=tuple(read_only),
                 changed=tuple(changed), on_main=tuple(on_main), suite_on_main=tuple(suite_on_main),
                 trees=tuple(tree_rows), tree_names=tuple(names),
                 tree_claims=tuple(tree_claims), progress=tuple(progress))
