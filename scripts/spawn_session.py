#!/usr/bin/env python3
"""Start a worker in a worktree using fixed argv templates.

Launch templates live here because workers may edit workspace configuration. The eval harness
may override them with `CHIEF_OF_STUFF_LAUNCHER`. Token substitution never re-splits argv.
This module does not stop sessions or delete worktrees or branches.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import dispatch_prompt  # noqa: E402
import runtimes  # noqa: E402
from settings import HERDR_NAME, SettingsError, load as load_settings  # noqa: E402
from shell_setup import ShellError, resolve  # noqa: E402

ENV = "CHIEF_OF_STUFF_LAUNCHER"
# Keep assignments and stop files in the worktree's ignored metadata directory.
PROMPT_DIR = dispatch_prompt.PROMPT_DIR
PROMPT_FILE = dispatch_prompt.DISPATCH_FILE
# Use absolute paths: Ghostty can ignore its configured working directory.
BOOTSTRAP = ("Read the file {dispatch} — that is your assignment, in full, and read it before you do "
             "anything else. Your working directory is {cwd}. Start there and stay there: every path "
             "the assignment names is inside it.")
# Pin cwd in argv because the terminal may ignore its configured directory.
ENV_BIN = "/usr/bin/env"
# Enforce planning at launch and pass the assigned session name explicitly.
CLAUDE_ARGV = ["claude", "--agent", "{type}", "--name", "{title}", "--model", "{model}",
               *runtimes.get("claude").effort_argv, "--permission-mode", "plan", BOOTSTRAP]
# Antigravity gets its name from the tab and assignment and reads workspace rules explicitly.
AGY_BOOTSTRAP = BOOTSTRAP + " Then read {root}/CLAUDE.md: its house rules bind you."
AGY_ARGV = ["agy", "--model", "{model}", *runtimes.get("agy").effort_argv, "--mode", "plan", "-i", AGY_BOOTSTRAP]
CODEX_ARGV = ["codex", "-C", "{cwd}", "--sandbox", "read-only", "--model", "{model}",
              *runtimes.get("codex").effort_argv, AGY_BOOTSTRAP]
CURSOR_ARGV = ["agent", "--workspace", "{cwd}", "--mode", "plan", "--model", "{model}", AGY_BOOTSTRAP]
TEMPLATES = {"claude": CLAUDE_ARGV, "agy": AGY_ARGV, "codex": CODEX_ARGV, "cursor": CURSOR_ARGV}
MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
# The eval harness may override this template; normal launches use Ghostty tabs.
DEFAULT_LAUNCHER = ["ghostty", "--working-directory={cwd}", "--title={title}", "-e", *CLAUDE_ARGV]
CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")
AS_ESCAPE = str.maketrans({'"': '\\"', "\\": "\\\\"})
# Keep shell essentials and SSH auth, but exclude the coordinator's CLAUDE_* identity.
KEEP = ("PATH", "HOME", "USER", "LOGNAME", "SHELL", "TERM", "TERM_PROGRAM", "TMPDIR",
        "LANG", "LC_ALL", "LC_CTYPE", "COLORTERM", "TZ", "SSH_AUTH_SOCK", "XDG_CONFIG_HOME",
        "CHIEF_OF_STUFF_RELEASE", "CHIEF_OF_STUFF_WORKSPACE")
OSASCRIPT = "/usr/bin/osascript"
SHELLS = {"sh", "bash", "zsh", "dash", "ksh", "fish", "env", "eval", "exec", "xargs"}
METACHARACTERS = re.compile(r"[;&|`$<>\n]")
PLACEHOLDER = re.compile(r"\{(\w+)\}")
FIELDS = ("cwd", "title", "type", "dispatch", "model", "effort", "root")
HERDR_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9:_-]{0,63}")
# The pane's tty cuts a typed line near 1024 bytes; refuse well before that.
HERDR_LINE_LIMIT = 900
# `pane run` confirms delivery, not execution: the start is real only when the worker's registration
# changes on disk (#551). A cut line never registers; a slow login shell gets a full CLI timeout to boot.
HERDR_REGISTER_WAIT = 30.0
HERDR_REGISTER_POLL = 0.25


class RefusedError(ValueError):
    """Invalid launch configuration; no session started."""


def launcher(template: list[str] | None = None) -> list[str]:
    """Return the configured argv template."""
    if template is None:
        raw = os.environ.get(ENV)
        if raw:
            try:
                template = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise RefusedError(f"{ENV} must be a JSON array of argv tokens: {exc}") from None
            if not isinstance(template, list) or not all(isinstance(t, str) for t in template):
                raise RefusedError(f"{ENV} must be a JSON array of strings")
        else:
            template = list(DEFAULT_LAUNCHER)
    if not template:
        raise RefusedError("launcher is empty: there is nothing to run")
    argv0 = Path(template[0]).name
    if argv0 in SHELLS:
        raise RefusedError(f"launcher runs {argv0!r}, which would interpret its arguments; give the program directly")
    for token in template:
        if METACHARACTERS.search(token):
            raise RefusedError(f"launcher token {token!r} carries a shell metacharacter")
    return template


def _without(template: list[str], field: str) -> list[str]:
    """Remove an unset field and its flag without changing argv token boundaries: a token that is itself a flag
    (`--model={model}`) goes alone; a value goes with the flag before it."""
    mark = "{" + field + "}"
    out = []
    for token in template:
        if mark not in token:
            out.append(token)
        elif not token.startswith("-") and out and out[-1].startswith("-"):
            out.pop()
    return out


def _paths(cwd: str) -> tuple[str, str]:
    """Return absolute cwd and dispatch paths, preserving symlink spelling."""
    root = Path(os.path.abspath(cwd))
    return str(root), str(root / PROMPT_FILE)


def _unset(template: list[str], values: dict[str, str]) -> list[str]:
    """Drop each optional flag whose value is empty, as a pair (see `_without`)."""
    for field in ("type", "title", "model", "effort"):
        if not values.get(field):
            template = _without(template, field)
    return template


def argv(template: list[str], *, agent_type: str | None, cwd: str, title: str, model: str = "", effort: str = "",
         workspace: str = ".") -> list[str]:
    """Substitute per whole token. A value never becomes more argv entries than the token it fills."""
    root, dispatch = _paths(cwd)
    values = {"cwd": root, "title": title, "type": agent_type or "", "dispatch": dispatch,
              "model": model, "effort": effort, "root": os.path.abspath(workspace)}
    template = _unset(template, values)
    out = []
    for token in template:
        unknown = [name for name in PLACEHOLDER.findall(token) if name not in values]
        if unknown:
            raise RefusedError(f"launcher token {token!r} uses {unknown[0]!r}; known fields are {', '.join(FIELDS)}")
        out.append(PLACEHOLDER.sub(lambda m: values[m.group(1)], token))
    return out


def _as_string(value: str) -> str:
    """Quote one AppleScript string and reject control characters."""
    if CONTROL.search(value):
        raise RefusedError(f"{value!r} carries a control character; nothing was started")
    return '"' + value.translate(AS_ESCAPE) + '"'


def worker_tokens(*, cwd: str, agent_type: str | None, binary: Path | None, title: str | None = None,
                  runtime: str = "claude", model: str = "", effort: str = "", workspace: str = ".",
                  claude_as_profile: str = "") -> list[str]:
    """The worker argv from `sys.executable` on, without the `env -C <root> PATH=... WORKSPACE=...` prefix."""
    program, lead = runtimes.program(runtime, claude_as_profile)
    if binary is None:
        raise RefusedError(f"cannot find `{program}` on PATH")
    root, dispatch = _paths(cwd)
    values = {"cwd": root, "title": title or "", "type": agent_type or "", "dispatch": dispatch,
              "model": model, "effort": effort, "root": os.path.abspath(workspace)}
    template = _unset(TEMPLATES[runtime], values)
    rest = [PLACEHOLDER.sub(lambda m: values.get(m.group(1), m.group(0)), t) for t in template[1:]]
    if runtime == "claude":
        rest = ["--plugin-dir", str(Path(__file__).resolve().parent.parent), *rest]
        tokens = [sys.executable, str(Path(__file__).resolve().parent / "session_exec.py"),
                  "exec-login", "--"]
    else:
        tokens = [sys.executable, str(Path(__file__).resolve().parent / "session_exec.py"),
                  "--registry", str(Path(os.path.abspath(workspace)) / PROMPT_DIR / "sessions" / f"{title}.json"),
                  "--runtime", runtime, "--name", title or "", "--worktree", root,
                  "--login-shell", "--"]
    return tokens + [str(binary)] + lead + rest


def runtime_tokens(*, cwd: str, workspace: str = ".", **worker) -> list[str]:
    """Exact worker argv shared by terminal launchers: cwd and PATH pinned inline by `env`."""
    return [ENV_BIN, "-C", _paths(cwd)[0], f"PATH={os.environ.get('PATH', '')}",
            f"CHIEF_OF_STUFF_WORKSPACE={os.path.abspath(workspace)}",
            *worker_tokens(cwd=cwd, workspace=workspace, **worker)]


def tmux_command(*, tmux: Path | None, **worker) -> list[str]:
    """Open a new tmux window in an existing tmux session, with argv executed directly."""
    if tmux is None:
        raise RefusedError("tmux launcher is configured but tmux is unavailable on PATH")
    tokens = runtime_tokens(**worker)
    return [str(tmux), "new-window", "-d", "-c", os.path.abspath(worker["cwd"]),
            "-n", worker.get("title") or worker["runtime"], *tokens]


def herdr_commands(*, herdr: Path | None, session: str, **worker) -> tuple[list[str], list[str]]:
    """Create a tab, then run the shared worker wrapper through its root pane's shell."""
    if herdr is None:
        raise RefusedError("herdr launcher is configured but herdr is unavailable on PATH")
    if not HERDR_NAME.fullmatch(session):
        raise RefusedError("[workers] herdr_session must match [a-z0-9][a-z0-9_-]{0,31}")
    title = worker.get("title")
    prefix = [str(herdr), "--session", session]
    tokens = worker_tokens(**worker)
    if tokens[2:3] == ["exec-login"]:
        # The launcher verifies the worker's registration after `pane run` (#551), so through herdr the
        # claude wrapper registers too; tmux and Ghostty keep their exact argv.
        registry = Path(os.path.abspath(worker.get("workspace", "."))) / PROMPT_DIR / "sessions" / f"{title}.json"
        tokens = tokens[:3] + ["--registry", str(registry), "--name", title or ""] + tokens[3:]
    # The tab is created in the worktree with the workspace in its env, so the typed line skips `env -C`.
    line = [shlex.quote(token) for token in tokens]
    size = len(" ".join(line).encode())
    if size > HERDR_LINE_LIMIT:
        raise RefusedError(f"herdr pane line is {size} bytes, over the {HERDR_LINE_LIMIT}-byte limit: "
                           "the pane shell cannot take a line that long")
    return ([*prefix, "tab", "create", "--cwd", os.path.abspath(worker["cwd"]),
             "--label", title, "--no-focus",
             "--env", f"CHIEF_OF_STUFF_WORKSPACE={os.path.abspath(worker.get('workspace', '.'))}"],
            [*prefix, "pane", "run", "<root-pane>", *line])


