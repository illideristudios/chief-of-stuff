"""Read structured local limit events without starting a CLI or making a network call.

    Event timestamps stay on readings: rereading a transcript is not a new usage observation.
    Token/cost counters and ordinary conversation text are ignored; native cap-hit errors report limits.
"""
from __future__ import annotations

import math
import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from limits import Reading
from runtimes import NAMES
import settings
import session_logs


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
        valid = []
        for candidate in resets:
            try:
                utc = candidate.astimezone(timezone.utc)
                # ZoneInfo also represents nonexistent gap times; only accept clocks that round-trip.
                if utc > stamp and utc.astimezone(zone).replace(tzinfo=None) == candidate.replace(tzinfo=None):
                    valid.append(utc)
            except ValueError:
                continue
        if not valid:
            return Reading("claude", account, name, None, None, "", None, stamp,
                           "Claude cap-hit event has no supported reset clock/timezone")
        reset = min(valid)
        return Reading("claude", account, name, 100, 100, "%", reset, stamp, "")
    return None


def claude(account: str) -> list[Reading]:
    candidates = []
    for _path, _line, event in session_logs.limit_events(account):
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
        name = data.get("rateLimitType")
        if not isinstance(name, str) or not name:
            continue
        try:
            r = _reading("claude", account, name, _number(data.get("utilization")) * 100,
                         data.get("resetsAt"), event.get("timestamp"))
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
