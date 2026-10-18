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
from collections.abc import Callable, Collection
from dataclasses import is_dataclass, replace
from datetime import datetime, timedelta
from functools import partial
from pathlib import Path
from typing import Any

import cleanup
import claude_adapter
import clock as engine_clock
import decision_actions
import dispatch_prompt
import engine_facts
import engine_merge
import forge
import issue_gate
import limits_history
import limits_probe
import model_port
import notify
import ownership
import policy
import readiness
import reconcile
import settings
import shell_setup
import slots
import startup
import store_access
import supervisor
import trio_gate
import worker
from forge_port import ForgeError
from reconcile import HOLD_REROUTE, HOLD_STAGE_WRITE, OP_DECISION_COMMENT, OP_MERGE_REFUSED, OP_STAGE_WRITE, OP_WORKER_RUN
from workspace import worktrees_dir

Launch = Callable[[str, str, int, str | None], tuple[str, str] | None]


def adopt_login_path(environ=os.environ) -> None:
    """#871: under its LaunchAgent (the only place CHIEF_OF_STUFF_VERSION is set) the engine has launchd's short PATH, so
    the `gh`, `glab` and `git` it runs itself are not found. Take the login shell's PATH; an unreadable one changes nothing."""
    if environ.get("CHIEF_OF_STUFF_VERSION") and (path := shell_setup.login_path(dict(environ))):
        environ["PATH"] = path


def _resume(store: Any, key: str) -> dict:
    data = store.resume(key)
    return dict(data) if isinstance(data, dict) else {}


def _remember_verified(store: Any, facts: reconcile.Facts) -> None:
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
        if data.get("verified_landing") is True:
            continue
        data["verified_landing"] = True
        if head := dict(facts.landing_heads).get(key):
            data["verified_landing_head"] = head
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
        # The engine's own pending move holds its issue; a hold release is planned through, as before.
        "pending_stages": {key: pending for key, state in states.items()
                           if (pending := state.get("pending_stage")) and not pending.get("release_hold")},
        "pending_asks": {item.issue_key: any(not ask.answer for ask in store.asks(item.issue_key)) for item in items},
        "quota_resets": {key: datetime.fromisoformat(quota["retry_at"]) for key, state in states.items()
                         if (quota := state.get("quota"))},
        "usage": {key: nums for key, state in states.items() if (nums := _measured(state))},
        "waived_gates": {key: tuple(state.get("waived_gates") or ())
                         for key, state in states.items() if state.get("waived_gates")},
    }


def _measured(state: dict) -> dict:
    return {key: state[key] for key in ("spend_usd", "time_minutes", "quota_percent")
            if type(state.get(key)) in (int, float)}


def _interrupt(store: Any, key: str, kind: str, reason: str, at: str, *, scope: dict) -> None:
    """Persist the issue's interrupt when it changes."""
    data = _resume(store, key)
    record = {"kind": kind, "reason": reason, **scope}
    if data.get("interrupt") != record:
        store.add_log(key, f"{kind}: {reason}", at)
        data["interrupt"] = record
        store.set_resume(key, data)


def _add_spend(store: Any, key: str, amount: float) -> None:
    if type(amount) not in (int, float) or not amount:
        return
    data = _resume(store, key)
    data["spend_usd"] = float(data.get("spend_usd") or 0) + float(amount)
    store.set_resume(key, data)


def _metered(store: Any, key: str, model: model_port.Model):
    def call(request: model_port.Request):
        got = model(request)
        if isinstance(got, (model_port.Reply, model_port.Failure)):
            _add_spend(store, key, got.cost_usd)
        return got
    return call


def _resolved(policies: policy.Policies, store: Any, key: str, stage: str) -> policy.Policy:
    room = _resume(store, key).get("room")
    if room not in policies.rooms:
        room = None
    known = stage if stage in policies.stages else None
    return policies.resolve(policies.stages[known].persona if known else None, known, room)


def _workflow_for(policies: policy.Policies, store: Any, item: Any) -> policy.Workflow:
    issue_type = _resume(store, item.issue_key).get("issue_type") or getattr(item, "issue_type", None)
    return policies.workflow_for(issue_type)


