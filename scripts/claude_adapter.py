"""The `claude -p --output-format json` adapter behind the model port (design 7a; ledger E17).

Nothing else in 1.0 spawns `claude` or parses its JSON. The executable and effort flag come from the runtime
registry (E13), `claude-as run <profile>` under a claude-as profile (E12), found and run through the user's login
shell (E14: launchd's PATH is short). An answer is judged as the M0 spike measured it: exit code, `is_error` and
parseability together, never `subtype` alone.
"""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

import runtimes
from model_port import Failure, Reply, Request, quota_reset
from shell_setup import ShellError, clean_env, login_argv, resolve

RUNTIME = runtimes.get("claude")


class ClockError(ShellError):
    """The host is present and the worker clock is not usable. Not a missing binary."""


def command(request: Request, claude_as_profile: str = "", *, check_clock: bool = True) -> list[str]:
    """Resolve the host, refuse an unusable worker clock, and build exact login argv."""
    if request.session_id and not request.resume and not _is_uuid(request.session_id):
        raise ShellError(f"session_id {request.session_id!r} must be a UUID to start a session")
    binary, lead = _host(claude_as_profile)
    if request.worker and check_clock:
        _check_clock(binary, lead, request.cwd)
    return login_argv(argv(request, binary, lead))


def require_worker_clock(claude_as_profile: str = "", cwd: str | None = None) -> None:
    """Refuse before a worker launch creates a run directory or claims a slot."""
    binary, lead = _host(claude_as_profile)
    _check_clock(binary, lead, cwd)


def _host(claude_as_profile: str) -> tuple[str, list[str]]:
    program, lead = runtimes.program(RUNTIME.name, claude_as_profile)
    binary = resolve(program)
    if not binary:
        why = f"; claude_as_profile {claude_as_profile!r} needs it" if claude_as_profile else ""
        raise ShellError(f"`{program}` is not on the login shell's PATH{why}")
    return binary, lead


def _check_clock(binary: str, lead: list[str], cwd: str | None) -> None:
    """Unbilled preflight in the worker's account. Workers load user settings only, never project plugins."""
    try:
        code, out, err = _inventory(login_argv([binary, *lead, "--setting-sources", "user", "plugin", "list", "--json"]), cwd)
        if code:
            detail = err.strip()
            raise ValueError(f"plugin list exited {code}" + (f": {detail}" if detail else ""))
        plugins = json.loads(out)
        if not isinstance(plugins, list) or any(not isinstance(p, dict) for p in plugins):
            raise ValueError("plugin list must be an array of objects")
    except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
        raise ClockError(f"worker clock preflight failed: {exc}") from None
    clock = next((p for p in plugins if p.get("id") == "datetime-inject@ai-additions"
                  and p.get("scope") == "user" and p.get("enabled") is True), None)
    if clock is None:
        raise ClockError("worker requires datetime-inject@ai-additions installed and enabled at user scope "
                         "in its Claude account; install/enable it there before retrying")
    if not _payload(clock.get("installPath")):
        raise ClockError("worker requires datetime-inject@ai-additions installed at user scope with its hook "
                         "payload on disk; reinstall it in its Claude account")
    spec = _declared_clock()
    if not _in_range(str(clock.get("version", "")), spec):
        raise ClockError(f"worker requires datetime-inject@ai-additions version {spec}; update it in its Claude account")


def _inventory(argv: list[str], cwd: str | None) -> tuple[int, str, str]:
    """Read plugin list from a file. A pipe truncates past 64KiB on affected Claude Code versions."""
    with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
        proc = subprocess.Popen(argv, env=clean_env(), cwd=cwd, stdin=subprocess.DEVNULL,
                                stdout=stdout, stderr=stderr, start_new_session=True)
        try:
            code = proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            # The login shell execs the host, which can fork; kill the group, not only the child.
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
            raise
        stdout.seek(0)
        stderr.seek(0)
        return code, stdout.read().decode(), stderr.read().decode()


def _payload(path: object) -> bool:
    # shortcut: payload is hooks/hooks.json; a renamed hook file needs this path updated.
    return isinstance(path, str) and path != "" and (Path(path) / "hooks" / "hooks.json").is_file()


def _declared_clock() -> str:
    manifest = json.loads((Path(__file__).resolve().parent.parent / ".claude-plugin" / "plugin.json").read_text())
    dep = next((item for item in manifest.get("dependencies", [])
                if item.get("name") == "datetime-inject" and item.get("marketplace") == "ai-additions"), None)
    version = dep.get("version") if isinstance(dep, dict) else None
    if not isinstance(version, str):
        raise ClockError("worker requires datetime-inject@ai-additions declared in plugin.json")
    return version


def _in_range(version: str, spec: str) -> bool:
    # shortcut: caret ranges only, the form plugin.json declares; another operator needs a semver library.
    declared = re.fullmatch(r"\^(\d+)\.(\d+)(?:\.(\d+))?", spec)
    got = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)(?:\+[0-9A-Za-z.-]+)?", version)
    if not declared or not got:
        return False
    major, minor = int(declared[1]), int(declared[2])
    patch = int(declared[3]) if declared[3] is not None else 0
    numbers = (int(got[1]), int(got[2]), int(got[3]))
    if major > 0:
        return (major, minor, patch) <= numbers < (major + 1, 0, 0)
    if minor > 0:
        return (0, minor, patch) <= numbers < (0, minor + 1, 0)
    return (0, 0, patch) <= numbers < (0, 0, patch + 1)


