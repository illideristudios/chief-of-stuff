"""Shared Claude transcript discovery and decoding for worker activity and account limits (§10)."""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat as stat_module
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Decoded records per (kind, file), kept compact; a changed or replaced file invalidates its entry.
# shortcut: a changed transcript is rescanned in full, tail-only reads once active transcripts get large.
_scan_lock = threading.Lock()
_scans: dict[tuple[str, Path], tuple[tuple[int, ...], tuple[tuple[int, dict], ...]]] = {}


def claude_home(account: str = "") -> Path:
    if not account:
        return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
    if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_-]*", account):
        raise ValueError("invalid Claude profile name")
    root = Path(os.environ.get("CLAUDE_AS_PROFILES_DIR") or Path.home() / ".claude-profiles") / account
    if root.is_symlink():
        raise ValueError("Claude profile is a symlink; claude-as refuses it")
    return root


def _records(kind: str, path: Path, admit, reduce) -> tuple[tuple[int, dict], ...]:
    """The reduced records of the lines `admit` accepts, decoded outside the lock so readers never queue behind a scan."""
    try:
        stat = path.stat()
        signature = (stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size, stat.st_ino)
        with _scan_lock:
            hit = _scans.get((kind, path))
        if hit and hit[0] == signature:
            return hit[1]
        records = []
        with path.open(errors="replace") as stream:
            for n, line in enumerate(stream):
                if not admit(line):
                    continue
                try:
                    raw = json.loads(line)
                except ValueError:
                    continue
                if isinstance(raw, dict) and (event := reduce(raw)) is not None:
                    records.append((n, event))
        with _scan_lock:
            _scans[kind, path] = signature, tuple(records)
        return tuple(records)
    except OSError:
        # A rotated/deleted/unreadable transcript does not hide the remaining sessions.
        return ()


def _limit(raw: dict) -> dict:
    event = {k: raw[k] for k in ("type", "subtype", "timestamp", "error", "isApiErrorMessage",
                                 "rate_limit_info", "rateLimitInfo") if k in raw}
    message = raw.get("message")
    if isinstance(message, dict):
        # A cap-hit error's text is the only retained message body: it carries the reset clock.
        keys = ("id", "usage", "content") if raw.get("isApiErrorMessage") is True else ("id", "usage")
        event["message"] = {k: message[k] for k in keys if k in message}
    return event


def _activity(raw: dict) -> dict | None:
    if "timestamp" not in raw:
        return None
    event = {"timestamp": raw["timestamp"]}
    message = raw.get("message")
    if isinstance(message, dict):
        event["message"] = {k: message[k] for k in ("id", "usage") if k in message}
    return event


def _slug(tree: Path) -> str:
    return re.sub(r"[^A-Za-z0-9]", "-", str(tree.resolve()))


def _logs(account: str, tree: Path | None) -> list[Path]:
    """The account's transcripts (one worker tree's, when given), forgetting cached files that no longer exist."""
    root = claude_home(account) / "projects"
    scope = root if tree is None else root / _slug(tree)
    logs = list(scope.rglob("*.jsonl") if tree is None else scope.glob("*.jsonl"))
    live = set(logs)
    with _scan_lock:
        for key in [k for k in _scans if k[1] not in live and (
                k[1].is_relative_to(scope) if tree is None else k[1].parent == scope)]:
            del _scans[key]
    return logs


def limit_events(account: str = ""):
    """Yield (log path, line number, event) for every line of the account that can hold a rate-limit event."""
    for log in _logs(account, None):
        for n, event in _records("limit", log, lambda line: "rate_limit" in line, _limit):
            yield log, n, event


def retain_activity(account: str, trees) -> None:
    """Drop activity entries for worker trees this render did not ask for.

    The page server never scans the account root, and an ended worker's transcript stays on disk, so the
    entry would otherwise live until the process restarts.
    """
    root = claude_home(account) / "projects"
    keep = {root / _slug(Path(tree)) for tree in trees}
    with _scan_lock:
        for key in [k for k in _scans if k[0] == "activity" and k[1].parent not in keep]:
            del _scans[key]


def activity_events(account: str, tree: Path):
    """Yield (log path, line number, {timestamp, message id and usage}) for one worker tree's transcripts."""
    for log in _logs(account, tree):
        for n, event in _records("activity", log, lambda line: True, _activity):
            yield log, n, event


# Read one Claude worker's native log without conflating worktrees or sessions. Callers must
# replace their displayed history on a cursor reset and escape text when rendering HTML.
MAX_RECORD_BYTES = 1024 * 1024
MAX_PAGE_BYTES = 4 * MAX_RECORD_BYTES
MAX_PAGE_RECORDS = 200
_TAIL_BYTES = 64


