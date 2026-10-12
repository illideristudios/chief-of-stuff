"""Execute a planned Merge (1.0 M4, Use Cases), with the forge and store passed in."""

from __future__ import annotations

from datetime import datetime
from typing import Any

import reconcile
from forge_port import ForgeError
from policy import Policies, PolicyError, SEVERITIES


def _findings(state: dict, history: list) -> dict[str, int] | None:
    latest = max((s for s in history if s.stage == "review" and s.ended_at),
                 key=lambda s: (datetime.fromisoformat(s.started_at), s.attempt), default=None)
    record = state.get("review_findings")
    # TODO #639: replace this attempt-keyed resume bridge with structured findings from the latest review.
    # Forge approval and threads have no severity counts; neither means a worker reported zero findings.
    if latest is None or latest.outcome != "done" or not isinstance(record, dict):
        return None
    counts = record.get("counts")
    if (record.get("attempt") != latest.attempt or not isinstance(counts, dict)
            or any(k not in SEVERITIES or type(v) is not int or v < 0 for k, v in counts.items())):
        return None
    return counts


def planning_inputs(store: Any, items: list) -> dict:
    """Read the same issue context for the engine pass and merge recheck."""
    segments = {item.issue_key: store.segments(item.issue_key) for item in items}
    states = {item.issue_key: store.resume(item.issue_key) for item in items}
    states = {key: state if isinstance(state, dict) else {} for key, state in states.items()}
    return {
        "segments": segments,
        "issue_types": {item.issue_key: states[item.issue_key].get("issue_type")
                        or getattr(item, "issue_type", None) for item in items},
        "open_findings": {key: counts for key, history in segments.items()
                          if (counts := _findings(states[key], history)) is not None},
        "rooms": {key: state.get("room") for key, state in states.items()},
        "decisions": {item.issue_key: store.decisions(item.issue_key) for item in items},
    }


def execute(action: Any, forge: Any, store: Any, at: str, *, policies: Policies) -> bool:
    """Re-plan with shared store inputs and current policies, then log one attempt without retrying.

    Resume data holds issue_type, room and attempt-keyed review_findings; absent findings remain unknown.
    """
    try:
        request = forge.closing_request(action.issue_key)
        item = next((i for i in store.items() if i.issue_key == action.issue_key), None)
        if item is None:
            raise ForgeError("no stored work item")
        # The issue itself can close or leave its stage after planning: re-read it, not only the request.
        got = forge.issue(int(action.issue_key.rsplit("#", 1)[1]))
        if not got.ok or got.issue is None:
            raise ForgeError(f"issue: {got.error or 'not found'}")
        if not got.issue.is_open:
            raise ForgeError("the issue is no longer open on the forge")
        board = forge.kanban
        stage_mark = board.label_for(item.stage) if board and item.stage in board.stage_map else None
        # A shared board mark cannot name a precise stage; only a move off the stored stage's mark replaces it.
        if stage_mark is not None and stage_mark not in got.issue.labels:
            item.stage = next((s for s in board.stage_map if board.label_for(s) in got.issue.labels), "")
            stage_mark = board.label_for(item.stage) if item.stage else None
        if board and board.human_review_label in got.issue.labels:
            item.human_review = True  # a forge-side hold counts even before the store hears of it
        item.merge_state = request.verdict
        item.review_state = request.approval  # without an approver this is no approval, never a stale cached one
        planned = reconcile.plan([item], policies=policies, free_slots=1, now=store.clock(),  # not `at`: a gate may expire mid-pass
                                 **planning_inputs(store, [item]))
        if not planned or not isinstance(planned[0], reconcile.Merge):
            reason = (getattr(planned[0], "reason", f"{type(planned[0]).__name__.lower()} required")
                      if planned else "no eligible merge")
            mark = "#" if forge.github else "!"
            raise ForgeError(f"engine policy: {reason} ({mark}{request.number}: {request.verdict})")
        message = forge.merge(request.number, commit=True, issue_key=action.issue_key, approval=request.approval,
                              head=request.head, stage_mark=stage_mark)
    except (ForgeError, PolicyError) as exc:
        store.add_log(action.issue_key, f"merge refused: {exc}", at)
        return False
    store.add_log(action.issue_key, message, at)
    return message.startswith("merged ")
