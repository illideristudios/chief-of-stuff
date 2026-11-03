"""The workspace's `## Coordinator` block in CLAUDE.md: the user, where the trackers, logs and pages are, the zone,
the deadlines and the forge. A script that needs to know where something is reads it here.

Precise deadlines can also be decided in a tracker's `## Decisions` table; `with_decision_deadlines` folds them in.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import tempfile
from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from backlog_ref import Backlog, BacklogError, GitHubBacklog, parse_backlog
from clock import DATE, HHMM
from md import BULLET, cells as _cells, is_separator as _is_separator, section as _section, unmark as _unmark, unquote as _unquote

DECISION_DEADLINE = re.compile(r"^deadline:\s*(.+?)\s+(?:(\d{4}-\d{2}-\d{2})[ T])?(\d{1,2}):(\d{2})$", re.I)

ENGINE_STATE_DIR = ".chief-of-stuff"
ENGINE_LOCK_FILE = "engine.lock"
ENGINE_STATUS_FILE = "engine-status.json"
ENGINE_STALE_FACTOR = 3.0
ENGINE_STALE_FLOOR_S = 180.0
# A pass can contain many bounded operations; this is an advisory, not a failure deadline.
ENGINE_LONG_PASS_S = 24 * 60 * 60


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class EngineStatus:
    """Recorded pass health and live lock state for an engine-backed workspace (#936)."""
    running: bool
    present: bool = False
    corrupt: bool = False
    mode: str = "service"
    interval: float = 60.0
    started_at: datetime | None = None
    last_pass_at: datetime | None = None
    pass_started_at: datetime | None = None
    last_attempt_at: datetime | None = None
    stopped_at: datetime | None = None
    last_error: str | None = None
    error_kind: str | None = None
    pid: int | None = None
    release: str | None = None


@dataclass(frozen=True)
class EngineHealth:
    """Classified engine health for page headers and warnings (#936)."""
    state: str
    meta: tuple[str, ...]
    warning: str | None = None


def lock_held(path: Path) -> bool:
    """True when an exclusive flock is held on `path`. Shared probe never blocks concurrent readers or creates files."""
    if not path.is_file():
        return False
    try:
        with path.open("r") as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_SH | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            fcntl.flock(handle, fcntl.LOCK_UN)
            return False
    except OSError:
        return False


def _parse_iso(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        dt = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    return dt if dt.tzinfo is not None else None


def write_engine_status(
    root: Path,
    *,
    started_at: datetime | None,
    last_pass_at: datetime | None,
    last_attempt_at: datetime | None,
    stopped_at: datetime | None,
    pass_started_at: datetime | None = None,
    interval: float = 60.0,
    mode: str = "service",
    last_error: str | None = None,
    error_kind: str | None = None,
    pid: int | None = None,
    release: str | None = None,
) -> None:
    """Atomically write `.chief-of-stuff/engine-status.json` while holding `engine.lock`."""
    state_dir = root.resolve() / ENGINE_STATE_DIR
    state_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "started_at": started_at.isoformat() if started_at is not None else None,
        "last_pass_at": last_pass_at.isoformat() if last_pass_at is not None else None,
        "pass_started_at": pass_started_at.isoformat() if pass_started_at is not None else None,
        "last_attempt_at": last_attempt_at.isoformat() if last_attempt_at is not None else None,
        "stopped_at": stopped_at.isoformat() if stopped_at is not None else None,
        "interval": float(interval),
        "mode": mode,
        "last_error": last_error,
        "error_kind": error_kind,
        "pid": os.getpid() if pid is None else pid,
        "release": release,
    }
    _swap_in(state_dir / ENGINE_STATUS_FILE, json.dumps(payload, indent=2, sort_keys=True) + "\n")


def read_engine_status(root: Path) -> EngineStatus | None:
    """Read engine status and live lock state, or return None for tracker-only workspaces without store/engine state."""
    resolved = root.resolve()
    state_dir = resolved / ENGINE_STATE_DIR
    running = lock_held(state_dir / ENGINE_LOCK_FILE)
    status_path = state_dir / ENGINE_STATUS_FILE
    has_status = status_path.is_file()
    has_store = (resolved / "store.db").is_file()
    if not running and not has_store and not has_status:
        return None
    if not has_status:
        return EngineStatus(running=running, present=False)
    try:
        data = json.loads(status_path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("status root must be an object")
        raw_interval = data.get("interval", 60.0)
        interval = float(raw_interval) if isinstance(raw_interval, (int, float)) and raw_interval > 0 else 60.0
    except (OSError, ValueError, TypeError):
        return EngineStatus(
            running=running,
            present=False,
            corrupt=True,
            last_error=f"{ENGINE_STATUS_FILE} is unreadable",
            error_kind="status",
        )
    last_error = data.get("last_error")
    error_kind = data.get("error_kind")
    mode = data.get("mode")
    pid = data.get("pid")
    release = data.get("release")
    return EngineStatus(
        running=running,
        present=True,
        corrupt=False,
        mode=mode if isinstance(mode, str) and mode else "service",
        interval=interval,
        started_at=_parse_iso(data.get("started_at")),
        last_pass_at=_parse_iso(data.get("last_pass_at")),
        pass_started_at=_parse_iso(data.get("pass_started_at")),
        last_attempt_at=_parse_iso(data.get("last_attempt_at")),
        stopped_at=_parse_iso(data.get("stopped_at")),
        last_error=last_error.strip() if isinstance(last_error, str) and last_error.strip() else None,
        error_kind=error_kind.strip() if isinstance(error_kind, str) and error_kind.strip() else None,
        pid=pid if isinstance(pid, int) else None,
        release=release if isinstance(release, str) and release else None,
    )


def _engine_stamp(at: datetime, now: datetime, zone: ZoneInfo) -> str:
    local_at = at.astimezone(zone)
    local_now = now.astimezone(zone)
    days = (local_now.date() - local_at.date()).days
    if days == 0:
        return f"{local_at:%H:%M}"
    if 0 < days < 7:
        return f"{local_at:%a %H:%M}"
    return f"{local_at:%Y-%m-%d %H:%M}"


def engine_health(status: EngineStatus, now: datetime, zone: ZoneInfo) -> EngineHealth:
    """Classify live engine status into header metadata and an actionable warning when degraded."""
    last_pass = _engine_stamp(status.last_pass_at, now, zone) if status.last_pass_at is not None else None
    pass_meta = f"last pass {last_pass}" if last_pass is not None else "last pass none"
    if not status.running:
        if status.corrupt:
            return EngineHealth(
                "unknown",
                ("engine stopped", "last pass unknown"),
                f"engine not running: {ENGINE_STATUS_FILE} is unreadable (last pass unknown)",
            )
        if not status.present:
            return EngineHealth(
                "missing",
                ("engine stopped", "last pass unknown"),
                "engine not running (last pass unknown)",
            )
        if status.last_error:
            pass_clause = f" (last pass {last_pass})" if last_pass is not None else ""
            if status.error_kind == "startup":
                at_clause = f" at {_engine_stamp(status.last_attempt_at, now, zone)}" if status.last_attempt_at else ""
                return EngineHealth(
                    "failed",
                    ("engine stopped", pass_meta),
                    f"engine failed to start{at_clause}{pass_clause}: {status.last_error}",
                )
            since_clause = f" since {_engine_stamp(status.stopped_at, now, zone)}" if status.stopped_at else ""
            return EngineHealth(
                "failed",
                ("engine stopped", pass_meta),
                f"engine not running{since_clause}{pass_clause}: {status.last_error}",
            )
        since_clause = f" since {_engine_stamp(status.stopped_at, now, zone)}" if status.stopped_at else ""
        pass_clause = f" (last pass {last_pass})" if last_pass is not None else " (no completed pass)"
        return EngineHealth(
            "stopped",
            ("engine stopped", pass_meta),
            f"engine not running{since_clause}{pass_clause}",
        )

    if status.corrupt:
        return EngineHealth(
            "unknown",
            ("engine running", "last pass unknown"),
            f"engine status unreadable: {ENGINE_STATUS_FILE} is unreadable (last pass unknown)",
        )
    if not status.present:
        return EngineHealth(
            "unknown",
            ("engine running", "last pass unknown"),
            "engine pass status unavailable (last pass unknown)",
        )
    if status.last_error:
        at_clause = f" at {_engine_stamp(status.last_attempt_at, now, zone)}" if status.last_attempt_at else ""
        pass_clause = f" (last pass {last_pass})" if last_pass is not None else ""
        long_pass = ""
        if status.pass_started_at is not None and (now - status.pass_started_at).total_seconds() > ENGINE_LONG_PASS_S:
            long_pass = f"; engine pass has been in progress since {_engine_stamp(status.pass_started_at, now, zone)}; it may be stuck"
        return EngineHealth(
            "failed",
            ("engine error", pass_meta),
            f"engine pass failed{at_clause}{pass_clause}: {status.last_error}{long_pass}",
        )
    if status.pass_started_at is not None:
        if (now - status.pass_started_at).total_seconds() > ENGINE_LONG_PASS_S:
            since = _engine_stamp(status.pass_started_at, now, zone)
            return EngineHealth("warning", ("engine running", "pass in progress", pass_meta),
                                f"engine pass has been in progress since {since}; it may be stuck")
        return EngineHealth("running", ("engine running", "pass in progress", pass_meta), None)
    stale_after = timedelta(seconds=max(float(status.interval or 60.0) * ENGINE_STALE_FACTOR, ENGINE_STALE_FLOOR_S))
    anchors = [t for t in (status.started_at, status.last_pass_at) if t is not None]
    anchor = max(anchors) if anchors else None
    if anchor is None or (now.astimezone(zone) - anchor.astimezone(zone)) > stale_after:
        if last_pass is not None:
            warn = f"engine stale: no completed pass since {last_pass}"
        elif status.started_at is not None:
            warn = f"engine stale: no completed pass since start at {_engine_stamp(status.started_at, now, zone)}"
        else:
            warn = "engine stale: no completed pass recorded"
        return EngineHealth("stale", ("engine stale", pass_meta), warn)
    if status.last_pass_at is None or (status.started_at is not None and status.last_pass_at < status.started_at):
        return EngineHealth("starting", ("engine starting", pass_meta), None)
    return EngineHealth("running", ("engine running", f"last pass {last_pass}"), None)


@dataclass(frozen=True)
class WorkspaceFile:
    """A file init leaves in a workspace and the command that adds it to one that lacks it; `command` takes `{root}`."""
    name: str
    command: str


# Init's config and settings, also read by health for older workspaces. Init needs user/forge inputs;
# --help explains those rather than suggesting an incomplete command that would fail.
# Health only exercises `settings`: a missing config is a usage fault there. Model guidance stays out: the engine rotates models (ledger D10), so preflight does not advise installing it.
WORKSPACE_FILES: dict[str, WorkspaceFile] = {
    "config": WorkspaceFile("CLAUDE.md", "chief-of-stuff init --help"),
    "settings": WorkspaceFile("chief-of-stuff.toml", "chief-of-stuff init --help"),
}


@dataclass(frozen=True)
class Deadline:
    name: str
    at: datetime
    requirements: str | None = None
    # "all": unnamed tasks fall to it (the default). "named": only tasks whose `due` names it — an exam
    # governs no task on the table, and a derived end toward it would make it look like a plan (finding 73).
    scope: str = "all"


@dataclass(frozen=True)
class Config:
    user: str
    log_dir: str
    tracker_template: str
    tz: str
    deadlines: tuple[Deadline, ...]
    board_tool: str | None
    board_url: str | None
    # The `Board:` line's ``dir `X` ``: where the board is written for pages.py to serve. None writes it
    # beside the tracker.
    pages_dir: str | None = None
    # The parsed `Backlog:` line, GitLab or GitHub. None when there is no line: the issue rule binds only
    # where there is a tracker to back a task with.
    backlog: Backlog | GitHubBacklog | None = None
    # `Settings:` — the workspace's chief-of-stuff.toml, relative to the root (settings.py reads it).
    settings_path: str | None = None
    # An optional workspace identity rule for dispatched workers.
    identity_policy: str | None = None

    @property
    def zone(self) -> ZoneInfo:
        return ZoneInfo(self.tz)

    def tracker_path(self, day: str) -> str:
        return self.tracker_template.replace("<date>", day)

    def log_path(self, day: str) -> str:
        return f"{self.log_dir.rstrip('/')}/{day}.md"


def _first_path(value: str) -> str | None:
    """The path a line names: its first backtick span, else its first word. What follows is prose."""
    m = re.search(r"`([^`]+)`", value)
    words = value.split()
    return m.group(1).strip() if m else (words[0] if words else None)


def _bullets(body: list[str]) -> tuple[dict[str, str], dict[str, list[tuple[str, str]]]]:
    """The block's top-level `- key: value` bullets, a repeated key's last line winning, and the bullets nested under each."""
    top: dict[str, str] = {}
    nested: dict[str, list[tuple[str, str]]] = {}
    current = ""
    for line in body:
        m = BULLET.match(line)
        if not m:
            continue
        indent, key, value = len(m.group(1)), _unquote(m.group(2)), m.group(3)
        if indent == 0:
            current = key
            top[key] = value
        else:
            nested.setdefault(current, []).append((key, value))
    return top, nested


def settings_path(text: str) -> str | None:
    """The path the block's `Settings:` line names, or None. Asks the block for no other line, so `health` can run on a block `parse_coordinator` refuses."""
    return _first_path(_bullets(_section(text, "## Coordinator"))[0].get("Settings", ""))


def architecture_document(root: Path) -> Path | None:
    """The canonical document's first backticked path, resolved against the workspace root."""
    try:
        text = (root / "CLAUDE.md").read_text()
    except FileNotFoundError:
        return None
    value = _bullets(_section(text, "## Architecture"))[0].get("Canonical document", "")
    match = re.search(r"`([^`]+)`", value)
    return (root / match[1].strip()).resolve() if match else None


def parse_coordinator(text: str, today: date) -> Config:
    body = _section(text, "## Coordinator")
    if not body:
        raise ConfigError("no `## Coordinator` block in CLAUDE.md")
    top, nested = _bullets(body)
    for needed in ("User", "Daily log dir", "Tracker", "Timezone"):
        if needed not in top:
            raise ConfigError(f"`## Coordinator` block has no `{needed}:` line")
    tz = _unquote(top["Timezone"])
    zone = ZoneInfo(tz)
    identity_policy = _unquote(top.get("Identity", "")).lower() or None
    if identity_policy not in (None, "user-name-only"):
        raise ConfigError("`Identity:` must be `user-name-only` when present")
    deadlines = []
    for name, value in nested.get("Deadlines", []):
        when, *options = [part.strip() for part in value.split(";")]
        m = DATE.match(_unquote(when))
        if m:
            reqs = next((r.group(1) for o in options if (r := re.match(r"(?i)requirements\s+`?([^`]+?)`?\s*$", o))), None)
            scope = "named" if any(re.match(r"(?i)named tasks only\s*$", o) for o in options) else "all"
            deadlines.append(Deadline(name, datetime(int(m[1]), int(m[2]), int(m[3]), int(m[4] or 23), int(m[5] or 59), tzinfo=zone), reqs, scope))
    tracker = re.search(r"`([^`]+)`", top["Tracker"])
    board_tool = board_url = pages_dir = None
    if "Board" in top:
        parts = [p.strip() for p in top["Board"].split(";")]
        board_tool = _unquote(parts[0]) or None
        for p in parts[1:]:
            m = re.match(r"(?i)url\s+(\S+)", p)
            if m:
                board_url = m.group(1)
            m = re.match(r"(?i)dir\s+`([^`]+)`", p)
            if m:
                pages_dir = m.group(1)
    backlog = None
    if "Backlog" in top:
        try:
            backlog = parse_backlog(top["Backlog"])
        except BacklogError as e:
            raise ConfigError(f"`Backlog:` line: {e}") from None
    return Config(
        user=_unquote(top["User"]),
        log_dir=_unquote(top["Daily log dir"]),
        tracker_template=tracker.group(1) if tracker else _unquote(top["Tracker"]),
        tz=tz,
        deadlines=tuple(sorted(deadlines, key=lambda d: d.at)),
        board_tool=board_tool,
        board_url=board_url,
        pages_dir=pages_dir,
        backlog=backlog,
        settings_path=settings_path(text),
        identity_policy=identity_policy,
    )


def read_config(root: Path) -> Config:
    """`root`'s `## Coordinator` block, read from its CLAUDE.md. Raises OSError or ConfigError."""
    return parse_coordinator((root / "CLAUDE.md").read_text(), today=date.today())


def pages_address(root: Path) -> tuple[Path, int]:
    """(pages dir, port) from the block's Board line, where pages.py serves. Raises OSError or ConfigError."""
    cfg = read_config(root)
    port = urlsplit(cfg.board_url or "").port
    if not cfg.pages_dir or not port:
        raise ConfigError("the Board: line names no `URL http://127.0.0.1:<port>/` and `dir` to serve")
    return root / cfg.pages_dir, port


# A person's loads of the board (#229), in the pages dir; dotfiles, which the page server never serves.
LAST_LOOKED, LOOKED_BEFORE = ".last-looked", ".looked-before"


def _swap_in(path: Path, text: str) -> None:
    """Atomically, through a temporary file of its own: the threaded page server may record two looks at once."""
    with tempfile.NamedTemporaryFile("w", dir=path.parent, prefix=f"{path.name}-", delete=False) as out:
        out.write(text)
    os.replace(out.name, path)


def record_look(pages_dir: Path, now: datetime) -> dict[str, str | None]:
    """A person loaded the board at `now` (the server's clock): the look before it becomes `.looked-before`, freshly
    written so the board, whose source it is, re-renders from it. Returns both files as they were, for `unrecord_look`."""
    prior = {}
    for name in (LAST_LOOKED, LOOKED_BEFORE):
        try:
            prior[name] = (pages_dir / name).read_text()
        except FileNotFoundError:
            prior[name] = None  # a first look: nothing before it
    if prior[LAST_LOOKED] is not None:
        _swap_in(pages_dir / LOOKED_BEFORE, prior[LAST_LOOKED])
    _swap_in(pages_dir / LAST_LOOKED, now.isoformat())
    return prior


def unrecord_look(pages_dir: Path, prior: dict[str, str | None]) -> None:
    """Put both files back as `record_look` found them: the board it was for never showed."""
    for name, text in prior.items():
        if text is None:
            (pages_dir / name).unlink(missing_ok=True)
        else:
            _swap_in(pages_dir / name, text)


def looked_before(pages_dir: Path) -> datetime | None:
    """When a person last looked at the board before the latest look, or None."""
    try:
        return datetime.fromisoformat((pages_dir / LOOKED_BEFORE).read_text().strip())
    except (OSError, ValueError):
        return None


def worktrees_dir(claude_md: str) -> str:
    """The `Worktrees:` line of the `## Coordinator` block, or "" when there is none (trees sit at the root)."""
    for line in _section(claude_md, "## Coordinator"):
        m = BULLET.match(line)
        if m and _unquote(m.group(2)).lower() == "worktrees":
            return _unquote(m.group(3))
    return ""


def _decision_deadline(cells: list[str], item_col: int, tracker_day: date,
                       zone: ZoneInfo) -> tuple[str, datetime, list[str]] | None:
    """A Decisions row's `deadline:` item as (name, instant, option parts), or None when the row is not a valid one."""
    if _is_separator(cells) or item_col >= len(cells):
        return None
    item, *options = [part.strip() for part in _unmark(cells[item_col]).split(";")]
    match = DECISION_DEADLINE.fullmatch(item)
    if not match:
        return None
    name, day_text, hour_text, minute_text = match.groups()
    name = name.strip(" ,—–-")
    if not name:
        return None
    try:
        day = date.fromisoformat(day_text) if day_text else tracker_day
        at = datetime.combine(day, time(int(hour_text), int(minute_text)), tzinfo=zone)
    except ValueError:
        return None
    return name, at, options


def _recorded_time(cells: list[str], time_col: int | None) -> time | None:
    """The time a Decisions row was recorded, or None when it has no readable one."""
    if time_col is None or time_col >= len(cells):
        return None
    stamp = HHMM.fullmatch(cells[time_col])
    if not stamp:
        return None
    try:
        return time(int(stamp[1]), int(stamp[2]))
    except ValueError:
        return None


def _decision_terms(options: list[str], old: Deadline | None) -> tuple[str | None, str]:
    """The requirements path and task scope a decision names, each defaulting to the configured deadline's."""
    requirements = next((r.group(1) for option in options
                         if (r := re.fullmatch(r"(?i)requirements\s+`?([^`]+?)`?", option))),
                        old.requirements if old else None)
    scope = ("named" if any(re.fullmatch(r"(?i)named tasks only", option) for option in options)
             else old.scope if old else "all")
    return requirements, scope


def with_decision_deadlines(cfg: Config, tracker_text: str, tracker_day: date,
                            *, min_day: date | None = None) -> Config:
    """Project precise Decisions-row deadlines into the board's configured deadline list.

    A time without a date belongs to the tracker day, even when an older board is rendered later.
    If a name is decided more than once, the decision recorded at the latest time wins. A decision
    that gives no exact time cannot supply an instant and leaves the existing configuration alone.
    """
    rows = [line for line in _section(tracker_text, "## Decisions") if line.strip().startswith("|")]
    if not rows:
        return cfg
    header = [cell.lower() for cell in _cells(rows[0])]
    if "item" not in header:
        return cfg
    item_col = header.index("item")
    time_col = header.index("time") if "time" in header else None
    base = {deadline.name.casefold(): deadline for deadline in cfg.deadlines}
    decided: dict[str, tuple[time | None, Deadline]] = {}
    for row in rows[1:]:
        cells = _cells(row)
        parsed = _decision_deadline(cells, item_col, tracker_day, cfg.zone)
        if parsed is None:
            continue
        name, at, options = parsed
        if min_day is not None and at.date() < min_day:
            continue
        key = name.casefold()
        old = base.get(key)
        requirements, scope = _decision_terms(options, old)
        recorded = _recorded_time(cells, time_col)
        previous = decided.get(key)
        if previous is None or (recorded is not None and (previous[0] is None or recorded > previous[0])):
            decided[key] = (recorded, Deadline(old.name if old else name, at, requirements, scope))
    if not decided:
        return cfg
    base.update({name: deadline for name, (_, deadline) in decided.items()})
    return replace(cfg, deadlines=tuple(sorted(base.values(), key=lambda deadline: deadline.at)))


def daily_trackers(root: Path, cfg: Config) -> list[tuple[date, Path]]:
    """Every day's tracker in the workspace, oldest first."""
    template = cfg.tracker_template
    if template.count("<date>") != 1:
        return []
    prefix, suffix = template.split("<date>")
    found = []
    for path in root.glob(f"{prefix}*{suffix}"):
        if not path.is_file() or not path.resolve().is_relative_to(root.resolve()):
            continue
        relative = path.relative_to(root).as_posix()
        if not relative.startswith(prefix) or not relative.endswith(suffix):
            continue
        token = relative[len(prefix):len(relative) - len(suffix) if suffix else None]
        try:
            day = date.fromisoformat(token)
        except ValueError:
            continue
        found.append((day, path))
    return sorted(found)


def with_workspace_decision_deadlines(root: Path, cfg: Config, tracker_text: str, tracker_day: date) -> Config:
    """Carry future deadlines forward from earlier daily trackers, then apply today's decisions."""
    for day, path in daily_trackers(root, cfg):
        if day < tracker_day:
            cfg = with_decision_deadlines(cfg, path.read_text(), day, min_day=tracker_day)
    return with_decision_deadlines(cfg, tracker_text, tracker_day)
