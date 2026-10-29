"""The T3 launcher behind the host adapter contract (§4, §13, #1178): a worker as a T3 thread.

T3 is a launcher, not a fifth runtime: the worker is still a claude worker, started as a thread through the `t3` CLI.
The detached runner of `claude_adapter` runs this module's `run` as the "host", so the run evidence (`command.json`,
`started.json`, `exit.json`, `stdout`), cancel and collection are the contract's own. `run` registers the worker's tree as
a T3 project (`thread start` takes only a registered project), starts the thread with the assignment, records the thread
id in `thread.json`, and exits when the thread is idle with nothing pending. The engine channel reaches the thread
through files the engine writes into the tree, as for Cursor: T3 gives a thread no environment or argv of its own.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path
from datetime import datetime

import claude_adapter
import host_adapter
import runtimes
from model_port import Request
from shell_setup import ShellError
from worker_health import Evidence

PROVIDERS = {"claude": "claudeAgent"}  # runtime -> T3 provider instance; the others need their own channel file under T3
POLL_S = 5.0
CHANNEL_EXCLUDES = ("/.mcp.json", "/.claude/settings.local.json")
FAILED = ("error", "failed", "interrupted")


def cli() -> str | None:
    """The `t3` CLI: T3 sets T3CODE_CLI_PATH inside its own sessions; otherwise the first `t3` on PATH."""
    path = os.environ.get("T3CODE_CLI_PATH")
    return path if path and os.access(path, os.X_OK) else shutil.which("t3")


def available(runtime: str) -> bool:
    return runtime in PROVIDERS and cli() is not None


def environment_id(home: Path | str | None = None) -> str:
    """The id T3 keeps in its data directory; it names the environment in a deep link and is never hard-coded."""
    base = Path(home or os.environ.get("T3CODE_HOME") or Path.home() / ".t3")
    try:
        found = (base / "userdata" / "environment-id").read_text().strip()
    except OSError as exc:
        raise ShellError(f"cannot read T3's environment id under {base}: {exc}") from exc
    if not found:
        raise ShellError(f"T3's environment id under {base} is empty")
    return found


def thread_link(environment: str, thread: str) -> str:
    return f"t3code://threads/{environment}/{thread}"


def outcome(thread: dict) -> int | None:
    """The exit code of a thread that has ended, else None: idle with no pending request is ended, and `failed` kinds
    exit 1. A pending request is a thread waiting for an answer, which is not an end."""
    if (thread.get("activeRunId") or thread.get("pendingRequestCount")
            or thread.get("status") not in (*FAILED, "completed", "idle")):
        return None
    return 1 if thread.get("status") in FAILED else 0


def _hooks(guard: Path, binding: dict[str, str]) -> dict:
    """The guard plugin's hooks with no inherited environment to read: each command carries its own CHIEF_* binding."""
    declared = json.loads((guard / "hooks" / "hooks.json").read_text())["hooks"]
    lead = " ".join(f"{key}={shlex.quote(value)}" for key, value in sorted(binding.items()) if key != "CHIEF_PYTHON")
    for entries in declared.values():
        for entry in entries:
            for hook in entry["hooks"]:
                hook["command"] = f'{lead} {hook["command"]}'.replace("${CHIEF_PYTHON}", binding["CHIEF_PYTHON"]).replace(
                    "${CLAUDE_PLUGIN_ROOT}", str(guard))
    return declared


def write_channel(tree: Path, binding: dict[str, str], *, agent: str, guard: Path) -> None:
    """Write what a T3 thread cannot be given on a command line: the engine's report and ask server with its binding,
    the named agent, and the guard hooks. Git-excluded, and never over a file the engine did not write.

    T3 cannot pass `--setting-sources user --strict-mcp-config`, so a branch that brings its own Claude config would
    have it run as this user: that tree is refused, as is any link the write would follow out of the tree."""
    command, args, bound = host_adapter._channel(binding)
    files = {tree / ".mcp.json": {"mcpServers": {"engine": {"command": command, "args": args, "env": bound}}},
             tree / ".claude" / "settings.local.json": {**({"agent": agent} if agent else {}), "hooks": _hooks(guard, binding)}}
    claude = tree / ".claude"
    if os.path.lexists(claude) or os.path.lexists(tree / ".mcp.json"):  # fail closed: any entry is the branch's own
        raise ShellError("the worker tree has its own Claude config (.mcp.json or .claude/); refusing to launch it under T3")
    claude.mkdir()
    for path, content in files.items():
        with path.open("x") as handle:  # exclusive create: it fails on a link rather than following it
            json.dump(content, handle)
    for entry in CHANNEL_EXCLUDES:
        host_adapter._exclude(tree, entry)


