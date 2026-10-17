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

import claude_adapter
import cleanup
import clock as engine_clock
import decision_actions
import notify
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
import trio_gate
import worker
from forge_port import ForgeError
from reconcile import OP_DECISION_COMMENT, OP_MERGE_REFUSED, OP_STAGE_WRITE, OP_WORKER_RUN
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
        "redispatches": {key: state["redispatch"] for key, state in states.items() if state.get("redispatch")},
        "pending_stages": {key: state["pending_stage"] for key, state in states.items() if state.get("pending_stage")},
        "quota_resets": {key: datetime.fromisoformat(quota["retry_at"]) for key, state in states.items()
                         if (quota := state.get("quota"))},
    }


def _write_stage(forge: Any, store: Any, key: str, pending: dict) -> None:
    stage = pending["stage"]
    result = forge.write_stage(int(key.rpartition("#")[2]), stage, expected=pending["expected"] or None,
                               initialize=not pending["expected"], commit=True)
    fields: dict[str, Any] = {"stage": stage}
    board = getattr(forge, "kanban", None)
    if board is not None and board.holds(stage):
        # A retried write runs before planning, so cache its new hold immediately.
        fields["human_review"] = True
    with store.transaction():
        if _current(store, key, pending):
            if not result.ok:
                raise ForgeError(result.error)
            store.upsert_item(key, **fields)
            _stage_landed(store, key, pending)


def _apply(store: Any, update: decision_actions.DecisionUpdate | None) -> None:
    if update is not None:
        store.update_decision(update.store_write)


def _current(store: Any, key: str, pending: dict) -> bool:
    """Whether the intent is still the one stored: a newer re-route replaces it, and a late result must not touch that."""
    return _resume(store, key).get("pending_stage") == pending


def _stage_landed(store: Any, key: str, pending: dict) -> None:
    """The stage a pending write wanted is set: drop the intent and restore the decision that named it. A write
    without a decision id (the engine's own move, or one queued before ids) names none."""
    with store.transaction():
        if _current(store, key, pending):
            store.update_resume(key, lambda data: data.pop("pending_stage", None))
            _apply(store, decision_actions.unblock(store.decisions(key), pending.get("decision_id")))


def _block_reroutes(store: Any, key: str, pending: dict, reason: str) -> None:
    """A retried stage write was refused again: the decision that named it shows blocked with the new reason."""
    with store.transaction():
        if _current(store, key, pending):
            _apply(store, decision_actions.block(store.decisions(key), pending.get("decision_id"), reason))


def _drop_request(store: Any, key: str, request: dict | None) -> None:
    """End the redispatch a launch or escalation carried; one the board queued since is a different request and stays."""
    store.update_resume(key, lambda data: data.pop("redispatch") if request and data.get("redispatch") == request else None)


def _sync_decisions_with_action(store: Any, key: str, action: reconcile.Action) -> None:
    with store.transaction():
        decisions = store.decisions(key)
        if isinstance(action, reconcile.Wait) and action.reason == "decision pending":
            # With no open decision, a blocked answer is what holds merge: restore it.
            for update in decision_actions.restore_held(decisions):
                _apply(store, update)
            return
        for dec in decisions:
            # A re-route's block waits on its stage write, which restores it by id; no action here decides it.
            st = decision_actions.state_of(dec)
            if st.status not in {"answered", "blocked"} or st.rerouted_to or st.blocked_from == "reversed":
                continue
            tr = decision_actions.meet_action(dec, action)
            if tr.store_write is not None:
                store.update_decision(tr.store_write)


def _queue_comments(store: Any, key: str, transition: decision_actions.Transition) -> None:
    """Queue a transition's issue comments for delivery; the resume read happens inside the write."""
    bodies = [a.body for a in transition.actions if isinstance(a, decision_actions.ForgeComment)]
    if bodies:
        store.update_resume(key, lambda data: data.__setitem__("decision_comments", [*data.get("decision_comments", []), *bodies]))


