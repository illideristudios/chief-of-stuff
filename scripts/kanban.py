#!/usr/bin/env python3
"""The Kanban stage rules: the label delta, the drift check, and the GitLab sync. The command line is kanban_cli.py."""

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


def extra_hold(config: Kanban, current, expected_stage: str | None) -> bool:
    """A review hold the recorded stage did not set: the launcher's, which only the user clears."""
    return config.human_review_label in current and not (expected_stage and config.holds(expected_stage))


def _guard(current, desired, priors, *, expected_stage: str | None, initialize: bool, blanks=(frozenset(),)) -> str:
    """Write only over a state the tracker explains: the recorded stage, with or without the launcher's hold."""
    if current == desired or (expected_stage is not None and current in priors) or (initialize and current in blanks):
        return ""
    if expected_stage is None and not initialize:
        return "commit needs --from-stage or --initialize"
    return "Kanban state changed outside the tracker; reconcile before writing"


def sync_gitlab(cfg: backlog.Backlog, ref: backlog.IssueRef, config: Kanban, stage: str,
                *, token: str | None = None, commit: bool = False,
                expected_stage: str | None = None, initialize: bool = False,
                timeout: float = backlog.TIMEOUT) -> Sync:
    """GitLab's add/remove update leaves unrelated issue labels untouched."""
    if ref.host != backlog.home_of(cfg)[0]:
        return Sync(ref, stage, error="issue host does not match the configured GitLab host")
    try:
        config.label_for(stage)
    except KeyError:
        return Sync(ref, stage, error=f"unmapped tracker stage: {stage}")
    if expected_stage is not None:
        try:
            config.label_for(expected_stage)
        except KeyError:
            return Sync(ref, stage, error=f"unmapped prior stage: {expected_stage}")
    project = cfg if cfg.project == ref.repo else cfg.sibling(ref.repo)
    secret = token if token is not None else backlog.token(project)
    if not secret:
        return Sync(ref, stage, error=cfg.missing_token)
    url = f"{project.issues_url}/{ref.number}"
    row, _, error = backlog._get(url, secret, timeout)
    if error:
        return Sync(ref, stage, error=error)
    if not isinstance(row, dict) or not isinstance(row.get("labels"), list):
        return Sync(ref, stage, error="GitLab issue has no readable labels")
    current = tuple(str(label) for label in row["labels"])
    held = extra_hold(config, current, expected_stage)
    delta = label_delta(config, current, stage, keep_hold=held)
    managed = _managed(config)
    prior = frozenset(label_delta(config, (), expected_stage).add) if expected_stage else frozenset()
    hold = frozenset({config.human_review_label})
    desired = set(after(current, delta)) & managed
    if commit:
        error = _guard(frozenset(current) & managed, desired, (prior, prior | hold),
                       expected_stage=expected_stage, initialize=initialize, blanks=(frozenset(), hold))
        if error:
            return Sync(ref, stage, delta, error=error)
    if not delta.changed:
        return Sync(ref, stage, delta, done=True)
    if not commit:
        return Sync(ref, stage, delta)
    have, error = backlog.existing_labels(project, token=secret, timeout=timeout)
    if error:
        return Sync(ref, stage, delta, error=error)
    missing = set(delta.add) - have
    if missing:
        return Sync(ref, stage, delta, error=f"missing label in GitLab: {', '.join(sorted(missing))}; "
                    f"create the label(s) {', '.join(sorted(missing))} before syncing")
    payload = {}
    if delta.add:
        payload["add_labels"] = ",".join(delta.add)
    if delta.remove:
        payload["remove_labels"] = ",".join(delta.remove)
    _, _, error = backlog._call("PUT", url, secret, timeout, payload)
    if error:
        return Sync(ref, stage, delta, error=error)
    verified, _, error = backlog._get(url, secret, timeout)
    if error or not isinstance(verified, dict) or not isinstance(verified.get("labels"), list):
        return Sync(ref, stage, delta, error=error or "could not verify GitLab labels after update")
    if set(verified["labels"]) != set(after(current, delta)):
        return Sync(ref, stage, delta, error="GitLab labels differ after update; reconcile before continuing")
    return Sync(ref, stage, delta, done=True)


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
