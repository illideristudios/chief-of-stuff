#!/usr/bin/env python3
"""Drive one workspace's reconcile plan, under a singleton lock (1.0 M3)."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import threading
import time
from collections.abc import Callable
from datetime import datetime
from functools import partial
from pathlib import Path
from typing import Any

import cleanup
import clock as engine_clock
import dispatch_prompt
import engine_facts
import engine_merge
import forge
import issue_gate
import limits_history
import limits_probe
import ownership
import policy
import reconcile
import settings
import slots
import startup
import store_access
import supervisor
import worker
from forge_port import ForgeError
from workspace import worktrees_dir

Launch = Callable[[str, str, int, str | None], tuple[str, str] | None]


def _resume(store: Any, key: str) -> dict:
    data = store.resume(key)
    return dict(data) if isinstance(data, dict) else {}


def _landing_shas(snapshots: list) -> dict[str, str]:
    return {row["issue_key"]: sha for row in snapshots
            if isinstance(sha := row.get("landing_sha"), str) and sha}


def _invalidate_landing(store: Any, shas: dict[str, str]) -> None:
    """A memo names the merge commit it proved. Reopen or a different commit drops it."""
    for item in store.items():
        data = _resume(store, item.issue_key)
        memo = data.get("verified_landing")
        if memo is None:
            continue
        closed = item.forge_state == "closed" and item.merge_state == "merged"
        sha = shas.get(item.issue_key)
        if not closed or (isinstance(sha, str) and memo != sha):
            data.pop("verified_landing", None)
            store.set_resume(item.issue_key, data)


def _remember_verified(store: Any, facts: reconcile.Facts, shas: dict[str, str]) -> None:
    """A closed merge proved on main with green CI is not re-read on later gathers."""
    on_main = dict(facts.on_main)
    suite = dict(facts.suite_on_main)
    for item in store.items():
        key = item.issue_key
        if item.forge_state != "closed" or item.merge_state != "merged":
            continue
        if on_main.get(key) is not True or suite.get(key) is not True:
            continue
        data = _resume(store, key)
        memo = data.get("verified_landing")
        if memo is True or (isinstance(memo, str) and memo):
            continue
        sha = shas.get(key)
        data["verified_landing"] = sha if isinstance(sha, str) and sha else True
        store.set_resume(key, data)


def _arm_suite_hold(store: Any, key: str, stage: str, attempt: int) -> None:
    data = _resume(store, key)
    data["suite_hold"] = {"stage": stage, "attempt": attempt, "published": False}
    store.set_resume(key, data)


def _planning_inputs(store: Any, items: list) -> dict:
    """Read the same issue context for the engine pass and merge recheck."""
    segments = {item.issue_key: store.segments(item.issue_key) for item in items}
    states = {item.issue_key: _resume(store, item.issue_key) for item in items}
    return {
        "segments": segments,
        "launcher_holds": {key: state.get("launcher_hold", {}) for key, state in states.items()},
        "issue_types": {item.issue_key: states[item.issue_key].get("issue_type")
                        or getattr(item, "issue_type", None) for item in items},
        "open_findings": {key: counts for key, history in segments.items()
                          if (counts := reconcile.review_findings(states[key], history)) is not None},
        "rooms": {key: state.get("room") for key, state in states.items()},
        "decisions": {item.issue_key: store.decisions(item.issue_key) for item in items},
        "quota_resets": {key: datetime.fromisoformat(quota["retry_at"]) for key, state in states.items()
                         if (quota := state.get("quota"))},
    }


def _write_stage(forge: Any, store: Any, key: str, stage: str, expected: str,
                *, release_hold: bool = False) -> None:
    result = forge.write_stage(int(key.rpartition("#")[2]), stage, expected=expected or None,
                               initialize=not expected, commit=True, release_hold=release_hold)
    if not result.ok:
        raise ForgeError(result.error)
    fields: dict[str, Any] = {"stage": stage}
    board = getattr(forge, "kanban", None)
    if board is not None and board.holds(stage):
        # A retried write runs before planning, so cache its new hold immediately.
        fields["human_review"] = True
    elif release_hold:
        fields["human_review"] = False
    store.upsert_item(key, **fields)
    data = _resume(store, key)
    data.pop("pending_stage", None)
    if release_hold:
        data.pop("launcher_hold", None)
        store.add_log(key, f"hold released for {stage}", store.clock().isoformat())
    store.set_resume(key, data)


def _restore_hold(forge: Any, store: Any, key: str) -> None:
    """Verify a durable hold restoration before consuming its local gate."""
    data = _resume(store, key)
    pending = data["pending_hold"]
    store.set_human_review(key, True)
    error = forge.add_human_hold(int(key.rpartition("#")[2]))
    if error:
        raise ForgeError(f"restoring hold failed: {error}")
    data.pop("pending_hold")
    if pending["retry"]:
        data["launcher_hold"] = {"stage": pending["stage"], "attempt": pending["attempt"]}
    store.set_resume(key, data)


def _record_limits(workers: settings.Workers) -> None:
    """A failed reading never stops the pass."""
    try:
        history = limits_history.LimitsHistory()
        try:
            history.record(limits_probe.read_all(workers))
        finally:
            history.engine.dispose()
    except Exception as exc:
        print(f"engine: limits reading failed: {exc}", file=sys.stderr, flush=True)


def _retry_restore(forge: Any, store: Any, key: str, at: str) -> None:
    """Retry a pending restoration; a failure keeps the local gate and logs only a change."""
    try:
        _restore_hold(forge, store, key)
    except Exception as exc:
        store.set_human_review(key, True)
        message = str(exc)
        logged = store.log(key)
        if not logged or logged[-1].message != message:
            store.add_log(key, message, at)


def _retire_ownership(forge: Any, store: Any, key: str, stage: str, at: str) -> None:
    """End automatic release: the hold becomes a normal human hold, restored now if a release removed it."""
    data = _resume(store, key)
    owned = data.pop("launcher_hold", None)
    if owned is None:
        return
    if (pending := data.get("pending_stage")) and pending.get("release_hold"):
        data.pop("pending_stage")
        data["pending_hold"] = {"stage": stage, "attempt": owned["attempt"], "retry": False}
    store.set_resume(key, data)
    if "pending_hold" in data:
        _retry_restore(forge, store, key, at)


def _fail_start(forge: Any, store: Any, key: str, stage: str, current: str, attempt: int,
                has_board: bool, cause: Exception) -> None:
    """Keep a failed start's stage move and replace its hold, so ownership sits on the stage that failed."""
    store.set_human_review(key, True)
    data = _resume(store, key)
    data.pop("launcher_hold", None)
    store.set_resume(key, data)
    try:
        if stage != current:
            data["pending_stage"] = {"stage": stage, "expected": current, "restore_hold": has_board}
            store.set_resume(key, data)
            _write_stage(forge, store, key, stage, current)
        if has_board:
            data = _resume(store, key)
            data["pending_hold"] = {"stage": stage, "attempt": attempt, "retry": True}
            store.set_resume(key, data)
            _restore_hold(forge, store, key)
    except Exception as exc:
        raise ForgeError(f"{cause}; keeping the failed start's state also failed: {exc}") from cause


