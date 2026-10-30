#!/usr/bin/env python3
"""Drive one workspace's reconcile plan, under a singleton lock (1.0 M3)."""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import plistlib
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Collection
from dataclasses import is_dataclass, replace
from datetime import datetime, timedelta, timezone
from functools import cache, partial
from pathlib import Path
from typing import Any

import cleanup
import claude_adapter
import clock as engine_clock
import decision_actions
import decision_escalation
import dispatch_prompt
import engine_facts
import engine_merge
import forge
import guard_commands
import heartbeat_store
import host_adapter
import issue_gate
import limits_history
import limits_probe
import model_port
import notify
import policy
import readiness
import reconcile
import release_selection
import runtimes
import settings
import shell_setup
import slots
import startup
import store_access
import supervisor
import trio_gate
import worker
from forge_port import ForgeError
from reconcile import OP_DECISION_COMMENT, OP_MERGE_REFUSED, OP_STAGE_WRITE, OP_WORKER_RUN
from workspace import read_engine_status, worktrees_dir, write_engine_status

Launch = Callable[[str, str, int, str | None], tuple[str, str] | None]
TRIO_PASS_BUDGET_S = 480  # #927: trio calls stop here in one pass; above TIMEOUT_S so a slow vote leaves room for two more


def adopt_login_path(environ=os.environ) -> None:
    """#871: under its LaunchAgent (the only place CHIEF_OF_STUFF_VERSION is set) the engine has launchd's short PATH, so
    the `gh`, `glab` and `git` it runs itself are not found. Take the login shell's PATH; an unreadable one changes nothing."""
    if environ.get("CHIEF_OF_STUFF_VERSION") and (path := shell_setup.login_path(dict(environ))):
        environ["PATH"] = path


def _promote_merge_pending(store: Any, item: Any) -> None:
    """After a merge queue accepts a submission, move to merge once the forge reports merged."""
    data = _resume(store, item.issue_key)
    pending = data.get("merge_pending")
    if not pending:
        return
    key = item.issue_key
    if item.merge_state == "merged" and item.stage != "merge":
        data = dict(data)
        data["pending_stage"] = {"stage": "merge", "expected": pending["expected"]}
        data.pop("merge_pending")
        store.set_resume(key, data)
    elif item.forge_state == "closed" and item.merge_state != "merged":
        data = dict(data)
        data.pop("merge_pending", None)
        store.set_resume(key, data)


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
                          if (counts := reconcile.review_findings(history, _review_reports(store, key, history)))
                          is not None},
        "rooms": {key: state.get("room") for key, state in states.items()},
        "decisions": {item.issue_key: store.decisions(item.issue_key) for item in items},
        "pending_asks": {item.issue_key: any(not ask.answer for ask in store.asks(item.issue_key)) for item in items},
        "quota_resets": {key: datetime.fromisoformat(quota["retry_at"]) for key, state in states.items()
                         if (quota := state.get("quota"))},
        "usage": {key: nums for key, state in states.items() if (nums := _measured(state))},
        "failure_acknowledged": {key: state.get("failure_acknowledged", ()) for key, state in states.items()},
    }


def _review_reports(store: Any, key: str, history: list) -> list:
    """The latest review attempt's reports from its recorded session; reconcile counts their findings."""
    latest = reconcile.latest_review(history)
    return store.segment_reports(key, "review", latest.attempt) if latest else []


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
    inner = model.call if isinstance(model, model_port.Port) else model

    def call(request: model_port.Request):
        got = inner(request)
        if isinstance(got, (model_port.Reply, model_port.Failure)):
            _add_spend(store, key, got.cost_usd)
        return got
    return replace(model, call=call) if isinstance(model, model_port.Port) else call  # keep the trio's rotation


def _resolved(policies: policy.Policies, store: Any, key: str, stage: str) -> policy.Policy:
    room = _resume(store, key).get("room")
    if room not in policies.rooms:
        room = None
    known = stage if stage in policies.stages else None
    return policies.resolve(policies.stages[known].persona if known else None, known, room)


def _workflow_for(policies: policy.Policies, store: Any, item: Any) -> policy.Workflow:
    issue_type = _resume(store, item.issue_key).get("issue_type") or getattr(item, "issue_type", None)
    return policies.workflow_for(issue_type)