def _unquote(word: str) -> str:
    """The exact inverse of shlex.quote for one typed word: strip its single quotes and
    restore the quote character from its escape sequence."""
    if len(word) >= 2 and word.startswith("'") and word.endswith("'"):
        marker = "'" + chr(34) + "'" + chr(34) + "'"  # how shlex.quote escapes a quote
        return word[1:-1].replace(marker, "'")
    return word


def _registry_from(pane_run: list[str]) -> Path | None:
    """The registration file the pane command itself declares (#551): the token after `--registry`.
    The pane command's words are this module's own shlex.quote output, so the value comes back
    without re-tokenizing the line (token substitution never re-splits argv)."""
    words = pane_run[6:]  # [herdr, --session, session, pane, run, pane-id, ...quoted line]
    for at, word in enumerate(words[:-1]):
        if word == "--registry":
            return Path(_unquote(words[at + 1]))
    return None


def _registered_state(registry: Path | None) -> bytes | None:
    """The registration on disk now, or None when there is none or it cannot be read."""
    if registry is None:
        return None
    try:
        return registry.read_bytes() if registry.is_file() else None
    except OSError:
        return None


def wait_for_registration(registry: Path | None, before: bytes | None) -> None:
    """#551: a line the pane's shell cut never registers, so an unchanged file is a failed start.
    A registration left by an earlier worker does not vouch: the file must change."""
    if registry is None:
        return
    deadline = time.monotonic() + HERDR_REGISTER_WAIT
    while _registered_state(registry) == before:
        if time.monotonic() >= deadline:
            raise RefusedError(f"the worker never registered its start (no new {registry.name} within "
                               f"{HERDR_REGISTER_WAIT:g}s); herdr may have cut the command at the pane's shell")
        time.sleep(HERDR_REGISTER_POLL)


