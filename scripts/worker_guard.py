"""Python command hooks for the purpose-built worker guard (design 9 and 11)."""
from __future__ import annotations

import json
import sys

from decision_escalation import ask_decision
from guard_binding import Binding
from guard_commands import denial_reason, permission_judgement
from guard_rules import denial_binds, permission_ask, resume_state, stop_allowed
from store import Store

RELAY = "Call the engine MCP ask tool instead. An ask is not an approval. Report blocked, then stop and wait."


def _earlier(db: Store, issue_key: str, event: dict) -> tuple[object, str | None, str | None] | None:
    """The newest permission ask this issue raised for the same tool, input and worktree, with its answer read from
    the decision the engine links to it (the trio's or the guard's), else from the ask, and who decided it
    (None with no linked decision); None when there is none."""
    same = (event.get("tool_name"), event.get("tool_input"), event.get("cwd"))
    for ask in reversed(db.asks(issue_key)):
        try:
            asked = json.loads(ask.context or "")
        except ValueError:
            continue
        if isinstance(asked, dict) and (asked.get("tool_name"), asked.get("tool_input"), asked.get("cwd")) == same:
            row = ask_decision(db.decisions(issue_key), ask)
            return ask, ((row.answer if row is not None else None) or ask.answer or None), (row.decided_by if row is not None else None)
    return None


def _deny(event: str, reason: str) -> dict:
    if event == "PermissionRequest":
        return {"hookSpecificOutput": {"hookEventName": event, "decision": {"behavior": "deny", "message": reason}}}
    if event == "Stop":
        return {"decision": "block", "reason": reason}
    if event == "SessionStart":
        return {"hookSpecificOutput": {"hookEventName": event, "additionalContext": reason}}
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                    "permissionDecisionReason": reason}}


def handle(event_name: str, event: dict, binding: Binding) -> dict:
    if event_name == "PreToolUse":
        if event.get("tool_name") == "AskUserQuestion":
            return _deny(event_name, RELAY)
        if event.get("tool_name") == "Bash":
            reason = denial_reason(event.get("tool_input", {}).get("command"), binding.protected_branches)
            return _deny(event_name, reason) if reason else {}
        return {}
    if event_name == "Stop" and event.get("stop_hook_active") is True:
        return {}
    if not binding.store_path.is_file():
        raise ValueError("engine store is missing; the engine must migrate it before launch")
    db = Store(binding.store_path)
    try:
        if event_name == "PermissionRequest":
            # #874: judged as the PreToolUse gate judges, so routine reads never stop a worker or reach the owner.
            verdict, why = permission_judgement(event, binding.protected_branches)
            if verdict == "allow":
                return {"hookSpecificOutput": {"hookEventName": event_name, "decision": {"behavior": "allow"}}}
            if verdict == "deny":
                return _deny(event_name, why)
            # The trio answers after this hook has denied, so a retry of the same request takes that answer (#954).
            earlier = _earlier(db, binding.issue_key, event)
            if earlier is not None:
                ask, answer, decided_by = earlier
                if answer == "allow":
                    return {"hookSpecificOutput": {"hookEventName": event_name, "decision": {"behavior": "allow"}}}
                if answer == "deny" and denial_binds(decided_by, ask, binding):
                    return _deny(event_name, f"permission ask {ask.id} was denied. Report blocked, then stop.")
                if answer != "deny":
                    return _deny(event_name, f"{why}: permission ask {ask.id} is still waiting for the trio. "
                                             "An ask is not an approval. Report blocked, then stop and wait.")
            question, context = permission_ask(event)
            # #750 ruling: the unjudged become an allow/deny ask that the trio is consulted on first.
            context = json.dumps({**json.loads(context), "why": why})
            ask_id = db.add_ask(binding.issue_key, binding.stage, binding.attempt, binding.session_id,
                                question, ["allow", "deny"], context)
            # Already recorded: asking again through the MCP tool would duplicate it.
            return _deny(event_name, f"{why}: recorded permission ask {ask_id}; the trio is consulted first. "
                                     "An ask is not an approval. Report blocked, then stop and wait.")
        if event_name == "Stop":
            reports = db.reports(binding.issue_key, stage=binding.stage, attempt=binding.attempt,
                                 session_id=binding.session_id)
            if stop_allowed(reports, binding.launched_at):
                return {}
            return _deny(event_name, "Call the engine MCP report tool for this run before stopping (done, failed or blocked).")
        if event_name == "SessionStart":
            item = next((i for i in db.items() if i.issue_key == binding.issue_key), None)
            if item is None:
                raise ValueError("bound issue is missing from the engine store")
            state = resume_state(binding, item, db.asks(binding.issue_key), db.reports(binding.issue_key))
            return {"hookSpecificOutput": {"hookEventName": event_name, "additionalContext": json.dumps(state)}}
        raise ValueError(f"unknown hook event: {event_name}")
    finally:
        db.engine.dispose()


def main() -> int:
    event_name = sys.argv[1] if len(sys.argv) == 2 else "PreToolUse"
    try:
        event = json.load(sys.stdin)
        if not isinstance(event, dict):
            raise ValueError("hook input must be an object")
        # The host's repeat-stop escape applies even if the store or binding is broken.
        result = {} if event_name == "Stop" and event.get("stop_hook_active") is True else handle(
            event_name, event, Binding.from_env())
    except Exception as exc:  # A command hook crash fails open in the host: emit an explicit decision instead.
        result = _deny(event_name, f"Worker guard unavailable: {exc}. {RELAY}")
    if result:
        print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
