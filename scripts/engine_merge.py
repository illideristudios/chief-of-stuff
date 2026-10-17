"""Execute a planned Merge (1.0 M4, Use Cases), with the forge and store passed in."""

from __future__ import annotations

from typing import Any

import reconcile
from forge_port import ForgeError
from policy import Policies, PolicyError


def execute(action: Any, forge: Any, store: Any, at: str, *, policies: Policies, inputs: dict) -> bool:
    """Re-plan with shared store inputs and current policies, then log one attempt without retrying.

    Resume data holds issue_type, room and attempt-keyed review_findings; absent findings remain unknown.
    `inputs` is the same issue context `engine._planning_inputs` builds for the pass.
    """
    try:
        request = forge.closing_request(action.issue_key)
        item = next((i for i in store.items() if i.issue_key == action.issue_key), None)
        if item is None:
            raise ForgeError("no stored work item")
        snap = forge.snapshot(action.issue_key, request=request)
        if snap["forge_state"] != "open":
            raise ForgeError(f"{action.issue_key} is no longer open on the forge")
        # Mark planning assumed; forge owns label decoding. Terminal/blank expect no mapped label.
        stage_mark = forge.mark_for_stage(item.stage)
        if stage_mark == "" and snap.get("stage"):
            raise ForgeError(f"{action.issue_key} left its stage since planning: found {snap['stage']}")
        if stage_mark and stage_mark != snap.get("stage_mark"):
            # Expected mark is gone; adopt the observed stage for re-plan and merge's final read.
            item.stage = snap["stage"]
            stage_mark = snap["stage_mark"] or ""
        # Mark unchanged: keep item.stage, since a label several stages share decodes to the first of them.
        if "human_review" in snap:
            item.human_review = snap["human_review"]
        item.merge_state = request.verdict
        item.review_state = request.approval  # without an approver this is no approval, never a stale cached one
        planned = reconcile.plan([item], policies=policies, free_slots=1, now=store.clock(),  # not `at`: a gate may expire mid-pass
                                 **inputs)
        if not planned or not isinstance(planned[0], reconcile.Merge):
            reason = (getattr(planned[0], "reason", f"{type(planned[0]).__name__.lower()} required")
                      if planned else "no eligible merge")
            raise ForgeError(f"engine policy: {reason} ({forge.mark(request.number)}: {request.verdict})")
        message = forge.merge(request.number, commit=True, issue_key=action.issue_key, approval=request.approval,
                              head=request.head, stage_mark=stage_mark)
    except (ForgeError, PolicyError) as exc:
        store.add_log(action.issue_key, f"merge refused: {exc}", at, reconcile.OP_MERGE_REFUSED)
        return False
    merged = message.startswith("merged ")
    store.add_log(action.issue_key, message, at, reconcile.OP_MERGE if merged else "")  # queued or unknown: not a merge
    return merged
