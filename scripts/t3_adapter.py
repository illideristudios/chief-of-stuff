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
import asyncio
import fcntl
import hashlib
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import quote, urlsplit
from datetime import datetime

import claude_adapter
import host_adapter
import runtimes
from model_port import Request
from shell_setup import ShellError, resolve
from worker_health import Evidence

PROVIDERS = {"claude": "claudeAgent"}  # runtime -> T3 provider instance; the others need their own channel file under T3
POLL_S = 5.0
CHANNEL_EXCLUDES = ("/.mcp.json", "/.claude/settings.local.json")
FAILED = ("error", "failed", "interrupted", "cancelled", "rolled_back")


class StartRefused(RuntimeError):
    """A failure before any thread start was attempted, so retry cannot duplicate a remote worker."""


def cli() -> str | None:
    """The `t3` CLI: T3 sets T3CODE_CLI_PATH inside its own sessions; otherwise the first `t3` on PATH."""
    path = os.environ.get("T3CODE_CLI_PATH")
    return path if path and os.access(path, os.X_OK) else shutil.which("t3") or resolve("t3")


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
    return f"t3code://threads/{quote(environment, safe=':')}/{quote(thread, safe=':')}"


def outcome(thread: dict) -> int | None:
    """The exit code of a thread that has ended, else None: idle with no pending request is ended, and `failed` kinds
    exit 1. A pending request is a thread waiting for an answer, which is not an end."""
    if (thread.get("activeRunId") or thread.get("pendingRequestCount")
            or thread.get("status") not in (*FAILED, "completed", "idle")):
        return None
    if thread.get("status") in FAILED:
        return 1
    return 0 if thread.get("status") in ("idle", "completed") else None


def _hooks(guard: Path, binding: dict[str, str]) -> dict:
    """The guard plugin's hooks with no inherited environment to read: each command carries its own CHIEF_* binding."""
    declared = json.loads((guard / "hooks" / "hooks.json").read_text())["hooks"]
    lead = " ".join(f"{key}={shlex.quote(value)}" for key, value in sorted(binding.items()) if key != "CHIEF_PYTHON")
    for entries in declared.values():
        for entry in entries:
            for hook in entry["hooks"]:
                argv = [part.replace("${CHIEF_PYTHON}", binding["CHIEF_PYTHON"]).replace(
                    "${CLAUDE_PLUGIN_ROOT}", str(guard)) for part in shlex.split(hook["command"])]
                hook["command"] = f"{lead} {shlex.join(argv)}"
    return declared


def write_channel(tree: Path, binding: dict[str, str], *, agent: str, guard: Path, state: Path | None = None) -> None:
    """Write what a T3 thread cannot be given on a command line: the engine's report and ask server with its binding,
    the named agent, and the guard hooks. Git-excluded, and never over a file the engine did not write.

    T3 cannot pass `--setting-sources user --strict-mcp-config`, so a branch that brings its own Claude config would
    have it run as this user: that tree is refused, as is any link the write would follow out of the tree."""
    command, args, bound = host_adapter._channel(binding)
    files = {tree / ".mcp.json": {"mcpServers": {"engine": {"command": command, "args": args, "env": bound}}},
             tree / ".claude" / "settings.local.json": {**({"agent": agent} if agent else {}), "hooks": _hooks(guard, binding)}}
    # Ownership lives outside the worker's tree. A marker in a branch-controlled file is not proof of ownership.
    manifest = state / ("t3-channel-" + hashlib.sha256(str(tree.resolve()).encode()).hexdigest() + ".json") if state else None
    if state:
        state.mkdir(parents=True, exist_ok=True)
    lock = os.open(state or tree, os.O_RDONLY)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX)
        _write_channel(tree, files, manifest)
    finally:
        os.close(lock)