class HoldReleaseFailed(ForgeError):
    """A hold release that failed and was already logged; the dispatch error would repeat it."""


def _sync_suite_holds(forge: Any, store: Any, snapshots: list[dict], at: str) -> None:
    """Retry pending suite labels; only a published label's explicit removal accepts its segment."""
    observed = {row["issue_key"]: row.get("human_review") for row in snapshots}
    for item in store.items():
        key = item.issue_key
        data = _resume(store, key)
        if "suite_hold" not in data:
            continue
        try:
            action = reconcile.suite_hold_action(item, data["suite_hold"], store.segments(key),
                                                  observed=observed.get(key))
            if action in ("retire", "accept"):
                store.finish_suite_hold(key, accept=action == "accept")
                if action == "accept":
                    store.add_log(key, "suite hold released: accepted completion", at)
                continue
            store.set_human_review(key, True)
            if action == "publish":
                error = forge.add_human_hold(int(key.rpartition("#")[2]))
                if error:
                    raise ForgeError(error)
                data["suite_hold"]["published"] = True
                store.set_resume(key, data)
        except Exception as exc:
            # A corrupt record or one unavailable forge operation must not halt unrelated work.
            store.set_human_review(key, True)
            message = f"suite hold: {exc}"
            logged = store.log(key)
            if not logged or logged[-1].message != message:
                store.add_log(key, message, at)


def _close_segment(forge: Any, store: Any, key: str, stage: str, attempt: int,
                   outcome: str, reason: str, human_review: bool, now: datetime) -> None:
    """Both closure paths persist a forge hold before ending the segment."""
    if human_review:
        if getattr(forge, "kanban", None) is not None:
            error = forge.add_human_hold(int(key.rpartition("#")[2]))
            if error:
                raise ForgeError(error)
        store.upsert_item(key, human_review=True)
    store.end_segment(key, stage, attempt, now.isoformat(), outcome)
    store.add_log(key, f"worker {stage} attempt {attempt}: {outcome}" +
                  (f": {reason}" if reason else ""), now.isoformat())


