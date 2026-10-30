"""The engine's heartbeat (#1179, T3 design §10): where each item stands, as one short line and one longer summary. Use Cases; no I/O.

The engine gathers plain `Facts` from the store, `status` classifies them (a stall is `worker_health.silence`'s call, not
a second one here), and `deliver` hands the result to the sinks. A sink is where the heartbeat shows: the file the board
and the report read today; a T3 thread title and a pinned board plug in beside it once the engine has an MCP client. One
failing sink never stops the engine or the other sinks: if the title cannot be set, the board carries the heartbeat alone.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol

import worker_health


@dataclass(frozen=True)
class Facts:
    """One item as the store records it."""
    key: str
    title: str = ""
    done: bool = False            # closed and merged
    trio_decided: bool = False    # the decision trio answered for the owner on the way
    park: str = ""                # the engine's park reason, once it parked the item
    split_trio: bool = False      # a split trio left a decision waiting on the owner
    started_at: datetime | None = None  # the open segment's start: a worker is running
    thread_link: str = ""         # the worker's `t3code://threads/<environment>/<thread>`, from its run record


@dataclass(frozen=True)
class Entry:
    key: str
    title: str
    state: str  # running, stalled, parked, done or assumed (done on assumptions)
    why: str = ""  # a parked item's reason
    link: str = ""


def _why(facts: Facts) -> str:
    if facts.split_trio:
        return "split trio"
    if "two failures" in facts.park:
        return "two failures"
    if "fix rounds" in facts.park:
        return "failures"
    return "architectural decision"


def _entry(facts: Facts, now: datetime, progress: Mapping[str, datetime | None], limit: timedelta) -> Entry | None:
    state, why = "", ""
    if facts.done:
        state = "assumed" if facts.trio_decided else "done"
    elif facts.started_at is not None:
        # Unknown progress is no sign of a stall, as in the planner: only a worker whose progress was read can be judged.
        silent = facts.key in progress and worker_health.silence(facts.started_at, progress[facts.key], now, limit) == "act"
        state = "stalled" if silent else "running"
    elif facts.park or facts.split_trio:
        state, why = "parked", _why(facts)
    return Entry(facts.key, facts.title, state, why, facts.thread_link) if state else None


def _age(seconds: float) -> str:
    seconds = max(int(seconds), 0)
    return f"{seconds}s" if seconds < 60 else f"{seconds // 60}m" if seconds < 3600 else f"{seconds // 3600}h"


@dataclass(frozen=True)
class Status:
    entries: tuple[Entry, ...]
    now: datetime
    tick: datetime | None  # the last engine tick

    def count(self, state: str) -> int:
        return sum(entry.state == state for entry in self.entries)

    @property
    def line(self) -> str:
        """Short enough for a thread title."""
        tick = f"tick {self.tick:%H:%M}" if self.tick else "no tick yet"
        return (f"chief · {self.count('running')} running · {self.count('stalled')} stalled · {self.count('parked')} parked"
                f" · {self.count('done')} done · {self.count('assumed')} assumed · {tick}")

    @property
    def summary(self) -> str:
        tick = f"last tick {_age((self.now - self.tick).total_seconds())} ago" if self.tick else "no engine tick yet"
        lines = [tick, f"{self.count('running')} running, {self.count('stalled')} stalled, {self.count('parked')} parked, "
                       f"{self.count('done')} done, {self.count('assumed')} done on assumptions"]
        for state in ("stalled", "parked"):
            found = [f"{e.key} ({e.why})" if e.why else e.key for e in self.entries if e.state == state]
            if found:
                lines.append(f"{state}: " + "; ".join(found))
        return "\n".join(lines)


def status(facts: Sequence[Facts], now: datetime, tick: datetime | None, progress: Mapping[str, datetime | None],
           stall_after: timedelta) -> Status:
    """`progress` holds each running worker's last sign of life, where it was read; `stall_after` is the planner's limit."""
    if now.utcoffset() is None or (tick is not None and tick.utcoffset() is None):
        raise ValueError("timestamps must include a timezone")
    entries = (_entry(f, now, progress, stall_after) for f in facts)
    return Status(tuple(e for e in entries if e is not None), now, tick)


class Sink(Protocol):
    name: str

    def deliver(self, status: Status) -> None: ...


def deliver(sinks: Sequence[Sink], status: Status) -> list[str]:
    """Hand the status to every sink. A sink that fails is reported as `name: reason` and never stops the rest."""
    failures = []
    for sink in sinks:
        try:
            sink.deliver(status)
        except Exception as exc:  # a sink talks to something outside the engine: whatever it raises is its failure
            failures.append(f"{sink.name}: {exc}")
    return failures