def _write_channel(tree: Path, files: dict, manifest: Path | None) -> None:
    claude = tree / ".claude"
    previous = json.loads(manifest.read_text()) if manifest and manifest.exists() else None
    if os.path.lexists(claude) or os.path.lexists(tree / ".mcp.json"):
        owned = previous and not claude.is_symlink() and claude.is_dir() and set(p.name for p in claude.iterdir()) == {"settings.local.json"}
        for path in files:
            owned = owned and not path.is_symlink() and path.is_file() and path.read_text() == previous.get(str(path))
        if not owned:
            raise ShellError("the worker tree has its own Claude config (.mcp.json or .claude/); refusing to launch it under T3")
    else:
        previous = None
    # Exclusion failures precede any writes, so a retry does not inherit half an unowned channel.
    for entry in CHANNEL_EXCLUDES:
        host_adapter._exclude(tree, entry)
    if previous:
        for path in files:
            path.unlink()
        claude.rmdir()
    claude.mkdir()
    # Anchor every write to a verified directory fd. Exclusive creation alone does not protect a parent symlink.
    directory = os.open(claude, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    created = []
    try:
        for path, content in files.items():
            parent = directory if path.parent == claude else os.open(tree, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                fd = os.open(path.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent)
                created.append(path)
                with os.fdopen(fd, "w") as handle:
                    json.dump(content, handle)
            finally:
                if parent != directory:
                    os.close(parent)
        if manifest:
            temporary = manifest.with_suffix(".tmp")
            temporary.write_text(json.dumps({str(p): json.dumps(c) for p, c in files.items()}))
            temporary.replace(manifest)
    except BaseException:
        for path in created:
            if path.parent == claude:
                os.unlink(path.name, dir_fd=directory)
            else:
                path.unlink(missing_ok=True)
        if not claude.is_symlink():
            claude.rmdir()
        raise
    finally:
        os.close(directory)


def provider_instance(profile: str, mapping: dict[str, str], home: Path | str | None = None) -> str:
    """The T3 provider instance a Claude worker under `profile` runs as: T3's default for no profile, else the one
    `[workers.t3_providers]` maps it to. T3 lists its instances in its settings file, which is no documented interface:
    when that cannot be read, an unknown instance is left to T3 to refuse."""
    if not profile:
        return PROVIDERS["claude"]
    instance = mapping.get(profile)
    if not instance:
        raise ShellError(f"no T3 provider instance is mapped to claude_as_profile {profile!r}; add it to [workers.t3_providers]")
    base = Path(home or os.environ.get("T3CODE_HOME") or Path.home() / ".t3")
    try:
        known = list(json.loads((base / "userdata" / "settings.json").read_text())["providerInstances"])
    except (OSError, ValueError, KeyError, TypeError):
        return instance
    if instance not in known:
        raise ShellError(f"T3 provider instance {instance!r} does not exist (mapped from claude_as_profile {profile!r}); "
                         f"T3 has: {', '.join(known)}")
    return instance


def start(request: Request, run_dir: Path, *, env: dict[str, str], cli: str | None = None, home: Path | str | None = None,
          poll_s: float = POLL_S, provider: str | None = None) -> "subprocess.Popen":
    """Start a worker as a T3 thread, detached: the runner of `claude_adapter` runs `run` below."""
    binary = cli or globals()["cli"]()
    if not binary:
        raise ShellError("`t3` is not on PATH and T3CODE_CLI_PATH is not set")
    provider = provider or PROVIDERS.get(request.runtime or "claude")
    if provider is None:
        raise ValueError(f"no T3 provider for {request.runtime}")
    if provider != PROVIDERS["claude"] and not request.model:
        raise ValueError(f"T3 provider instance {provider!r} needs a model: `t3 thread start` ignores --provider without --model")
    guard = Path(request.plugin_dirs[0])
    environment = environment_id(home)  # fail before touching the worker's tree
    write_channel(Path(request.cwd), env, agent=request.agent, guard=guard, state=run_dir.parent)
    assignment = run_dir / "assignment.md"
    assignment.write_text(request.prompt)
    prompt = runtimes.WORKER_BOOTSTRAP.format(assignment=assignment, cwd=request.cwd, session=request.session_id)
    spec = run_dir / "t3.json"
    spec.write_text(json.dumps({"cli": binary, "tree": request.cwd, "provider": provider, "model": request.model,
                                "environment": environment, "prompt": prompt, "poll_s": poll_s, "home": str(home or os.environ.get("T3CODE_HOME") or ""),
                                "title": f"{env.get('CHIEF_ISSUE_KEY', '')} {env.get('CHIEF_STAGE', '')} {request.session_id}".strip()}))
    prepared = [sys.executable, str(Path(__file__).resolve()), "run", request.session_id, str(spec)]
    process = claude_adapter.spawn(prepared, request, run_dir, env)
    process.t3_run_dir = run_dir
    return process


def _t3(spec: dict, *argv: str, stdin: str | None = None) -> subprocess.CompletedProcess:
    base = ["--base-dir", spec["home"]] if spec["home"] else []
    return subprocess.run([spec["cli"], *argv, *base], input=stdin, capture_output=True, text=True, timeout=120)


def send(run_dir: Path, thread: str, body: str) -> None:
    """Hand an operator message to the thread as its next turn (`t3 thread send`, the body on stdin so no text is a flag).

    A CLI refusal raises RuntimeError with its words. A timeout or a missing CLI raises the OS or subprocess error: the
    message may or may not have reached the thread, so the caller must not read that as a failure."""
    done = _t3(json.loads((run_dir / "t3.json").read_text()), "thread", "send", thread, "-", stdin=body)
    if done.returncode:
        raise RuntimeError(f"t3 thread send failed: {(done.stderr or done.stdout).strip()[:300]}")


def observe(record: dict | Path) -> Evidence | dict:
    """Read the actual thread, never infer its activity from the local polling process.

    Use the launch's saved CLI and home, so an engine restart observes the same environment.
    A CLI error, partial response or missing identity is unknown, not permission to relaunch.
    A run-directory Path retains the launcher's raw-state recovery API; a record returns
    the stall watcher's validated Evidence, including update time and pending requests.
    """
    if isinstance(record, Path):
        return thread_state(record)
    try:
        run_dir = Path(record["run_dir"])
        if (run_dir / "start_failed.json").exists():
            return Evidence(alive=False, launcher="t3", status="failed")
        thread = thread_state(run_dir, thread_id=record.get("thread_id"))
        thread_id = record.get("thread_id")
        if not thread_id:
            thread_id = json.loads((run_dir / "thread.json").read_text())["thread_id"]
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
    # Always register the exact tree. A path inside a registered parent project resolves to that parent's root.
    try:
        added = _t3(spec, "project", "add", spec["tree"], "--title", Path(spec["tree"]).name)
    except (OSError, subprocess.SubprocessError) as exc:
        raise StartRefused("T3 project registration unavailable; no thread start attempted") from exc
    if added.returncode and "already exists" not in (added.stderr + added.stdout).lower():
        raise StartRefused(f"t3 project add failed: {(added.stderr or added.stdout).strip()[:300]}")
    argv = ["thread", "start", spec["tree"], "-", "--json", "--title", spec["title"] or "chief-of-stuff worker",
            *(["--provider", spec["provider"], "--model", spec["model"]] if spec["model"] else [])]
    done = _t3(spec, *argv, stdin=spec["prompt"])
    if done.returncode:
        raise RuntimeError(f"t3 thread start failed: {(done.stderr or done.stdout).strip()[:300]}")
    launched = json.loads(done.stdout)
    if isinstance(launched.get("runId"), str):
        spec["run_id"] = launched["runId"]
    return launched["threadId"]


def _write_thread(run_dir: Path, spec: dict, thread: str) -> None:
    _atomic(run_dir / "thread.json", {"thread_id": thread, "thread_link": thread_link(spec["environment"], thread),
                                    **({"thread_run_id": spec["run_id"]} if spec.get("run_id") else {})})


def _atomic(path: Path, data: dict) -> None:
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=path.name + "-")
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(data, handle)
        Path(name).replace(path)
    finally:
        Path(name).unlink(missing_ok=True)


def run(session_id: str, spec_path: Path) -> int:
    """The runner's host: start the thread, record its id, wait for it to end, and print its last reply."""
    spec = json.loads(spec_path.read_text())
    (spec_path.parent / "launching.json").write_text("{}")  # durable intent: a failed CLI reply is ambiguous
    try:
        thread = _start_thread(spec)
    except StartRefused as exc:
        _atomic(spec_path.parent / "start_failed.json", {"error": str(exc)})
        print(exc, file=sys.stderr)
        return 1
    except (RuntimeError, ValueError, KeyError, subprocess.SubprocessError, OSError) as exc:
        print(exc, file=sys.stderr)
        return 1
    _write_thread(spec_path.parent, spec, thread)
    while True:
        time.sleep(spec["poll_s"])
        try:
            done = _t3(spec, "thread", "show", thread, "--json", "--messages", "1")
            if done.returncode:
                continue
            shown = json.loads(done.stdout)
            code = outcome(shown["thread"])
        except (ValueError, KeyError, TypeError, subprocess.SubprocessError, OSError):
            continue  # a failed read is not an end: the next poll decides
        if code is not None:
            replies = [m["text"] for m in shown.get("messages", []) if m.get("role") == "assistant"]
            print(replies[-1] if replies else "")
            return code


def _mcp(spec: dict, tool: str, arguments: dict) -> dict:
    """Use the already-pinned MCP client and a short-lived, scoped local CLI session. Never persist its token."""
    async def call():
        import httpx2
        from mcp import ClientSession
        from mcp.client.streamable_http import streamable_http_client
        base = Path(spec.get("home") or Path.home() / ".t3")
        origin = json.loads((base / "userdata/server-runtime.json").read_text())["origin"]
        url = urlsplit(origin)
        if url.scheme != "http" or url.hostname not in ("127.0.0.1", "localhost", "::1") or url.username or url.password:
            raise RuntimeError("T3 control requires a loopback server origin")
        issued = _t3(spec, "auth", "session", "issue", "--subject", "mcp-client", "--scope", "orchestration:read",
                     "--scope", "orchestration:operate", "--ttl", "1m", "--label", "chief-of-stuff worker control", "--json")
        if issued.returncode:
            raise RuntimeError("cannot issue T3 worker-control session")
        credential = json.loads(issued.stdout)
        try:
            async with httpx2.AsyncClient(headers={"Authorization": "Bearer " + credential["token"]}, timeout=15) as http:
                async with streamable_http_client(origin + "/mcp", http_client=http) as streams:
                    async with ClientSession(*streams) as session:
                        await session.initialize()
                        result = await session.call_tool(tool, arguments)
                        if result.is_error or not isinstance(result.structured_content, dict):
                            raise RuntimeError(f"T3 {tool} failed")
                        return result.structured_content
        finally:
            _t3(spec, "auth", "session", "revoke", credential["sessionId"])
    try:
        return asyncio.run(asyncio.wait_for(call(), timeout=30))
    except Exception as exc:
        raise RuntimeError(f"T3 control unavailable ({type(exc).__name__})") from None


def thread_state(run_dir: Path, *, thread_id: str | None = None) -> dict:
    """Read thread liveness from T3; a missing/unreadable thread is unknown, never a dead process."""
    spec = json.loads((run_dir / "t3.json").read_text())
    if (run_dir / "start_failed.json").exists():
        return {"status": "failed"}
    if not thread_id and not (run_dir / "thread.json").exists():
        project = spec.get("project_id")
        if not project:
            cursor = 0
            while cursor is not None:
                listed = _mcp(spec, "t3_project_list", {"limit": 100, "cursor": cursor})
                project = next((p["id"] for p in listed["projects"] if p.get("workspaceRoot") == spec["tree"]), None)
                if project:
                    break
                cursor = listed.get("nextCursor")
            if not project:
                raise RuntimeError("T3 worker project is unconfirmed; preserving its launch")
        cursor = 0
        matches = []
        while cursor is not None:
            listed = _mcp(spec, "t3_thread_list", {"projectId": project, "titleContains": spec["title"], "limit": 100, "cursor": cursor})
            matches += [t for t in listed["threads"] if t.get("title") == spec["title"]]
            cursor = listed.get("nextCursor")
        if len(matches) != 1:
            raise RuntimeError("T3 start is unconfirmed; no second thread will be started")
        _write_thread(run_dir, spec, matches[0]["threadId"])
    thread = thread_id or json.loads((run_dir / "thread.json").read_text())["thread_id"]
    done = _t3(spec, "thread", "show", thread, "--json", "--messages", "1")
    if done.returncode:
        raise RuntimeError("cannot read T3 worker thread")
    shown = json.loads(done.stdout)["thread"]
    if not isinstance(shown, dict) or not isinstance(shown.get("status"), str):
        raise ValueError("incomplete T3 thread state")
    return shown


def refresh(run_dir: Path) -> dict:
    """Recover completion even when the detached poller died. A local exit never proves remote completion."""
    shown = observe(run_dir)
    code = outcome(shown)
    if code is not None:
        # Preserve a normal runner's reply and ended time; replace a killed poller's exit.
        try:
            old = json.loads((run_dir / "exit.json").read_text())
        except (OSError, ValueError):
            old = {}
        if not isinstance(old, dict) or old.get("exit_code") != code:
            from datetime import datetime, timezone
            _atomic(run_dir / "exit.json", {"exit_code": code, "ended_at": datetime.now(timezone.utc).isoformat()})
        for name in ("stdout", "stderr"):
            if not (run_dir / name).exists():
                (run_dir / name).write_text("")
    return shown


def classify(exit_code: int, stdout: str, stderr: str):
    """The host-adapter classify contract; T3 replies are plain text, not Claude CLI envelopes."""
    return host_adapter.classify("claude", exit_code, stdout, stderr)


def cancel(process, run_dir: Path | None = None) -> None:
    """Stop the remote worker first. Failure preserves the runner and its evidence for recovery."""
    run_dir = run_dir or getattr(process, "t3_run_dir", None)
    if not isinstance(run_dir, Path):
        raise RuntimeError("T3 cancellation requires its engine run directory")
    spec = json.loads((run_dir / "t3.json").read_text())
    if not (run_dir / "thread.json").exists():
        observe(run_dir)  # recover a start whose CLI reply was lost
    thread = json.loads((run_dir / "thread.json").read_text())
    run_id = thread.get("thread_run_id")
    if not run_id:
        if outcome(observe(run_dir)) is not None:
            claude_adapter.cancel(process)
            return
        raise RuntimeError("T3 worker run id is unconfirmed; refusing to interrupt a different run")
    _mcp(spec, "t3_thread_interrupt", {"threadId": thread["thread_id"], "runId": run_id})
    for _ in range(10):
        if outcome(observe(run_dir)) is not None:
            claude_adapter.cancel(process)
            return
        time.sleep(0.1)
    raise RuntimeError("T3 thread has not confirmed cancellation; preserving its runner")


if __name__ == "__main__":
    if sys.argv[1:2] == ["run"]:
        sys.exit(run(sys.argv[2], Path(sys.argv[3])))
