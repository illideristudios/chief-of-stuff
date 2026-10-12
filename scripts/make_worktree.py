#!/usr/bin/env python3
"""Create a checked worktree and branch for a dispatch.

Branch and path are validated before Git writes. The branch is cut from a freshly fetched origin/main, or from local
main with --from-local. A failed add reports what it left and removes only an empty directory.
"""

from __future__ import annotations

import argparse
import fcntl
import os
import re
import subprocess
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import git_trees  # noqa: E402
import git_view  # noqa: E402
from md import section as _section  # noqa: E402
from settings import SettingsError, load as load_settings  # noqa: E402
from workspace import settings_path, worktrees_dir  # noqa: E402

AGENT = re.compile(r"^\s*(?:[-*]\s*)?Agent:\s*(\S+)\s+(\S+)\s*$", re.MULTILINE)
LIFETIMES = ("task", "standing")
GIT_TIMEOUT = 30  # writes and check-ref-format; reads use git_trees.GIT_TIMEOUT via git_trees.git


class RefusedError(ValueError):
    """Invalid worktree request or failed build; a failed add reports what it left and removes only an empty directory."""


@dataclass(frozen=True)
class Made:
    path: Path
    branch: str
    agent_type: str
    base: str  # "origin/main" or "local main"
    sha: str  # the base commit, short
    clone: Path  # the repository root the tree was cut from (#311)


