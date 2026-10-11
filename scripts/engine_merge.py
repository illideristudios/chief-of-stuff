"""Execute a planned Merge (1.0 M4, Use Cases), with the forge and store passed in."""

from __future__ import annotations

from typing import Any

from forge_port import ForgeError


def execute(action: Any, forge: Any, store: Any, at: str) -> bool:
    """Log one merge attempt on the planned issue; a forge refusal returns False without retrying."""
    try:
        closing = [r for r in forge.requests()
                   if any(forge.key(int(ref[1:])) == action.issue_key for ref in r.closes)]
        if not closing:
            raise ForgeError(f"no open request closes {action.issue_key}")
        # Snapshots report the newest closing request, so rework cannot merge an older ready one.
        request = max(closing, key=lambda r: r.number)
        message = forge.merge(request.number, commit=True)
    except ForgeError as exc:
        store.add_log(action.issue_key, f"merge refused: {exc}", at)
        return False
    store.add_log(action.issue_key, message, at)
    return True
