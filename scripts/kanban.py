#!/usr/bin/env python3
"""Preview or sync a tracked issue's Kanban stage without replacing unrelated labels."""

from __future__ import annotations

import argparse
from datetime import datetime
import json
from dataclasses import dataclass
from pathlib import Path
import sys

import backlog
from _vendor.toon_format import encode as toon_encode
from tracker import parse_tracker
from workspace import ConfigError, read_config
from settings import Kanban, SettingsError, load as load_settings


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


def label_delta(config: Kanban, current: tuple[str, ...], stage: str, keep_hold: bool = False) -> Delta:
    """Exactly one numbered stage and an independent hold, preserving every other label."""
    desired = config.label_for(stage)
    hold = config.holds(stage) or keep_hold
    managed = set(config.stages) | {config.human_review_label}
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
    try:
        target = config.label_for(stage)
    except KeyError:
        return f"unmapped tracker stage {stage}"
    hold = config.human_review_label if (config.holds(stage) or
           (allow_extra_hold and config.human_review_label in labels)) else None
    managed = set(config.stages) | {config.human_review_label}
    expected = {x for x in (hold, target if config.github_project is None else None) if x}
    actual = set(labels) & managed
    problems = []
    if actual != expected:
        problems.append(f"expected {', '.join(sorted(expected)) or 'no managed labels'}, "
                        f"found {', '.join(sorted(actual)) or 'none'}")
    if config.github_project:
        wanted = config.github_project.options[config.stage_map[stage]] if target else None
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
    managed = set(config.stages) | {config.human_review_label}
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
        return Sync(ref, stage, delta, error=f"missing label in GitLab: {', '.join(sorted(missing))}")
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


def _gh_json(gh, args: list[str]):
    code, stdout, stderr = gh(args)
    if code:
        return None, stderr.strip() or f"gh exited {code}"
    try:
        return json.loads(stdout), ""
    except ValueError:
        return None, "gh did not answer with JSON"


def _github_issue(gh, ref: backlog.IssueRef):
    row, error = _gh_json(gh, ["issue", "view", str(ref.number), "-R", ref.repo,
                               "--json", "labels,state,url"])
    if error:
        return (), error
    if not isinstance(row, dict) or not isinstance(row.get("labels"), list):
        return (), "GitHub issue has no readable labels"
    return tuple(str(x["name"]) for x in row["labels"] if isinstance(x, dict) and "name" in x), ""


# shortcut: 20 memberships and chunks of 25, page or raise the chunk when a tracker or an issue exceeds that.
_PROJECT_PAGE = 20
_PROJECT_CHUNK = 25


def _project_query(project, refs):
    grouped, order = {}, []
    for ref in refs:
        if ref.repo not in grouped:
            order.append(ref.repo)
            grouped[ref.repo] = []
        grouped[ref.repo].append(ref)
    parts, layout = [], []
    field = json.dumps(project.field)
    for index, repo in enumerate(order):
        owner, name = repo.split("/", 1)
        issues = []
        for ref in grouped[repo]:
            alias = f"n{ref.number}"
            issues.append(
                f"{alias}: issue(number: {ref.number}) {{ "
                f"projectItems(first: {_PROJECT_PAGE}) {{ pageInfo {{ hasNextPage }} nodes {{ "
                f"project {{ number owner {{ ... on User {{ login }} ... on Organization {{ login }} }} }} "
                f"fieldValueByName(name: {field}) {{ __typename "
                f"... on ProjectV2ItemFieldSingleSelectValue {{ name }} }} }} }} }}")
            layout.append((f"r{index}", alias, ref))
        parts.append(f"r{index}: repository(owner: {json.dumps(owner)}, name: {json.dumps(name)}) "
                     f"{{ {' '.join(issues)} }}")
    return "query { " + " ".join(parts) + " }", layout


def _graphql_messages(body) -> str:
    errors = body.get("errors")
    if not isinstance(errors, list):
        return "GitHub Project query failed"
    messages = [str(item.get("message", item)) if isinstance(item, dict) else str(item) for item in errors]
    return "; ".join(message for message in messages if message) or "GitHub Project query failed"


