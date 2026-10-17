"""One pass's recovery facts, gathered before planning. Frameworks and Drivers; no policy of its own."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import worker
import process_status


def recovery(store: Any, key: str, *, trees: Path, repo: Path | None) -> dict:
    """Decision rows and the current recovery conflict. No repository means unreadable recovery facts."""
    decisions = [(row.topic, row.question, row.answer) for row in store.decisions(key)]
    segments = store.segments(key)
    latest = segments[-1] if segments else None
    data = store.resume(key)
    runs = data.get("runs", {}) if isinstance(data, dict) else {}
    record = runs.get(f"{latest.stage}-{latest.attempt}", {}) if latest else {}
    live = False
    if latest is not None and latest.ended_at is None and record:
        # Store identity first: an active worker needs no git recovery scan.
        live = any(process_status.identity(record[field], record["session_id"]) is not None
                   for field in ("pid", "runner_pid") if record.get(field))
    needed = any(topic == "conflict" for topic, _, _ in decisions) or (latest is not None and latest.ended_at is None and not live)
    return {"decisions": decisions,
            "conflict": worker.recovery_conflict(store, key, trees=trees, repo=repo) if needed and repo is not None else None,
            "readable": repo is not None}
