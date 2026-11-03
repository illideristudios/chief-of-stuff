#!/usr/bin/env python3
"""On open or resume, check workspace settings, the Claude runtime, and Health: targets.

Probe the `Health:` targets in a workspace `## Coordinator` block and print one line each, after
one line for the file its `Settings:` line names, loaded through the settings loader, and one for the
Claude runtime when its `[models]` rotations name it: `ok` or `missing`, as the adapter finds it.

The coordinator runs this on open and on resume instead of remembering what was up an hour ago.
One invocation covers every target, so a restart costs one call rather than one per service.

    python3 probe_health.py --config /path/to/CLAUDE.md

Exit code is the number of failed checks: the targets that did not answer as expected, plus one when
the settings file cannot be loaded, plus one for missing Claude, or one when the login shell
could not be asked. A caller can branch on it without parsing. Every target gets a
line even when it fails: a missing line would read as a healthy service. No `Settings:` line is no
settings line, and a settings file that is not there is `absent, defaults`: not a fault, and not ok.
Settings that name no runtime, or did not load, get no runtimes line.
A missing or non-file settings path gets one `workspace file <name> missing (advice); <command>`
(or `not a file`, or `unreadable`) line, printed after any settings and runtimes lines that were emitted and before the targets, with or without a
`Settings:` line. Model guidance is not advised. These lines are advice only and do not affect the exit code.
A usage fault (no config at the path, a `Health:` line that is not a target) is stderr and exit 2.
"""

from __future__ import annotations

import argparse
import re
import shlex
import stat
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

sys.path.insert(0, str(Path(__file__).resolve().parent))
from md import section
import claude_adapter
from settings import ModelEntry, SettingsError, load as load_settings
from shell_setup import ShellError
from workspace import WORKSPACE_FILES, settings_path

HEALTH = re.compile(r"^\s*(?:[-*]\s*)?Health:\s*(.+?)\s*$", re.MULTILINE)
SCHEMES = ("http", "https")
TIMEOUT = 3.0
# A path up to its last name, quoted or bare: a shell fault quotes the shell's path, and a home path carries a username.
# ponytail: an unquoted path with a space keeps the words before the space; quote-aware parsing if a ShellError ever prints one bare.
PATH_HEAD = re.compile(r"""(')[^']*/|[^\s'"]*/""")


class TargetError(ValueError):
    """A `Health:` line that cannot be read as a target."""


@dataclass(frozen=True)
class Target:
    name: str
    url: str
    expect: int = 200


@dataclass(frozen=True)
class Result:
    target: Target
    status: int | None
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.status == self.target.expect


def parse_target(spec: str) -> Target:
    """`<name> <url> [expected status]`. The url must be http or https: nothing else is a probe."""
    parts = spec.split()
    if len(parts) < 2:
        raise TargetError(f"health target needs a name and a url: {spec!r}")
    name, url = parts[0], parts[1]
    if urlsplit(url).scheme not in SCHEMES:
        raise TargetError(f"health target must be http or https: {url!r}")
    expect = 200
    if len(parts) > 2:
        try:
            expect = int(parts[2])
        except ValueError as exc:
            raise TargetError(f"expected status must be a number: {parts[2]!r}") from exc
    return Target(name, url, expect)


def _targets(text: str) -> list[Target]:
    """Every `Health:` line inside the `## Coordinator` block. No block, no targets, no error."""
    return [parse_target(m.group(1)) for m in HEALTH.finditer("\n".join(section(text, "## Coordinator")))]


def check_settings(root: Path, name: str) -> tuple[str, int, dict[str, tuple[ModelEntry, ...]]]:
    """The line and fault count for the settings file `name`, loaded from `root`, and its `[models]` rotations."""
    absent = f"settings {name} absent, defaults", 0, {}
    try:
        try:
            (root / name).stat()  # distinguish absent from a path that is there but not a file (#347)
        except (FileNotFoundError, NotADirectoryError):
            return absent
        models = load_settings(root, name).models  # directory / unreadable / bad TOML → SettingsError
    except (SettingsError, ValueError, OSError) as exc:  # schema or TOML; not UTF-8; a NUL in the name; unreadable
        why = getattr(exc, "strerror", None) or str(exc)  # an OSError's own text carries the absolute path
        return f"settings {name} invalid: {' '.join(why.split()).removeprefix(f'{name}: ')}", 1, {}
    return f"settings {name} ok", 0, models


def check_runtimes(models: dict[str, tuple[ModelEntry, ...]]) -> tuple[list[str], int]:
    """Startup preflight for Claude when a rotation names it (ledger D11)."""
    if not any(entry.runtime == "claude" for rotation in models.values() for entry in rotation):
        return [], 0
    try:
        found = claude_adapter.available()
    except ShellError as exc:
        return ["runtimes not checked: " + PATH_HEAD.sub(r"\1", str(exc))], 1
    return ["runtimes claude " + ("ok" if found else "missing")], int(not found)