def start(request: Request, run_dir: Path, *, claude_as_profile: str = "",
          env: dict[str, str] | None = None) -> subprocess.Popen:
    """Detached adapter runner: durable stdout, stderr and exit status survive an engine restart."""
    # The launch port checks the clock before it creates this directory.
    prepared = command(request, claude_as_profile, check_clock=False)
    (run_dir / "command.json").write_text(json.dumps({"argv": prepared, "cwd": request.cwd,
                                                    "session_id": request.session_id,
                                                    "env": env or {}}))
    try:
        with (run_dir / "runner.stderr").open("w") as errors:
            return subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--worker", str(run_dir),
                                     request.session_id], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                    stderr=errors, env=clean_env(), start_new_session=True)
    except Exception:
        # No runner exists to produce started/exit evidence; allow this attempt to be retried.
        (run_dir / "command.json").unlink(missing_ok=True)
        (run_dir / "runner.stderr").unlink(missing_ok=True)
        raise


def cancel(process: subprocess.Popen) -> None:
    """A refused start must leave no detached runner or host behind."""
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(process.pid, sig)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=5)
            return
        except subprocess.TimeoutExpired:
            pass
    raise RuntimeError(f"runner {process.pid} survived SIGKILL; not cancelled")


def _write(run_dir: Path, name: str, record: dict) -> None:
    path = run_dir / name
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(record))
    temporary.replace(path)


def _worker(run_dir: Path) -> None:
    prepared = json.loads((run_dir / "command.json").read_text())
    with (run_dir / "stdout").open("w") as stdout, (run_dir / "stderr").open("w") as stderr:
        try:
            process = subprocess.Popen(prepared["argv"], cwd=prepared["cwd"], env={**clean_env(), **prepared.get("env", {})},
                                       stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr)
            _write(run_dir, "started.json", {"pid": process.pid, "runner_pid": os.getpid(),
                                             "session_id": prepared["session_id"]})
            code = process.wait()
        except OSError as exc:
            stderr.write(str(exc))
            code = 127
    _write(run_dir, "exit.json", {"exit_code": code})


def call(request: Request, *, claude_as_profile: str = "") -> Reply | Failure:
    """The model port for Claude. Bind the profile with `functools.partial` to get a `model_port.Model`."""
    if request.session_id and not request.resume and not _is_uuid(request.session_id):
        return Failure("error", f"session_id {request.session_id!r} must be a UUID to start a session")
    try:
        prepared = command(request, claude_as_profile)
    except ClockError as exc:
        return Failure("clock", str(exc))
    except ShellError as exc:
        return Failure("missing_program", str(exc))
    try:
        done = subprocess.run(prepared, env=clean_env(), cwd=request.cwd, stdin=subprocess.DEVNULL,
                              capture_output=True, text=True, timeout=request.timeout_s)
    except subprocess.TimeoutExpired:
        return Failure("unknown", f"no answer within {request.timeout_s}s")
    except OSError as exc:  # e.g. the worktree in cwd was removed; the caller's retry policy decides
        return Failure("unknown", f"could not launch: {exc}")
    return classify(done.returncode, done.stdout, done.stderr)


def argv(request: Request, binary: str, lead: list[str]) -> list[str]:
    """The exact argv; the prompt follows `--`, so a prompt that starts with `-` stays the prompt."""
    out = [binary, *lead, "-p", "--output-format", "json"]
    out += ["--model", request.model] if request.model else []
    out += RUNTIME.effort_tokens(request.effort)
    out += ["--json-schema", json.dumps(request.schema)] if request.schema is not None else []
    if request.session_id:
        out += ["--resume" if request.resume else "--session-id", request.session_id]
    else:
        out.append("--no-session-persistence")
    for path in request.plugin_dirs:
        out += ["--plugin-dir", path]
    if request.agent:
        out += ["--agent", request.agent]
    if not request.plugin_dirs and not request.worker:  # judgement calls have no tools or workspace context
        out += ["--tools", "", "--safe-mode"]
    if request.worker:  # a PR branch's own settings, hooks and MCP servers would run as this user
        out += ["--setting-sources", "user", "--strict-mcp-config"]
    return out + ["--", request.prompt]


def _is_uuid(value: str) -> bool:
    try:
        uuid.UUID(value)
    except ValueError:
        return False
    return True


def classify(exit_code: int, stdout: str, stderr: str) -> Reply | Failure:
    try:
        envelope = json.loads(stdout)
    except ValueError:
        envelope = None
    envelope = envelope if isinstance(envelope, dict) else None
    said = str(envelope.get("result", "")) if envelope else ""
    if exit_code == 0 and envelope and envelope.get("is_error") is False:
        return Reply(text=said, data=envelope.get("structured_output"), session_id=str(envelope.get("session_id", "")),
                     cost_usd=float(envelope.get("total_cost_usd") or 0), num_turns=int(envelope.get("num_turns") or 0))
    # A quota stop is checked first: it is never reported as the model's failure (ledger E5, #422).
    reset = quota_reset(f"{stderr}\n{said}")
    if reset is not None:
        return Failure("quota", reset or "quota stop")
    if "No conversation found" in stderr:
        return Failure("session_missing", stderr.strip())
    if not envelope:
        return Failure("unknown", stderr.strip() or f"exit {exit_code} with no answer")
    if envelope.get("subtype") == "error_max_budget_usd":
        return Failure("budget", said or "budget cap reached")
    if "Not logged in" in said:
        return Failure("auth", said)
    return Failure("error", said or f"exit {exit_code}")


if __name__ == "__main__":
    _worker(Path(sys.argv[2]))
