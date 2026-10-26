"""Gather one pass's git, worktree, process and forge facts. Frameworks and Drivers.

The store is the one `store_access` opened for the pass. Judgements stay in the use cases;
this module only reads.
"""

from __future__ import annotations

import subprocess
import json
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any

import dispatch_prompt
import completion
import decision_escalation
import git_trees
import git_view
import process_status
import readiness
from forge_port import ForgeError
from orphans import Claim
from reconcile import Facts, latest_segment


def recovery_candidates(store: Any, key: str, *, trees: Path, clone: Path | None,
                        root: Path | None = None) -> tuple[completion.Candidate, ...] | None:
    """Read one stopped issue's survivors. None means no recovery is due (or no clone)."""
    if clone is None:
        return None
    data = store.resume(key) or {}
    history = store.segments(key)
    latest = history[-1] if history else None
    record = _record(store, key, latest.stage, latest.attempt) if latest else None
    if record is None and latest is not None and root is not None:
        run = root / ".chief-of-stuff" / "runs" / f"{key.rpartition('#')[2]}-{latest.stage}-{latest.attempt}"
        if (run / "started.json").exists():
            record = json.loads((run / "started.json").read_text())
            record["run_dir"] = str(run)
    decision_open = any(row.topic == "recovery" and (not row.answer or row.answer == "hold")
                        for row in decision_escalation.latest(store.decisions(key)).values())
    if "recovery" not in data and not decision_open:
        if latest is None or latest.ended_at is not None or record is None:
            return None
        if record.get("run_dir") and (Path(record["run_dir"]) / "exit.json").exists():
            return None  # ordinary, fully recorded exits already have a checkout for retry
        # Even an exit file is not permission to move a tree while its host still runs.
        for field, host in (("pid", True), ("runner_pid", False)):
            pid, session = record.get(field), record.get("session_id")
            if type(pid) is not int or pid <= 0 or not session:
                return None
            if process_status.identity(pid, session, host=host) is not None:
                return None
    common = git_view.locate(clone).common
    number = key.rpartition("#")[2]
    prefix = f"work/issue-{number}-"
    paths = {p for p in trees.glob(f"issue-{number}-*") if p.is_dir()}
    if raw := data.get("worktree"):
        path = Path(raw).resolve()
        if path.is_relative_to(trees.resolve()) and "quarantine" not in path.relative_to(trees.resolve()).parts:
            paths.add(path)
    code, refs = git_trees.git(["for-each-ref", "--format=%(refname)", f"refs/quarantine/{number}/"], clone)
    if code:
        raise RuntimeError("cannot read quarantined branches")
    quarantined = {ref.split("/", 4)[4] for ref in refs.splitlines() if len(ref.split("/", 4)) == 5}
    candidates, checked_out = [], set()
    # A quarantined checkout can still name its old ref if the engine died during the rename.
    for path in (trees / "quarantine").glob("**/.git"):
        code, branch = git_trees.git(["symbolic-ref", "--short", "HEAD"], path.parent, expected_common=common)
        if code == 0:
            checked_out.add(branch)
    for path in sorted(paths):
        path = path.resolve()
        if not path.is_relative_to(trees.resolve()) or "quarantine" in path.relative_to(trees.resolve()).parts:
            continue
        code, branch = git_trees.git(["symbolic-ref", "--short", "HEAD"], path, expected_common=common)
        ok, head = git_trees.git(["rev-parse", "--verify", "HEAD"], path, expected_common=common)
        readable = code == 0 and ok == 0 and branch.startswith(prefix)
        # Preserve a known ref even when the checkout's metadata is damaged.
        branch = branch if code == 0 else (data.get("work_branch", "") if str(path) == data.get("worktree") else "")
        if not branch.startswith(prefix):
            branch = ""
        if branch:
            checked_out.add(branch)
        try:
            layout = git_view.locate(path, expected_common=common)
            merging = any((layout.gitdir / name).exists() for name in ("MERGE_HEAD", "rebase-merge", "rebase-apply"))
        except git_view.Unviewable:
            merging = False
        candidates.append(completion.Candidate(str(path), branch, head if ok == 0 else "", readable,
                                               branch in quarantined, merging))
    code, refs = git_trees.git(["for-each-ref", "--format=%(refname:short) %(objectname)", f"refs/heads/{prefix}*"], clone)
    if code:
        raise RuntimeError("cannot read orphan branches")
    for line in refs.splitlines():
        branch, _, head = line.partition(" ")
        if branch not in checked_out:
            candidates.append(completion.Candidate("", branch, head, True, branch in quarantined, False))
    return tuple(candidates)


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


