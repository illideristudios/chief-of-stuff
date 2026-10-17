"""Production worker launch and completion, using engine-owned evidence (M3.2, E3, E5, #551)."""

from __future__ import annotations

import json
import re
import subprocess
import time
import uuid
from dataclasses import replace
from pathlib import Path
from typing import Any, NoReturn

import claude_adapter
import clock as engine_clock
import completion
import dispatch_prompt
import git_trees
import git_view
import guard_binding
import make_worktree
import process_status
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


def _read(path: Path) -> str:
    """A transcript, however the host cut it: a truncated multi-byte character must not hide the run."""
    return path.read_text(errors="replace") if path.is_file() else ""


def _finished(run_dir: Path) -> bool:
    """Whether the adapter's atomic exit write is there and readable."""
    if not (run_dir / "exit.json").exists():
        return False
    try:
        _exit_code(run_dir)
    except (OSError, ValueError, KeyError, TypeError):
        return False
    return True


def _inferred_exit_code(run_dir: Path, store: Any = None, key: str = "",
                        stage: str = "", attempt: int = 0, session_id: str = "") -> int | None:
    """Infer an exit code when the runner died before writing exit.json but the host left durable evidence."""
    code = claude_adapter.inferred_exit(_read(run_dir / "stdout"), _read(run_dir / "stderr"))
    if code is None and store is not None and key and session_id and store.reports(key, stage=stage, attempt=attempt, session_id=session_id):
        return 0
    return code


def _existing_run(data: dict, run_dir: Path, stage: str, attempt: int,
                  trees: Path, clock: engine_clock.Clock, *, store: Any = None, key: str = "") -> dict | None:
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
        if (run_dir / "actor.json").is_file():
            record.update(json.loads((run_dir / "actor.json").read_text()))
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
    if _finished(run_dir):
        # The adapter's atomic exit write survives both processes and a missing slot claim.
        return record
    host = process_status.identity(record["pid"], record["session_id"], host=True)
    runner = process_status.identity(record["runner_pid"], record["session_id"])
    host_live = host is not None and ("identity" not in record or host == record["identity"])
    runner_live = runner is not None and ("runner_identity" not in record or runner == record["runner_identity"])
    if not host_live and not runner_live:
        if _finished(run_dir):  # written while the identities were probed
            return record
        if not (run_dir / "exit.json").exists() and _inferred_exit_code(run_dir, store, key, stage, attempt, record["session_id"]) is not None:
            record.setdefault("identity", "exited")
            record.setdefault("runner_identity", "")
            return record
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


_CONFLICT_OPTIONS = ("abort", "hold")


def _conflict_question(stage: str, reason: str) -> str:
    """One answer per issue, stage and conflict reason. Tree heads are not part of it."""
    return f"{stage}\n{reason}"


def _ask_conflict(store: Any, key: str, stage: str, attempt: int, reason: str, clock: engine_clock.Clock) -> NoReturn:
    now = clock().isoformat()
    question = _conflict_question(stage, reason)
    rows = [d for d in store.decisions(key) if (d.topic, d.question) == ("conflict", question)]
    if not rows or rows[-1].answer not in _CONFLICT_OPTIONS:
        store.add_decision(key, "conflict", question)
    if not any(a.question == question and tuple(a.options) == _CONFLICT_OPTIONS for a in store.asks(key)):
        store.add_ask(key, stage, attempt, "", question, list(_CONFLICT_OPTIONS), reason)
    data = _resume(store, key)
    record = {"kind": "Ask", "gate": "conflict", "reason": reason}
    if data.get("interrupt") != record:
        store.add_log(key, f"ask: {reason}", now)
        data["interrupt"] = record
        store.set_resume(key, data)
    raise RuntimeError(reason)


def _conflict_choice(store: Any, key: str, stage: str, attempt: int, reason: str, clock: engine_clock.Clock) -> str:
    """The offered option, matched exactly. Anything else asks again."""
    question = _conflict_question(stage, reason)
    rows = [d for d in store.decisions(key) if (d.topic, d.question) == ("conflict", question)]
    answer = rows[-1].answer if rows and rows[-1].answer else ""
    if answer in _CONFLICT_OPTIONS:
        return answer
    _ask_conflict(store, key, stage, attempt, reason, clock)


def _common(repo: Path) -> Path | None:
    """The git common directory of a clone, bare or not; None when `repo` is no repository."""
    try:
        return git_view.locate(repo).common
    except git_view.Unviewable:
        return None