def _clear_waived_gates(store: Any, key: str) -> None:
    data = _resume(store, key)
    if data.pop("waived_gates", None) is not None:
        store.set_resume(key, data)


def _choice(store: Any, key: str, topic: str, question: str, options: tuple[str, ...],
            resolved: policy.Policy, personas, model, *, context: str = "", instance: str = "",
            stage: str = "", attempt: int = 0,
            gate: bool = False,
            clock: engine_clock.Clock = engine_clock.system) -> str | None:
    """The trio's adopted choice. A split, a missing trio, or no model escalates once."""
    decision = trio_gate.Decision(key, topic, question, options, context=context, instance=instance,
                                  stage=stage, attempt=attempt, gate=gate)
    earlier = store.decisions(key)
    retry_key = json.dumps([topic, question, instance, stage, attempt, context, options])
    retry = (_resume(store, key).get("trio_retries") or {}).get(retry_key, {})
    prior = decision_actions.latest(earlier).get((topic, question, instance))
    failures = max(retry.get("failures", 0), decision_actions.stamped_failures(prior, context, options, stage, attempt))
    if (retry and prior is not None and not prior.answer and decision_actions.retryable_failure(prior.record)
            and datetime.fromisoformat(retry["retry_at"]) > clock()):
        return None
    outcome = trio_gate.decide(decision, earlier, resolved, personas,
                               _metered(store, key, model) if model is not None else None, store, failures=failures)
    rows = store.decisions(key)
    data = _resume(store, key)
    retries = data.get("trio_retries") or {}
    if len(rows) > len(earlier) and decision_actions.retryable_failure(rows[-1].record):
        delay = min(3600, 60 * 2 ** min(failures, 6))
        retries[retry_key] = {"failures": failures + 1,
                              "retry_at": (clock() + timedelta(seconds=delay)).isoformat()}
        data["trio_retries"] = retries
        store.set_resume(key, data)
        return None
    if retry_key in retries:
        retries.pop(retry_key)
        if not retries:
            data.pop("trio_retries", None)
        store.set_resume(key, data)
    return outcome.recommendation if outcome.unanimous else None


def _clear_interrupt(store: Any, key: str, kind: str, *, scope: dict) -> None:
    data = _resume(store, key)
    record = data.get("interrupt", {})
    if record.get("kind") == kind and all(record.get(name) == value for name, value in scope.items()):
        data.pop("interrupt")
        store.set_resume(key, data)


def _consultation_blocked(store: Any, key: str, resolved: policy.Policy, at: str, *, stage: str) -> bool:
    data = _resume(store, key)
    kind = policy.breach(resolved, _measured(data))
    if kind:
        _interrupt(store, key, "limit", f"reached {kind} limit", at, scope={"stage": stage})
        return True
    _clear_interrupt(store, key, "limit", scope={"stage": stage})
    quota = data.get("quota")
    return bool(quota and datetime.fromisoformat(quota["retry_at"]) > datetime.fromisoformat(at))


def _link_replacement_asks(store: Any, key: str, replacements: list) -> None:
    for origin, segment in replacements:
        pending = decision_actions.replacement_ask(origin, segment, store.decisions(key), store.asks(key))
        if pending is None:
            continue
        spec, ask, prior = pending
        ask_id = ask.id if ask is not None else store.add_ask(key, **spec)
        update = decision_actions.link_ask(prior, ask_id)
        if update != prior:
            trio_gate.record(store, key, update)


def _settle_asks(store: Any, key: str) -> None:
    for ask in store.asks(key):
        rows = store.decisions(key)
        linked = decision_actions.recover_ask_link(rows, ask)
        if linked is not None:
            trio_gate.record(store, key, linked)
            rows = store.decisions(key)
        answer = decision_actions.committed_ask_answer(rows, ask)
        if answer is not None:
            store.answer_ask(key, ask.id, answer)
        elif ask.answer:
            settled = decision_actions.answer_ask(rows, ask)
            if settled is not None:
                trio_gate.record(store, key, settled)


