#!/usr/bin/env python3
"""List the open pull or merge requests ready to merge, and merge one that is.

    chief-of-stuff merge-approved --root R [--repo owner/name]            list, ready ones first
    chief-of-stuff merge-approved --root R [--repo owner/name] --merge N  merge N, or say what blocks it

With `[workflow] merge_owner = "approval"` (the user, 2026-09-26 01:57, #97: "I want to be able to mark a PR
as approved, and you handle merging in what I mark approved."): `approver` is the user's forge username, and
no other account's approval counts. Ready is all three: the approver's approval on the head commit, a
pipeline that passed (or none), and no conflict. The merge names the head it checked, so a push after the
check fails the merge instead of landing unreviewed code.

With `merge_owner = "worker"` (#420): the owning worker merges a request once its reviewer cleared it and its
head pipeline is green, so this command only lists. Each open request prints its merge-ready line (head sha,
pipeline id, status and sha, conflicts, unresolved threads), ready ones first, then one line naming the ready
ones for their owning worker. It never merges and refuses `--merge`. Here a request with no pipeline is not ready: a
green pipeline on the head sha is what a worker merges on. With `user` merging stays the user's.

On GitHub, a merge uses the first method the repository allows (merge commit, then squash, then rebase), read from
the repository settings; if none are allowed or the settings cannot be read, the merge is refused.
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


_MERGE_SETTING_KEYS = ("allow_merge_commit", "allow_squash_merge", "allow_rebase_merge")
_MERGE_FLAGS = ("--merge", "--squash", "--rebase")


def github_merge_flag(repo: str, gh) -> tuple[str | None, str]:
    """Return the `gh pr merge` flag GitHub allows for this repository, or refuse with why."""
    code, out, err = gh(["api", f"repos/{repo}"])
    if code != 0:
        return None, (err.strip() or "could not read repository settings")
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return None, "could not read repository settings"
    if not isinstance(data, dict):
        return None, "could not read repository settings"
    settings: dict[str, bool] = {}
    for key in _MERGE_SETTING_KEYS:
        if key not in data:
            return None, "could not read repository settings"
        val = data[key]
        if not isinstance(val, bool):
            return None, "could not read repository settings"
        settings[key] = val
    for key, flag in zip(_MERGE_SETTING_KEYS, _MERGE_FLAGS, strict=True):
        if settings[key]:
            return flag, ""
    return None, "repository allows no merge method"


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
    worker = workflow.merge_owner == "worker"
    if workflow.merge_owner != "approval" and not worker:
        print(f"merge-approved: [workflow] merge_owner is {workflow.merge_owner}; merging on approval is "
              "off, and merging stays the user's", file=sys.stderr)
        return 2
    if worker and args.merge is not None:
        print("merge-approved: [workflow] merge_owner is worker; the owning worker merges, this command only lists",
              file=sys.stderr)
        return 2
    try:
        if worker:
            import merge_ready  # only the worker listing needs it
            found = merge_ready.read(home, github, repo, gh, call)
        elif github:
            found = github_requests(repo, workflow.approver, gh)
        else:
            secret = backlog.token(home)
            found = gitlab_requests(home, repo, workflow.approver, secret, call)
    except (RuntimeError, ValueError, KeyError, TypeError, AttributeError) as exc:
        print(f"merge-approved: {exc}", file=sys.stderr)
        return 1
    mark = "#" if github else "!"
    if worker:
        for r in sorted(found, key=lambda r: (r.verdict != "ready", r.number)):
            print(merge_ready.line(r, mark))
        ready = [f"{mark}{r.number}" for r in found if r.verdict == "ready"]
        if ready:
            print("ready for their owning worker to merge: " + " ".join(sorted(ready, key=lambda n: int(n[1:]))))
        return 0
    if args.merge is None:
        for r in sorted(found, key=lambda r: (bool(blocked(r)), r.number)):
            print(f"{mark}{r.number} {'ready' if not blocked(r) else '; '.join(blocked(r))} · {r.title} · {r.url}")
        return 0
    one = next((r for r in found if r.number == args.merge), None)
    if one is None:
        print(f"merge-approved: {mark}{args.merge} is not an open request in {repo}", file=sys.stderr)
        return 1
    if blocked(one):
        print(f"merge-approved: {mark}{one.number} not merged: {'; '.join(blocked(one))}", file=sys.stderr)
        return 1
    if github:
        flag, why = github_merge_flag(repo, gh)
        if flag is None:
            print(f"merge-approved: {mark}{one.number} not merged: {why}", file=sys.stderr)
            return 1
        code, _, err = gh(["pr", "merge", str(one.number), "-R", repo, flag, "--match-head-commit", one.head])
        err = err.strip() if code else ""
    else:
        _, _, err = call("PUT", f"{home.api(repo)}/merge_requests/{one.number}/merge", secret,
                         backlog.TIMEOUT, {"sha": one.head})
    if err:
        print(f"merge-approved: {mark}{one.number} not merged: {err}", file=sys.stderr)
        return 1
    print(f"{mark}{one.number} merged at {one.head[:12]} · {one.url}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
