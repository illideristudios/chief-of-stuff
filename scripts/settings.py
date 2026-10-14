#!/usr/bin/env python3
"""The workspace's chief-of-stuff settings: a TOML file the `## Coordinator` block names with `Settings:`.

Zach, 2026-09-22 22:20, on the day open and close times: "Configurable. We're going to need to pull
settings into a config file. Default to 6 and 22". Notification, lane, budget, and Kanban settings
live here, and so do the suggested models and their rotations (#111).

No `Settings:` line, no file, or no `[notify]` section is notify off, and nothing else changes. A value
that is there and cannot be read is refused: a guessed day-open time is a notification at the wrong hour.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path

import policy
import runtimes
from runtimes import NAMES as RUNTIMES

ADAPTERS = ("md-notify", "off")
HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
OFFSET = re.compile(r"^(\d+)([hm])$")
WORKER_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
HERDR_NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,31}")
CLAUDE_AS_PROFILE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_-]*")  # claude-as's valid_profile_name
DEFAULT_WARNINGS = ("24h", "3h", "1h")
POLICY = Path(__file__).resolve().parent.parent / "assets" / "policy.toml"  # the shipped policy and workflow
EFFORTS = ("high", "medium", "low")


class SettingsError(ValueError):
    """A settings file that is there and cannot be read as written."""


def _offset(label: str, where: str = "[notify] deadline_warnings") -> timedelta:
    m = OFFSET.match(label) if isinstance(label, str) else None
    if not m:
        raise SettingsError(f"{where}: {label!r} is not Nh or Nm")
    n = int(m.group(1))
    return timedelta(hours=n) if m.group(2) == "h" else timedelta(minutes=n)


@dataclass(frozen=True)
class Notify:
    adapter: str = "off"
    queue: str = "notifications/NOTIFICATIONS.md"
    day_open: str = "06:00"
    day_close: str = "22:00"
    warnings: tuple[tuple[str, timedelta], ...] = tuple((w, _offset(w)) for w in DEFAULT_WARNINGS)

    def queue_path(self, root: Path) -> Path:
        return (Path(root) / self.queue).resolve()


@dataclass(frozen=True)
class Lane:
    """A lane is the sequence of stages a task moves through (Zach, 2026-09-23, #33). A gate is a stage only
    the user's word passes."""
    stages: tuple[str, ...]
    gates: tuple[str, ...] = ()


@dataclass(frozen=True)
class GitHubProject:
    owner: str
    number: int
    field: str = "Status"
    options: tuple[str, ...] = ()


@dataclass(frozen=True)
class Workflow:
    architecture_reviewer: str | None = None
    reviewer_session: str | None = None
    delivery: str = "pull-request"
    merge_owner: str = "user"
    approver: str | None = None


@dataclass(frozen=True)
class Workers:
    launcher: str = "ghostty"
    mode: str = "interactive"
    # #100: one-shots running at once across every session in the workspace; None is no cap.
    max_concurrency: int | None = None
    herdr_session: str = "chief-of-stuff"
    claude_as_profile: str = ""  # a claude-as profile Claude workers run under; empty is a bare `claude`


@dataclass(frozen=True)
class Pages:
    # The user, 2026-09-26: "allow the board server to have up to 4 workers", then "well, N workers. 4 default".
    workers: int = 4  # pages the pages server renders at once


@dataclass(frozen=True)
class Kanban:
    """One visible board stage per tracker stage, with an independent human hold."""
    stages: tuple[str, ...]
    human_review_label: str
    stage_map: dict[str, int]
    hold_stages: tuple[str, ...] = ()
    terminal_stages: tuple[str, ...] = ()
    github_project: GitHubProject | None = None

    def label_for(self, stage: str) -> str | None:
        # Terminal stages leave a mark named like the stage so a rebuild can round-trip (#627).
        if stage in self.terminal_stages:
            return stage
        return self.stages[self.stage_map[stage]]

    def status_for(self, stage: str) -> str | None:
        """GitHub Project Status option for a tracker stage; None when no project is configured."""
        if self.github_project is None:
            return None
        if stage in self.terminal_stages:
            return stage
        return self.github_project.options[self.stage_map[stage]]

    def stage_for_label(self, labels: tuple[str, ...]) -> str:
        """The tracker stage those labels encode. A mark shared by stages reads as the first of them; the engine keeps its
        stored stage when it shares that mark (#651). Marks for two different columns are refused."""
        names = {label.casefold() for label in labels}
        hits = [s for s in (*self.stage_map, *self.terminal_stages) if self.label_for(s).casefold() in names]
        if not hits:
            return ""
        if len({self.label_for(s).casefold() for s in hits}) > 1:
            raise ValueError(f"labels map to more than one stage: {', '.join(hits)}")
        return hits[0]

    def stage_for_status(self, option: str | None) -> str:
        """The tracker stage a Status option encodes; an unknown option is refused."""
        if not option:
            return ""
        hits = [s for s in (*self.stage_map, *self.terminal_stages) if self.status_for(s) == option]
        if not hits:
            raise ValueError(f"unknown project Status option: {option}")
        return hits[0]  # a shared option reads as its first stage, as stage_for_label does

    def holds(self, stage: str) -> bool:
        return stage in self.hold_stages


@dataclass(frozen=True)
class ModelEntry:
    """#111: one `runtime:model-id@effort` entry of a `[models.<class>]` rotation; the ID is verbatim."""
    runtime: str
    model: str
    effort: str | None = None

    def __str__(self) -> str:
        return f"{self.runtime}:{self.model}" + (f"@{self.effort}" if self.effort else "")


@dataclass(frozen=True)
class Graph:
    """The decision page's MODULE GRAPH: the clone branch-graph reads (under the workspace root), its import root,
    module depth, the rules file inside the clone, the module globs it leaves out, and the most modules one view holds."""
    clone: str
    root: str
    depth: int = 2
    rules: str | None = None
    exclude: tuple[str, ...] = ("tests.*", "evals.*")
    max_nodes: int = 12


@dataclass(frozen=True)
class Settings:
    notify: Notify = field(default_factory=Notify)
    lanes: dict[str, Lane] = field(default_factory=dict)
    # #33 stage 3: how long a running task of each size goes before the coordinator polls its session. XL has none.
    budgets: dict[str, timedelta] = field(default_factory=dict)
    kanban: Kanban | None = None
    workflow: Workflow = field(default_factory=Workflow)
    workers: Workers = field(default_factory=Workers)
    pages: Pages = field(default_factory=Pages)
    # #111: task class -> rotation; the first entry is the suggested model, the rest the fallback order.
    models: dict[str, tuple[ModelEntry, ...]] = field(default_factory=dict)
    graph: Graph | None = None
    # #54: repository name -> path (relative to the workspace root) that `chief-of-stuff worktree --clone <name>` cuts from.
    repos: dict[str, str] = field(default_factory=dict)


def _entry(where: str, text) -> ModelEntry:
    runtime, sep, rest = text.partition(":") if isinstance(text, str) else ("", "", "")
    if not sep or runtime not in RUNTIMES:
        raise SettingsError(f"{where}: {text!r} is not runtime:model-id with runtime one of {', '.join(RUNTIMES)}")
    model, at, effort = rest.rpartition("@") if "@" in rest else (rest, "", None)
    if at and effort not in EFFORTS:
        raise SettingsError(f"{where}: {text!r} effort must be one of {', '.join(EFFORTS)}")
    if at and not runtimes.get(runtime).takes_effort:
        raise SettingsError(f"{where}: {text!r} {runtime} takes no effort; drop @{effort}")
    if not model.strip():
        raise SettingsError(f"{where}: {text!r} has no model ID")
    return ModelEntry(runtime, model, effort)


def _models(table) -> dict[str, tuple[ModelEntry, ...]]:
    if not isinstance(table, dict):
        raise SettingsError("[models] must be a table of task classes")
    models = {}
    for name, t in table.items():
        where = f"[models.{name}]"
        if not isinstance(t, dict) or set(t) != {"rotation"}:
            raise SettingsError(f"{where}: a table with only rotation, like {{ rotation = [\"claude:opus@high\"] }}")
        rotation = t["rotation"]
        if not isinstance(rotation, list) or not rotation:
            raise SettingsError(f"{where}: rotation is a non-empty list of runtime:model-id@effort entries")
        entries = tuple(_entry(where, x) for x in rotation)
        if len(set(entries)) != len(entries):
            raise SettingsError(f"{where}: an entry appears twice")
        models[name] = entries
    return models


def pick(rotation: tuple[ModelEntry, ...], after: str | None = None, not_family: str | None = None) -> ModelEntry:
    """The suggested entry, or the next one after `after` (with or without its @effort); `not_family` skips a runtime."""
    start = 0
    if after:
        hits = [i for i, e in enumerate(rotation) if after in (str(e), f"{e.runtime}:{e.model}")]
        if not hits:
            raise SettingsError(f"{after} is not in the rotation")
        start = hits[0] + 1
    for entry in rotation[start:]:
        if entry.runtime != not_family:
            return entry
    raise SettingsError("rotation exhausted: no entry left" + (f" after {after}" if after else "") +
                        (f" outside {not_family}" if not_family else ""))


def _workers(table) -> Workers:
    if not isinstance(table, dict):
        raise SettingsError("[workers] must be a table")
    launcher = table.get("launcher", "ghostty")
    if launcher not in ("ghostty", "tmux", "herdr"):
        raise SettingsError("[workers] launcher must be `ghostty`, `tmux` or `herdr`")
    session = table.get("herdr_session", Workers.herdr_session)
    if not isinstance(session, str) or not HERDR_NAME.fullmatch(session):
        raise SettingsError("[workers] herdr_session must match [a-z0-9][a-z0-9_-]{0,31}")
    mode = table.get("mode", "interactive")
    if mode not in ("interactive", "one-shot"):
        raise SettingsError("[workers] mode must be `interactive` or `one-shot`")
    cap = table.get("max_concurrency")
    if cap is not None and (type(cap) is not int or cap < 1):
        raise SettingsError("[workers] max_concurrency must be a positive whole number")
    if "claude_profile" in table:
        raise SettingsError("[workers] claude_profile is renamed claude_as_profile")
    profile = table.get("claude_as_profile", "")
    if "claude_as_profile" in table and not (isinstance(profile, str) and CLAUDE_AS_PROFILE.fullmatch(profile)):
        raise SettingsError("[workers] claude_as_profile must be a claude-as profile name: letters, digits, '_' and '-', "
                            "not starting with '-'")
    return Workers(launcher=launcher, mode=mode, max_concurrency=cap, herdr_session=session,
                   claude_as_profile=profile)


def _repos(table) -> dict[str, str]:
    if not isinstance(table, dict):
        raise SettingsError("[repos] must be a table")
    for name, path in table.items():
        if not isinstance(path, str) or not path.strip():
            raise SettingsError(f"[repos] {name} must be a path")
    return dict(table)


def _pages(table) -> Pages:
    if not isinstance(table, dict):
        raise SettingsError("[pages] must be a table")
    n = table.get("workers", Pages.workers)
    if type(n) is not int or n < 1:
        raise SettingsError("[pages] workers must be a positive whole number")
    return Pages(n)


def _graph(table) -> Graph:
    if not isinstance(table, dict):
        raise SettingsError("[graph] must be a table")
    for key in ("clone", "root"):
        if not isinstance(table.get(key), str) or not table[key].strip():
            raise SettingsError(f"[graph] {key} must be a path")
    depth = table.get("depth", 2)
    if type(depth) is not int or depth < 1:
        raise SettingsError("[graph] depth must be a positive whole number")
    rules = table.get("rules")
    if rules is not None and (not isinstance(rules, str) or not rules.strip()):
        raise SettingsError("[graph] rules must be a path inside the clone")
    exclude = table.get("exclude", Graph.exclude)
    if not isinstance(exclude, (list, tuple)) or not all(isinstance(x, str) and x.strip() for x in exclude):
        raise SettingsError("[graph] exclude must be a list of module globs")
    cap = table.get("max_nodes", Graph.max_nodes)
    if type(cap) is not int or cap < 3:
        raise SettingsError("[graph] max_nodes must be a whole number of at least 3")
    return Graph(table["clone"], table["root"], depth, rules, tuple(exclude), cap)


def _workflow(table) -> Workflow:
    if not isinstance(table, dict):
        raise SettingsError("[workflow] must be a table")
    names = {}
    for key in ("architecture_reviewer", "reviewer_session"):
        name = table.get(key)
        if name is not None and (not isinstance(name, str) or not WORKER_NAME.fullmatch(name)):
            raise SettingsError(f"[workflow] {key} must be a session name")
        names[key] = name
    delivery = table.get("delivery", "pull-request")
    if delivery not in ("pull-request", "branch"):
        raise SettingsError("[workflow] delivery must be `pull-request` or `branch`")
    merge_owner = table.get("merge_owner", "user")
    if merge_owner not in ("user", "worker", "approval"):
        raise SettingsError("[workflow] merge_owner must be `user`, `worker` or `approval`")
    if delivery == "branch" and merge_owner != "user":
        raise SettingsError(f"[workflow] merge_owner={merge_owner} requires delivery=pull-request")
    # #97: the forge username whose approval the coordinator merges on; nobody else's counts.
    approver = table.get("approver")
    if merge_owner == "approval" and (not isinstance(approver, str) or not approver.strip()):
        raise SettingsError("[workflow] merge_owner=approval needs approver, the user's forge username")
    return Workflow(**names, delivery=delivery, merge_owner=merge_owner, approver=approver)


def _lane(name: str, table) -> Lane:
    if not isinstance(table, dict):
        raise SettingsError(f"[lanes] {name}: a table, like {{ stages = [\"implement\", \"pr\"] }}")
    stages, gates = table.get("stages"), table.get("gates", [])
    if not (isinstance(stages, list) and stages and all(isinstance(x, str) and x for x in stages)):
        raise SettingsError(f"[lanes] {name}: stages is a non-empty list of names")
    if len(set(stages)) != len(stages):
        raise SettingsError(f"[lanes] {name}: a stage appears twice")
    if not isinstance(gates, list) or not all(isinstance(g, str) for g in gates) or not set(gates) <= set(stages):
        raise SettingsError(f"[lanes] {name}: gates must be stages of the lane")
    return Lane(tuple(stages), tuple(gates))


def _github_project(table, stages: list[str], terminal: list[str]) -> GitHubProject:
    if not isinstance(table, dict):
        raise SettingsError("[kanban.github_project] must be a table")
    owner, number, field_name = table.get("owner"), table.get("number"), table.get("field", "Status")
    options = table.get("options", stages)
    if not isinstance(owner, str) or not owner.strip() or type(number) is not int or number <= 0:
        raise SettingsError("[kanban.github_project] needs an owner and positive project number")
    if not isinstance(field_name, str) or not field_name.strip():
        raise SettingsError("[kanban.github_project] field must be a name")
    if (not isinstance(options, list) or len(options) != len(stages)
            or any(not isinstance(x, str) or not x.strip() for x in options)
            or len(set(options)) != len(options)):
        raise SettingsError("[kanban.github_project] options must match stages in order and be unique")
    if set(terminal) & set(options):
        raise SettingsError("[kanban.github_project] options must not collide with terminal_stages")
    return GitHubProject(owner, number, field_name, tuple(options))


def _project_ref(value) -> dict:
    """`owner/number` shorthand for [kanban.github_project]."""
    if not isinstance(value, str) or "/" not in value:
        raise SettingsError('[kanban] project must be "owner/number"')
    owner, _, number = value.partition("/")
    if not owner.strip() or not number.isdigit() or int(number) <= 0:
        raise SettingsError('[kanban] project must be "owner/number"')
    return {"owner": owner.strip(), "number": int(number)}


def _kanban(table) -> Kanban:
    if not isinstance(table, dict):
        raise SettingsError("[kanban] must be a table")
    stages = table.get("stages")
    if not isinstance(stages, list) or not stages or any(not isinstance(x, str) or not x.strip() for x in stages):
        raise SettingsError("[kanban] stages must be a non-empty list of labels")
    fold = {label.casefold() for label in stages}  # forges compare label names case-insensitively
    if len(stages) != len(fold) or any("," in label for label in stages):
        raise SettingsError("[kanban] stages must be unique")
    human = table.get("human_review_label")
    if not isinstance(human, str) or not human.strip() or human.casefold() in fold or "," in human:
        raise SettingsError("[kanban] human_review_label must be distinct from stage labels")
    mapping = table.get("map")
    if not isinstance(mapping, dict) or not mapping:
        raise SettingsError("[kanban.map] must map tracker stages to label indexes")
    for name, index in mapping.items():
        if not name.strip() or type(index) is not int or not 0 <= index < len(stages):
            raise SettingsError(f"[kanban.map] {name!r} must be a stage index in 0..{len(stages)-1}")
    holds, terminal = table.get("hold_stages", []), table.get("terminal_stages", [])
    if not isinstance(holds, list) or not all(isinstance(x, str) for x in holds) or not set(holds) <= set(mapping):
        raise SettingsError("[kanban] hold_stages must be mapped stages")
    if (not isinstance(terminal, list) or not all(isinstance(x, str) and x.strip() for x in terminal)
            or len(terminal) != len({x.casefold() for x in terminal}) or any("," in x for x in terminal)
            or human.casefold() in {x.casefold() for x in terminal} or set(terminal) & set(mapping)
            or {x.casefold() for x in terminal} & fold):
        raise SettingsError("[kanban] terminal_stages must be unique, comma-free labels distinct from "
                            "human_review_label, mapped stages and board labels")
    project = None
    if "github_project" in table and "project" in table:
        raise SettingsError("[kanban] set github_project or project, not both")
    if "github_project" in table:
        project = _github_project(table["github_project"], stages, terminal)
    elif "project" in table:
        project = _github_project(_project_ref(table["project"]), stages, terminal)
    return Kanban(tuple(stages), human, mapping, tuple(holds), tuple(terminal), project)


def _notify(table: dict, root: Path) -> Notify:
    got = Notify()
    adapter = table.get("adapter", got.adapter)
    if adapter not in ADAPTERS:
        raise SettingsError(f"[notify] adapter: {adapter!r} is not one of {', '.join(ADAPTERS)}")
    times = {}
    for key in ("day_open", "day_close"):
        value = table.get(key, getattr(got, key))
        if not isinstance(value, str) or not HHMM.match(value):
            raise SettingsError(f"[notify] {key}: {value!r} is not HH:MM")
        times[key] = value
    labels = table.get("deadline_warnings", list(DEFAULT_WARNINGS))
    if not isinstance(labels, list):
        raise SettingsError("[notify] deadline_warnings: a list, like [\"24h\", \"3h\"]")
    queue = table.get("queue", got.queue)
    if not isinstance(queue, str) or not queue.strip() or "\0" in queue:  # a NUL makes resolve() raise ValueError
        raise SettingsError("[notify] queue: a path under the workspace root")
    resolved = (Path(root) / queue).resolve()
    if not resolved.is_relative_to(Path(root).resolve()):
        raise SettingsError(f"[notify] queue: {queue!r} is outside the workspace root")
    return Notify(adapter=adapter, queue=queue, warnings=tuple((w, _offset(w)) for w in labels), **times)


def load(root: Path, settings_path: str | None) -> Settings:
    """`settings_path` is the `Settings:` line's value, relative to `root`, or None when there is none.

    An absent file is the defaults. A path that is there but not a readable UTF-8 file (a directory,
    unreadable, not UTF-8) is a refusal — never silent defaults (#347, #285).
    """
    if not settings_path:
        return Settings()
    path = Path(root) / settings_path
    try:
        path.stat()
    except (FileNotFoundError, NotADirectoryError):
        return Settings()
    except OSError as e:
        raise SettingsError(f"{settings_path}: {e.strerror or e}") from None
    if not path.is_file():
        raise SettingsError(f"{settings_path}: not a file")
    try:
        data = tomllib.loads(path.read_text())
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as e:
        raise SettingsError(f"{settings_path}: {e}") from None
    except OSError as e:
        raise SettingsError(f"{settings_path}: {e.strerror or e}") from None
    lanes = data.get("lanes", {})
    if not isinstance(lanes, dict):
        raise SettingsError(f"{settings_path}: [lanes] is not a table")
    lanes = {name: _lane(name, t) for name, t in lanes.items()}
    kanban = _kanban(data["kanban"]) if "kanban" in data else None
    workflow = _workflow(data["workflow"]) if "workflow" in data else Workflow()
    workers = _workers(data["workers"]) if "workers" in data else Workers()
    models = _models(data["models"]) if "models" in data else {}
    graph = _graph(data["graph"]) if "graph" in data else None
    pages = _pages(data["pages"]) if "pages" in data else Pages()
    repos = _repos(data["repos"]) if "repos" in data else {}
    budgets = data.get("budgets", {})
    if not isinstance(budgets, dict) or not set(budgets) <= {"S", "M", "L", "XL"}:
        raise SettingsError(f"{settings_path}: [budgets] is a table of S, M, L, XL")
    budgets = {size: _offset(v, f"[budgets] {size}") for size, v in budgets.items()}
    table = data.get("notify")
    if table is None:
        return Settings(lanes=lanes, budgets=budgets, kanban=kanban, workflow=workflow, workers=workers, pages=pages, models=models, graph=graph, repos=repos)
    if not isinstance(table, dict):
        raise SettingsError(f"{settings_path}: [notify] is not a table")
    return Settings(notify=_notify(table, Path(root)), lanes=lanes, budgets=budgets,
                    kanban=kanban, workflow=workflow, workers=workers, pages=pages, models=models, graph=graph, repos=repos)


def load_policy(path: Path = POLICY) -> policy.Policies:
    """The policy and workflow file (design decisions 12 and 13), the shipped one by default."""
    try:
        return policy.parse(tomllib.loads(Path(path).read_text()))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as e:
        raise policy.PolicyError(f"{path}: {e}") from None