def _sync_decision_interrupts(store: Any, item: Any, at: str, start: str = "implement",
                              passed: Collection[str] = ()) -> None:
    """Derive one interrupt per decision attempt, changing only decision-scoped state."""
    key = item.issue_key
    desired = {}
    pending_rows = (decision_actions.owner_waits(store.decisions(key), store.segments(key), store.asks(key), start, passed)
                    if item.forge_state != "closed" else ())
    for pending in pending_rows:
        scope = {"id": list(pending.decision_id), "stage": pending.stage, "attempt": pending.attempt}
        desired[json.dumps(scope, sort_keys=True)] = {"kind": "ambiguity", "reason": pending.reason, "decision": scope}
    data = _resume(store, key)
    current = {json.dumps(i["decision"], sort_keys=True): i for i in data.get("decision_interrupts", ())}
    for added in desired.keys() - current.keys():
        store.add_log(key, f"ambiguity: {desired[added]['reason']}", at)
    # The old singleton may contain a decision interrupt from an earlier engine version.
    legacy = isinstance((data.get("interrupt") or {}).get("decision"), dict)
    if current != desired or legacy:
        if desired:
            data["decision_interrupts"] = list(desired.values())
        else:
            data.pop("decision_interrupts", None)
        if legacy:
            data.pop("interrupt")
        store.set_resume(key, data)


def _route_asks(store: Any, policies: policy.Policies, model, at: str, clock: engine_clock.Clock) -> None:
    for item in store.items():
        if item.forge_state == "closed" or _resume(store, item.issue_key).get("pending_stage"):
            continue
        key = item.issue_key
        workflow = _workflow_for(policies, store, item)
        replacements = decision_actions.replacements(store.decisions(key), store.segments(key), workflow.start,
                                                     tuple(edge.gate for edge in workflow.edges if edge.gate))
        # Repair an interrupted ask/link write before accepting any direct answer.
        _link_replacement_asks(store, key, replacements)
        _settle_asks(store, key)
        asks = store.asks(key)
        for ask in (ask for ask in asks if not ask.answer):
            stage = ask.stage or item.stage or workflow.start
            resolved = _resolved(policies, store, key, stage)
            if _consultation_blocked(store, key, resolved, at, stage=stage):
                break
            row = decision_actions.ask_decision(store.decisions(key), ask)
            topic, question, instance = decision_actions.identity(row) if row is not None else (
                "ask", ask.question, f"ask-{ask.id}")
            choice = _choice(store, key, topic, question, tuple(ask.options), resolved, policies.personas,
                             model, context=ask.context, instance=instance, stage=stage, attempt=ask.attempt, clock=clock)
            if choice:
                store.answer_ask(key, ask.id, choice)
        for row, segment in replacements:
            resolved = _resolved(policies, store, key, segment.stage)
            if _consultation_blocked(store, key, resolved, at, stage=segment.stage):
                break
            prior = row.record.get("decision")
            if not isinstance(prior, dict):
                _interrupt(store, key, "ambiguity", row.question, at, scope={"stage": segment.stage})
                continue
            context = decision_actions.replacement_context(row, segment)
            _choice(store, key, row.topic, row.question, tuple(prior.get("options") or ()),
                    resolved, policies.personas, model, context=context,
                    instance=prior.get("instance", ""), stage=segment.stage, attempt=segment.attempt, clock=clock)
        _link_replacement_asks(store, key, replacements)
        _settle_asks(store, key)



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
    def clear(data: dict) -> None:
        data.pop("pending_stage", None)
        if release_hold:
            data.pop("launcher_hold", None)

    with store.transaction():
        store.upsert_item(key, **fields)
        store.update_resume(key, clear)
    if release_hold:
        store.add_log(key, f"hold released for {stage}", store.clock().isoformat())


def _apply(store: Any, key: str, update: decision_actions.DecisionUpdate | None) -> None:
    if update is not None:
        trio_gate.record(store, key, update)


def _settle(store: Any, key: str, step: Callable[..., Any], intent_id: int, *args: Any) -> bool:
    """Record an intent's outcome only while it is still the issue's current one, with any older intent superseded
    in the same transaction so it can never become current again."""
    with store.transaction():
        rows = store.decisions(key)
        current = decision_actions.current_intent(rows)
        if current is None or current.intent_id != intent_id:
            return False
        for update in decision_actions.supersede(rows):
            _apply(store, key, update)
        _apply(store, key, step(rows, intent_id, *args))
    return True


