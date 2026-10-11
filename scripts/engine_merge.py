"""Execute a planned Merge (1.0 M4, Use Cases), with the forge and store passed in."""

from __future__ import annotations

from typing import Any

from forge_port import ForgeError


def execute(action: Any, forge: Any, store: Any, at: str) -> bool:
    """Log one merge attempt; queued, unknown and refused outcomes return False without retrying."""
    try:
        request = forge.closing_request(action.issue_key)
        message = forge.merge(request.number, commit=True, issue_key=action.issue_key)
    except ForgeError as exc:
        store.add_log(action.issue_key, f"merge refused: {exc}", at)
        return False
    store.add_log(action.issue_key, message, at)
    return message.startswith("merged ")
