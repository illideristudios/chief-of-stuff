#!/usr/bin/env python3
"""The forge and worker facts the pages draw, read in one place and cached beside the pages.

    chief-of-stuff sources --root R [--refresh]    # print what the cache holds; --refresh reads it all first

Today's tracker names its tasks' issues and the pull or merge requests their rows
mention (`!58` on GitLab, `PR #58` on GitHub, or the request's URL), and the requests merged today,
using bounded GraphQL reads and a resumable GitHub inventory. An engine workspace (one with a `store.db`) names its tasks in `work_item`
rather than tracker rows, so there the store's issues, recorded merge requests, open requests and recent
merged requests are fetched too (#896, #996). Workers are the registered sessions process_status finds running, live
one-shot workers under the Worktrees dir, and the tracker's Sessions rows. The cache is
`<pages dir>/.sources.json`. A source that cannot be read keeps its last good facts, and `errors` says why.
`change_states` is the audit's read of the changes its rows name, neither drawn nor cached.
"""

from __future__ import annotations

import argparse
import contextlib
import contextvars
import fcntl
import json
import os
import re
import sqlite3
import sys
import tempfile
import time as monotonic_time
from dataclasses import asdict, dataclass, replace
from datetime import date, datetime, time, timedelta
from pathlib import Path
from urllib.parse import quote, urlsplit

import backlog
import process_status
from _vendor.toon_format import encode as toon_encode
from backlog_ref import GH_REPO, ambiguous_gitlab_label, store_label
from clock import leading
from forge_review import (
    GITLAB_PIPELINE,
    closes as _closes,
    github_approval as _github_approval,
    github_closes as _github_closes,
    github_pipeline as _github_pipeline,
)
from md import section as _section
from settings import SettingsError, load as load_settings
from tracker import Tracker, bare_name as _bare_name, launcher, parse_tracker
from tracker_log import STARTED_DETAIL
from workspace import Config, ConfigError, read_config, worktrees_dir

CACHE = ".sources.json"
_SCAN_NOW = contextvars.ContextVar("github_scan_now", default=None)
GH_PR = re.compile(r"(?i)\bPR\s*#(\d+)\b")
GL_MR = re.compile(r"(?<![\w&])!(\d+)\b")
GL_STATE = {"opened": "open", "locked": "closed"}
GL_WAITING = {"pending", "created", "waiting_for_resource", "preparing", "scheduled", "manual"}
_GITLAB_PROJECT = re.compile(r"^[\w.-]+(?:/[\w.-]+)+$")
_BAD_BRANCH_CHARS = frozenset("~^:?*[\\")

GH_ISSUE = ("number url title state body labels(first: 50) { nodes { name } } closedAt "
            "milestone { title url dueOn state issues(states: OPEN) { totalCount } }")
# A review thread is its first comment, as review_threads.QUERY reads them; one comment keeps the node count low.
GH_CHANGE = ("number url title state isDraft mergedAt baseRefName body headRefOid reviewDecision mergeable "
             "headRefName headRef { name } headRepository { url } "
             "closingIssuesReferences(first: 25) { nodes { number repository { name owner { login } } } } "
             "files(first: 100) { nodes { path additions deletions } } "
             "latestReviews(first: 50) { nodes { author { login } state submittedAt commit { oid } } } "
             "reviewThreads(first: 100) { nodes { isResolved comments(first: 1) { nodes { author { login } body createdAt url } } } } "
             "commits(last: 1) { nodes { commit { statusCheckRollup { contexts(first: 100) { nodes { __typename "
             "... on CheckRun { name status conclusion startedAt completedAt } ... on StatusContext { state } } } } } } }")
# Five rich PRs cost 5 points in a live public-repository probe. Bound both
# request size and total work; the ten-minute automatic cadence makes even the
# 200-point stop target about 1,200 points/hour per workspace (plus at most
# the last request's cost). The response supplies the cost after each request.
GH_PAGE = 5
GH_MAX_REQUESTS = 40
GH_POINT_BUDGET = 200
GH_RESERVE = 500
GH_INTERVAL = timedelta(minutes=10)
GL_PAGE = 100  # the most nodes GitLab answers for one connection
GL_ISSUE = ("iid webUrl title state description labels { nodes { title } } closedAt "
            "milestone { title webPath dueDate state stats { totalIssuesCount closedIssuesCount } }")
GL_CHANGE = ("iid webUrl title state draft mergedAt targetBranch description approved conflicts "
             "approvedBy { nodes { username } } headPipeline { status } diffStats { path additions deletions } diffHeadSha "
             "sourceBranch sourceBranchExists sourceProject { webUrl }")
# Jobs and discussions only for open changes, in a second query: in GL_CHANGE they took it past GitLab's complexity cap of 250.
GL_DETAIL = ("iid headPipeline { jobs { nodes { name status duration } } } "
             "discussions { nodes { resolvable resolved notes(first: 1) { nodes { author { username } body createdAt url } } } }")


@dataclass(frozen=True)
class FileStat:
    path: str
    additions: int
    deletions: int


@dataclass(frozen=True)
class Job:            # one job of the head pipeline (GitLab) or one check run (GitHub)
    name: str
    status: str       # as Change.pipeline
    seconds: int | None


