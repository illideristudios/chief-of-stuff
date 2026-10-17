"""Gather one pass's git, worktree, process and forge facts. Frameworks and Drivers.

The store is the one `store_access` opened for the pass. Judgements stay in the use cases;
this module only reads.
"""

from __future__ import annotations

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

# shortcut: nothing in the forge says the suite ran on main; unknown does not clear a done report


def _blockers(forge: Any) -> dict[str, tuple[str, ...] | None] | None:
    read = getattr(forge, "blocked_by", None)
    if read is None:
        return {}
    try:
        got = read()
    except (ForgeError, OSError, ValueError, TypeError):
        return None
    if got is None:
        return None
    if not isinstance(got, dict):
        return None
    return got


def _record(store: Any, issue_key: str, stage: str, attempt: int) -> dict | None:
    data = store.resume(issue_key)
    runs = data.get("runs") if isinstance(data, dict) else None
    record = runs.get(f"{stage}-{attempt}") if isinstance(runs, dict) else None
    return record if isinstance(record, dict) else None


def _alive(record: dict | None) -> bool | None:
    if record is None:
        return None
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


def _changed(state: Any) -> bool | None:
    if state is None or state.dirty is None:
        return None
    if state.dirty:
        return True
    if state.off_origin is None and state.unpushed is None:
        return None
    return bool(state.off_origin or state.unpushed)


def _tree_facts(tree: Path) -> tuple[Any, bool | None, datetime | None | object]:
    try:
        with git_view.opened(tree, git_trees.audit_env()) as view:
            return git_trees.read_state(tree, view), _on_main(tree, view), _progress(tree, view)
    except (OSError, git_view.Unviewable, ValueError, RuntimeError):
        return None, None, object()


def _trees_dir(root: Path, trees: Path) -> str:
    try:
        rel = trees.resolve().relative_to(root.resolve())
    except ValueError:
        return ""
    return "" if rel == Path(".") else rel.as_posix()


def gather(*, forge: Any, store: Any, root: Path, trees: Path, now: datetime) -> Facts:
    """Plain fact records for `reconcile.plan`. A tree is read only when `git_trees.discover` listed it."""
    if now.utcoffset() is None:
        raise ValueError("timestamps must include a timezone")
    claims: list[readiness.Claim] = []
    asks: list[readiness.Ask] = []
    reports: list[readiness.Report] = []
    read_only: list[str] = []
    for item in store.items():
        rows = store.ownership(item.issue_key)
        claims.extend(readiness.Claim(item.issue_key, row.path, row.mode) for row in rows)
        if rows and all(row.mode == "read" for row in rows):
            read_only.append(item.issue_key)
        asks.extend(readiness.Ask(row.issue_key, row.answer) for row in store.asks(item.issue_key))
        reports.extend(readiness.Report(row.issue_key, row.outcome, row.stage, row.attempt)
                       for row in store.reports(item.issue_key))
    alive: list[tuple[tuple[str, str, int], bool]] = []
    changed: list[tuple[str, bool | None]] = []
    on_main: list[tuple[str, bool | None]] = []
    tree_rows: list[tuple[str, Any]] = []
    names: list[tuple[str, str]] = []
    tree_claims: list[tuple[str, tuple]] = []
    progress: list[tuple[str, datetime | None]] = []
    seen: set[str] = set()
    try:
        known = {path.resolve() for path in git_trees.discover(root, _trees_dir(root, trees))}
    except OSError:
        known = set()
    for segment in store.segments():
        if segment.ended_at is not None:
            continue
        record = _record(store, segment.issue_key, segment.stage, segment.attempt)
        flag = _alive(record)
        if flag is not None:
            alive.append(((segment.issue_key, segment.stage, segment.attempt), flag))
        raw = record.get("tree") if record else None
        tree = Path(raw) if isinstance(raw, str) and raw else None
        try:
            outside = tree is None or tree.resolve() not in known
        except OSError:
            outside = True
        if outside or segment.issue_key in seen:
            continue
        seen.add(segment.issue_key)
        state, landed, stamp = _tree_facts(tree)
        if state is not None:
            tree_rows.append((segment.issue_key, state))
            names.append((segment.issue_key, tree.name))
            tree_claims.append((segment.issue_key, (Claim(segment.issue_key, flag is not False,
                                                         "" if flag is not False else "has no process"),)))
        change = _changed(state)
        if change is not None:
            changed.append((segment.issue_key, change))
        if landed is not None:
            on_main.append((segment.issue_key, landed))
        if stamp is None or isinstance(stamp, datetime):
            progress.append((segment.issue_key, stamp))
    blockers = _blockers(forge)
    return Facts(claims=tuple(claims), blockers=None if blockers is None else tuple(blockers.items()),
                 asks=tuple(asks), reports=tuple(reports), alive=tuple(alive), read_only=tuple(read_only),
                 changed=tuple(changed), on_main=tuple(on_main), trees=tuple(tree_rows), tree_names=tuple(names),
                 tree_claims=tuple(tree_claims), progress=tuple(progress))
