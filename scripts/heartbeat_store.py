"""The heartbeat's facts from the store, and the file sink the board and report read (#1179). Interface Adapters."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import heartbeat
from heartbeat import Facts
from workspace import ENGINE_STATE_DIR, _swap_in

HEARTBEAT_FILE = "heartbeat.json"
THREAD_LINK = "t3code://threads/"


def _when(stamp: str) -> datetime:
    found = datetime.fromisoformat(stamp)
    return found if found.utcoffset() is not None else found.replace(tzinfo=timezone.utc)


def _thread_link(resume: Any) -> str:
    """The latest worker thread's deep link from the run records; anything that is not one is no link."""
    runs = resume.get("runs") if isinstance(resume, dict) else None
    links = [r["thread_link"] for r in (runs or {}).values() if isinstance(r, dict) and r.get("thread_link")]
    return links[-1] if links and str(links[-1]).startswith(THREAD_LINK) else ""


def facts(db: Any) -> list[Facts]:
    running = {s.issue_key: s.started_at for s in db.segments() if s.ended_at is None}
    decisions: dict[str, list] = {}
    for decision in db.decisions():
        decisions.setdefault(decision.issue_key, []).append(decision)
    found = []
    for item in db.items():
        resume = db.resume(item.issue_key)
        mine = decisions.get(item.issue_key, [])
        park = resume.get("park") if isinstance(resume, dict) else None
        found.append(Facts(
            item.issue_key, item.title,
            done=item.forge_state == "closed" and item.merge_state == "merged",
            trio_decided=any(d.decided_by == "trio" and d.answer for d in mine),
            park=str(park.get("reason", "")) if isinstance(park, dict) else "",
            # shortcut: a "split trio" is an owner decision the trio did not agree on and nobody has answered; the store has no other marker
            split_trio=any(d.decided_by == "owner" and not d.answer and isinstance(d.record, dict)
                           and d.record.get("unanimous") is False for d in mine),
            started_at=_when(running[item.issue_key]) if item.issue_key in running else None,
            thread_link=_thread_link(resume)))
    return found


class FileSink:
    """Writes the status where the page server reads it: the board and the report render from this file."""
    name = "file"

    def __init__(self, root: Path) -> None:
        self.path = Path(root) / ENGINE_STATE_DIR / HEARTBEAT_FILE

    def deliver(self, status: heartbeat.Status) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        _swap_in(self.path, json.dumps({"line": status.line, "summary": status.summary,
                                        "now": status.now.isoformat(), "tick": status.tick.isoformat() if status.tick else None,
                                        "entries": [asdict(e) for e in status.entries]}, indent=2) + "\n")


def sinks(root: Path) -> tuple[heartbeat.Sink, ...]:
    """Where the heartbeat shows. The T3 thread-title sink and the pinned-board sink join here once the engine has an MCP
    client; until then the file carries it, which is the design's fallback when the rename fails (§10)."""
    return (FileSink(root),)


def read(root: Path) -> dict | None:
    """The last delivered status, or None before the first beat or when the file is unreadable."""
    try:
        found = json.loads((Path(root) / ENGINE_STATE_DIR / HEARTBEAT_FILE).read_text())
    except (OSError, ValueError):
        return None
    return found if isinstance(found, dict) else None


def beat(db: Any, now: datetime, tick: datetime | None, progress: Mapping[str, datetime | None], stall_after: timedelta,
         *, sinks: Sequence[heartbeat.Sink]) -> list[str]:
    """One heartbeat: compute the status from the store and deliver it. Returns the sink failures."""
    return heartbeat.deliver(sinks, heartbeat.status(facts(db), now, tick, progress, stall_after))