@dataclass(frozen=True)
class ReviewThread:   # its first comment
    resolved: bool
    author: str
    body: str
    at: datetime | None
    url: str


@dataclass(frozen=True)
class Change:          # a PR or MR
    ref: str           # "!58" (GitLab) or "#58" (GitHub PR)
    url: str
    title: str
    state: str         # "open" | "merged" | "closed"
    draft: bool
    pipeline: str | None   # "passed" | "failed" | "running" | "pending" | None
    approved: bool
    approvals: int
    merged_at: datetime | None
    base: str
    files: tuple[str, ...]
    issues: tuple[str, ...]   # issue refs it closes, e.g. ("#111",)
    head: str = ""            # the head commit's sha, which the decision page graphs
    stats: tuple[FileStat, ...] = ()
    jobs: tuple[Job, ...] = ()
    conflicts: bool = False
    threads: tuple[ReviewThread, ...] = ()
    branch: str = ""          # source branch name when known
    branch_url: str = ""      # verified forge tree URL only while the branch and source repo exist


@dataclass(frozen=True)
class ChangeState:     # what the audit asks of a PR or MR (#45)
    ref: str
    state: str         # as Change.state
    base: str
    merge_sha: str     # the merge commit's sha; empty unless the change merged


@dataclass(frozen=True)
class Milestone:       # an epic (#230)
    title: str
    url: str           # its identity: two repos may each have a "v2"
    due: date | None
    state: str         # "open" | "closed"
    open_issues: int


@dataclass(frozen=True)
class Issue:
    ref: str
    url: str
    title: str
    state: str
    labels: tuple[str, ...]
    body: str = ""
    closed_at: datetime | None = None  # (#220)
    milestone: Milestone | None = None


@dataclass(frozen=True)
class Worker:
    name: str
    kind: str          # "one-shot" | "session"
    runtime: str
    model: str
    task: str
    tree: str
    started: datetime | None
    last: datetime | None


@dataclass(frozen=True)
class Sources:
    fetched: dict[str, datetime]   # keys: "kanban", "merge requests", "workers"
    issues: dict[str, Issue]       # by ref
    changes: dict[str, Change]     # by ref
    workers: tuple[Worker, ...]
    errors: dict[str, str]         # source -> why it could not be read
    home: backlog.Backlog | backlog.GitHubBacklog | None = None  # the Backlog the issue keys are relative to


EMPTY = Sources({}, {}, {}, (), {})


def for_home(sources: Sources, home: backlog.Backlog | backlog.GitHubBacklog | None) -> Sources:
    """View cached forge facts only when their home is the currently configured forge identity.

    Keep timestamps, errors and workers so stale-source warnings remain visible. Never rewrite the cache:
    returning to its old home after a failed refresh can still use its last good facts.
    """
    same = (sources.home is not None and home is not None and type(sources.home) is type(home)
            and backlog.home_of(sources.home) == backlog.home_of(home))
    return replace(sources, home=home, issues=sources.issues if same else {}, changes=sources.changes if same else {})


