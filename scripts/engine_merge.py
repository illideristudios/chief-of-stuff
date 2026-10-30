"""Execute a planned Merge (1.0 M4, Use Cases), with the forge and store passed in."""

from __future__ import annotations

from collections.abc import Sequence
import json
from typing import Any

import reconcile
from forge_port import ForgeError
from policy import Policies, PolicyError


def architect_review(forge: Any, key: str, *, head: str | None = None) -> reconcile.ArchitectReview:
    """Read the same issue thread used for requests, binding its replies to the forge's head."""
    if head is None:
        head = forge.closing_request(key).head
    return reconcile.ArchitectReview(head, tuple(note.body for note in forge.comments(int(key.rpartition("#")[2]))))


def consultations(forge: Any, store: Any, actions: Sequence[reconcile.Action], at: str) -> dict[str, reconcile.ArchitectReview]:
    """Refresh only merge candidates waiting on the architect; request each unapproved head once."""
    reviews = {}
    for action in actions:
        if not isinstance(action, reconcile.Wait) or action.reason != "architect consultation is required":
            continue
        key = action.issue_key
        try:
            review = architect_review(forge, key)
            reviews[key] = review
            data = dict(store.resume(key) or {})
            if review.verdict == "CHANGES":
                data["architect_changes"] = {"head": review.head, "reply": review.reply}
            else:
                data.pop("architect_changes", None)
            store.set_resume(key, data)
            header = f"Architect review requested: {review.head}"
            if review.verdict or any(body.split("\n", 1)[0] == header for body in review.comments):
                continue
            latest = reconcile.latest_review(store.segments(key))
            reports = store.segment_reports(key, "review", latest.attempt) if latest else []
            findings = reports[-1].findings if reports else []
            body = header + "\n\nFindings to dispose of:\n" + json.dumps(findings, ensure_ascii=False)
            error = forge.comment(int(key.rpartition("#")[2]), body, commit=True)
            if error:
                raise ForgeError(error)
        except ForgeError as exc:
            message = f"architect consultation failed: {exc}"
            if not store.log(key) or store.log(key)[-1].message != message:
                store.add_log(key, message, at)
    return reviews


def execute(action: Any, forge: Any, store: Any, at: str, *, policies: Policies, inputs: dict) -> bool:
    """Re-plan with shared store inputs and current policies, then log one attempt without retrying.

    Resume data holds issue_type and room; findings come from the latest review's report, and without one they
    remain unknown.
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
        # Discard any planning-time consultation snapshot. Only a fresh forge read can authorize this merge.
        checks = {**inputs, "architect_reviews": {}}

        def plan():
            return reconcile.plan([item], policies=policies, free_slots=1, now=store.clock(), **checks)

        planned = plan()
        if planned == [reconcile.Wait(action.issue_key, "architect consultation is required")]:
            checks["architect_reviews"] = {action.issue_key: architect_review(forge, action.issue_key, head=request.head)}
            planned = plan()
        if not planned or not isinstance(planned[0], reconcile.Merge):
            reason = (getattr(planned[0], "reason", f"{type(planned[0]).__name__.lower()} required")
                      if planned else "no eligible merge")
            raise ForgeError(f"engine policy: {reason} ({forge.mark(request.number)}: {request.verdict})")
        # A completed review is evidence for exactly the request head it read, never a later push.
        history = store.segments(action.issue_key)
        review = reconcile.latest_review(history)
        data = store.resume(action.issue_key) or {}
        if review is not None:
            reviewed_head = data.get("runs", {}).get(f"review-{review.attempt}", {}).get("reviewed_head")
            if not reviewed_head or reviewed_head != request.head:
                # Keep the run and its report, but mark its product stale so the next pass reviews again.
                store.record_segment(action.issue_key, "review", review.attempt, review.started_at, review.ended_at, "stale")
                raise ForgeError("reviewed head is missing or differs from the current request head; review again")
        main = forge.branch_head("main")
        if main.status != "passed":
            raise ForgeError(f"main CI is not green: {main.status}")
        message = forge.merge(request.number, commit=True, issue_key=action.issue_key, approval=request.approval,
                              head=request.head, stage_mark=stage_mark)
    except (ForgeError, PolicyError) as exc:
        store.add_log(action.issue_key, f"merge refused: {exc}", at, reconcile.OP_MERGE_REFUSED)
        return False
    merged = message.startswith("merged ")
    queued = message.startswith("queued ")
    store.add_log(action.issue_key, message, at, reconcile.OP_MERGE if merged else "")  # queued or unknown: not a merge
    data = dict(store.resume(action.issue_key) or {})
    if merged:
        # §13 (c): a structured record of what the engine merged, so a non-Claude run's check can tell it apart.
        sha = getattr(forge, "merge_commit", lambda number: None)(request.number)
        data.pop("merge_pending", None)
        data["merges"] = [*data.get("merges", []), {"request": request.number, "sha": sha, "at": at}]
        store.set_resume(action.issue_key, data)
    elif queued:
        data["merge_pending"] = {"expected": item.stage, "request": request.number}
        store.set_resume(action.issue_key, data)
    return merged
