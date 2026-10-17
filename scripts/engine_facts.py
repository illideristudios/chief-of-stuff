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


def architect_comments(comments: list, *, head: str | None = None, author: str | None = None,
                       engine_author: str | None = None, requests: dict | None = None
                       ) -> tuple[reconcile.ArchitectReply | None, list[str]]:
    """Read only authorized comments, matching replies to locally recorded requests by commit prefix.

    Dispositions fold oldest first and are limited to findings the engine actually asked about.
    Only authenticated comments with the recorded token and findings confirm posting intent.
    The digest is an internal key, not a reply SHA.
    """
    records = reconcile.request_records(requests or {})
    replies, requested, postings = [], [], []
    for index, note in enumerate(comments):
        login = getattr(note, "author", None)
        is_engine = bool(engine_author and login == engine_author)
        is_architect = not author or login == author
        if not is_engine and not is_architect:
            continue
        body = note.body.strip()
        if is_engine:
            request = re.search(r"(?m)^(?:Request:|Architect review requested:) (\S+)$", body)
            token = request[1] if request else None
            # New bodies put the internal token on a separate Request line.
            internal = re.search(r"(?m)^Request: (\S+)$", body)
            if internal:
                token = internal[1]
            payload = re.search(r"```(?:json)?\s*(.*?)```", body, re.S)
            try:
                findings = json.loads(payload[1]).get("findings") if payload else None
            except (ValueError, TypeError, AttributeError):
                findings = None
            if token in records and findings == records[token]:
                requested.append(token)
                try:
                    posted_at = datetime.fromisoformat(note.time)
                    if posted_at.utcoffset() is not None:
                        postings.append((posted_at, index, token))
                except (ValueError, TypeError):
                    pass
        if not is_architect:
            continue
        match = re.match(r"Architect: (APPROVED|CHANGES) ([0-9a-fA-F]{7,})(?:\s|$)", body)
        if not match:
            continue
        prefix = match[2].lower()
        matching = {token.split("@", 1)[0] for token in records
                    if token.split("@", 1)[0].lower().startswith(prefix)}
        if len(matching) != 1:
            continue
        sha = matching.pop()
        allowed = {f["finding_key"] for token, findings in records.items()
                   if token.split("@", 1)[0] == sha for f in findings}
        try:
            when = datetime.fromisoformat(note.time)
            if when.utcoffset() is None:
                continue
        except (ValueError, TypeError):
            continue
        try:
            tail = body[match.end():].strip()
            payload = re.search(r"```(?:json)?\s*(.*?)```", tail, re.S)
            data = json.loads(payload[1] if payload else tail) if tail else {}
            got = data.get("dispositions", {})
            if not isinstance(got, dict) or any(not isinstance(k, str) or v not in DISPOSITIONS for k, v in got.items()):
                got = {}
            got = {k: v for k, v in got.items() if k in allowed}
        except (ValueError, TypeError, AttributeError):
            got = {}
        replies.append((when, index, sha, match[1], note.time, got))
    replies.sort(key=lambda row: row[:2])
    dispositions = {}
    for *_, got in replies:
        dispositions.update(got)
    matched = [row for row in replies if head is None or row[2] == head]
    if not matched:
        if not dispositions:
            return None, requested
        return reconcile.ArchitectReply(head or "", "", "", dict(dispositions)), requested
    when, index, sha, status, created_at, _ = matched[-1]
    prior = [row for row in postings if row[:2] <= (when, index) and row[2].split("@", 1)[0] == sha]
    # A successful local post remains trusted even when the independent login read is unavailable.
    token = max(prior)[2] if prior else next((t for t in reversed(records) if t.split("@", 1)[0] == sha), "")
    return reconcile.ArchitectReply(sha, status, created_at, dict(dispositions), token), requested


def gather(store: Any, items: list, forge: Any = None, at: str = "", policies: Any = None,
           architect_login: str | None = None) -> dict:
    """Read the same issue context for the engine pass and merge recheck; `at` stamps a head-read failure log."""
    segments = {item.issue_key: store.segments(item.issue_key) for item in items}
    states = {item.issue_key: _resume(store, item.issue_key) for item in items}
    issue_types = {item.issue_key: states[item.issue_key].get("issue_type") or getattr(item, "issue_type", None)
                   for item in items}
    findings = {}
    heads, replies, requested = {}, {}, {}
    requests, listing = None, None
    engine_author, identity_read = None, False
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
            if request is None or not (policies is None or reconcile.consults_architect(
                    policies, segments[key], issue_types[key], states[key].get("room"))):
                continue
            known = reconcile.request_records(states[key].get("architect_requests") or {})
            pending = reconcile.request_records(states[key].get("architect_pending_requests") or {})
            # Uncertain delivery waits until both the identity and comments can be verified.
            requested[key] = {**known, **pending}
            if not identity_read:
                identity_read = True
                try:
                    engine_author = forge.login() if hasattr(forge, "login") else None
                except ForgeError as exc:
                    if at:
                        store.add_log(key, f"engine identity unreadable: {exc}", at)
            try:
                comments = forge.change_comments(request.number)
            except ForgeError as exc:
                message = f"architect comments unreadable: {exc}"
                logged = store.log(key)
                if at and (not logged or logged[-1].message != message):
                    store.add_log(key, message, at)
                continue
            if pending and engine_author:
                _, posted = architect_comments(comments, head=heads[key], author=architect_login or None,
                                                engine_author=engine_author, requests={**known, **pending})
                for token in posted:
                    if token in pending:
                        known[token] = pending.pop(token)
                data = states[key]
                data["architect_requests"] = known
                if pending:
                    data["architect_pending_requests"] = pending
                else:
                    data.pop("architect_pending_requests", None)
                store.set_resume(key, data)
                requested[key] = known  # a verified absent post can be attempted again
            reply, _ = architect_comments(comments, head=heads[key], author=architect_login or None,
                                           engine_author=engine_author, requests=known)
            if reply is not None:
                replies[key] = reply

    return {
        "segments": segments,
        "launcher_holds": {key: state.get("launcher_hold", {}) for key, state in states.items()},
        "issue_types": issue_types,
        "open_findings": {key: value for key, value in findings.items() if value is not None},
        "heads": heads,
        "architect_replies": replies,
        "consult_requests": requested,
        "rooms": {key: state.get("room") for key, state in states.items()},
        "decisions": {item.issue_key: store.decisions(item.issue_key) for item in items},
        "quota_resets": {key: datetime.fromisoformat(quota["retry_at"]) for key, state in states.items()
                         if (quota := state.get("quota"))},
    }

