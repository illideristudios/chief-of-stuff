#!/usr/bin/env python3
"""The Kanban stage rules: the label delta and the drift check. Forge writes stages; the command line is kanban_cli.py."""

from __future__ import annotations

from dataclasses import dataclass

import backlog
from settings import Kanban


@dataclass(frozen=True)
class Delta:
    add: tuple[str, ...] = ()
    remove: tuple[str, ...] = ()

    @property
    def changed(self) -> bool:
        return bool(self.add or self.remove)


@dataclass(frozen=True)
class Sync:
    issue: backlog.IssueRef
    stage: str
    delta: Delta = Delta()
    done: bool = False
    error: str = ""
    current_status: str | None = None
    project_status: str | None = None
    project_add: bool = False

    @property
    def ok(self) -> bool:
        return not self.error


def _managed(config: Kanban) -> set[str]:
    return set(config.stages) | set(config.terminal_stages) | {config.human_review_label}


def _canonical(config: Kanban, labels: tuple[str, ...]) -> tuple[str, ...]:
    """Forge label names are case-insensitive: read a managed label in its configured spelling."""
    names = {name.casefold(): name for name in _managed(config)}
    return tuple(names.get(label.casefold(), label) for label in labels)


def label_delta(config: Kanban, current: tuple[str, ...], stage: str, keep_hold: bool = False) -> Delta:
    """Exactly one numbered stage and an independent hold, preserving every other label."""
    desired = config.label_for(stage)
    hold = config.holds(stage) or keep_hold
    managed = _managed(config)
    remove = tuple(label for label in current if label in managed and label != desired
                   and not (label == config.human_review_label and hold))
    add = tuple(label for label in (desired, config.human_review_label if hold else None)
                if label and label not in current)
    return Delta(add, remove)


def after(current: tuple[str, ...], delta: Delta) -> tuple[str, ...]:
    return tuple(label for label in current if label not in delta.remove) + delta.add


def drift(config: Kanban, labels: tuple[str, ...], stage: str,
          *, project_status: str | None = None, allow_extra_hold: bool = False) -> str:
    """Describe a board disagreement; the audit never repairs one silently."""
    labels = _canonical(config, labels)
    try:
        target = config.label_for(stage)
    except KeyError:
        return f"unmapped tracker stage {stage}"
    hold = config.human_review_label if (config.holds(stage) or
           (allow_extra_hold and config.human_review_label in labels)) else None
    managed = _managed(config)
    expected = {x for x in (hold, target if config.github_project is None else None) if x}
    actual = set(labels) & managed
    problems = []
    if actual != expected:
        problems.append(f"expected {', '.join(sorted(expected)) or 'no managed labels'}, "
                        f"found {', '.join(sorted(actual)) or 'none'}")
    if config.github_project:
        wanted = config.status_for(stage)
        if project_status != wanted:
            problems.append(f"expected project {config.github_project.field} {wanted or 'empty'}, "
                            f"found {project_status or 'none'}")
    return "; ".join(problems)


def release_error(stage: str, expected_stage: str | None, initialize: bool, release_hold: bool) -> str:
    """An extra hold can only be released at an explicitly guarded, unchanged stage."""
    if release_hold and (not expected_stage or stage != expected_stage or initialize):
        return "hold release needs the same current and expected stage, without initialize"
    return ""


def keep_hold(config: Kanban, current, expected_stage: str | None, release_hold: bool) -> bool:
    """Preserve an extra hold unless explicitly released."""
    return not release_hold and extra_hold(config, current, expected_stage)


def extra_hold(config: Kanban, current, expected_stage: str | None) -> bool:
    """A review hold the recorded stage did not set."""
    return config.human_review_label in current and not (expected_stage and config.holds(expected_stage))


def _guard(current, desired, priors, *, expected_stage: str | None, initialize: bool, blanks=(frozenset(),)) -> str:
    """Write only over a state the tracker explains: the recorded stage, with or without the launcher's hold."""
    if current == desired or (expected_stage is not None and current in priors) or (initialize and current in blanks):
        return ""
    if expected_stage is None and not initialize:
        return "commit needs --from-stage or --initialize"
    return "Kanban state changed outside the tracker; reconcile before writing"


def _github_label_delta(config: Kanban, current: tuple[str, ...], stage: str, keep_hold: bool = False) -> Delta:
    if config.github_project is None:
        return label_delta(config, current, stage, keep_hold)
    # A Project Status is the numbered stage; only the human hold lives on its issue.
    hold = config.holds(stage) or keep_hold
    stage_labels = set(config.stages) | set(config.terminal_stages)
    remove = tuple(x for x in current if x in stage_labels or
                   (x == config.human_review_label and not hold))
    add = (config.human_review_label,) if hold and config.human_review_label not in current else ()
    return Delta(add, remove)