def _is_current(store: Any, key: str, intent_id: int) -> bool:
    current = decision_actions.current_intent(store.decisions(key))
    return current is not None and current.intent_id == intent_id


def _apply_intent(forge: Any, store: Any, key: str) -> None:
    """The one place a board intent is acted on (§6). Choose the issue's current intent and supersede the older
    ones, then write its stage and record that it landed. A failed write raises and the intent stays pending."""
    if decision_actions.current_intent(store.decisions(key)) is None:
        return  # no write lock for an issue with nothing to apply
    with store.transaction():
        rows = store.decisions(key)
        for update in decision_actions.supersede(rows):
            _apply(store, key, update)
        intent = decision_actions.current_intent(rows)
        if decision_actions.replaces_engine_move(intent, store.item(key).stage):
            store.update_resume(key, lambda data: data.pop("pending_stage", None))  # a person's intent replaces the engine's own move
    if intent.status == "blocked":
        return
    item = store.item(key)
    if intent.rerouted_to and item.stage != intent.rerouted_to:
        if not _is_current(store, key, intent.intent_id):
            return  # a newer intent arrived; the next pass applies it
        _write_stage(forge, store, key, intent.rerouted_to, item.stage)
    if intent.intent == "write":
        _settle(store, key, decision_actions.advance, intent.intent_id)


def _replaced(store: Any, key: str, planned: decision_actions.DecisionState | None) -> bool:
    """Whether the issue's current intent changed, or first appeared, since planning chose its action."""
    current = decision_actions.current_intent(store.decisions(key))
    return (planned.intent_id if planned else None) != (current.intent_id if current else None)


def _sync_decisions_with_action(store: Any, key: str, action: reconcile.Action) -> None:
    """An answered decision shows blocked while its issue's action waits, and answered again once it does not."""
    if isinstance(action, reconcile.Wait) and action.reason in (HOLD_REROUTE, HOLD_STAGE_WRITE):
        return  # a pending move holds its own issue only; no sibling decision shows blocked for it
    with store.transaction():
        decisions = store.decisions(key)
        if isinstance(action, reconcile.Wait) and action.reason == "decision pending":
            # With no open decision, a blocked answer is what holds merge: restore it.
            for update in decision_actions.restore_held(decisions):
                _apply(store, key, update)
            return
        for state in decision_actions.states(decisions):
            _apply(store, key, decision_actions.meet_action(state, action).update)


