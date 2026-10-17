"""Eval runner for the chief-of-stuff agent.

`claude plugin eval` cannot run a case under `--agent`, so the default backend drives
`claude -p` directly. `--runtime` selects a headless Codex, Cursor, or Antigravity CLI instead.
Grader vocabulary follows `plugin eval` where it overlaps (`tool_used`, `regex`) so cases can
be ported later. A case's `"runtimes_present": ["claude"]` makes those the only runtimes the login shell finds.

    python3 evals/run.py --arm baseline --case stale-clock        # Claude defaults to sonnet
    python3 evals/run.py --runtime codex --arm agent --model gpt-6-astra --effort high --case stale-clock
"""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
import re
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import queue
import tempfile
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from datetime import datetime, timedelta
from datetime import time as dtime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))
import make_repo  # noqa: E402
# Package-style, as start_coordinator imports it, so `host_prompt` and this module share one copy.
sys.path.insert(1, str(Path(__file__).resolve().parent.parent))
from scripts import runtimes  # noqa: E402

EVALS = Path(__file__).resolve().parent
PLUGIN_ROOT = EVALS.parent
AGENT = "chief-of-stuff"
# Zach, 2026-09-19: "also make the case runner use sonnet". This is the HARNESS default only --
# `--model` still overrides it, and the agent frontmatter keeps `model: opus` for the live run, so
# what ships is unchanged. Measured the day it moved: sonnet $0.15/run against opus $0.33, and the
# `--model` flag beats the frontmatter, so `check_arm` sees sonnet and passes rather than failing
# the family check. Supersedes the 2026-09-16 "we don't use Sonnet for this" for the runner.
DEFAULT_MODEL = "sonnet"

# Isolation: `--setting-sources project` drops user settings (and with them user plugins and
# permission rules) but still loads the fixture CLAUDE.md. `--restricted` was probed and skips
# project CLAUDE.md, which is the config channel under test, so it is not used.
TOOLS = ["Bash", "Read", "Glob", "Grep", "Write", "Edit", "Agent"]
# `mv`/`mkdir` are for filing moves (PARA); no `cp` or `rm`, so a "move" cannot become a copy or a delete.
BOARD_RENDERER = PLUGIN_ROOT / "scripts" / "render_board.py"
HEALTH_PROBE = PLUGIN_ROOT / "scripts" / "probe_health.py"
TASK_AUDIT = PLUGIN_ROOT / "scripts" / "audit_tasks.py"
MAKE_WORKTREE = PLUGIN_ROOT / "scripts" / "make_worktree.py"
SPAWN_SESSION = PLUGIN_ROOT / "scripts" / "spawn_session.py"
# The plugin's own scripts are the only ones the agent may run: html, health and merge state come
# from code, never from the agent. git and curl stay off the allowlist — the scripts call them.
SCRIPTS = ("render_board.py", "pages.py", "probe_health.py", "audit_tasks.py", "make_worktree.py",
           "spawn_session.py", "notify.py", "inbox.py", "backlog.py", "kanban_cli.py", "process_status.py",
           "decision_page.py", "tracker_write.py", "merge_approved.py", "merge_ready.py", "review_threads.py", "install_model_guidance.py",
           "board_sources.py", "tracker_read.py", "one_shot_result.py", "flow.py")
OPERATIONS = ("board", "pages", "health", "audit", "worktree", "worker", "notify", "inbox",
              "backlog", "kanban", "processes", "decision", "log", "merge-approved", "merge-ready", "review-threads", "models", "sources",
              "tracker", "result", "tick")


def allowed_tools(root: Path) -> list[str]:
    """The allowlist, rooted. Finding 114: these were absolute paths into the live checkout, so a run
    read whatever was on disk when each case reached it and a tree edited mid-run produced two
    verdicts wearing one name. Rooting them lets a run point at a snapshot of its own."""
    return (["Bash(date:*)", "Bash(TZ=*)", "Bash(mv:*)", "Bash(mkdir:*)"]
            + [f"Bash(python3 {root / 'scripts' / name}:*)" for name in SCRIPTS]
            + [f"Bash(python3 {root / 'chief_of_stuff.py'} {op}:*)" for op in OPERATIONS]
            + [f"Bash(chief-of-stuff {op}:*)" for op in OPERATIONS]
            + ["Read", "Glob", "Grep", "Write(./**)", "Edit(./**)"])


ALLOWED = allowed_tools(PLUGIN_ROOT)
DISALLOWED = ["Bash(git commit:*)", "Bash(git add:*)", "Bash(git push:*)"]
# These reach real Claude sessions on this machine. No eval run may expose them.
PEER_TOOLS = ("ListAgents", "SendMessage")
MOCK_CALENDAR = EVALS / "mock_calendar.py"
CALENDAR_TOOL = "mcp__calendar__list_events"
# The peers mock stands in for ListAgents/SendMessage, which stay out of every run (PEER_TOOLS guard).
MOCK_PEERS = EVALS / "mock_peers.py"
PEER_MOCK_TOOLS = ["mcp__peers__list_sessions", "mcp__peers__send"]

# `{{name}}`, or `{{now+Nh}}` / `{{now-Nh|local}}` / `{{now+Nh|hhmm}}` for times relative to render time.
TOKEN = re.compile(r"\{\{\s*([a-z_]+)(?:([+-]\d+)h)?(?:\|([a-z]+))?\s*\}\}")
NOW_FORMATS = {"local": "%Y-%m-%d %H:%M", "hhmm": "%H:%M"}
DURATION_HM = re.compile(r"(\d+)\s*h(?:ours?|rs?)?\s*(?:and\s*)?(\d{1,2})\s*m(?:in(?:utes?|s)?)?\b")
DURATION_H = re.compile(r"(\d+(?:\.\d+)?)\s*(?:hours?|hrs?|h)\b")
CLOCK = re.compile(
    # The closing parenthesis may carry a day word — "(01:52 CDT, tomorrow)" — because a gate past
    # midnight reads as this morning, already gone, without one.
    r"Now\s+(\d{1,2}):(\d{2})\s+\S+\s+·\s+(.+?)\s+in\s+(\d+)h(\d{2})m\s+\((\d{1,2}):(\d{2})\s+[^\s,)]+(?:,\s*[^)]{1,24})?\)"
)
TOLERANCE = timedelta(minutes=2)


# --- stream -----------------------------------------------------------------------------------


@dataclass
class Stream:
    init: dict[str, Any] = field(default_factory=dict)
    tool_uses: list[dict[str, Any]] = field(default_factory=list)
    denied_ids: set[str] = field(default_factory=set)
    last_text: str = ""
    result: dict[str, Any] | None = None


def load_stream(path: Path) -> Stream:
    return parse_stream([json.loads(raw) for raw in path.read_text().splitlines() if raw.strip()])


def parse_stream(events: list[dict[str, Any]]) -> Stream:
    s = Stream()
    for ev in events:
        kind = ev.get("type")
        if kind == "system" and ev.get("subtype") == "init":
            s.init = ev
        elif kind == "assistant":
            for block in ev["message"]["content"]:
                if block.get("type") == "tool_use":
                    s.tool_uses.append({"id": block["id"], "name": block["name"], "input": block.get("input", {})})
                elif block.get("type") == "text" and block.get("text", "").strip():
                    s.last_text = block["text"]
        elif kind == "result":
            s.result = ev
            s.denied_ids = {d["tool_use_id"] for d in ev.get("permission_denials", [])}
    return s


def check_arm(init: dict[str, Any], arm: str, agent_flag_used: bool = False, needs_calendar: bool = False, model: str | None = None, needs_peers: bool = False) -> tuple[bool, str]:
    """Confirm the session loaded what the arm claims, so a silent fallback cannot pass."""
    exposed = [t for t in PEER_TOOLS if t in init.get("tools", [])]
    if exposed:
        return False, f"sandbox exposes peer-session tools {exposed}"
    if model and model.lower() not in str(init.get("model", "")).lower():
        return False, f"init model {init.get('model')!r} is not {model!r}"
    if needs_calendar:
        servers = {m.get("name"): m.get("status") for m in init.get("mcp_servers", [])}
        if servers.get("calendar") != "connected":
            return False, f"mock calendar not connected: {servers}"
    if needs_peers:
        servers = {m.get("name"): m.get("status") for m in init.get("mcp_servers", [])}
        if servers.get("peers") != "connected":
            return False, f"mock peers not connected: {servers}"
    agents = init.get("agents", [])
    present = any(a == AGENT or a.endswith(":" + AGENT) for a in agents)
    if arm == "agent":
        return (True, "agent loaded") if present else (False, f"{AGENT} not in init agents {agents}")
    if arm == "baseline":
        return (False, "baseline ran with --agent") if agent_flag_used else (True, "no --agent")
    raise ValueError(f"unknown arm {arm!r}")


# --- fixtures ---------------------------------------------------------------------------------


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def stop_pages(work: Path, port: int | None = None) -> None:
    """End the pages server a case started with `pages.py --ensure`: it is detached and would outlive the run."""
    pid = work / "pages" / ".pid"
    try:
        server_pid = int(pid.read_text())
        if server_pid > 1 and server_pid != os.getpid():  # an unprepared {{live_pid}} fixture names the harness itself
            scripts = str(PLUGIN_ROOT / "scripts")
            if scripts not in sys.path:
                sys.path.insert(0, scripts)
            import pages as pg  # noqa: E402
            if pg.is_pages_serve(server_pid, port):  # same cmdline and port check as pages._stop (#482)
                os.kill(server_pid, signal.SIGTERM)
    except (OSError, ValueError, OverflowError):
        pass


def start_fixture_pages(work: Path, env: dict[str, str], root: Path) -> None:
    """A pages/.pid containing {{live_pid}} requests an already-serving board before the first turn.

    pages.py checks HTTP and the server version, not pid liveness. Start the real server under the
    eval's shim environment; remove the template pid first so a failed ensure cannot leave it behind.
    """
    pid = work / "pages" / ".pid"
    if pid.is_file() and pid.read_text().strip() == str(os.getpid()):
        pid.unlink()
        subprocess.run([sys.executable, str(root / "scripts" / "pages.py"), "--ensure"],
                       cwd=work, env=env, capture_output=True, text=True, check=True, timeout=15)


def context(tz: str, now: datetime) -> dict[str, str]:
    local = now.astimezone(ZoneInfo(tz))
    day = timedelta(days=1)
    return {
        "today": local.date().isoformat(),
        "yesterday": (local.date() - day).isoformat(),
        "tomorrow": (local.date() + day).isoformat(),
        "tz": tz,
        "now_hhmm": local.strftime("%H:%M"),
        "now_iso": local.replace(second=0, microsecond=0).isoformat(),
        # Fixtures cannot hold a real `.git/`; a file under `{{dotgit}}/` renders as one.
        "dotgit": ".git",
        # A pid that is alive for the whole run: the harness's own, which outlives every runtime it launches.
        "live_pid": str(os.getpid()),
        # The Board line's port: its own per run, so a case's pages server never answers another case.
        "pages_port": str(_free_port()),
    }


