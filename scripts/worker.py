"""Production worker launch and completion, using engine-owned evidence (M3.2, E3, E5, #551)."""

from __future__ import annotations

import json
import re
import time
import uuid
from pathlib import Path
from typing import Any

import claude_adapter
import clock as engine_clock
import dispatch_prompt
import guard_binding
import make_worktree
import process_status
import reconcile
import supervisor
from settings import load as load_settings
from workspace import settings_path
from forge_port import ForgeError  # noqa: F401  (launch's documented failure)
from model_port import Failure, Request, retry_at

_runners: dict[str, Any] = {}


def _resume(store: Any, key: str) -> dict:
    data = store.resume(key)
    return dict(data) if isinstance(data, dict) else {}


def _run_dir(root: Path, key: str, stage: str, attempt: int) -> Path:
    number = key.rpartition("#")[2]
    if not number.isdecimal() or not re.fullmatch(r"[A-Za-z0-9_-]+", stage) or attempt < 1:
        raise ValueError("invalid worker issue, stage or attempt")
    return supervisor.state_dir(root) / "runs" / f"{int(number)}-{stage}-{attempt}"


def _exit_code(run_dir: Path) -> int:
    code = json.loads((run_dir / "exit.json").read_text())["exit_code"]
    if type(code) is not int:
        raise ValueError("invalid adapter exit code")
    return code


def _existing_run(data: dict, run_dir: Path, stage: str, attempt: int,
                  trees: Path, clock: engine_clock.Clock) -> dict | None:
    """Recover a detached start before changing its tree or moving its exit evidence."""
    if not run_dir.exists():
        return None
    record = data.get("runs", {}).get(f"{stage}-{attempt}")
    if record is None:
        started = run_dir / "started.json"
        if not started.exists():
            if (run_dir / "command.json").exists() and not (run_dir / "exit.json").exists():
                raise RuntimeError("existing worker start is unverified; preserving its evidence")
            return None
        record = json.loads(started.read_text())
        prepared = data.get("review_trees", {}).get(str(attempt), {}) if stage == "review" else {
            "tree": data.get("worktree"), "branch": data.get("work_branch")}
        record.update(run_dir=str(run_dir), tree=prepared.get("tree"), branch=prepared.get("branch"),
                      started_at=clock().isoformat())
    else:
        record = dict(record)
    if stage == "review":
        record.setdefault("reviewed_head", data.get("review_trees", {}).get(str(attempt), {}).get("head"))
    if (any(type(record.get(field)) is not int or record[field] <= 0 for field in ("pid", "runner_pid"))
            or not isinstance(record.get("session_id"), str) or not record["session_id"]
            or not isinstance(record.get("tree"), str) or not record["tree"]
            or Path(record.get("run_dir", "")).resolve() != run_dir.resolve()
            or not Path(record["tree"]).resolve().is_relative_to(trees.resolve())
            or run_dir.resolve().is_relative_to(Path(record["tree"]).resolve())):
        raise RuntimeError("existing worker identity is incomplete; preserving its evidence")
    try:
        _exit_code(run_dir)
    except (OSError, ValueError, KeyError, TypeError):
        pass
    else:
        # The adapter's atomic exit write survives both processes and a missing slot claim.
        return record
    host = process_status.identity(record["pid"], record["session_id"], host=True)
    runner = process_status.identity(record["runner_pid"], record["session_id"])
    host_live = host is not None and ("identity" not in record or host == record["identity"])
    runner_live = runner is not None and ("runner_identity" not in record or runner == record["runner_identity"])
    if not host_live and not runner_live:
        return None
    record.setdefault("identity", host)
    record.setdefault("runner_identity", runner or "")
    if not record["identity"]:
        # The runner may still be between spawning the host and its exec through the login shell.
        raise RuntimeError("existing worker host is unverified; preserving its evidence")
    return record


def _save_run(store: Any, key: str, stage: str, attempt: int, record: dict) -> None:
    data = _resume(store, key)
    data["runs"] = {**data.get("runs", {}), f"{stage}-{attempt}": record}
    data.pop("quota", None)
    store.set_resume(key, data)