def _status(tree: Path, common: Path) -> tuple[bool, bool] | None:
    """(uncommitted work, unresolved merge conflicts), or None when git cannot say. Engine metadata is not work.
    Read raw: git_trees.git strips the first line's leading space, which belongs to its status column."""
    try:
        out = git_view.run(["status", "--porcelain"], tree, env=git_trees.audit_env(),
                           timeout=git_trees.GIT_TIMEOUT, expected_common=common)
    except (git_view.Unviewable, RuntimeError, ValueError, OSError, subprocess.SubprocessError):
        return None
    if out.returncode:
        return None
    lines = [line for line in out.stdout.splitlines() if line and line[3:] != ".chief-of-stuff" and not line[3:].startswith(".chief-of-stuff/")]
    return bool(lines), any(line[:2] in {"UU", "AA", "DD", "AU", "UA", "DU", "UD"} for line in lines)


def _read_tree(path: Path, common: Path) -> completion.OrphanTree:
    branch_code, branch = make_worktree.git(["symbolic-ref", "--short", "HEAD"], path, expected_common=common)
    head_code, head = make_worktree.git(["rev-parse", "HEAD"], path, expected_common=common)
    status = _status(path, common)
    if branch_code or head_code or status is None or not branch or not head:
        return completion.OrphanTree(str(path), "", "", unreadable=True)
    # Private Git views intentionally omit merge state; read only the validated metadata entry.
    layout = git_view.locate(path, expected_common=common)
    merging = (layout.gitdir / "MERGE_HEAD").exists()
    code, count = make_worktree.git(["rev-list", "--count", f"{git_trees.REF}..HEAD"], path, expected_common=common)
    return completion.OrphanTree(str(path), branch, head, dirty=status[0], conflicted=status[1] or merging,
                                 ahead=int(count) if code == 0 and count.isdecimal() else None)


def _read_branches(number: int, data: dict, repo: Path, common: Path,
                   checked_out: set[str]) -> list[completion.OrphanTree]:
    names: list[str] = [data["work_branch"]] if data.get("work_branch") else []
    for prefix, strip in ((f"refs/heads/work/issue-{number}-*", ""),
                          (f"refs/remotes/origin/work/issue-{number}-*", "origin/")):
        code, out = make_worktree.git(["for-each-ref", "--format=%(refname:short)", prefix], repo, expected_common=common)
        if code == 0 and out:
            names.extend(ref.removeprefix(strip) for ref in out.splitlines()
                         if ref and not ref.removeprefix(strip).startswith(f"work/issue-{number}-review-"))
    found = []
    for name in dict.fromkeys(names):
        if name in checked_out:
            continue
        for ref in (f"refs/heads/{name}", f"refs/remotes/origin/{name}"):
            code, sha = make_worktree.git(["rev-parse", "--verify", "-q", ref], repo, expected_common=common)
            if code == 0 and sha:
                found.append(completion.OrphanTree(None, name, sha))
    return found


def _with_ancestry(found: list[completion.OrphanTree], repo: Path, common: Path) -> list[completion.OrphanTree]:
    """Each candidate with which of the others' heads its history holds; an unanswerable comparison is unreadable."""
    heads = {c.head for c in found if not c.unreadable}
    if len(heads) < 2:
        return found
    out = []
    for c in found:
        if c.unreadable:
            out.append(c)
            continue
        held, failed = set(), False
        for other in heads - {c.head}:
            code, _ = git_trees.git(["merge-base", "--is-ancestor", other, c.head], repo, expected_common=common)
            if code == 0:
                held.add(other)
            elif code != 1:  # 1 is a real answer: not an ancestor
                failed = True
        out.append(replace(c, ancestors=frozenset(held), unreadable=failed))
    return out


def _read_orphans(candidates: list[Path], review_paths: set[Path], number: int, data: dict, trees: Path,
                  repo: Path, common: Path) -> list[completion.OrphanTree]:
    """Gather the facts `completion.recovery` judges: this repository's leftover trees, then branches without one."""
    found: list[completion.OrphanTree] = []
    seen: set[Path] = set()
    for path in candidates:
        if not path.is_dir() or not (path / ".git").exists():
            continue
        resolved = path.resolve()
        if resolved in seen or resolved in review_paths or not resolved.is_relative_to(trees.resolve()):
            continue
        seen.add(resolved)
        try:
            git_view.locate(path, expected_common=common)
        except git_view.Unviewable:
            try:
                git_view.locate(path)
            except git_view.Unviewable:
                found.append(completion.OrphanTree(str(path), "", "", unreadable=True))
            continue  # another repository's tree is none of ours to adopt
        found.append(_read_tree(path, common))
    checked_out = {c.branch for c in found if c.branch}
    return _with_ancestry(found + _read_branches(number, data, repo, common, checked_out), repo, common)


