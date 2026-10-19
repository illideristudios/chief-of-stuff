"""The `claude -p --output-format json` adapter behind the model port (design 7a; ledger E17).

Nothing else in 1.0 spawns `claude` or parses its JSON. The executable and effort flag come from the runtime
registry (E13), `claude-as run <profile>` under a claude-as profile (E12), found and run through the user's login
shell (E14: launchd's PATH is short). An answer is judged as the M0 spike measured it: exit code, `is_error` and
parseability together, never `subtype` alone.
"""

from __future__ import annotations

import json
import os
import selectors
import signal
import shutil
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import runtimes
from model_port import Failure, Reply, Request, quota_reset
from shell_setup import ShellError, clean_env, login_argv, resolve

RUNTIME = runtimes.get("claude")


def available() -> str | None:
    """Claude's executable on the user's interactive login PATH; no model call."""
    return resolve(RUNTIME.binary)


def command(request: Request, claude_as_profile: str = "", *, extra_args: tuple[str, ...] = (),
            path_prefix: str = "") -> list[str]:
    """Resolve the registry's host through shell setup and build exact login argv."""
    if request.session_id and not request.resume and not _is_uuid(request.session_id):
        raise ShellError(f"session_id {request.session_id!r} must be a UUID to start a session")
    program, lead = runtimes.program(RUNTIME.name, claude_as_profile)
    binary = resolve(program) if claude_as_profile else available()
    if not binary:
        why = f"; claude_as_profile {claude_as_profile!r} needs it" if claude_as_profile else ""
        raise ShellError(f"`{program}` is not on the login shell's PATH{why}")
    built = argv(request, binary, lead)
    # The delimiter, rather than the tail length, owns the boundary between options and prompt.
    boundary = built.index("--")
    return login_argv([*built[:boundary], *extra_args, *built[boundary:]], path_prefix=path_prefix)


def start(request: Request, run_dir: Path, *, claude_as_profile: str = "",
          env: dict[str, str] | None = None) -> subprocess.Popen:
    """Detached adapter runner: durable stdout, stderr and exit status survive an engine restart."""
    return spawn(command(request, claude_as_profile), request, run_dir, env)


def spawn(prepared: list[str], request: Request, run_dir: Path, env: dict[str, str] | None = None) -> subprocess.Popen:
    """The detached runner for any host's prepared login argv (#700: shared by every host adapter)."""
    (run_dir / "command.json").write_text(json.dumps({"argv": prepared, "cwd": request.cwd,
                                                    "session_id": request.session_id,
                                                    "env": env or {}}))
    try:
        with (run_dir / "runner.stderr").open("w") as errors:
            return subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--worker", str(run_dir),
                                     request.session_id], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                    stderr=errors, env=model_env(), start_new_session=True)
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
        except (ProcessLookupError, PermissionError):
            pass  # macOS answers EPERM for a group whose leader already exited and is unreaped (#901)
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
            process = subprocess.Popen(prepared["argv"], cwd=prepared["cwd"], env=model_env({**os.environ, **prepared.get("env", {})}),
                                       stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr)
            _write(run_dir, "started.json", {"pid": process.pid, "runner_pid": os.getpid(),
                                             "session_id": prepared["session_id"]})
            code = process.wait()
        except OSError as exc:
            stderr.write(str(exc))
            code = 127
    _write(run_dir, "exit.json", {"exit_code": code, "ended_at": datetime.now(timezone.utc).isoformat()})


