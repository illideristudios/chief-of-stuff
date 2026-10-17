#!/usr/bin/env python3
"""Drive one workspace's reconcile plan, under a singleton lock (1.0 M3)."""

from __future__ import annotations

import argparse
import sys
import time
from collections.abc import Callable
from datetime import datetime
from functools import partial
from pathlib import Path
from typing import Any

import cleanup
import clock as engine_clock
import engine_merge
import forge
import issue_gate
import limits_history
import limits_probe
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


def run_pass(*, forge: Any, store: Any, policies: policy.Policies, root: Path, trees: Path,
             launch: Launch, workers: settings.Workers, clock: engine_clock.Clock = engine_clock.system,
             max_concurrency: int | None = None) -> list[reconcile.Action]:
    """Sync, plan, execute independently, write stages, then clean engine-owned trees.

    snapshots() must return the complete successful issue read or raise (including on partial reads),
    as Forge does. Materialize first so a failed page cannot sync or close any item.
    """
    _record_limits(workers)
    worker.collect(store, root, clock, trees)
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
    present = {row["issue_key"] for row in snapshots}
    for item in store.items():
        if item.forge_state == "open" and item.issue_key not in present:
            store.upsert_item(item.issue_key, forge_state="closed")

    now = clock()
    at = now.isoformat()
    # Retry label writes even when a successful merge closed the issue since the last pass.
    for item in store.items():
        data = _resume(store, item.issue_key)
        pending = data.get("pending_stage")
        if restore := data.get("pending_hold"):
            if item.forge_state != "open" or item.stage != restore["stage"]:
                data.pop("pending_hold")
                store.set_resume(item.issue_key, data)
            else:
                try:
                    _restore_hold(forge, store, item.issue_key)
                except Exception as exc:
                    store.set_human_review(item.issue_key, True)
                    message = str(exc)
                    logged = store.log(item.issue_key)
                    if not logged or logged[-1].message != message:
                        store.add_log(item.issue_key, message, at)
            continue
        owned = data.get("launcher_hold")
        if pending and pending.get("release_hold"):
            # A release is part of dispatch, never an independent pre-plan write. Adopt board drift.
            if (item.forge_state != "open" or not item.stage or item.stage != pending["expected"]
                    or item.stage != pending["stage"] or not owned):
                data.pop("pending_stage")
                data.pop("launcher_hold", None)
                store.set_resume(item.issue_key, data)
            else:
                # The mutation may have succeeded before verification failed. Keep the local gate
                # through every sync, while planning still checks quota, slots and retry budgets.
                store.set_human_review(item.issue_key, True)
            continue
        if owned and (not item.human_review or item.stage != owned.get("stage")):
            data.pop("launcher_hold")
            store.set_resume(item.issue_key, data)
        if pending:
            try:
                if item.stage == pending["stage"]:
                    data.pop("pending_stage")
                    store.set_resume(item.issue_key, data)
                else:
                    _write_stage(forge, store, item.issue_key, pending["stage"], pending["expected"])
            except Exception as exc:
                message = f"stage write failed: {exc}"
                logged = store.log(item.issue_key)
                if not logged or logged[-1].message != message:  # retried every pass; log only a change
                    store.add_log(item.issue_key, message, at)

    items = [item for item in store.items()
             if not (pending := _resume(store, item.issue_key).get("pending_stage")) or pending.get("release_hold")]
    actions = reconcile.plan(items, policies=policies,
                             free_slots=slots.free_slots(max_concurrency, slots.running(store)), now=now,
                             hold_stages=board.hold_stages if board is not None else (),
                             **_planning_inputs(store, items))
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
                            raise ForgeError(message) from exc
                    try:
                        return launch(key, stage, action.attempt, persona)
                    except Exception:
                        # slots drops a failed reservation. Keep the actual failed start so its
                        # attempt spends the retry budget, and replace the hold before returning.
                        launch_failed = True
                        store.set_human_review(key, True)
                        data = _resume(store, key)
                        if board is not None:
                            data["pending_hold"] = {"stage": stage, "attempt": action.attempt, "retry": True}
                            data.pop("launcher_hold", None)
                            store.set_resume(key, data)
                            _restore_hold(forge, store, key)
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
                                         inputs=_planning_inputs(store, [by_key[key]])):
                    stage = "merge"
            elif isinstance(action, (reconcile.Ask, reconcile.Escalate)):
                data = _resume(store, key)
                pending = data.get("pending_stage")
                if isinstance(action, reconcile.Escalate) and pending and pending.get("release_hold"):
                    # A permanent refusal ends automatic release. Restore a normal human hold;
                    # removing it later works without editing the engine's resume records.
                    owned = data.pop("launcher_hold")
                    data.pop("pending_stage")
                    data["pending_hold"] = {"stage": stages[key], "attempt": owned["attempt"], "retry": False}
                    store.set_resume(key, data)
                record = {"kind": type(action).__name__, "reason": action.reason}
                if isinstance(action, reconcile.Ask):
                    record["gate"] = action.gate
                if data.get("interrupt") != record:
                    store.add_log(key, f"{record['kind'].lower()}: {action.reason}", at)
                    data["interrupt"] = record  # M5/M6 delivers and resolves the interrupt.
                    store.set_resume(key, data)
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
            if not str(exc).startswith("hold release failed: "):
                store.add_log(key, f"{type(action).__name__.lower()} failed: {exc}", at)
    if cleanup.execute(root, trees if trees.resolve() != root.resolve() else None, store, remove=True):
        raise RuntimeError("cleanup failed; see the issue logs")
    return actions


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--root", type=Path, default=Path("."), help="workspace holding store.db and CLAUDE.md")
    ap.add_argument("--interval", type=float, default=60, help="seconds between passes (default 60)")
    ap.add_argument("--once", action="store_true", help="run one pass and exit")
    args = ap.parse_args(argv)
    if not 0 < args.interval < float("inf"):
        ap.error("--interval must be a finite positive number")
    root = args.root.resolve()
    try:
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
                             claude_as_profile=config.workers.claude_as_profile, rules=config.workers.rules)
            try:
                while True:
                    failed = False
                    try:
                        run_pass(forge=source, store=db, policies=policies, root=root, trees=trees,
                                 launch=launch, workers=config.workers, max_concurrency=config.workers.max_concurrency)
                    except Exception as exc:
                        failed = True
                        print(f"engine: pass failed: {exc}", file=sys.stderr, flush=True)
                    if args.once:
                        return 1 if failed else 0
                    time.sleep(args.interval)
            finally:
                db.engine.dispose()
    except KeyboardInterrupt:
        return 0
    except Exception as exc:
        print(f"engine: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