def _when(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def _due(value: str | None) -> date | None:
    """The date as written: GitHub's dueOn is a midnight-ish UTC time that the workspace zone would move a day back."""
    return date.fromisoformat(value[:10]) if value else None


def _milestone_of(v: dict | None) -> Milestone | None:
    """A cached milestone; a cache from an older release has none."""
    return Milestone(**{**v, "due": _due(v["due"])}) if v else None


def _change_of(v: dict, home=None) -> Change:
    """Read old GitLab closing refs in the MR's forge context, never as GitHub shorthand."""
    issues = tuple(v["issues"])
    mr = re.fullmatch(r"https://([^/]+)/((?:[\w.-]+/)+[\w.-]+)/-/merge_requests/\d+/?", v.get("url", ""))
    if mr or v.get("ref", "").startswith("!"):
        host = mr.group(1) if mr else ""
        migrated = []
        for ref in issues:
            path, marker, number = ref.rpartition("#")
            if marker and number.isdecimal() and (not path or GH_REPO.fullmatch(path)):
                if not host:
                    continue
                ref = backlog.IssueRef(path or mr.group(2), int(number), host).label(home)
            elif marker and number.isdecimal() and ambiguous_gitlab_label(ref):
                continue  # old nested project and host-qualified labels cannot be distinguished
            migrated.append(ref)
        issues = tuple(migrated)
    return Change(**{**v, "merged_at": _when(v["merged_at"]), "files": tuple(v["files"]), "issues": issues,
                     "stats": tuple(FileStat(**s) for s in v.get("stats") or ()),
                     "jobs": tuple(Job(**j) for j in v.get("jobs") or ()),
                     "threads": tuple(ReviewThread(**{**t, "at": _when(t["at"])}) for t in v.get("threads") or ())})


def _home_of(d: dict | None):
    return None if not d else backlog.Backlog(**d) if "host" in d else backlog.GitHubBacklog(**d)


def _cached_issues(values: dict, home) -> dict[str, Issue]:
    """Rekey legacy entries only when their URL proves their forge and issue identity."""
    result: dict[str, Issue] = {}
    collisions: set[str] = set()
    for key, v in values.items():
        issue = Issue(**{**v, "labels": tuple(v["labels"]), "closed_at": _when(v.get("closed_at")),
                         "milestone": _milestone_of(v.get("milestone"))})
        ref = backlog.issue_ref(issue.url, home, any_host=True)
        if ref:
            canonical = ref.label(home)
            legacy = {canonical, f"{ref.repo}#{ref.number}", f"{ref.host}/{ref.repo}#{ref.number}"}
            if home is None:
                legacy.add(f"#{ref.number}")
            if key not in legacy:
                continue
            key = canonical
            issue = replace(issue, ref=key)
        elif home is None and re.fullmatch(r"#\d+", key):
            continue  # no home or URL proves what a bare key names
        elif (home is None or isinstance(home, backlog.Backlog)) and (
                GH_REPO.fullmatch(key.rpartition("#")[0]) or ambiguous_gitlab_label(key)):
            continue  # an unverified old GitLab key could otherwise masquerade as GitHub
        if key in collisions:
            continue
        if key in result and result[key] != issue:
            del result[key]
            collisions.add(key)
        else:
            result[key] = issue
    return result


def load(pages_dir: Path) -> Sources:
    """The cache, or EMPTY when it is absent or unreadable."""
    try:
        d = json.loads((pages_dir / CACHE).read_text())
        home = _home_of(d.get("home"))
        return Sources({k: datetime.fromisoformat(v) for k, v in d["fetched"].items()},
                       _cached_issues(d["issues"], home),
                       {k: _change_of(v, home) for k, v in d["changes"].items()},
                       tuple(Worker(**{**w, "started": _when(w["started"]), "last": _when(w["last"])})
                             for w in d["workers"]),
                       dict(d["errors"]), home)
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return EMPTY


def pages_dir(root: Path, cfg: Config, day: str) -> Path:
    """Where the board is written: the Board line's dir, else beside the tracker (as render_board does)."""
    return root / cfg.pages_dir if cfg.pages_dir else (root / cfg.tracker_path(day)).parent


def _graphql_errors(body) -> str:
    return "; ".join(str(e.get("message", e)) for e in body.get("errors") or [] if isinstance(e, dict))


def _stats(files: list[dict]) -> tuple[FileStat, ...]:
    return tuple(FileStat(f["path"], int(f.get("additions") or 0), int(f.get("deletions") or 0))
                 for f in files if "additions" in f)


def _valid_branch(branch: str) -> bool:
    """Git ref-format rules for a branch name that can safely sit in a forge `/tree/<branch>` URL."""
    if not branch or branch != branch.strip() or branch.startswith("/") or branch.endswith(("/", ".")):
        return False
    if ".." in branch or "//" in branch or "@{" in branch:
        return False
    if any(not c.isprintable() or c.isspace() or c in _BAD_BRANCH_CHARS for c in branch):
        return False
    return all(part and not part.startswith(".") and not part.endswith(".lock") for part in branch.split("/"))


def _branch_url(repo_url: str | None, branch: str, *, gitlab: bool = False) -> str:
    clean_repo = (repo_url or "").strip().rstrip("/")
    if not clean_repo or any(ord(c) <= 32 or ord(c) == 127 for c in clean_repo) or not _valid_branch(branch):
        return ""
    parts = urlsplit(clean_repo)
    if parts.scheme.lower() not in ("http", "https") or not parts.netloc:
        return ""
    tree = "-/tree" if gitlab else "tree"
    return f"{clean_repo}/{tree}/{quote(branch, safe='/')}"


# --- GitHub ----------------------------------------------------------------------------------------

def _gh_change(key: str, p: dict, approver: str, repo: str = "", origin: str = "") -> Change:
    reviews = (p.get("latestReviews") or {}).get("nodes") or []
    commit = (((p.get("commits") or {}).get("nodes") or [{}])[0] or {}).get("commit") or {}
    checks = ((commit.get("statusCheckRollup") or {}).get("contexts") or {}).get("nodes") or []
    pipeline = _github_pipeline(checks)
    runs = [c for c in checks if c.get("__typename") == "CheckRun"]
    jobs = tuple(Job(c.get("name") or "", _github_pipeline([c]),
                     int((_when(c["completedAt"]) - _when(c["startedAt"])).total_seconds())
                     if c.get("startedAt") and c.get("completedAt") else None) for c in runs)
    threads = tuple(ReviewThread(bool(t.get("isResolved")), (c.get("author") or {}).get("login") or "", c.get("body") or "",
                                 _when(c.get("createdAt")), c.get("url") or "")
                    for t in (p.get("reviewThreads") or {}).get("nodes") or []
                    for c in ((t.get("comments") or {}).get("nodes") or [])[:1])
    files = (p.get("files") or {}).get("nodes") or []
    approved = (_github_approval(reviews, approver, p.get("headRefOid", "")) == "approved" if approver
                else p.get("reviewDecision") == "APPROVED")
    closing = p.get("closingIssuesReferences")
    closing_nodes = (closing.get("nodes") if isinstance(closing, dict) else closing) or []
    issues = _github_closes({**p, "closingIssuesReferences": [x for x in closing_nodes if isinstance(x, dict)]}, origin or repo)
    if origin and origin.casefold() != repo.casefold():
        issues = tuple(dict.fromkeys(f"{origin}{ref}" if ref.startswith("#") else
                                     f"#{ref.rpartition('#')[2]}" if ref.rpartition("#")[0].casefold() == repo.casefold() else ref
                                     for ref in issues))
    head_ref = p.get("headRef") if isinstance(p.get("headRef"), dict) else None
    branch = (head_ref or {}).get("name") or p.get("headRefName") or ""
    head_repo = p.get("headRepository") if isinstance(p.get("headRepository"), dict) else None
    branch_url = _branch_url((head_repo or {}).get("url") if head_ref else "", branch)
    return Change(key, p["url"], p["title"], p["state"].lower(), bool(p.get("isDraft")),
                  None if pipeline == "none" else pipeline, approved,
                  sum(r.get("state") == "APPROVED" for r in reviews), _when(p.get("mergedAt")),
                  p.get("baseRefName") or "", tuple(f["path"] for f in files), issues,
                  p.get("headRefOid") or "", _stats(files), jobs, p.get("mergeable") == "CONFLICTING", threads,
                  branch, branch_url)


# --- GitLab ----------------------------------------------------------------------------------------

def _gl_pipeline(status: str | None) -> str | None:
    if not status:
        return None
    s = status.lower()
    return GITLAB_PIPELINE.get(s) or ("pending" if s in GL_WAITING else "running")


def _gl_detail(m: dict) -> tuple[tuple[Job, ...], tuple[ReviewThread, ...]]:
    pipe = m.get("headPipeline") or {}
    jobs = tuple(Job(j.get("name") or "", _gl_pipeline(j.get("status")) or "pending",
                     None if j.get("duration") is None else int(j["duration"]))
                 for j in (pipe.get("jobs") or {}).get("nodes") or [])
    threads = tuple(ReviewThread(bool(d.get("resolved")), (n.get("author") or {}).get("username") or "", n.get("body") or "",
                                 _when(n.get("createdAt")), n.get("url") or "")
                    for d in (m.get("discussions") or {}).get("nodes") or [] if d.get("resolvable")
                    for n in ((d.get("notes") or {}).get("nodes") or [])[:1])
    return jobs, threads


def _gl_change(m: dict, approver: str, home: backlog.Backlog | None = None) -> Change:
    by = {(u or {}).get("username") for u in (m.get("approvedBy") or {}).get("nodes") or []}
    state = GL_STATE.get(m["state"], m["state"])
    pipe = m.get("headPipeline") or {}
    issues = _closes(m.get("description"), gitlab=True, project=home.project if home else "", host=home.host if home else "")
    if home:
        host = backlog.home_of(home)[0]
        issues = tuple(dict.fromkeys(
            backlog.IssueRef(path, int(number), host).label(home)
            if path and "://" not in path and number.isdecimal() else ref
            for ref in issues if "://" not in ref for path, _, number in (ref.rpartition("#"),)
        ))
    branch = m.get("sourceBranch") or ""
    src_project = m.get("sourceProject") if isinstance(m.get("sourceProject"), dict) else None
    branch_url = _branch_url((src_project or {}).get("webUrl") if m.get("sourceBranchExists") else "", branch, gitlab=True)
    return Change(f"!{m['iid']}", m["webUrl"], m["title"], state, bool(m.get("draft")), _gl_pipeline(pipe.get("status")),
                  approver in by if approver else bool(m.get("approved")), len(by), _when(m.get("mergedAt")),
                  m.get("targetBranch") or "", tuple(d["path"] for d in m.get("diffStats") or []),
                  issues, m.get("diffHeadSha") or "", _stats(m.get("diffStats") or []), (),
                  bool(m.get("conflicts")), (), branch, branch_url)


def gitlab(home: backlog.Backlog, wanted: dict[str, set[int]], mrs: set[int], since: datetime, approver: str, call,
           every_open: bool = False):
    """(issues, changes, errors) from the host's /api/graphql, with the Backlog's token: one POST per connection (a project's issues,
    the named merge requests, the merged page, and with `every_open` the open and recent-merged pages), each under GitLab's complexity
    cap, then one for open changes' jobs and threads.
    A connection answers at most GL_PAGE nodes, so past that it goes again for the next GL_PAGE iids or the next merged page.
    `errors` maps a panel ("kanban": the issues; "merge requests": the rest) to its first error. A panel whose issues or changes
    failed is None, so the board keeps its last good cards; a failed jobs-and-threads request keeps the changes without them.
    A transport error stops every request after it; a GraphQL error does not."""
    secret = backlog.token(home)
    if not secret:
        return None, None, {"kanban": home.missing_token, "merge requests": home.missing_token}
    host, errors, lost, down = backlog.home_of(home)[0], {}, set(), []
    issues, changes = {}, {}

    def ask(project: str, fields: str, panel: str, lose: bool = True) -> dict | None:
        if down:
            body, err = None, down[0]
        else:
            body, _, err = call("POST", f"{home.host.rstrip('/')}/api/graphql", secret, backlog.TIMEOUT,
                                {"query": f"query {{ p0: project(fullPath: {json.dumps(project)}) {{ {fields} }} }}"})
            down.extend([err] if err else [])
        if err or not isinstance(body, dict) or not isinstance(body.get("data"), dict):
            errors.setdefault(panel, err or _graphql_errors(body if isinstance(body, dict) else {}) or "GitLab did not answer")
            if lose:
                lost.add(panel)
            return None
        if message := _graphql_errors(body):
            errors.setdefault(panel, message)
        return body["data"].get("p0") or {}

    def pages(project: str, name: str, iids: list[int], body: str, panel: str, lose: bool = True):
        for start in range(0, len(iids), GL_PAGE):
            chunk = iids[start:start + GL_PAGE]
            got = ask(project, f"{name}(iids: {json.dumps([str(n) for n in chunk])}, first: {len(chunk)}) {{ nodes {{ {body} }} }}", panel, lose)
            if got is None:
                return
            yield from (got.get(name) or {}).get("nodes") or []

    for project in sorted(set(wanted) | {home.project}):
        for node in pages(project, "issues", sorted(wanted.get(project) or ()), GL_ISSUE, "kanban"):
            key = backlog.IssueRef(project, int(node["iid"]), host).label(home)
            m, stats = node.get("milestone"), (node.get("milestone") or {}).get("stats") or {}
            issues[key] = Issue(key, node["webUrl"], node["title"], {"opened": "open"}.get(node["state"], node["state"]),
                                tuple(x["title"] for x in (node.get("labels") or {}).get("nodes") or []),
                                node.get("description") or "", _when(node.get("closedAt")),
                                Milestone(m["title"], f"{home.host.rstrip('/')}{m['webPath']}", _due(m.get("dueDate")),
                                          {"active": "open"}.get(m["state"], m["state"]),
                                          int(stats.get("totalIssuesCount") or 0) - int(stats.get("closedIssuesCount") or 0))
                                if m else None)
    for node in pages(home.project, "mergeRequests", sorted(mrs), GL_CHANGE, "merge requests"):
        changes.setdefault(f"!{node['iid']}", _gl_change(node, approver, home))
    listed = {"merged": f"state: merged, mergedAfter: {json.dumps(since.isoformat())}", **({"open": "state: opened"} if every_open else {})}
    for alias, args in listed.items():
        cursor = ""  # the page to ask for next, None once the last one is in
        while cursor is not None:
            after = f", after: {json.dumps(cursor)}" if cursor else ""
            got = ask(home.project, f"{alias}: mergeRequests({args}{after}) "
                                    f"{{ nodes {{ {GL_CHANGE} }} pageInfo {{ hasNextPage endCursor }} }}", "merge requests")
            if got is None:
                break
            for node in (got.get(alias) or {}).get("nodes") or []:
                changes.setdefault(f"!{node['iid']}", _gl_change(node, approver, home))  # a named change wins over the same one here
            info = (got.get(alias) or {}).get("pageInfo") or {}
            cursor = info.get("endCursor") if info.get("hasNextPage") else None
    if every_open:
        got = ask(home.project, f"recent: mergeRequests(state: merged, first: {GL_PAGE}) {{ nodes {{ {GL_CHANGE} }} }}",
                  "merge requests")
        for node in ((got or {}).get("recent") or {}).get("nodes") or []:
            changes.setdefault(f"!{node['iid']}", _gl_change(node, approver, home))
    for node in pages(home.project, "mergeRequests", sorted(int(k[1:]) for k, c in changes.items() if c.state == "open"),
                      GL_DETAIL, "merge requests", lose=False):
        if (key := f"!{node['iid']}") in changes:
            jobs, threads = _gl_detail(node)
            changes[key] = replace(changes[key], jobs=jobs, threads=threads)
    return (None if "kanban" in lost else issues), (None if "merge requests" in lost else changes), errors


def change_ref(home, number) -> str:
    """How a change is keyed: `#58` for a GitHub pull request, `!58` for a GitLab merge request."""
    return f"{'#' if isinstance(home, backlog.GitHubBacklog) else '!'}{number}"


def spelled(ref: str) -> str:
    """A change's ref as a row spells it: `PR #58` on GitHub, where a bare `#58` is an issue."""
    return f"PR {ref}" if ref.startswith("#") else ref


def change_numbers(task, home, then: str = "") -> set[int]:
    """The pull or merge requests a task's row names: `!N` on GitLab, `PR #N` on GitHub, or the request's URL.
    With `then`, only those a cell follows with it. Each cell is read alone, so nothing is read across two."""
    github_home = isinstance(home, backlog.GitHubBacklog)
    url = (rf"github\.com/{re.escape(home.repo)}/pull/(\d+)" if github_home
           else rf"{re.escape(backlog.home_of(home)[0])}/{re.escape(home.project)}/-/merge_requests/(\d+)")
    return {int(n) for cell in (task.name, task.item, task.issue, task.checklist)
            for named in ((GH_PR if github_home else GL_MR).pattern, url) for n in re.findall(named + then, cell)}


def change_states(home, numbers, gh, call) -> tuple[dict[str, ChangeState], str]:
    """({ref: ChangeState}, error) for the changes numbered, from one GraphQL call that asks for nothing else.
    A number the forge does not have is absent from the dict, and is no error."""
    # ponytail: one request names every change. Past about GL_PAGE of them GitLab refuses the query as too complex,
    # which the audit prints as `merged: unknown`; page as gitlab() does once live rows name that many.
    states: dict[str, ChangeState] = {}
    if isinstance(home, backlog.GitHubBacklog):
        owner, name = home.repo.split("/", 1)
        pulls = " ".join(f"n{n}: pullRequest(number: {n}) {{ number state baseRefName mergeCommit {{ oid }} }}"
                         for n in sorted(numbers))
        code, out, err = gh(["api", "graphql", "-f", f"query=query {{ repository(owner: {json.dumps(owner)}, "
                             f"name: {json.dumps(name)}) {{ {pulls} }} }}"])
        try:
            body = json.loads(out)
            nodes = body["data"]["repository"] or {}
        except (ValueError, KeyError, TypeError):
            return {}, err.strip() or f"gh exited {code}"
        for p in filter(None, nodes.values()):
            ref = change_ref(home, p["number"])
            states[ref] = ChangeState(ref, p["state"].lower(), p.get("baseRefName") or "", (p.get("mergeCommit") or {}).get("oid") or "")
        # A number that is not a pull request is a null node, a NOT_FOUND at its alias, and a failing `gh`.
        return states, _graphql_errors({"errors": [e for e in body.get("errors") or [] if not (
            isinstance(e, dict) and e.get("type") == "NOT_FOUND" and len(e.get("path") or ()) == 2)]})
    secret = backlog.token(home)
    if not secret:
        return {}, home.missing_token
    query = (f"query {{ project(fullPath: {json.dumps(home.project)}) {{ mergeRequests(iids: "
             f"{json.dumps([str(n) for n in sorted(numbers)])}) {{ nodes {{ iid state targetBranch mergeCommitSha }} }} }} }}")
    body, _, err = call("POST", f"{home.host.rstrip('/')}/api/graphql", secret, backlog.TIMEOUT, {"query": query})
    body = body if isinstance(body, dict) else {}
    project = (body.get("data") or {}).get("project")
    if err or not project:  # a project the token cannot see answers null
        return {}, err or _graphql_errors(body) or f"GitLab did not answer for {home.project}"
    for m in (project.get("mergeRequests") or {}).get("nodes") or []:
        # A fast-forward merge has no merge commit, and the head it merged from may never have reached the base.
        ref = change_ref(home, m["iid"])
        states[ref] = ChangeState(ref, GL_STATE.get(m["state"], m["state"]), m.get("targetBranch") or "", m.get("mergeCommitSha") or "")
    return states, _graphql_errors(body)


def _request_ids(raw: object) -> set[int]:
    if not isinstance(raw, (str, bytes)):
        return set()
    try:
        data = json.loads(raw)
    except ValueError:
        return set()
    if not isinstance(data, dict):
        return set()
    entries = [*(data.get("merges") if isinstance(data.get("merges"), list) else ()), data.get("merge_pending")]
    return {req for entry in entries if isinstance(entry, dict)
            and isinstance(req := entry.get("request"), int) and not isinstance(req, bool) and req > 0}


def _store_targets(db_path: Path, home: backlog.Backlog | backlog.GitHubBacklog) -> tuple[dict[str, set[int]], set[int]]:
    """Issues and home-repo change numbers recorded in `store.db` for `home`'s forge (read-only)."""
    wanted: dict[str, set[int]] = {}
    changes: set[int] = set()
    github_home = isinstance(home, backlog.GitHubBacklog)
    own_host = backlog.home_of(home)[0]
    try:
        with contextlib.closing(sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True)) as conn:
            item_rows = conn.execute("SELECT issue_key FROM work_item").fetchall()
            resume_rows = conn.execute("SELECT issue_key, data FROM resume").fetchall()
    except (sqlite3.Error, OSError, ValueError) as exc:
        raise ValueError("cannot read the workspace store") from exc
    for (raw_key,) in item_rows:
        if not isinstance(raw_key, str):
            continue
        namespace, sep, num = raw_key.rpartition("#")
        if not (sep and num.isdecimal()):
            continue
        if github_home and namespace.startswith("github:"):
            repo = namespace[len("github:"):]
            if GH_REPO.match(repo):
                wanted.setdefault(repo, set()).add(int(num))
        elif not github_home and namespace.startswith("gitlab:"):
            host, slash, project = namespace[len("gitlab:"):].partition("/")
            if slash and host == own_host and _GITLAB_PROJECT.match(project):
                wanted.setdefault(project, set()).add(int(num))
    for raw_key, raw_data in resume_rows:
        if isinstance(raw_key, str) and store_label(raw_key, home).startswith("#"):
            changes |= _request_ids(raw_data)
    return wanted, changes