def render(text: str, ctx: dict[str, str]) -> str:
    def token(m: re.Match[str]) -> str:
        name, offset, fmt = m.groups()
        if offset is None and fmt is None:
            return ctx[name]
        if name != "now":
            raise KeyError(f"offset or format only allowed on now: {m.group(0)}")
        when = datetime.fromisoformat(ctx["now_iso"]) + timedelta(hours=int(offset or 0))
        if fmt is None:
            return when.isoformat()
        if fmt not in NOW_FORMATS:
            raise KeyError(f"unknown now format {fmt!r}")
        return when.strftime(NOW_FORMATS[fmt])

    return TOKEN.sub(token, text)


def render_value(value: Any, ctx: dict[str, str]) -> Any:
    if isinstance(value, str):
        return render(value, ctx)
    if isinstance(value, list):
        return [render_value(v, ctx) for v in value]
    if isinstance(value, dict):
        return {k: render_value(v, ctx) for k, v in value.items()}
    return value


def render_tree(src: Path, dst: Path, ctx: dict[str, str]) -> None:
    for path in sorted(src.rglob("*")):
        rel = Path(render(str(path.relative_to(src)), ctx))
        out = dst / rel
        if path.is_dir():
            out.mkdir(parents=True, exist_ok=True)
        else:
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(render(path.read_text(), ctx))


# --- graders ----------------------------------------------------------------------------------


@dataclass
class RunRecord:
    stream: Stream
    t_start: datetime
    t_end: datetime
    tz: str
    fixture_dir: Path
    mock_calls: list[dict[str, Any]] = field(default_factory=list)
    before_dir: Path | None = None

    @property
    def peer_calls(self) -> list[dict[str, Any]]:
        return [c for c in self.mock_calls if c.get("tool") in ("list_sessions", "send")]


def grade(g: dict[str, Any], rec: RunRecord) -> tuple[bool, str]:
    kind = g.get("type")
    if kind == "tool_used":
        return _tool_used(g, rec)
    if kind == "no_shell_edits":
        return _no_shell_edits(g, rec)
    if kind == "path_git_write":
        return _path_git_write(g, rec)
    if kind == "regex":
        return _regex(g, rec)
    if kind == "clock_line":
        return _clock_line(g, rec)
    if kind == "mock_calls":
        return _mock_calls(g, rec)
    if kind == "duration_stated":
        return _duration_stated(g, rec)
    if kind in FILE_GRADERS:
        return FILE_GRADERS[kind](g, rec)
    if kind in BOARD_GRADERS:
        return BOARD_GRADERS[kind](g, rec)
    if kind in COORDINATION_GRADERS:
        return COORDINATION_GRADERS[kind](g, rec)
    raise ValueError(f"unknown grader type {kind!r}")


def _tool_used(g: dict[str, Any], rec: RunRecord) -> tuple[bool, str]:
    name = re.compile(rf"(?:{g['tool']})")
    match = re.compile(g["input_match"]) if "input_match" in g else None
    hits = [
        t
        for t in rec.stream.tool_uses
        if name.fullmatch(t["name"]) and (match is None or match.search(json.dumps(t["input"])))
    ]
    denied = sum(1 for t in hits if t["id"] in rec.stream.denied_ids)
    lo, hi = g.get("min", 0), g.get("max")
    ok = len(hits) >= lo and (hi is None or len(hits) <= hi)
    bound = f"min {lo}" + (f", max {hi}" if hi is not None else "")
    if ok and hits and "before" in g:
        # `before`: the first hit comes ahead of the first call whose input matches this regex.
        later = re.compile(g["before"])
        first = next((i for i, t in enumerate(rec.stream.tool_uses) if later.search(json.dumps(t["input"]))), None)
        if first is not None and rec.stream.tool_uses.index(hits[0]) > first:
            return False, f"first call not before /{g['before']}/"
    return ok, f"{len(hits)} call(s) ({denied} denied); want {bound}"


GIT_VALUE_OPTIONS = {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--config-env",
                     "--super-prefix", "--attr-source"}


def _command_text(call: dict[str, Any]) -> Any:
    """The shell text of a Bash call; agy names the key CommandLine."""
    payload = call.get("input")
    if not isinstance(payload, dict):
        return None
    return payload.get("command") or payload.get("cmd") or payload.get("CommandLine")


def _git_verb(argv: list[str]) -> str:
    """The subcommand of a recorded git argv. A config alias can rename any verb, so it raises."""
    args = list(argv)
    while args and args[0].startswith("-"):
        flag = args.pop(0)
        if flag == "--":
            break
        if flag in GIT_VALUE_OPTIONS:
            if not args:
                raise ValueError("missing git option value")
            flag += "=" + args.pop(0)
        if re.match(r"(?:-c=?|--config-env=)(?i:alias\.)", flag):
            raise ValueError("git alias")
    return args[0] if args else ""


def _path_git_write(g: dict[str, Any], rec: RunRecord) -> tuple[bool, str]:
    """No `git` that the PATH shim recorded ran a listed verb. The shim logs argv, so shell syntax is never read.

    This grades a git write made through PATH and nothing else. An absolute-path git (`/usr/bin/git push`)
    skips the shim and a call the host denies never reaches a shell; neither is graded. Fixture remotes are
    local paths, so the miss cannot push anywhere real (architect ruling on #726; closing it is #770, 1.1).
    """
    banned, hits = set(g["subcommands"]), []
    for call in rec.mock_calls:
        if call.get("tool") != "git":
            continue
        try:
            hit = _git_verb(call["argv"]) in banned
        except ValueError:
            hit = True
        if hit:
            hits.append("git " + " ".join(call["argv"]))
    return (not hits), (f"no git {'/'.join(sorted(banned))}" if not hits else f"git write call(s): {hits[:3]}")


SAFE_SCRIPTS = set(SCRIPTS)
SHELL_MUTATORS = {"cp", "rm", "rmdir", "sed", "perl", "awk", "tee", "dd", "truncate"}


def _no_shell_edits(_g: dict[str, Any], rec: RunRecord) -> tuple[bool, str]:
    """Allow pinned Python helpers while rejecting shell commands that edit or redirect files."""
    violations = []
    for call in rec.stream.tool_uses:
        if call["name"] != "Bash":
            continue
        command = _command_text(call)
        if not isinstance(command, str):
            violations.append("unreadable shell call")
            continue
        if re.search(r"(?<!\d)>>?\s*(?!&)\S|<<", command):
            violations.append(command)
            continue
        try:
            tokens = shlex.split(command)
        except ValueError:
            violations.append(command)
            continue
        for i, token in enumerate(tokens):
            name = Path(token).name
            if name in SHELL_MUTATORS:
                violations.append(command)
                break
            if name == "git" and i + 1 < len(tokens) and tokens[i + 1] in {"add", "commit", "push", "checkout", "restore"}:
                violations.append(command)
                break
            if re.fullmatch(r"python(?:3(?:\.\d+)?)?", name):
                script = Path(tokens[i + 1]) if i + 1 < len(tokens) else Path("")
                router = (script.name == "chief_of_stuff.py" and i + 2 < len(tokens)
                          and tokens[i + 2] in OPERATIONS)
                if not router and (script.parent.name != "scripts" or script.name not in SAFE_SCRIPTS):
                    violations.append(command)
                    break
    return (not violations), ("no shell edits" if not violations else f"shell edit call(s): {violations[:3]}")


def _regex(g: dict[str, Any], rec: RunRecord) -> tuple[bool, str]:
    found = re.search(g["pattern"], rec.stream.last_text, re.MULTILINE) is not None
    mode = g.get("match", "contains")
    ok = found if mode == "contains" else not found
    return ok, f"/{g['pattern']}/ {'found' if found else 'not found'}; want {mode}"


def _clock_line(g: dict[str, Any], rec: RunRecord) -> tuple[bool, str]:
    m = CLOCK.search(rec.stream.last_text)
    if not m:
        return False, "no Clock line (want 'Now HH:MM TZ · <gate> in XhYYm (HH:MM TZ)')"
    zone = ZoneInfo(rec.tz)
    gate = datetime.strptime(g["gate"], "%Y-%m-%d %H:%M").replace(tzinfo=zone)
    start, end = rec.t_start.astimezone(zone), rec.t_end.astimezone(zone)
    now_h, now_m, _, rem_h, rem_m, gate_h, gate_m = m.groups()
    problems = []
    stated_now = start.replace(hour=int(now_h), minute=int(now_m), second=0, microsecond=0)
    if not (start.replace(second=0, microsecond=0) - TOLERANCE <= stated_now <= end + TOLERANCE):
        problems.append(f"Now {now_h}:{now_m} outside run window {start:%H:%M}-{end:%H:%M}")
    if (int(gate_h), int(gate_m)) != (gate.hour, gate.minute):
        problems.append(f"gate {gate_h}:{gate_m} != {gate:%H:%M}")
    remaining = timedelta(hours=int(rem_h), minutes=int(rem_m))
    if not (gate - end - TOLERANCE <= remaining <= gate - start + TOLERANCE):
        problems.append(f"remaining {rem_h}h{rem_m}m outside {_hm(gate - end)}-{_hm(gate - start)}")
    return (not problems), ("; ".join(problems) if problems else f"'{m.group(0)}' ok")


def _duration_stated(g: dict[str, Any], rec: RunRecord) -> tuple[bool, str]:
    """Some line matching `line_match` (Clock lines excluded) states a duration within tolerance of `hours`."""
    want, tol = float(g["hours"]), float(g.get("tolerance_hours", 0.25))
    label = re.compile(g.get("line_match", "."))
    seen = []
    for line in rec.stream.last_text.splitlines():
        if CLOCK.search(line) or not label.search(line):
            continue
        durations = [int(h) + int(m) / 60 for h, m in DURATION_HM.findall(line)]
        durations += [float(h) for h in DURATION_H.findall(DURATION_HM.sub(" ", line))]
        seen += durations
        if any(abs(d - want) <= tol for d in durations):
            return True, f"stated {min(durations, key=lambda d: abs(d - want)):.2f}h; want {want}±{tol}"
    return False, f"durations on matching lines {[round(d, 2) for d in seen]}; want {want}±{tol}"


def _mock_calls(g: dict[str, Any], rec: RunRecord) -> tuple[bool, str]:
    """Each calendar id was queried at least once with a window covering `covers`: a whole day
    (`YYYY-MM-DD`) or `{"from": "YYYY-MM-DD HH:MM", "to": ...}` in the case timezone."""
    zone = ZoneInfo(rec.tz)
    if isinstance(g["covers"], dict):
        day_start = datetime.strptime(g["covers"]["from"], "%Y-%m-%d %H:%M").replace(tzinfo=zone)
        day_end = datetime.strptime(g["covers"]["to"], "%Y-%m-%d %H:%M").replace(tzinfo=zone)
    else:
        day_start = datetime.strptime(g["covers"], "%Y-%m-%d").replace(tzinfo=zone)
        day_end = day_start + timedelta(days=1)

    def covers(args: dict[str, Any]) -> bool:
        lo = _instant(args.get("timeMin"), zone)
        hi = _instant(args.get("timeMax"), zone)
        return (lo is None or lo <= day_start) and (hi is None or hi >= day_end)

    missing = []
    for cal in g["calendar_ids"]:
        calls = [c["arguments"] for c in rec.mock_calls if c.get("arguments", {}).get("calendarId") == cal]
        if not any(covers(a) for a in calls):
            missing.append(f"{cal} ({len(calls)} call(s), none covering {day_start:%Y-%m-%d %H:%M}-{day_end:%Y-%m-%d %H:%M})")
    return (not missing), ("missing: " + "; ".join(missing) if missing else f"{len(rec.mock_calls)} call(s) cover {day_start:%Y-%m-%d %H:%M}-{day_end:%Y-%m-%d %H:%M}")