def _owner_login(item):
    project_node = item.get("project") if isinstance(item, dict) else None
    owner = project_node.get("owner") if isinstance(project_node, dict) else None
    login = owner.get("login") if isinstance(owner, dict) else None
    return login if isinstance(login, str) and login else None


def _read_membership(node, project):
    """(present, status, error). A missing membership is present=False with no error; a short read is an error."""
    if not isinstance(node, dict):
        return False, None, "GitHub issue could not be read"
    connection = node.get("projectItems")
    if not isinstance(connection, dict):
        return False, None, "GitHub Project items could not be parsed"
    page = connection.get("pageInfo")
    if isinstance(page, dict) and page.get("hasNextPage") is True:
        return False, None, "GitHub Project memberships exceed one page"
    nodes = connection.get("nodes")
    if not isinstance(page, dict) or page.get("hasNextPage") is not False or not isinstance(nodes, list):
        return False, None, "GitHub Project items could not be parsed"
    matched = None
    for item in nodes:
        login = _owner_login(item)
        if login is None:
            return False, None, "GitHub Project owner could not be read"
        if item["project"].get("number") == project.number and login.casefold() == project.owner.casefold():
            if matched is not None:
                return False, None, "GitHub Project has two items for this issue"
            matched = item
    if matched is None:
        return False, None, ""
    value = matched.get("fieldValueByName")
    if value is None:
        return True, None, ""
    if not isinstance(value, dict) or value.get("__typename") != "ProjectV2ItemFieldSingleSelectValue":
        return False, None, "GitHub Project status is not a single-select value"
    name = value.get("name")
    if name is not None and not isinstance(name, str):
        return False, None, "GitHub Project status is not a single-select value"
    return True, name, ""


def _project_items(gh, project, refs):
    """Map each issue URL to its single-select value, or None when the field is unset. A missing key is absence."""
    refs = tuple(refs)
    if not refs:
        return {}, ""
    found = {}
    for start in range(0, len(refs), _PROJECT_CHUNK):
        query, layout = _project_query(project, refs[start:start + _PROJECT_CHUNK])
        code, stdout, stderr = gh(["api", "graphql", "-f", f"query={query}"])
        if code:
            return {}, stderr.strip() or f"gh exited {code}"
        try:
            body = json.loads(stdout)
        except ValueError:
            return {}, "gh did not answer with JSON"
        if not isinstance(body, dict):
            return {}, "GitHub Project items could not be parsed"
        if body.get("errors"):
            return {}, _graphql_messages(body)
        data = body.get("data")
        if not isinstance(data, dict):
            return {}, "GitHub Project items could not be parsed"
        for repo_alias, issue_alias, ref in layout:
            repo = data.get(repo_alias)
            if not isinstance(repo, dict) or issue_alias not in repo:
                return {}, "GitHub issue could not be read"
            present, status, error = _read_membership(repo[issue_alias], project)
            if error:
                return {}, error
            if present:
                found[ref.url] = status
    return found, ""


def _project_item(gh, project, ref: backlog.IssueRef):
    items, error = _project_items(gh, project, (ref,))
    return ref.url in items, items.get(ref.url), error


def _project_option_exists(gh, project, value: str) -> str:
    row, error = _gh_json(gh, ["project", "field-list", str(project.number), "--owner", project.owner,
                               "--format", "json", "--limit", "100"])
    if error:
        return error
    fields = row.get("fields") if isinstance(row, dict) else None
    if not isinstance(fields, list):
        return "GitHub Project fields could not be parsed"
    for field in fields:
        if isinstance(field, dict) and field.get("name") == project.field:
            options = field.get("options")
            if isinstance(options, list) and value in {x.get("name") for x in options if isinstance(x, dict)}:
                return ""
            return f"missing GitHub Project option: {value}"
    return f"missing GitHub Project field: {project.field}"


def _github_labels_exist(gh, ref: backlog.IssueRef, names: tuple[str, ...]) -> str:
    if not names:
        return ""
    row, error = _gh_json(gh, ["label", "list", "-R", ref.repo, "--json", "name", "--limit", "5000"])
    if error:
        return error
    if not isinstance(row, list):
        return "GitHub labels could not be parsed"
    have = {x.get("name") for x in row if isinstance(x, dict)}
    missing = set(names) - have
    return f"missing label in GitHub: {', '.join(sorted(missing))}" if missing else ""