def _tree_facts(tree: Path) -> tuple[Any, bool | None, datetime | None | object]:
    try:
        with git_view.opened(tree, git_trees.audit_env()) as view:
            state = git_trees.read_state(tree, view)
            if state.dirty is not None and state.unpushed is None:
                # Detached and --no-track checkouts can still be retained by a remote branch.
                code, refs = git_trees.git(["for-each-ref", "--format=%(objectname)", "refs/remotes"], tree, view)
                if code == 0:
                    code, count = git_trees.git(["rev-list", "--count", "HEAD", *("^" + oid for oid in refs.splitlines())], tree, view)
                    state = replace(state, unpushed=int(count) if code == 0 and count.isdecimal() else None)
            return state, _on_main(tree, view), _progress(tree, view)
    except (OSError, git_view.Unviewable, git_view.CacheError, subprocess.SubprocessError, ValueError, RuntimeError):
        return None, None, object()


def _recorded_tree(raw: Any, root: Path, trees: Path) -> Path | None:
    try:
        tree = None
        if isinstance(raw, str) and raw:
            try:
                tree = Path(raw).resolve(strict=True)
            except FileNotFoundError:
                tree = Path(raw).resolve()
        return tree if tree is not None and any(tree.is_relative_to(base.resolve()) for base in (root, trees)) else None
    except (OSError, RuntimeError, ValueError):
        return None


def _implementation_requests(forge: Any, store: Any, observed: dict[str, bool | None]) -> tuple:
    """Bind released work to the same open request review launch selects; unread evidence stays unknown."""
    candidates = []
    for item in store.items():
        key = item.issue_key
        data = store.resume(key)
        data = data if isinstance(data, dict) else {}
        if (item.forge_state != "open" or item.stage != "implement" or item.human_review
                or observed.get(key) is not False or "suite_hold" in data):
            continue
        history = store.segments(key)
        if any(s.ended_at is None for s in history):
            continue
        completed = [s for s in history if s.started_at and s.ended_at and s.stage != "architect"]
        latest, ambiguous = latest_segment(completed)
        if latest is None or ambiguous or latest.stage != "implement" or latest.outcome != "human_review":
            continue
        runs = data.get("runs", {})
        run_name = f"implement-{latest.attempt}"
        if not isinstance(runs, dict) or (run_name in runs and not isinstance(runs[run_name], dict)):
            candidates.append(((key, latest.stage, latest.attempt), None))
            continue
        record = _record(store, key, latest.stage, latest.attempt)
        branch = record.get("branch") if record else None
        if branch is None or (isinstance(branch, str) and not branch.strip()):
            # The shared branch belongs to the current attempt, never to an older segment with a later clock.
            current_attempt = max(s.attempt for s in history if s.stage == "implement")
            branch = data.get("work_branch") if latest.attempt == current_attempt else None
        candidates.append(((key, latest.stage, latest.attempt), branch))
    result = {identity: None for identity, _ in candidates}
    if not any(isinstance(branch, str) and branch.strip() for _, branch in candidates):
        return tuple(result.items())
    try:
        requests = forge.requests()
        if not isinstance(requests, list) or any(
                type(r.number) is not int or r.number < 1 or not isinstance(r.branch, str) or not r.branch.strip()
                or not isinstance(r.closes, tuple) or any(not isinstance(ref, str) for ref in r.closes)
                for r in requests):
            return tuple(result.items())
    except (ForgeError, OSError, ValueError, TypeError, AttributeError):
        return tuple(result.items())
    for identity, branch in candidates:
        if not isinstance(branch, str) or not branch.strip():
            continue
        try:
            request = forge.closing_request(identity[0], requests)
            result[identity] = request.branch == branch
        except ForgeError as exc:
            if str(exc) == f"no open request closes {identity[0]}":
                result[identity] = False
        except (OSError, ValueError, TypeError, AttributeError):
            pass
    return tuple(result.items())


