"""Python command hooks for the purpose-built worker guard (design 9 and 11)."""
from __future__ import annotations

import json
import sys

from guard_binding import Binding
from guard_commands import denial_reason
from store import Store

RELAY = "Call the engine MCP ask tool instead. An ask is not an approval. Report blocked, then stop and wait."


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
            context = json.dumps({"tool_name": event.get("tool_name"), "tool_input": event.get("tool_input")})
            ask_id = db.add_ask(binding.issue_key, binding.stage, binding.attempt, binding.session_id,
                                f"Permission requested for {event.get('tool_name', 'tool')}", [], context)
            # Already recorded: asking again through the MCP tool would duplicate it.
            return _deny(event_name, f"Recorded permission ask {ask_id}. An ask is not an approval. "
                                     "Report blocked, then stop and wait.")
        if event_name == "Stop":
            if db.reports(binding.issue_key, stage=binding.stage, attempt=binding.attempt, session_id=binding.session_id):
                return {}
            return _deny(event_name, "Call the engine MCP report tool for this run before stopping (done, failed or blocked).")
        if event_name == "SessionStart":
            item = next((i for i in db.items() if i.issue_key == binding.issue_key), None)
            if item is None:
                raise ValueError("bound issue is missing from the engine store")
            state = {"issue_key": binding.issue_key, "title": item.title, "stage": binding.stage,
                     "attempt": binding.attempt, "session_id": binding.session_id, "forge_stage": item.stage,
                     "asks": [{"id": a.id, "stage": a.stage, "attempt": a.attempt, "session_id": a.session_id,
                               "question": a.question, "options": a.options, "context": a.context, "answer": a.answer}
                              for a in db.asks(binding.issue_key)],
                     "reports": [{"id": r.id, "stage": r.stage, "attempt": r.attempt, "session_id": r.session_id,
                                  "outcome": r.outcome, "summary": r.summary, "findings": r.findings}
                                 for r in db.reports(binding.issue_key)]}
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