# Only the model adapter receives provider credentials; shell setup keeps its narrower
# environment. An unrelated secret must not become visible to a model subprocess.
MODEL_ENV = {
    "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL", "ANTHROPIC_CUSTOM_HEADERS",
    "CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CONFIG_DIR", "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY",
    "CLAUDE_CODE_SKIP_BEDROCK_AUTH", "CLAUDE_CODE_SKIP_VERTEX_AUTH", "CLAUDE_CODE_SKIP_FOUNDRY_AUTH",
    "AWS_BEARER_TOKEN_BEDROCK", "ANTHROPIC_BEDROCK_BASE_URL",
    "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "AWS_PROFILE", "AWS_DEFAULT_PROFILE",
    "AWS_REGION", "AWS_DEFAULT_REGION", "AWS_CONFIG_FILE", "AWS_SHARED_CREDENTIALS_FILE",
    "AWS_WEB_IDENTITY_TOKEN_FILE", "AWS_ROLE_ARN", "AWS_ROLE_SESSION_NAME",
    "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI", "AWS_CONTAINER_CREDENTIALS_FULL_URI",
    "AWS_CONTAINER_AUTHORIZATION_TOKEN", "AWS_CONTAINER_AUTHORIZATION_TOKEN_FILE",
    "ANTHROPIC_VERTEX_BASE_URL", "ANTHROPIC_VERTEX_PROJECT_ID", "CLOUD_ML_REGION", "GOOGLE_APPLICATION_CREDENTIALS",
    "GOOGLE_CLOUD_PROJECT", "GOOGLE_CLOUD_QUOTA_PROJECT", "CLOUDSDK_CONFIG",
    "ANTHROPIC_FOUNDRY_AUTH_TOKEN", "AZURE_CLIENT_ID", "AZURE_CLIENT_SECRET", "AZURE_TENANT_ID",
    "AZURE_FEDERATED_TOKEN_FILE", "AZURE_AUTHORITY_HOST",
    "ANTHROPIC_FOUNDRY_API_KEY", "ANTHROPIC_FOUNDRY_BASE_URL", "ANTHROPIC_FOUNDRY_RESOURCE",
    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "all_proxy", "no_proxy",
    "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE", "NODE_EXTRA_CA_CERTS", "NODE_USE_SYSTEM_CA",
}


def model_env(source: dict[str, str] | None = None) -> dict[str, str]:
    source = os.environ if source is None else source
    return {**clean_env(source), **{k: v for k, v in source.items() if k in MODEL_ENV}}


def _kill_and_drain(process: subprocess.Popen) -> tuple[str, str]:
    """Reap the leader and bound pipe draining even if a descendant escaped its group."""
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    try:
        return process.communicate(timeout=1)
    except subprocess.TimeoutExpired as exc:
        # TimeoutExpired carries all bytes collected so far, including the first communicate.
        for pipe in (process.stdout, process.stderr):
            if pipe is not None:
                pipe.close()
        process.wait(timeout=1)
        def text(value):
            return value.decode(errors="replace") if isinstance(value, bytes) else value or ""
        return text(exc.output), text(exc.stderr)


def forget_session(session_id: str, env: dict[str, str]) -> None:
    """Remove only an eval-owned UUID's transcript and subagent files, including failed calls.

    Keep the user's config and authentication in place. Unique session ids let concurrent evals
    clean their own files without touching any other conversation or changing shared settings.
    """
    if not _is_uuid(session_id):
        return
    config = Path(env.get("CLAUDE_CONFIG_DIR") or Path(env.get("HOME") or Path.home()) / ".claude")
    for project in (config / "projects").glob("*"):
        if project.is_dir():
            (project / f"{session_id}.jsonl").unlink(missing_ok=True)
            shutil.rmtree(project / session_id, ignore_errors=True)
    for directory in ("session-env", "file-history"):
        shutil.rmtree(config / directory / session_id, ignore_errors=True)


def call(request: Request, *, claude_as_profile: str = "", extra_args: tuple[str, ...] = (),
         env: dict[str, str] | None = None, artifacts: Path | None = None) -> Reply | Failure:
    """Complete a model call; driver callers can retain exact launch and raw output artifacts.

    Artifact environments contain key names only, never authentication values. A new process
    group lets the deadline terminate descendants as well as the login shell's direct child.
    """
    if request.session_id and not request.resume and not _is_uuid(request.session_id):
        return Failure("error", f"session_id {request.session_id!r} must be a UUID to start a session")
    source = os.environ if env is None else env
    environment = model_env(source)
    try:
        prepared = command(request, claude_as_profile, extra_args=extra_args,
                           path_prefix=source.get("CHIEF_OF_STUFF_PATH_PREFIX", ""))
    except ShellError as exc:
        return Failure("missing_program", str(exc))
    if artifacts is not None:
        artifacts.mkdir(parents=True, exist_ok=True)
        (artifacts / "command.json").write_text(json.dumps({"argv": prepared, "cwd": request.cwd,
                                                          "env_keys": sorted(environment)}, indent=1))
    # communicate drains both streams while the group is alive. Kill the entire group on timeout,
    # even when its leader already exited and a descendant is keeping a pipe open.
    try:
        process = subprocess.Popen(prepared, env=environment, cwd=request.cwd, stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, errors="replace", start_new_session=True)
    except OSError as exc:
        return Failure("unknown", f"could not launch: {exc}")
    timed_out = False
    try:
        stdout, stderr = process.communicate(timeout=request.timeout_s)
    except subprocess.TimeoutExpired:
        timed_out = True
        stdout, stderr = _kill_and_drain(process)
    except BaseException:
        _kill_and_drain(process)
        raise
    if artifacts is not None:
        (artifacts / "stdout").write_text(stdout)
        (artifacts / "stderr").write_text(stderr)
        (artifacts / "exit.json").write_text(json.dumps({"exit_code": process.returncode, "timed_out": timed_out}))
    if timed_out:
        return Failure("unknown", f"no answer within {request.timeout_s}s")
    return classify(process.returncode, stdout, stderr, keep_events=request.keep_events)


