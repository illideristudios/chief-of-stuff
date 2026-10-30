"""The engine's clock, and the tracker clock readings: `HH:MM` alone or leading a line, a `HH:MM–HH:MM`
range and a `YYYY-MM-DD[ HH:MM]` date."""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

# Zero-arg callable → aware UTC instant. Injected so deadline and age logic can pin time in tests.
Clock = Callable[[], datetime]


def system() -> datetime:
    """Wall clock in UTC. The one impure read in this module."""
    return datetime.now(timezone.utc)


HHMM = re.compile(r"^(\d{1,2}):(\d{2})$")
# The clock a Log line or a tracker cell starts with; whatever follows it is the line's own text.
LEADING = re.compile(r"^\s*(\d{1,2}):(\d{2})\b")
# Accept both separators in a completed time range.
RAN = re.compile(r"^(\d{1,2}:\d{2})[–-](\d{1,2}:\d{2})$")
DATE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})(?:[ T](\d{1,2}):(\d{2}))?$")


def hhmm(value: str, day: date, zone: ZoneInfo) -> datetime | None:
    m = HHMM.match(value.strip())
    try:
        return datetime.combine(day, time(int(m[1]), int(m[2])), tzinfo=zone) if m else None
    except ValueError:  # 24:00, 9:75: not a clock, so no clock
        return None


def leading(text: str | None, day: date, zone: ZoneInfo) -> datetime | None:
    """The `HH:MM` `text` starts with, on `day`; None when it starts with no clock."""
    m = LEADING.match(text or "")
    try:
        return datetime.combine(day, time(int(m[1]), int(m[2])), zone) if m else None
    except ValueError:  # 24:00, 9:75: not a clock, so no clock
        return None


def done_clock(value: str, now: datetime, zone: ZoneInfo) -> datetime | None:
    """(#213) A done task's bare `HH:MM` is its latest occurrence at or before `now`: today's, unless
    that is still later than `now`, in which case it hasn't happened yet and means yesterday's."""
    t = hhmm(value, now.date(), zone)
    return t if t is None or t <= now else t - timedelta(days=1)


def dur(td: timedelta) -> str:
    h, m = divmod(int(td.total_seconds()) // 60, 60)
    return f"{h}h{m:02d}m" if h else f"{m}m"


def parse_stamp(raw: str | None, fallback: datetime) -> datetime:
    """Read a stored ISO timestamp in the fallback zone; malformed values use the fallback instant."""
    if raw:
        try:
            dt = datetime.fromisoformat(raw)
            if dt.tzinfo is None and fallback.tzinfo is not None:
                dt = dt.replace(tzinfo=fallback.tzinfo)
            elif dt.tzinfo is not None and fallback.tzinfo is not None:
                dt = dt.astimezone(fallback.tzinfo)
            return dt
        except ValueError:
            pass
    return fallback