@dataclass(frozen=True)
class Cursor:
    device: int
    inode: int
    offset: int
    tail_hash: str = ""

    def __post_init__(self):
        if any(type(v) is not int or v < 0 for v in (self.device, self.inode, self.offset)):
            raise ValueError("invalid transcript cursor")
        if not isinstance(self.tail_hash, str):
            raise ValueError("invalid transcript cursor")


@dataclass(frozen=True)
class Page:
    status: str  # ok, missing, unreadable, record_too_large
    events: tuple[dict[str, Any], ...]
    cursor: Cursor
    reset: bool = False
    has_more: bool = False


def _tail_hash(stream, offset: int) -> str:
    stream.seek(max(0, offset - _TAIL_BYTES))
    return hashlib.sha256(stream.read(min(offset, _TAIL_BYTES))).hexdigest()


def _events(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, dict) or raw.get("type") not in ("user", "assistant") or raw.get("isMeta") is True:
        return []
    role = raw["type"]
    message = raw.get("message")
    if not isinstance(message, dict):
        return []
    timestamp = raw.get("timestamp") if isinstance(raw.get("timestamp"), str) else ""
    content = message.get("content")
    if isinstance(content, str):
        return [{"kind": role, "timestamp": timestamp, "text": content}]
    if not isinstance(content, list):
        return []
    result = []
    for block in content:
        if not isinstance(block, dict):
            continue
        kind = block.get("type")
        if kind == "text" and isinstance(block.get("text"), str):
            result.append({"kind": role, "timestamp": timestamp, "text": block["text"]})
        elif kind == "tool_use" and role == "assistant" and isinstance(block.get("name"), str):
            result.append({"kind": kind, "timestamp": timestamp, "name": block["name"],
                           "id": block.get("id", ""), "input": block.get("input")})
        elif kind == "tool_result" and role == "user":
            result.append({"kind": kind, "timestamp": timestamp, "tool_use_id": block.get("tool_use_id", ""),
                           "content": block.get("content"), "is_error": block.get("is_error") is True})
    return result


def read_claude(tree: Path, session_id: str, *, account: str = "", config_dir: Path | None = None,
                cursor: Cursor | None = None) -> Page:
    """Read bounded events for exactly one session in one worker tree and account.

    Missing/unreadable logs are explicit; neither means the worker has ended. Cursor identity
    and a trailing digest detect replacement, truncation, and rewrites around the last read.
    Oversized records are reported, rather than returned as incomplete transcript content.
    """
    try:
        if str(uuid.UUID(session_id)) != session_id:
            raise ValueError
    except (ValueError, AttributeError, TypeError):
        raise ValueError("session_id must be a canonical UUID") from None
    if cursor is not None and not isinstance(cursor, Cursor):
        raise ValueError("invalid transcript cursor")
    if config_dir is not None and (account or not Path(config_dir).is_absolute()):
        raise ValueError("config_dir must be absolute and cannot be combined with an account")
    home = Path(config_dir) if config_dir is not None else claude_home(account)
    path = home / "projects" / _slug(Path(tree)) / f"{session_id}.jsonl"
    previous = cursor or Cursor(0, 0, 0)
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    except FileNotFoundError:
        return Page("missing", (), previous)
    except OSError:
        return Page("unreadable", (), previous)
    try:
        with os.fdopen(fd, "rb") as stream:
            stat = os.fstat(stream.fileno())
            if not stat_module.S_ISREG(stat.st_mode):
                return Page("unreadable", (), previous)
            reset = bool(cursor and (cursor.device != stat.st_dev or cursor.inode != stat.st_ino
                                     or cursor.offset > stat.st_size
                                     or (cursor.tail_hash and _tail_hash(stream, cursor.offset) != cursor.tail_hash)))
            offset = 0 if reset or cursor is None else cursor.offset
            stream.seek(offset)
            events = []
            consumed = 0
            status = "ok"
            has_more = False
            for _ in range(MAX_PAGE_RECORDS):
                line = stream.readline(MAX_RECORD_BYTES + 1)
                if not line:
                    break
                if len(line) > MAX_RECORD_BYTES:
                    status = "record_too_large"
                    break
                if not line.endswith(b"\n"):
                    break
                if consumed and consumed + len(line) > MAX_PAGE_BYTES:
                    has_more = True
                    break
                offset += len(line)
                consumed += len(line)
                try:
                    events.extend(_events(json.loads(line)))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    continue
            else:
                has_more = offset < stat.st_size
            return Page(status, tuple(events), Cursor(stat.st_dev, stat.st_ino, offset, _tail_hash(stream, offset)), reset, has_more)
    except OSError:
        return Page("unreadable", (), previous)