def gather(*, forge: Any, store: Any, root: Path, trees: Path, now: datetime,
           require_green: bool = False, hold_observations: tuple[tuple[str, bool | None], ...] = ()) -> Facts:
    """Read recorded worker trees directly; discovery is not evidence that a recorded tree is unreadable."""
    if now.utcoffset() is None:
        raise ValueError("timestamps must include a timezone")
    asks: list[readiness.Ask] = []
    reports: list[readiness.Report] = []
    for item in store.items():
        asks.extend(readiness.Ask(row.issue_key, row.answer, row.stage, row.attempt) for row in store.asks(item.issue_key))
    alive: list[tuple[tuple[str, str, int], bool]] = []
    unreadable: list[str] = []
    on_main: list[tuple[str, bool | None]] = []
    tree_rows: list[tuple[str, Any]] = []
    names: list[tuple[str, str]] = []
    tree_claims: list[tuple[str, tuple]] = []
    progress: list[tuple[str, datetime | None]] = []
    suite_on_main: list[tuple[str, bool | None]] = []
    recorded_trees: dict[str, Path] = {}
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
        tree = _recorded_tree(raw, root, trees)
        state, landed, stamp = (None, None, None) if tree is None else _tree_facts(tree)
        if state is None or state.dirty is None:
            unreadable.append(segment.issue_key)
        if state is not None:
            recorded_trees[segment.issue_key] = tree
            tree_rows.append((segment.issue_key, state))
            names.append((segment.issue_key, tree.name))
            tree_claims.append((segment.issue_key, (Claim(segment.issue_key, flag is not False,
                                                         "" if flag is not False else "has no process"),)))
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
    merged = [item.issue_key for item in store.items()
              if item.forge_state == "closed" and item.merge_state == "merged"]
    pending, remembered = [], []
    landing_heads = {}
    for key in merged:
        data = store.resume(key)
        if isinstance(data, dict) and data.get("verified_landing") is True:
            remembered.append(key)
            if head := data.get("verified_landing_head"):
                landing_heads[key] = head
        else:
            pending.append(key)
    landings = {}
    landing_errors = {}
    if pending and pipeline is not None and callable(getattr(forge, "landings", None)):
        try:
            landings = forge.landings(pipeline, pending, errors=landing_errors, heads=landing_heads)
        except (ForgeError, OSError, ValueError, TypeError) as exc:
            landing_errors.update((key, str(exc)) for key in pending)
    local_on_main = dict(on_main)
    for key in pending:
        landed, suite = landings.get(key, (None, None))
        on_main.append((key, landed))
        suite_on_main.append((key, suite))
    for key in remembered:
        on_main.append((key, True))
        suite_on_main.append((key, True))
    landed_trees = []
    for key, head in landing_heads.items():
        if key in recorded_trees:
            code, local_head = git_trees.git(["rev-parse", "HEAD"], recorded_trees[key])
            landed_trees.append((key, code == 0 and (local_head == head or local_on_main.get(key) is True)))
    # Read after liveness, using the same recorded-session selector as worker.collect.
    suites = []
    for item in store.items():
        segment = latest.get(item.issue_key)
        rows = (store.segment_reports(item.issue_key, segment.stage, segment.attempt) if segment else
                sorted(store.reports(item.issue_key), key=lambda row: row.id))
        reports.extend(readiness.Report(row.issue_key, row.outcome, row.stage, row.attempt) for row in rows)
        if rows and rows[-1].outcome == "done":
            raw = rows[-1].suite if isinstance(rows[-1].suite, dict) else {}
            suites.append(((rows[-1].issue_key, rows[-1].stage, rows[-1].attempt),
                           (dispatch_prompt.suite_counts(raw), rows[-1].summary or "")))
    blockers, why = _blockers(forge)
    return Facts(blockers=None if blockers is None else tuple(blockers.items()), blockers_error=why,
                 asks=tuple(asks), reports=tuple(reports), alive=tuple(alive),
                 unreadable=tuple(unreadable), on_main=tuple(on_main), suite_on_main=tuple(suite_on_main),
                 landing_heads=tuple(landing_heads.items()), landed_trees=tuple(landed_trees),
                 trees=tuple(tree_rows), tree_names=tuple(names),
                 tree_claims=tuple(tree_claims), progress=tuple(progress), landing_errors=tuple(landing_errors.items()),
                 suites=tuple(suites), require_green=require_green,
                 hold_observations=hold_observations,
                 implementation_requests=_implementation_requests(forge, store, dict(hold_observations)),
                 suite_holds=tuple(item.issue_key for item in store.items()
                                   if isinstance(data := store.resume(item.issue_key), dict) and "suite_hold" in data))