def _run(args: list[str], cwd: Path, env: dict[str, str]) -> tuple[int, str]:
    """Keep helper failures distinguishable from Git's exit 1 (which can mean a missing ref)."""
    try:
        out = subprocess.run(
            ["git", "-C", str(cwd), *args], capture_output=True, text=True, errors="backslashreplace",
            timeout=GIT_TIMEOUT, env=env,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return git_trees.ReadFailure(1, f"{type(exc).__name__}: {exc}")
    return out.returncode, (out.stdout or out.stderr).strip()


def git(args: list[str], cwd: Path, *, expected_common: Path | None = None) -> tuple[int, str]:
    """Read through git_trees.git (#449). Helper failures become code 1 so git's 128 stays 'no such ref'."""
    try:
        result = git_trees.git(args, cwd, expected_common=expected_common)
    except git_view.CacheError as exc:
        return 1, f"CacheError: {exc}"
    if isinstance(result, git_trees.ReadFailure):
        return 1, result.detail
    return result


def git_trusted(args: list[str], cwd: Path) -> tuple[int, str]:
    """Run a bounded Git command in the clone with the write pins and no caller environment beyond its credentials
    (#556): the clone's config and hooks are worker-writable, so `worktree add` runs none of them."""
    return _run([*git_trees.GIT_WRITE_PINS, *args], cwd, git_trees.write_env())


def agent_types(claude_md: str) -> dict[str, str]:
    """Parse Agent declarations from the Coordinator block."""
    body = "\n".join(_section(claude_md, "## Coordinator"))
    types: dict[str, str] = {}
    for name, lifetime in AGENT.findall(body):
        if lifetime not in LIFETIMES:
            raise RefusedError(f"agent type {name!r} has lifetime {lifetime!r}; expected one of {', '.join(LIFETIMES)}")
        types[name] = lifetime
    return types


def check_type(claude_md: str, agent_type: str) -> None:
    """Check a declared type; an empty list permits the default behavior."""
    types = agent_types(claude_md)
    if types and agent_type not in types:
        raise RefusedError(f"no agent type {agent_type!r} in the Coordinator block; it lists {', '.join(sorted(types))}")


def check_branch(branch: str) -> None:
    """Validate a branch with Git after rejecting option-like names and rev-parse shorthands. The check needs no repository."""
    if not branch or branch.startswith("-"):
        raise RefusedError(f"branch name {branch!r} is empty or would be read as an option")
    if branch == "@" or branch.startswith("@{"):
        raise RefusedError(f"branch name {branch!r} is git's HEAD or previous-checkout shorthand, not a branch name")
    code, detail = _run(["check-ref-format", "--branch", branch], Path("/"), git_trees.audit_env())  # needs no repository
    if code != 0:
        raise RefusedError(f"git refuses the branch name {branch!r}: {detail or 'not a valid ref'}")


def resolve(root: Path, trees: str, name: str) -> Path:
    """Return an unused worktree path inside the workspace root."""
    if not name or name.startswith("-") or "/" in name or name in (".", ".."):
        raise RefusedError(f"worktree name {name!r} must be one path segment and not an option")
    base = (root / trees).resolve() if trees else root.resolve()
    candidate = (base / name).resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError:
        raise RefusedError(f"{name} resolves to {candidate}, outside the workspace root — refused") from None
    if candidate.exists():
        raise RefusedError(f"{candidate} already exists; a dispatch gets a tree of its own")
    return candidate


def require_root(clone: Path) -> None:
    """Refuse a path that is not itself the repository root git would report (#311)."""
    code, top = _run(["rev-parse", "--show-toplevel"], clone, git_trees.audit_env())
    if code != 0:
        bare_code, bare = _run(["rev-parse", "--is-bare-repository"], clone, git_trees.audit_env())
        if bare_code == 0 and bare == "true":
            code, top = _run(["rev-parse", "--absolute-git-dir"], clone, git_trees.audit_env())
    try:
        resolved = clone.resolve()
        root = Path(top).resolve() if code == 0 and top else None
    except OSError as exc:
        raise RefusedError(f"cannot read {clone}: {type(exc).__name__}: {exc}") from None
    if root != resolved:
        raise RefusedError(
            f"{clone} is not a repository root" + (f"; git finds {top}" if code == 0 and top else "")
        )


def discard(clone: Path, path: Path, cut: str | None) -> str:
    """Report what a failed add left; remove only an empty directory, never a tree or branch that could hold work."""
    warnings = []
    try:
        path.rmdir()  # never recursively remove contents, even if another add won the path
    except OSError:
        pass
    if os.path.lexists(path):
        warnings.append(f"left {path}: remove it after checking it")
    if cut is not None:
        probe = _run(["show-ref", "--verify", "--quiet", f"refs/heads/{cut}"], clone, git_trees.audit_env())
        if isinstance(probe, git_trees.ReadFailure) or probe[0] != 1:
            warnings.append(f"left branch {cut}")
    return "".join(f"; {warning}" for warning in warnings)


def base_ref(fetch_error: str, from_local: bool) -> str:
    """The ref to cut from: local main when asked, the fetched origin/main when the fetch worked, else a refusal."""
    if from_local:
        return "refs/heads/main"
    if fetch_error:
        raise RefusedError(f"fetching origin/main failed: {fetch_error}")
    return git_trees.REF


@contextmanager
def clone_lock(clone: Path):
    """Hold the clone's lock: a `git fetch` overlapping another dispatch's `git worktree add` fails with `bad object
    worktrees/<name>/HEAD`. The lock is on the git common directory, so every worktree of one repository shares it and no
    file is left behind. The wait is bounded by the holders' own git timeouts."""
    try:
        common = git_view.locate(clone).common
    except git_view.Unviewable:
        raise RefusedError(f"{clone} is not a git repository") from None
    fd = os.open(common, os.O_RDONLY)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield common
    finally:
        os.close(fd)


def build(root: Path, clone: Path, trees: str, name: str, branch: str, agent_type: str, from_local: bool = False,
          from_branch: str = "") -> Made:
    """Check everything that came out of a file, then make the tree. A failed add reports what it left and removes only
    an empty directory (a fetch may already have moved origin/main). `from_branch` (#62) checks an existing remote branch out into
    the tree instead of cutting one: it fetches first, and a branch the remote lacks is refused."""
    check_type((root / "CLAUDE.md").read_text() if (root / "CLAUDE.md").is_file() else "", agent_type)
    if not clone.is_dir():
        raise RefusedError(f"{clone} is not a directory; --clone names a [repos] entry or the repository's path")
    require_root(clone)
    if from_branch:
        if from_local:
            raise RefusedError("--from-branch fetches the branch from origin, --from-local cuts local main: take one")
        if branch != from_branch:
            raise RefusedError(f"--from-branch checks out {from_branch}; drop --branch or name it {from_branch}")
    check_branch(branch)
    path = resolve(root, trees, name)
    with clone_lock(clone) as common:
        if path.exists():  # resolve checked outside the lock; refuse rather than reuse a race winner
            raise RefusedError(f"{path} already exists; a dispatch gets a tree of its own")
        no_main, local = git(["log", "-1", "--format=%h %cr", "refs/heads/main"], clone, expected_common=common)  # git would quietly fall back to origin/main
        if no_main == 1:  # 128 is git's "no such ref"; 1 is git() failing to view or run, which is not "no main"
            raise RefusedError(f"cannot read {clone}: {local}")
        if from_local and no_main:
            raise RefusedError("no local main to cut from; drop --from-local")
        if from_branch:  # fetch, then the remote ref must exist: only the fetch can say the branch is there
            error = git_trees.fetch_branch(clone, branch)
            if not error:
                code, _ = git(["rev-parse", "--verify", "-q", f"refs/remotes/origin/{branch}"], clone, expected_common=common)
                if code != 0:
                    error = f"no branch {branch} on origin"
            if error:
                raise RefusedError(f"cannot cut from {branch}: {error}")
            ref, base = f"refs/remotes/origin/{branch}", f"origin/{branch}"  # detached at the fresh remote head: the claim stays with the tree that holds it (#115)
        else:
            try:
                ref = base_ref("" if from_local else git_trees.fetch_base(clone), from_local)
            except RefusedError as exc:
                raise RefusedError(f"{exc}; no local main" if no_main else f"{exc}; --from-local cuts from local main ({local}) instead") from None
            base = "local main" if from_local else "origin/main"
        # Only report a leftover branch if it was absent before this add.
        cut = None
        if not from_branch:
            probe = _run(["show-ref", "--verify", "--quiet", f"refs/heads/{branch}"], clone, git_trees.audit_env())
            probe_code, probe_detail = probe
            if isinstance(probe, git_trees.ReadFailure) or probe_code not in (0, 1):
                raise RefusedError(f"cannot check whether branch {branch!r} exists: {probe_detail or f'git exited {probe_code}'}")
            if probe_code == 1:
                cut = branch
        path.parent.mkdir(parents=True, exist_ok=True)
        add = ["worktree", "add"] + (["--detach"] if from_branch else ["--no-track", "-b", branch]) + ["--", str(path), ref]
        code, detail = git_trusted(add, clone)
        if code != 0:
            cleanup = discard(clone, path, cut)
            raise RefusedError(f"git worktree add failed: {detail}{cleanup}")
        sha = r[1] if (r := git(["rev-parse", "--short", "HEAD"], path, expected_common=common))[0] == 0 else "?"  # not git's error text
    return Made(path, branch, agent_type, base, sha, clone)


def line(made: Made) -> str:
    return f"worktree {made.path} on {made.branch} off {made.base} {made.sha} for a {made.agent_type} from {made.clone}"


def pick_clone(root: Path, given: str) -> Path:
    """The repository to cut from (#54): a `given` naming a `[repos]` entry is that entry's path, relative to `root`;
    any other `given` is a path as it stands. Settings that cannot be read refuse, whatever `given` is."""
    try:
        repos = load_settings(root, settings_path((root / "CLAUDE.md").read_text())).repos
    except (OSError, SettingsError) as exc:
        raise RefusedError(str(exc)) from None
    return root / repos[given] if given in repos else Path(given)  # an absolute value replaces the root


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--type", dest="agent_type", required=True, help="an agent type from the Coordinator block")
    ap.add_argument("--name", required=True, help="the worktree directory name, one path segment")
    ap.add_argument("--branch", default="", help="the new branch, cut from origin/main unless --from-local or --from-branch")
    ap.add_argument("--from-local", action="store_true", help="cut from local main, skipping the fetch of origin/main")
    ap.add_argument("--from-branch", default="", metavar="BRANCH",
                    help="check an existing remote branch out into the new tree instead of cutting one (#62)")
    ap.add_argument("--root", default=".", help="workspace root holding CLAUDE.md")
    ap.add_argument("--clone", required=True, help="the repository the worktree belongs to: a name from [repos] in the workspace settings, or the repository's path")
    args = ap.parse_args(argv)

    root = Path(args.root)
    if not (root / "CLAUDE.md").is_file():
        print(f"make_worktree: no CLAUDE.md at {root}", file=sys.stderr)
        return 2
    try:
        if args.clone == "":
            raise RefusedError("--clone is empty; name a [repos] entry or the repository's path")
        clone = pick_clone(root, args.clone)
        branch = args.branch or args.from_branch  # a --from-branch with its own --branch still reaches build's refusal
        made = build(root, clone, worktrees_dir((root / "CLAUDE.md").read_text()), args.name, branch,
                     args.agent_type, args.from_local, from_branch=args.from_branch)
    except RefusedError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 1
    print(line(made))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