def _legacy_worktree(issue_key: str, number: int, stage: str, attempt: int, tree_name: str, branch: str,
                     agent: str, data: dict, store: Any, root: Path, trees: Path,
                     repo: Path) -> tuple[Path, str, bool]:
    """No repository to read: the saved worktree, or a fresh one for a first attempt."""
    if data.get("worktree"):
        tree, branch = Path(data["worktree"]), data["work_branch"]
        code, actual = make_worktree.git(["symbolic-ref", "--short", "HEAD"], tree)
        if code or actual != branch or not tree.resolve().is_relative_to(trees.resolve()):
            raise RuntimeError("existing worktree is missing or no longer on the work branch")
        store.set_ownership(issue_key, str(tree))
        return tree, branch, stage == "fix" or attempt > 1 or bool(data.get("runs"))
    if stage == "fix" or attempt > 1:
        raise RuntimeError("fix/retry has no existing worktree; refusing a fresh branch")
    made = make_worktree.build(root, repo, str(trees), tree_name, branch, agent)
    store.set_ownership(issue_key, str(made.path))
    data.update(worktree=str(made.path), work_branch=branch)
    store.set_resume(issue_key, data)
    return made.path, branch, False


def _resolve_worktree(issue_key: str, number: int, stage: str, attempt: int, tree_name: str, branch: str,
                      agent: str, data: dict, store: Any, root: Path, trees: Path, repo: Path,
                      clock: engine_clock.Clock) -> tuple[Path, str, bool]:
    """Reuse or recover an orphaned worktree/branch without asking; ask only on a genuine conflict (#725).
    Gathers the git facts and executes; `completion.recovery` decides (architecture §4)."""
    review_paths = {Path(v["tree"]).resolve() for v in data.get("review_trees", {}).values()
                    if isinstance(v, dict) and v.get("tree")}
    candidates: list[Path] = []
    if data.get("worktree"):
        candidates.append(Path(data["worktree"]))
    candidates.extend(Path(row.path) for row in store.ownership(issue_key))
    candidates.extend(p for p in sorted(trees.glob(f"issue-{number}-*"))
                      if not p.name.startswith(f"issue-{number}-review-"))
    common = _common(repo)
    if common is None:
        return _legacy_worktree(issue_key, number, stage, attempt, tree_name, branch, agent, data, store, root, trees, repo)

    if data.get("worktree") and Path(data["worktree"]).exists():
        tree = Path(data["worktree"])
        code, actual = make_worktree.git(["symbolic-ref", "--short", "HEAD"], tree)
        if code or (data.get("work_branch") and actual != data["work_branch"]) or not tree.resolve().is_relative_to(trees.resolve()):
            raise RuntimeError("existing worktree is missing or no longer on the work branch")

    # The facts, the tip they name and the fast-forward or re-cut must not change between reading and acting.
    abort = False
    with make_worktree.clone_lock(repo):
        found = _read_orphans(candidates, review_paths, number, data, trees, repo, common)
        preferred = str(Path(data["worktree"])) if data.get("worktree") else None
        chosen = completion.recovery(found, preferred=preferred)
        if isinstance(chosen, completion.Conflict):
            choice = _conflict_choice(store, issue_key, stage, attempt, chosen.reason, clock)
            if choice == "hold":
                raise RuntimeError(f"orphan recovery held: {chosen.reason}")
            abort = True
            chosen = None
        if chosen is not None and chosen.path is not None:
            tree = Path(chosen.path)
            if next(c.head for c in found if c.path == chosen.path) != chosen.head:
                code, detail = make_worktree.git_trusted(["merge", "--ff-only", chosen.head], tree)
                if code != 0:
                    reason = f"could not fast-forward orphaned worktree: {detail}"
                    if _conflict_choice(store, issue_key, stage, attempt, reason, clock) == "hold":
                        raise RuntimeError(f"orphan recovery held: {reason}")
                    abort, chosen = True, None
        elif chosen is not None:
            make_worktree.git_trusted(["worktree", "prune"], repo)
            if (data.get("worktree") and not Path(data["worktree"]).exists()
                    and Path(data["worktree"]).resolve().is_relative_to(trees.resolve())):
                tree = Path(data["worktree"]).resolve()
            else:
                tree = make_worktree.resolve(root, str(trees), tree_name)
            tree.parent.mkdir(parents=True, exist_ok=True)
            code, detail = make_worktree.git_trusted(
                ["worktree", "add", "--no-track", "-B", chosen.branch, "--", str(tree), chosen.head], repo)
            if code != 0:
                raise RuntimeError(f"git worktree add failed: {detail}")
    if abort:
        # A fresh tree from main. Its name cannot be one the conflicting checkouts already hold.
        fresh = f"{tree_name}-{uuid.uuid4()}"
        made = make_worktree.build(root, repo, str(trees), fresh, f"work/{fresh}", agent)
        store.set_ownership(issue_key, str(made.path))
        data.update(worktree=str(made.path), work_branch=f"work/{fresh}")
        store.set_resume(issue_key, data)
        return made.path, f"work/{fresh}", False
    if chosen is not None:
        recovered = (chosen.recovered or data.get("worktree") != str(tree) or attempt > 1 or bool(data.get("runs")))
        store.set_ownership(issue_key, str(tree))
        if data.get("worktree") != str(tree) or data.get("work_branch") != chosen.branch:
            data.update(worktree=str(tree), work_branch=chosen.branch)
            store.set_resume(issue_key, data)
        return tree, chosen.branch, recovered

    if data.get("worktree"):
        raise RuntimeError("existing worktree is missing or no longer on the work branch")
    if stage == "fix" or attempt > 1:
        raise RuntimeError("fix/retry has no existing worktree; refusing a fresh branch")
    made = make_worktree.build(root, repo, str(trees), tree_name, branch, agent)
    store.set_ownership(issue_key, str(made.path))
    data.update(worktree=str(made.path), work_branch=branch)
    store.set_resume(issue_key, data)
    return made.path, branch, False