def missing_workspace_files(config: Path, settings: str | None) -> list[str]:
    """Advice lines for init's outputs that are not regular files; respects configured paths and installs nothing.

    `config` and (with a `Settings:` line) `settings` take their configured paths; a malformed settings name
    (ValueError) is left to `check_settings`.
    """
    root = config.parent
    paths = {"config": config, "settings": root / settings} if settings else {"config": config}
    lines = []
    for key, item in WORKSPACE_FILES.items():
        path = paths.get(key, root / item.name)
        name = str(path.relative_to(root)) if path.is_relative_to(root) else str(path)
        try:
            try:
                st = path.stat()
            except (FileNotFoundError, NotADirectoryError):
                st = path.lstat()
        except ValueError:
            continue
        except (FileNotFoundError, NotADirectoryError):
            st = None
        except OSError as exc:
            why = " ".join((exc.strerror or str(exc)).split())
            lines.append(f"workspace file {name} unreadable (advice); {why}")
            continue
        if st is not None and stat.S_ISREG(st.st_mode):
            continue
        exists = st is not None
        command = item.command.replace("{root}", shlex.quote(str(root.resolve())))
        fix = f"move it aside, then {command}" if exists else command
        lines.append(f"workspace file {name} {'not a file' if exists else 'missing'} (advice); {fix}")
    return lines


def _safe(url: str) -> str:
    """The url without query or credentials: a probe line is quoted into a tracker people read."""
    parts = urlsplit(url)
    host = parts.hostname or ""
    if parts.port:
        host = f"{host}:{parts.port}"
    return urlunsplit((parts.scheme, host, parts.path, "", ""))