def forge(root: Path, cfg: Config, tracker: Tracker, since: datetime, gh, call):
    """What the tracker's tasks and `store.db` name, from the Backlog's forge: (issues, changes, errors), each side None
    when it failed, `errors` mapping a panel ("kanban", "merge requests") to its error."""
    home = cfg.backlog
    if home is None:
        return {}, {}, {}
    try:
        approver = load_settings(root, cfg.settings_path).workflow.approver
    except SettingsError:
        approver = ""
    wanted: dict[str, set[int]] = {}
    changes: set[int] = set()
    github_home = isinstance(home, backlog.GitHubBacklog)
    store_db = root / "store.db"
    every_open = store_db.is_file()  # the store board's cards are store items, which no tracker row names
    for task in tracker.tasks:
        if task.standing:
            continue
        ref = backlog.issue_ref(task.issue, home) if task.issue.strip() else None
        if ref and (ref.host == backlog.GITHUB) == github_home:
            wanted.setdefault(ref.repo, set()).add(ref.number)
        changes |= change_numbers(task, home)
        if github_home:
            for cell in (task.name, task.item, task.issue, task.checklist):
                for repo, number in re.findall(r"https://github\.com/([\w.-]+/[\w.-]+)/pull/(\d+)/?(?=$|[\s)>.,;])", cell):
                    wanted.setdefault(repo, set()).add(int(number))
    if every_open:
        try:
            store_wanted, store_changes = _store_targets(store_db, home)
        except ValueError:
            reason = "cannot read the workspace store"
            return None, None, {"kanban": reason, "merge requests": reason}
        for repo, nums in store_wanted.items():
            wanted.setdefault(repo, set()).update(nums)
        changes |= store_changes
    if github_home:
        if changes:
            wanted.setdefault(home.repo, set()).update(changes)
        import github_scan
        pages = pages_dir(root, cfg, since.date().isoformat())
        admission = github_scan.Admission(gh, GH_MAX_REQUESTS, GH_POINT_BUDGET, GH_RESERVE)
        issues, changes, errors, _ = github_scan.scan(
            pages / github_scan.CHECKPOINT, home, wanted, since, approver, every_open, admission,
            _SCAN_NOW.get() or datetime.now().astimezone())
        return issues, changes, errors
    return gitlab(home, wanted, changes, since, approver, call, every_open)