def launch(issue_key: str, stage: str, attempt: int, persona: str | None, *, store: Any,
           root: Path, trees: Path, policies: Any, forge: Any, clone: Path | None = None,
           claude_as_profile: str = "", rules: tuple[str, ...] = (),
           clock: engine_clock.Clock = engine_clock.system) -> tuple[str, str]:
    """The Launch port: verify a live start or adopt durable completion before slots records it."""
    name = persona or policies.stages[stage].persona
    if name not in policies.personas:
        raise ValueError(f"no worker persona for {stage}")
    actor = policies.personas[name]
    run_dir = _run_dir(root, issue_key, stage, attempt)
    data = _resume(store, issue_key)
    existing = _existing_run(data, run_dir, stage, attempt, trees, clock, store=store, key=issue_key)
    if existing is not None:
        existing.setdefault("model", actor.model)
        existing.setdefault("effort", actor.effort or "")
        store.set_ownership(issue_key, existing["tree"])
        _save_run(store, issue_key, stage, attempt, existing)
        return actor.model, actor.effort or ""
    text = (root / "CLAUDE.md").read_text() if (root / "CLAUDE.md").is_file() else ""
    defaults = load_settings(root, settings_path(text)).stages.get(stage)
    agent = _declared_agent(text, actor.agent or (defaults.agent if defaults else "") or "")
    row = next(item for item in store.items() if item.issue_key == issue_key)
    got = forge.issue(int(issue_key.rpartition("#")[2]))
    if not got.ok or got.issue is None:
        raise RuntimeError(f"cannot read worker issue: {got.error}")
    number = int(issue_key.rpartition("#")[2])
    tree_name = f"issue-{number}-{stage}-{attempt}"
    branch = f"work/{tree_name}"
    recovered = False
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
    if stage != "review":
        tree, branch, recovered = _resolve_worktree(
            issue_key, number, stage, attempt, tree_name, branch, agent,
            data, store, root, trees, clone or root, clock)
    elif not prepared_review:
        from_branch = request_head.branch if request_head is not None else ""
        branch = from_branch or branch
        made = make_worktree.build(root, clone or root, str(trees), tree_name, branch,
                                   agent, from_branch=from_branch)
        tree = made.path
        store.set_ownership(issue_key, str(tree))
        data["review_trees"] = {**data.get("review_trees", {}), str(attempt): {
            "tree": str(tree), "branch": branch, "head": request_head.head}}
        store.set_resume(issue_key, data)
        code, actual_head = make_worktree.git(["rev-parse", "HEAD"], tree)
        if code or actual_head != request_head.head:
            raise RuntimeError("prepared review tree does not match the request head")
    assignment = dispatch_prompt.compose_issue(row, got.issue.title, got.issue.body, stage, actor, root=root,
                                               skills=defaults.skills if defaults else (), rules=rules, recover=recovered)
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
    (run_dir / "actor.json").write_text(json.dumps({"model": actor.model, "effort": actor.effort or ""}))
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
                  "run_dir": str(run_dir), "started_at": clock().isoformat(), "tree": str(tree), "branch": branch,
                  "model": actor.model, "effort": actor.effort or ""}
        if stage == "review":
            record["reviewed_head"] = request_head.head
        _save_run(store, issue_key, stage, attempt, record)
    except BaseException:  # an interrupt too: a start that was never verified must not leave a runner behind
        claude_adapter.cancel(runner)
        if not (run_dir / "started.json").exists():
            # cancel waits on the runner process: this command can no longer produce a start.
            run_dir.rename(run_dir.with_name(f"{run_dir.name}-canceled-{uuid.uuid4()}"))
        raise
    _runners[str(run_dir)] = runner
    return actor.model, actor.effort or ""


