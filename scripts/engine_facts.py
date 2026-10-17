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


def architect_comments(comments: list, *, head: str | None = None,
                       author: str | None = None) -> tuple[reconcile.ArchitectReply | None, list[str]]:
    """The reply for `head` (or the latest reply), plus dispositions merged across every accepted reply.

    Replies start `Architect: APPROVED <sha>` or `Architect: CHANGES <sha>`.
    Dispositions map finding keys to fix/file/keep/discard and stay in force after the head moves.
    Only the approval is bound to `head`: a later reply for another sha does not hide this head's reply.
    When `author` is set, a reply counts only from that forge login. With none configured, the worker
    guard is what refuses a worker's `Architect:` marker; this function then accepts any author.
    A malformed disposition body adds nothing and does not erase dispositions already read.
    """
    replies, requested, dispositions = [], [], {}
    for index, note in enumerate(comments):
        body = note.body.strip()
        request = re.match(r"Architect review requested: ([^\s]+)", body)
        if request:
            requested.append(request[1])
        match = re.match(r"Architect: (APPROVED|CHANGES) ([^\s]+)(?:\s|$)", body)
        if not match or (author and getattr(note, "author", None) != author):
            continue
        try:
            when = datetime.fromisoformat(note.time)
            if when.utcoffset() is None:
                continue
            tail = body[match.end():].strip()
            payload = re.search(r"```(?:json)?\s*(.*?)```", tail, re.S)
            data = json.loads(payload[1] if payload else tail) if tail else {}
            got = data.get("dispositions", {})
            if not isinstance(got, dict) or any(not isinstance(k, str) or v not in DISPOSITIONS for k, v in got.items()):
                got = {}
        except (ValueError, TypeError, AttributeError):
            got = {}
            try:
                when = datetime.fromisoformat(note.time)
                if when.utcoffset() is None:
                    continue
            except (ValueError, TypeError):
                continue
        dispositions.update(got)
        replies.append((when, index, reconcile.ArchitectReply(match[2], match[1], note.time, dict(dispositions))))
    matched = [row for row in replies if head is None or row[2].reviewed_head == head]
    if not matched:
        if not dispositions:
            return None, requested
        return reconcile.ArchitectReply(head or "", "", "", dict(dispositions)), requested
    when, index, reply = max(matched, key=lambda row: (row[0], row[1]))
    return reconcile.ArchitectReply(reply.reviewed_head, reply.status, reply.created_at, dict(dispositions)), requested


def _wants_architect(policies: Any, item: Any, room: str | None) -> bool:
    """Whether this item's stage or review panel consults the architect. Unknown policy still reads."""
    if policies is None or "review" not in policies.stages:
        return policies is None
    if "review" in policies.stages:
        panel = policies.resolve(policies.stages["review"].persona, "review", room)
        names = panel.reviewers or (policies.stages["review"].persona,)
        if any(policies.resolve(name, "review", room).architect for name in names):
            return True
    if item.stage in policies.stages and policies.stages[item.stage].persona:
        if policies.resolve(policies.stages[item.stage].persona, item.stage, room).architect:
            return True
    return False


def gather(store: Any, items: list, forge: Any = None, at: str = "", policies: Any = None,
           architect_login: str | None = None) -> dict:
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
            except ForgeError as exc:
                # Review and merge stay blocked on an unknown head; say why, once per change of message.
                heads[key] = None
                message = f"change head unreadable: {exc}"
                logged = store.log(key)
                if at and (not logged or logged[-1].message != message):
                    store.add_log(key, message, at)
                continue
            if request is None or not _wants_architect(policies, item, states[key].get("room")):
                continue
            try:
                comments = forge.change_comments(request.number)
            except ForgeError:
                # The head is already known. A comment failure leaves the reply absent; it does not block the item.
                requested[key] = list(dict.fromkeys(states[key].get("architect_requests") or []))
                continue
            reply, posted = architect_comments(comments, head=heads[key], author=architect_login or None)
            if reply is not None:
                replies[key] = reply
            requested[key] = list(dict.fromkeys([*posted, *states[key].get("architect_requests", [])]))
    return {
        "segments": segments,
        "launcher_holds": {key: state.get("launcher_hold", {}) for key, state in states.items()},
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