def _finish_worker(forge: Any, store: Any, segment: Any,
                   outcome: str, reason: str, now: datetime, *, policies: policy.Policies,
                   facts: reconcile.Facts) -> None:
    """Close the exited run; the planner and cleanup gate merged-item completion on main CI."""
    key = segment.issue_key
    item = next((item for item in store.items() if item.issue_key == key), None)
    workflow = policies.workflow_for(_resume(store, key).get("issue_type") or getattr(item, "issue_type", None))
    verdict = reconcile.completion_verdict(key, segment, facts, workflow, report=outcome, worker_alive=False)
    held = verdict.human_review or reason.startswith("unreadable worker run:")
    if verdict.human_review:
        outcome = verdict.outcome
        reason = "; ".join(part for part in (reason, verdict.reason) if part)
    if verdict.suite_hold:
        _arm_suite_hold(store, key, segment.stage, segment.attempt)
    _close_segment(forge, store, key, segment.stage, segment.attempt, outcome, reason, held, now)


def run_pass(*, forge: Any, store: Any, policies: policy.Policies, root: Path, trees: Path,
             launch: Launch, workers: settings.Workers, clock: engine_clock.Clock = engine_clock.system,
             max_concurrency: int | None = None, require_green: bool = False, teardown_command: str = "") -> list[reconcile.Action]:
    """Sync, plan, execute independently, write stages, then clean engine-owned trees.

    snapshots() must return the complete successful issue read or raise (including on partial reads),
    as Forge does. Materialize first so a failed page cannot sync or close any item.
    """
    _record_limits(workers)

    snapshots = list(forge.snapshots())
    board = getattr(forge, "kanban", None)
    if board is not None:
        stored_stages = {item.issue_key: item.stage for item in store.items()}
        for row in snapshots:
            observed = row.get("stage")
            stored = stored_stages.get(row["issue_key"])
            # Shared labels cannot identify a precise workflow stage; a different label still wins.
            if (observed in board.stage_map and stored in board.stage_map
                    and board.stage_map[observed] == board.stage_map[stored]):
                row["stage"] = stored
    store.sync_from_forge(snapshots)
    scopes = {}
    for row in snapshots:
        key = row["issue_key"]
        body = row.get("body")
        if not isinstance(body, str):
            try:
                got = forge.issue(int(key.rpartition("#")[2]))
                if not got.ok or got.issue is None:
                    raise ForgeError(got.error or "issue not found")
                body = got.issue.body if isinstance(got.issue.body, str) else ""
            except (ForgeError, OSError, ValueError, TypeError) as exc:
                store.add_log(key, f"ownership read failed: {exc}", clock().isoformat())
                # A failed per-issue fallback preserves prior claims; a new issue owns the repository.
                if not any(row.mode != "tree" for row in store.ownership(key)):
                    scopes[key] = [(".", "write")]
                continue
        scopes[key] = ownership.issue_scopes(body)
    for key, declared in scopes.items():
        store.replace_file_ownership(key, declared)
    present = {row["issue_key"] for row in snapshots}
    for item in store.items():
        if item.forge_state == "open" and item.issue_key not in present:
            store.upsert_item(item.issue_key, forge_state="closed")
    landing = _landing_shas(snapshots)
    _invalidate_landing(store, landing)

    facts = worker.collect(store, root, clock, trees,
                   gather=partial(engine_facts.gather, forge=forge, store=store, root=root, trees=trees,
                                  now=clock(), require_green=require_green),
                   finish=partial(_finish_worker, forge, store, policies=policies),
                   require_green=require_green, teardown_command=teardown_command)

    now = clock()
    at = now.isoformat()
    _sync_suite_holds(forge, store, snapshots, at)
    # Retry label writes even when a successful merge closed the issue since the last pass.
    for item in store.items():
        key = item.issue_key
        data = _resume(store, key)
        pending = data.get("pending_stage")
        release = pending if pending and pending.get("release_hold") else None
        action = reconcile.hold_recovery(item, store.segments(key), pending_hold=data.get("pending_hold"),
                                         pending_release=release, owned=data.get("launcher_hold"),
                                         pending_stage=None if release else pending)
        if action == "restore":
            _retry_restore(forge, store, key, at)
            continue
        if action == "drop_hold":
            data.pop("pending_hold")
            store.set_resume(key, data)
            continue
        if action == "replace":
            # A stale release or drifted failed handoff needs a normal human hold.
            data.pop("pending_stage")
            data.pop("launcher_hold", None)
            data["pending_hold"] = {"stage": item.stage, "attempt": 0, "retry": False}
            store.set_resume(key, data)
            _retry_restore(forge, store, key, at)
            continue
        if release:
            if action == "drop_release":
                data.pop("pending_stage")
                data.pop("launcher_hold", None)
                store.set_resume(key, data)
            else:
                # The mutation may have succeeded before verification failed. Keep the local gate
                # through every sync, while planning still checks quota, slots and retry budgets.
                store.set_human_review(key, True)
            continue
        if action == "drop_stage":
            # A person moved the card while the forge was failing; the write can never apply.
            data.pop("pending_stage")
            store.set_resume(key, data)
            continue
        if action == "drop_owner":
            data.pop("launcher_hold")
            store.set_resume(key, data)
        if pending:
            try:
                if item.stage == pending["stage"]:
                    data.pop("pending_stage")
                    store.set_resume(key, data)
                else:
                    _write_stage(forge, store, key, pending["stage"], pending["expected"])
            except Exception as exc:
                message = f"stage write failed: {exc}"
                logged = store.log(key)
                if not logged or logged[-1].message != message:  # retried every pass; log only a change
                    store.add_log(key, message, at)

    items = [item for item in store.items()
             if not (pending := _resume(store, item.issue_key).get("pending_stage")) or pending.get("release_hold")]
    _remember_verified(store, facts, landing)
    actions = reconcile.plan(items, policies=policies,
                             free_slots=slots.free_slots(max_concurrency, slots.running(store)), now=now,
                             hold_stages=board.hold_stages if board is not None else (),
                             facts=facts, **_planning_inputs(store, items))
    stages = {item.issue_key: item.stage for item in items}
    by_key = {item.issue_key: item for item in items}
    for action in actions:
        key = action.issue_key
        try:
            stage = None
            if isinstance(action, (reconcile.Dispatch, reconcile.Review)):
                stage = action.stage if isinstance(action, reconcile.Dispatch) else "review"
                persona = action.persona if isinstance(action, reconcile.Dispatch) else policies.stages[stage].persona
                if policies.resolve(persona=persona, stage=stage).launch == "session":
                    # Interactive sessions are out of this PR; refuse rather than silently use the adapter.
                    raise RuntimeError(f"launch=session is not supported for {stage} ({persona})")
                launch_failed = False

                def start():
                    nonlocal launch_failed
                    # slots.claim has already reserved this attempt before any hold is removed.
                    if by_key[key].human_review:
                        data = _resume(store, key)
                        data["pending_stage"] = {"stage": stage, "expected": stages[key], "release_hold": True}
                        store.set_resume(key, data)
                        try:
                            _write_stage(forge, store, key, stage, stages[key], release_hold=True)
                        except Exception as exc:
                            store.set_human_review(key, True)
                            data = _resume(store, key)
                            owned = dict(data["launcher_hold"])
                            owned["release_failures"] = owned.get("release_failures", 0) + 1
                            data["launcher_hold"] = owned
                            store.set_resume(key, data)
                            message = f"hold release failed: {exc}"
                            logged = store.log(key)
                            if not logged or logged[-1].message != message:
                                store.add_log(key, message, at)
                            raise HoldReleaseFailed(message) from exc
                    try:
                        return launch(key, stage, action.attempt, persona)
                    except Exception as exc:
                        # slots drops a failed reservation. Keep the actual failed start so its
                        # attempt spends the retry budget, and replace the hold before returning.
                        launch_failed = True
                        _fail_start(forge, store, key, stage, stages[key], action.attempt, board is not None, exc)
                        raise

                try:
                    slots.claim(trees, store, issue_key=key, stage=stage, attempt=action.attempt,
                                max_concurrency=max_concurrency, clock=clock, launch=start,
                                agent_type=persona, runtime="claude")
                except Exception:
                    # claim removes the reservation on failure; retain a real launch failure only.
                    if launch_failed:
                        store.record_segment(key, stage, action.attempt, at, at, "failed",
                                             runtime="claude", agent_type=persona)
                    raise
            elif isinstance(action, reconcile.Merge):
                if board is not None and board.holds("merge") and stages[key] != "merge":
                    # Enter the hold before merging; a later pass observes the human's release.
                    stage = "merge"
                elif engine_merge.execute(action, forge, store, at, policies=policies,
                                         inputs=_planning_inputs(store, [by_key[key]]),
                                         hold_stages=board.hold_stages if board is not None else ()):
                    stage = "merge"
            elif isinstance(action, reconcile.Close):
                worker.teardown_once(store, root, key, action.stage, action.attempt, teardown_command, at)
                if action.suite_hold:
                    _arm_suite_hold(store, key, action.stage, action.attempt)
                _close_segment(forge, store, key, action.stage, action.attempt, action.outcome,
                               action.reason, action.human_review, now)
            elif isinstance(action, (reconcile.Ask, reconcile.Escalate)):
                data = _resume(store, key)
                record = {"kind": type(action).__name__, "reason": action.reason}
                if isinstance(action, reconcile.Ask):
                    record["gate"] = action.gate
                if data.get("interrupt") != record:
                    store.add_log(key, f"{record['kind'].lower()}: {action.reason}", at)
                    data["interrupt"] = record  # M5/M6 delivers and resolves the interrupt.
                    store.set_resume(key, data)
                if isinstance(action, reconcile.Escalate):
                    # A permanent refusal or exhausted budget ends automatic release; removing the
                    # hold later works without editing the engine's resume records.
                    _retire_ownership(forge, store, key, stages[key], at)
            elif isinstance(action, reconcile.Wait):
                message = f"wait: {action.reason}"
                last = next((entry.message for entry in reversed(store.log(key)) if entry.message.startswith("wait: ")), None)
                if last != message:
                    store.add_log(key, message, at)
            if stage is not None and stage != stages[key]:
                data = _resume(store, key)
                data["pending_stage"] = {"stage": stage, "expected": stages[key]}
                store.set_resume(key, data)
                _write_stage(forge, store, key, stage, stages[key])
        except Exception as exc:
            if not isinstance(exc, HoldReleaseFailed):
                store.add_log(key, f"{type(action).__name__.lower()} failed: {exc}", at)
    completed = {item.issue_key for item in store.items()
                 if item.forge_state == "closed" and item.merge_state == "merged"
                 and reconcile.completion_wait(item.issue_key, facts) is None
                 and not any(segment.ended_at is None for segment in store.segments(item.issue_key))}
    if cleanup.execute(root, trees if trees.resolve() != root.resolve() else None, store, remove=True,
                       completed=completed):
        raise RuntimeError("cleanup failed; see the issue logs")
    return actions