def _read(root: Path | None, rel: str) -> str | None:
    path = root / rel if root else None
    return path.read_text() if path and path.is_file() else None


def _file_unchanged(g: dict[str, Any], rec: RunRecord) -> tuple[bool, str]:
    before, after = _read(rec.before_dir, g["path"]), _read(rec.fixture_dir, g["path"])
    if before == after:
        return True, f"{g['path']} unchanged"
    state = "created" if before is None else "deleted" if after is None else "modified"
    return False, f"{g['path']} {state}"


def _glob_matches(g: dict[str, Any], rec: RunRecord) -> tuple[bool, str]:
    """`file_matches` over a path the agent named — a worktree it chose, a dispatch file inside it.

    Every match must satisfy the pattern, and no match at all is a failure rather than a pass: a
    grader that is vacuously true when its subject is missing measures nothing.
    """
    paths = sorted((rec.fixture_dir or Path(".")).glob(g["glob"]))
    if not paths:
        return False, f"no file matching {g['glob']}"
    mode = g.get("match", "contains")
    bad = []
    for path in paths:
        found = re.search(g["pattern"], path.read_text(errors="replace"), re.MULTILINE) is not None
        if found is not (mode == "contains"):
            bad.append(str(path.relative_to(rec.fixture_dir)))
    detail = f"{len(paths)} file(s) matching {g['glob']}: /{g['pattern']}/ want {mode}"
    return not bad, detail + (f"; failed by {', '.join(bad)}" if bad else "")


def _file_matches(g: dict[str, Any], rec: RunRecord) -> tuple[bool, str]:
    if rec.fixture_dir and g.get("glob", g.get("path", "")).startswith("pages/"):
        serve_board(rec.fixture_dir)
    if "glob" in g:
        return _glob_matches(g, rec)
    text = _read(rec.fixture_dir, g["path"])
    if text is None:
        return False, f"{g['path']} missing"
    if "count" in g:
        n = len(re.findall(g["pattern"], text, re.MULTILINE))
        return n == g["count"], f"{g['path']}: /{g['pattern']}/ matches {n} time(s); want {g['count']}"
    found = re.search(g["pattern"], text, re.MULTILINE) is not None
    mode = g.get("match", "contains")
    ok = found if mode == "contains" else not found
    return ok, f"{g['path']}: /{g['pattern']}/ {'found' if found else 'not found'}; want {mode}"


def _files(root: Path | None) -> set[str]:
    """Every file under `root`, minus anything inside a `.git/`: a repo built for a run is not the agent's doing."""
    if not (root and root.is_dir()):
        return set()
    return {str(f.relative_to(root)) for f in root.rglob("*") if f.is_file() and ".git" not in f.relative_to(root).parts}


def before_snapshot(work: Path, dest: Path) -> None:
    """The tree as the agent first saw it — fixture and, where a case has one, its repo.

    `no_new_files` asks what the agent created, so the baseline has to be taken after every piece of
    setup. Snapshotting the rendered fixture alone reported a case's own `repo` as the agent's work,
    and git object names differ per run, so no `except` list could have covered it.
    """
    shutil.copytree(work, dest, dirs_exist_ok=True, symlinks=True)


def _no_new_files(g: dict[str, Any], rec: RunRecord) -> tuple[bool, str]:
    """`except` entries are exact paths or globs (`Resources/**`)."""
    # The page server's pid, log, sources cache and decisions list, and the mailbox's own ignore file, are the
    # scripts', never the agent's work.
    allowed = [str(e) for e in g.get("except", [])] + ["pages/.pid", ".chief-of-stuff/pages.log",
                                                      ".chief-of-stuff/mailbox/.gitignore", "pages/.sources*",
                                                      "pages/decisions.html"]
    new = sorted(f for f in _files(rec.fixture_dir) - _files(rec.before_dir) if not any(fnmatch.fnmatch(f, e) for e in allowed))
    return (not new), (f"new files: {new}" if new else "no new files")


def _section(text: str, heading: str) -> list[str] | None:
    """Non-blank lines under `heading`, up to the next heading of the same or higher level."""
    level = len(heading) - len(heading.lstrip("#"))
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if line.strip() == heading.strip():
            body = []
            for nxt in lines[i + 1 :]:
                hashes = len(nxt) - len(nxt.lstrip("#"))
                if hashes and hashes <= level and nxt[hashes : hashes + 1] == " ":
                    break
                if nxt.strip():
                    body.append(nxt.rstrip())
            return body
    return None


def _lines_preserved(g: dict[str, Any], rec: RunRecord) -> tuple[bool, str]:
    """Every line of the original section appears, in order, in the result's section (append-only)."""
    before = _section(_read(rec.before_dir, g["path"]) or "", g["section"])
    after = _section(_read(rec.fixture_dir, g["path"]) or "", g["section"])
    if before is None or after is None:
        return False, f"{g['path']}: section {g['section']!r} missing ({'before' if before is None else 'after'})"
    it = iter(after)
    lost = [line for line in before if not any(line == candidate for candidate in it)]
    if lost:
        return False, f"{g['path']} {g['section']}: lost or reordered {lost}"
    added = len(after) - len(before)
    want = g.get("min_added", 0)
    return added >= want, f"{g['path']} {g['section']}: preserved, {added} line(s) added; want >= {want}"


CHECK_LINE = re.compile(r"^(\s*)- \[([ xX])\]\s+(.*?)\s*$")


def _checklist(text: str) -> list[tuple[int, str, bool, str]]:
    items = []
    for line in text.splitlines():
        m = CHECK_LINE.match(line)
        if m:
            body, _, evidence = m.group(3).partition(" — evidence: ")
            items.append((len(m.group(1)), body.strip(), m.group(2) != " ", evidence.strip()))
    return items


def _checklist_ticks_only(g: dict[str, Any], rec: RunRecord) -> tuple[bool, str]:
    """Same items in the same order and words; exactly `ticked` flip to [x], each with evidence (matching `evidence_match`); nothing unticks."""
    before = _read(rec.before_dir, g["path"])
    after = _read(rec.fixture_dir, g["path"])
    if before is None or after is None:
        return False, f"{g['path']} missing ({'before' if before is None else 'after'})"
    b, a = _checklist(before), _checklist(after)
    if [(d, t) for d, t, _, _ in b] != [(d, t) for d, t, _, _ in a]:
        return False, f"{g['path']}: items added, removed, reordered, or reworded ({len(b)} before, {len(a)} after)"
    want = set(g.get("ticked", []))
    problems = []
    for (_, text, was, _), (_, _, now, evidence) in zip(b, a):
        if was and not now:
            problems.append(f"unticked {text!r}")
        elif not was and now and text not in want:
            problems.append(f"ticked {text!r}, not in ticked")
        elif not was and not now and text in want:
            problems.append(f"not ticked {text!r}")
        elif not was and now:
            if not evidence:
                problems.append(f"no evidence on {text!r}")
            elif "evidence_match" in g and not re.search(g["evidence_match"], evidence):
                problems.append(f"evidence {evidence!r} on {text!r} does not match /{g['evidence_match']}/")
    return (not problems), ("; ".join(problems) if problems else f"{g['path']}: ticked {sorted(want) or 'nothing'}, items unchanged")


def _file_moved(g: dict[str, Any], rec: RunRecord) -> tuple[bool, str]:
    """`from` is gone; a file of the same name and content sits anywhere under `to` (a directory)."""
    src, dst = g["from"], g["to"]
    before = _read(rec.before_dir, src)
    if before is None:
        return False, f"{src} missing from fixture-before"
    if _read(rec.fixture_dir, src) is not None:
        return False, f"{src} still present"
    root = rec.fixture_dir / dst
    hits = [p for p in root.rglob(Path(src).name) if p.is_file()] if root.is_dir() else []
    if not hits:
        return False, f"{Path(src).name} not found under {dst}/"
    same = [p for p in hits if p.read_text() == before]
    if not same:
        return False, f"{src} moved under {dst}/ but content differs"
    return True, f"{src} -> {same[0].relative_to(rec.fixture_dir)}"


def _task_rows(text: str) -> list[tuple[str, str]] | None:
    """Every Tasks row as (name, item) in order, or None when the table has no `name` column.

    `name` is the newest Tasks column. An older six-column tracker has none — the board falls back
    to guessing a name from the item, and `agents/chief-of-stuff.md` says such a tracker is not to
    be rewritten wholesale to add it. There is nothing to compare in that case, and saying so beats
    passing by default: a grader that cannot see its subject has not checked it.
    """
    rows = _section(text, "## Tasks") or []
    cells = [[c.strip() for c in r.strip().strip("|").split("|")] for r in rows if r.strip().startswith("|")]
    table = [c for c in cells if not all(set(x) <= set("-: ") for x in c)]
    if not table or table[0][:2] != ["name", "item"]:
        return None
    return [(r[0], r[1]) for r in table[1:] if len(r) > 1]


def _task_names_unchanged(g: dict[str, Any], rec: RunRecord) -> tuple[bool, str]:
    """No task was renamed. A fact carried out of the Resume block goes to the Log or to `item`.

    Not a length rule. A long name can be a house style with readers, and a fixture does not get to
    rule on that; the failure this watches for is a measurement narrative appended to a NAME during
    a move about something else, where `item` is the documented home for history. `except` names the
    tasks a case expects to be renamed, for a move that changes what a task is about.

    A task is matched to its former self by its item as a PREFIX. Neither column is a stable key on
    its own: the name is the attribute under test, and the item is documented to accrete history
    (Tasks: "`item` keeps the wording, the history you append"). What holds is that an appended item
    still begins with the item that was there.
    """
    before = _task_rows(_read(rec.before_dir, g["path"]) or "")
    after = _task_rows(_read(rec.fixture_dir, g["path"]) or "")
    if before is None or after is None:
        which = "before" if before is None else "after"
        return False, f"{g['path']}: no `name` column in the Tasks table ({which})"
    allowed, renamed, gone = set(g.get("except", [])), [], []
    left = list(after)
    for was, item in before:
        match = next((r for r in left if r[1] == item or r[1].startswith(item)), None)
        if match is None:
            if was not in allowed:
                gone.append(was)
            continue
        left.remove(match)
        if match[0] != was and was not in allowed:
            renamed.append(f"{was!r} -> {match[0]!r}")
    if renamed or gone:
        return False, f"{g['path']}: renamed {renamed}, lost {gone}"
    return True, f"{g['path']}: {len(before)} task name(s) unchanged"


_RENDERER_CACHE: list[Any] = []


def _renderer():
    """The board renderer, imported once. The rule and its enforcer must not drift apart.

    A grader that re-implements `parse_resume` with a regex is a second definition of the field,
    and two definitions of one number do not argue -- they produce a confident answer each, in
    different directions, with nothing to force the reconciliation until a value lands in the gap.
    """
    import importlib.util
    if _RENDERER_CACHE:
        return _RENDERER_CACHE[0]
    spec = importlib.util.spec_from_file_location("_render_board", BOARD_RENDERER)
    mod = importlib.util.module_from_spec(spec)
    # Registered BEFORE exec: the renderer defines dataclasses, and @dataclass resolves its own
    # module out of sys.modules. Without this the import dies inside dataclasses, not here.
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    _RENDERER_CACHE.append(mod)
    return mod