def drive_turns(request: Request, prompts: list[str | None], waits: list[float], *,
                extra_args: tuple[str, ...] = (), env: dict[str, str], artifacts: Path,
                snapshot, before_turn) -> list[tuple[list[dict], datetime, datetime]] | Failure:
    """Eval stream input: wait turns send nothing and keep the same CLI alive for notifications."""
    deadline = time.monotonic() + request.timeout_s
    try:
        prepared = command(request, extra_args=(*extra_args, "--input-format", "stream-json"),
                           path_prefix=env.get("CHIEF_OF_STUFF_PATH_PREFIX", ""))
    except ShellError as exc:
        return Failure("missing_program", str(exc))
    # Stream input has no positional prompt; command's final empty prompt is deliberately omitted.
    prepared.pop()
    environment = model_env(env)
    artifacts.mkdir(parents=True, exist_ok=True)
    (artifacts / "command.json").write_text(json.dumps({"argv": prepared, "cwd": request.cwd,
                                                       "env_keys": sorted(environment)}, indent=1))
    turns = []
    timed_out = False
    raw = bytearray()
    pending = bytearray()
    process = None
    try:
        with (artifacts / "stderr").open("w+b") as errors, selectors.DefaultSelector() as selector:
            try:
                process = subprocess.Popen(prepared, cwd=request.cwd, env=environment, stdin=subprocess.PIPE,
                                           stdout=subprocess.PIPE, stderr=errors, start_new_session=True)
            except OSError as exc:
                return Failure("unknown", f"could not launch: {exc}")
            selector.register(process.stdout, selectors.EVENT_READ)
            try:
                for n, (prompt, wait) in enumerate(zip(prompts, waits), 1):
                    if prompt is None and turns and any(e.get("type") == "system" and
                            e.get("subtype") == "task_notification" for e in turns[-1][0]):
                        continue
                    before_turn(n)
                    started = datetime.now().astimezone()
                    if prompt is not None:
                        process.stdin.write((json.dumps({"type": "user", "message": {
                            "role": "user", "content": prompt}}) + "\n").encode())
                        process.stdin.flush()
                    wait_deadline = min(deadline, time.monotonic() + wait) if prompt is None else deadline
                    events = []
                    while True:
                        if b"\n" not in pending:
                            remaining = (deadline if events else wait_deadline) - time.monotonic()
                            if remaining <= 0 or not selector.select(max(0, remaining)):
                                if time.monotonic() >= deadline:
                                    timed_out = True
                                    return Failure("unknown", f"no answer within {request.timeout_s}s")
                                break  # idle wait expired: no turn to grade
                            chunk = os.read(process.stdout.fileno(), 65536)
                            if not chunk:
                                process.wait(timeout=max(0.01, deadline - time.monotonic()))
                                errors.flush()
                                errors.seek(0)
                                diagnostic = errors.read().decode(errors="replace")
                                if process.returncode or events:
                                    return classify(process.returncode, json.dumps(events[-1]) if events else "",
                                                    diagnostic, keep_events=True)
                                if prompt is not None:
                                    return classify(0, "", diagnostic, keep_events=True)
                                break
                            raw.extend(chunk)
                            pending.extend(chunk)
                            continue
                        line, _, rest = pending.partition(b"\n")
                        pending[:] = rest
                        try:
                            event = json.loads(line.decode(errors="replace"))
                        except ValueError:
                            continue
                        if not isinstance(event, dict):
                            continue
                        events.append(event)
                        if event.get("type") == "result":
                            got = classify(0, "\n".join(json.dumps(e) for e in events), "", keep_events=True)
                            if isinstance(got, Failure):
                                return got
                            turns.append((events, started, datetime.now().astimezone()))
                            if snapshot(len(turns)) is False:
                                return Failure("unknown", "fixture snapshot failed; see snapshot-errors.jsonl")
                            break
                    if not events or events[-1].get("type") != "result":
                        break
            except (BrokenPipeError, subprocess.TimeoutExpired) as exc:
                return Failure("unknown", str(exc))
            finally:
                # Closing stdin permits an ordinary exit. Drain/cleanup is always bounded.
                try:
                    process.stdin.close()
                except BrokenPipeError:
                    pass
                process.stdin = None
                natural_exit = True
                try:
                    tail, _ = process.communicate(timeout=1)
                except subprocess.TimeoutExpired:
                    # A leader that exited while a descendant holds a pipe is still a natural exit.
                    natural_exit = process.poll() is not None
                    tail, _ = _kill_and_drain(process)
                except BaseException:
                    _kill_and_drain(process)
                    raise
                raw.extend(tail.encode() if isinstance(tail, str) else tail)
                errors.flush()
                errors.seek(0)
                diagnostic = errors.read().decode(errors="replace")
    finally:
        (artifacts / "stdout").write_bytes(raw)
        if process is not None:
            (artifacts / "exit.json").write_text(json.dumps({"exit_code": process.returncode,
                                                            "timed_out": timed_out}))
    if natural_exit and turns:
        final = classify(process.returncode, "\n".join(json.dumps(e) for e in turns[-1][0]),
                         diagnostic, keep_events=True)
        if isinstance(final, Failure):
            return final
    return turns