def _require_session(create: list[str], run) -> None:
    """#552: herdr has no `session create` (v0.9.3 CLI reference); a session exists only once
    `herdr --session <name>` launched it. Check the roster before tab create, so a start against a
    session that was never begun refuses naming the setting and the way back, instead of herdr's
    socket error. A probe that fails or cannot be read is never a no; tab create reports that."""
    try:
        out = run([*create[:3], "session", "list", "--json"], "session list")
    except RefusedError:
        return
    try:
        reply = json.loads(out.stdout)
    except (ValueError, RecursionError):
        return
    if isinstance(reply, dict):
        holder = reply.get("result")
        holder = holder if isinstance(holder, dict) else reply
        roster = holder.get("sessions")
    else:
        roster = reply
    if not isinstance(roster, list):
        return
    names = set()
    for entry in roster:
        if isinstance(entry, str):
            names.add(entry)
        elif isinstance(entry, dict):
            name = next((entry[key] for key in ("name", "session", "session_id")
                         if isinstance(entry.get(key), str)), None)
            if name is not None:
                names.add(name)
    session = create[2]
    if session in names:
        return
    raise RefusedError(f"no running herdr session is named {session!r}: herdr has no `session create`, so "
                       f"start it once with `herdr --session {session}` and leave it running, or set "
                       "[workers] herdr_session to a session that is running (`herdr session list` shows them)")


