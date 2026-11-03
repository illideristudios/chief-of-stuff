"""Claude JSONL ingestion with a durable attempt binding, bounded prefix and explicit gaps.

No discovery by tree or most-recent session: the caller supplies the launch's exact source.
A replaced, truncated or rewritten source halts ingestion; its retained prefix remains readable.
"""
from __future__ import annotations

import hashlib
import json
import os
import stat as stat_module
from pathlib import Path

import session_logs

DEFAULT_MAX_BYTES = 1024 * 1024
CHUNK = 64 * 1024


def _append(state: dict, kind: str, content, at: str = "") -> None:
    sequence = state["next_sequence"]
    state["next_sequence"] += 1
    event = {"sequence": sequence, "kind": kind, "at": at, "content": content}
    # Match SQLAlchemy's default JSON serializer, including escaped Unicode and spacing.
    size = len(json.dumps(event).encode("utf-8")) + 2  # array brackets, or the next comma and space
    if state["capped"] or state["retained_bytes"] + size > state["max_bytes"]:
        state["dropped_events"] += 1
        first = not state["capped"]
        state["capped"] = True
        if first and kind != "gap":
            _gap(state, "retention limit exceeded; subsequent events are dropped", retain=False)
        return
    state["events"].append(event)
    state["retained_bytes"] += size


def _gap(state: dict, reason: str, *, retain: bool = True) -> None:
    # Fixed-size last-gap metadata remains visible even when the event buffer is full.
    offset = state["cursor"]["offset"]
    if state["last_gap"] == reason and state.get("last_gap_offset") == offset:
        return
    state["last_gap"] = reason
    state["last_gap_offset"] = offset
    state["gap_count"] += 1
    if retain:
        _append(state, "gap", reason)


def _events(raw: dict):
    kind = raw.get("type")
    message = raw.get("message", {})
    content = message.get("content", "") if isinstance(message, dict) else message
    if kind == "error" or raw.get("isApiErrorMessage") or raw.get("is_error") or raw.get("error"):
        yield "error", content or raw.get("error", "runtime error")
    else:
        for event in session_logs.claude_events(raw):
            yield {"assistant": "worker", "user": "user", "tool_use": "tool", "tool_result": "tool"}[event["kind"]], event


def capture(db, issue_key: str, stage: str, attempt: int, session_id: str,
            path: Path | None, *, ended: bool = False, max_bytes: int = DEFAULT_MAX_BYTES,
            source_error: str = "") -> None:
    """Atomically ingest complete records and advance their cursor; safely retry after restart.

    The cap is pinned at first capture. Once exceeded, keep the prefix and drop every later
    normalized event (including tools), counting drops. Metadata and cursors are fixed-size.
    Oversized JSONL records are counted separately in dropped_records: their event count is unknown.
    """
    if type(max_bytes) is not int or max_bytes <= 0:
        raise ValueError("transcript max_bytes must be a positive integer")

    def ingest(state: dict, matches: bool) -> None:
        if not state:
            state.update(events=[], retained_bytes=0, dropped_events=0, dropped_records=0, next_sequence=1,
                         max_bytes=max_bytes, capped=False, gap_count=0, last_gap="", halted=False,
                         cursor={"offset": 0, "digest": hashlib.sha256(b"").hexdigest()})
        if not matches:
            _gap(state, "launch/source identity changed; capture halted")
            state["halted"] = True
        if state["halted"]:
            return
        if path is None:
            _gap(state, source_error or "transcript source unavailable")
            return
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd, "rb") as stream:
                if not stat_module.S_ISREG(os.fstat(stream.fileno()).st_mode):
                    _gap(state, "transcript source unavailable")
                    return
                _read(stream, state, session_id, ended)
        except OSError:
            _gap(state, "transcript source unavailable")

    db.update_transcript(issue_key, stage, attempt, session_id, str(path.absolute()) if path else None, ingest)


def _read(stream, state: dict, session_id: str, ended: bool) -> None:
    cursor = state["cursor"]
    stat = os.fstat(stream.fileno())
    identity = [stat.st_dev, stat.st_ino]
    digest = hashlib.sha256()
    remaining = cursor["offset"]
    while remaining:
        chunk = stream.read(min(CHUNK, remaining))
        if not chunk:
            break
        digest.update(chunk)
        remaining -= len(chunk)
    if (remaining or digest.hexdigest() != cursor["digest"]
            or ("identity" in cursor and identity != cursor["identity"])):
        _gap(state, "transcript source changed (rotated, truncated or rewritten); capture halted")
        state["halted"] = True
        return
    cursor["identity"] = identity
    while True:
        # A bounded read even for an enormous or unfinished record. Never save raw partial text.
        line = stream.readline(state["max_bytes"] + 1)
        if not line:
            break
        oversized = len(line) > state["max_bytes"]
        if oversized:
            digest.update(line)
            while not line.endswith(b"\n"):
                line = stream.readline(CHUNK)
                digest.update(line)
                if not line:
                    break
        if not line.endswith(b"\n"):
            if ended:
                _gap(state, "partial final transcript record; awaiting completion")
            break  # leave the committed offset before this record, even across a restart
        if oversized:
            state["dropped_records"] += 1
            state["capped"] = True
            _gap(state, "retention limit exceeded; oversized record and subsequent events are dropped", retain=False)
        else:
            digest.update(line)
            try:
                raw = json.loads(line)
            except (ValueError, UnicodeDecodeError):
                _gap(state, "malformed transcript record skipped")
            else:
                if not isinstance(raw, dict):
                    _gap(state, "non-object transcript record skipped")
                else:
                    events = tuple(_events(raw))
                    identity = raw.get("sessionId", raw.get("session_id"))
                    if (events or identity is not None) and identity != session_id:
                        _gap(state, "record session identity changed or missing; capture halted")
                        state["halted"] = True
                        break
                    for kind, content in events:
                        _append(state, kind, content, str(raw.get("timestamp", "")))
        cursor.update(offset=stream.tell(), digest=digest.hexdigest())
