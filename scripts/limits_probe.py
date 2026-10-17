"""Read structured local limit events without starting a CLI or making a network call.

    Event timestamps stay on readings: rereading a transcript is not a new usage observation.
    Token/cost counters and ordinary conversation text are ignored; native cap-hit errors report limits.
"""
from __future__ import annotations

import json
import math
import mmap
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from limits import Reading
from runtimes import NAMES
import settings


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _time(value) -> datetime:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return datetime.fromtimestamp(value, timezone.utc)
    result = datetime.fromisoformat(value)
    if result.tzinfo is None:
        raise ValueError("timestamp has no timezone")
    return result.astimezone(timezone.utc)


def _number(value) -> float:
    if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value) or value < 0:
        raise ValueError("invalid usage number")
    return float(value)


def _unknown(runtime: str, account: str, reason: str, limit: str = "limits") -> Reading:
    return Reading(runtime, account, limit, None, None, "", None, _now(), reason)


# One engine process repeats the pass. Unchanged files are not mapped again.
# shortcut: changed transcripts are rescanned in full; cached events and deleted-file keys live until process exit.
_scans: dict[tuple[str, int, int], tuple[dict, ...]] = {}


def _nearby_timestamp(data, at: int) -> str | None:
    """A limit event can omit its own timestamp; the nearest other record supplies one."""
    key = b'"timestamp":'
    forward = data.find(key, at)
    backward = data.rfind(key, 0, at)
    if forward < 0 and backward < 0:
        return None
    if backward < 0 or (forward >= 0 and forward - at <= at - backward):
        chosen = forward
    else:
        chosen = backward
    start = chosen + len(key)
    while start < len(data) and data[start] in (32, 9):
        start += 1
    if start >= len(data) or data[start] != 34:
        return None
    end = data.find(b'"', start + 1)
    if end < 0:
        return None
    try:
        return data[start + 1:end].decode()
    except UnicodeDecodeError:
        return None


def _scan_file(path: Path, needles: tuple[bytes, ...]) -> tuple[dict, ...] | None:
    """Search bytes before decoding: transcripts mostly contain unrelated conversation text."""
    found = []
    try:
        with path.open("rb") as stream:
            if not os.fstat(stream.fileno()).st_size:
                return ()
            with mmap.mmap(stream.fileno(), 0, access=mmap.ACCESS_READ) as data:
                offsets = set()
                for needle in needles:
                    start = 0
                    while (hit := data.find(needle, start)) >= 0:
                        left = data.rfind(b"\n", 0, hit) + 1
                        right = data.find(b"\n", hit)
                        if right < 0:
                            right = len(data)
                        if left not in offsets:
                            offsets.add(left)
                            try:
                                event = json.loads(data[left:right])
                            except (ValueError, UnicodeDecodeError):
                                event = None
                            if isinstance(event, dict):
                                if "timestamp" not in event:
                                    stamp = _nearby_timestamp(data, left)
                                    if stamp:
                                        event["timestamp"] = stamp
                                found.append(event)
                        start = right + 1
    except (OSError, ValueError):
        # A rotated/deleted transcript does not hide the other sessions' data.
        return None
    return tuple(found)


def _events(root: Path, markers: tuple[str, ...]):
    needles = tuple(marker.encode() for marker in markers)
    for path in root.rglob("*.jsonl"):
        try:
            stat = path.stat()
        except OSError:
            continue
        if not stat.st_size:
            continue
        key = (str(path), stat.st_mtime_ns, stat.st_size)
        cached = _scans.get(key)
        if cached is None:
            scanned = _scan_file(path, needles)
            if scanned is None:
                continue
            for stale in [item for item in _scans if item[0] == key[0]]:
                del _scans[stale]
            _scans[key] = cached = scanned
        yield from cached


def _reading(runtime, account, name, used, reset, stamp) -> Reading:
    used = _number(used)
    if used > 100:
        raise ValueError("usage exceeds 100 percent")
    return Reading(runtime, account, name, used, 100, "%", _time(reset), _time(stamp), "")


def _latest(runtime: str, account: str, candidates: list[Reading]) -> list[Reading]:
    latest = {}
    now = _now()

    def priority(row: Reading) -> tuple[bool, datetime]:
        # A status-only event cannot erase numeric evidence inside its reset window.
        active = not row.unknown and row.used is not None and row.resets_at is not None and row.resets_at > now
        return active, row.read_at

    for r in candidates:
        if r.read_at > now:
            continue
        if r.limit not in latest or priority(r) >= priority(latest[r.limit]):
            latest[r.limit] = r
    return [(_unknown(runtime, account, "last local limit event has expired; awaiting a new event", name)
             if r.resets_at is not None and r.resets_at <= now else r)
            for name, r in sorted(latest.items())] or [
                _unknown(runtime, account, "no readable structured limit events in local session transcripts")]


