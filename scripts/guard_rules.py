"""Pure Use Case judgements for the worker guard: Stop, permission asks, and resume state.

No file, store, gateway or driver access. The hook driver supplies data and writes asks.
"""
from __future__ import annotations

from datetime import datetime
import json


def stop_allowed(reports, launched_at: datetime) -> bool:
    return any(datetime.fromisoformat(report.created_at) >= launched_at for report in reports)


def permission_ask(event: dict) -> tuple[str, str]:
    context = json.dumps({"tool_name": event.get("tool_name"), "tool_input": event.get("tool_input")})
    return f"Permission requested for {event.get('tool_name', 'tool')}", context


def resume_state(binding, item, asks, reports) -> dict:
    return {
        "issue_key": binding.issue_key,
        "title": item.title,
        "stage": binding.stage,
        "attempt": binding.attempt,
        "session_id": binding.session_id,
        "forge_stage": item.stage,
        "asks": [{"id": a.id, "stage": a.stage, "attempt": a.attempt, "session_id": a.session_id,
                  "question": a.question, "options": a.options, "context": a.context, "answer": a.answer}
                 for a in asks],
        "reports": [{"id": r.id, "stage": r.stage, "attempt": r.attempt, "session_id": r.session_id,
                     "outcome": r.outcome, "summary": r.summary, "findings": r.findings}
                    for r in reports],
    }
