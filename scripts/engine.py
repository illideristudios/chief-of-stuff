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
        "issue_types": {item.issue_key: states[item.issue_key].get("issue_type")
                        or getattr(item, "issue_type", None) for item in items},
        "open_findings": {key: counts for key, history in segments.items()
                          if (counts := reconcile.review_findings(states[key], history)) is not None},
        "rooms": {key: state.get("room") for key, state in states.items()},
        "decisions": {item.issue_key: store.decisions(item.issue_key) for item in items},
        "quota_resets": {key: datetime.fromisoformat(quota["retry_at"]) for key, state in states.items()
                         if (quota := state.get("quota"))},
    }


def _write_stage(forge: Any, store: Any, key: str, stage: str, expected: str) -> None:
    result = forge.write_stage(int(key.rpartition("#")[2]), stage, expected=expected or None,
                               initialize=not expected, commit=True)
    if not result.ok:
        raise ForgeError(result.error)
    fields: dict[str, Any] = {"stage": stage}
    board = getattr(forge, "kanban", None)
    if board is not None and board.holds(stage):
        # A retried write runs before planning, so cache its new hold immediately.
        fields["human_review"] = True
    store.upsert_item(key, **fields)
    data = _resume(store, key)
    data.pop("pending_stage", None)
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
        pending = _resume(store, item.issue_key).get("pending_stage")
        if pending:
            try:
                if item.stage == pending["stage"]:
                    data = _resume(store, item.issue_key)
                    data.pop("pending_stage")
                    store.set_resume(item.issue_key, data)
                else:
                    _write_stage(forge, store, item.issue_key, pending["stage"], pending["expected"])
            except Exception as exc:
                message = f"stage write failed: {exc}"
                logged = store.log(item.issue_key)
                if not logged or logged[-1].message != message:  # retried every pass; log only a change
                    store.add_log(item.issue_key, message, at)

    items = [item for item in store.items() if not _resume(store, item.issue_key).get("pending_stage")]
    actions = reconcile.plan(items, policies=policies,
                             free_slots=slots.free_slots(max_concurrency, slots.running(store)), now=now,
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
                slots.claim(trees, store, issue_key=key, stage=stage, attempt=action.attempt,
                            max_concurrency=max_concurrency, clock=clock,
                            launch=lambda: launch(key, stage, action.attempt, persona), agent_type=persona, runtime="claude")
            elif isinstance(action, reconcile.Merge):
                if board is not None and board.holds("merge") and stages[key] != "merge":
                    # Enter the hold before merging; a later pass observes the human's release.
                    stage = "merge"
                elif engine_merge.execute(action, forge, store, at, policies=policies,
                                         inputs=_planning_inputs(store, [by_key[key]])):
                    stage = "merge"
            elif isinstance(action, (reconcile.Ask, reconcile.Escalate)):
                data = _resume(store, key)
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
                             claude_as_profile=config.workers.claude_as_profile)
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