def codex(account: str) -> list[Reading]:
    return [_unknown("codex", account, "read in 1.1")]


def _claude_error(event: dict, account: str) -> Reading | None:
    """Native Claude transcripts retain cap-hit errors even when they omit numeric status events."""
    if event.get("type") != "assistant" or event.get("error") != "rate_limit" or event.get("isApiErrorMessage") is not True:
        return None
    message = event.get("message")
    if not isinstance(message, dict) or not isinstance(message.get("content"), list):
        return None
    for block in message["content"]:
        if not isinstance(block, dict) or block.get("type") != "text":
            continue
        text = block.get("text", "")
        if not isinstance(text, str):
            continue
        match = re.fullmatch(r"You've hit your (?:(session|weekly|Opus|Sonnet) )?limit\s*·\s*resets (.+) \(([^)]+)\)", text)
        if not match:
            continue
        name = (match[1] or "subscription").lower()
        try:
            stamp = _time(event.get("timestamp"))
        except (ValueError, TypeError, OverflowError, OSError):
            return None
        clock = re.fullmatch(r"(1[0-2]|[1-9])(?::([0-5]\d))?(am|pm)", match[2])
        try:
            zone = ZoneInfo(match[3])
        except (ZoneInfoNotFoundError, ValueError):
            zone = None
        if clock is None or zone is None:
            return Reading("claude", account, name, None, None, "", None, stamp,
                           "Claude cap-hit event has no supported reset clock/timezone")
        hour = int(clock[1]) % 12 + (12 if clock[3] == "pm" else 0)
        local = stamp.astimezone(zone)
        # Interpret the displayed clock relative to the error's timestamp, never the probe's day.
        # Consider both folds so a repeated DST hour does not silently become tomorrow.
        resets = [(local.replace(hour=hour, minute=int(clock[2] or 0), second=0, microsecond=0)
                   + timedelta(days=day)).replace(fold=fold) for day in (0, 1) for fold in (0, 1)]
        reset = min(r.astimezone(timezone.utc) for r in resets if r.astimezone(timezone.utc) > stamp)
        return Reading("claude", account, name, 100, 100, "%", reset, stamp, "")
    return None


def claude(account: str) -> list[Reading]:
    if account:
        if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_-]*", account):
            return [_unknown("claude", account, "invalid Claude profile name")]
        root = Path(os.environ.get("CLAUDE_AS_PROFILES_DIR") or Path.home() / ".claude-profiles") / account
        if root.is_symlink():
            return [_unknown("claude", account, "Claude profile is a symlink; claude-as refuses it")]
    else:
        root = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
    candidates = []
    for event in _events(root / "projects", ('"rate_limit_info"', '"rateLimitInfo"', '"isApiErrorMessage"')):
        native = _claude_error(event, account)
        if native is not None:
            candidates.append(native)
            continue
        if event.get("type") == "rate_limit_event":
            data = event.get("rate_limit_info", event.get("rateLimitInfo"))
        elif event.get("type") == "system" and event.get("subtype") == "rate_limit":
            data = event.get("rateLimitInfo")
        else:
            continue
        if not isinstance(data, dict):
            continue
        windows = data.get("unifiedWindows")
        if isinstance(windows, dict) and windows:
            items = windows.items()
        else:
            items = [(data.get("rateLimitType"), data)]
        for name, window in items:
            if not isinstance(name, str) or not name or not isinstance(window, dict):
                continue
            try:
                r = _reading("claude", account, name, _number(window.get("utilization")) * 100,
                             window.get("resetsAt"), event.get("timestamp"))
            except (ValueError, TypeError, OverflowError, OSError):
                try:
                    r = Reading("claude", account, name, None, None, "", None,
                                _time(event.get("timestamp")), "local Claude limit event has no numeric usage/reset")
                except (ValueError, TypeError, OverflowError, OSError):
                    continue
            candidates.append(r)
    return _latest("claude", account, candidates)


def cursor(account: str) -> list[Reading]:
    return [_unknown("cursor", account, "read in 1.1")]


def agy(account: str) -> list[Reading]:
    return [_unknown("agy", account, "read in 1.1")]


def account(runtime: str, workers: settings.Workers) -> str:
    return workers.claude_as_profile if runtime == "claude" else ""


def read(runtime: str, account: str) -> list[Reading]:
    try:
        return {"codex": codex, "claude": claude, "cursor": cursor, "agy": agy}[runtime](account)
    except (OSError, ValueError) as exc:
        return [_unknown(runtime, account, f"local limit source unreadable: {exc}")]


def read_all(workers: settings.Workers) -> list[Reading]:
    rows = []
    for runtime in NAMES:
        acct = account(runtime, workers)
        try:
            rows.extend(read(runtime, acct))
        except Exception as exc:
            rows.append(_unknown(runtime, acct, f"local limit read failed: {exc}"))
    return rows
