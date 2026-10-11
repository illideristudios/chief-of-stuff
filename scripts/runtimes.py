"""The agent CLIs a coordinator or worker runs in, one record each.

What a launch needs to know about a runtime without knowing which one it is. The argv each launcher builds
and the words each prompt uses stay with that launcher and that prompt. This module imports nothing from the
plugin, so `start_coordinator` can load it as `scripts.runtimes` and the scripts as `runtimes`.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Runtime:
    name: str  # the `--runtime` value, a `[models]` entry's prefix, and a registration's `runtime`
    binary: str  # the executable its CLI installs
    display: str  # how a prompt names the host
    needs_model: bool  # a worker launch refuses to start without `--model`
    schedules: bool  # the host can run a recurring in-session check; without one, checks happen each turn
    effort_argv: tuple[str, ...] = ()  # the argv tokens that pass an effort, `{effort}` standing for the level; none where the CLI takes no effort

    @property
    def takes_effort(self) -> bool:
        return bool(self.effort_argv)

    def effort_tokens(self, level: str) -> list[str]:
        """The argv tokens that pass `level`; none for an empty level."""
        return [token.format(effort=level) for token in self.effort_argv] if level else []


RUNTIMES = (
    Runtime("claude", binary="claude", display="Claude Code", needs_model=False, schedules=True,
            effort_argv=("--effort", "{effort}")),
    Runtime("codex", binary="codex", display="Codex CLI", needs_model=False, schedules=False,
            effort_argv=("-c", "model_reasoning_effort={effort}")),
    Runtime("cursor", binary="agent", display="Cursor CLI", needs_model=False, schedules=True),
    Runtime("agy", binary="agy", display="Antigravity", needs_model=True, schedules=True,
            effort_argv=("--effort", "{effort}")),
)
NAMES = tuple(runtime.name for runtime in RUNTIMES)
EFFORTS = ("low", "medium", "high", "xhigh", "max")  # the levels a worker launch and a persona take
_BY_NAME = {runtime.name: runtime for runtime in RUNTIMES}


def get(name: str) -> Runtime:
    """The record for `name`. An unknown name is a KeyError: callers validate before they launch."""
    return _BY_NAME[name]


def program(name: str, claude_as_profile: str = "") -> tuple[str, list[str]]:
    """The executable a launch resolves and the argv between it and the CLI's own flags. Zach, 2026-10-08 20:50:
    Claude workers "need to use "cl secondary" (aka claude-as)", so a Claude launch with a profile runs
    `claude-as run <profile>`, which execs `claude` under that profile's config dir."""
    if name == "claude" and claude_as_profile:
        return "claude-as", ["run", claude_as_profile]
    return binary(name), []


def binary(name: str) -> str:
    """The executable for `name`. A session registration can name a runtime this release does not know;
    its name stands in for the executable, as the liveness checks always read it."""
    runtime = _BY_NAME.get(name)
    return runtime.binary if runtime else name
