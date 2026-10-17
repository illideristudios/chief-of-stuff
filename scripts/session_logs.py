"""Cached session transcript records shared by limits and worker activity (gateway, §10).

Both consumers use the same decoded shape. The cache is bounded and invalidated by file
identity, size and modification time; a rotated or unreadable log cannot hide other logs.
"""
from __future__ import annotations

import json
import mmap
from functools import lru_cache
from pathlib import Path


@lru_cache(maxsize=128)
def _scan(path: Path, inode: int, size: int, modified: int) -> tuple[tuple[int, dict], ...]:
    if not size:
        return ()
    found = []
    with path.open("rb") as stream:
        with mmap.mmap(stream.fileno(), 0, access=mmap.ACCESS_READ) as data:
            while line := data.readline():
                offset = data.tell() - len(line)
                try:
                    event = json.loads(line)
                except (ValueError, UnicodeDecodeError):
                    continue
                if isinstance(event, dict):
                    # Keep usage/status fields; ordinary message text is not needed by either reader.
                    kept = {key: event[key] for key in ("timestamp", "type", "subtype", "rate_limit_info",
                            "rateLimitInfo", "error", "isApiErrorMessage") if key in event}
                    message = event.get("message")
                    if isinstance(message, dict):
                        kept["message"] = {key: value for key, value in message.items()
                                           if key in ("id", "usage") or (key == "content" and event.get("isApiErrorMessage"))}
                    found.append((offset, kept))
    return tuple(found)


def records(root: Path):
    """Yield (source path, byte offset, record) for Claude JSONL sessions below root."""
    for path in root.rglob("*.jsonl"):
        try:
            path = path.resolve()
            stat = path.stat()
            yield from ((path, offset, event) for offset, event in
                        _scan(path, stat.st_ino, stat.st_size, stat.st_mtime_ns))
        except (OSError, ValueError, RuntimeError):
            continue
