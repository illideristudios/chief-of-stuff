"""The `claude -p --output-format json` adapter behind the model port (design 7a; ledger E17).

Nothing else in 1.0 spawns `claude` or parses its JSON. The executable and effort flag come from the runtime
registry (E13), `claude-as run <profile>` under a claude-as profile (E12), found and run through the user's login
shell (E14: launchd's PATH is short). An answer is judged as the M0 spike measured it: exit code, `is_error` and
parseability together, never `subtype` alone.
"""

from __future__ import annotations

import datetime
import json
import os
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path

import runtimes
from model_port import Failure, Reply, Request, quota_reset
from shell_setup import ShellError, clean_env, login_argv, resolve

RUNTIME = runtimes.get("claude")
LAUNCH_TIMEOUT = 5
_BIRTH = "%a %b %d %H:%M:%S %Y"
_BIRTH_SLOP = 2  # lstart is whole seconds, so a record taken at birth can read this far before it


def command(request: Request, claude_as_profile: str = "") -> list[str]:
    """Resolve the registry's host through shell setup and build exact login argv."""
    if request.session_id and not request.resume and not _is_uuid(request.session_id):
        raise ShellError(f"session_id {request.session_id!r} must be a UUID to start a session")
    program, lead = runtimes.program(RUNTIME.name, claude_as_profile)
    binary = resolve(program)
    if not binary:
        why = f"; claude_as_profile {claude_as_profile!r} needs it" if claude_as_profile else ""
        raise ShellError(f"`{program}` is not on the login shell's PATH{why}")
    return login_argv(argv(request, binary, lead))


def start(request: Request, run_dir: Path, *, claude_as_profile: str = "",
          env: dict[str, str] | None = None) -> subprocess.Popen:
    """Detached adapter runner: durable stdout, stderr and exit status survive an engine restart."""
    prepared = command(request, claude_as_profile)
    _write(run_dir, "command.json", {"argv": prepared, "cwd": request.cwd, "session_id": request.session_id,
                                     "env": env or {}, "start_deadline": time.time() + LAUNCH_TIMEOUT})
    process = None
    try:
        with (run_dir / "runner.stderr").open("w") as errors:
            process = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--worker", str(run_dir),
                                     request.session_id], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                    stderr=errors, env=clean_env(), start_new_session=True)
            _write(run_dir, "runner-spawn.json", {"pid": process.pid, "pgid": process.pid, "start_time": time.time()})
            return process
    except Exception:
        if process is not None:
            # The spawn may have succeeded even when persisting its record failed. Keep all evidence.
            cancel(process)
            raise
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
            deadline = time.monotonic() + 5
            while group_alive(process.pid):
                if sig == signal.SIGTERM or time.monotonic() >= deadline:
                    break
                time.sleep(0.01)
            else:
                return
        except subprocess.TimeoutExpired:
            pass
    raise RuntimeError(f"runner {process.pid} survived SIGKILL; not cancelled")


def _group_members(pgid: int) -> list[tuple[int, float]]:
    """Live members' (pid, birth time). Empty when the group is gone or belongs to someone else."""
    try:
        os.killpg(pgid, 0)
    except (ProcessLookupError, PermissionError):
        return []
    try:  # lstart is localized; the C locale is the only one _BIRTH parses. Its own session keeps ps out of the group.
        probe = subprocess.run(["ps", "-axo", "pgid=,pid=,stat=,lstart="], capture_output=True, text=True, timeout=3,
                               env={**os.environ, "LC_ALL": "C"}, start_new_session=True)
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError("cannot verify worker process group") from exc
    if probe.returncode:
        raise RuntimeError("cannot verify worker process group")
    members = []
    for line in probe.stdout.splitlines():
        fields = line.split(None, 3)
        if len(fields) != 4 or fields[0] != str(pgid) or fields[2].startswith("Z"):
            continue
        try:
            members.append((int(fields[1]), datetime.datetime.strptime(fields[3].strip(), _BIRTH).timestamp()))
        except ValueError:
            raise RuntimeError("cannot read worker start time") from None
    return members


def group_alive(pgid: int) -> bool:
    """Every member counts, even when the runner has exited. Zombies cannot touch the tree."""
    return bool(_group_members(pgid))