def add_human_hold(ref: backlog.IssueRef, home: backlog.Backlog | backlog.GitHubBacklog,
                   config: Kanban, *, gh=None, token: str | None = None) -> str:
    """Add only the review hold; leave the numbered stage and Project Status untouched.

    Return an error string, or empty string when the hold is verified. This is used when a
    one-shot worker exits unfinished, including after partial edits.
    """
    label = config.human_review_label
    if ref.host == backlog.GITHUB:
        gh = gh or backlog.run_gh
        current, error = _github_issue(gh, ref)
        if error or label in current:
            return error
        # #566: the hold is the point — a label the repo has never seen is created, not refused.
        for made in backlog.ensure_labels(backlog.GitHubBacklog(ref.repo), (label,), commit=True, gh=gh):
            if made.error:
                return made.error
        code, _, stderr = gh(["issue", "edit", str(ref.number), "-R", ref.repo, "--add-label", label])
        if code:
            return stderr.strip() or f"gh exited {code}"
        updated, error = _github_issue(gh, ref)
        return error or ("GitHub review hold was not applied" if label not in updated else "")
    if not isinstance(home, backlog.Backlog) or ref.host != backlog.home_of(home)[0]:
        return "issue host does not match the configured backlog"
    project = home if home.project == ref.repo else home.sibling(ref.repo)
    secret = token if token is not None else backlog.token(project)
    if not secret:
        return home.missing_token
    url = f"{project.issues_url}/{ref.number}"
    row, _, error = backlog._get(url, secret, backlog.TIMEOUT)
    if error:
        return error
    if not isinstance(row, dict) or not isinstance(row.get("labels"), list):
        return "GitLab issue has no readable labels"
    if label in row["labels"]:
        return ""
    # #566: the hold is the point — a label the project has never seen is created, not refused.
    for made in backlog.ensure_labels(project, (label,), token=secret, commit=True):
        if made.error:
            return made.error
    _, _, error = backlog._call("PUT", url, secret, backlog.TIMEOUT, {"add_labels": label})
    if error:
        return error
    updated, _, error = backlog._get(url, secret, backlog.TIMEOUT)
    return error or ("GitLab review hold was not applied" if not isinstance(updated, dict) or
                     label not in updated.get("labels", []) else "")


def _github_label_delta(config: Kanban, current: tuple[str, ...], stage: str, keep_hold: bool = False) -> Delta:
    if config.github_project is None:
        return label_delta(config, current, stage, keep_hold)
    # A Project Status is the numbered stage; only the human hold lives on its issue.
    hold = config.holds(stage) or keep_hold
    remove = tuple(x for x in current if x in config.stages or
                   (x == config.human_review_label and not hold))
    add = (config.human_review_label,) if hold and config.human_review_label not in current else ()
    return Delta(add, remove)


