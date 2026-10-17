"""One pass's recovery facts, gathered before planning. Frameworks and Drivers; no policy of its own."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import worker


def held_stages(store: Any, key: str, *, trees: Path, repo: Path | None) -> set[str]:
    """Stages whose conflict answer is `hold` and whose orphans still conflict.

    `repo` None means no recovery: a hold stays held, and git is not read.
    """
    latest: dict[str, str | None] = {}
    for decision in store.decisions(key):
        if decision.topic == "conflict" and decision.question:
            latest[decision.question] = decision.answer
    held = {question.split("\n", 1)[0] for question, answer in latest.items() if answer == "hold"}
    if repo is None or not held:
        return held
    reason = worker.recovery_conflict(store, key, trees=trees, repo=repo)
    if reason is None:
        return set()
    return {question.split("\n", 1)[0] for question, answer in latest.items()
            if answer == "hold" and question.split("\n", 1)[1] == reason}