def collect(store: Any, root: Path, clock: engine_clock.Clock = engine_clock.system,
            trees: Path | None = None) -> None:
    """Finish every exited or lost open segment, using only the engine's run directory."""
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
                    record = _existing_run(data, run_dir, segment.stage, segment.attempt, trees, clock,
                                           store=store, key=key)
                except RuntimeError:
                    # command.json without started.json (or incomplete identity): keep the reservation open.
                    continue
                if record is not None:
                    _save_run(store, key, segment.stage, segment.attempt, record)
                    data = _resume(store, key)
                    record = data["runs"][f"{segment.stage}-{segment.attempt}"]
                elif not (run_dir / "exit.json").exists() and (
                        not any((run_dir / name).exists() for name in ("started.json", "command.json", "exit.json"))
                        or (run_dir / "started.json").is_file()):
                    # Nothing verified ran. An interrupt after started.json leaves the rename for this pass.
                    if (run_dir / "started.json").is_file():
                        run_dir.rename(run_dir.with_name(f"{run_dir.name}-canceled-{uuid.uuid4()}"))
                    store.drop_segment(key, segment.stage, segment.attempt)
                    if (data.get("pending_stage") or {}).get("stage") == segment.stage:
                        data.pop("pending_stage")
                        store.set_resume(key, data)
                    store.add_log(key, f"worker {segment.stage} attempt {segment.attempt}: dropped, nothing had started",
                                  now.isoformat())
                    continue
            if not isinstance(record, dict):
                raise ValueError("missing run record")
            if (type(record.get("pid")) is not int or record["pid"] <= 0
                    or any(not isinstance(record.get(field), str) or not record[field]
                           for field in ("session_id", "run_dir", "tree"))):
                raise ValueError("incomplete run identity")
            run_dir = _run_dir(root, key, segment.stage, segment.attempt)
            if Path(record["run_dir"]).resolve() != run_dir or run_dir.is_relative_to(Path(record["tree"]).resolve()):
                raise ValueError("run record is outside the engine evidence directory")
            if not segment.model and record.get("model"):
                # The claim was killed before it stamped who ran; the run record kept it.
                store.record_segment(key, segment.stage, segment.attempt, segment.started_at,
                                     model=record["model"], effort=record.get("effort"))
            runner = _runners.get(str(run_dir))
            if runner is not None and runner.poll() is not None:
                _runners.pop(str(run_dir))
            has_exit = (run_dir / "exit.json").exists()
            if not has_exit:
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
                has_exit = (run_dir / "exit.json").exists()  # written while the identities were probed
            if has_exit:
                code = _exit_code(run_dir)
            else:
                inferred = _inferred_exit_code(run_dir, store, key, segment.stage, segment.attempt, record["session_id"])
                if inferred is None:
                    raise ValueError("missing adapter exit record")
                code = inferred
            stdout = _read(run_dir / "stdout")
            stderr = _read(run_dir / "stderr")
            reply = claude_adapter.classify(code, stdout, stderr)
            reports = store.reports(key, stage=segment.stage, attempt=segment.attempt,
                                    session_id=record["session_id"])
            if isinstance(reply, Failure) and not (reply.kind == "unknown" and not has_exit and reports):
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
                if not reports:
                    reason = "worker stopped without reporting an outcome"
                elif reports[-1].outcome == "done":
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
