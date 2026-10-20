"""Codex, Cursor and Antigravity workers behind the model port (§13, #700).

A host's argv and bootstrap come from its `runtimes.py` entry; the detached runner, its evidence and cancel are
`claude_adapter`'s. What differs per host lives here: how the engine channel (`engine_mcp.py`, `report` and `ask`)
is registered, and how an exit is classified. None of these hosts can load the guard plugin, so `git`, `gh` and
`glab` run behind a PATH shim that refuses what `guard_commands` refuses: a guard rail, as the hook is, never the
boundary. The engine's own checks remain the boundary (guard/README.md).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import claude_adapter
import git_trees
import make_worktree
import runtimes
from model_port import Failure, Reply, Request, quota_reset
from shell_setup import ShellError, login_argv, resolve

SCRIPTS = Path(__file__).resolve().parent
HOSTS = ("codex", "cursor", "agy")
SHIMMED = ("git", "gh", "glab")
SHIM = '''#!{python}
"""chief-of-stuff worker guard shim (#700): {name} behind guard_commands, then the real {name}."""
import json, os, shlex, sys
sys.path.insert(0, {scripts!r})
import guard_commands
protected = tuple(json.loads(os.environ.get("CHIEF_PROTECTED_BRANCHES") or "[]"))
reason = guard_commands.denial_reason(shlex.join([{name!r}, *sys.argv[1:]]), protected)
if reason:
    sys.exit("chief-of-stuff guard: " + reason)
here = os.path.dirname(os.path.abspath(__file__))
for folder in os.environ.get("PATH", "").split(os.pathsep):
    real = os.path.join(folder, {name!r})
    if folder and os.path.abspath(folder) != here and os.path.isfile(real) and os.access(real, os.X_OK):
        os.execv(real, [real, *sys.argv[1:]])
sys.exit("chief-of-stuff guard: no {name} on PATH")
'''


def available(runtime: str) -> bool:
    """Whether this engine can launch `runtime` here: an adapter exists and its CLI is on the login shell's PATH."""
    return runtime in HOSTS and resolve(runtimes.binary(runtime)) is not None


def shim_dir(run_dir: Path, python: str = sys.executable) -> Path:
    """Write the git/gh/glab shims for one run; the launch puts this directory first on the host's PATH."""
    folder = run_dir / "bin"
    folder.mkdir(exist_ok=True)
    for name in SHIMMED:
        path = folder / name
        path.write_text(SHIM.format(python=python, scripts=str(SCRIPTS), name=name))
        path.chmod(0o755)
    return folder


def _channel(env: dict[str, str]) -> tuple[str, list[str], dict[str, str]]:
    """The engine MCP server as (command, args, env): the engine venv's Python, as the guard plugin's .mcp.json runs it."""
    binding = {key: value for key, value in env.items() if key.startswith("CHIEF_")}
    return binding.get("CHIEF_PYTHON") or sys.executable, [str(SCRIPTS / "engine_mcp.py")], binding


def _exclude(tree: Path, entry: str) -> None:
    """Keep a per-run file out of every commit without touching the tree's tracked .gitignore."""
    # The real repository, not git_trees' sanitised view: the view's info/exclude is a throwaway copy.
    code, path = make_worktree.git_trusted(["rev-parse", "--git-path", "info/exclude"], tree)
    if code:
        raise ShellError("cannot locate the worker tree's info/exclude")
    exclude = Path(path) if Path(path).is_absolute() else tree / path
    lines = exclude.read_text().splitlines() if exclude.exists() else []
    if entry not in lines:
        exclude.parent.mkdir(parents=True, exist_ok=True)
        exclude.write_text("\n".join([*lines, entry]) + "\n")


def _register_codex(request: Request, command: str, args: list[str], binding: dict[str, str]) -> list[str]:
    table = ", ".join(f"{key} = {json.dumps(value)}" for key, value in sorted(binding.items()))
    return ["-c", f"mcp_servers.engine.command={json.dumps(command)}",
            "-c", f"mcp_servers.engine.args={json.dumps(args)}",
            "-c", f"mcp_servers.engine.env={{{table}}}",
            # `-a never` refuses any MCP call that needs approval, the report tool included.
            "-c", 'mcp_servers.engine.default_tools_approval_mode="approve"',
            *git_trees.codex_add_dir_args(Path(request.cwd))]


def _cursor_config_replaceable(config: Path, binding: dict[str, str]) -> bool:
    """Whether the tree's existing `.cursor/mcp.json` is the engine's own channel from an earlier run's retry."""
    try:
        found = json.loads(config.read_text()).get("mcpServers")
    except (ValueError, AttributeError, OSError):
        found = None
    earlier = found.get("engine") if isinstance(found, dict) and set(found) == {"engine"} else None
    given = earlier.get("args") if isinstance(earlier, dict) else None
    bound = earlier.get("env") if isinstance(earlier, dict) else None
    # The engine's own channel from an earlier run (any release's engine_mcp.py, a CHIEF_ binding) is
    # replaced on a retry; a second registration of this run, or anything else in the tree, is kept.
    ours_before = (isinstance(given, list) and len(given) == 1 and Path(str(given[0])).name == "engine_mcp.py"
                   and isinstance(bound, dict) and any(str(key).startswith("CHIEF_") for key in bound))
    return ours_before and bound != binding


def _register_cursor(request: Request, command: str, args: list[str], binding: dict[str, str]) -> None:
    tree = Path(request.cwd)
    config = tree / ".cursor" / "mcp.json"
    if config.exists() and not _cursor_config_replaceable(config, binding):
        raise ShellError("the worker tree already has .cursor/mcp.json; refusing to replace it")
    config.parent.mkdir(exist_ok=True)
    config.write_text(json.dumps({"mcpServers": {"engine": {"command": command, "args": args, "env": binding}}}))
    _exclude(tree, "/.cursor/mcp.json")


def _register_agy(request: Request, command: str, args: list[str], binding: dict[str, str]) -> str:
    name = f"chief-{request.session_id}"
    binary = resolve(runtimes.binary("agy"))
    if not binary:
        raise ShellError("`agy` is not on the login shell's PATH")
    flags = [part for key, value in sorted(binding.items()) for part in ("-e", f"{key}={value}")]
    done = subprocess.run(login_argv([binary, "mcp", "add", *flags, name, command, *args]),
                          capture_output=True, text=True, timeout=60, env=claude_adapter.model_env())
    if done.returncode:
        raise ShellError(f"agy mcp add failed: {(done.stderr or done.stdout).strip()[:200]}")
    return name


def register(runtime: str, request: Request, env: dict[str, str]) -> tuple[list[str], str]:
    """Register the engine channel for one run: (extra argv for the host, the agy server name to remove later).

    The spike of 2026-10-17 settled each host: Codex takes it per launch with `-c`; Cursor reads only
    `.cursor/mcp.json` and passes no environment of its own, so the binding goes in the file; Antigravity keeps a
    global list, so each run adds a uniquely named entry and `cleanup` removes it.
    """
    command, args, binding = _channel(env)
    if runtime == "codex":
        return _register_codex(request, command, args, binding), ""
    if runtime == "cursor":
        _register_cursor(request, command, args, binding)
        return [], ""
    if runtime == "agy":
        return [], _register_agy(request, command, args, binding)
    raise ValueError(f"no host adapter for {runtime}")


def cleanup(record: dict) -> None:
    """Remove what `register` left outside the run directory; a missing entry is already clean."""
    name = record.get("agy_mcp")
    binary = resolve(runtimes.binary("agy")) if name else None
    if name and binary:
        subprocess.run(login_argv([binary, "mcp", "remove", name]), capture_output=True, text=True, timeout=60,
                       env=claude_adapter.model_env())


def start(runtime: str, request: Request, run_dir: Path, *, env: dict[str, str]) -> tuple[subprocess.Popen, str]:
    """Start a detached worker on a non-Claude host: (runner, agy server name or "")."""
    if runtime not in HOSTS:
        raise ValueError(f"no host adapter for {runtime}")
    entry = runtimes.get(runtime)
    binary = resolve(entry.binary)
    if not binary:
        raise ShellError(f"`{entry.binary}` is not on the login shell's PATH")
    assignment = run_dir / "assignment.md"
    assignment.write_text(request.prompt)
    prompt = runtimes.WORKER_BOOTSTRAP.format(assignment=assignment, cwd=request.cwd, session=request.session_id)
    extra, name = register(runtime, request, env)
    try:
        argv = entry.worker_command(binary, request.cwd, request.model, request.effort, prompt, extra)
        prepared = login_argv(argv, path_prefix=str(shim_dir(run_dir, env.get("CHIEF_PYTHON") or sys.executable)))
        return claude_adapter.spawn(prepared, request, run_dir, env), name
    except Exception:
        cleanup({"agy_mcp": name})
        raise


def classify(runtime: str, exit_code: int, stdout: str, stderr: str) -> Reply | Failure:
    """The port's result shape from a host that has no JSON envelope: a quota line is retryable, a missing CLI is
    named, a clean exit is a reply (the worker's own report still decides completion), anything else is unknown."""
    text = f"{stdout}\n{stderr}"
    reset = quota_reset(text)
    if reset is not None:
        return Failure("quota", reset or f"{runtime} reported an exhausted quota")
    if exit_code == 0:
        return Reply(text=stdout[-4000:], data=None, session_id="", cost_usd=0.0, num_turns=0)
    if exit_code == 127:
        return Failure("missing_program", f"{runtime}: {stderr.strip()[-300:] or 'command not found'}")
    return Failure("unknown", f"{runtime} exited {exit_code}: {(stderr or stdout).strip()[-300:]}")


def _structured(runtime: str, stdout: str, out: Path) -> object:
    """The answer each host gives a schema: Codex writes it to `-o`, agy returns `structured_output`, Cursor ends its
    result text with it."""
    if runtime == "codex":
        return json.loads(out.read_text()) if out.is_file() else None
    envelope = json.loads(stdout)
    if runtime == "agy":
        return envelope.get("structured_output") if envelope.get("status") == "SUCCESS" else None
    if envelope.get("is_error"):
        return None
    text, decoder, found = str(envelope.get("result") or ""), json.JSONDecoder(), None
    for start in (i for i, ch in enumerate(text) if ch == "{"):
        try:
            value, _ = decoder.raw_decode(text[start:])
        except ValueError:
            continue
        found = value if isinstance(value, dict) else found
    return found


def judge(request: Request) -> Reply | Failure:
    """A read-only judgement call on Codex, Cursor or agy, the trio's port for those hosts (§6, #879). It runs in a
    fresh empty directory under the host's read-only mode, with no engine channel and no write access."""
    entry = runtimes.get(request.runtime) if request.runtime in HOSTS else None
    if entry is None or not entry.judge_argv:
        return Failure("missing_program", f"{request.runtime or 'this host'} has no read-only judgement mode")
    binary = resolve(entry.binary)
    if not binary:
        return Failure("missing_program", f"`{entry.binary}` is not on the login shell's PATH")
    schema = json.dumps(request.schema or {"type": "object"})
    with tempfile.TemporaryDirectory(prefix="cos-judge-") as tmp:
        (Path(tmp) / "schema.json").write_text(schema)
        out = Path(tmp) / "answer.json"
        fill = {"cwd": tmp, "schema": str(Path(tmp) / "schema.json"), "out": str(out), "schema_text": schema}
        prompt = f"{request.prompt}\nAnswer with only one JSON object matching this schema: {schema}"
        argv = [binary, *(token.format(**fill) for token in entry.judge_argv),
                *(["--model", request.model] if request.model else []), *entry.effort_tokens(request.effort),
                f"{entry.judge_prompt}{prompt}"]
        try:
            done = subprocess.run(login_argv(argv), capture_output=True, text=True, stdin=subprocess.DEVNULL, cwd=tmp,
                                  timeout=request.timeout_s, env=claude_adapter.model_env())
        except subprocess.TimeoutExpired:
            return Failure("unknown", f"{request.runtime} timed out after {request.timeout_s}s")
        except (OSError, ShellError) as exc:
            return Failure("missing_program", f"{request.runtime}: {exc}")
        got = classify(request.runtime, done.returncode, done.stdout, done.stderr)
        if isinstance(got, Failure):
            return got
        try:
            data = _structured(request.runtime, done.stdout, out)
        except (ValueError, OSError, AttributeError) as exc:
            return Failure("error", f"{request.runtime} gave no readable answer: {exc}")
    if data is None:
        return Failure("error", f"{request.runtime} gave no structured answer")
    return Reply(text=done.stdout[-4000:], data=data, session_id="", cost_usd=0.0, num_turns=1)


def branch_heads(tree: Path, branches) -> dict[str, str]:
    """Each branch's current head on the tree's origin, in one read; "" for a branch that is unreadable or absent."""
    branches = list(dict.fromkeys(branches))
    code, out = make_worktree.git_trusted(["ls-remote", "origin", *(f"refs/heads/{b}" for b in branches)], tree)
    found = {}
    for line in ([] if code else out.splitlines()):
        sha, _, ref = line.partition("\t")
        if ref.startswith("refs/heads/") and sha:
            found[ref[len("refs/heads/"):]] = sha
    return {branch: found.get(branch, "") for branch in branches}


def engine_merges(store) -> set[str]:
    """Every merge commit the engine recorded (engine_merge's structured `merges`), across all issues."""
    found = set()
    for item in store.items():
        data = store.resume(item.issue_key)
        for merge in (data.get("merges", []) if isinstance(data, dict) else []):
            if isinstance(merge, dict) and isinstance(merge.get("sha"), str) and merge["sha"]:
                found.add(merge["sha"])
    return found


def moved_outside_engine(store, record: dict, now: str) -> str:
    """§13: why a non-Claude run holds for human review, or "" when every first-parent commit each protected branch
    gained during the run is a merge the engine recorded (#860). A direct push, a merge outside the engine, or an
    unreadable branch or range holds (ledger F3: unknown is never safe), naming each commit and its branch."""
    heads = record.get("branch_heads")
    if not isinstance(heads, dict):  # a run started before #860 recorded the default branch alone
        heads = {record["default_branch"]: record.get("default_head", "")} if record.get("default_branch") else {}
    runtime, tree = record.get("runtime"), Path(record["tree"])
    problems, recorded = [], None
    for branch, before in sorted(heads.items()):
        fetched, _ = make_worktree.git_trusted(["fetch", "-q", "origin", f"refs/heads/{branch}"], tree)
        after = branch_heads(tree, [branch])[branch]
        if fetched or not before or not after:
            problems.append(f"cannot verify {branch}")
            continue
        if before == after:
            continue
        code, out = make_worktree.git_trusted(["rev-list", "--first-parent", f"{before}..{after}"], tree)
        if code:
            problems.append(f"cannot list {branch} from {before[:12]} to {after[:12]}")
            continue
        recorded = engine_merges(store) if recorded is None else recorded
        unknown = [commit for commit in out.split() if commit not in recorded]
        if unknown:
            problems.append(f"{branch} gained commits the engine did not merge: {', '.join(c[:12] for c in unknown)}")
    if not problems:
        return ""
    return f"{'; '.join(problems)} during a {runtime} run; holding for human review"
