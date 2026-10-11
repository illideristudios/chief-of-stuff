#!/usr/bin/env python3
"""Register a Ghostty worker's PID, then replace this process with its CLI."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import runtimes
import git_trees
from one_shot import trees_lock
from shell_setup import clean_env, exec_login, resolve
from workspace import worktrees_dir


def register(path: Path, *, runtime: str, name: str, worktree: str, pid: int) -> None:
    # Registries live at <workspace>/.chief-of-stuff/sessions/<name>.json, so no launcher argument is needed.
    root = path.absolute().parents[2]
    try:
        text = (root / "CLAUDE.md").read_text()
    except (OSError, UnicodeDecodeError):
        text = ""  # cleanup cannot run without a readable CLAUDE.md, so the root's lock is enough
    trees = root / worktrees_dir(text)
    trees.mkdir(parents=True, exist_ok=True)
    with trees_lock(trees):
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {"runtime": runtime, "name": name, "worktree": worktree, "pid": pid,
                "started": datetime.now(timezone.utc).isoformat()}
        temp = path.with_suffix(".tmp")
        temp.write_text(json.dumps(data) + "\n")
        temp.replace(path)


def alive(path: Path) -> bool:
    """A worker is live only while its registered PID still runs in its worktree."""
    try:
        data = json.loads(path.read_text())
        pid = int(data["pid"])
        if pid <= 0:
            return False
        os.kill(pid, 0)
        # A reused PID in a different tree must not resurrect an old worker.
        out = subprocess.run(["ps", "-ww", "-p", str(pid), "-o", "command="], capture_output=True,
                             text=True, timeout=3)
        binary = runtimes.binary(data["runtime"])
        return out.returncode == 0 and binary in out.stdout and data["worktree"] in out.stdout
    except (OSError, ValueError, KeyError, TypeError):
        return False


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "exec-login":
        rest = argv[1:]
        registration: dict[str, str] = {}
        while rest and rest[0] in ("--registry", "--name", "--worktree"):
            if len(rest) < 2:
                raise SystemExit(f"expected a value after {rest[0]}")
            registration[rest[0]] = rest[1]
            rest = rest[2:]
        if len(rest) < 2 or rest[0] != "--":
            raise SystemExit("expected exec-login -- followed by the runtime command")
        # exec-login is only built for the claude wrapper. The herdr launcher verifies this
        # registration after `pane run` (#551); a bare exec-login (tmux, Ghostty) stays unregistered,
        # exactly as before. The pane's cwd is the worktree the tab was created in.
        if "--registry" in registration and "--name" in registration:
            register(Path(registration["--registry"]), runtime="claude", name=registration["--name"],
                     worktree=registration.get("--worktree", os.getcwd()), pid=os.getpid())
        exec_login(rest[1:])
    if argv and argv[0] == "resume-codex":
        resume = argparse.ArgumentParser(description="Resume an approved Codex plan with write access")
        resume.add_argument("--root", type=Path, required=True)
        resume.add_argument("--name", required=True)
        resume.add_argument("--worktree", type=Path, required=True)
        resume.add_argument("--session-id", required=True)
        options = resume.parse_args(argv[1:])
        binary = resolve(runtimes.get("codex").binary)
        if not binary:
            resume.error("codex is unavailable on PATH")
        path = options.root / ".chief-of-stuff" / "sessions" / f"{options.name}.json"
        register(path, runtime="codex", name=options.name, worktree=str(options.worktree), pid=os.getpid())
        command = [binary, "resume", options.session_id, "-C", str(options.worktree), "-s", "workspace-write",
                   *git_trees.codex_add_dir_args(options.worktree)]
        exec_login(command)
    if argv and argv[0] == "list":
        listing = argparse.ArgumentParser(description="List registered non-Claude workers and liveness")
        listing.add_argument("--root", type=Path, required=True)
        options = listing.parse_args(argv[1:])
        folder = options.root / ".chief-of-stuff" / "sessions"
        for path in sorted(folder.glob("*.json")):
            try:
                record = json.loads(path.read_text())
                record["alive"] = alive(path)
                print(json.dumps(record))
            except (OSError, ValueError):
                continue
        return 0
    ap = argparse.ArgumentParser()
    ap.add_argument("--registry", type=Path, required=True)
    ap.add_argument("--runtime", required=True)
    ap.add_argument("--name", required=True)
    ap.add_argument("--worktree", required=True)
    ap.add_argument("--login-shell", action="store_true",
                    help="load the user's login shell setup before starting the CLI")
    args, command = ap.parse_known_args(argv)
    if not command or command[0] != "--" or len(command) < 2:
        ap.error("expected -- followed by the runtime command")
    register(args.registry, runtime=args.runtime, name=args.name, worktree=args.worktree, pid=os.getpid())
    # Keep login and terminal essentials; drop inherited host session identity and launch overrides.
    if args.login_shell:
        exec_login(command[1:])
    os.execve(command[1], command[1:], clean_env())


if __name__ == "__main__":
    raise SystemExit(main())