def _deliver_comments(forge: Any, store: Any, key: str, at: str) -> None:
    queued = list(_resume(store, key).get("decision_comments", []))
    delivered, failure = [], ""
    for body in queued:
        try:
            error = forge.comment(int(key.rpartition("#")[2]), body, commit=True)
        except Exception as exc:
            error = str(exc)
        if error:
            failure = error
        else:
            delivered.append(body)
    if delivered:
        # The board may have queued more while the forge calls ran: drop only what was sent.
        def drop(data: dict) -> None:
            left = list(data.get("decision_comments", []))
            for body in delivered:
                if body in left:
                    left.remove(body)
            data["decision_comments"] = left

        store.update_resume(key, drop)
    message = f"decision comment failed: {failure}"
    logged = store.log(key)
    if failure and (not logged or logged[-1].message != message):  # retried every pass; log only a change
        store.add_log(key, message, at, OP_DECISION_COMMENT)


def run_pass(*, forge: Any, store: Any, policies: policy.Policies, root: Path, trees: Path,
             launch: Launch, clock: engine_clock.Clock = engine_clock.system,
             max_concurrency: int | None = None, trio_port: Any = None) -> list[reconcile.Action]:
    """Sync, plan, execute independently, write stages, then clean engine-owned trees.

    snapshots() must return the complete successful issue read or raise (including on partial reads),
    as Forge does. Materialize first so a failed page cannot sync or close any item.
    """
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
                    _stage_landed(store, item.issue_key, pending)
                else:
                    _write_stage(forge, store, item.issue_key, pending)
            except Exception as exc:
                with store.transaction():
                    if _current(store, item.issue_key, pending):
                        message = f"stage write failed: {exc}"
                        _block_reroutes(store, item.issue_key, pending, message)
                        logged = store.log(item.issue_key)
                        last = next((entry for entry in reversed(logged) if entry.kind == OP_STAGE_WRITE), None)
                        if last is None or last.message != message:  # intervening wait logs do not repeat a refusal
                            store.add_log(item.issue_key, message, at, OP_STAGE_WRITE)

    for item in store.items():
        if _resume(store, item.issue_key).get("decision_comments"):
            _deliver_comments(forge, store, item.issue_key, at)

    items = store.items()
    inputs = _planning_inputs(store, items)
    actions = reconcile.plan(items, policies=policies,
                             free_slots=slots.free_slots(max_concurrency, slots.running(store)), now=now, **inputs)
    requests = inputs["redispatches"]  # what planning saw; only that request is ended by the action it produced
    stages = {item.issue_key: item.stage for item in items}
    by_key = {item.issue_key: item for item in items}
    claude_text = (root / "CLAUDE.md").read_text(encoding="utf-8") if (root / "CLAUDE.md").is_file() else ""
    for action in actions:
        key = action.issue_key
        op = (OP_MERGE_REFUSED if isinstance(action, reconcile.Merge)
              else OP_WORKER_RUN if isinstance(action, (reconcile.Dispatch, reconcile.Review)) else "")
        try:
            _sync_decisions_with_action(store, key, action)
            stage = None
            if isinstance(action, (reconcile.Dispatch, reconcile.Review)):
                stage = action.stage if isinstance(action, reconcile.Dispatch) else "review"
                persona = action.persona if isinstance(action, reconcile.Dispatch) else policies.stages[stage].persona
                if policies.resolve(persona=persona, stage=stage).launch == "session":
                    # Interactive sessions are out of this PR; refuse rather than silently use the adapter.
                    raise RuntimeError(f"launch=session is not supported for {stage} ({persona})")
                agent_name = worker.agent_for(claude_text, policies.personas.get(persona or policies.stages[stage].persona),
                                              worker.stage_defaults(root, claude_text, stage))
                slots.claim(trees, store, issue_key=key, stage=stage, attempt=action.attempt,
                            max_concurrency=max_concurrency, clock=clock,
                            launch=lambda: launch(key, stage, action.attempt, persona),
                            agent_type=agent_name, runtime="claude")
                _drop_request(store, key, requests.get(key))
            elif isinstance(action, reconcile.Merge):
                if board is not None and board.holds("merge") and stages[key] != "merge":
                    # Enter the hold before merging; a later pass observes the human's release.
                    stage = "merge"
                elif engine_merge.execute(action, forge, store, at, policies=policies,
                                          inputs=_planning_inputs(store, [by_key[key]])):
                    stage = "merge"
            elif isinstance(action, (reconcile.Ask, reconcile.Escalate)):
                if isinstance(action, reconcile.Escalate) and (requests.get(key) or {}).get("decision_id"):
                    # A spent retry limit holds the decision that asked for the request and ends that request;
                    # a request queued before ids names none, and stays for the next pass.
                    with store.transaction():
                        if _resume(store, key).get("redispatch") == requests[key]:
                            _apply(store, decision_actions.block(store.decisions(key), requests[key].get("decision_id"), action.reason))
                            _drop_request(store, key, requests[key])
                record = {"kind": type(action).__name__, "reason": action.reason}
                if isinstance(action, reconcile.Ask):
                    record["gate"] = action.gate
                    existing_dec = next(
                        (d for d in reversed(store.decisions(key)) if d.topic == action.gate and d.question == action.reason),
                        None,
                    )
                    if existing_dec is None:
                        existing_dec = store.add_decision(key, action.gate, action.reason)
                    eff_policy = policies.resolve(stage=stages.get(key) if stages.get(key) in policies.stages else None)
                    # A gate with no trio (none tagged for it, no default) stays for the board, never a failed consult.
                    if (trio_port is not None and (eff_policy.trio or action.gate in eff_policy.trios)
                            and decision_actions.state_of(existing_dec).status == "open"
                            and not (isinstance(existing_dec.record, dict) and "verdicts" in existing_dec.record)):
                        trio_dec = trio_gate.Decision(key, action.gate, action.reason, ("adopt", "escalate"),
                                                      tags=(action.gate,))
                        outcome = trio_gate.consult(trio_dec, eff_policy, policies.personas, trio_port, store, decision_id=existing_dec.id)
                        with store.transaction():
                            current = store.decision(existing_dec.id)
                            if decision_actions.state_of(current).status == "answered" and current.decided_by == "trio":
                                tr = decision_actions.from_trio(existing_dec, outcome, answered_at=at)
                                if tr.store_write is not None:
                                    store.update_decision(tr.store_write)
                                _queue_comments(store, key, tr)
                if _resume(store, key).get("interrupt") != record:
                    store.add_log(key, f"{record['kind'].lower()}: {action.reason}", at,
                                  OP_WORKER_RUN if isinstance(action, reconcile.Escalate) else "")
                    store.update_resume(key, lambda data: data.__setitem__("interrupt", record))  # M5/M6 delivers and resolves it.
            elif isinstance(action, reconcile.Wait):
                message = f"wait: {action.reason}"
                last = next((entry.message for entry in reversed(store.log(key)) if entry.message.startswith("wait: ")), None)
                if last != message:
                    store.add_log(key, message, at)
            if stage is not None and stage != stages[key]:
                op = OP_STAGE_WRITE
                pending = {"stage": stage, "expected": stages[key]}  # the engine's own move: it names no decision
                with store.transaction():
                    current = _resume(store, key)
                    # A board intent accepted during the action owns the single slot and its stage.
                    mine = not current.get("pending_stage") and not current.get("redispatch")
                    if mine:
                        store.update_resume(key, lambda data: data.__setitem__("pending_stage", pending))
                if mine:
                    _write_stage(forge, store, key, pending)
        except Exception as exc:
            store.add_log(key, f"{type(action).__name__.lower()} failed: {exc}", at, op)
    try:
        notify.sync_workspace(root, now)
    except Exception as exc:
        print(f"engine: exception pings failed: {exc}", file=sys.stderr)
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
                                 launch=launch, max_concurrency=config.workers.max_concurrency,
                                 trio_port=partial(claude_adapter.call, claude_as_profile=config.workers.claude_as_profile))
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
