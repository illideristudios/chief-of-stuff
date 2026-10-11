#!/usr/bin/env python3
"""List the review threads the user opened on open pull or merge requests, and reply to one.

    chief-of-stuff review-threads --root R [--repo owner/name]                        needs work first
    chief-of-stuff review-threads --root R [--repo owner/name] --reply REF --body TEXT

The user, 2026-09-26 (#104): "I also want to be able to leave review comments in PRs and have them get
addressed and picked up." A thread counts when `[workflow] approver`, the user's forge username, wrote in
it and it is unresolved. It needs work while the user has the last word, and awaits the user once someone
answers. A reply never resolves a thread: resolving is the user's. REF is `#12/<comment id>` on GitHub and
`!5/<discussion id>` on GitLab, as the listing prints it.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlencode

import backlog
from forge_review import forge
from workspace import ConfigError
from settings import SettingsError

LIMIT = 100  # open requests, threads per request and comments per thread, each read once
QUERY = f"""query($owner: String!, $name: String!) {{ repository(owner: $owner, name: $name) {{
  pullRequests(states: OPEN, first: {LIMIT}) {{ nodes {{ number title url
    reviewThreads(first: {LIMIT}) {{ nodes {{ isResolved path line
      comments(first: {LIMIT}) {{ nodes {{ author {{ login }} body url databaseId }} }} }} }} }} }} }} }}"""


def _whole(what: str, items: list) -> list:
    """`items` unless the read reached LIMIT, which may have cut it short: a partial list never passes for a whole one."""
    if len(items) >= LIMIT:
        # shortcut: refuse rather than page; page these reads when a repo or request keeps this many open
        raise RuntimeError(f"{what}: {LIMIT} or more; refusing to read a partial list as whole")
    return items


@dataclass(frozen=True)
class Thread:
    ref: str
    state: str  # needs work | awaiting you
    where: str
    said: str   # the user's latest words in it
    url: str    # the thread's latest comment


def _thread(ref: str, notes: list[tuple[str, str, str]], where: str, approver: str) -> Thread | None:
    """`notes` is (author, body, url) in order; None when the user never wrote in it."""
    mine = [n for n in notes if n[0] == approver]
    if not mine:
        return None
    state = "needs work" if notes[-1][0] == approver else "awaiting you"
    return Thread(ref, state, where, mine[-1][1].strip(), notes[-1][2])


def github_threads(repo: str, approver: str, gh=backlog.run_gh) -> list[Thread]:
    owner, name = repo.split("/", 1)
    code, out, err = gh(["api", "graphql", "-f", f"query={QUERY}", "-F", f"owner={owner}", "-F", f"name={name}"])
    if code != 0:
        raise RuntimeError(f"gh api graphql: {err.strip() or code}")
    found = []
    for pr in _whole("open pull requests", json.loads(out)["data"]["repository"]["pullRequests"]["nodes"]):
        for t in _whole(f"#{pr['number']} review threads", pr["reviewThreads"]["nodes"]):
            comments = _whole(f"#{pr['number']} thread comments", t["comments"]["nodes"])
            if t["isResolved"] or not comments:
                continue
            notes = [((c.get("author") or {}).get("login"), c["body"], c["url"]) for c in comments]
            one = _thread(f"#{pr['number']}/{comments[0]['databaseId']}", notes,
                          f"{t['path']}:{t.get('line') or '-'}", approver)
            if one:
                found.append(one)
    return found


def gitlab_threads(home: backlog.Backlog, project: str, approver: str, token: str, call=backlog._call) -> list[Thread]:
    base = home.api(project)
    listed, _, err = call("GET", f"{base}/merge_requests?" + urlencode({"state": "opened", "per_page": LIMIT}), token, backlog.TIMEOUT)
    if err:
        raise RuntimeError(f"merge requests: {err}")
    found = []
    for mr in _whole("merge requests", listed):
        iid = int(mr["iid"])
        discussions, _, err = call("GET", f"{base}/merge_requests/{iid}/discussions?per_page={LIMIT}", token, backlog.TIMEOUT)
        if err:
            raise RuntimeError(f"!{iid} discussions: {err}")
        for d in _whole(f"!{iid} discussions", discussions):
            notes = [n for n in d.get("notes") or [] if not n.get("system")]
            if not notes or not notes[0].get("resolvable") or notes[0].get("resolved"):
                continue
            pos = notes[0].get("position") or {}
            one = _thread(f"!{iid}/{d['id']}",
                          [((n.get("author") or {}).get("username"), n.get("body", ""), f"{mr['web_url']}#note_{n['id']}")
                           for n in notes],
                          f"{pos.get('new_path', '-')}:{pos.get('new_line') or '-'}", approver)
            if one:
                found.append(one)
    return found


def main(argv: list[str] | None = None, gh=backlog.run_gh, call=backlog._call) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--root", type=Path, required=True, help="workspace holding CLAUDE.md")
    ap.add_argument("--repo", help="owner/name or group/name; default the backlog's")
    ap.add_argument("--reply", metavar="REF", help="the thread to answer, as the listing prints it")
    ap.add_argument("--body", help="the reply: the fixing commit and what changed")
    args = ap.parse_args(argv)
    if bool(args.reply) != bool(args.body):
        ap.error("--reply and --body go together")
    try:
        workflow, home, github, repo = forge(args.root, args.repo)
    except (OSError, ConfigError, SettingsError) as exc:
        print(f"review-threads: {exc}", file=sys.stderr)
        return 2
    if not workflow.approver:
        print("review-threads: set [workflow] approver to the user's forge username", file=sys.stderr)
        return 2
    try:
        secret = None if github else backlog.token(home)
        if args.reply:
            number, _, tid = args.reply.lstrip("#!").partition("/")
            if github:
                code, _, err = gh(["api", f"repos/{repo}/pulls/{int(number)}/comments/{int(tid)}/replies",
                                   "-f", f"body={args.body}"])
                err = err.strip() if code else ""
            else:
                _, _, err = call("POST", f"{home.api(repo)}/merge_requests/{int(number)}/discussions/{tid}/notes",
                                 secret, backlog.TIMEOUT, {"body": args.body})
            if err:
                raise RuntimeError(f"{args.reply} not answered: {err}")
            print(f"{args.reply} answered")
            return 0
        found = github_threads(repo, workflow.approver, gh) if github else \
            gitlab_threads(home, repo, workflow.approver, secret, call)
    except (RuntimeError, ValueError, KeyError, TypeError, AttributeError) as exc:
        print(f"review-threads: {exc}", file=sys.stderr)
        return 1
    for t in sorted(found, key=lambda t: t.state != "needs work"):
        print(f"{t.ref} {t.state} · {t.where} · {json.dumps(t.said[:200])} · {t.url}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