def launch_herdr(create: list[str], pane_run: list[str]) -> None:
    """Treat CLI output as data; close the created tab if the pane command fails."""
    env = {k: v for k, v in launch_env().items() if not k.upper().startswith("HERDR_")}

    def run(command: list[str], action: str, *, timeout: float = 30):
        try:
            out = subprocess.run(command, text=True, capture_output=True, timeout=timeout, env=env)
        except subprocess.TimeoutExpired:
            raise RefusedError(f"herdr {action} timed out") from None
        except (OSError, UnicodeError) as exc:
            raise RefusedError(f"herdr {action} failed: {str(exc)[:1000]}") from None
        if out.returncode != 0:
            detail = " ".join(CONTROL.sub("", out.stderr[:1000]).split())
            raise RefusedError(f"herdr {action} failed (exit {out.returncode}): {detail}")
        return out

    _require_session(create, run)
    registry = _registry_from(pane_run)
    before = _registered_state(registry)
    out = run(create, "tab create")
    try:
        reply = json.loads(out.stdout)
    except (ValueError, RecursionError):
        raise RefusedError("herdr tab create returned invalid JSON; no root pane") from None
    result = reply.get("result") if isinstance(reply, dict) else None
    if not isinstance(result, dict):
        raise RefusedError("herdr tab create JSON has no result containing a root pane")
    tab = result.get("tab")
    tab = tab.get("tab_id") if isinstance(tab, dict) else None
    if not isinstance(tab, str) or not HERDR_TOKEN.fullmatch(tab):
        raise RefusedError("herdr tab create JSON has no valid tab id")
    try:
        pane = result.get("root_pane")
        pane = pane.get("pane_id") if isinstance(pane, dict) else None
        if not isinstance(pane, str) or not HERDR_TOKEN.fullmatch(pane):
            raise RefusedError("herdr tab create JSON has no valid root pane id")
        command = list(pane_run)
        command[5] = pane
        run(command, "pane run")
        wait_for_registration(registry, before)
    except Exception:
        try:
            run([*create[:3], "tab", "close", tab], "tab close")
        except RefusedError as exc:
            print(f"cleanup: {exc}", file=sys.stderr)
        raise


