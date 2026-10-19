"""Resolve a CLI through the user's configured interactive login shell."""

from __future__ import annotations

import os
import pwd
import re
import shlex
import signal
import subprocess
import tempfile
from pathlib import Path

PROGRAM = re.compile(r"^[A-Za-z][A-Za-z0-9._-]*$")
KEEP = {"PATH", "HOME", "USER", "LOGNAME", "SHELL", "TERM", "TERM_PROGRAM", "TMPDIR",
        "LANG", "LC_ALL", "LC_CTYPE", "COLORTERM", "TZ", "SSH_AUTH_SOCK", "XDG_CONFIG_HOME",
        "CHIEF_OF_STUFF_RELEASE", "CHIEF_OF_STUFF_WORKSPACE"}

# Keep ownership until a later command can reap children that outlast bounded cleanup.
_unreaped_children: list[subprocess.Popen] = []


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


def login_path(source: dict[str, str] | None = None) -> str:
    """The login and interactive shell's PATH, or "" when it cannot be read (#871: launchd's PATH is short, E14).
    Only the final output line counts, so rc chatter before it is never taken for the PATH."""
    try:
        out = subprocess.run([login_shell(source), "-lic", 'printf "\\n%s" "$PATH"'], env=clean_env(source),
                             capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired, ShellError):
        return ""
    lines = out.stdout.splitlines()
    path = lines[-1].strip() if out.returncode == 0 and lines else ""
    return path if path and all(Path(part).is_absolute() for part in path.split(os.pathsep) if part) else ""


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


def run_command(command: str, cwd: Path, *, workspace: Path, worker: str = "",
                timeout: int = 120, worktree: str = "") -> subprocess.CompletedProcess:
    """Run workspace-authored shell source in the caller-supplied cwd with login setup.

    Teardown uses this driver and supplies the trusted workspace as cwd. Workspace, worker and
    optional worktree identity travel in the environment. Execution has its own timeout;
    cleanup allows at most one additional second to reap the shell after a failure.
    """
    # Popen.poll uses WNOHANG: a stuck child must not delay the next command either.
    _unreaped_children[:] = [child for child in _unreaped_children if child.poll() is None]
    env = {**clean_env(), "CHIEF_OF_STUFF_WORKSPACE": str(workspace.resolve()),
           "CHIEF_OF_STUFF_WORKER": worker}
    if worktree:
        env["CHIEF_OF_STUFF_WORKTREE"] = worktree
    shell = login_shell()
    kind = Path(shell).name
    if kind not in {"zsh", "bash", "sh", "dash", "ksh", "fish"}:
        raise ShellError(f"login shell {shell!r} is not supported for command execution")
    # Keep command stdout separate from shell startup, including banners from login config.
    # A file also works when shell startup closes inherited descriptors above stderr.
    with tempfile.TemporaryDirectory(prefix="chief-command-") as directory:
        output = Path(directory) / "stdout"
        quoted = shlex.quote(str(output))
        if kind == "fish":
            source = f"status job-control none; begin\n{command}\nend > {quoted}"
        else:
            source = f"exec 1>{quoted}; set +m\n{command}"
        argv = [shell, "-lic", source]
        errors = Path(directory) / "stderr"
        with errors.open("w") as stderr_file:
            # Popen.__exit__ waits without a timeout, so manage the child explicitly.
            child = subprocess.Popen(
                argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=stderr_file, start_new_session=True)
            try:
                child.wait(timeout=timeout)
            except BaseException as exc:
                # Bound command execution even when descendants outlive the shell.
                if isinstance(exc, subprocess.TimeoutExpired) or child.poll() is None:
                    try:
                        os.killpg(child.pid, signal.SIGKILL)
                    except OSError:
                        pass
                    try:
                        child.wait(timeout=1)
                    except subprocess.TimeoutExpired:
                        # SIGKILL may still be pending on an uninterruptible child. Retain it
                        # explicitly; Popen's destructor has not run while the exception holds it.
                        _unreaped_children.append(child)
                        detail = f"child pid {child.pid} not reaped after SIGKILL"
                        if isinstance(exc, subprocess.TimeoutExpired):
                            # TimeoutExpired formats its message from cmd, not args.
                            exc.cmd = f"({detail}) {exc.cmd}"
                        else:
                            exc.args = (*exc.args, detail)
                raise
            stderr = errors.read_text(errors="replace")
            stdout = output.read_text(errors="replace") if output.exists() else ""
            return subprocess.CompletedProcess(argv, child.returncode, stdout, stderr)


def teardown(root: Path, cwd: Path, name: str, command: str) -> list[str]:
    """Run cleanup independently of the worker outcome, returning nonblocking warnings."""
    if not command:
        return []

    def brief(text: str) -> str:
        return " ".join(re.sub(r"[\x00-\x1f\x7f]", " ", text).split())[:1000]

    try:
        # Always the trusted workspace. A review worktree is contributor code; a relative
        # teardown script must not run from there. The worktree path is only an environment value.
        result = run_command(command, root, workspace=root, worker=name, worktree=str(cwd.resolve()))
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
        return [f"teardown: {brief(str(exc))}"]
    if result.returncode:
        detail = brief(result.stderr.strip() or result.stdout.strip())
        return [f"teardown: exit {result.returncode}" + (f": {detail}" if detail else "")]
    return []