def _choice(store: Any, key: str, topic: str, question: str, options: tuple[str, ...],
            resolved: policy.Policy, personas, model, *, context: str = "", instance: str = "",
            stage: str = "", attempt: int = 0,
            gate: bool = False,
            clock: engine_clock.Clock = engine_clock.system, until: datetime | None = None) -> str | None:
    """The trio's adopted choice. A split, a missing trio, or no model escalates once. `until` bounds every model
    call by the pass's trio budget (#927)."""
    decision = trio_gate.Decision(key, topic, question, options, context=context, instance=instance,
                                  stage=stage, attempt=attempt, gate=gate)
    earlier = store.decisions(key)
    retry_key = json.dumps([topic, question, instance, stage, attempt, context, options])
    retry = (_resume(store, key).get("trio_retries") or {}).get(retry_key, {})
    prior = decision_escalation.latest(earlier).get((topic, question, instance))
    failures = max(retry.get("failures", 0), decision_escalation.stamped_failures(prior, context, options, stage, attempt))
    if (retry and prior is not None and not prior.answer and decision_escalation.retryable_failure(prior.record)
            and datetime.fromisoformat(retry["retry_at"]) > clock()):
        return None
    outcome = trio_gate.decide(decision, earlier, resolved, personas,
                               _metered(store, key, model) if model is not None else None, store, failures=failures,
                               remaining=(lambda: (until - clock()).total_seconds()) if until is not None else None)
    rows = store.decisions(key)
    data = _resume(store, key)
    retries = data.get("trio_retries") or {}
    if len(rows) > len(earlier) and decision_escalation.retryable_failure(rows[-1].record):
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
        pending = decision_escalation.replacement_ask(origin, segment, store.decisions(key), store.asks(key))
        if pending is None:
            continue
        spec, ask, prior = pending
        ask_id = ask.id if ask is not None else store.add_ask(key, **spec)
        update = decision_escalation.link_ask(prior, ask_id)
        if update != prior:
            trio_gate.record(store, key, update)


def _settle_asks(store: Any, key: str) -> None:
    for ask in store.asks(key):
        rows = store.decisions(key)
        linked = decision_escalation.recover_ask_link(rows, ask)
        if linked is not None:
            trio_gate.record(store, key, linked)
            rows = store.decisions(key)
        answer = decision_escalation.committed_ask_answer(rows, ask)
        if answer is not None:
            store.answer_ask(key, ask.id, answer)
        elif ask.answer:
            settled = decision_escalation.answer_ask(rows, ask)
            if settled is not None:
                trio_gate.record(store, key, settled)


