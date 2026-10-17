#!/usr/bin/env python3
"""Probe the `Health:` targets in a workspace `## Coordinator` block and print one line each, after
one line for the file its `Settings:` line names, loaded through the settings loader, and one for the
runtimes its `[models]` rotations name: each `ok` or `missing`, as the login shell finds its binary.

The coordinator runs this on open and on resume instead of remembering what was up an hour ago.
One invocation covers every target, so a restart costs one call rather than one per service.

    python3 probe_health.py --config /path/to/CLAUDE.md

Exit code is the number of failed checks: the targets that did not answer as expected, plus one when
the settings file cannot be loaded, plus one for each missing runtime, or one when the login shell
could not be asked. A caller can branch on it without parsing. Every target gets a
line even when it fails: a missing line would read as a healthy service. No `Settings:` line is no
settings line, and a settings file that is not there is `absent, defaults`: not a fault, and not ok.
Settings that name no runtime, or did not load, get no runtimes line.
A missing or non-file settings path gets one `workspace file <name> missing (advice); <command>`
(or `not a file`, or `unreadable`) line, printed after the runtimes line and before the targets, with or without a
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
from runtimes import RUNTIMES
from settings import ModelEntry, SettingsError, load as load_settings
from shell_setup import ShellError, resolve
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
    """The line and fault count for the runtimes the rotations name; no line when they name none."""
    named = [r for r in RUNTIMES if any(entry.runtime == r.name for rotation in models.values() for entry in rotation)]
    try:
        missing = [r for r in named if not resolve(r.binary)]
    except ShellError as exc:
        return ["runtimes not checked: " + PATH_HEAD.sub(r"\1", str(exc))], 1
    said = [f"{r.name} ok" if r not in missing else
            f"{r.name} missing" + (f" (binary {r.binary})" if r.binary != r.name else "") for r in named]
    return ["runtimes " + ", ".join(said)] if said else [], len(missing)


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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check settings, runtimes and Health: targets; advise about a missing settings file (exit code unaffected).")
    parser.add_argument("--config", default="CLAUDE.md", help="workspace CLAUDE.md holding the block")
    parser.add_argument("--timeout", type=float, default=TIMEOUT, help="seconds per target")
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