def _deliver_comments(forge: Any, store: Any, key: str, at: str) -> None:
    """Post the issue comments board actions recorded on their rows; each is sent once, keyed by its words."""
    queued = decision_actions.pending_comments(store.decisions(key), _resume(store, key).get("decision_comments_sent", ()))
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
        store.update_resume(key, lambda data: data.__setitem__(
            "decision_comments_sent", [*data.get("decision_comments_sent", []), *delivered]))
    message = f"decision comment failed: {failure}"
    logged = store.log(key)
    if failure and (not logged or logged[-1].message != message):  # retried every pass; log only a change
        store.add_log(key, message, at, OP_DECISION_COMMENT)


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
             max_concurrency: int | None = None, require_green: bool = False, teardown_command: str = "",
             model: model_port.Model | None = None) -> list[reconcile.Action]:
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

    facts = worker.collect(store, root, clock, trees,
                   gather=partial(engine_facts.gather, forge=forge, store=store, root=root, trees=trees,
                                  now=clock(), require_green=require_green),
                   finish=partial(_finish_worker, forge, store, policies=policies),
                   require_green=require_green, teardown_command=teardown_command)

    now = clock()
    at = now.isoformat()
    _sync_suite_holds(forge, store, snapshots, at)
    for item in store.items():
        data = _resume(store, item.issue_key)
        quota = data.get("quota")
        if quota and datetime.fromisoformat(quota["retry_at"]) <= now and "quota_percent" in data:
            data.pop("quota_percent")
            # The planner stamps the evaluated stage, which need not be item.stage.
            interrupt = data.get("interrupt") or {}
            if (interrupt.get("kind"), interrupt.get("reason")) == ("limit", "reached quota limit"):
                data.pop("interrupt")
            store.set_resume(item.issue_key, data)
    # Retry label writes even when a successful merge closed the issue since the last pass.
    for item in store.items():
        key = item.issue_key
        try:
            _apply_intent(forge, store, key)
        except Exception as exc:
            message = f"stage write failed: {exc}"
            last = next((entry for entry in reversed(store.log(key)) if entry.kind == OP_STAGE_WRITE), None)
            if last is None or last.message != message:  # intervening wait logs do not repeat a refusal
                store.add_log(key, message, at, OP_STAGE_WRITE)
            continue
        item = store.item(key) or item
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
                # A failed start keeps its segment to spend the retry budget; it does not consume a restart route.
                claimed = ("attempt" not in pending or any(
                    seg.stage == pending["stage"] and seg.attempt == pending["attempt"] and seg.outcome != "failed"
                    for seg in store.segments(key)))
                if not claimed or item.stage == pending["stage"]:
                    data.pop("pending_stage")
                    store.set_resume(key, data)
                else:
                    _write_stage(forge, store, key, pending["stage"], pending["expected"])
            except Exception as exc:
                message = f"stage write failed: {exc}"
                last = next((entry for entry in reversed(store.log(key)) if entry.kind == OP_STAGE_WRITE), None)
                if last is None or last.message != message:  # intervening wait logs do not repeat a refusal
                    store.add_log(key, message, at, OP_STAGE_WRITE)
    for item in store.items():
        _deliver_comments(forge, store, item.issue_key, at)

    _route_asks(store, policies, model, at, clock)
    if is_dataclass(facts):
        # The trio may have answered asks since collect gathered them; plan from the answers it just stored.
        asks = tuple(readiness.Ask(row.issue_key, row.answer, row.stage, row.attempt)
                     for item in store.items() for row in store.asks(item.issue_key))
        if asks != facts.asks:
            facts = replace(facts, asks=asks)
    items = store.items()
    _remember_verified(store, facts)
    inputs = _planning_inputs(store, items)
    actions = reconcile.plan(items, policies=policies,
                             free_slots=slots.free_slots(max_concurrency, slots.running(store)), now=now,
                             hold_stages=board.hold_stages if board is not None else (),
                             facts=facts, **inputs)
    intents = {key: decision_actions.current_intent(rows) for key, rows in inputs["decisions"].items()}  # what planning saw
    stages = {item.issue_key: item.stage for item in items}
    by_key = {item.issue_key: item for item in items}
    claude_text = (root / "CLAUDE.md").read_text(encoding="utf-8") if (root / "CLAUDE.md").is_file() else ""
    for action in actions:
        key = action.issue_key
        planned = intents.get(key)
        if isinstance(action, (reconcile.Dispatch, reconcile.Review)) and _replaced(store, key, planned):
            continue  # a newer intent arrived since planning; the next pass plans from it
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

                restart = (stage != stages[key] and stage == _workflow_for(policies, store, by_key[key]).start
                           and decision_actions.open_reversal(store.decisions(key), store.segments(key), stage))
                if restart:
                    # Persist the restart route before its durable segment consumes the
                    # reversal, so a crash still retries/advances from workflow.start.
                    data = _resume(store, key)
                    data["pending_stage"] = {"stage": stage, "expected": stages[key], "attempt": action.attempt}
                    store.set_resume(key, data)
                try:
                    slots.claim(trees, store, issue_key=key, stage=stage, attempt=action.attempt,
                                max_concurrency=max_concurrency, clock=clock, launch=start,
                                agent_type=agent_name, runtime="claude")
                except BaseException:
                    # claim removes the reservation on failure; retain a real launch failure only.
                    if launch_failed:
                        store.record_segment(key, stage, action.attempt, at, at, "failed",
                                             runtime="claude", agent_type=agent_name)
                    # A rejected claim or failed launch leaves no segment. A crash
                    # after a durable claim must keep the route for recovery.
                    # #750 ruling 2: a failed launch keeps _fail_start's stage move and hold, whose pending_stage
                    # supersedes the route; only a claim refused before any launch rolls the route back.
                    if restart and not launch_failed and not any(s.stage == stage and s.attempt == action.attempt
                                                                 for s in store.segments(key)):
                        data = _resume(store, key)
                        data.pop("pending_stage", None)
                        store.set_resume(key, data)
                    raise
                _clear_waived_gates(store, key)
                _clear_interrupt(store, key, "limit", scope={"stage": stage})
                if planned is not None:
                    # The intent's worker launched: a re-route is done, and a reversal's done phase is its restart.
                    _settle(store, key, decision_actions.advance, planned.intent_id, stage, action.attempt)
                if isinstance(action, reconcile.Dispatch) and stage == _workflow_for(policies, store, by_key[key]).start:
                    for rev in decision_actions.restarted(store.decisions(key), stage, action.attempt):
                        trio_gate.record(store, key, rev)
            elif isinstance(action, reconcile.Merge):
                if board is not None and board.holds("merge") and stages[key] != "merge":
                    # Enter the hold before merging; a later pass observes the human's release.
                    stage = "merge"
                elif engine_merge.execute(action, forge, store, at, policies=policies,
                                         inputs=_planning_inputs(store, [by_key[key]]),
                                         hold_stages=board.hold_stages if board is not None else ()):
                    stage = "merge"
                    _clear_waived_gates(store, key)
            elif isinstance(action, reconcile.Close):
                worker.teardown_once(store, root, key, action.stage, action.attempt, teardown_command, at)
                if action.suite_hold:
                    _arm_suite_hold(store, key, action.stage, action.attempt)
                _close_segment(forge, store, key, action.stage, action.attempt, action.outcome,
                               action.reason, action.human_review, now)
            elif isinstance(action, reconcile.Ask):
                resolved = _resolved(policies, store, key, action.source)
                if _consultation_blocked(store, key, resolved, at, stage=action.source):
                    continue
                choice = _choice(store, key, action.gate, action.reason, ("approve", "hold"),
                                 resolved, policies.personas, model, context=action.context, stage=action.stage,
                                 attempt=action.attempt, gate=True, clock=clock)
                if choice == "approve":
                    data = _resume(store, key)
                    data["waived_gates"] = sorted(set(data.get("waived_gates") or ()) | {action.gate})
                    store.set_resume(key, data)
            elif isinstance(action, (reconcile.Escalate, reconcile.Limit)):
                kind = "exhausted" if isinstance(action, reconcile.Escalate) else "limit"
                _interrupt(store, key, kind, action.reason, at, scope={"stage": action.stage})
                if isinstance(action, reconcile.Escalate) and planned is not None and planned.intent == "run":
                    # A spent retry limit blocks the intent's own decision, which keeps holding the issue.
                    _settle(store, key, decision_actions.block, planned.intent_id, action.reason)
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
                op = OP_STAGE_WRITE
                pending = {"stage": stage, "expected": stages[key]}  # the engine's own move: it names no decision
                with store.transaction():
                    # A board intent accepted during the action owns the stage; the engine's own route to it does not.
                    queued = _resume(store, key).get("pending_stage")
                    mine = ((not queued or queued.get("stage") == stage)
                            and decision_actions.current_intent(store.decisions(key)) is None)
                    if mine:
                        store.update_resume(key, lambda data: data.__setitem__("pending_stage", pending))
                if mine:
                    _write_stage(forge, store, key, stage, stages[key])
        except Exception as exc:
            if not isinstance(exc, HoldReleaseFailed):
                store.add_log(key, f"{type(action).__name__.lower()} failed: {exc}", at, op)
    for item in store.items():
        workflow, state = _workflow_for(policies, store, item), _resume(store, item.issue_key)
        passed = reconcile.passed_gates(workflow, policies, state.get("room"), now, state.get("waived_gates") or ())
        _sync_decision_interrupts(store, item, at, workflow.start, passed)
    completed = {item.issue_key for item in store.items()
                 if item.forge_state == "closed" and item.merge_state == "merged"
                 and reconcile.completion_wait(item.issue_key, facts) is None
                 and not any(segment.ended_at is None for segment in store.segments(item.issue_key))}
    try:
        notify.sync_workspace(root, now)
    except Exception as exc:
        print(f"engine: exception pings failed: {exc}", file=sys.stderr)
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
    adopt_login_path()
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
            model = partial(claude_adapter.call, claude_as_profile=config.workers.claude_as_profile)
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
                                 launch=launch, workers=config.workers, max_concurrency=config.workers.max_concurrency, model=model,
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