def _declared_agent(text: str, agent: str) -> str:
    """Pass the agent type only when the workspace's Agent: lines declare it (§9)."""
    if not agent:
        return ""
    return agent if agent in make_worktree.agent_types(text) else ""


def launch(issue_key: str, stage: str, attempt: int, persona: str | None, *, store: Any,
           root: Path, trees: Path, policies: Any, forge: Any, clone: Path | None = None,
           claude_as_profile: str = "", rules: tuple[str, ...] = (), require_green: bool = False,
           clock: engine_clock.Clock = engine_clock.system) -> tuple[str, str]:
    """The Launch port: verify a live start or adopt durable completion before slots records it."""
    name = persona or policies.stages[stage].persona
    if name not in policies.personas:
        raise ValueError(f"no worker persona for {stage}")
    actor = policies.personas[name]
    run_dir = _run_dir(root, issue_key, stage, attempt)
    data = _resume(store, issue_key)
    existing = _existing_run(data, run_dir, stage, attempt, trees, clock)
    if existing is not None:
        store.set_ownership(issue_key, existing["tree"])
        _save_run(store, issue_key, stage, attempt, existing)
        return actor.model, actor.effort or ""
    text = (root / "CLAUDE.md").read_text() if (root / "CLAUDE.md").is_file() else ""
    loaded = load_settings(root, settings_path(text))
    defaults = loaded.stages.get(stage)
    agent = _declared_agent(text, actor.agent or (defaults.agent if defaults else "") or "")
    row = next(item for item in store.items() if item.issue_key == issue_key)
    got = forge.issue(int(issue_key.rpartition("#")[2]))
    if not got.ok or got.issue is None:
        raise RuntimeError(f"cannot read worker issue: {got.error}")
    assignment = dispatch_prompt.compose_issue(row, got.issue.title, got.issue.body, stage, actor, root=root,
                                               skills=defaults.skills if defaults else (), rules=rules,
                                               require_green=require_green)
    number = int(issue_key.rpartition("#")[2])
    tree_name = f"issue-{number}-{stage}-{attempt}"
    branch = f"work/{tree_name}"
    # build's from_branch is detached. Reviews use it; fixes need the original writable checkout.
    prepared_review = data.get("review_trees", {}).get(str(attempt)) if stage == "review" else None
    request_head = forge.closing_request(issue_key) if stage == "review" else None
    if stage == "review" and (request_head is None or not request_head.branch or not request_head.head):
        raise RuntimeError("request head branch or SHA is unreadable")
    if prepared_review:
        tree, branch = Path(prepared_review["tree"]), prepared_review["branch"]
        head = make_worktree.git(["rev-parse", "HEAD"], tree)
        if head[0] or not tree.resolve().is_relative_to(trees.resolve()):
            raise RuntimeError("prepared review tree is unreadable")
        if request_head.branch != branch:
            raise RuntimeError("request branch changed since the refused start")
        store.set_ownership(issue_key, str(tree))
        if prepared_review.get("head") != request_head.head or head[1] != request_head.head:
            # Retain the obsolete tree under cleanup ownership; build the new head in a fresh checkout.
            tree_name = f"{tree_name}-{uuid.uuid4()}"
            prepared_review = None
    if stage != "review" and data.get("worktree"):
        tree, branch = Path(data["worktree"]), data["work_branch"]
        code, actual = make_worktree.git(["symbolic-ref", "--short", "HEAD"], tree)
        if code or actual != branch or not tree.resolve().is_relative_to(trees.resolve()):
            raise RuntimeError("existing worktree is missing or no longer on the work branch")
        store.set_ownership(issue_key, str(tree))
    elif not prepared_review:
        started = any(name.startswith(f"{stage}-") for name in data.get("runs", {}))
        # A failed start before any worker (such as an unreadable issue) left no tree to lose.
        if stage == "fix" or (stage != "review" and attempt > 1 and started):
            raise RuntimeError("fix/retry has no existing worktree; refusing a fresh branch")
        from_branch = request_head.branch if request_head is not None else ""
        branch = from_branch or branch
        made = make_worktree.build(root, clone or root, str(trees), tree_name, branch,
                                   agent, from_branch=from_branch)
        tree = made.path
        store.set_ownership(issue_key, str(tree))
        if stage != "review":
            # Keep this even after a refused start: a relaunch can use the tree without a fresh cut (#551).
            data.update(worktree=str(tree), work_branch=branch)
            store.set_resume(issue_key, data)
        else:
            data["review_trees"] = {**data.get("review_trees", {}), str(attempt): {
                "tree": str(tree), "branch": branch, "head": request_head.head}}
            store.set_resume(issue_key, data)
            code, actual_head = make_worktree.git(["rev-parse", "HEAD"], tree)
            if code or actual_head != request_head.head:
                raise RuntimeError("prepared review tree does not match the request head")
    if run_dir.resolve().is_relative_to(tree.resolve()):
        raise RuntimeError("engine run directory must be outside the worker tree")
    # Read before touching evidence: an unreadable forge refuses the start, never launches with defaults only.
    protected = tuple(dict.fromkeys((*guard_binding.DEFAULT_PROTECTED_BRANCHES, *forge.protected_branches())))
    # A failed start may be retried at the same attempt. Preserve its evidence under a unique sibling.
    if run_dir.exists():
        run_dir.rename(run_dir.with_name(f"{run_dir.name}-refused-{uuid.uuid4()}"))
    run_dir.mkdir(parents=True)
    session_id = str(uuid.uuid4())
    plugin = guard_binding.guard_plugin_dir()
    if plugin is None:
        raise RuntimeError("worker launch requires the guard/ plugin beside scripts/")
    plugin_dirs = (str(plugin),)
    env = {"CHIEF_OF_STUFF_WORKSPACE": str(root.resolve()),
           **guard_binding.guard_env(guard_binding.Binding(
               issue_key, stage, attempt, session_id, root.resolve(), clock(), protected))}
    request = Request(assignment, model=actor.model, effort=actor.effort or "", session_id=session_id,
                      cwd=str(tree), worker=True, agent=agent,
                      plugin_dirs=plugin_dirs)
    runner = claude_adapter.start(request, run_dir, claude_as_profile=claude_as_profile, env=env)
    try:
        deadline = time.monotonic() + 5
        while not (run_dir / "started.json").exists():
            if runner.poll() is not None or time.monotonic() >= deadline:
                raise RuntimeError("could not verify worker start: adapter did not start a host")
            time.sleep(0.01)
        pid = json.loads((run_dir / "started.json").read_text())["pid"]
        while True:
            identity = process_status.identity(pid, session_id, host=True)
            runner_identity = process_status.identity(runner.pid, session_id)
            if identity is not None and runner_identity is not None and runner.poll() is None:
                break
            if runner.poll() is not None or time.monotonic() >= deadline:
                raise RuntimeError("could not verify worker start: process exited or identity mismatched")
            time.sleep(0.01)
        record = {"pid": pid, "session_id": session_id, "identity": identity,
                  "runner_pid": runner.pid, "runner_identity": runner_identity,
                  "run_dir": str(run_dir), "started_at": clock().isoformat(), "tree": str(tree), "branch": branch}
        if stage == "review":
            record["reviewed_head"] = request_head.head
        _save_run(store, issue_key, stage, attempt, record)
    except Exception:
        claude_adapter.cancel(runner)
        raise
    _runners[str(run_dir)] = runner
    return actor.model, actor.effort or ""