# --- workers ---------------------------------------------------------------------------------------

def _owned_task(name: str, tasks: tuple) -> str:
    """(#199) The open task row `name`'s session owns: an open (not `done`) row whose owner cell is `name`'s
    bare name (`_bare_name`, as `Task.standing_for` matches owners). Several open rows owned: the last one,
    since later rows win elsewhere (`tracker.launcher`'s own tie-break). None owned: empty."""
    bare = _bare_name(name)
    owned = [t for t in tasks if bare and t.kind != "done" and _bare_name(t.owner) == bare]
    return owned[-1].label if owned else ""


def workers(root: Path, cfg: Config, tracker: Tracker, tracker_text: str, day: date) -> tuple[Worker, ...]:
    zone = cfg.zone
    rows = {_bare_name(s.name): s for s in tracker.sessions}
    found = []
    records = process_status.registrations(root)
    for row in process_status.check(root, []):
        if row["status"] != "running":
            continue
        record = records[row["pid"]]
        s = rows.pop(_bare_name(record["name"]), None)
        found.append(Worker(record["name"], "session", record["runtime"], "", _owned_task(record["name"], tracker.tasks),
                            record["worktree"], _when(record.get("started")), leading(s.last_reply, day, zone) if s else None))
    found += [Worker(s.label, "session", "", "", _owned_task(s.label, tracker.tasks), "", None,
                     leading(s.last_reply, day, zone)) for s in rows.values()]
    started = {m[5]: m for m in (STARTED_DETAIL.match(line) for line in _section(tracker_text, "## Log")) if m}
    trees, row_of = root / worktrees_dir((root / "CLAUDE.md").read_text()), launcher(tracker.tasks)
    for tree, task in process_status.running_trees(trees).items():
        m = started.get(task)
        runtime, model, *_ = [*(m[3] if m else "").split(" "), "", ""]
        model = "" if model.startswith("effort=") else model
        found.append(Worker(m[2] if m else tree.name, "one-shot", runtime, model, row_of(task)[1], tree.name,
                            leading(m[1], day, zone) if m else None, None))
    return tuple(found)


