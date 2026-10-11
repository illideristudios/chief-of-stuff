"""The `claude -p --output-format json` adapter behind the model port (design 7a; ledger E17).

Nothing else in 1.0 spawns `claude` or parses its JSON. The executable and effort flag come from the runtime
registry (E13), `claude-as run <profile>` under a claude-as profile (E12), found and run through the user's login
shell (E14: launchd's PATH is short). An answer is judged as the M0 spike measured it: exit code, `is_error` and
parseability together, never `subtype` alone.
"""

from __future__ import annotations

import json
import subprocess

import runtimes
from model_port import Failure, Reply, Request, quota_reset
from shell_setup import ShellError, clean_env, login_argv, resolve

RUNTIME = runtimes.get("claude")


def call(request: Request, *, claude_as_profile: str = "") -> Reply | Failure:
    """The model port for Claude. Bind the profile with `functools.partial` to get a `model_port.Model`."""
    program, lead = runtimes.program(RUNTIME.name, claude_as_profile)
    try:
        binary = resolve(program)
        if not binary:  # never a bare `claude` in place of a missing `claude-as` (E12)
            why = f"; claude_as_profile {claude_as_profile!r} needs it" if claude_as_profile else ""
            return Failure("missing_program", f"`{program}` is not on the login shell's PATH{why}")
        command = login_argv(argv(request, binary, lead))
    except ShellError as exc:
        return Failure("missing_program", str(exc))
    try:
        done = subprocess.run(command, env=clean_env(), cwd=request.cwd, stdin=subprocess.DEVNULL,
                              capture_output=True, text=True, timeout=request.timeout_s)
    except subprocess.TimeoutExpired:
        return Failure("unknown", f"no answer within {request.timeout_s}s")
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
    if not request.plugin_dirs:  # design 9: a judgement call runs without tools, hooks, plugins or CLAUDE.md
        out += ["--tools", "", "--safe-mode"]
    return out + ["--", request.prompt]


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