def probe(target: Target, timeout: float = TIMEOUT) -> Result:
    """One GET. Any failure is a Result, never an exception: one dead service is not a dead move."""
    request = urllib.request.Request(target.url, method="GET", headers={"User-Agent": "chief-of-stuff/probe"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 (scheme checked in parse_target)
            return Result(target, response.status)
    except urllib.error.HTTPError as exc:
        return Result(target, exc.code)
    except Exception as exc:  # timeout, refused, DNS, TLS
        return Result(target, None, f"{type(exc).__name__}: {exc}")


def line(result: Result) -> str:
    where = _safe(result.target.url)
    if result.status is None:
        return f"{result.target.name} {where} unreachable ({result.detail})"
    if result.ok:
        return f"{result.target.name} {where} {result.status} ok"
    return f"{result.target.name} {where} {result.status}, expected {result.target.expect}"


def report(targets: list[Target], timeout: float = TIMEOUT) -> tuple[list[str], int]:
    results = [probe(t, timeout) for t in targets]
    return [line(r) for r in results], sum(1 for r in results if not r.ok)


DESCRIPTION = (
    "On open or resume, check workspace settings, the Claude runtime, and Health: targets.\n\n"
    "Use it when the question is that preflight: whether the Settings: file loads, whether\n"
    "Claude is on the login PATH when a [models] rotation names it, whether workspace files\n"
    "need (advice), and whether each Health: URL answers. A non-zero exit with empty stderr is\n"
    "the count of failed checks, which an agent can branch on without parsing."
)

EPILOG = """\
When not to use:
  To see which worker processes are alive, use chief-of-stuff processes
  (processes); health does not read PIDs.
  To create a file that an (advice) line names, use chief-of-stuff init
  (init); health prints the command and does not run it.
  To audit tasks against worktrees and issues, use chief-of-stuff audit
  (audit).

Output:
  Plain text lines on stdout (not TOON and not JSON; no structured fields).
  Stderr is empty unless the exit is a usage fault (exit 2). Lines are emitted
  on stdout in this order when present:

  1. Settings probe (only when a top-level "- Settings:" bullet in
     ## Coordinator names a path; a repeated key's last bullet wins; without
     that bullet no settings line is printed and the file is not read):
     - "settings <name> ok" when the file loads cleanly.
     - "settings <name> absent, defaults" when the named file is not there.
     - "settings <name> invalid: <why>" when the file cannot be read or fails
       TOML/schema validation (<why> is one line, names <name> once, and carries
       no machine-specific absolute path).

  2. Claude runtime probe (only when the settings file loaded and a [models]
     rotation names claude; checked once via claude_adapter.available() on the
     login shell PATH; only claude is checked; codex, cursor, and agy are not
     probed):
     - "runtimes claude ok" when claude is found on the login PATH.
     - "runtimes claude missing" when claude is not found.
     - "runtimes not checked: <why>" when the login shell fails (redacts
       directory prefixes from paths and says neither ok nor missing).
     No line is emitted when claude is not named or when settings did not load.

  3. Workspace file advice lines (zero or more, from WORKSPACE_FILES, printed
     whether or not ## Coordinator has a Settings: line, after any settings and
     runtimes lines and before targets):
     - "workspace file <name> missing (advice); <command>"
     - "workspace file <name> not a file (advice); move it aside, then <command>"
       (directory, broken symlink, fifo, or other non-regular file)
     - "workspace file <name> unreadable (advice); <why>"
     Each advice line starts with "workspace file <name>" and contains
     "(advice)". Advice lines are not failed checks: an (advice) line does not
     change the exit code. <name> is the configured Settings: path, or
     chief-of-stuff.toml when ## Coordinator names none; a custom path that is a
     regular file does not also advise the default name. <command> has {root}
     replaced by the shell-quoted resolved workspace directory (currently
     "chief-of-stuff init --help"). Model guidance is never advised and health
     does not install it. A missing --config file is a usage fault on stderr,
     not an advice line. A Settings: name with a NUL (ValueError) gets the
     settings invalid line and no duplicate advice line.

     Advice against failed checks and absent, defaults:
     - "settings <name> absent, defaults" is neither a failed check nor an
       advice line; nothing was validated, defaults apply, and it never says ok.
     - Missing configured settings file: "settings <name> absent, defaults" (0)
       plus "workspace file <name> missing (advice); ..." (0) -> 0 failed checks.
     - Directory or non-regular file at the Settings: path: "settings <name>
       invalid: ..." (1) plus "workspace file <name> not a file (advice); ..."
       (0) -> 1 failed check.
     - Unreadable Settings: path: "settings <name> invalid: ..." (1) plus
       "workspace file <name> unreadable (advice); <why>" (0) -> 1 failed check.

  4. Health: target probes (each "- Health: <name> <url> [status]" line inside
     ## Coordinator, in block order, or "no Health: targets in the
     ## Coordinator block"):
     - "<name> <url> <status> ok" when the HTTP GET status equals the expected
       status (default 200).
     - "<name> <url> <status>, expected <expect>" on any other HTTP status,
       including 4xx/5xx responses.
     - "<name> <url> unreachable (<ExceptionType>: <detail>)" when the request
       fails without an HTTP status (timeout, connection refused, DNS, TLS).
     Every target gets a line even when it fails. Probe requests send GET with
     "User-Agent: chief-of-stuff/probe"; the printed <url> strips query strings
     and credentials. Only http and https URLs are valid targets; a malformed
     Health: line inside ## Coordinator prints to stderr and exits 2 after steps
     1-3 have already printed to stdout.

Exit codes:
  0  No failed checks (or -h/--help). Includes settings ok, settings absent
     (absent, defaults), claude ok, claude not named, no Health: targets, all
     targets ok, and any (advice) lines. Stderr is empty.
  N  (1 or more, with empty stderr) N failed checks: +1 if settings are invalid
     or unreadable; +1 for "runtimes claude missing" or "runtimes not checked"
     (however many rotations named claude); +1 per target that is not ok.
     Advice lines and absent, defaults add 0. Exit 2 with empty stderr means two
     failed checks, not a usage fault.
  2  (with non-empty stderr) Usage fault. Any failed-check count already
     computed is discarded. Causes: argparse error (unknown flag, non-float
     --timeout); --config is not a file ("no config at <path>", stdout empty);
     --config is not valid UTF-8 ("<path>: not UTF-8 (<reason>)", stdout empty);
     a Health: line in ## Coordinator is not "<name> <http(s)-url> [int]"
     (stdout may already hold settings, runtimes, and advice lines).

Side effects:
  Reads only. No preview or --dry-run and no --commit exist, because nothing is
  written.

  Reads: the --config file; the settings file via the settings loader when
  Settings: names one; stat/lstat on WORKSPACE_FILES paths; Claude's binary on
  the login shell PATH when a [models] rotation names claude; one HTTP/HTTPS GET
  per Health: target.

  Writes: none. Not the store, not the forge, not the workspace files, not the
  probed services. Advice does not run init.

Examples:
  chief-of-stuff health --config ~/cedar-desk/CLAUDE.md
  chief-of-stuff health --config ~/cedar-desk/CLAUDE.md --timeout 1
  chief-of-stuff health
"""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="chief-of-stuff health",
        description=DESCRIPTION,
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        add_help=False,
    )
    parser.add_argument(
        "-h", "--help", action="help",
        help="flag, default: off. Print this help text on stdout and exit 0.",
    )
    parser.add_argument(
        "--config", default="CLAUDE.md",
        help="path, default: CLAUDE.md. File whose ## Coordinator block is read; its parent is the workspace root for the Settings: path and for (advice) commands.",
    )
    parser.add_argument(
        "--timeout", type=float, default=TIMEOUT,
        help=f"float seconds, default: {TIMEOUT}. Per-target HTTP GET timeout only; does not bound the settings load or the Claude login-PATH lookup (resolve uses 15 seconds).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    config = Path(args.config)
    if not config.is_file():
        print(f"no config at {config}", file=sys.stderr)
        return 2
    try:
        text = config.read_text()
    except UnicodeDecodeError as exc:
        print(f"{config}: not UTF-8 ({exc.reason})", file=sys.stderr)
        return 2
    bad = 0
    if name := settings_path(text):
        said, bad, models = check_settings(config.parent, name)
        lines, missing = check_runtimes(models)
        print("\n".join([said, *lines]))
        bad += missing
    for advice in missing_workspace_files(config, name):
        print(advice)
    try:
        targets = _targets(text)
    except TargetError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if not targets:
        print("no Health: targets in the ## Coordinator block")
        return bad
    lines, failed = report(targets, args.timeout)
    print("\n".join(lines))
    return bad + failed


if __name__ == "__main__":
    raise SystemExit(main())
