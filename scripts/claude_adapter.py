"""The `claude -p --output-format json` adapter behind the model port (design 7a; ledger E17).

Nothing else in 1.0 spawns `claude` or parses its JSON. The executable and effort flag come from the runtime
registry (E13), `claude-as run <profile>` under a claude-as profile (E12), found and run through the user's login
shell (E14: launchd's PATH is short). An answer is judged as the M0 spike measured it: exit code, `is_error` and
parseability together, never `subtype` alone.
"""

from __future__ import annotations

import json
import os
import signal
import shutil
import subprocess
import sys
import uuid
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
    prepared = command(request, claude_as_profile)
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


# Only the model adapter receives provider credentials; shell setup and worker launch keep their
# narrower environment. An unrelated secret must not become visible to a model subprocess.
MODEL_ENV = {
    "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL", "ANTHROPIC_CUSTOM_HEADERS",
    "CLAUDE_CONFIG_DIR", "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY",
    "CLAUDE_CODE_SKIP_BEDROCK_AUTH", "CLAUDE_CODE_SKIP_VERTEX_AUTH", "CLAUDE_CODE_SKIP_FOUNDRY_AUTH",
    "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "AWS_PROFILE", "AWS_DEFAULT_PROFILE",
    "AWS_REGION", "AWS_DEFAULT_REGION", "AWS_CONFIG_FILE", "AWS_SHARED_CREDENTIALS_FILE",
    "AWS_WEB_IDENTITY_TOKEN_FILE", "AWS_ROLE_ARN", "AWS_ROLE_SESSION_NAME",
    "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI", "AWS_CONTAINER_CREDENTIALS_FULL_URI",
    "AWS_CONTAINER_AUTHORIZATION_TOKEN", "AWS_CONTAINER_AUTHORIZATION_TOKEN_FILE",
    "ANTHROPIC_VERTEX_PROJECT_ID", "CLOUD_ML_REGION", "GOOGLE_APPLICATION_CREDENTIALS",
    "GOOGLE_CLOUD_PROJECT", "GOOGLE_CLOUD_QUOTA_PROJECT", "CLOUDSDK_CONFIG",
    "ANTHROPIC_FOUNDRY_API_KEY", "ANTHROPIC_FOUNDRY_BASE_URL", "ANTHROPIC_FOUNDRY_RESOURCE",
    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "all_proxy", "no_proxy",
    "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE", "NODE_EXTRA_CA_CERTS", "NODE_USE_SYSTEM_CA",
}


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
    environment = {**clean_env(source), **{k: v for k, v in source.items() if k in MODEL_ENV}}
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
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)
    except OSError as exc:
        return Failure("unknown", f"could not launch: {exc}")
    timed_out = False
    try:
        stdout, stderr = process.communicate(timeout=request.timeout_s)
    except subprocess.TimeoutExpired:
        timed_out = True
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        stdout, stderr = process.communicate()
    if artifacts is not None:
        (artifacts / "stdout").write_text(stdout)
        (artifacts / "stderr").write_text(stderr)
        (artifacts / "exit.json").write_text(json.dumps({"exit_code": process.returncode, "timed_out": timed_out}))
    if timed_out:
        return Failure("unknown", f"no answer within {request.timeout_s}s")
    return classify(process.returncode, stdout, stderr, keep_events=request.keep_events)


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
    return out + ["--", request.prompt]


def _is_uuid(value: str) -> bool:
    try:
        uuid.UUID(value)
    except ValueError:
        return False
    return True


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
    if exit_code == 0 and envelope and envelope.get("is_error") is False:
        return Reply(text=said, data=envelope.get("structured_output"), session_id=str(envelope.get("session_id", "")),
                     cost_usd=float(envelope.get("total_cost_usd") or 0), num_turns=int(envelope.get("num_turns") or 0),
                     events=events)
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