def _sync_decision_interrupts(store: Any, item: Any, at: str, start: str = "implement",
                              passed: Collection[str] = ()) -> None:
    """Derive one interrupt per decision attempt, changing only decision-scoped state."""
    key = item.issue_key
    desired = {}
    pending_rows = (decision_escalation.owner_waits(store.decisions(key), store.segments(key), store.asks(key), start, passed)
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


def _permission(ask: Any) -> dict | None:
    """The event a worker's permission ask records (#874), or None for any other ask."""
    try:
        event = json.loads(ask.context or "")
    except (TypeError, ValueError):
        return None
    if not isinstance(event, dict):
        return None
    # #893: new asks carry kind, so recognition does not depend on the question text.
    if event.get("kind") == "permission" and "tool_name" in event:
        return event
    # Asks raised before #874 carry no options; they are permission requests all the same (#890 follow-up).
    # Contexts with no kind keep the prefix check, so asks already on the store still settle as permissions.
    if ("kind" not in event and "tool_name" in event and tuple(ask.options) in (("allow", "deny"), ())
            and str(ask.question).startswith("Permission requested for ")):
        return event
    return None


def _settle_permission(store: Any, key: str, ask: Any, answer: str, why: str, at: str) -> None:
    """#890: a permission request is never an owner decision. The guard's rule or the trio answers it; a split,
    unavailable or exhausted trio denies it, with the reason on the run's log. The answer is appended, never a delete."""
    store.answer_ask(key, ask.id, answer)
    row = decision_escalation.ask_decision(store.decisions(key), ask)
    if row is not None and not (row.answer or "").strip():
        trio_gate.record(store, key, decision_escalation.DecisionUpdate(
            row.topic, row.question, answer, "guard", {**row.record, "recommendation": answer, "settled_by": why}))
    store.add_log(key, f"permission {answer}: {ask.question}: {why}", at)


def _route_asks(store: Any, policies: policy.Policies, model, at: str, clock: engine_clock.Clock,
                until: datetime | None = None) -> None:
    spent = lambda: until is not None and clock() >= until  # noqa: E731
    for item in store.items():
        if item.forge_state == "closed" or _resume(store, item.issue_key).get("pending_stage"):
            continue
        key = item.issue_key
        workflow = _workflow_for(policies, store, item)
        replacements = decision_escalation.replacements(store.decisions(key), store.segments(key), workflow.start,
                                                     tuple(edge.gate for edge in workflow.edges if edge.gate))
        # Repair an interrupted ask/link write before accepting any direct answer.
        _link_replacement_asks(store, key, replacements)
        _settle_asks(store, key)
        asks = store.asks(key)
        for ask in (ask for ask in asks if not ask.answer):
            permission = _permission(ask)
            if permission is not None:
                # Judged again by today's rule, so an ask raised under a narrower one is closed by its answer.
                verdict, why = guard_commands.permission_judgement(permission, ())
                if verdict != "trio":
                    _settle_permission(store, key, ask, verdict, "the guard's rule" + (f": {why}" if why else ""), at)
                    continue
            if spent():
                break  # asked next pass; a permission ask is never denied for want of time
            stage = ask.stage or item.stage or workflow.start
            resolved = _resolved(policies, store, key, stage)
            if _consultation_blocked(store, key, resolved, at, stage=stage):
                if permission is not None:
                    _settle_permission(store, key, ask, "deny", "the trio is unavailable: a limit or a quota holds it", at)
                    continue
                break
            row = decision_escalation.ask_decision(store.decisions(key), ask)
            topic, question, instance = decision_escalation.identity(row) if row is not None else (
                "ask", ask.question, f"ask-{ask.id}")
            options = ("allow", "deny") if permission is not None else tuple(ask.options)
            choice = _choice(store, key, topic, question, options, resolved, policies.personas,
                             model, context=ask.context, instance=instance, stage=stage, attempt=ask.attempt, clock=clock,
                             until=until)
            if choice:
                store.answer_ask(key, ask.id, choice)
            elif permission is not None:
                _settle_permission(store, key, ask, "deny", "the trio did not adopt an answer", at)
        for row, segment in replacements:
            resolved = _resolved(policies, store, key, segment.stage)
            if spent() or _consultation_blocked(store, key, resolved, at, stage=segment.stage):
                break
            prior = row.record.get("decision")
            if not isinstance(prior, dict):
                _interrupt(store, key, "ambiguity", row.question, at, scope={"stage": segment.stage})
                continue
            context = decision_escalation.replacement_context(row, segment)
            _choice(store, key, row.topic, row.question, tuple(prior.get("options") or ()),
                    resolved, policies.personas, model, context=context,
                    instance=prior.get("instance", ""), stage=segment.stage, attempt=segment.attempt, clock=clock,
                    until=until)
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


def _apply(store: Any, key: str, update: decision_escalation.DecisionUpdate | None) -> None:
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


def _settle_waits(store: Any) -> None:
    """Settle every answer an earlier wait blocked back to answered, on open and closed issues alike: a closed
    issue gets no planned action, so this reads every decision rather than following the plan (#1124)."""
    for key in sorted({st.issue_key for st in decision_actions.states(store.decisions())
                       if decision_actions.meet_action(st).update}):
        with store.transaction():  # re-read under the lock so a concurrent board intent is never overwritten
            for update in decision_actions.settle_waits(store.decisions(key)):
                _apply(store, key, update)


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
    """Retire legacy owner-release suite gates into the normal issue failure/retry path."""
    for item in store.items():
        key = item.issue_key
        data = _resume(store, key)
        if "suite_hold" not in data:
            continue
        try:
            hold = data["suite_hold"]
            history = store.segments(key)
            reconcile.suite_hold_action(item, hold, history, observed=None)  # validate the saved identity
            segment = next(s for s in history if s.stage == hold["stage"] and s.attempt == hold["attempt"])
            data.pop("suite_hold")
            if item.forge_state != "closed":
                store.record_segment(key, segment.stage, segment.attempt, segment.started_at,
                                     segment.ended_at, "failed")
                # The attempt-owned release protocol preserves stage and worktree on a retry.
                data["launcher_hold"] = {"stage": segment.stage, "attempt": segment.attempt}
                store.add_log(key, "legacy suite hold retired: failed check enters automatic retry", at)
            store.set_resume(key, data)
        except (ValueError, KeyError, TypeError, StopIteration) as exc:
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
        # A failed automated check spends the issue failure budget; it needs no owner release.
        outcome, held = "failed", False
    _close_segment(forge, store, key, segment.stage, segment.attempt, outcome, reason, held, now)


def _wait_log(store: Any, key: str, reason: str, at: str) -> None:
    message = f"wait: {reason}"
    last = next((entry.message for entry in reversed(store.log(key)) if entry.message.startswith("wait: ")), None)
    if last != message:
        store.add_log(key, message, at)


def _stop_stall(store: Any, root: Path, key: str, at: str, teardown_command: str) -> None:
    """Resume a durable stop intent. Only a verified stop releases the slot; no files are removed."""
    data = _resume(store, key)
    recovery = data["stall_recovery"]
    stage, attempt = recovery["stage"], recovery["attempt"]
    record = data.get("runs", {}).get(f"{stage}-{attempt}", {})
    worker.stop(record)
    worker.teardown_once(store, root, key, stage, attempt, teardown_command, at)
    with store.transaction():
        store.end_segment(key, stage, attempt, at, "stopped")
        store.update_resume(key, lambda d: d["stall_recovery"].update(phase="stopped", stopped_at=at))
        store.add_log(key, f"suspected stall recovery: {stage} attempt {attempt} stopped; awaiting relaunch", at)


def _complete_stall_relaunch(store: Any, key: str, at: str) -> None:
    data = _resume(store, key)
    recovery = data.get("stall_recovery", {})
    if recovery.get("phase") != "stopped":
        return
    stage, attempt = recovery["stage"], recovery["attempt"]
    history = store.segments(key)
    replacement = next((s for s in history if s.stage == stage and s.attempt > attempt
                        and data.get("runs", {}).get(f"{stage}-{s.attempt}")), None)
    if replacement is None:
        return  # a reservation or refused launch is not a relaunch
    old = next(s for s in history if s.stage == stage and s.attempt == attempt)
    with store.transaction():
        store.record_segment(key, stage, attempt, old.started_at, old.ended_at, "stalled")
        def complete(d):
            d["runs"][f"{stage}-{attempt}"]["recovery"] = {
                **d.pop("stall_recovery"), "relaunched_as": f"{stage}-{replacement.attempt}"}
        store.update_resume(key, complete)
        store.add_log(key, f"stall relaunch: {stage} attempt {attempt} counts as a failure; "
                          f"replacement attempt {replacement.attempt}", at)


def _stall_action(store: Any, root: Path, key: str, workers: settings.Workers, at: str,
                  teardown_command: str) -> str:
    data = _resume(store, key)
    segment = next((s for s in store.segments(key) if s.ended_at is None), None)
    if segment is None:
        return "suspected stall"
    record = data.get("runs", {}).get(f"{segment.stage}-{segment.attempt}", {})
    if workers.stall.action == "wait":
        return "suspected stall"
    if record.get("launcher") == "t3":
        return "suspected stall: T3 cannot cancel a thread"
    acknowledged = data.get("failure_acknowledged", ())
    restarts = sum(s.outcome == "stalled" and f"{s.stage}-{s.attempt}" not in acknowledged
                   for s in store.segments(key))
    if restarts >= workers.stall.max_restarts:
        return "suspected stall: max_restarts reached"
    store.update_resume(key, lambda d: d.update(stall_recovery={
        "stage": segment.stage, "attempt": segment.attempt, "phase": "stopping", "requested_at": at}))
    try:
        _stop_stall(store, root, key, at, teardown_command)
    except (OSError, ValueError, RuntimeError) as exc:
        return f"suspected stall: recovery waiting for verified termination: {exc}"
    return "suspected stall: prior process ended; relaunch pending"


def _remember_health(store: Any, facts: reconcile.Facts) -> None:
    for key, evidence in facts.activity:
        segment = next((s for s in store.segments(key) if s.ended_at is None), None)
        if segment is None:
            continue
        store.update_resume(key, lambda d: d.update(worker_health={
            "run": f"{segment.stage}-{segment.attempt}", "status": evidence.status,
            "launcher": evidence.launcher, "pending_requests": evidence.pending_requests,
            "pending_since": evidence.pending_since.isoformat() if evidence.pending_requests and evidence.pending_since else None,
            "last_activity_at": evidence.last_activity_at.isoformat() if evidence.last_activity_at else None,
            "error": evidence.error}))


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
    present = {row["issue_key"] for row in snapshots}
    for item in store.items():
        if item.forge_state == "open" and item.issue_key not in present:
            store.upsert_item(item.issue_key, forge_state="closed")

    worker.deliver_messages(store, root, clock)
    facts = worker.collect(store, root, clock, trees,
                   gather=partial(engine_facts.gather, forge=forge, store=store, root=root, trees=trees,
                                  now=clock(), require_green=require_green,
                                  stall_after=timedelta(minutes=workers.stall.after_minutes),
                                  account=workers.claude_as_profile,
                                  hold_observations=tuple((row["issue_key"], row.get("human_review")) for row in snapshots)),
                   finish=partial(_finish_worker, forge, store, policies=policies),
                   require_green=require_green, teardown_command=teardown_command)

    now = clock()
    at = now.isoformat()
    _remember_health(store, facts)
    for item in store.items():
        _complete_stall_relaunch(store, item.issue_key, at)
    _sync_suite_holds(forge, store, snapshots, at)
    for item in store.items():
        _promote_merge_pending(store, item)
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
    _settle_waits(store)

    until = clock() + timedelta(seconds=TRIO_PASS_BUDGET_S)
    _route_asks(store, policies, model, at, clock, until)
    if is_dataclass(facts):
        # The trio may have answered asks since collect gathered them; plan from the answers it just stored.
        asks = tuple(readiness.Ask(row.issue_key, row.answer, row.stage, row.attempt)
                     for item in store.items() for row in store.asks(item.issue_key))
        if asks != facts.asks:
            facts = replace(facts, asks=asks)
    for item in store.items():
        key = item.issue_key
        if _resume(store, key).get("stall_recovery", {}).get("phase") != "stopping":
            continue
        history = store.segments(key)
        segment = next((s for s in history if s.ended_at is None), None)
        if segment is None:
            continue
        verdict = reconcile.stall_state(key, segment, facts, now, store.decisions(key), history,
                                        _workflow_for(policies, store, item).start)
        if verdict in ("ok", "watch", "waiting"):
            store.update_resume(key, lambda d: d.pop("stall_recovery", None))
            _wait_log(store, key, "stall recovery cancelled: worker is active or waiting", at)
        elif verdict in ("suspected", "gone"):
            try:
                _stop_stall(store, root, key, at, teardown_command)
            except (OSError, ValueError, RuntimeError) as exc:
                _wait_log(store, key, f"suspected stall: recovery waiting for verified termination: {exc}", at)
        else:
            _wait_log(store, key, "stall recovery waiting for verified worker evidence", at)
    items = [item for item in store.items()
             if _resume(store, item.issue_key).get("stall_recovery", {}).get("phase") != "stopping"
             and (not (pending := _resume(store, item.issue_key).get("pending_stage")) or pending.get("release_hold"))]
    _remember_verified(store, facts)
    inputs = _planning_inputs(store, items)
    free_slots = slots.free_slots(max_concurrency, slots.running(store))

    def plan():
        return reconcile.plan(items, policies=policies, free_slots=free_slots, now=now,
                              failure_limit=workers.failure_limit, facts=facts, **inputs)

    actions = plan()
    if reviews := engine_merge.consultations(forge, store, actions, at):
        inputs["architect_reviews"] = reviews
        actions = plan()
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
                failed_entry = None

                def start():
                    nonlocal launch_failed, failed_entry
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
                        if isinstance(exc, worker.StartFailed):
                            failed_entry = exc.chosen
                        _fail_start(forge, store, key, stage, stages[key], action.attempt, board is not None, exc)
                        raise

                restart = (stage != stages[key] and stage == _workflow_for(policies, store, by_key[key]).start
                           and decision_escalation.open_reversal(store.decisions(key), store.segments(key), stage))
                if restart:
                    # Persist the restart route before its durable segment consumes the
                    # reversal, so a crash still retries/advances from workflow.start.
                    data = _resume(store, key)
                    data["pending_stage"] = {"stage": stage, "expected": stages[key], "attempt": action.attempt}
                    store.set_resume(key, data)
                try:
                    slots.claim(trees, store, issue_key=key, stage=stage, attempt=action.attempt,
                                max_concurrency=max_concurrency, clock=clock, launch=start,
                                agent_type=agent_name)
                except BaseException:
                    # claim removes the reservation on failure; retain a real launch failure only.
                    if launch_failed:
                        store.record_segment(key, stage, action.attempt, at, at, "failed",
                                             runtime=failed_entry.runtime if failed_entry else "",
                                             model=failed_entry.model if failed_entry else "",
                                             effort=(failed_entry.effort or "") if failed_entry else "",
                                             agent_type=agent_name)
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
                _clear_interrupt(store, key, "limit", scope={"stage": stage})
                # A successful relaunch after a raised limit must not keep the exhausted interrupt (#963).
                _clear_interrupt(store, key, "exhausted", scope={})
                store.update_resume(key, lambda d: d.pop("park", None))
                _complete_stall_relaunch(store, key, at)
                if planned is not None:
                    # The intent's worker launched: a re-route is done, and a reversal's done phase is its restart.
                    _settle(store, key, decision_actions.advance, planned.intent_id, stage, action.attempt)
                if isinstance(action, reconcile.Dispatch) and stage == _workflow_for(policies, store, by_key[key]).start:
                    for rev in decision_escalation.restarted(store.decisions(key), stage, action.attempt):
                        trio_gate.record(store, key, rev)
            elif isinstance(action, reconcile.Merge):
                if engine_merge.execute(action, forge, store, at, policies=policies,
                                        inputs={**_planning_inputs(store, [by_key[key]]), "failure_limit": workers.failure_limit}):
                    stage = "merge"
            elif isinstance(action, reconcile.Close):
                worker.teardown_once(store, root, key, action.stage, action.attempt, teardown_command, at)
                if action.suite_hold:
                    _arm_suite_hold(store, key, action.stage, action.attempt)
                _close_segment(forge, store, key, action.stage, action.attempt, action.outcome,
                               action.reason, action.human_review, now)
            elif isinstance(action, (reconcile.Escalate, reconcile.Limit)):
                kind = "exhausted" if isinstance(action, reconcile.Escalate) else "limit"
                _interrupt(store, key, kind, action.reason, at, scope={"stage": action.stage})
                if isinstance(action, reconcile.Escalate) and planned is not None and planned.intent == "run":
                    # A spent retry limit blocks the intent's own decision, which keeps holding the issue.
                    _settle(store, key, decision_actions.block, planned.intent_id, action.reason)
                if isinstance(action, reconcile.Escalate):
                    # Preparation is durable before the issue is parked and the owner is notified.
                    data = _resume(store, key)
                    data["park"] = {
                        "reason": action.reason,
                        "research": {
                            "attempts": [{"stage": segment.stage, "attempt": segment.attempt,
                                          "outcome": segment.outcome, "runtime": segment.runtime,
                                          "model": segment.model} for segment in store.segments(key)],
                            "reports": [{"summary": report.summary, "findings": report.findings}
                                        for report in store.reports(key)],
                            "log": [entry.message for entry in store.log(key)],
                        },
                        "options": ["Resolve the architectural question with the architect",
                                    "Investigate the recorded failures and prepare a revised approach",
                                    "Close the issue if the work is no longer needed"],
                        "recommendation": "Use the recorded findings and failures to settle the cause before retrying; preserve the worktree.",
                    }
                    store.set_resume(key, data)
                    # A permanent refusal or exhausted budget ends automatic release; removing the
                    # hold later works without editing the engine's resume records.
                    _retire_ownership(forge, store, key, stages[key], at)
            elif isinstance(action, reconcile.Wait):
                reason = (_stall_action(store, root, key, workers, at, teardown_command)
                          if action.reason == "suspected stall" else action.reason)
                _wait_log(store, key, reason, at)
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
        workflow = _workflow_for(policies, store, item)
        passed = {edge.gate for edge in workflow.edges if edge.gate}
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


def _judge(request: model_port.Request, *, claude_as_profile: str = "") -> "model_port.Reply | model_port.Failure":
    """The trio's model port (§6, #879): Claude through claude_adapter, the other hosts read-only through host_adapter."""
    if request.runtime in ("", "claude"):
        return claude_adapter.call(request, claude_as_profile=claude_as_profile)
    return host_adapter.judge(request)


@cache  # whether a CLI is installed does not change while the engine runs
def _judges(runtime: str) -> bool:
    """A host can sit in the trio only if it has a read-only judgement mode and its CLI is installed here."""
    return runtime == "claude" or (runtime in runtimes.NAMES and bool(runtimes.get(runtime).judge_argv)
                                   and host_adapter.available(runtime))


def _note_policy(root: Path, data: dict, overlay: str, db) -> None:
    """Log the merged policy's fingerprint when it differs from the one this workspace last ran (#827).

    The row is stored before the fingerprint file advances, so a failed insert is retried next start.
    """
    fingerprint = hashlib.sha256(json.dumps(data, sort_keys=True, default=str).encode()).hexdigest()[:12]
    seen = supervisor.state_dir(root) / "policy.fingerprint"
    try:
        if seen.read_text().strip() == fingerprint:
            return
    except OSError:
        pass
    message = f"engine: policy changed: {fingerprint} ({'overlay ' + overlay if overlay else 'shipped policy'})"
    print(message, file=sys.stderr, flush=True)
    db.add_engine_log(message, "policy_change")
    seen.parent.mkdir(parents=True, exist_ok=True)
    seen.write_text(fingerprint + "\n")


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
    release_selection.validate_handoff(root, release, supervisor)
    argv = release_selection.isolated_release_argv(python, release / "scripts/engine.py",
                                            "--root", str(root), "--interval", str(interval),
                                            *(["--once"] if once else []))
    previous_cwd = os.getcwd()
    try:
        os.chdir("/")
        os.execve(str(python), argv, release_selection.python_environment())
    finally:  # exec failures and tests that intercept exec must restore the caller's cwd
        os.chdir(previous_cwd)


def _pending_explicit_install(root: Path) -> bool:
    intent = release_selection.explicit_intent(root, supervisor)
    return bool(intent and intent["operation"] == "install" and intent["pending"])


def _persist_launchd_handoff(root: Path, release: Path) -> bool:
    """Reload launchd from a detached helper while this engine holds its pass lock."""
    path = supervisor.job_path(supervisor.label("engine", root), None)
    if "commit-handoff" not in supervisor.ACTIONS or sys.platform != "darwin" or not path.exists():
        return False
    job = plistlib.loads(path.read_bytes())
    if (supervisor.current_release_from_job(job) != supervisor.RELEASE
            and not (supervisor.state_dir(root) / getattr(supervisor, "HANDOFF_MARKER", "engine-handoff.json")).exists()
            and not _pending_explicit_install(root)):
        return False
    target = f"gui/{os.getuid()}/{supervisor.label('engine', root)}"
    if not supervisor.launchctl("print", target, missing_ok=True):
        return False
    child = subprocess.Popen([sys.executable, "-I", str(Path(supervisor.__file__).resolve()), "--root", str(root),
                              "--release", str(release), "commit-handoff"],
                             stdin=subprocess.DEVNULL, start_new_session=True, cwd="/",
                             env=release_selection.python_environment())
    if child.wait(timeout=1500):
        raise supervisor.ServiceError("launchd handoff failed; see engine log")
    return True


def _beat(db: Any, forge: Any, root: Path, trees: Path, workers: settings.Workers, tick: datetime) -> None:
    """Deliver the heartbeat after a pass (#1179). A failure is logged to the engine log and never stops the engine."""
    try:
        now = datetime.now(timezone.utc)
        limit = timedelta(minutes=workers.stall.after_minutes)
        facts = engine_facts.gather(forge=forge, store=db, root=root, trees=trees, now=now, stall_after=limit)
        failures = heartbeat_store.beat(db, now, tick, dict(facts.progress), limit, sinks=heartbeat_store.sinks(root))
    except Exception as exc:
        failures = [f"heartbeat: {exc}"]
    for failure in failures:
        db.add_engine_log(failure, "heartbeat")


def _message_loop(root: Path, stop: threading.Event, *, every: float = 1.0) -> None:
    """Settle and hand off messages promptly, independently of slow forge passes.

    Its store is private to this thread. Mailbox locks and store claims serialize it with a normal pass.
    """
    db = None
    try:
        while not stop.is_set():
            try:
                if db is None:
                    db = store_access.migrated_store(root)
                worker.deliver_messages(db, root)
            except Exception as exc:
                print(f"engine: message delivery sweep failed: {exc}", file=sys.stderr, flush=True)
                if db is not None:
                    db.engine.dispose()
                    db = None
            stop.wait(every)
    finally:
        if db is not None:
            db.engine.dispose()


def main(argv: list[str] | None = None) -> int:
    """Run passes until stopped, or one with --once; `install`, `uninstall`, `ensure` and `status` manage the LaunchAgent.

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
    previous_sigterm = None
    if threading.current_thread() is threading.main_thread():
        def _on_sigterm(_signum: int, _frame: Any) -> None:
            raise KeyboardInterrupt
        previous_sigterm = signal.signal(signal.SIGTERM, _on_sigterm)
    try:
        installed = release_selection.selected_release(root, supervisor)
        if ((supervisor.state_dir(root) / getattr(supervisor, "HANDOFF_MARKER", "engine-handoff.json")).exists()
                or _pending_explicit_install(root)):
            with supervisor.lock(supervisor.state_dir(root) / "engine.lock"):
                if _persist_launchd_handoff(root, installed):
                    return 0
        if installed != supervisor.RELEASE:
            if (python := _prepare_handoff(installed)) is not None and release_selection.selected_release(root, supervisor) == installed:
                with supervisor.lock(supervisor.state_dir(root) / "engine.lock"):
                    if _persist_launchd_handoff(root, installed):
                        return 0
                _handoff(installed, python, root, args.interval, args.once)
                return 0
        with supervisor.lock(supervisor.state_dir(root) / "engine.lock"):
            prior = read_engine_status(root)
            mode = "once" if args.once else "service"
            release = str(supervisor.RELEASE)
            started_at = datetime.now(timezone.utc)
            last_pass_at = prior.last_pass_at if prior is not None and prior.present else None
            last_attempt_at = prior.last_attempt_at if prior is not None and prior.present else None
            last_error = (
                prior.last_error
                if prior is not None and prior.present and prior.error_kind != "startup"
                else None
            )
            error_kind = (
                prior.error_kind
                if prior is not None and prior.present and prior.error_kind != "startup"
                else None
            )
            try:
                text, config, source = startup.workspace_forge(root)
                if source is None:
                    refused = issue_gate.start(None, None, "")
                    raise ValueError(f"{refused.refused}\n{refused.setup}")
                if config.kanban is None:
                    raise ValueError("no [kanban] stage labels configured; configure workspace stage labels before running the engine")
                policies = settings.workspace_policy(root, config.policy_overlay)
                # A stage write the map cannot express leaves pending_stage set, which hides the issue forever.
                unmapped = settings.unmapped_stages(config.kanban, policies)
                if unmapped:
                    raise ValueError(f"[kanban.map] has no label for workflow stage {', '.join(unmapped)}")
                dispatch_prompt.validate_worker_rules(root, config, policies)
                trees = root / worktrees_dir(text)
                trees.mkdir(parents=True, exist_ok=True)
                db = store_access.migrated_store(root, engine_owned=True)
                _note_policy(root, settings.workspace_policy_data(root, config.policy_overlay), config.policy_overlay, db)
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
                                 require_green=config.workers.require_green_tests, teardown_command=config.workers.teardown,
                                 launcher=config.workers.worker_launcher)
                model = model_port.Port(partial(_judge, claude_as_profile=config.workers.claude_as_profile),
                                        rotation=config.models.get("trio", ()), pick=config.model_picks.get("trio", "first"),
                                        launchable=_judges)
            except Exception as exc:
                failed_at = datetime.now(timezone.utc)
                with contextlib.suppress(OSError):
                    write_engine_status(
                        root,
                        started_at=started_at,
                        last_pass_at=last_pass_at,
                        last_attempt_at=failed_at,
                        stopped_at=failed_at,
                        interval=args.interval,
                        mode=mode,
                        last_error=str(exc).strip() or type(exc).__name__,
                        error_kind="startup",
                        release=release,
                    )
                raise
            with contextlib.suppress(OSError):
                write_engine_status(
                    root,
                    started_at=started_at,
                    last_pass_at=last_pass_at,
                    last_attempt_at=last_attempt_at,
                    stopped_at=None,
                    interval=args.interval,
                    mode=mode,
                    last_error=last_error,
                    error_kind=error_kind,
                    release=release,
                )
            stop_pages = threading.Event()
            page_thread = None
            message_thread = None
            try:
                if not args.once:
                    import pages  # deferred: --once never serves the board
                    page_thread = threading.Thread(target=pages.supervise_with_retry, args=(root, stop_pages), daemon=True)
                    page_thread.start()
                    message_thread = threading.Thread(target=_message_loop, args=(root, stop_pages), daemon=True)
                    message_thread.start()
                while True:
                    with contextlib.suppress(OSError):
                        write_engine_status(root, started_at=started_at, last_pass_at=last_pass_at,
                                            last_attempt_at=last_attempt_at, stopped_at=None,
                                            pass_started_at=datetime.now(timezone.utc), interval=args.interval,
                                            mode=mode, last_error=last_error, error_kind=error_kind, release=release)
                    failed = False
                    try:
                        run_pass(forge=source, store=db, policies=policies, root=root, trees=trees,
                                 launch=launch, workers=config.workers, max_concurrency=config.workers.max_concurrency, model=model,
                                 require_green=config.workers.require_green_tests, teardown_command=config.workers.teardown)
                    except Exception as exc:
                        failed = True
                        last_attempt_at = datetime.now(timezone.utc)
                        last_error = str(exc).strip() or type(exc).__name__
                        error_kind = "pass"
                        print(f"engine: pass failed: {exc}", file=sys.stderr, flush=True)
                    else:
                        done = datetime.now(timezone.utc)
                        last_pass_at = done
                        last_attempt_at = done
                        last_error = None
                        error_kind = None
                    if args.once:
                        return 1 if failed else 0
                    _beat(db, source, root, trees, config.workers, datetime.now(timezone.utc))
                    with contextlib.suppress(OSError):
                        write_engine_status(
                            root,
                            started_at=started_at,
                            last_pass_at=last_pass_at,
                            last_attempt_at=last_attempt_at,
                            stopped_at=None,
                            interval=args.interval,
                            mode=mode,
                            last_error=last_error,
                            error_kind=error_kind,
                            release=release,
                        )
                    time.sleep(args.interval)
                    installed = release_selection.selected_release(root, supervisor)
                    if (installed != supervisor.RELEASE or
                            _pending_explicit_install(root)):
                        if (python := _prepare_handoff(installed)) is not None and release_selection.selected_release(root, supervisor) == installed:
                            newer = installed, python
                            break
                    if page_thread is not None and not page_thread.is_alive():
                        # A temporary supervisor-lock conflict or unexpected thread exit must not
                        # disable board recovery for the remainder of this engine process.
                        print("engine: page supervisor exited; restarting it", file=sys.stderr, flush=True)
                        page_thread = threading.Thread(target=pages.supervise_with_retry, args=(root, stop_pages), daemon=True)
                        page_thread.start()
            finally:
                stop_pages.set()
                if message_thread is not None and message_thread.ident is not None:
                    message_thread.join()
                if page_thread is not None and page_thread.ident is not None:
                    page_thread.join()  # supervise's I/O is bounded; cancellation reaps unrecorded children before exec
                db.engine.dispose()
                with contextlib.suppress(OSError):
                    write_engine_status(
                        root,
                        started_at=started_at,
                        last_pass_at=last_pass_at,
                        last_attempt_at=last_attempt_at,
                        stopped_at=datetime.now(timezone.utc),
                        interval=args.interval,
                        mode=mode,
                        last_error=last_error,
                        error_kind=error_kind,
                        release=release,
                    )
            if newer is not None and _persist_launchd_handoff(root, newer[0]):
                return 0
        if newer is not None:  # after the lock and board thread are released, so the new release can take them
            _handoff(*newer, root, args.interval, args.once)
        return 0
    except KeyboardInterrupt:
        return 0
    except Exception as exc:
        print(f"engine: {exc}", file=sys.stderr)
        return 2
    finally:
        if previous_sigterm is not None:
            signal.signal(signal.SIGTERM, previous_sigterm)


if __name__ == "__main__":
    raise SystemExit(main())