# --- refresh ---------------------------------------------------------------------------------------

def _write(path: Path, sources: Sources, started: datetime | None = None, generation: int | None = None) -> None:
    """Atomically: the page server reads this file while the refresher writes it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    name = None
    try:
        with tempfile.NamedTemporaryFile("w", dir=path.parent, prefix=".sources-", delete=False) as out:
            name = out.name
            data = asdict(sources)
            if started is not None:
                data["refresh_started"] = started.isoformat()
                data["refresh_generation"] = generation
            out.write(json.dumps(data, default=lambda v: v.isoformat(), indent=1))
            out.flush()
            os.fsync(out.fileno())
        os.replace(name, path)
        name = None
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if name:
            os.unlink(name)


def refresh(root: Path, now: datetime, gh=backlog.run_gh, call=backlog._call, *, scheduled: bool = False) -> Sources:
    """Fetch everything, write the cache, return it. Never raises: a failed source is in `errors`."""
    generation = monotonic_time.monotonic_ns()
    try:
        cfg = read_config(root)
    except (OSError, ConfigError) as e:
        return replace(EMPTY, errors={"config": str(e)})
    local = now.astimezone(cfg.zone)
    day = local.date()
    pages = pages_dir(root, cfg, day.isoformat())
    try:
        pages.mkdir(parents=True, exist_ok=True)
        with (pages / ".sources.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                raw = json.loads((pages / CACHE).read_text())
                started = _when(raw.get("refresh_started"))
                previous_generation = raw.get("refresh_generation")
            except (OSError, ValueError, TypeError, AttributeError):
                started = None
                previous_generation = None
            try:
                if started is not None and (started > now or (started == now and isinstance(previous_generation, int)
                                                             and previous_generation > generation)):
                    return load(pages)
            except TypeError:  # a legacy or damaged timestamp has no comparable timezone
                pass
            return _refresh_locked(root, now, cfg, day, pages, gh, call, scheduled, generation)
    except OSError as e:
        previous = load(pages)
        return replace(previous, errors={**previous.errors, "cache": str(e)})


def _refresh_locked(root, now, cfg, day, pages, gh, call, scheduled, generation):
    prev = load(pages)
    fetched = dict(prev.fetched)
    try:
        text = (root / cfg.tracker_path(day.isoformat())).read_text()
    except OSError:
        text = ""
    tracker = parse_tracker(text)
    home = cfg.backlog
    attempt_key = f"github attempt {home.repo.casefold()}" if isinstance(home, backlog.GitHubBacklog) else ""
    attempt = fetched.get(attempt_key)
    skip = scheduled and bool(attempt_key) and attempt is not None and timedelta(0) <= now - attempt < GH_INTERVAL
    if skip:
        issues, changes = None, None
        errors = {k: v for k, v in prev.errors.items() if k in ("kanban", "merge requests")}
    else:
        if scheduled and isinstance(home, backlog.GitHubBacklog):
            fetched[attempt_key] = now
        try:
            token = _SCAN_NOW.set(now)
            try:
                issues, changes, errors = forge(root, cfg, tracker, datetime.combine(day, time(0), cfg.zone), gh, call)
            finally:
                _SCAN_NOW.reset(token)
        except Exception as e:  # a forge answer shaped unlike its schema; the page still renders
            issues, changes, errors = None, None, dict.fromkeys(("kanban", "merge requests"), f"{type(e).__name__}: {e}")
    # Both failed panels may be carried together under their old home. When only one
    # succeeded under a different home, the other panel cannot safely share its keys.
    same_home = (prev.home is None and home is None) or (
        prev.home is not None and home is not None and type(prev.home) is type(home)
        and backlog.home_of(prev.home) == backlog.home_of(home))
    changed_home = not same_home and (issues is not None or changes is not None)
    if changed_home:
        fetched.pop("github sweep", None)
    if issues is None:
        if changed_home:
            issues = {}
            fetched.pop("kanban", None)
        else:
            issues = prev.issues
            if changes is None:
                home = prev.home
    elif cfg.backlog is not None:
        fetched["kanban"] = now
        if isinstance(cfg.backlog, backlog.GitHubBacklog):
            try:
                import github_scan
                checkpoint = json.loads((pages / github_scan.CHECKPOINT).read_text())
                observed = _when(checkpoint.get("named_observed"))
                if observed:
                    fetched["kanban"] = observed
            except (OSError, ValueError, TypeError, AttributeError):
                pass
    if changes is None:
        changes = {} if changed_home else prev.changes
        if changed_home:
            fetched.pop("merge requests", None)
    elif cfg.backlog is not None:
        fetched["merge requests"] = now
        if isinstance(cfg.backlog, backlog.GitHubBacklog):
            try:
                import github_scan
                checkpoint = json.loads((pages / github_scan.CHECKPOINT).read_text())
                observed = _when(checkpoint.get("observed"))
                if observed:
                    fetched["merge requests"] = observed
                    fetched["github sweep"] = observed
            except (OSError, ValueError, TypeError, AttributeError):
                pass
    try:
        found = workers(root, cfg, tracker, text, day)
        fetched["workers"] = now
    except Exception as e:  # a registry or ps that cannot be read
        found, errors["workers"] = prev.workers, f"{type(e).__name__}: {e}"
    got = Sources(fetched, issues, changes, found, errors, home)
    try:
        _write(pages / CACHE, got, now, generation)
        if isinstance(cfg.backlog, backlog.GitHubBacklog) and not skip and not any(
                key in errors for key in ("kanban", "merge requests")):
            import github_scan
            try:
                (pages / github_scan.CHECKPOINT).unlink(missing_ok=True)
            except OSError:
                pass  # A completed checkpoint may be safely replayed after a crash.
    except OSError as e:
        got = replace(got, errors={**errors, "cache": str(e)})
    return got


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--root", type=Path, default=Path("."), help="workspace root holding CLAUDE.md")
    ap.add_argument("--refresh", action="store_true", help="read every source now and rewrite the cache")
    args = ap.parse_args(argv)
    now = datetime.now().astimezone()
    try:
        cfg = read_config(args.root)
    except (OSError, ConfigError) as e:
        print(f"sources: {e}", file=sys.stderr)
        return 2
    got = refresh(args.root, now) if args.refresh else load(pages_dir(args.root, cfg, now.astimezone(cfg.zone).date().isoformat()))
    print(toon_encode({"fetched": {k: v.astimezone(cfg.zone).strftime("%H:%M") for k, v in got.fetched.items()},
                       "issues": len(got.issues), "changes": len(got.changes), "workers": len(got.workers),
                       "errors": got.errors}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