def ghostty_script(*, cwd: str, agent_type: str | None, claude: Path | None, title: str | None = None,
                   runtime: str = "claude", model: str = "", effort: str = "", workspace: str = ".",
                   claude_as_profile: str = "") -> str:
    """Build a Ghostty tab script with an absolute binary and the launching shell's PATH."""
    tokens = runtime_tokens(cwd=cwd, agent_type=agent_type, binary=claude, title=title, runtime=runtime,
                            model=model, effort=effort, workspace=workspace, claude_as_profile=claude_as_profile)
    command = " ".join(shlex.quote(tok) for tok in tokens)
    # Surface configuration has no title field.
    named = (f'  perform action {_as_string("set_tab_title:" + title)} on focused terminal of tb\n'
             if title else "")
    return (
        'tell application "Ghostty"\n'
        "  activate\n"
        "  set cfg to new surface configuration\n"
        f"  set initial working directory of cfg to {_as_string(cwd)}\n"
        f"  set command of cfg to {_as_string(command)}\n"
        # Keep startup failures visible.
        "  set wait after command of cfg to true\n"
        "  if (count of windows) is 0 then\n"
        "    set tb to selected tab of (new window with configuration cfg)\n"
        "  else\n"
        # Target the front window explicitly.
        "    set tb to new tab in front window with configuration cfg\n"
        "  end if\n"
        + named +
        "end tell\n"
    )


def launch_env(parent: dict[str, str] | None = None) -> dict[str, str]:
    """Keep only approved environment variables; exclude coordinator identity."""
    source = os.environ if parent is None else parent
    return {k: v for k, v in source.items() if k in KEEP and not k.upper().startswith("CLAUDE")}


EFFORTS = ("", "low", "medium", "high", "xhigh", "max")
CHECK_TREE = Path("<worktree>")  # --check makes no tree; the one-shot assignment still names one


def assignment(args, one_shot: bool, worktree: Path | None, *, name: str | None = None) -> str:
    """The assignment a dispatch composes from its flags: one path for a launch and for --check."""
    return dispatch_prompt.compose(Path(args.root), args.date, args.task, worktree=worktree, coordinator=args.coordinator,
                                   name=args.title if name is None else name, runtime=args.runtime, one_shot=one_shot)


