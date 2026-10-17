"""Shared Claude transcript discovery and decoding for worker activity and account limits (§10)."""
from __future__ import annotations

import json
import os
import re
import threading
from pathlib import Path

# One decoded snapshot per file, shared by both consumers. Replaced files invalidate it too.
# shortcut: changed transcripts are rescanned in full; deleted-file keys live until process exit.
_scan_lock = threading.Lock()
_scans: dict[Path, tuple[tuple[int, ...], tuple[tuple[int, dict], ...]]] = {}


def claude_home(account: str = "") -> Path:
    if not account:
        return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
    if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_-]*", account):
        raise ValueError("invalid Claude profile name")
    root = Path(os.environ.get("CLAUDE_AS_PROFILES_DIR") or Path.home() / ".claude-profiles") / account
    if root.is_symlink():
        raise ValueError("Claude profile is a symlink; claude-as refuses it")
    return root


def _records(path: Path) -> tuple[tuple[int, dict], ...]:
    with _scan_lock:
        try:
            stat = path.stat()
            signature = (stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size, stat.st_ino)
            if path in _scans and _scans[path][0] == signature:
                return _scans[path][1]
            records = []
            with path.open(errors="replace") as stream:
                for n, line in enumerate(stream):
                    try:
                        raw = json.loads(line)
                    except ValueError:
                        continue
                    if not isinstance(raw, dict):
                        continue
                    # Keep the shared metadata, not the conversation bodies, in the process cache.
                    event = {k: raw[k] for k in ("type", "subtype", "timestamp", "error", "isApiErrorMessage",
                                                 "rate_limit_info", "rateLimitInfo") if k in raw}
                    message = raw.get("message")
                    if isinstance(message, dict):
                        keys = ("id", "usage", "content") if raw.get("isApiErrorMessage") is True else ("id", "usage")
                        event["message"] = {k: message[k] for k in keys if k in message}
                    records.append((n, event))
            _scans[path] = signature, tuple(records)
            return _scans[path][1]
        except OSError:
            # A rotated/deleted/unreadable transcript does not hide the remaining sessions.
            return ()


def claude_events(account: str = "", *, tree: Path | None = None):
    """Yield (log path, line number, event metadata) for an account, optionally one worker tree."""
    root = claude_home(account) / "projects"
    logs = root.rglob("*.jsonl") if tree is None else (
        root / re.sub(r"[^A-Za-z0-9]", "-", str(tree.resolve()))).glob("*.jsonl")
    for log in logs:
        for n, event in _records(log):
            yield log, n, event
