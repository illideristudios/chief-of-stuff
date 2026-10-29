"""Whether an item may be dispatched now. Use Cases; no I/O.

Dependency blockers are the forge's native relationships for this pass. Stop records are the engine's Ask and
Report rows, in the order written. File ownership never holds an item (#1039, C29): overlapping edits surface as
merge conflicts at review.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence


@dataclass(frozen=True)
class Ask:
    issue_key: str
    answer: str | None = None
    stage: str = ""
    attempt: int = 0


@dataclass(frozen=True)
class Report:
    issue_key: str
    outcome: str
    stage: str = ""
    attempt: int = 0


def why_not(issue_key: str, *, blockers: tuple[str, ...] | None, asks: Sequence[Ask],
            reports: Sequence[Report]) -> str | None:
    """Why this item must not be dispatched now, or None when it may."""
    mine_asks = [row for row in asks if row.issue_key == issue_key]
    if any(not (row.answer or "").strip() for row in mine_asks):
        return "unanswered ask"
    mine = [row for row in reports if row.issue_key == issue_key]
    # Reports arrive in the order written, so the last is the latest stop; an answer clears only its own run's stop.
    if mine and mine[-1].outcome == "blocked" and not any(
            row.stage == mine[-1].stage and row.attempt == mine[-1].attempt for row in mine_asks):
        return "worker stopped"
    if blockers is None:
        return "dependency blockers unknown"
    if blockers:
        return "blocked by " + ", ".join(blockers)
    return None


EXHAUSTED_CHIP = "retries exhausted"
EXHAUSTED_UNBLOCK = "Raise the retry limit and release any human-review hold, or close the issue."


@dataclass(frozen=True)
class RetryExhaustion:
    """Evidence that the engine has stopped dispatching this issue (#963)."""
    reason: str
    failure: str = ""
    stage: str = ""
    unblock: str = EXHAUSTED_UNBLOCK

    @property
    def summary(self) -> str:
        """One line for a card mark or Workers action: failure when known, else the interrupt reason."""
        return self.failure or self.reason


def _failure_before_exhausted(messages: list[str]) -> str:
    """The last concrete failure logged before the engine's exhausted line, else ""."""
    for i in range(len(messages) - 1, -1, -1):
        message = messages[i]
        if not str(message).startswith("exhausted:"):
            continue
        for prior in reversed(messages[:i]):
            text = str(prior)
            low = text.casefold()
            if text.startswith(("dispatch failed:", "failed:", "hold release failed:", "launch failed:")):
                return text
            if "failed" in low and not text.startswith(("wait:", "exhausted:", "ambiguity:", "limit:")):
                return text
        return ""
    return ""


def retry_exhaustion(resume: Any, segments, log_entries=()) -> RetryExhaustion | None:
    """The issue's current exhausted interrupt when no live segment runs, else None.

    The resume interrupt is the live signal: a historical `exhausted:` log line alone must not
    mislabel a recovered or re-dispatched issue (#963). A live segment wins over a stale interrupt.
    """
    data = resume if isinstance(resume, dict) else {}
    interrupt = data.get("interrupt") if isinstance(data.get("interrupt"), dict) else {}
    if interrupt.get("kind") != "exhausted":
        return None
    if any(getattr(s, "ended_at", None) is None for s in segments):
        return None
    reason = str(interrupt.get("reason") or "retry limit reached").strip() or "retry limit reached"
    messages = [getattr(entry, "message", entry) for entry in log_entries]
    failure = _failure_before_exhausted([str(m) for m in messages if m])
    return RetryExhaustion(reason=reason, failure=failure, stage=str(interrupt.get("stage") or ""))