def _group_is_ours(pgid: int, start_time: float) -> bool:
    """A group id outlives its leader while any member lives, so only a live leader born after the record is foreign.

    shortcut: a reused leader born within _BIRTH_SLOP of the record reads as ours, when the pid wraps in two seconds.
    """
    members = _group_members(pgid)
    return bool(members) and not any(pid == pgid and birth - start_time >= _BIRTH_SLOP for pid, birth in members)


def spawn_alive(run_dir: Path) -> bool:
    """Check both spawn windows. With no record, find the detached runner by its unique run path."""
    recorded = False
    for name in ("runner-spawn.json", "spawn.json"):
        path = run_dir / name
        if not path.exists():
            continue
        record = json.loads(path.read_text())
        if (any(type(record.get(field)) is not int or record[field] <= 0 for field in ("pid", "pgid"))
                or not isinstance(record.get("start_time"), (int, float)) or record["start_time"] <= 0):
            raise RuntimeError("invalid spawn identity")
        recorded = True
        if _group_is_ours(record["pgid"], record["start_time"]):
            return True
    if recorded:
        return False
    # Legacy/crashed parent: no durable pid yet. A live runner's argv still names this directory.
    try:
        prepared = json.loads((run_dir / "command.json").read_text())
    except (OSError, ValueError):
        return False
    session = prepared.get("session_id", "")
    try:
        probe = subprocess.run(["ps", "-axww", "-o", "stat=,command="], capture_output=True, text=True, timeout=3)
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError("cannot verify unrecorded spawn") from exc
    if probe.returncode:
        raise RuntimeError("cannot verify unrecorded spawn")
    for line in probe.stdout.splitlines():
        fields = line.split()
        if fields and not fields[0].startswith("Z") and (str(run_dir) in fields[1:] or session and session in fields[1:]):
            return True
    return False


def _write(run_dir: Path, name: str, record: dict) -> None:
    path = run_dir / name
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(record))
    temporary.replace(path)


def _stop_host(process: subprocess.Popen) -> None:
    """Kill every other member of this runner's group, host and descendants, and reap the host.

    Publishing exit.json first would free the slot while any of them can still write.
    """
    me = os.getpid()
    for sig in (signal.SIGTERM, signal.SIGKILL):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            others = [pid for pid, _ in _group_members(os.getpgrp()) if pid != me]
            if not others:
                process.wait()
                return
            for pid in others:
                try:
                    os.kill(pid, sig)
                except ProcessLookupError:
                    pass
            time.sleep(0.05)
    raise RuntimeError(f"host {process.pid} or its children survived SIGKILL")


def _worker(run_dir: Path) -> None:
    prepared = json.loads((run_dir / "command.json").read_text())
    with (run_dir / "stdout").open("w") as stdout, (run_dir / "stderr").open("w") as stderr:
        try:
            process = subprocess.Popen(prepared["argv"], cwd=prepared["cwd"], env={**clean_env(), **prepared.get("env", {})},
                                       stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr)
        except OSError as exc:
            stderr.write(str(exc))
            _write(run_dir, "exit.json", {"exit_code": 127})
            return
        try:
            _write(run_dir, "spawn.json", {"pid": process.pid, "pgid": os.getpgrp(), "start_time": time.time()})
            _write(run_dir, "started.json", {"pid": process.pid, "runner_pid": os.getpid(),
                                             "session_id": prepared["session_id"]})
            code = process.wait()
        except OSError as exc:
            stderr.write(str(exc))
            _stop_host(process)
            code = 127
    _write(run_dir, "exit.json", {"exit_code": code})


def call(request: Request, *, claude_as_profile: str = "") -> Reply | Failure:
    """The model port for Claude. Bind the profile with `functools.partial` to get a `model_port.Model`."""
    if request.session_id and not request.resume and not _is_uuid(request.session_id):
        return Failure("error", f"session_id {request.session_id!r} must be a UUID to start a session")
    try:
        prepared = command(request, claude_as_profile)
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
    reset = quota_reset(f"{stderr}\n{said if envelope else stdout}")
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


def inferred_exit(stdout: str, stderr: str) -> int | None:
    """The exit code a runner that died before writing exit.json would have had, from what the host left; else None."""
    try:
        envelope = json.loads(stdout)
    except ValueError:
        envelope = None
    if isinstance(envelope, dict) and isinstance(envelope.get("is_error"), bool):
        return 1 if envelope["is_error"] else 0
    failure = classify(1, stdout, stderr)
    return 1 if isinstance(failure, Failure) and failure.kind == "quota" else None


if __name__ == "__main__":
    _worker(Path(sys.argv[2]))
