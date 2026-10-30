#!/usr/bin/env python3
"""Propose a least-conflict order for open changes using pairwise trial merges.

    chief-of-stuff merge-order --root R [--repo owner/name]

Fetches overlapping heads and writes merge-tree objects, without merging or moving
HEAD or the index. Pairwise clean chains do not prove a whole sequence stays clean.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from itertools import combinations
from pathlib import Path
from urllib.parse import urlencode, urlsplit

import backlog
import forge_review
import git_trees
from settings import SettingsError
from workspace import ConfigError

OID = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")


@dataclass(frozen=True)
class Change:
    number: int
    title: str
    url: str
    head: str
    base: str
    files: frozenset[str]
    complete: bool = True


@dataclass(frozen=True)
class Trial:
    status: str  # clean | conflict | unverified
    paths: tuple[str, ...] = ()
    why: str = ""


def pages(read, endpoint: str) -> tuple[list[dict], bool]:
    """Read bounded pages; a full last page leaves completeness unknown."""
    rows = []
    for page in range(1, backlog.MAX_PAGES + 1):
        query = urlencode({"per_page": backlog.PER_PAGE, "page": page})
        batch = read(endpoint + ("&" if "?" in endpoint else "?") + query)
        if not isinstance(batch, list) or any(not isinstance(row, dict) for row in batch):
            raise ValueError("expected a list of forge records")
        rows.extend(batch)
        if len(batch) < backlog.PER_PAGE:
            return rows, True
    return rows, False


def read_changes(home, github: bool, repo: str, gh=backlog.run_gh, call=backlog._call) -> list[Change]:
    if github:
        def read(url):
            code, out, err = gh(["api", url])
            if code:
                raise RuntimeError(f"gh api: {err.strip() or code}")
            return json.loads(out)

        code, out, err = gh(["pr", "list", "-R", repo, "--state", "open", "--limit", str(backlog.PER_PAGE),
                             "--json", "number,title,url,baseRefName,headRefOid"])
        if code:
            raise RuntimeError(f"gh pr list: {err.strip() or code}")
        listed = json.loads(out)
        if not isinstance(listed, list):
            raise ValueError("expected a list of pull requests")
        # `pr list` has no page option. A full answer needs the bounded REST read,
        # including its final short page; exactly 100 open PRs is not truncation.
        if len(listed) >= backlog.PER_PAGE:
            listed, complete = pages(read, f"repos/{repo}/pulls?state=open")
            if not complete:
                raise RuntimeError("open pull requests truncated")
            listed = [dict(number=r["number"], title=r["title"], url=r["html_url"],
                           headRefOid=r["head"]["sha"], baseRefName=r["base"]["ref"]) for r in listed]
        endpoint = f"repos/{repo}/pulls"
    else:
        token = backlog.token(home)

        def read(url):
            rows, _, err = call("GET", url, token, backlog.TIMEOUT)
            if err:
                raise RuntimeError(f"GitLab: {err}")
            return rows

        endpoint = home.api(repo) + "/merge_requests"
        listed, complete = pages(read, endpoint + "?state=opened")
        if not complete:
            raise RuntimeError("open merge requests truncated")
    found = []
    for row in listed:
        number = int(row["number" if github else "iid"])
        if number <= 0:
            raise ValueError("invalid change number")
        files, complete = pages(read, f"{endpoint}/{number}/{'files' if github else 'diffs'}")
        keys = ("filename", "previous_filename") if github else ("old_path", "new_path")
        paths = frozenset(f[key] for f in files for key in keys if f.get(key))
        # New GitLab versions can explicitly omit a diff even on a short page.
        complete = complete and not any(f.get("too_large") for f in files)
        found.append(Change(number, row["title"], row["url" if github else "web_url"],
                            row["headRefOid" if github else "sha"], row["baseRefName" if github else "target_branch"],
                            paths, complete))
    if len({c.number for c in found}) != len(found):
        raise ValueError("duplicate changes in forge listing; retry the read")
    return found


def overlapping(changes: list[Change]) -> list[tuple[int, int]]:
    return [(a.number, b.number) for a, b in combinations(sorted(changes, key=lambda c: c.number), 2)
            if a.complete and b.complete and a.base == b.base and a.files & b.files]


def order(changes: list[Change], trials: dict[tuple[int, int], Trial]) -> list[int]:
    """Disjoint prefix, deterministic walks of clean components, then conflicts.

    Unverified endpoints are omitted even when another trial involving them was clean.
    The original overlap graph still decides which changes are disjoint.
    """
    pairs = overlapping(changes)
    edges = {n for pair in pairs for n in pair}
    unknown = {n for pair in pairs if trials.get(pair, Trial("unverified")).status == "unverified" for n in pair}
    eligible = {c.number for c in changes if c.complete} - unknown
    result = sorted(eligible - edges)
    clean = {n: set() for n in eligible & edges}
    for pair in pairs:
        a, b = pair
        if a in eligible and b in eligible and trials[pair].status == "clean":
            clean[a].add(b)
            clean[b].add(a)
    remaining = {n for n in clean if clean[n]}
    while remaining:
        start = min(remaining)
        component, todo = set(), [start]
        while todo:
            n = todo.pop()
            if n not in component:
                component.add(n)
                todo.extend(clean[n] - component)
        current = start
        while component:
            result.append(current)
            component.remove(current)
            remaining.remove(current)
            if component:
                current = min(clean[current] & component or component)
    return result + sorted(eligible - set(result))


def run_git(root: Path, args: list[str], timeout: int, git=subprocess.run):
    return git(["git", *git_trees.GIT_WRITE_PINS, "-C", str(root), *args],
               capture_output=True, text=True, errors="backslashreplace", timeout=timeout,
               stdin=subprocess.DEVNULL, env=git_trees.write_env())


def check_checkout(root: Path, repo: str, git=subprocess.run) -> None:
    inside = run_git(root, ["rev-parse", "--is-inside-work-tree"], 15, git)
    if inside.returncode or inside.stdout.strip() != "true":
        raise ConfigError("root is not a git work tree")
    origin = run_git(root, ["config", "--get", "remote.origin.url"], 15, git)
    url = origin.stdout.strip()
    # Compare the whole project path, not a substring such as owner/repo-other.
    path = urlsplit(url).path if "://" in url else url.partition(":")[2]
    if origin.returncode or path.strip("/").removesuffix(".git") != repo:
        raise ConfigError("origin does not name the backlog project")


def fetch(root: Path, change: Change, github: bool, git=subprocess.run) -> str:
    if not isinstance(change.head, str) or not OID.fullmatch(change.head):
        return "forge head unread"
    source = "pull" if github else "merge-requests"
    dest = "pr" if github else "mr"
    ref = f"refs/remotes/origin/{dest}/{change.number}"
    try:
        for _ in range(2):
            # Disable configured ref mappings, pruning, and maintenance: an explicit
            # destination alone still permits opportunistic remote-tracking updates.
            done = run_git(root, ["fetch", "-q", "--no-tags", "--refmap=", "--no-prune", "--no-prune-tags",
                                  "--no-auto-maintenance", "--no-write-commit-graph", "origin",
                                  f"+refs/{source}/{change.number}/head:{ref}"], 60, git)
            if done.returncode == 0 or "cannot lock ref" not in done.stderr:
                break
        if done.returncode:
            return f"fetch exited {done.returncode}"
        head = run_git(root, ["rev-parse", "--verify", ref], 15, git)
        if head.returncode or head.stdout.strip() != change.head:
            return "fetched head differs from forge head"
    except (OSError, subprocess.SubprocessError) as exc:
        return f"fetch did not run ({type(exc).__name__})"
    return ""


def trial(root: Path, a: Change, b: Change, git=subprocess.run) -> Trial:
    if not all(isinstance(c.head, str) and OID.fullmatch(c.head) for c in (a, b)):
        return Trial("unverified", why="forge head unread")
    try:
        done = run_git(root, ["merge-tree", "--write-tree", "--name-only", "--no-messages", "-z",
                              "--end-of-options", a.head, b.head], 30, git)
    except (OSError, subprocess.SubprocessError) as exc:
        return Trial("unverified", why=f"merge-tree did not run ({type(exc).__name__})")
    if done.returncode not in (0, 1):
        return Trial("unverified", why=f"merge-tree exited {done.returncode}")
    fields = done.stdout.split("\0")
    if not OID.fullmatch(fields[0]) or fields[-1] != "" or (done.returncode == 0 and len(fields) != 2):
        return Trial("unverified", why="merge-tree output unread")
    return Trial("clean") if done.returncode == 0 else Trial("conflict", tuple(sorted(set(fields[1:-1]))))


def report(changes: list[Change], trials: dict[tuple[int, int], Trial], mark: str) -> str:
    """Shared text for the CLI and the board's merge-order.html page (#75)."""
    blocks = []
    for base in sorted({c.base for c in changes}):
        group = sorted((c for c in changes if c.base == base), key=lambda c: c.number)
        lines = [f"base {git_trees.clean(base)}"]
        for c in group:
            title = git_trees.clean(c.title.replace("\n", "").replace("\r", ""))
            lines.append(f"{mark}{c.number} {title}")
            lines.extend(f"  {git_trees.clean(p)}" for p in sorted(c.files))
        lines.append("order:" + "".join(f" {mark}{n}" for n in order(group, trials)))
        for c in group:
            if not c.complete:
                lines.append(f"unverified {mark}{c.number} files truncated")
        for a, b in overlapping(group):
            result = trials.get((a, b), Trial("unverified", why="trial unread"))
            if result.status == "conflict":
                lines.append(f"conflict {mark}{a} {mark}{b}")
                lines.extend(f"  {git_trees.clean(p)}" for p in result.paths)
            elif result.status == "unverified":
                lines.append(f"unverified {mark}{a} {mark}{b} {git_trees.clean(result.why)}")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def main(argv: list[str] | None = None, gh=backlog.run_gh, call=backlog._call, git=subprocess.run) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0], epilog="Pairwise clean results do not guarantee a clean sequential merge.")
    ap.add_argument("--root", type=Path, required=True, help="workspace holding CLAUDE.md and the git checkout")
    ap.add_argument("--repo", help="owner/name or group/name; default the backlog's")
    args = ap.parse_args(argv)
    try:
        _, home, github, repo = forge_review.forge(args.root, args.repo)
        check_checkout(args.root, repo, git)
    except (OSError, ConfigError, SettingsError, subprocess.SubprocessError) as exc:
        print(f"merge-order: {exc}", file=sys.stderr)
        return 2
    try:
        changes = read_changes(home, github, repo, gh, call)
    except (OSError, RuntimeError, ValueError, KeyError, TypeError, AttributeError) as exc:
        print(f"merge-order: {exc}", file=sys.stderr)
        return 1
    pairs = overlapping(changes)
    by_number = {c.number: c for c in changes}
    fetched = {n: fetch(args.root, by_number[n], github, git) for n in sorted({n for pair in pairs for n in pair})}
    trials = {}
    for a, b in pairs:
        why = fetched[a] or fetched[b]
        trials[a, b] = Trial("unverified", why=why) if why else trial(args.root, by_number[a], by_number[b], git)
    output = report(changes, trials, "#" if github else "!")
    if output:
        print(output)
    return 0


if __name__ == "__main__":
    sys.exit(main())
