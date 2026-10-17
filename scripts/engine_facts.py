"""Gather the engine planner's store and forge facts. Frameworks and Drivers."""
from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any

import reconcile
import worker
from forge_port import ForgeError
from policy import DISPOSITIONS


def _resume(store: Any, key: str) -> dict:
    data = store.resume(key)
    return dict(data) if isinstance(data, dict) else {}


def architect_comments(comments: list) -> tuple[reconcile.ArchitectReply | None, list[str]]:
    """Read the latest explicit architect reply, with timestamp and source order as the tie-breaker.

    Replies start `Architect: APPROVED <sha>` or `Architect: CHANGES <sha>`.
    An optional JSON block contains `dispositions` mapping finding keys to fix/file/keep/discard.
    """
    replies, requested = [], []
    for index, note in enumerate(comments):
        body = note.body.strip()
        request = re.match(r"Architect review requested: ([^\s]+)", body)
        if request:
            requested.append(request[1])
        match = re.match(r"Architect: (APPROVED|CHANGES) ([^\s]+)(?:\s|$)", body)
        if not match:
            continue
        try:
            when = datetime.fromisoformat(note.time)
            if when.utcoffset() is None:
                continue
            tail = body[match.end():].strip()
            payload = re.search(r"```(?:json)?\s*(.*?)```", tail, re.S)
            data = json.loads(payload[1] if payload else tail) if tail else {}
            dispositions = data.get("dispositions", {})
            if not isinstance(dispositions, dict) or any(not isinstance(k, str) or v not in DISPOSITIONS
                                                         for k, v in dispositions.items()):
                dispositions = {}
        except (ValueError, TypeError, AttributeError):
            # A malformed disposition body cannot inherit an earlier approval.
            dispositions = {}
            try:
                when = datetime.fromisoformat(note.time)
                if when.utcoffset() is None:
                    continue
            except (ValueError, TypeError):
                continue
        replies.append((when, index, reconcile.ArchitectReply(match[2], match[1], note.time, dispositions)))
    return (max(replies, key=lambda r: (r[0], r[1]))[2] if replies else None), requested


def gather(store: Any, items: list, forge: Any = None, at: str = "") -> dict:
    """Read the same issue context for the engine pass and merge recheck; `at` stamps a head-read failure log."""
    segments = {item.issue_key: store.segments(item.issue_key) for item in items}
    states = {item.issue_key: _resume(store, item.issue_key) for item in items}
    findings = {}
    heads, replies, requested = {}, {}, {}
    requests, listing = None, None
    if forge is not None:
        try:
            requests = forge.requests()  # one list per pass, shared by every item's closing request
        except ForgeError as exc:
            listing = exc
    for item in items:
        key = item.issue_key
        findings[key] = reconcile.review_findings(
            store.reports(key), segments[key], sessions=worker.run_sessions(states[key], segments[key], "review"))
        if forge is not None and item.forge_state != "closed":
            try:
                if listing is not None:
                    raise listing
                request = forge.closing_request(key, requests)
                heads[key] = request.head if request else None
                if request is not None:
                    comments = forge.change_comments(request.number)
                    reply, posted = architect_comments(comments)
                    if reply is not None:
                        replies[key] = reply
                    requested[key] = list(dict.fromkeys([*posted, *states[key].get("architect_requests", [])]))
            except ForgeError as exc:
                # Review and merge stay blocked on an unknown head; say why, once per change of message.
                heads[key] = None
                message = f"change head unreadable: {exc}"
                logged = store.log(key)
                if at and (not logged or logged[-1].message != message):
                    store.add_log(key, message, at)
    return {
        "segments": segments,
        "issue_types": {item.issue_key: states[item.issue_key].get("issue_type")
                        or getattr(item, "issue_type", None) for item in items},
        "open_findings": {key: value for key, value in findings.items() if value is not None},
        "heads": heads,
        "architect_replies": replies,
        "consult_requests": requested,
        "rooms": {key: state.get("room") for key, state in states.items()},
        "decisions": {item.issue_key: store.decisions(item.issue_key) for item in items},
        "quota_resets": {key: datetime.fromisoformat(quota["retry_at"]) for key, state in states.items()
                         if (quota := state.get("quota"))},
    }

