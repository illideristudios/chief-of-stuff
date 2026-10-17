"""Shared Claude transcript discovery and decoding for worker activity and account limits (§10)."""
from __future__ import annotations

import json
import os
import re
import threading
from pathlib import Path

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


def _logs(account: str, tree: Path | None) -> list[Path]:
    """The account's transcripts (one worker tree's, when given), forgetting cached files that no longer exist."""
    root = claude_home(account) / "projects"
    scope = root if tree is None else root / re.sub(r"[^A-Za-z0-9]", "-", str(tree.resolve()))
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


def activity_events(account: str, tree: Path):
    """Yield (log path, line number, {timestamp, message id and usage}) for one worker tree's transcripts."""
    for log in _logs(account, tree):
        for n, event in _records("activity", log, lambda line: True, _activity):
            yield log, n, event
