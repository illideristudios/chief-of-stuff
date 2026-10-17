"""Account limit readings and pure judgements; rates are units per hour (§10)."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class Reading:
    runtime: str
    account: str
    limit: str
    used: float | None
    cap: float | None
    unit: str
    resets_at: datetime | None
    read_at: datetime
    unknown: str


def burn_rate(readings: list[Reading]) -> float | None:
    """Observed units/hour in the latest reset window, never across accounts or limits.

    Missing data, equal timestamps, changing caps and counter corrections cannot establish a rate.
    """
    if not readings or len({(r.runtime, r.account, r.limit) for r in readings}) != 1:
        return None
    ordered = sorted(readings, key=lambda r: r.read_at)
    last = ordered[-1]
    if last.unknown or last.used is None or last.cap is None:
        return None
    window = [r for r in ordered if not r.unknown and r.used is not None
              and (r.resets_at, r.cap, r.unit) == (last.resets_at, last.cap, last.unit)]
    if len(window) < 2:
        return None
    if any(b.used < a.used for a, b in zip(window, window[1:])):
        return None
    first = window[0]
    hours = (last.read_at - first.read_at).total_seconds() / 3600
    return (last.used - first.used) / hours if hours > 0 else None


def near_cap(readings: list[Reading], now: datetime, warn_at: float) -> bool:
    """Any limit can trigger the warning, but runtime and account must stay isolated."""
    if not readings or len({(r.runtime, r.account) for r in readings}) != 1:
        return False
    groups: dict[str, list[Reading]] = {}
    for r in readings:
        groups.setdefault(r.limit, []).append(r)
    return any(_limit_near_cap(rows, now, warn_at) for rows in groups.values())


def _limit_near_cap(readings: list[Reading], now: datetime, warn_at: float) -> bool:
    last = max(readings, key=lambda r: r.read_at)
    if (last.unknown or last.used is None or last.cap is None or last.cap <= 0
            or last.read_at > now or last.resets_at is None or last.resets_at <= now):
        return False
    if last.used / last.cap >= warn_at:
        return True
    rate = burn_rate(readings)
    return rate is not None and (last.used + rate * (last.resets_at - last.read_at).total_seconds() / 3600) / last.cap >= warn_at