def main(argv_in: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--type", dest="agent_type", help="an agent type from the Coordinator block; omitted runs the default agent")
    ap.add_argument("--cwd", help="the worktree the session starts in")
    ap.add_argument("--name", "--title", dest="title",
                    help="the session's name, e.g. impl07; the listing, the prompt box and the tab all show it")
    ap.add_argument("--task", required=True, help="the Tasks row this dispatch owns, by its item or its name; the assignment is read back from it")
    ap.add_argument("--root", default=".", help="workspace root holding CLAUDE.md")
    ap.add_argument("--coordinator", help="your own session name as a listing shows it, so the session knows who to register with")
    ap.add_argument("--date", help="YYYY-MM-DD; default: today in the workspace timezone")
    ap.add_argument("--runtime", choices=sorted(runtimes.NAMES),
                    help="claude (default), agy, codex, or cursor")
    ap.add_argument("--class", dest="model_class",
                    help="a [models] task class; with no --model or --runtime, launch its suggested entry")
    ap.add_argument("--launcher", choices=("ghostty", "tmux", "herdr"),
                    help="terminal launcher; default: [workers] launcher in workspace settings, then ghostty")
    ap.add_argument("--model", default="", help="model id; required for agy")
    ap.add_argument("--effort", default="",
                    help="one of " + ", ".join(e for e in EFFORTS if e) + "; not supported for " + ", ".join(r.name for r in runtimes.RUNTIMES if not r.takes_effort))
    ap.add_argument("--dry-run", action="store_true", help="print the argv and start nothing")
    ap.add_argument("--check", action="store_true",
                    help="print the assignment the task row composes, or the refusal; checks the Tasks and File ownership rows only: "
                         "needs no --cwd, makes no worktree, starts nothing and writes nothing; "
                         "--name checks a standing row, --one-shot a one-shot task; other launch flags are ignored")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--one-shot", action="store_true",
                      help="run this task once; [workers] mode = one-shot already makes this the default")
    # #188: the user's direct request for a standing session wins over [workers] mode = one-shot.
    mode.add_argument("--interactive", action="store_true",
                      help="start a standing session for this task even in a one-shot workspace")
    ap.add_argument("--timeout-minutes", type=int, default=60,
                    help="one-shot run limit before human review (default: 60)")
    args = ap.parse_args(argv_in)
    missing = [flag for flag, value in (("--cwd", args.cwd), ("--name", args.title)) if not value]
    if missing and not args.check:
        ap.error("the following arguments are required: " + ", ".join(missing))
    try:
        config = dispatch_prompt.config(Path(args.root))
        settings = load_settings(Path(args.root), config.settings_path)
        args.task = dispatch_prompt.resolve_task(Path(args.root), args.date, args.task)
    except (dispatch_prompt.RefusedError, SettingsError, OSError) as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 1
    worker_settings = settings.workers
    one_shot = args.one_shot or (worker_settings.mode == "one-shot" and not args.interactive)
    if args.check:
        # The launch-only flags below (--runtime, --model, --effort, --class, --coordinator) are no part of the rows.
        args.coordinator, args.runtime = None, "claude"
        try:
            sys.stdout.write(assignment(args, one_shot, Path(args.cwd) if args.cwd else CHECK_TREE if one_shot else None))
        except dispatch_prompt.RefusedError as exc:
            print(f"refused: {exc}", file=sys.stderr)
            return 1
        return 0
    if args.effort not in EFFORTS:
        ap.error(f"argument --effort: invalid choice: {args.effort!r} (choose from {', '.join(map(repr, EFFORTS))})")
    try:
        if args.model_class and not (args.model or args.runtime):
            # #111: explicit flags always win; otherwise the class's first entry, effort included.
            if args.model_class not in settings.models:
                raise SettingsError(f"no [models.{args.model_class}] in the Settings TOML")
            entry = settings.models[args.model_class][0]
            args.runtime, args.model = entry.runtime, entry.model
            args.effort = args.effort or entry.effort or ""  # an entry's own effort was checked at load
    except SettingsError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 1
    args.runtime = args.runtime or "claude"
    runtime = runtimes.get(args.runtime)
    if (args.model or runtime.needs_model) and not MODEL.fullmatch(args.model):
        print(f"refused: --model needs a model id (agy: one from `agy models`, and it is required), not {args.model!r}", file=sys.stderr)
        return 1
    if args.effort and not runtime.takes_effort:
        print(f"refused: --effort is not supported for {runtime.name}", file=sys.stderr)
        return 1
    # Compose and launch must use the same absolute cwd.
    args.cwd = os.path.abspath(args.cwd)

    if one_shot:
        if args.timeout_minutes < 1:
            print("refused: --timeout-minutes must be positive", file=sys.stderr)
            return 1
        if args.launcher:
            print("refused: --launcher applies to interactive sessions, not --one-shot", file=sys.stderr)
            return 1
        try:
            # Local on purpose: it rebinds the `one_shot` flag above to the module, and an interactive
            # launch never loads the runner.
            import one_shot
            return one_shot.run(root=Path(args.root).resolve(), day=args.date, task=args.task,
                                cwd=Path(args.cwd), name=args.title, runtime=args.runtime,
                                agent_type=args.agent_type, model=args.model, effort=args.effort,
                                dry_run=args.dry_run, timeout_minutes=args.timeout_minutes)
        except (ValueError, OSError, dispatch_prompt.RefusedError, ShellError, SettingsError) as exc:
            print(f"refused: {exc}", file=sys.stderr)
            return 1

    override = os.environ.get(ENV)
    try:
        selected = args.launcher or worker_settings.launcher
        body = assignment(args, False, Path(args.cwd))
        # The eval harness uses the argv override.
        worker = dict(cwd=args.cwd, agent_type=args.agent_type, title=args.title, runtime=args.runtime,
                      model=args.model, effort=args.effort, workspace=args.root,
                      claude_as_profile=worker_settings.claude_as_profile)
        program = runtimes.program(args.runtime, worker_settings.claude_as_profile)[0]
        if override:
            command = argv(launcher(), agent_type=args.agent_type, cwd=args.cwd, title=args.title,
                           model=args.model, effort=args.effort, workspace=args.root)
            script = None
        elif selected == "tmux":
            command = tmux_command(tmux=Path(p) if (p := resolve("tmux")) else None,
                                   binary=Path(p) if (p := resolve(program)) else None,
                                   **worker)
            script = None
        elif selected == "herdr":
            command, pane_command = herdr_commands(
                herdr=Path(p) if (p := resolve("herdr")) else None,
                session=worker_settings.herdr_session,
                binary=Path(p) if (p := resolve(program)) else None, **worker)
            script = None
        else:
            command = [OSASCRIPT, "-"]
            script = ghostty_script(claude=Path(p) if (p := resolve(program)) else None,
                                   **worker)
    except (RefusedError, dispatch_prompt.RefusedError, SettingsError, ShellError) as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 1

    if args.dry_run:
        # Keep each argv token intact if the preview is copied into a shell.
        print("would run: " + " ".join(shlex.quote(t) for t in command))
        if not override and selected == "herdr":
            print("would run: " + " ".join(shlex.quote(t) for t in pane_command))
        if script:
            print("would ask Ghostty for a tab:")
            print(script.rstrip())
        print(f"would write: {Path(args.cwd) / PROMPT_FILE}")
        return 0
    if not Path(args.cwd).is_dir():
        print(f"refused: no directory at {args.cwd}; make the worktree first", file=sys.stderr)
        return 1
    try:
        written = dispatch_prompt.write_dispatch(Path(args.cwd), body)
    except dispatch_prompt.RefusedError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 1
    try:
        if override:
            proc = subprocess.Popen(command, cwd=args.cwd, env=launch_env(), start_new_session=True)
            where = f"pid {proc.pid}"
        elif selected == "tmux":
            out = subprocess.run(command, text=True, capture_output=True, timeout=30, env=launch_env())
            if out.returncode != 0:
                print(f"refused: tmux would not open a window: {out.stderr.strip()}", file=sys.stderr)
                written.unlink(missing_ok=True)
                return 1
            where = "a new tmux window"
        elif selected == "herdr":
            launch_herdr(command, pane_command)
            where = f"a new herdr tab in session {worker_settings.herdr_session}"
        else:
            # Pass AppleScript on stdin to avoid another quoting layer.
            out = subprocess.run(command, input=script, text=True, capture_output=True,
                                 timeout=30, env=launch_env())
            if out.returncode != 0:
                print(f"refused: Ghostty would not open a tab: {out.stderr.strip()}", file=sys.stderr)
                written.unlink(missing_ok=True)
                return 1
            where = "a new Ghostty tab"
    except subprocess.TimeoutExpired as exc:
        # Keep the dispatch: a tab may have opened and be reading it.
        print(f"refused: could not start {command[0]}: {exc}", file=sys.stderr)
        return 1
    except (OSError, ValueError) as exc:
        print(f"refused: could not start {command[0]}: {exc}", file=sys.stderr)
        written.unlink(missing_ok=True)
        return 1
    print(f"started {args.agent_type or 'the default agent'}{f' on agy {args.model}' if args.runtime == 'agy' else ''} in {args.cwd} as {args.title} "
          f"({where}), assignment in {written}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
