"""Resolve a CLI through the user's configured interactive login shell."""

from __future__ import annotations

import os
import pwd
import re
import subprocess
from pathlib import Path

PROGRAM = re.compile(r"^[A-Za-z][A-Za-z0-9._-]*$")
KEEP = {"PATH", "HOME", "USER", "LOGNAME", "SHELL", "TERM", "TERM_PROGRAM", "TMPDIR",
        "LANG", "LC_ALL", "LC_CTYPE", "COLORTERM", "TZ", "SSH_AUTH_SOCK", "XDG_CONFIG_HOME",
        "CHIEF_OF_STUFF_RELEASE", "CHIEF_OF_STUFF_WORKSPACE"}


class ShellError(ValueError):
    pass


def clean_env(source: dict[str, str] | None = None) -> dict[str, str]:
    env = os.environ if source is None else source
    # CHIEF_* is the engine's own channel (worker guard binding, #658); nothing else of the caller's passes.
    return {key: value for key, value in env.items() if key in KEEP or key.startswith("CHIEF_")}


def login_shell(source: dict[str, str] | None = None) -> str:
    env = os.environ if source is None else source
    shell = env.get("SHELL") or pwd.getpwuid(os.getuid()).pw_shell
    if not Path(shell).is_absolute() or not os.access(shell, os.X_OK):
        raise ShellError(f"configured login shell {shell!r} is not executable")
    return shell


def resolve(program: str, *, source: dict[str, str] | None = None) -> str | None:
    """Return an executable path from login and interactive shell setup, never shell output text."""
    if not PROGRAM.fullmatch(program):
        raise ShellError(f"invalid executable name {program!r}")
    env = os.environ if source is None else source
    # CHIEF_OF_STUFF_SHELL stands in for the login shell in this lookup only, so an eval can say which programs exist. The shell a
    # launch goes through never reads it; the binary a launch runs is whatever this lookup returns, as with $SHELL.
    shell = login_shell({**env, "SHELL": env["CHIEF_OF_STUFF_SHELL"]} if env.get("CHIEF_OF_STUFF_SHELL") else source)
    try:
        out = subprocess.run([shell, "-lic", f"command -v {program}"], env=clean_env(source),
                             capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ShellError(f"could not inspect login shell for {program}: {exc}") from None
    if out.returncode != 0:
        return None
    for line in reversed(out.stdout.splitlines()):
        path = Path(line.strip())
        if path.is_absolute() and path.is_file() and os.access(path, os.X_OK):
            return str(path)
    return None


def exec_login(command: list[str]) -> None:
    """Load login and interactive config, then replace the shell with exact argv."""
    os.execve(login_shell(), login_argv(command), clean_env())


def login_argv(command: list[str], *, path_prefix: str = "") -> list[str]:
    """Pass worker arguments as shell positional values, never as shell source text."""
    shell = login_shell()
    kind = Path(shell).name
    if kind not in {"zsh", "bash", "sh", "dash", "ksh", "fish"}:
        raise ShellError(f"login shell {shell!r} is not supported for exact argv execution")
    if kind == "fish":
        source = 'set -gx PATH $argv[1] $PATH; set -e argv[1]; exec $argv' if path_prefix else 'exec $argv'
        return [shell, "-lic", source, *([path_prefix] if path_prefix else []), *command]
    source = 'export PATH="$1:$PATH"; shift; exec "$@"' if path_prefix else 'exec "$@"'
    return [shell, "-lic", source, "chief-of-stuff", *([path_prefix] if path_prefix else []), *command]
