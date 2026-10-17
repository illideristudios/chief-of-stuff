"""One pass's recovery facts, gathered before planning. Frameworks and Drivers; no policy of its own."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import worker


def recovery(store: Any, key: str, *, trees: Path, repo: Path | None) -> dict:
    """Decision rows and the current recovery conflict. No repository means unreadable recovery facts."""
    decisions = [(row.topic, row.question, row.answer) for row in store.decisions(key)]
    return {"decisions": decisions,
            "conflict": worker.recovery_conflict(store, key, trees=trees, repo=repo) if repo is not None else None,
            "readable": repo is not None}