def sync_github(ref: backlog.IssueRef, config: Kanban, stage: str, *, gh=None,
                commit: bool = False, expected_stage: str | None = None,
                initialize: bool = False) -> Sync:
    """Update issue labels, or a GitHub Project Status plus the issue's hold label."""
    if ref.host != backlog.GITHUB:
        return Sync(ref, stage, error="issue is not on GitHub")
    try:
        label = config.label_for(stage)
    except KeyError:
        return Sync(ref, stage, error=f"unmapped tracker stage: {stage}")
    if expected_stage is not None:
        try:
            config.label_for(expected_stage)
        except KeyError:
            return Sync(ref, stage, error=f"unmapped prior stage: {expected_stage}")
    gh = gh or backlog.run_gh
    current, error = _github_issue(gh, ref)
    if error:
        return Sync(ref, stage, error=error)
    delta = _github_label_delta(config, current, stage, keep_hold=extra_hold(config, current, expected_stage))
    managed = set(config.stages) | {config.human_review_label}
    current_labels = frozenset(current) & managed
    desired_labels = set(after(current, delta)) & managed
    prior_labels = frozenset(_github_label_delta(config, (), expected_stage).add) if expected_stage else frozenset()
    hold = frozenset({config.human_review_label})
    project = config.github_project
    present, old_status, wanted = False, None, None
    if project:
        present, old_status, error = _project_item(gh, project, ref)
        if error:
            return Sync(ref, stage, delta, error=error)
        wanted = project.options[config.stage_map[stage]] if label is not None else None
        if wanted:
            error = _project_option_exists(gh, project, wanted)
            if error:
                return Sync(ref, stage, delta, error=error)
    project_add = bool(project and not present and wanted is not None)
    changed = delta.changed or (project is not None and (project_add or (present and old_status != wanted)))
    result = Sync(ref, stage, delta, current_status=old_status, project_status=wanted,
                  project_add=project_add)
    if not changed:
        return Sync(ref, stage, delta, done=True, current_status=old_status, project_status=wanted)
    if not commit:
        return result
    if project:
        prior_status = (project.options[config.stage_map[expected_stage]]
                        if expected_stage and expected_stage not in config.terminal_stages else None)
        current_state = (old_status if present else None, frozenset(current_labels))
        desired_state = (wanted, frozenset(desired_labels))
        prior_state = (prior_status, frozenset(prior_labels))
        blanks = ((None, frozenset()), (None, hold))
        if current_state in blanks and initialize:
            error = ""
        else:
            error = _guard(current_state, desired_state, (prior_state, (prior_status, prior_labels | hold)),
                           expected_stage=expected_stage, initialize=False)
    else:
        error = _guard(current_labels, desired_labels, (prior_labels, prior_labels | hold),
                       expected_stage=expected_stage, initialize=initialize, blanks=(frozenset(), hold))
    if error:
        return Sync(ref, stage, delta, error=error, current_status=old_status, project_status=wanted)
    error = _github_labels_exist(gh, ref, delta.add)
    if error:
        return Sync(ref, stage, delta, error=error, current_status=old_status, project_status=wanted)

    def edit_labels():
        if not delta.changed:
            return ""
        cmd = ["issue", "edit", str(ref.number), "-R", ref.repo]
        if delta.add:
            cmd += ["--add-label", ",".join(delta.add)]
        if delta.remove:
            cmd += ["--remove-label", ",".join(delta.remove)]
        code, _, stderr = gh(cmd)
        return stderr.strip() or f"gh exited {code}" if code else ""

    def edit_project():
        if project is None or (not present and wanted is None) or (present and old_status == wanted):
            return ""
        if not present:
            code, _, stderr = gh(["project", "item-add", str(project.number), "--owner", project.owner,
                                  "--url", ref.url])
            if code:
                return stderr.strip() or f"gh exited {code}"
        cmd = ["project", "item-edit", str(project.number), "--owner", project.owner,
               "--url", ref.url, "--field", project.field]
        cmd += ["--value", wanted] if wanted is not None else ["--clear"]
        code, _, stderr = gh(cmd)
        return stderr.strip() or f"gh exited {code}" if code else ""

    # Enter a hold before moving the card. Leave a hold only after the destination is set.
    steps = (edit_labels, edit_project) if config.holds(stage) else (edit_project, edit_labels)
    for step in steps:
        error = step()
        if error:
            return Sync(ref, stage, delta, error=error, current_status=old_status,
                        project_status=wanted, project_add=project_add)
    verified_labels, error = _github_issue(gh, ref)
    if error or set(verified_labels) != set(after(current, delta)):
        return Sync(ref, stage, delta, error=error or "GitHub labels differ after update")
    if project:
        present, status, error = _project_item(gh, project, ref)
        if error or (wanted is not None and not present) or (present and status != wanted):
            return Sync(ref, stage, delta, error=error or "GitHub Project status differs after update")
    return Sync(ref, stage, delta, done=True, current_status=old_status,
                project_status=wanted, project_add=project_add)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--root", type=Path, required=True, help="workspace holding CLAUDE.md and the tracker")
    ap.add_argument("--date", help="tracker date, YYYY-MM-DD; defaults to today in the workspace timezone")
    ap.add_argument("--issue", help="one tracked issue reference, required with --commit")
    ap.add_argument("--task", help="with --issue and several rows on it: the tracker row's name or item number (#69)")
    ap.add_argument("--from-stage", help="stage last observed on the issue or project")
    ap.add_argument("--initialize", action="store_true", help="start an unlabeled issue or project item")
    ap.add_argument("--commit", action="store_true", help="apply one verified move; default is preview")
    args = ap.parse_args(argv)
    if args.commit and not args.issue:
        print("kanban: --commit requires --issue; bulk updates are preview only", file=sys.stderr)
        return 2
    if args.commit and not (args.from_stage or args.initialize):
        print("kanban: --commit requires --from-stage or --initialize", file=sys.stderr)
        return 2
    if args.from_stage and args.initialize:
        print("kanban: choose --from-stage or --initialize", file=sys.stderr)
        return 2
    if args.task and not args.issue:
        print("kanban: --task needs --issue; it names one row of that issue", file=sys.stderr)
        return 2
    try:
        cfg = read_config(args.root)
        settings = load_settings(args.root, cfg.settings_path)
        if settings.kanban is None:
            print("kanban: no [kanban] section in the workspace settings", file=sys.stderr)
            return 2
        day = args.date or datetime.now(cfg.zone).date().isoformat()
        tracker = parse_tracker((args.root / cfg.tracker_path(day)).read_text())
    except (OSError, ConfigError, SettingsError) as exc:
        print(f"kanban: {exc}", file=sys.stderr)
        return 2
    wanted = backlog.parse_issue_arg(args.issue, cfg.backlog) if args.issue else None
    if args.issue and wanted is None:
        print(f"kanban: invalid issue reference {args.issue!r}", file=sys.stderr)
        return 2
    selected = []
    for task in tracker.tasks:
        if task.standing or (task.kind == "done" and not wanted) or not task.lane.strip() or not task.issue.strip():
            continue
        ref = backlog.issue_ref(task.issue, cfg.backlog)
        if ref is None or (wanted and ref != wanted):
            continue
        selected.append((task, ref))
    if wanted and not selected:
        print(f"kanban: {args.issue} is not a tracked task with a lane", file=sys.stderr)
        return 2
    if args.task:
        # #69: a --task names one row of the issue, by the row's name or its tracker item number.
        picked = [(task, ref) for task, ref in selected
                  if task.name.strip() == args.task
                  or task.item.strip().lstrip("#") == args.task.strip().lstrip("#")]
        if not picked:
            print(f"kanban: no task named {args.task!r} on {args.issue}", file=sys.stderr)
            return 2
        selected = picked
    elif len({ref for _, ref in selected}) != len(selected):
        # #69: rows sharing an issue cannot be auto-picked; an issue carries one stage label.
        seen = set()
        for task, ref in selected:
            if ref in seen:
                names = ", ".join(f"{t.label!r} (item {t.item.strip()})" for t, r in selected if r == ref)
                print(f"kanban: multiple tracker tasks point to {ref.label(cfg.backlog)}: {names}; "
                      f"name one with --task", file=sys.stderr)
                return 2
            seen.add(ref)
    rows = []
    for task, ref in selected:
        stage = task.stage.strip()
        if not stage:
            result = Sync(ref, stage, error="tracker stage is blank")
        elif ref.host == backlog.GITHUB:
            result = sync_github(ref, settings.kanban, stage, commit=args.commit,
                                 expected_stage=args.from_stage, initialize=args.initialize)
        elif isinstance(cfg.backlog, backlog.Backlog):
            result = sync_gitlab(cfg.backlog, ref, settings.kanban, stage, commit=args.commit,
                                 expected_stage=args.from_stage, initialize=args.initialize)
        else:
            result = Sync(ref, stage, error="no configured GitLab host for this issue")
        changed = result.delta.changed or result.project_add or result.current_status != result.project_status
        rows.append({"issue": ref.label(cfg.backlog), "stage": stage,
                     "add": ", ".join(result.delta.add), "remove": ", ".join(result.delta.remove),
                     "project_status": result.project_status or "",
                     "action": ("error" if result.error else "unchanged" if not changed else
                                "updated" if result.done else "would update"),
                     "error": result.error})
    print(toon_encode(rows) if rows else "[]")
    return 1 if any(row["error"] for row in rows) else 0


if __name__ == "__main__":
    raise SystemExit(main())