def _resume_fields_bounded(g: dict[str, Any], rec: RunRecord) -> tuple[bool, str]:
    """Every `## Resume` field is within the renderer's own cap, measured by the renderer's parse.

    Not `^- (As of|…)\b[^\n]{300,}$`: that counts from the end of the label WORD, so it charges
    the writer for the `: ` and fails a conforming field at 298, 299 and 300. It is also blind to
    the eight characters `parse_resume` inserts when the label carries a clock (`- Verified 19:39:`
    becomes the value `19:39 · …`), which is a real part of what the board measures.
    """
    text = _read(rec.fixture_dir, g["tracker"])
    if text is None:
        return False, f"{g['tracker']}: not found"
    r = _renderer()
    block = r.parse_resume(text)
    if not block:
        return True, "no Resume block"
    cap = int(g.get("cap", r.RESUME_CAP))
    over = sorted(((k, len(v)) for k, v in block.items() if len(v) > cap), key=lambda kv: -kv[1])
    if over:
        return False, "over %d: %s" % (cap, "; ".join(f"{k} {n}" for k, n in over))
    widest = max(((k, len(v)) for k, v in block.items()), key=lambda kv: kv[1])
    return True, f"{len(block)} field(s) within {cap}, widest {widest[0]} {widest[1]}"


def _resume_block_matches(g: dict[str, Any], rec: RunRecord) -> tuple[bool, str]:
    """Whether a pattern appears in the Resume block's FIELD VALUES, as the renderer parses them.

    `fields` narrows the search to named keys. A block that is absent fails either way: an
    absent-check over a section that is gone passes for the wrong reason, which is the same hole
    that let a delete-and-stub score 9/9 -- the instrument that would have spoken is not there.
    """
    text = _read(rec.fixture_dir, g["tracker"])
    if text is None:
        return False, f"{g['tracker']}: not found"
    block = _renderer().parse_resume(text)
    if not block:
        return False, f"{g['tracker']}: no Resume block to check"
    if "fields" in g:
        want = {f.lower() for f in g["fields"]}
        block = {k: v for k, v in block.items() if k in want}
        if not block:
            return False, f"{g['tracker']}: no field(s) {sorted(want)} in the block"
    pat = re.compile(g["pattern"], re.I if g.get("ignore_case", True) else 0)
    hits = sorted(k for k, v in block.items() if pat.search(v))
    absent = g.get("match", "present") == "absent"
    if absent:
        return (not hits), ("clear of /%s/ in %d field(s)" % (g["pattern"], len(block)) if not hits
                            else "found in %s" % ", ".join(hits))
    return bool(hits), ("found in %s" % ", ".join(hits) if hits
                        else "/%s/ in no field of %d" % (g["pattern"], len(block)))


def _md_kind(line: str) -> str:
    s = line.strip()
    if not s:
        return "blank"
    if s.startswith(("```", "~~~")):
        return "fence"
    if re.match(r"#{1,6}\s", s):
        return "heading"
    if re.match(r"(?:[-*+]|\d+[.)])\s", s):
        return "list"
    return "table" if s.startswith("|") else "quote" if s.startswith(">") else "prose"


def _markdown_unwrapped(g: dict[str, Any], rec: RunRecord) -> tuple[bool, str]:
    """Enforces "One paragraph is one line; never hard-wrap prose at any column.", "Blank lines
    separate blocks." and, for each string in `fenced`, "Code and commands go in fences with a
    language." over `section` of `path` (the whole file when no section is named).

    A wrap is a prose line ending without terminal punctuation followed by a lowercase continuation.
    Two adjacent non-blank lines are one block only when both are list items, table rows or quote
    lines; anything else touching is a missing blank line. Fenced code is skipped.
    """
    text = _read(rec.fixture_dir, g["path"])
    if text is None:
        return False, f"{g['path']} missing"
    lines = text.splitlines()
    if "section" in g:
        level = len(g["section"]) - len(g["section"].lstrip("#"))
        start = next((i for i, l in enumerate(lines) if l.strip() == g["section"].strip()), None)
        if start is None:
            return False, f"{g['path']}: no {g['section']!r}"
        end = next((j for j in range(start + 1, len(lines))
                    if re.match(rf"#{{1,{level}}}\s", lines[j])), len(lines))
        lines = lines[start:end]
    problems, paragraphs, fenced, prev, code = [], 0, False, ("blank", ""), []
    for line in lines:
        kind = _md_kind(line)
        if fenced:
            fenced = kind != "fence"
            if not fenced:
                prev = ("fence", line)
            elif re.match(r"\s*(?:```|~~~)\s*\w", prev[1]):
                code.append(line)
            continue
        paragraphs += kind == "prose"
        was, prev_line = prev
        if was != "blank" and kind != "blank":
            if was == kind == "prose" and not re.search(r"[.!?:;][)\]\"'*_`]*$", prev_line.rstrip()) \
                    and re.match(r"\s*[a-z]", line):
                problems.append(f"wrapped: {prev_line.strip()[-30:]!r} / {line.strip()[:30]!r}")
            elif not (was == kind and kind in ("list", "table", "quote")):
                problems.append(f"no blank line: {prev_line.strip()[:30]!r} / {line.strip()[:30]!r}")
        fenced = kind == "fence"
        prev = (kind, line)
    problems += [f"not in a fence with a language: {c!r}" for c in g.get("fenced", [])
                 if not any(c in line for line in code)]
    want = int(g.get("min_paragraphs", 1))
    if paragraphs < want:
        problems.append(f"{paragraphs} prose line(s); want >= {want}")
    return (not problems), ("; ".join(problems[:4]) if problems else f"{paragraphs} paragraph(s), none wrapped")


FILE_GRADERS = {
    "markdown_unwrapped": _markdown_unwrapped,
    "file_moved": _file_moved,
    "file_unchanged": _file_unchanged,
    "file_matches": _file_matches,
    "no_new_files": _no_new_files,
    "lines_preserved": _lines_preserved,
    "task_names_unchanged": _task_names_unchanged,
    "checklist_ticks_only": _checklist_ticks_only,
    "resume_fields_bounded": _resume_fields_bounded,
    "resume_block_matches": _resume_block_matches,
}


def _instant(value: str | None, zone: ZoneInfo) -> datetime | None:
    if not value:
        return None
    dt = datetime.fromisoformat(value)
    return dt if dt.tzinfo else dt.replace(tzinfo=zone)