def start(request: Request, run_dir: Path, *, env: dict[str, str], cli: str | None = None, home: Path | str | None = None,
          poll_s: float = POLL_S) -> "subprocess.Popen":
    """Start a worker as a T3 thread, detached: the runner of `claude_adapter` runs `run` below."""
    binary = cli or globals()["cli"]()
    if not binary:
        raise ShellError("`t3` is not on PATH and T3CODE_CLI_PATH is not set")
    provider = PROVIDERS.get(request.runtime or "claude")
    if provider is None:
        raise ValueError(f"no T3 provider for {request.runtime}")
    guard = Path(request.plugin_dirs[0])
    write_channel(Path(request.cwd), env, agent=request.agent, guard=guard)
    assignment = run_dir / "assignment.md"
    assignment.write_text(request.prompt)
    prompt = runtimes.WORKER_BOOTSTRAP.format(assignment=assignment, cwd=request.cwd, session=request.session_id)
    spec = run_dir / "t3.json"
    spec.write_text(json.dumps({"cli": binary, "tree": request.cwd, "provider": provider, "model": request.model,
                                "environment": environment_id(home), "prompt": prompt, "poll_s": poll_s, "home": str(home or os.environ.get("T3CODE_HOME") or ""),
                                "title": f"{env.get('CHIEF_ISSUE_KEY', '')} {env.get('CHIEF_STAGE', '')}".strip()}))
    prepared = [sys.executable, str(Path(__file__).resolve()), "run", request.session_id, str(spec)]
    return claude_adapter.spawn(prepared, request, run_dir, env)


def _t3(spec: dict, *argv: str, stdin: str | None = None) -> subprocess.CompletedProcess:
    base = ["--base-dir", spec["home"]] if spec["home"] else []
    return subprocess.run([spec["cli"], *argv, *base], input=stdin, capture_output=True, text=True, timeout=120)


def observe(record: dict) -> Evidence:
    """Read the actual thread, never infer its activity from the local polling process.

    Use the launch's saved CLI and home, so an engine restart observes the same environment.
    A CLI error, partial response or missing identity is unknown, not permission to relaunch.
    """
    try:
        run_dir = Path(record["run_dir"])
        spec = json.loads((run_dir / "t3.json").read_text())
        thread_id = record.get("thread_id")
        if not thread_id:
            thread_id = json.loads((run_dir / "thread.json").read_text())["thread_id"]
        done = _t3(spec, "thread", "show", thread_id, "--json", "--messages", "0")
        if done.returncode:
            raise RuntimeError((done.stderr or "T3 thread read failed").strip()[:300])
        thread = json.loads(done.stdout)["thread"]
        if thread["threadId"] != thread_id:
            raise ValueError("T3 thread identity mismatch")
        status = thread["status"]
        updated = datetime.fromisoformat(thread["updatedAt"])
        pending = thread["pendingRequestCount"]
        if not isinstance(status, str) or not status or updated.utcoffset() is None or type(pending) is not int or pending < 0:
            raise ValueError("incomplete T3 thread evidence")
        return Evidence(alive=outcome(thread) is None, last_activity_at=updated, launcher="t3",
                        status=status, pending_requests=pending)
    except (OSError, ValueError, KeyError, TypeError, RuntimeError, subprocess.SubprocessError) as exc:
        return Evidence(launcher="t3", error=f"T3 evidence unavailable: {exc}")


def _start_thread(spec: dict) -> str:
    argv = ["thread", "start", spec["tree"], "-", "--json", "--title", spec["title"] or "chief-of-stuff worker",
            *(["--provider", spec["provider"], "--model", spec["model"]] if spec["model"] else [])]
    done = _t3(spec, *argv, stdin=spec["prompt"])
    if done.returncode and "No T3 Code project matches" in done.stderr:
        added = _t3(spec, "project", "add", spec["tree"], "--title", Path(spec["tree"]).name)
        if added.returncode:
            raise RuntimeError(f"t3 project add failed: {(added.stderr or added.stdout).strip()[:300]}")
        done = _t3(spec, *argv, stdin=spec["prompt"])
    if done.returncode:
        raise RuntimeError(f"t3 thread start failed: {(done.stderr or done.stdout).strip()[:300]}")
    return json.loads(done.stdout)["threadId"]


def run(session_id: str, spec_path: Path) -> int:
    """The runner's host: start the thread, record its id, wait for it to end, and print its last reply."""
    spec = json.loads(spec_path.read_text())
    try:
        thread = _start_thread(spec)
    except (RuntimeError, ValueError, KeyError, subprocess.SubprocessError, OSError) as exc:
        print(exc, file=sys.stderr)
        return 1
    (spec_path.parent / "thread.json").write_text(json.dumps(
        {"thread_id": thread, "thread_link": thread_link(spec["environment"], thread)}))
    while True:
        time.sleep(spec["poll_s"])
        done = _t3(spec, "thread", "show", thread, "--json", "--messages", "1")
        try:
            shown = json.loads(done.stdout)
        except ValueError:
            continue  # a failed read is not an end: the next poll decides
        code = outcome(shown["thread"])
        if code is not None:
            replies = [m["text"] for m in shown.get("messages", []) if m.get("role") == "assistant"]
            print(replies[-1] if replies else "")
            return code


if __name__ == "__main__":
    if sys.argv[1:2] == ["run"]:
        sys.exit(run(sys.argv[2], Path(sys.argv[3])))