def _prepare_handoff(release: Path) -> Path | None:
    """Prepare an update, persisting failed release/pins retries with 60s–1h exponential backoff."""
    try:
        data = supervisor.data_dir()
        # Lock order is engine.lock -> supervisor.lock; never wait on another workspace's build.
        with supervisor.lock(data / "supervisor.lock"):
            key = hashlib.sha256(os.fsencode(release) + b"\0" + (release / "requirements.txt").read_bytes()).hexdigest()
            failure = data / f"handoff-{key}.json"
            try:
                previous = json.loads(failure.read_text())
            except (FileNotFoundError, ValueError):
                previous = {}
            now = time.time()
            if now < previous.get("retry_at", 0):
                return None
            try:
                if not (release / "scripts/engine.py").is_file():
                    raise supervisor.ServiceError(f"missing engine entry file: {release / 'scripts/engine.py'}")
                python = supervisor.ensure_venv(data, release=release, timeout=60)
            except Exception as exc:
                delay = min(previous.get("delay", 30) * 2, 3600)
                message = str(exc)
                supervisor.atomic_write(failure, json.dumps({
                    "retry_at": time.time() + delay, "delay": delay, "message": message,
                }).encode())
                if message != previous.get("message"):
                    print(f"engine: cannot prepare release {release}: {exc}; keeping {supervisor.RELEASE}",
                          file=sys.stderr, flush=True)
                return None
            failure.unlink(missing_ok=True)
            return python
    except supervisor.ServiceError:  # another workspace or CLI owns the global lock; try on a later pass
        return None
    except Exception as exc:
        print(f"engine: cannot prepare release {release}: {exc}; keeping {supervisor.RELEASE}",
              file=sys.stderr, flush=True)
        return None


