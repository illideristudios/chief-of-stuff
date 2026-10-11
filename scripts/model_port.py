"""The model port: what the engine asks a model and what comes back, whichever host answers (design 7, 7a).

Every model call goes through one adapter that takes a `Request` and returns a `Reply` or a `Failure`
(`claude_adapter.call` is the only one built in 1.0; ledger E13). This module imports nothing from the plugin.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from typing import Any, Callable, Sequence


@dataclass(frozen=True)
class Request:
    prompt: str
    model: str = ""  # empty is the host's default
    effort: str = ""
    schema: dict | None = None  # a JSON schema; the answer comes back as `Reply.data`, which the caller validates
    session_id: str = ""  # with resume, continues that session; without, starts one under this id; empty is stateless
    resume: bool = False
    cwd: str | None = None
    # The worker launch profile: plugin dirs (the guard plugin, D9). Empty is a judgement call: no tools, no plugins.
    plugin_dirs: tuple[str, ...] = ()
    timeout_s: float | None = None


@dataclass(frozen=True)
class Reply:
    text: str
    data: Any  # the structured output when the request had a schema, else None
    session_id: str
    cost_usd: float  # the host's own estimate
    num_turns: int


# missing_program, quota, session_missing, auth and budget each name their cause; error is the model's own failure;
# unknown is anything the host did not explain, outage included, and is worth a retry.
RETRYABLE = ("quota", "unknown")


@dataclass(frozen=True)
class Failure:
    kind: str  # missing_program | quota | session_missing | auth | budget | error | unknown
    message: str

    @property
    def retryable(self) -> bool:
        return self.kind in RETRYABLE


Model = Callable[[Request], "Reply | Failure"]


def quota_reset(text: str) -> str | None:
    """None unless a line shows an exhausted quota: a standalone 429 (after whitespace, `=`, `(` or the line start)
    with RESOURCE_EXHAUSTED or quota, in any case. Then that line's reset text, cut to 80 characters, or ""."""
    for line in text.splitlines():
        low = line.lower()
        if re.search(r"(?<![^\s=(])429(?!\w)", line) and ("resource_exhausted" in low or "quota" in low):
            reset = re.search(r"\bresets?\b(?:[^\".]|\.(?!\s|$))*(?:\.(?=\s|$))?", line, re.IGNORECASE)
            return reset.group()[:80].rstrip() if reset else ""
    return None


def rotate(call: Model, request: Request, models: Sequence[tuple[str, str]]) -> Reply | Failure:
    """Ask each (model, effort) in turn while the answer is a quota stop (ledger E20: Claude models only).
    Any other answer ends the rotation; every model out of quota is the last quota stop, never a failure (#422)."""
    for model, effort in models or [(request.model, request.effort)]:
        got = call(replace(request, model=model, effort=effort))
        if not (isinstance(got, Failure) and got.kind == "quota"):
            return got
    return got
