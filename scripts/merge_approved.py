#!/usr/bin/env python3
"""Compatibility listing of merge readiness; the engine executes every merge through its policy gate.

Legacy owner approval modes do not hold work. Use `chief-of-stuff engine --root R --once`
to advance reviewed changes. `merge_head` remains the forge adapter's head-pinned write.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import backlog
from forge_review import NO_PIPELINE, Request, forge, github_requests, gitlab_requests  # noqa: F401  (re-exported)
from workspace import ConfigError
from settings import SettingsError


def blocked(r: Request) -> list[str]:
    """What stops an approved request, for `approval`: a request with no pipeline stays ready, as its docstring says."""
    return [b for b in r.blockers if b != NO_PIPELINE]


# GitHub's own order for the methods a repository allows (its merge button's default), never a choice of ours (#864).
GITHUB_METHODS = (("allow_merge_commit", "--merge"), ("allow_squash_merge", "--squash"), ("allow_rebase_merge", "--rebase"))


def allowed_method(repo: str, gh=backlog.run_gh) -> str:
    """The `gh pr merge` flag for the first method the repository allows, or "error: <why>" when none is or the
    settings are unreadable (Zach, 2026-10-18: "we shouldn't prefer a merge strategy")."""
    code, out, err = gh(["api", f"repos/{repo}"])
    if code:
        return f"error: repository merge settings: {err.strip() or f'gh api exited {code}'}"
    try:
        settings = json.loads(out)
    except ValueError as exc:
        return f"error: repository merge settings: {exc}"
    if not isinstance(settings, dict):
        return "error: repository merge settings: not an object"
    flag = next((flag for key, flag in GITHUB_METHODS if settings.get(key) is True), "")
    return flag or "error: the repository allows no merge method"


def merge_head(one: Request, home, github: bool, repo: str, gh=backlog.run_gh,
               call=backlog._call, token: str | None = None) -> tuple[int, str]:
    """Merge at the checked head; return the status code and merged/queued/unknown, or an error."""
    if github:
        method = allowed_method(repo, gh)
        if method.startswith("error: "):
            return 1, method[len("error: "):]
        code, _, err = gh(["pr", "merge", str(one.number), "-R", repo, method, "--match-head-commit", one.head])
        if code:
            return code, err.strip()
    else:
        response, _, err = call("PUT", f"{home.api(repo)}/merge_requests/{one.number}/merge", token,
                               backlog.TIMEOUT, {"sha": one.head})
        if err:
            if err == "GitLab did not answer with JSON":
                return 0, f"unknown: {err}"
            return 1, err
    # The write succeeded. An unreadable state cannot tell us whether it merged or queued.
    try:
        if github:
            code, out, err = gh(["pr", "view", str(one.number), "-R", repo, "--json", "state"])
            if code:
                return 0, f"unknown: {err.strip() or f'gh pr view exited {code}'}"
            state = json.loads(out)["state"].lower()
        else:
            state = response["state"]
    except (RuntimeError, ValueError, KeyError, TypeError, AttributeError) as exc:
        return 0, f"unknown: {exc}"
    if state == "merged":
        return 0, "merged"
    if state in ("open", "opened"):
        return 0, "queued"
    return 1, f"request state after merge: {state}"


def main(argv: list[str] | None = None, gh=backlog.run_gh, call=backlog._call) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--root", type=Path, required=True, help="workspace holding CLAUDE.md")
    ap.add_argument("--repo", help="owner/name or group/name; default the backlog's")
    ap.add_argument("--merge", type=int, metavar="N", help="merge N if it is ready")
    args = ap.parse_args(argv)
    try:
        workflow, home, github, repo = forge(args.root, args.repo)
    except (OSError, ConfigError, SettingsError) as exc:
        print(f"merge-approved: {exc}", file=sys.stderr)
        return 2
    if args.merge is not None:
        print("merge-approved: merges run through the engine's automated review and CI gate; "
              "run chief-of-stuff engine --root <workspace> --once", file=sys.stderr)
        return 2
    try:
        import merge_ready
        found = merge_ready.read(home, github, repo, gh, call)
    except (RuntimeError, ValueError, KeyError, TypeError, AttributeError) as exc:
        print(f"merge-approved: {exc}", file=sys.stderr)
        return 1
    mark = "#" if github else "!"
    for request in sorted(found, key=lambda request: (request.verdict != "ready", request.number)):
        print(merge_ready.line(request, mark))
    return 0


if __name__ == "__main__":
    sys.exit(main())
