"""Execute a planned Merge (1.0 M4, Use Cases), with the forge and store passed in."""

from __future__ import annotations

from datetime import datetime
from typing import Any

import reconcile
from forge_port import ForgeError
from policy import Policies, PolicyError


def execute(action: Any, forge: Any, store: Any, at: str, *, policies: Policies) -> bool:
    """Re-plan under the caller's current policies, then log one attempt without retrying.

    Resume data holds issue_type, room and open_findings (absent findings remain unknown).
    """
    try:
        request = forge.closing_request(action.issue_key)
        item = next((i for i in store.items() if i.issue_key == action.issue_key), None)
        if item is None:
            raise ForgeError("no stored work item")
        item.merge_state = request.verdict
        if forge.approver:
            item.review_state = request.approval
        state = store.resume(action.issue_key) or {}
        planned = reconcile.plan([item], {item.issue_key: store.segments(item.issue_key)}, policies,
                                 {item.issue_key: state.get("issue_type")}, 1, datetime.fromisoformat(at),
                                 {item.issue_key: state["open_findings"]} if "open_findings" in state else {},
                                 rooms={item.issue_key: state.get("room")},
                                 decisions={item.issue_key: store.decisions(item.issue_key)})
        if not planned or not isinstance(planned[0], reconcile.Merge):
            reason = (getattr(planned[0], "reason", f"{type(planned[0]).__name__.lower()} required")
                      if planned else "no eligible merge")
            mark = "#" if forge.github else "!"
            raise ForgeError(f"engine policy: {reason} ({mark}{request.number}: {request.verdict})")
        message = forge.merge(request.number, commit=True, issue_key=action.issue_key)
    except (ForgeError, PolicyError) as exc:
        store.add_log(action.issue_key, f"merge refused: {exc}", at)
        return False
    store.add_log(action.issue_key, message, at)
    return message.startswith("merged ")
