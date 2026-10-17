#!/usr/bin/env python3
"""One verdict per open pull or merge request, from its head pipeline and never the forge's own merge status (#419).

    chief-of-stuff merge-ready --root R [--repo owner/name]

The coordinator wrote "mergeable" from the forge's detailed merge status, which says only that there are no
conflicts, so a red pipeline and a request with open review findings were both called mergeable. This prints
one line per open request: the verdict (ready | red | running | conflicts | findings open | stale pipeline |
no pipeline | draft | unverified | blocked; `unverified` and `blocked` say why in parentheses: a pipeline sha or
thread count that was not read, threads cut off at 100, the forge's mergeStateStatus), the head sha, the head
pipeline's id, status and the sha it ran on, conflicts, and unresolved review threads. "Mergeable" is written only from a `ready` line, quoting its pipeline id and sha. It never
merges and needs no approver or merge_owner setting; merge_approved.py lists the same lines for `worker`.
GitHub reads its pull requests with `gh pr list` and one `gh api graphql` call (review threads, the head
commit and its workflow run id); GitLab reads merge requests and their discussions over REST.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import sys
from pathlib import Path
from urllib.parse import urlencode

import backlog
from forge_port import ForgeError
from forge_review import Request, forge, github_requests, gitlab_requests, scrub
from settings import SettingsError
from workspace import ConfigError

THREAD_LIMIT = 100
# Page open pull requests; threads past THREAD_LIMIT print `unverified`. A full MAX_PAGES read is refused as partial (#425).
QUERY = """query($owner: String!, $name: String!, $cursor: String) { repository(owner: $owner, name: $name) {
  pullRequests(states: OPEN, first: 100, after: $cursor) { pageInfo { hasNextPage endCursor }
    nodes { number isDraft
    reviewThreads(first: %d) { pageInfo { hasNextPage } nodes { isResolved } }
    commits(last: 1) { nodes { commit { oid statusCheckRollup { contexts(first: 100) { nodes {
      __typename ... on CheckRun { checkSuite { workflowRun { databaseId } } }
    } } } } } } }
  } } }""" % THREAD_LIMIT


def github_ready(repo: str, gh=backlog.run_gh, approver: str = "") -> list[Request]:
    owner, name = repo.split("/", 1)
    nodes: dict[int, dict] = {}
    cursor = None
    for _ in range(backlog.MAX_PAGES):
        # -f for strings; -F cursor=null types a GraphQL null for the first page
        args = ["api", "graphql", "-f", f"query={QUERY}", "-f", f"owner={owner}", "-f", f"name={name}"]
        args += ["-f", f"cursor={cursor}"] if cursor else ["-F", "cursor=null"]
        code, out, err = gh(args)
        if code != 0:
            raise ForgeError(f"gh api graphql: {err.strip() or code}", retryable=bool(getattr(err, "retryable", False)))
        answer = json.loads(out)
        if answer.get("errors"):
            raise ForgeError(f"gh api graphql: {answer['errors']}", retryable=backlog.graphql_rate_limited(answer))
        page = answer["data"]["repository"]["pullRequests"]
        for n in page["nodes"]:
            nodes[n["number"]] = n
        if not page["pageInfo"]["hasNextPage"]:
            break
        cursor = page["pageInfo"]["endCursor"]
    else:
        raise RuntimeError(f"more than {backlog.MAX_PAGES} pages of pull requests; refusing to read a partial list as whole")
    found = []
    for r in github_requests(repo, approver, gh):
        n = nodes.get(r.number)
        if n is None:
            raise RuntimeError(f"#{r.number}: not in the graphql read")
        if not n["commits"]["nodes"]:
            raise RuntimeError(f"#{r.number}: no commit in the graphql read")
        commit = n["commits"]["nodes"][0]["commit"]
        rollup = commit.get("statusCheckRollup")
        ids = [str(run["databaseId"]) for c in (rollup or {}).get("contexts", {}).get("nodes", [])
               if (run := ((c.get("checkSuite") or {}).get("workflowRun") or {}))]
        threads = n["reviewThreads"]
        found.append(dataclasses.replace(
            r, pipeline_id=ids[0] if ids else "", pipeline_sha=commit["oid"], draft=r.draft or bool(n["isDraft"]),
            threads=sum(not t["isResolved"] for t in threads["nodes"]),
            threads_cut_at=THREAD_LIMIT if threads["pageInfo"]["hasNextPage"] else 0))
    return found


def gitlab_ready(home: backlog.Backlog, project: str, token: str, call=backlog._call, approver: str = "") -> list[Request]:
    base = home.api(project)
    found = []
    for r in gitlab_requests(home, project, approver, token, call):
        threads = 0
        for page in range(1, backlog.MAX_PAGES + 1):  # until a short page; x-next-page is not relied on
            discussions, _, err = call("GET", f"{base}/merge_requests/{r.number}/discussions?"
                                       + urlencode({"per_page": backlog.PER_PAGE, "page": page}), token, backlog.TIMEOUT)
            if err:
                raise RuntimeError(f"!{r.number}: {err}")
            threads += sum(any(n.get("resolvable") and not n.get("resolved") and not n.get("system")
                               for n in d.get("notes") or []) for d in discussions)
            if len(discussions) < backlog.PER_PAGE:
                break
        else:
            raise RuntimeError(f"!{r.number}: more than {backlog.MAX_PAGES} pages of discussions")
        found.append(dataclasses.replace(r, threads=threads))
    return found


def read(home, github: bool, repo: str, gh=backlog.run_gh, call=backlog._call) -> list[Request]:
    return github_ready(repo, gh) if github else gitlab_ready(home, repo, backlog.token(home), call)


def line(r: Request, mark: str) -> str:
    sha = "unread" if r.pipeline_sha is None else scrub(r.pipeline_sha[:7]) or "-"
    pid = scrub(r.pipeline_id or "-")
    status = scrub(r.pipeline)
    pipeline = "none" if r.pipeline == "none" else f"{pid} {status} on {sha}"
    why = f" ({scrub(r.why)})" if r.why else ""
    threads = "unread" if r.threads is None else r.threads
    head = scrub(r.head[:7])
    return (f"{mark}{r.number} {r.verdict}{why} · head {head} · pipeline {pipeline} · "
            f"conflicts {'yes' if r.mergeable == 'conflict' else 'no'} · unresolved threads {threads} · {scrub(r.url)}")


def main(argv: list[str] | None = None, gh=backlog.run_gh, call=backlog._call) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--root", type=Path, required=True, help="workspace holding CLAUDE.md")
    ap.add_argument("--repo", help="owner/name or group/name; default the backlog's")
    args = ap.parse_args(argv)
    try:
        _, home, github, repo = forge(args.root, args.repo)
    except (OSError, ConfigError, SettingsError) as exc:
        print(f"merge-ready: {exc}", file=sys.stderr)
        return 2
    try:
        found = read(home, github, repo, gh, call)
    except (RuntimeError, ValueError, KeyError, TypeError, AttributeError) as exc:
        print(f"merge-ready: {exc}", file=sys.stderr)
        return 1
    for r in sorted(found, key=lambda r: r.number):
        print(line(r, "#" if github else "!"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