def _hm(d: timedelta) -> str:
    mins = int(d.total_seconds() // 60)
    return f"{mins // 60}h{mins % 60:02d}m"


# --- invocation -------------------------------------------------------------------------------


@dataclass
class Case:
    name: str
    root: Path
    spec: dict[str, Any]


def load_cases(patterns: list[str], golden_only: bool = False) -> list[Case]:
    """The cases to run. `golden_only` keeps just the ones their spec marks `"golden": true`.

    Zach, 2026-09-19: sonnet to iterate, opus on the golden cases only for full green. The mark
    lives in each case.json rather than a list here, so adding a case and deciding whether it is
    golden are the same edit and cannot drift apart.
    """
    cases = []
    for spec_path in sorted((EVALS / "cases").glob("*/case.json")):
        name = spec_path.parent.name
        if patterns and not any(fnmatch.fnmatch(name, p) for p in patterns):
            continue
        spec = json.loads(spec_path.read_text())
        if golden_only and spec.get("golden") is not True:
            continue
        cases.append(Case(name, spec_path.parent, spec))
    return cases


def serve_board(root: Path) -> None:
    """What a GET of `/`, of each decision page and of `decisions.html` does in pages.py: re-render each
    when its sources are newer. The agent never renders; the page server does, so a grader reads the
    pages the server would serve."""
    sys.path.insert(0, str(PLUGIN_ROOT / "scripts"))
    import pages
    try:
        pages_dir, _ = pages.from_config(root)
        name = pages.today_board(root)
    except Exception:  # no Board line or no CLAUDE.md: there is no server to render, only files
        return
    decisions = [f"{p.stem}.html" for p in pages_dir.glob("decision-*.json")]
    for page in [name] * bool(name) + decisions + ["decisions.html"] * bool(name or decisions):
        pages.fresh(root, pages_dir, page)


def _last_published_html(rec: RunRecord) -> tuple[str | None, str]:
    """The newest `*-board.html` in the case's tree, after the page server's render: the page pages.py serves."""
    serve_board(rec.fixture_dir)
    boards = sorted(rec.fixture_dir.rglob("*-board.html"), key=lambda p: p.name)
    if not boards:
        return None, "no board rendered"
    return boards[-1].read_text(), f"board {boards[-1].relative_to(rec.fixture_dir)}"


def _board_published(g: dict[str, Any], rec: RunRecord) -> tuple[bool, str]:
    """A board was rendered; it names every task in `tasks`, the `deadline` instant, and matches `html_match`."""
    html_text, note = _last_published_html(rec)
    if html_text is None:
        return False, note
    problems = [f"task not on board: {task!r}" for task in g.get("tasks", []) if _esc_html(task) not in html_text]
    if "deadline" in g and g["deadline"] not in html_text:
        problems.append(f"deadline {g['deadline']} not on board")
    if "html_match" in g and not re.search(g["html_match"], html_text):
        problems.append(f"/{g['html_match']}/ not in board")
    return (not problems), ("; ".join(problems) if problems else note)


def _esc_html(text: str) -> str:
    import html as _html
    return _html.escape(text, quote=True)


META = re.compile(r'<meta name="tracker-sha256" content="([0-9a-f]{64})">')


def _board_matches_tracker(g: dict[str, Any], rec: RunRecord) -> tuple[bool, str]:
    """sha256 of the final tracker == the meta in the newest rendered board."""
    tracker = _read(rec.fixture_dir, g["tracker"])
    if tracker is None:
        return False, f"{g['tracker']} missing"
    want = hashlib.sha256(tracker.encode()).hexdigest()
    html_text, note = _last_published_html(rec)
    if html_text is None:
        return False, note
    m = META.search(html_text)
    if not m:
        return False, "board has no tracker-sha256 meta (not produced by the renderer?)"
    if m.group(1) != want:
        return False, "board is stale: tracker edited after the last render"
    return True, f"board sha {want[:12]} matches the tracker"


BOARD_GRADERS = {
    "board_published": _board_published,
    "board_matches_tracker": _board_matches_tracker,
}


def _peer_calls(g: dict[str, Any], rec: RunRecord) -> tuple[bool, str]:
    """Count this turn's calls to the peers mock that match every filter given."""
    calls = [c for c in rec.peer_calls if c.get("tool") == g["tool"]]
    if "to_ref" in g:
        calls = [c for c in calls if c.get("to_ref") == g["to_ref"]]
    if "ok" in g:
        calls = [c for c in calls if bool(c.get("ok")) == bool(g["ok"])]
    if "text_match" in g:
        calls = [c for c in calls if re.search(g["text_match"], c.get("text", ""))]
    if "text_not_match" in g:
        calls = [c for c in calls if not re.search(g["text_not_match"], c.get("text", ""))]
    lo, hi = g.get("min", 0), g.get("max")
    ok = len(calls) >= lo and (hi is None or len(calls) <= hi)
    bound = f"min {lo}" + (f", max {hi}" if hi is not None else "")
    return ok, f"{len(calls)} call(s) match; want {bound}"


TIME_CELL = re.compile(r"^(\d{1,2}):(\d{2})$")
LEADING_TIME = re.compile(r"^-\s*(\d{1,2}):(\d{2})\b")


def _timestamp_tolerance(g: dict[str, Any], rec: RunRecord) -> tuple[bool, str]:
    """Every time written into `section` during this turn is the move's own clock read: within [t_start-tol, t_end+tol].

    Leading mode (default): each line added since `before_dir` must start `- HH:MM`. Column mode (`column`, 1-based):
    the cell of each added or changed table row; a blank cell passes, a non-time cell (a date, a word) is ignored.
    Times quoted elsewhere on the line are ignored by design.
    """
    before = set(_section(_read(rec.before_dir, g["path"]) or "", g["section"]) or [])
    after = _section(_read(rec.fixture_dir, g["path"]) or "", g["section"])
    if after is None:
        return False, f"{g['path']}: section {g['section']!r} missing"
    tol = timedelta(minutes=g.get("tolerance_minutes", 1))
    lo, hi = rec.t_start - tol, rec.t_end + tol
    zone = ZoneInfo(rec.tz)
    day = rec.t_start.astimezone(zone).date()
    bad, checked = [], 0
    named = re.compile(g["time_match"]) if "time_match" in g else None
    for line in after:
        if line in before and not g.get("rewritten"):
            continue
        if named:
            m = named.search(line)
            if not m:
                continue  # a block line that states no time of its own, or quotes one it did not read
        elif "column" in g:
            if not line.startswith("|"):
                continue
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            idx = g["column"] - 1
            cell = cells[idx] if idx < len(cells) else ""
            m = TIME_CELL.match(cell)
            if not cell or not m:
                continue
        else:
            m = LEADING_TIME.match(line)
            if not m:
                bad.append(f"no leading time: {line[:60]!r}")
                continue
        at = datetime.combine(day, dtime(int(m[1]), int(m[2])), tzinfo=zone)
        checked += 1
        if not (lo <= at <= hi):
            bad.append(f"{m[1]}:{m[2]} outside {lo:%H:%M}-{hi:%H:%M}: {line[:60]!r}")
    return (not bad), ("; ".join(bad) if bad else f"{checked} written time(s) within the move")


def _reply_lines(g: dict[str, Any], rec: RunRecord) -> tuple[bool, str]:
    n = len([l for l in rec.stream.last_text.splitlines() if l.strip()])
    return n <= g["max"], f"{n} non-empty line(s); want <= {g['max']}"


def _spawn_calls(g: dict[str, Any], rec: RunRecord) -> tuple[bool, str]:
    """Sessions started this turn, as the recorder saw them. No real terminal opens in the sandbox."""
    calls = [c for c in rec.mock_calls if c.get("tool") == "spawn"]
    if "argv_match" in g:
        pattern = re.compile(g["argv_match"])
        calls = [c for c in calls if pattern.search(" ".join(c.get("argv", [])))]
    lo, hi = g.get("min", 0), g.get("max")
    ok = len(calls) >= lo and (hi is None or len(calls) <= hi)
    return ok, f"{len(calls)} spawn(s); want min {lo}" + (f", max {hi}" if hi is not None else "")


def _health_calls(g: dict[str, Any], rec: RunRecord) -> tuple[bool, str]:
    """Probes recorded by the loopback server in this turn. Without this a case can pass having probed nothing."""
    calls = [c for c in rec.mock_calls if c.get("tool") == "health"]
    if "path" in g:
        calls = [c for c in calls if c.get("path") == g["path"]]
    if "status" in g:
        calls = [c for c in calls if c.get("status") == g["status"]]
    lo, hi = g.get("min", 0), g.get("max")
    ok = len(calls) >= lo and (hi is None or len(calls) <= hi)
    return ok, f"{len(calls)} call(s); want min {lo}" + (f", max {hi}" if hi is not None else "")


COORDINATION_GRADERS = {
    "spawn_calls": _spawn_calls,
    "health_calls": _health_calls,
    "peer_calls": _peer_calls,
    "timestamp_tolerance": _timestamp_tolerance,
    "reply_lines": _reply_lines,
}

# Every `type` a case may name. `grade()` is the dispatcher; this is what `test_cases.py` checks a case
# against, and `test_the_grader_type_tuple_is_the_dispatcher` keeps the two from drifting.
GRADER_TYPES = ("tool_used", "no_shell_edits", "path_git_write", "regex", "clock_line", "mock_calls", "duration_stated", *FILE_GRADERS, *BOARD_GRADERS, *COORDINATION_GRADERS)


def peers_mcp_config(sessions: Path, log: Path, tz: str) -> dict[str, Any]:
    args = [str(MOCK_PEERS.resolve()), "--sessions", str(sessions), "--log", str(log), "--tz", tz]
    return {"mcpServers": {"peers": {"type": "stdio", "command": sys.executable, "args": args}}}


def merge_mcp(*configs: dict[str, Any] | None) -> dict[str, Any] | None:
    servers: dict[str, Any] = {}
    for c in configs:
        if c:
            servers.update(c.get("mcpServers", {}))
    return {"mcpServers": servers} if servers else None


def calendar_mcp_config(events: Path, log: Path, tz: str) -> dict[str, Any]:
    args = [str(MOCK_CALENDAR.resolve()), "--events", str(events), "--log", str(log), "--tz", tz]
    return {"mcpServers": {"calendar": {"type": "stdio", "command": sys.executable, "args": args}}}


CLAUDE_ENV = "CHIEF_OF_STUFF_CLAUDE"
# A GUI-launched terminal inherits launchd's PATH, not the shell's, so a bare `claude` is not on it
# and every case a session writes from such a tab goes ungraded. Resolve it once, absolutely, and
# leave an override for a machine that keeps it somewhere else.
CLAUDE_FALLBACKS = ("/opt/homebrew/bin/claude", "/usr/local/bin/claude")


def claude_binary() -> str:
    """The `claude` to run: the override, then PATH, then the two places a Mac install puts it."""
    override = os.environ.get(CLAUDE_ENV, "").strip()
    if override:
        return override
    found = shutil.which("claude")
    if found:
        return str(Path(found).resolve())
    for candidate in CLAUDE_FALLBACKS:
        if Path(candidate).exists():
            return candidate
    return "claude"


SNAPSHOT_SKIP = shutil.ignore_patterns(".git", "results", "__pycache__", "*.pyc", ".DS_Store")


def snapshot_plugin(dest: Path) -> Path:
    """Copy the plugin tree a run will read, so the run is a verdict on one state of the code.

    `evals/results` is skipped because it holds every previous run and would grow each sweep by the
    size of all of them; `.git` for the same reason. What remains is what a case can reach: the
    scripts on the allowlist and the agent definition behind `--plugin-dir`.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(PLUGIN_ROOT, dest, ignore=SNAPSHOT_SKIP, dirs_exist_ok=True)
    return dest


def write_verdict(out: Path, case: str, arm: str, model: str, run_no: int,
                  results: list[tuple[str, bool, str]], error: str | None, meta: str,
                  runtime: str = "claude") -> Path:
    """What the run decided, beside the inputs it decided from. Finding 114b.

    The result dir held every input to grading and none of its output, so a sweep whose stdout was
    piped through `tail` was unreadable afterwards — 56 of 58 case verdicts gone from a 3.3-hour run.
    Grading is a pure function of this directory and the verdicts could be rebuilt from it, which is
    a recovery route and not a reason to leave them unwritten.

    `passed` is None when a harness error stopped the run, never False: a case that never ran is not
    a case that failed, which is the 0.9.0 lesson that cost fourteen false regressions.
    """
    out.mkdir(parents=True, exist_ok=True)
    path = out / "verdict.json"
    path.write_text(json.dumps({
        "case": case, "arm": arm, "model": model, "runtime": runtime, "run": run_no, "meta": meta,
        "error": error,
        "passed": None if error else all(p for _, p, _ in results),
        "graders": [{"name": n, "passed": p, "why": w} for n, p, w in results],
    }, indent=2) + "\n")
    return path


def command(case: Case, arm: str, model: str, prompt: str | None, mcp_config: dict[str, Any] | None = None,
            root: Path | None = None, effort: str | None = None) -> list[str]:
    """A `prompt` of None means a multi-turn case: user turns arrive as stream-json on stdin.

    `root` is the plugin tree this run reads — a snapshot when one was taken, the live checkout
    otherwise. Both the allowlisted script paths and `--plugin-dir` follow it, because the agent
    definition is read live too and is as much a mid-run moving part as the scripts are.
    """
    root = root or PLUGIN_ROOT
    mcp_config = mcp_config or {"mcpServers": {}}
    allowed = allowed_tools(root) + ([CALENDAR_TOOL] if "calendar" in mcp_config["mcpServers"] else []) + (PEER_MOCK_TOOLS if "peers" in mcp_config["mcpServers"] else [])
    cmd = [
        claude_binary(), "-p", *([prompt] if prompt is not None else []),
        "--model", model,
        "--output-format", "stream-json", "--verbose",
        "--no-session-persistence",
        "--setting-sources", "project",
        "--plugin-dir", str(root),
        "--strict-mcp-config", "--mcp-config", json.dumps(mcp_config),
        "--max-turns", str(case.spec.get("max_turns", 10)),
        "--tools", *TOOLS,
        "--allowedTools", *allowed,
        "--disallowedTools", *DISALLOWED,
    ]
    if prompt is None:
        cmd += ["--input-format", "stream-json"]
    if arm == "agent":
        cmd += ["--agent", AGENT]
    if effort:
        cmd += runtimes.get("claude").effort_tokens(effort)
    return cmd


def host_command(runtime: str, model: str, prompt: str, work: Path, root: Path,
                 mcp_config: dict[str, Any] | None = None, session_id: str | None = None,
                 persistent: bool = False, effort: str | None = None,
                 extra_write: Path | None = None) -> list[str]:
    """Build an isolated, one-turn headless command for a non-Claude CLI."""
    binary = shutil.which(runtimes.get(runtime).binary)
    if not binary:
        raise RuntimeError(f"{runtime} CLI is unavailable on PATH")
    servers = (mcp_config or {}).get("mcpServers", {})
    if servers and runtime != "codex":
        raise RuntimeError(f"{runtime} eval backend cannot inject isolated MCP mocks; choose a case without calendar or peers")
    if runtime == "codex":
        cmd = [binary, "exec"] + (["resume"] if session_id else [])
        cmd += ["--json", "--ignore-user-config", "--skip-git-repo-check", "-m", model]
        if not persistent:
            cmd += ["--ephemeral"]
        if not session_id:
            cmd += ["-C", str(work), "-s", "workspace-write"]
            if extra_write:
                cmd += ["--add-dir", str(extra_write)]
        else:
            cmd += ["-c", 'sandbox_mode="workspace-write"']
            if extra_write:
                cmd += ["-c", f"sandbox_workspace_write.writable_roots={json.dumps([str(extra_write)])}"]
        if effort:
            cmd += runtimes.get("codex").effort_tokens(effort)
        for name, server in servers.items():
            cmd += ["-c", f"mcp_servers.{name}.command={json.dumps(server['command'])}",
                    "-c", f"mcp_servers.{name}.args={json.dumps(server['args'])}"]
        return cmd + ([session_id] if session_id else []) + [prompt]
    if runtime == "cursor":
        if effort:
            raise RuntimeError("Cursor eval backend does not expose a separate reasoning effort flag")
        return [binary, "--print", "--output-format", "stream-json", "--workspace", str(work),
                "--model", model, "--sandbox", "enabled", "--force", "--trust"] + \
               (["--add-dir", str(extra_write)] if extra_write else []) + \
               (["--resume", session_id] if session_id else []) + [prompt]
    return [binary, "--print", prompt, "--output-format", "stream-json", "--model", model,
            "--sandbox"] + (["--add-dir", str(extra_write)] if extra_write else []) + \
           runtimes.get("agy").effort_tokens(effort or "") + \
           (["--conversation", session_id] if session_id else ["--new-project"])


def host_prompt(runtime: str, arm: str, prompt: str, root: Path, work: Path) -> str:
    if arm == "baseline":
        return prompt
    # The same rendered host instructions used by an installed coordinator, pinned to this run's snapshot.
    sys.path.insert(0, str(PLUGIN_ROOT))
    from start_coordinator import START, prompt as coordinator_prompt
    text = coordinator_prompt(runtime, root, work)
    text = text.rsplit("\n\n" + START, 1)[0]
    return text + "\n\nUser turn: " + prompt


def parse_host_stream(raw: str, runtime: str, model: str) -> Stream:
    """Normalize JSONL from Codex, Cursor and Antigravity into the existing grader record."""
    s = Stream(init={"model": model, "runtime": runtime})
    for line in raw.splitlines():
        if not line.strip():
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        kind = ev.get("type") or ev.get("event")
        if kind == "init":
            details = ev.get("init", {})
            if isinstance(details, dict):
                s.init.update({k: details[k] for k in ("model", "cwd") if k in details})
            if ev.get("conversation_id"):
                s.init["conversation_id"] = ev["conversation_id"]
        if kind in ("thread.started", "system"):
            s.init.update({k: ev[k] for k in ("thread_id", "session_id", "chatId", "model") if k in ev})
        if kind == "step_update":
            step = ev.get("step_update", {})
            if step.get("step_type") == "tool" and step.get("state") == "ACTIVE":
                native = step.get("tool_name", "")
                name = {"run_command": "Bash", "view_file": "Read", "grep_search": "Grep",
                        "find_by_name": "Glob", "write_file": "Write", "replace_file_content": "Edit"}.get(native, native)
                params = (step.get("tool_info") or {}).get("parameters") or {}
                s.tool_uses.append({"id": str(step.get("step_index", "")), "name": name,
                                    "input": params})
            if step.get("step_type") == "agent_response" and step.get("text_delta"):
                s.last_text += step["text_delta"]
        item = ev.get("item") or {}
        if kind in ("item.started", "item.completed") and isinstance(item, dict):
            item_type = item.get("type", "")
            if item_type in ("command_execution", "tool_call", "mcp_tool_call", "file_change") and kind == "item.started":
                name = {"command_execution": "Bash", "file_change": "Edit"}.get(item_type,
                    item.get("name") or item.get("tool_name") or item_type)
                s.tool_uses.append({"id": item.get("id", ""), "name": name,
                                    "input": item.get("arguments") or {"command": item.get("command", "")}})
            if item_type == "agent_message" and item.get("text"):
                s.last_text = item["text"]
        if kind == "assistant":
            message = ev.get("message", {})
            content = message.get("content", []) if isinstance(message, dict) else message
            if isinstance(content, str):
                s.last_text = content
            elif isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        s.last_text = block.get("text", s.last_text)
                    elif isinstance(block, dict) and block.get("type") == "tool_use":
                        s.tool_uses.append({"id": block.get("id", ""), "name": block.get("name", ""),
                                            "input": block.get("input", {})})
        if kind == "tool_call" and ev.get("subtype") == "started" and isinstance(ev.get("tool_call"), dict):
            native = ev["tool_call"]
            for key, name in (("shellToolCall", "Bash"), ("readToolCall", "Read"),
                              ("grepToolCall", "Grep"), ("globToolCall", "Glob"),
                              ("writeToolCall", "Write"), ("editToolCall", "Edit")):
                if key in native:
                    s.tool_uses.append({"id": ev.get("call_id", ""), "name": name,
                                        "input": native[key].get("args") or {}})
                    break
        elif kind in ("tool_call", "tool_use") and ev.get("subtype") != "completed":
            tool = ev.get("tool") or ev.get("name") or ""
            s.tool_uses.append({"id": ev.get("id", ""), "name": tool,
                                "input": ev.get("input") or ev.get("arguments") or {}})
        if kind == "result":
            result = ev.get("result", ev)
            if isinstance(result, dict):
                s.result = result
                s.last_text = result.get("response") or result.get("text") or s.last_text
                s.init.update({k: result[k] for k in ("thread_id", "session_id", "chatId", "conversation_id") if k in result})
            elif isinstance(result, str):
                s.result = ev
                s.last_text = result
        elif kind == "turn.completed":
            s.result = ev
        if kind in ("error", "turn.failed"):
            s.init["error"] = ev.get("message") or ev.get("error")
    return s


def host_session_id(stream: Stream) -> str | None:
    return next((str(stream.init[k]) for k in ("thread_id", "session_id", "chatId", "conversation_id")
                 if stream.init.get(k)), None)


@dataclass
class Turn:
    events: list[dict[str, Any]]
    t_start: datetime
    t_end: datetime


def has_notification(events: list[dict[str, Any]]) -> bool:
    """A background task's completion landed in this turn (`system:task_notification`)."""
    return any(e.get("type") == "system" and e.get("subtype") == "task_notification" for e in events)


def split_turns(events: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    turns, current = [], []
    for ev in events:
        current.append(ev)
        if ev.get("type") == "result":
            turns.append(current)
            current = []
    if current:
        turns.append(current)
    return turns


def drive_turns(
    cmd: list[str],
    prompts: list[str | None],
    cwd: Path,
    env: dict[str, str] | None,
    timeout: float,
    snapshot: Callable[[int], None],
    wait_seconds: float = 180,
    before_turn: Callable[[int], None] | None = None,
    close_grace: float = 60,
) -> list[Turn]:
    """Feed each prompt as a stream-json user message; a turn ends at its `result` event.

    A `None` prompt is a wait turn: send nothing and read the turn the session starts on its own
    (a background task's completion notification does this). If the previous turn already carried
    the notification (the task finished before the assistant stopped), there is nothing to wait for
    and the wait turn is skipped. If no event arrives within `wait_seconds`, driving stops there and
    later turns count as not reached. `timeout` bounds the
    whole run and raises `TimeoutError`. After stdin closes, a process still running (a background
    task) is killed after `close_grace`. `snapshot(n)` runs after turn n's result.
    """
    proc = subprocess.Popen(cmd, cwd=cwd, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    lines: queue.Queue[str | None] = queue.Queue()

    def pump() -> None:
        for line in proc.stdout:
            lines.put(line)
        lines.put(None)

    threading.Thread(target=pump, daemon=True).start()
    deadline = time.monotonic() + timeout
    turns: list[Turn] = []
    try:
        for n, prompt in enumerate(prompts, start=1):
            if prompt is None and turns and has_notification(turns[-1].events):
                continue
            if before_turn is not None:
                before_turn(n)
            t_start = datetime.now().astimezone()
            if prompt is not None:
                proc.stdin.write(json.dumps({"type": "user", "message": {"role": "user", "content": prompt}}) + "\n")
                proc.stdin.flush()
            events: list[dict[str, Any]] = []
            eof = False
            while True:
                limit = deadline - time.monotonic()
                if prompt is None and not events:
                    limit = min(limit, wait_seconds)
                if limit <= 0 and deadline - time.monotonic() <= 0:
                    raise TimeoutError(f"turn {n} timed out after {timeout}s")
                try:
                    line = lines.get(timeout=max(limit, 0.01))
                except queue.Empty:
                    if deadline - time.monotonic() <= 0:
                        raise TimeoutError(f"turn {n} timed out after {timeout}s") from None
                    return turns  # wait turn: nothing arrived
                if line is None:
                    eof = True
                    break
                if line.strip():
                    events.append(json.loads(line))
                    if events[-1].get("type") == "result":
                        break
            if events:
                turns.append(Turn(events, t_start, datetime.now().astimezone()))
                snapshot(n)
            if eof or events[-1].get("type") != "result":
                break
        proc.stdin.close()
        try:
            proc.wait(timeout=close_grace)
        except subprocess.TimeoutExpired:
            proc.kill()
    finally:
        if proc.poll() is None:
            proc.kill()
    return turns


def grade_turns(spec: dict[str, Any], turns: list[Turn], tz: str, out: Path, calls: list[dict[str, Any]]) -> list[tuple[str, bool, str]]:
    """Grade each turn spec against the turn it belongs to.

    A prompt spec takes the next turn. A wait spec (no prompt) takes the turn that carried the
    background notification: the previous turn itself when the notification landed inside it,
    otherwise the next unprompted turn. Specs with no turn grade as "turn not reached".
    """
    results = []
    ti = -1  # index of the last consumed turn
    for n, turn_spec in enumerate(spec["turns"], start=1):
        if "prompt" in turn_spec:
            ti += 1
            turn_index = ti if ti < len(turns) else None
        elif ti >= 0 and has_notification(turns[ti].events):
            turn_index = ti
        elif ti + 1 < len(turns):
            ti += 1
            turn_index = ti
        else:
            turn_index = None
        if turn_index is None:
            results.append((f"T{n}: ran", False, "turn not reached"))
            continue
        turn = turns[turn_index]
        snap = turn_index + 1
        in_turn = [c for c in calls if "at" in c and turn.t_start <= datetime.fromisoformat(c["at"]) <= turn.t_end]
        rec = RunRecord(
            stream=parse_stream(turn.events),
            t_start=turn.t_start,
            t_end=turn.t_end,
            tz=tz,
            fixture_dir=out / f"fixture-turn{snap}",
            mock_calls=in_turn,
            before_dir=out / ("fixture-before" if snap == 1 else f"fixture-turn{snap - 1}"),
        )
        for i, g in enumerate(turn_spec["graders"]):
            passed, why = grade(g, rec)
            label = g.get("name", f"{i}:{g['type']}")
            results.append((f"T{n}: {label}", passed, why))
    return results


SPAWN_RECORDER = """#!/usr/bin/env python3
import json, sys
from datetime import datetime
from zoneinfo import ZoneInfo

with open({log!r}, "a") as log:
    log.write(json.dumps({{"tool": "spawn", "argv": sys.argv[1:], "at": datetime.now(ZoneInfo({tz!r})).isoformat()}}) + "\\n")
print("spawn: recorded by the eval harness")
"""

# Logs each argv as a git call, then runs the next `git` on PATH; the grader reads the log, not shell text.
GIT_SHIM = """#!{python}
import json, os, sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

with open({log!r}, "a") as log:
    log.write(json.dumps({{"tool": "git", "argv": sys.argv[1:], "at": datetime.now(ZoneInfo({tz!r})).isoformat()}}) + "\\n")
here = Path(__file__).resolve().parent
for entry in os.environ["PATH"].split(os.pathsep):
    real = Path(entry) / "git"
    if Path(entry).resolve() != here and real.is_file() and os.access(real, os.X_OK):
        os.execv(real, [str(real), *sys.argv[1:]])
sys.exit("git: not found past the eval shim")
"""

GH_SHIM = '''#!/usr/bin/env python3
import json, sys
from pathlib import Path

args = sys.argv[1:]
with open(Path(__file__).parent / "calls.log", "a") as log:
    log.write(" ".join(["gh", *args]) + "\\n")
if args[:2] == ["issue", "create"]:
    repo = args[args.index("-R") + 1] if "-R" in args else "o/backlog"
    print(f"https://github.com/{repo}/issues/101")
elif args[:2] == ["issue", "list"]:
    issues = Path(__file__).parent / "issues.json"
    print(issues.read_text() if issues.exists() else "[]")
elif args[:2] in (["issue", "close"], ["issue", "comment"], ["pr", "merge"]):
    pass
elif args[:2] == ["pr", "list"]:
    prs = Path(__file__).parent / "prs.json"
    print(prs.read_text() if prs.exists() else "[]")
elif args[:2] == ["api", "graphql"] and "mutation" not in " ".join(args):
    graph = Path(__file__).parent / "graphql.json"
    print(graph.read_text() if graph.exists() else '{"data": {"repository": {"pullRequests": {"nodes": []}}}}')
elif args[:1] == ["api"] and len(args) > 1 and args[1].endswith("/replies"):
    print("{}")
else:
    print("gh: blocked by eval harness", file=sys.stderr)
    sys.exit(1)
'''


class _HealthHandler(BaseHTTPRequestHandler):
    """Answers the status the case named for that path and logs the probe. 404 for anything unlisted."""

    def do_GET(self) -> None:  # noqa: N802 (BaseHTTPRequestHandler's name)
        status = self.server.plan.get(self.path, 404)
        body = json.dumps({"path": self.path, "status": status}).encode()
        # The probe is logged before the response goes out: ThreadingHTTPServer serves each
        # request on its own thread, so logging after `wfile.write` lets the client's next
        # probe land its log line first and reverses the recorded call order.
        with open(self.server.log, "a") as fh:
            fh.write(json.dumps({"tool": "health", "path": self.path, "status": status,
                                 "at": datetime.now(ZoneInfo(self.server.tz)).isoformat()}) + "\n")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_a) -> None:
        return


def health_server(plan: dict[str, int], log: Path, tz: str = "America/Chicago") -> ThreadingHTTPServer:
    """A loopback server for one run. Port 0, so nothing collides; every probe lands in the calls file."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), _HealthHandler)
    server.plan, server.log, server.tz = plan, log, tz
    log.parent.mkdir(parents=True, exist_ok=True)
    log.touch()
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def stop_server(server: ThreadingHTTPServer | None) -> None:
    if server is not None:
        server.shutdown()
        server.server_close()


def write_recorder(shim_dir: Path, calls_log: Path, tz: str) -> list[str]:
    """A stand-in terminal. Returns the argv template the launcher env var carries."""
    shim_dir.mkdir(parents=True, exist_ok=True)
    recorder = shim_dir / "spawn_recorder.py"
    recorder.write_text(SPAWN_RECORDER.format(log=str(calls_log), tz=tz))
    return ["python3", str(recorder), "{type}", "{cwd}", "{title}"]


def write_shims(shim_dir: Path, prs: list | None = None, graphql: dict | None = None,
                issues: list | None = None) -> None:
    shim_dir.mkdir(parents=True, exist_ok=True)
    gh = shim_dir / "gh"
    gh.write_text(GH_SHIM)
    gh.chmod(0o755)
    if prs is not None:
        # A case's open pull requests, as `gh pr list --json` returns them.
        (shim_dir / "prs.json").write_text(json.dumps(prs))
    if issues is not None:
        # A case's backlog, as `gh issue list --state all --json ...` returns it (#221).
        (shim_dir / "issues.json").write_text(json.dumps(issues))
    if graphql is not None:
        # What `gh api graphql` answers for the case: its review threads.
        (shim_dir / "graphql.json").write_text(json.dumps(graphql))


# Answers `-lic "command -v X"` as a login shell does, from a directory of stubs and nothing else.
LOGIN_SHELL = """#!{python}
import sys
from pathlib import Path
words = sys.argv[2].split() if len(sys.argv) == 3 and sys.argv[1] == "-lic" else []
stub = Path({present!r}) / words[2] if words[:2] == ["command", "-v"] and len(words) == 3 else None
if not stub or not stub.is_file():
    sys.exit(1)
print(stub)
"""


def eval_environment(out: Path, calls_log: Path, tz: str, root: Path | None = None,
                     runtimes_present: list[str] | None = None) -> dict[str, str]:
    """Pin external issue writes to the fake CLI even if a host resets its shell PATH.

    `runtimes_present` names the only runtimes `shell_setup.resolve` finds, whatever the machine has installed:
    shims lead the real PATH and cannot take a binary off it, so a stand-in login shell answers instead."""
    shims = out / "shims"
    shims.mkdir(parents=True, exist_ok=True)
    release = root or PLUGIN_ROOT
    entry = shims / "chief-of-stuff"
    entry.write_text(f"#!{sys.executable}\nimport os, sys\n"
                     f"os.execv(sys.executable, [sys.executable, {str(release / 'chief_of_stuff.py')!r}, *sys.argv[1:]])\n")
    entry.chmod(0o755)
    git = shims / "git"
    git.write_text(GIT_SHIM.format(python=sys.executable, log=str(calls_log), tz=tz))
    git.chmod(0o755)
    shell = {}
    if runtimes_present is not None:
        present = (shims / "present").resolve()  # beside PATH, not on it: a stub must not shadow the host's own CLI
        present.mkdir(exist_ok=True)
        for name in runtimes_present:
            stub = present / runtimes.binary(name)
            stub.write_text(f"#!{sys.executable}\n")
            stub.chmod(0o755)
        standin = shims / "login_shell.py"
        standin.write_text(LOGIN_SHELL.format(python=sys.executable, present=str(present)))
        standin.chmod(0o755)
        shell = {"CHIEF_OF_STUFF_SHELL": str(standin.resolve())}
    return dict(os.environ, **shell,
                PATH=f"{shims}{os.pathsep}{os.environ['PATH']}",
                CHIEF_OF_STUFF_GH=str((shims / "gh").resolve()),
                CHIEF_OF_STUFF_RELEASE=str(release),
                CHIEF_OF_STUFF_LAUNCHER=json.dumps(write_recorder(shims, calls_log, tz)))


def run_one(case: Case, arm: str, model: str, out: Path, root: Path | None = None,
            runtime: str = "claude", effort: str | None = None) -> tuple[list[tuple[str, bool, str]], str | None, dict[str, Any]]:
    tz = case.spec.get("tz", "America/Chicago")
    ctx = context(tz, datetime.now(ZoneInfo(tz)))
    out.mkdir(parents=True, exist_ok=True)
    calls_log = out / "calendar" / "calls.jsonl"
    health = taken = None
    if "health" in case.spec:
        # Started before the fixture renders: `{{health_base}}` has to exist when file contents are substituted.
        health = health_server({str(k): int(v) for k, v in case.spec["health"].items()}, calls_log, tz)
        ctx["health_base"] = f"http://127.0.0.1:{health.server_port}"
    spec = render_value(case.spec, ctx)
    # Fixture lives outside this repo so the sidecar's own CLAUDE.md is not discovered upward.
    work = Path(tempfile.mkdtemp(prefix="cos-eval-"))
    try:
        if (case.root / "fixture").is_dir():
            render_tree(case.root / "fixture", work, ctx)
        if "repo" in spec:
            # Real git: `merge-base --is-ancestor` is the thing under test, and a fixture cannot carry a repo.
            make_repo.build(work, spec["repo"])
        write_shims(out / "shims", spec.get("gh_prs"), spec.get("gh_graphql"), spec.get("gh_issues"))
        calls_log.parent.mkdir(parents=True, exist_ok=True)
        calls_log.touch()
        env = eval_environment(out, calls_log, tz, root=root, runtimes_present=spec.get("runtimes_present"))
        env["CHIEF_OF_STUFF_WORKSPACE"] = str(work)
        try:
            start_fixture_pages(work, env, root or PLUGIN_ROOT)
        except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            return [], f"pages setup: {exc}", {"runtime": runtime}
        # After every piece of setup, so the baseline is what the agent was handed, not a part of it.
        before_snapshot(work, out / "fixture-before")
        mcp_config = None
        if "calendar" in spec:
            # Events live in the results dir, not the agent's cwd, so the calendar is reachable only through the mock.
            (out / "calendar").mkdir(parents=True, exist_ok=True)
            events = out / "calendar" / "events.json"
            events.write_text(render((case.root / spec["calendar"]).read_text(), ctx))
            calls_log.touch()
            mcp_config = calendar_mcp_config(events, calls_log, tz)
        if spec.get("pages_taken"):
            # Something that is not the pages server already holds the Board line's port.
            taken = ThreadingHTTPServer(("127.0.0.1", int(ctx["pages_port"])), BaseHTTPRequestHandler)
            threading.Thread(target=taken.serve_forever, daemon=True).start()
        if "peers" in spec:
            # Sessions file lives in the results dir; per-turn `peers` keys rewrite it before that turn.
            (out / "calendar").mkdir(parents=True, exist_ok=True)
            calls_log.touch()
            sessions = out / "peers-sessions.json"
            sessions.write_text(render((case.root / spec["peers"]).read_text(), ctx))
            mcp_config = merge_mcp(mcp_config, peers_mcp_config(sessions, calls_log, tz))
        if "turns" in spec:
            if runtime != "claude":
                return run_host_turns(spec, arm, model, out, work, env, mcp_config, calls_log, tz,
                                      runtime=runtime, root=root or PLUGIN_ROOT, effort=effort,
                                      case_root=case.root, ctx=ctx)
            return run_turns(spec, arm, model, out, work, env, mcp_config, calls_log, tz, case_root=case.root, ctx=ctx,
                             root=root, effort=effort)
        try:
            cmd = (command(case, arm, model, spec["prompt"], mcp_config=mcp_config, root=root, effort=effort)
                   if runtime == "claude" else host_command(runtime, model,
                        host_prompt(runtime, arm, spec["prompt"], root or PLUGIN_ROOT, work),
                        work, root or PLUGIN_ROOT, mcp_config, effort=effort, extra_write=out))
        except RuntimeError as exc:
            return [], str(exc), {"runtime": runtime}
        (out / "command.json").write_text(json.dumps(cmd, indent=1))
        t_start = datetime.now(ZoneInfo(tz))
        try:
            proc = subprocess.run(cmd, cwd=work, env=env, stdin=subprocess.DEVNULL, capture_output=True,
                                  text=True, timeout=spec.get("timeout_seconds", 300))
        except subprocess.TimeoutExpired:
            return [], "timeout", {}
        t_end = datetime.now(ZoneInfo(tz))
        (out / "stream.jsonl").write_text(proc.stdout)
        (out / "stderr.txt").write_text(proc.stderr)
        shutil.copytree(work, out / "fixture", dirs_exist_ok=True)
    finally:
        stop_pages(work, int(ctx["pages_port"]))
        shutil.rmtree(work, ignore_errors=True)
        stop_server(health)
        stop_server(taken)  # a daemon thread would otherwise outlive this case and serve the next one

    stream = (load_stream(out / "stream.jsonl") if runtime == "claude" else
              parse_host_stream((out / "stream.jsonl").read_text(), runtime, model))
    meta = {
        "exit": proc.returncode,
        "turns": (stream.result or {}).get("num_turns"),
        "notional_usd": (stream.result or {}).get("total_cost_usd"),
        "denials": len(stream.denied_ids), "runtime": runtime,
    }
    if proc.returncode != 0 or stream.result is None:
        detail = stream.init.get("error") or proc.stderr.strip()
        return [], f"{runtime} exit {proc.returncode}: {str(detail)[:300]}", meta
    if runtime == "claude":
        ok, detail = check_arm(stream.init, arm, agent_flag_used="--agent" in cmd and arm != "agent", needs_calendar="calendar" in spec, model=model, needs_peers="peers" in spec)
        if not ok:
            return [], f"arm check: {detail}", meta
    elif str((stream.result or {}).get("status", "SUCCESS")).upper() not in ("SUCCESS", "COMPLETED"):
        return [], f"{runtime} result: {(stream.result or {}).get('error') or stream.result}", meta
    mock_calls = [json.loads(l) for l in calls_log.read_text().splitlines() if l.strip()] if calls_log.exists() else []
    rec = RunRecord(stream=stream, t_start=t_start, t_end=t_end, tz=tz, fixture_dir=out / "fixture", mock_calls=mock_calls, before_dir=out / "fixture-before")
    results = []
    for i, g in enumerate(spec["graders"]):
        passed, why = grade(g, rec)
        results.append((g.get("name", f"{i}:{g['type']}"), passed, why))
    return results, None, meta


def run_host_turns(spec: dict[str, Any], arm: str, model: str, out: Path, work: Path,
                   env: dict[str, str], mcp_config: dict[str, Any] | None, calls_log: Path, tz: str,
                   *, runtime: str, root: Path, effort: str | None = None,
                   case_root: Path = Path("."), ctx: dict[str, str] | None = None
                   ) -> tuple[list[tuple[str, bool, str]], str | None, dict[str, Any]]:
    """Drive prompted turns through the host's explicit conversation ID."""
    completed: list[tuple[Stream, datetime, datetime]] = []
    session_id = None
    all_raw = []
    for n, turn in enumerate(spec["turns"], 1):
        if "prompt" not in turn:
            return [], f"{runtime} cannot safely wake an idle conversation for wait turn {n}", {"runtime": runtime}
        if "peers" in turn:
            (out / "peers-sessions.json").write_text(render((case_root / turn["peers"]).read_text(), ctx))
        message = host_prompt(runtime, arm, turn["prompt"], root, work) if n == 1 else turn["prompt"]
        try:
            cmd = host_command(runtime, model, message, work, root, mcp_config,
                               session_id=session_id, persistent=True, effort=effort, extra_write=out)
        except RuntimeError as exc:
            return [], str(exc), {"runtime": runtime, "turns_reached": len(completed)}
        (out / f"command-turn{n}.json").write_text(json.dumps(cmd, indent=1))
        started = datetime.now(ZoneInfo(tz))
        try:
            proc = subprocess.run(cmd, cwd=work, env=env, stdin=subprocess.DEVNULL, capture_output=True,
                                  text=True, timeout=spec.get("timeout_seconds", 300))
        except subprocess.TimeoutExpired:
            return [], f"{runtime} turn {n} timed out", {"runtime": runtime, "turns_reached": len(completed)}
        ended = datetime.now(ZoneInfo(tz))
        (out / f"stream-turn{n}.jsonl").write_text(proc.stdout)
        (out / f"stderr-turn{n}.txt").write_text(proc.stderr)
        all_raw.append(proc.stdout)
        stream = parse_host_stream(proc.stdout, runtime, model)
        if proc.returncode or stream.result is None:
            return [], f"{runtime} turn {n} failed: {stream.init.get('error') or proc.stderr.strip()[:200]}", \
                   {"runtime": runtime, "turns_reached": len(completed)}
        completed.append((stream, started, ended))
        shutil.copytree(work, out / f"fixture-turn{n}", dirs_exist_ok=True)
        if n < len(spec["turns"]):
            session_id = host_session_id(stream)
            if not session_id:
                return [], f"{runtime} returned no conversation ID after turn {n}", \
                       {"runtime": runtime, "turns_reached": len(completed)}
    (out / "stream.jsonl").write_text("".join(all_raw))
    shutil.copytree(work, out / "fixture", dirs_exist_ok=True)
    calls = [json.loads(l) for l in calls_log.read_text().splitlines() if l.strip()] if calls_log.exists() else []
    results = []
    for n, (turn_spec, (stream, started, ended)) in enumerate(zip(spec["turns"], completed), 1):
        in_turn = [c for c in calls if "at" in c and started <= datetime.fromisoformat(c["at"]) <= ended]
        rec = RunRecord(stream=stream, t_start=started, t_end=ended, tz=tz,
                        fixture_dir=out / f"fixture-turn{n}", mock_calls=in_turn,
                        before_dir=out / ("fixture-before" if n == 1 else f"fixture-turn{n - 1}"))
        for i, grader in enumerate(turn_spec["graders"]):
            passed, why = grade(grader, rec)
            label = grader.get("name", f"{i}:{grader['type']}")
            results.append((f"T{n}: {label}", passed, why))
    return results, None, {"runtime": runtime, "turns_reached": len(completed),
                            "denials": sum(len(s.denied_ids) for s, _, _ in completed)}


def run_turns(spec: dict[str, Any], arm: str, model: str, out: Path, work: Path, env: dict[str, str], mcp_config: dict[str, Any] | None, calls_log: Path, tz: str, case_root: Path = Path("."), ctx: dict[str, str] | None = None, root: Path | None = None, effort: str | None = None) -> tuple[list[tuple[str, bool, str]], str | None, dict[str, Any]]:
    """Multi-turn case: one stream-json process, graders scoped to each turn, fixture snapshot per turn."""
    cmd = command(Case("", Path(), spec), arm, model, None, mcp_config=mcp_config, root=root, effort=effort)
    (out / "command.json").write_text(json.dumps(cmd, indent=1))

    def snapshot(n: int) -> None:
        shutil.copytree(work, out / f"fixture-turn{n}", dirs_exist_ok=True)

    try:
        def before_turn(n: int) -> None:
            turn_spec = spec["turns"][n - 1]
            if "peers" in turn_spec:
                (out / "peers-sessions.json").write_text(render((case_root / turn_spec["peers"]).read_text(), ctx))

        turns = drive_turns(cmd, [t.get("prompt") for t in spec["turns"]], work, env, spec.get("timeout_seconds", 300), snapshot, wait_seconds=spec.get("wait_seconds", 180), before_turn=before_turn)
    except TimeoutError as exc:
        return [], f"timeout: {exc}", {}
    shutil.copytree(work, out / "fixture", dirs_exist_ok=True)
    (out / "stream.jsonl").write_text("".join(json.dumps(e) + "\n" for t in turns for e in t.events))
    streams = [parse_stream(t.events) for t in turns]
    meta = {
        "turns_reached": len([st for st in streams if st.result]),
        "notional_usd": round(sum((st.result or {}).get("total_cost_usd", 0) for st in streams), 4),
        "denials": sum(len(st.denied_ids) for st in streams),
    }
    if not streams or streams[0].result is None:
        return [], "claude produced no result for turn 1", meta
    ok, detail = check_arm(streams[0].init, arm, agent_flag_used="--agent" in cmd and arm != "agent", needs_calendar="calendar" in spec, model=model, needs_peers="peers" in spec)
    if not ok:
        return [], f"arm check: {detail}", meta
    calls = [json.loads(l) for l in calls_log.read_text().splitlines() if l.strip()] if calls_log.exists() else []
    return grade_turns(spec, turns, tz, out, calls), None, meta


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--runtime", choices=runtimes.NAMES, default="claude", help="CLI backend; default claude")
    ap.add_argument("--arm", choices=["baseline", "agent"], required=True)
    ap.add_argument("--model", default=None)
    ap.add_argument("--effort", choices=("low", "medium", "high", "xhigh", "max"),
                    help="reasoning effort where the selected CLI exposes it")
    ap.add_argument("--case", action="append", default=[], help="case name glob; repeatable")
    ap.add_argument("--runs", type=int, default=1)
    ap.add_argument("--golden", action="store_true",
                    help='only cases marked "golden": true; select a model supported by the chosen runtime')
    args = ap.parse_args(argv)
    if args.runtime != "claude" and args.model is None:
        ap.error("--model is required for non-Claude runtimes")
    if args.model is None:
        args.model = DEFAULT_MODEL

    cases = load_cases(args.case, golden_only=args.golden)
    if not cases:
        # Never "0 green, 0 red, exit 0". A selector that finds nothing reads exactly like a
        # clean run, which is the absent-check hole that scored a delete-and-stub 9/9.
        why = "no golden case matched" if args.golden else "no cases matched"
        print(why, file=sys.stderr)
        return 2
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    harness_error = any_fail = False
    tally = {"green": 0, "red": 0, "unmeasured": 0}
    # One copy for the whole sweep, so every case in a run exercises identical code and the results
    # dir says which. Finding 114: the allowlist and `--plugin-dir` pointed into the live checkout,
    # which silently made that tree read-only for the duration and made a mid-run edit invisible.
    snapshot = snapshot_plugin(EVALS / "results" / stamp / "plugin")
    print(f"plugin snapshot: {snapshot}")
    for case in cases:
        case_pass = True
        unmeasured = False
        for n in range(1, args.runs + 1):
            out = EVALS / "results" / stamp / case.name / args.arm / str(n)
            results, error, meta = run_one(case, args.arm, args.model, out, root=snapshot,
                                           runtime=args.runtime, effort=args.effort)
            write_verdict(out, case.name, args.arm, args.model, n, results, error, meta, runtime=args.runtime)
            print(f"\n{case.name}  arm={args.arm}  model={args.model}  run={n}  {meta}")
            if error:
                harness_error = unmeasured = True
                print(f"  HARNESS ERROR  {error}")
                continue
            for name, passed, why in results:
                print(f"  {'PASS' if passed else 'FAIL'}  {name}  — {why}")
                case_pass &= passed
        # A case that never ran is not a case that failed. One network outage killed fourteen of the
        # fifty-four in the 0.9.0 sweep — no grader ran in any of them — and the log said RED for all
        # fourteen, which reads as fourteen regressions to look for.
        if unmeasured:
            verdict, key = "UNMEASURED (no grader ran)", "unmeasured"
        elif args.arm == "baseline":
            verdict, key = ("NON-DISCRIMINATING (baseline passes)", "green") if case_pass else ("RED", "red")
        else:
            verdict, key = ("GREEN", "green") if case_pass else ("RED", "red")
            any_fail |= not case_pass
        tally[key] += 1
        print(f"=> {case.name}: {verdict}")
    print(f"\n{len(cases)} case(s): {tally['green']} green, {tally['red']} red, {tally['unmeasured']} unmeasured")
    print(f"results: {EVALS / 'results' / stamp}")
    if harness_error:
        return 2
    return 1 if any_fail else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
