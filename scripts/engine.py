#!/usr/bin/env python3
"""Drive one workspace's reconcile plan, under a singleton lock (1.0 M3)."""

from __future__ import annotations

import argparse
import sys
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

import cleanup
import clock as engine_clock
import engine_merge
import forge
import issue_gate
import policy
import reconcile
import settings
import slots
import startup
import store_access
import supervisor
from forge_port import ForgeError
from workspace import worktrees_dir

Launch = Callable[[str, str, int, str | None], tuple[str, str] | None]


def _resume(store: Any, key: str) -> dict:
    data = store.resume(key)
    return dict(data) if isinstance(data, dict) else {}


def _findings(store: Any, key: str, history: list) -> dict[str, int] | None:
    latest = max((s for s in history if s.stage == "review" and s.ended_at),
                 key=lambda s: (datetime.fromisoformat(s.started_at), s.attempt), default=None)
    record = _resume(store, key).get("review_findings")
    # TODO #639: replace this attempt-keyed resume bridge with structured findings from the latest review.
    # Forge approval and threads have no severity counts; neither means a worker reported zero findings.
    if latest is None or latest.outcome != "done" or not isinstance(record, dict):
        return None
    counts = record.get("counts")
    if (record.get("attempt") != latest.attempt or not isinstance(counts, dict)
            or any(k not in policy.SEVERITIES or type(v) is not int or v < 0 for k, v in counts.items())):
        return None
    return counts


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


def run_pass(*, forge: Any, store: Any, policies: policy.Policies, root: Path, trees: Path,
             launch: Launch, clock: engine_clock.Clock = engine_clock.system,
             max_concurrency: int | None = None) -> list[reconcile.Action]:
    """Sync, plan, execute independently, write stages, then clean engine-owned trees.

    snapshots() must return the complete successful issue read or raise (including on partial reads),
    as Forge does. Materialize first so a failed page cannot sync or close any item.
    """
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
    segments = {item.issue_key: store.segments(item.issue_key) for item in items}
    # The store does not yet cache issue types; use the default workflow for an unknown type.
    issue_types = {item.issue_key: getattr(item, "issue_type", None) for item in items}
    findings = {key: counts for key, history in segments.items()
                if (counts := _findings(store, key, history)) is not None}
    actions = reconcile.plan(items, segments, policies, issue_types,
                             slots.free_slots(max_concurrency, slots.running(store)), now, findings)
    stages = {item.issue_key: item.stage for item in items}
    for action in actions:
        key = action.issue_key
        try:
            stage = None
            if isinstance(action, (reconcile.Dispatch, reconcile.Review)):
                stage = action.stage if isinstance(action, reconcile.Dispatch) else "review"
                persona = action.persona if isinstance(action, reconcile.Dispatch) else policies.stages[stage].persona
                slots.claim(trees, store, issue_key=key, stage=stage, attempt=action.attempt,
                            max_concurrency=max_concurrency, clock=clock,
                            launch=lambda: launch(key, stage, action.attempt, persona), agent_type=persona)
            elif isinstance(action, reconcile.Merge):
                if board is not None and board.holds("merge") and stages[key] != "merge":
                    # Enter the hold before merging; a later pass observes the human's release.
                    stage = "merge"
                elif engine_merge.execute(action, forge, store, at):
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


def launch(issue_key: str, stage: str, attempt: int, persona: str | None) -> None:
    """The production launch port is #631; a refusal must never record a worker start."""
    raise NotImplementedError("worker launch is not wired yet (#631)")


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
            unmapped = sorted(set(settings.workflow_stages(policies))
                              - set(config.kanban.stage_map) - set(config.kanban.terminal_stages))
            if unmapped:
                raise ValueError(f"[kanban.map] has no label for workflow stage {', '.join(unmapped)}")
            trees = root / worktrees_dir(text)
            trees.mkdir(parents=True, exist_ok=True)
            db = store_access.migrated_store(root, engine_owned=True)
            try:
                while True:
                    failed = False
                    try:
                        run_pass(forge=source, store=db, policies=policies, root=root, trees=trees,
                                 launch=launch, max_concurrency=config.workers.max_concurrency)
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