def collect(store: Any, root: Path, clock: engine_clock.Clock = engine_clock.system,
            trees: Path | None = None, *, require_green: bool = False) -> None:
    """Collect engine run evidence and session reports; persist suite holds for the engine to publish."""
    for segment in store.segments():
        if segment.ended_at is not None:
            continue
        key = segment.issue_key
        data = _resume(store, key)
        now = clock()
        outcome, reason = "failed", ""
        try:
            runs = data.get("runs", {})
            if not isinstance(runs, dict):
                raise ValueError("unreadable run records")
            record = runs.get(f"{segment.stage}-{segment.attempt}")
            if record is None and trees is not None:
                # slots.claim reserves before launch saves the record: adopt a start that outlived the engine.
                run_dir = _run_dir(root, key, segment.stage, segment.attempt)
                try:
                    record = _existing_run(data, run_dir, segment.stage, segment.attempt, trees, clock)
                except RuntimeError:
                    # command.json without started.json (or incomplete identity): keep the reservation open.
                    continue
                if record is not None:
                    _save_run(store, key, segment.stage, segment.attempt, record)
                    data = _resume(store, key)
                    record = data["runs"][f"{segment.stage}-{segment.attempt}"]
            if not isinstance(record, dict):
                raise ValueError("missing run record")
            if (type(record.get("pid")) is not int or record["pid"] <= 0
                    or any(not isinstance(record.get(field), str) or not record[field]
                           for field in ("session_id", "run_dir", "tree"))):
                raise ValueError("incomplete run identity")
            run_dir = _run_dir(root, key, segment.stage, segment.attempt)
            if Path(record["run_dir"]).resolve() != run_dir or run_dir.is_relative_to(Path(record["tree"]).resolve()):
                raise ValueError("run record is outside the engine evidence directory")
            runner = _runners.get(str(run_dir))
            if runner is not None and runner.poll() is not None:
                _runners.pop(str(run_dir))
            if not (run_dir / "exit.json").exists():
                if not isinstance(record.get("identity"), str) or not record["identity"]:
                    raise ValueError("incomplete run identity")
                try:
                    alive = process_status.identity(record["pid"], record["session_id"], host=True)
                    if alive == record["identity"]:
                        continue
                    # The runner may be between wait() and its atomic exit write.
                    if process_status.identity(record["runner_pid"], record["session_id"]) == record["runner_identity"]:
                        continue
                except (OSError, RuntimeError) as exc:
                    store.add_log(key, f"worker identity unverified: {exc}", now.isoformat())
                    continue
                raise ValueError("missing adapter exit record")
            code = _exit_code(run_dir)
            reply = claude_adapter.classify(code, (run_dir / "stdout").read_text(), (run_dir / "stderr").read_text())
            if isinstance(reply, Failure):
                if reply.retryable:
                    outcome = reply.kind
                    reason = reply.message
                    stops = sum(s.outcome == outcome for s in store.segments(key)) + 1
                    data["quota"] = {"reset": reason, "retry_at": retry_at(reason, now, stops).isoformat()}
                    store.set_resume(key, data)
                else:
                    outcome, reason = "failed", reply.message
            else:
                # A clean exit is not completion: the worker's own report for this session decides.
                reports = store.reports(key, stage=segment.stage, attempt=segment.attempt,
                                        session_id=record["session_id"])
                if not reports:
                    reason = "worker stopped without reporting an outcome"
                elif reports[-1].outcome == "done":
                    raw = reports[-1].suite if isinstance(reports[-1].suite, dict) else {}
                    counts = dispatch_prompt.suite_counts(raw)
                    if reconcile.suite_holds(counts, require_green=require_green):
                        outcome = "human_review"
                        shown = ", ".join(
                            f"{name}={counts[name] if counts[name] is not None else 'missing or invalid'}"
                            for name in dispatch_prompt.SUITE_KEYS)
                        reason = f"suite is not green; Suite: {shown}; worker said: {reports[-1].summary}"
                        store.set_human_review(key, True)
                        data["suite_hold"] = {"stage": segment.stage, "attempt": segment.attempt,
                                              "published": False}
                        store.set_resume(key, data)
                    else:
                        outcome = "done"
                else:
                    outcome, reason = reports[-1].outcome, reports[-1].summary
            if segment.stage == "review":
                # This attempt's collected result always names the SHA pinned at launch.
                record["result"] = {"reviewed_head": record.get("reviewed_head"),
                                    "outcome": outcome, "ended_at": now.isoformat()}
                store.set_resume(key, data)
        except (OSError, ValueError, KeyError, TypeError, RuntimeError, OverflowError) as exc:
            reason = f"unreadable worker run: {exc}"
        store.end_segment(key, segment.stage, segment.attempt, now.isoformat(), outcome)
        store.add_log(key, f"worker {segment.stage} attempt {segment.attempt}: {outcome}" + (f": {reason}" if reason else ""),
                      now.isoformat())