def _handoff(release: Path, python: Path, root: Path, interval: float, once: bool) -> None:
    os.execv(str(python), [str(python), str(release / "scripts/engine.py"),
                              "--root", str(root), "--interval", str(interval)] + (["--once"] if once else []))


def main(argv: list[str] | None = None) -> int:
    """Run passes until stopped, or one with --once; `install`, `uninstall` and `status` manage the LaunchAgent.

    Exits 0 when stopped, 1 when a --once pass failed, 2 on startup or driver errors (including failed exec).
    A different valid install pointer, including rollback, hands off before startup or after a pass
    once its dependency venv is ready. Invalid pointers and failed preparation are logged and retried."""
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--root", type=Path, default=Path("."), help="workspace holding store.db and CLAUDE.md")
    ap.add_argument("--interval", type=float, default=60, help="seconds between passes (default 60)")
    ap.add_argument("--once", action="store_true", help="run one pass and exit")
    ap.add_argument("cmd", nargs="?", choices=supervisor.ACTIONS,
                    help="manage the engine's LaunchAgent")
    args = ap.parse_args(argv)
    if args.cmd:
        return supervisor.main(["--root", str(args.root), args.cmd])
    if not 0 < args.interval < float("inf"):
        ap.error("--interval must be a finite positive number")
    root = args.root.resolve()
    newer = None
    try:
        if (installed := supervisor.current_release()) != supervisor.RELEASE:
            if (python := _prepare_handoff(installed)) is not None:
                _handoff(installed, python, root, args.interval, args.once)
                return 0
        with supervisor.lock(supervisor.state_dir(root) / "engine.lock"):
            text, config, source = startup.workspace_forge(root)
            if source is None:
                refused = issue_gate.start(None, None, "")
                raise ValueError(f"{refused.refused}\n{refused.setup}")
            if config.kanban is None:
                raise ValueError("no [kanban] stage labels configured; configure workspace stage labels before running the engine")
            policies = settings.load_policy()
            # A stage write the map cannot express leaves pending_stage set, which hides the issue forever.
            unmapped = settings.unmapped_stages(config.kanban, policies)
            if unmapped:
                raise ValueError(f"[kanban.map] has no label for workflow stage {', '.join(unmapped)}")
            dispatch_prompt.validate_worker_rules(root, config, policies)
            trees = root / worktrees_dir(text)
            trees.mkdir(parents=True, exist_ok=True)
            db = store_access.migrated_store(root, engine_owned=True)
            # Match the forge repository to a configured clone; a repository workspace needs no [repos] entry.
            repo = source.home.repo if getattr(source, "github", False) else getattr(getattr(source, "home", None), "project", "")
            selected = config.repos.get(repo) or config.repos.get(repo.rpartition("/")[2])
            if not selected and len(config.repos) == 1:
                selected = next(iter(config.repos.values()))
            if not selected and config.repos:
                raise ValueError("no [repos] clone matches the workspace backlog")
            launch = partial(worker.launch, store=db, root=root, trees=trees, policies=policies, forge=source,
                             clone=root / selected if selected else root,
                             claude_as_profile=config.workers.claude_as_profile, rules=config.workers.rules,
                             require_green=config.workers.require_green_tests, teardown_command=config.workers.teardown)
            stop_pages = threading.Event()
            page_thread = None
            try:
                if not args.once:
                    import pages  # deferred: --once never serves the board
                    page_thread = threading.Thread(target=pages.supervise, args=(root, stop_pages), daemon=True)
                    page_thread.start()
                while True:
                    failed = False
                    try:
                        run_pass(forge=source, store=db, policies=policies, root=root, trees=trees,
                                 launch=launch, workers=config.workers, max_concurrency=config.workers.max_concurrency,
                                 require_green=config.workers.require_green_tests, teardown_command=config.workers.teardown)
                    except Exception as exc:
                        failed = True
                        print(f"engine: pass failed: {exc}", file=sys.stderr, flush=True)
                    if args.once:
                        return 1 if failed else 0
                    time.sleep(args.interval)
                    if (installed := supervisor.current_release()) != supervisor.RELEASE:
                        if (python := _prepare_handoff(installed)) is not None:
                            newer = installed, python
                            break
            finally:
                stop_pages.set()
                if page_thread is not None and page_thread.ident is not None:
                    page_thread.join()  # supervise's I/O is bounded; cancellation reaps unrecorded children before exec
                db.engine.dispose()
        if newer is not None:  # after the lock and board thread are released, so the new release can take them
            _handoff(*newer, root, args.interval, args.once)
        return 0
    except KeyboardInterrupt:
        return 0
    except Exception as exc:
        print(f"engine: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