def argv(request: Request, binary: str, lead: list[str]) -> list[str]:
    """The exact argv; the prompt follows `--`, so a prompt that starts with `-` stays the prompt."""
    out = [binary, *lead, "-p", "--output-format", "stream-json" if request.keep_events else "json"]
    if request.keep_events:
        out.append("--verbose")
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
        # Strict mode also drops plugin MCP servers (Claude Code 2.1.303), so the guard's engine server, which
        # carries the report tool, is passed explicitly; ${CHIEF_PYTHON} still expands from the worker's env.
        servers = {}
        for path in request.plugin_dirs:
            declared = Path(path) / ".mcp.json"
            if declared.is_file():
                servers.update(json.loads(declared.read_text().replace("${CLAUDE_PLUGIN_ROOT}", str(path))))
        out += ["--mcp-config", json.dumps({"mcpServers": servers})] if servers else []
    return out + ["--", request.prompt]


def _is_uuid(value: str) -> bool:
    try:
        uuid.UUID(value)
    except ValueError:
        return False
    return True


def _cost(envelope: dict | None) -> float:
    if not envelope:
        return 0.0
    try:
        value = float(envelope.get("total_cost_usd") or 0)
    except (TypeError, ValueError):
        return 0.0
    return value if 0 < value < float("inf") else 0.0


def classify(exit_code: int, stdout: str, stderr: str, *, keep_events: bool = False) -> Reply | Failure:
    events = None
    try:
        if keep_events:
            events = []
            for line in stdout.splitlines():
                try:
                    event = json.loads(line)
                except ValueError:
                    continue  # login rc output is retained in raw artifacts, never treated as an event
                if isinstance(event, dict):
                    events.append(event)
            envelope = next((event for event in reversed(events) if event.get("type") == "result"), None)
        else:
            envelope = json.loads(stdout)
    except ValueError:
        envelope = None
    envelope = envelope if isinstance(envelope, dict) else None
    said = str(envelope.get("result", "")) if envelope else ""
    cost = _cost(envelope)
    if exit_code == 0 and envelope and envelope.get("is_error") is False:
        return Reply(text=said, data=envelope.get("structured_output"), session_id=str(envelope.get("session_id", "")),
                     cost_usd=cost, num_turns=int(envelope.get("num_turns") or 0), events=events)
    # A quota stop is checked first: it is never reported as the model's failure (ledger E5, #422).
    reset = quota_reset(f"{stderr}\n{said}")
    if reset is not None:
        return Failure("quota", reset or "quota stop", cost_usd=cost)
    if "No conversation found" in stderr:
        return Failure("session_missing", stderr.strip(), cost_usd=cost)
    if not envelope:
        return Failure("unknown", stderr.strip() or f"exit {exit_code} with no answer")
    if envelope.get("subtype") == "error_max_budget_usd":
        return Failure("budget", said or "budget cap reached", cost_usd=cost)
    if "Not logged in" in said:
        return Failure("auth", said, cost_usd=cost)
    return Failure("error", said or f"exit {exit_code}", cost_usd=cost)


if __name__ == "__main__":
    _worker(Path(sys.argv[2]))
