"""board_sources.py: the one place the pages get forge and worker facts, cached beside them."""

from __future__ import annotations

import io
import json
import os
import re
import shutil
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from dataclasses import replace
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

sys.path[:0] = [str(Path(__file__).resolve().parents[1] / "scripts"), str(Path(__file__).resolve().parent)]

import board_sources as bs  # noqa: E402
import render_board as rb  # noqa: E402
import issue_page as ip  # noqa: E402
import task_forge  # noqa: E402
import settings as st  # noqa: E402
from tracker import parse_tracker  # noqa: E402
import workers_page  # noqa: E402
from tracker import issue_key  # noqa: E402
from workspace import parse_coordinator  # noqa: E402
import test_flow_chart as fc_test  # noqa: E402

ZONE = ZoneInfo("America/Chicago")
NOW = datetime(2026, 9, 26, 2, 10, tzinfo=ZONE)
DAY = NOW.date().isoformat()
HEAD = "a" * 40
MERGE = "b" * 40

TRACKER = f"""# Tracker {DAY}

## Tasks

| name | item | owner | state | since | due | size | issue | checklist |
|---|---|---|---|---|---|---|---|---|
| Rate limit | Rate limit headers, PR #58 | impl-1 | running 01:28 | 01:28 |  | M | #118 |  |
| Search | Search pagination | Robin | waiting | 01:00 |  | S | {{issue}} | {{mr}} |

## Sessions

| ref | name | state | doing | waiting on | free at | constraints | children | last reply |
|---|---|---|---|---|---|---|---|---|
| abc123 | reviewer-2 | working | review of #58 | nothing |  |  | none | 01:58 reviewing |

## Log

- 01:28 one-shot impl-1 started: claude opus, worktree `rate-limit`, task Rate limit headers, PR #58
"""


def workspace(backlog: str, issue: str = "#115", mr: str = "") -> Path:
    root = Path(tempfile.mkdtemp())
    (root / "CLAUDE.md").write_text(
        "## Coordinator\n- User: Robin\n- Timezone: America/Chicago\n- Daily log dir: `daily/`\n"
        "- Tracker: `daily/<date>-tracker.md`\n- Worktrees: `trees/`\n"
        "- Board: self-hosted; URL http://127.0.0.1:8765/; dir `pages/`\n"
        f"- Settings: `cos.toml`\n- Backlog: {backlog}\n")
    (root / "cos.toml").write_text('[workflow]\nmerge_owner = "approval"\napprover = "robin"\n')
    (root / "daily").mkdir()
    (root / "daily" / f"{DAY}-tracker.md").write_text(TRACKER.replace("{issue}", issue).replace("{mr}", mr))
    return root


def gh_pr(number, merged_at=None, state="OPEN"):
    return {"__typename": "PullRequest", "number": number, "url": f"https://github.com/o/app/pull/{number}",
            "title": f"PR {number}", "state": state, "isDraft": False, "mergedAt": merged_at,
            "baseRefName": "main", "body": "Closes #118", "headRefOid": HEAD, "reviewDecision": "APPROVED",
            "files": {"nodes": [{"path": "src/a.py"}]},
            "latestReviews": {"nodes": [{"author": {"login": "robin"}}, {"author": {"login": "robin"}, "state": "APPROVED",
                                         "submittedAt": "2026-09-26T06:00:00Z", "commit": {"oid": HEAD}}][1:]},
            "commits": {"nodes": [{"commit": {"statusCheckRollup": {"contexts": {"nodes": [
                {"__typename": "CheckRun", "status": "COMPLETED", "conclusion": "SUCCESS"}]}}}}]}}


def gh_issue(number, labels=("stage:2-implement",)):
    return {"__typename": "Issue", "number": number, "url": f"https://github.com/o/app/issues/{number}",
            "title": f"Issue {number}", "state": "OPEN", "labels": {"nodes": [{"name": x} for x in labels]}}


class FakeGh:
    def __init__(self, fail=False, **nodes):
        self.calls, self.fail, self.nodes = [], fail, nodes

    def __call__(self, args, input_text=None):
        self.calls.append(args)
        if self.fail:
            return 1, "", "gh: not logged in"
        query = args[args.index("-f") + 1]
        repo = {"n118": gh_issue(118), "n115": gh_issue(115, ()), "n58": gh_pr(58), **self.nodes}
        data = {"r0": {n: repo.get(n) for n in re.findall(r"(n\d+): issueOrPullRequest", query)},
                "rateLimit": {"cost": 1, "remaining": 4900, "limit": 5000},
                "merged": {"nodes": [gh_pr(41, "2026-09-26T05:41:00Z", "MERGED")],
                           "pageInfo": {"hasNextPage": False, "endCursor": None}}}
        return 0, json.dumps({"data": data}), ""


class GitHubTest(unittest.TestCase):
    def setUp(self):
        self.root = workspace("GitHub issues; repo o/app")
        self.gh = FakeGh()
        self.got = bs.refresh(self.root, NOW, gh=self.gh)

    def test_named_and_merged_are_separate_bounded_queries(self):
        self.assertEqual(len(self.gh.calls), 2)
        queries = [args[args.index("-f") + 1] for args in self.gh.calls]
        for asked in ("n118: issueOrPullRequest(number: 118)", "n115:", "n58:"):
            self.assertIn(asked, queries[0])
        self.assertIn("is:merged", queries[1])
        self.assertIn("first: 5", queries[1])

    def test_issues_and_changes_by_ref(self):
        self.assertEqual(self.got.issues["#118"].labels, ("stage:2-implement",))
        pr = self.got.changes["#58"]
        self.assertEqual((pr.state, pr.pipeline, pr.approved, pr.approvals, pr.base, pr.files, pr.issues),
                         ("open", "passed", True, 1, "main", ("src/a.py",), ("#118",)))

    def test_a_change_carries_its_head_commit(self):
        # headRefOid, which GH_CHANGE already asks for, is the commit the decision page graphs.
        self.assertEqual(self.got.changes["#58"].head, HEAD)

    def test_merged_today_is_included(self):
        merged = self.got.changes["#41"]
        self.assertEqual((merged.state, merged.merged_at.astimezone(ZONE).strftime("%H:%M")), ("merged", "00:41"))

    def test_fetched_times_and_the_cache_round_trip(self):
        self.assertEqual(self.got.fetched, {"kanban": NOW, "merge requests": NOW, "workers": NOW})
        self.assertEqual(bs.load(self.root / "pages"), self.got)

    def test_a_failed_forge_is_an_error_and_keeps_the_last_good_facts(self):
        later = NOW.replace(hour=3)
        again = bs.refresh(self.root, later, gh=FakeGh(fail=True))
        self.assertIn("not logged in", again.errors["kanban"])
        self.assertIn("not logged in", again.errors["merge requests"])
        self.assertEqual((again.issues, again.changes), (self.got.issues, self.got.changes))
        self.assertEqual((again.fetched["kanban"], again.fetched["workers"]), (NOW, later))


class GitLabTest(unittest.TestCase):
    def test_graphql_calls_with_the_backlog_token(self):
        root = workspace("GitLab issues; host https://labs.example.test; project team/app", mr="!54")
        calls = []
        mr = {"iid": "54", "webUrl": "https://labs.example.test/team/app/-/merge_requests/54", "title": "Search",
              "state": "opened", "draft": False, "mergedAt": None, "targetBranch": "main",
              "description": "Closes #115", "approved": False, "approvedBy": {"nodes": [{"username": "robin"}]},
              "headPipeline": {"status": "PENDING"}, "diffStats": [{"path": "src/search.py"}]}
        issue = {"iid": "115", "webUrl": "https://labs.example.test/team/app/-/issues/115", "title": "Search",
                 "state": "opened", "labels": {"nodes": [{"title": "stage:3-pr"}]}}

        def call(method, url, token, timeout, payload=None):
            calls.append((method, url, token, payload))
            return {"data": {"p0": {"issues": {"nodes": [issue]}, "mergeRequests": {"nodes": [mr]},
                                    "merged": {"nodes": []}}}}, {}, ""

        with mock.patch.dict(os.environ, {"CHIEF_OF_STUFF_GITLAB_TOKEN": "tok"}):
            got = bs.refresh(root, NOW, call=call)
        # Every call goes to the host's GraphQL with the Backlog token; an open change adds one for its jobs and threads.
        self.assertEqual({(m, u, t) for m, u, t, _ in calls}, {("POST", "https://labs.example.test/api/graphql", "tok")})
        self.assertTrue(any('mergeRequests(iids: ["54"]' in c[3]["query"] for c in calls))
        change = got.changes["!54"]
        self.assertEqual((change.state, change.pipeline, change.approved, change.approvals, change.files, change.issues),
                         ("open", "pending", True, 1, ("src/search.py",), ("#115",)))
        self.assertEqual(got.issues["#115"].labels, ("stage:3-pr",))
        self.assertEqual(got.errors, {})

    def test_a_change_carries_its_head_commit(self):
        # diffHeadSha, asked for in the query, is the commit the decision page graphs.
        root = workspace("GitLab issues; host https://labs.example.test; project team/app", mr="!54")
        calls = []
        mr = {"iid": "54", "webUrl": "https://labs.example.test/team/app/-/merge_requests/54", "title": "Search",
              "state": "opened", "draft": False, "mergedAt": None, "targetBranch": "main", "description": "",
              "approved": False, "approvedBy": {"nodes": []}, "headPipeline": None, "diffStats": [], "diffHeadSha": HEAD}

        def call(method, url, token, timeout, payload=None):
            calls.append(payload)
            return {"data": {"p0": {"mergeRequests": {"nodes": [mr]}, "merged": {"nodes": []}}}}, {}, ""

        with mock.patch.dict(os.environ, {"CHIEF_OF_STUFF_GITLAB_TOKEN": "tok"}):
            got = bs.refresh(root, NOW, call=call)
        # The jobs-and-threads query runs for open changes only, so a merged change keeps its head from these two.
        by_kind = {gl_connections(c["query"])[0]: c["query"] for c in calls}
        self.assertIn("diffHeadSha", by_kind["named"])
        self.assertIn("diffHeadSha", by_kind["merged"])
        self.assertEqual(got.changes["!54"].head, HEAD)
        self.assertEqual(bs.load(root / "pages").changes["!54"].head, HEAD)

    def test_no_token_is_an_error_not_a_raise(self):
        root = workspace("GitLab issues; host https://labs.example.test; project team/app")
        with mock.patch.object(bs.backlog, "token", return_value=""):
            got = bs.refresh(root, NOW, call=lambda *a, **k: self.fail("no call without a token"))
        self.assertIn("no token", got.errors["kanban"])

    def test_a_store_workspace_reads_every_open_merge_request(self):
        # #896: a store board names its changes in no tracker row, so every open one is read.
        root = workspace("GitLab issues; host https://labs.example.test; project team/app")
        import sqlite3
        with sqlite3.connect(root / "store.db") as db:
            db.execute("CREATE TABLE work_item (issue_key TEXT)")
            db.execute("CREATE TABLE resume (issue_key TEXT, data TEXT)")
        mr = {"iid": "77", "webUrl": "https://labs.example.test/team/app/-/merge_requests/77", "title": "Search",
              "state": "opened", "draft": False, "mergedAt": None, "targetBranch": "develop", "description": "Closes #115",
              "approved": False, "approvedBy": {"nodes": []}, "headPipeline": None, "diffStats": []}

        def call(method, url, token, timeout, payload=None):
            opened = {"open": {"nodes": [mr]}} if "state: opened" in payload["query"] else {}
            return {"data": {"p0": {"issues": {"nodes": []}, "mergeRequests": {"nodes": []}, "merged": {"nodes": []}, **opened}}}, {}, ""

        with mock.patch.dict(os.environ, {"CHIEF_OF_STUFF_GITLAB_TOKEN": "tok"}):
            got = bs.refresh(root, NOW, call=call)
        self.assertEqual((got.changes["!77"].state, got.changes["!77"].issues), ("open", ("#115",)))


# Stage 2 of the issue page: per-file stats, pipeline jobs, conflicts, review threads and the issue body.
def gh_comment(login, body, at, n):
    return {"author": {"login": login}, "body": body, "createdAt": at, "url": f"https://github.com/o/app/pull/58#discussion_r{n}"}


def gh_rich(mergeable="CONFLICTING"):
    pr = gh_pr(58)
    pr["mergeable"] = mergeable
    pr["files"] = {"nodes": [{"path": "src/a.py", "additions": 12, "deletions": 3},
                             {"path": "src/b.py", "additions": 0, "deletions": 7}]}
    pr["commits"] = {"nodes": [{"commit": {"statusCheckRollup": {"contexts": {"nodes": [
        {"__typename": "CheckRun", "name": "unit-tests", "status": "COMPLETED", "conclusion": "FAILURE",
         "startedAt": "2026-09-26T06:00:00Z", "completedAt": "2026-09-26T06:05:10Z"},
        {"__typename": "CheckRun", "name": "lint-code", "status": "IN_PROGRESS", "conclusion": None,
         "startedAt": "2026-09-26T06:00:00Z", "completedAt": None}]}}}}]}
    pr["reviewThreads"] = {"nodes": [
        {"isResolved": False, "comments": {"nodes": [gh_comment("reviewer-a", "Cap should be configurable", "2026-09-26T06:30:00Z", 1),
                                                     gh_comment("impl-1", "Done", "2026-09-26T06:40:00Z", 2)]}},
        {"isResolved": True, "comments": {"nodes": [gh_comment("reviewer-b", "Typo in the message", "2026-09-26T05:10:00Z", 3)]}}]}
    return pr


def utc(hhmm):
    return datetime.fromisoformat(f"{DAY}T{hhmm}:00+00:00")


class GitHubStageTwoTest(unittest.TestCase):
    """B1-B4, C1 from GitHub: files' additions and deletions, check runs, mergeable, reviewThreads, the issue's body."""

    def refresh(self, **nodes):
        self.root = workspace("GitHub issues; repo o/app")
        self.gh = FakeGh(**nodes)
        got = bs.refresh(self.root, NOW, gh=self.gh)
        self.query = self.gh.calls[0][self.gh.calls[0].index("-f") + 1]
        self.assertEqual(bs.load(self.root / "pages"), got)  # the page reads these back from the cache
        return got

    def test_per_file_additions_and_deletions(self):
        # B1: Change carries per-file additions and deletions.
        got = self.refresh(n58=gh_rich()).changes["#58"]
        self.assertIn("additions", self.query)
        self.assertEqual([(f.path, f.additions, f.deletions) for f in got.stats], [("src/a.py", 12, 3), ("src/b.py", 0, 7)])

    def test_pipeline_jobs_are_the_head_commits_check_runs(self):
        # B2: jobs (name, status, duration seconds or None) from GitHub check runs.
        got = self.refresh(n58=gh_rich()).changes["#58"]
        self.assertIn("startedAt", self.query)
        self.assertEqual([(j.name, j.status, j.seconds) for j in got.jobs],
                         [("unit-tests", "failed", 310), ("lint-code", "running", None)])

    def test_conflicts_is_mergeable_conflicting(self):
        # B3: GitHub mergeable == "CONFLICTING".
        for mergeable, conflicts in (("CONFLICTING", True), ("MERGEABLE", False)):
            with self.subTest(mergeable=mergeable):
                self.assertIs(self.refresh(n58=gh_rich(mergeable)).changes["#58"].conflicts, conflicts)
                self.assertIn("mergeable", self.query)

    def test_review_threads_from_the_first_comment(self):
        # B4: resolved, the first comment's author and body, its time, and a URL, from reviewThreads.
        got = self.refresh(n58=gh_rich()).changes["#58"]
        self.assertIn("reviewThreads", self.query)
        self.assertEqual([(t.resolved, t.author, t.body, t.at, t.url) for t in got.threads], [
            (False, "reviewer-a", "Cap should be configurable", utc("06:30"), "https://github.com/o/app/pull/58#discussion_r1"),
            (True, "reviewer-b", "Typo in the message", utc("05:10"), "https://github.com/o/app/pull/58#discussion_r3")])

    def test_an_issue_carries_its_body(self):
        # C1: Issue gains body, from GitHub body.
        issue = {**gh_issue(118), "body": "### Acceptance\n- [ ] limits hold\n"}
        self.assertEqual(self.refresh(n118=issue).issues["#118"].body, "### Acceptance\n- [ ] limits hold\n")
        self.assertIn("body", self.query.split("... on Issue", 1)[1].split("... on PullRequest", 1)[0])

    def test_an_issue_carries_its_milestone(self):
        # #230: the Epics tab reads milestones. GitHub's dueOn is the date as written: midnight UTC on the 1st, which is
        # 19:00 on the 30th in Chicago (COS Architecture on #230; #580 review R1: GitHub answers T00:00:00Z).
        milestone = {"title": "v2", "url": "https://github.com/o/app/milestone/3", "dueOn": "2026-10-01T00:00:00Z",
                     "state": "OPEN", "issues": {"totalCount": 4}}
        got = self.refresh(n118={**gh_issue(118), "milestone": milestone}).issues
        self.assertEqual(got["#118"].milestone,
                         bs.Milestone("v2", "https://github.com/o/app/milestone/3", date(2026, 10, 1), "open", 4))
        self.assertIsNone(got["#115"].milestone)
        self.assertIn("milestone", self.query.split("... on Issue", 1)[1].split("... on PullRequest", 1)[0])


class GitLabStageTwoTest(unittest.TestCase):
    """B1-B4, C1 from GitLab: diffStats, headPipeline.jobs, conflicts, resolvable discussions, the issue's description."""

    MR = {"iid": "54", "webUrl": "https://labs.example.test/team/app/-/merge_requests/54", "title": "Search",
          "state": "opened", "draft": False, "mergedAt": None, "targetBranch": "main", "description": "Closes #115",
          "approved": False, "approvedBy": {"nodes": []}, "diffHeadSha": HEAD, "conflicts": True,
          "diffStats": [{"path": "src/search.py", "additions": 7, "deletions": 2}],
          "headPipeline": {"status": "RUNNING", "jobs": {"nodes": [
              {"name": "unit-tests", "status": "SUCCESS", "duration": 42},
              {"name": "lint-code", "status": "RUNNING", "duration": None}]}},
          "discussions": {"nodes": [
              {"resolvable": True, "resolved": False, "notes": {"nodes": [
                  {"author": {"username": "reviewer-a"}, "body": "Page size is off by one", "createdAt": "2026-09-26T06:30:00Z",
                   "url": "https://labs.example.test/team/app/-/merge_requests/54#note_1"},
                  {"author": {"username": "impl-1"}, "body": "Fixed", "createdAt": "2026-09-26T06:45:00Z",
                   "url": "https://labs.example.test/team/app/-/merge_requests/54#note_2"}]}},
              {"resolvable": True, "resolved": True, "notes": {"nodes": [
                  {"author": {"username": "reviewer-b"}, "body": "Name the constant", "createdAt": "2026-09-26T05:10:00Z",
                   "url": "https://labs.example.test/team/app/-/merge_requests/54#note_3"}]}},
              {"resolvable": False, "resolved": False, "notes": {"nodes": [
                  {"author": {"username": "reviewer-a"}, "body": "Looks close", "createdAt": "2026-09-26T06:50:00Z",
                   "url": "https://labs.example.test/team/app/-/merge_requests/54#note_4"}]}}]}}
    ISSUE = {"iid": "115", "webUrl": "https://labs.example.test/team/app/-/issues/115", "title": "Search", "state": "opened",
             "labels": {"nodes": []}, "description": "## Acceptance\n- [x] pages hold\n"}

    def refresh(self, mr=None):
        root = workspace("GitLab issues; host https://labs.example.test; project team/app", mr="!54")
        calls = []

        def call(method, url, token, timeout, payload=None):
            calls.append(payload)
            return {"data": {"p0": {"issues": {"nodes": [self.ISSUE]}, "mergeRequests": {"nodes": [mr or self.MR]},
                                    "merged": {"nodes": []}}}}, {}, ""

        with mock.patch.dict(os.environ, {"CHIEF_OF_STUFF_GITLAB_TOKEN": "tok"}):
            got = bs.refresh(root, NOW, call=call)
        self.queries = [c["query"] for c in calls]
        self.query = " ".join(self.queries)
        self.assertEqual(bs.load(root / "pages"), got)  # the page reads these back from the cache
        return got

    def test_jobs_and_threads_are_asked_for_open_merge_requests_only(self):
        # A live GitLab refused the board's one query at complexity 298 of 250 once every change carried its
        # jobs and discussions. Only an open change's card lists them, so only open changes ask for them.
        self.refresh()
        main = [q for q in self.queries if "discussions" not in q and "jobs" not in q]
        detail = [q for q in self.queries if "discussions" in q or "jobs" in q]
        self.assertEqual(sum("merged:" in q for q in main), 1)  # one query per connection; the merged page is one of them
        self.assertEqual(len(detail), 1)
        self.assertNotIn("merged:", detail[0])
        self.assertIn('mergeRequests(iids: ["54"]', detail[0])  # the argument list may go on, with `first:`

    def test_the_board_asks_for_no_squash_commit(self):
        # #45: GitLab's MergeRequest has no `squashCommitSha`, so a query naming it fails outright.
        self.refresh()
        self.assertNotIn("squashCommitSha", self.query)

    def test_no_open_merge_request_asks_for_no_jobs_or_threads(self):
        got = self.refresh({**self.MR, "state": "merged", "mergedAt": "2026-09-26T06:00:00Z"})
        self.assertNotIn("discussions", self.query)
        self.assertNotIn("jobs", self.query)
        self.assertEqual((got.changes["!54"].jobs, got.changes["!54"].threads), ((), ()))

    def test_per_file_additions_and_deletions(self):
        # B1: Change carries per-file additions and deletions (diffStats).
        got = self.refresh().changes["!54"]
        self.assertIn("additions", self.query)
        self.assertEqual([(f.path, f.additions, f.deletions) for f in got.stats], [("src/search.py", 7, 2)])

    def test_pipeline_jobs_are_the_head_pipelines_jobs(self):
        # B2: jobs (name, status, duration seconds or None) from GitLab headPipeline.jobs.
        got = self.refresh().changes["!54"]
        self.assertIn("jobs", self.query)
        self.assertEqual([(j.name, j.status, j.seconds) for j in got.jobs],
                         [("unit-tests", "passed", 42), ("lint-code", "running", None)])

    def test_conflicts_is_the_merge_requests_conflicts(self):
        # B3: GitLab conflicts.
        for conflicts in (True, False):
            with self.subTest(conflicts=conflicts):
                self.assertIs(self.refresh({**self.MR, "conflicts": conflicts}).changes["!54"].conflicts, conflicts)
                self.assertIn("conflicts", self.query)

    def test_review_threads_are_the_resolvable_discussions(self):
        # B4: resolvable discussions only; the first note's author, body, time and URL.
        got = self.refresh().changes["!54"]
        self.assertIn("discussions", self.query)
        self.assertEqual([(t.resolved, t.author, t.body, t.at, t.url) for t in got.threads], [
            (False, "reviewer-a", "Page size is off by one", utc("06:30"), "https://labs.example.test/team/app/-/merge_requests/54#note_1"),
            (True, "reviewer-b", "Name the constant", utc("05:10"), "https://labs.example.test/team/app/-/merge_requests/54#note_3")])

    def test_an_issue_carries_its_description_as_body(self):
        # C1: Issue gains body, from GitLab description.
        self.assertEqual(self.refresh().issues["#115"].body, "## Acceptance\n- [x] pages hold\n")
        self.assertIn("description", self.query.split("issues(", 1)[1].split("mergeRequests(", 1)[0])

    def test_an_issue_carries_its_milestone(self):
        # #230: GitLab's active is open; its open issues are the total less the closed.
        self.ISSUE = {**self.ISSUE, "milestone": {"title": "v2", "webPath": "/team/app/-/milestones/3", "dueDate": "2026-10-01",
                                                  "state": "active", "stats": {"totalIssuesCount": 5, "closedIssuesCount": 2}}}
        self.assertEqual(self.refresh().issues["#115"].milestone,
                         bs.Milestone("v2", "https://labs.example.test/team/app/-/milestones/3", date(2026, 10, 1), "open", 3))
        self.assertIn("milestone", self.query.split("issues(", 1)[1].split("mergeRequests(", 1)[0])

    def test_an_issue_with_no_milestone_has_none(self):
        self.assertIsNone(self.refresh().issues["#115"].milestone)


class FakeGitLab:
    """GitLab's /api/graphql as far as paging goes: a connection answers at most 100 nodes, from `after:` (a
    cursor it gave in pageInfo) for `first:` nodes, and pageInfo only when the query asks for it. Aliases,
    literal arguments and $variables; every project holds the same issues and merge requests, and one the
    token cannot see (`hidden`) answers null. `errors` ride beside the data."""

    PAGE = 100
    PROJECT = re.compile(r'(?:(\w+)\s*:\s*)?project\s*\(\s*fullPath\s*:\s*("[^"]*"|\$\w+)\s*\)')
    CONNECTION = re.compile(r"(?:(\w+)\s*:\s*)?\b(issues|mergeRequests)\s*\(([^)]*)\)")
    ARG = re.compile(r'(\w+)\s*:\s*(\[[^\]]*\]|"[^"]*"|\$\w+|[\w.-]+)')

    def __init__(self, issues=(), mrs=(), hidden=(), errors=()):
        self.nodes = {"issues": list(issues), "mergeRequests": list(mrs)}
        self.queries, self.posts, self.hidden, self.errors = [], [], set(hidden), list(errors)

    def __call__(self, method, url, token, timeout, payload=None):
        query, variables = payload["query"], payload.get("variables") or {}
        self.queries.append(query)
        self.posts.append((method, url, token))
        starts = list(self.PROJECT.finditer(query)) + [None]
        data = {}
        for m, nxt in zip(starts, starts[1:]):
            if m[2].strip('"') in self.hidden:
                data[m[1] or "project"] = None
                continue
            project = data.setdefault(m[1] or "project", {})
            for c in self.CONNECTION.finditer(query, m.end(), nxt.start() if nxt else len(query)):
                project[c[1] or c[2]] = self.page(c[2], self.args(c[3], variables), "pageInfo" in query)
        return {"data": data, **({"errors": self.errors} if self.errors else {})}, {}, ""

    def args(self, text, variables):
        return {name: (variables.get(value[1:]) if value.startswith("$")
                       else json.loads(value) if value[0] in '["' or value.isdigit() else value)
                for name, value in self.ARG.findall(text)}

    def page(self, kind, args, page_info):
        nodes = self.nodes[kind]
        if args.get("iids") is not None:
            wanted = {str(n) for n in args["iids"]}
            nodes = [n for n in nodes if n["iid"] in wanted]
        if args.get("state"):
            nodes = [n for n in nodes if n["state"] == str(args["state"]).lower()]
        if args.get("mergedAfter"):
            after = datetime.fromisoformat(args["mergedAfter"])
            nodes = [n for n in nodes if n.get("mergedAt") and datetime.fromisoformat(n["mergedAt"]) >= after]
        start = int(args["after"]) if args.get("after") else 0
        size = min(int(args.get("first") or self.PAGE), self.PAGE)
        got = {"nodes": nodes[start:start + size]}
        if page_info:
            got["pageInfo"] = {"hasNextPage": start + size < len(nodes), "endCursor": str(start + size)}
        return got


def gl_issue(iid):
    return {"iid": str(iid), "webUrl": f"https://labs.example.test/team/app/-/issues/{iid}", "title": f"Issue {iid}",
            "state": "opened", "labels": {"nodes": []}, "description": ""}


def gl_mr(iid, merged_at=None):
    return {"iid": str(iid), "webUrl": f"https://labs.example.test/team/app/-/merge_requests/{iid}", "title": f"MR {iid}",
            "state": "merged" if merged_at else "opened", "draft": False, "mergedAt": merged_at, "targetBranch": "main",
            "description": "", "approved": False, "approvedBy": {"nodes": []}, "headPipeline": None, "diffStats": [],
            "diffHeadSha": HEAD}


def gitlab_workspace(rows):
    """A GitLab workspace whose tracker names one task per (issue, merge request) row."""
    root = workspace("GitLab issues; host https://labs.example.test; project team/app")
    head = TRACKER.split("| Rate limit |", 1)[0]
    (root / "daily" / f"{DAY}-tracker.md").write_text(
        head + "".join(f"| Task {k} | Item {k} | Robin | waiting | 01:00 |  | S | {issue} | {mr} |\n"
                       for k, (issue, mr) in enumerate(rows)))
    return root


def refresh_gitlab(root, now, gitlab):
    with mock.patch.dict(os.environ, {"CHIEF_OF_STUFF_GITLAB_TOKEN": "tok"}):
        return bs.refresh(root, now, call=gitlab)


def refresh_rows(rows, gitlab):
    return refresh_gitlab(gitlab_workspace(rows), NOW, gitlab)


class GitLabPastOnePageTest(unittest.TestCase):
    """GitLab answers at most 100 nodes a connection page; the board wants every issue and change the tracker names."""

    def refresh(self, rows, gitlab):
        got = refresh_rows(rows, gitlab)
        self.assertEqual(got.errors, {})
        return got

    def test_every_named_issue_past_the_first_hundred(self):
        iids = range(1001, 1151)
        got = self.refresh([(f"#{n}", "") for n in iids], FakeGitLab(issues=[gl_issue(n) for n in range(1001, 1301)]))
        self.assertEqual(len(got.issues), 150)
        self.assertEqual(sorted(got.issues), sorted(f"#{n}" for n in iids))

    def test_every_named_change_past_the_first_hundred(self):
        iids = range(2001, 2151)
        got = self.refresh([("", f"!{n}") for n in iids], FakeGitLab(mrs=[gl_mr(n) for n in range(2001, 2301)]))
        self.assertEqual(len(got.changes), 150)
        self.assertEqual(sorted(got.changes), sorted(f"!{n}" for n in iids))

    def test_every_change_merged_today_past_the_first_hundred(self):
        merged = [gl_mr(n, f"{DAY}T06:{n % 60:02d}:00+00:00") for n in range(3001, 3131)]
        got = self.refresh([("#1001", "")], FakeGitLab(issues=[gl_issue(1001)], mrs=merged))
        self.assertEqual(len(got.changes), 130)
        self.assertEqual(sorted(got.changes), sorted(f"!{n}" for n in range(3001, 3131)))


# GitLab refuses a query whose complexity passes 250 ("Query has complexity of 281, which exceeds max complexity of
# 250"). gl_score() is GitLab's rule as its source states it (Types::BaseField#field_resolver_complexity and
# Resolvers::BaseResolver.complexity_multiplier), with the per-field weights fitted to what gitlab.com's
# `queryComplexity { score }` answered for these bodies on 2026-10-06 (FROZEN below, each to the point):
#   a leaf field                      1, GL_LEAF where GitLab charges more
#   an object field, and `nodes`      1 + its children
#   a connection (a field with a `nodes` child)
#                                     int((its children + 1, and 1 more directly under the project) * (1 + page * m))
#                                     page = min(first, 100), and 100 when no `first:` is given; m = 0.05 when the
#                                     connection takes an `iids:` list, else 0.01; GL_FLAT connections are not scaled
#   the project                       1
GL_CAP = 250
GL_LEAF = {"mergedAt": 5, "approved": 2, "diffHeadSha": 2, "mergeableDiscussionsState": 2, "detailedMergeStatus": 2}
GL_FLAT = {"approvedBy", "discussions"}


def gl_score(query: str) -> int:
    args = []
    text = re.sub(r"\(([^)]*)\)", lambda m: f" ARGS{args.append(m[1]) or len(args) - 1} ", query)
    toks, at = re.findall(r"ARGS\d+|\w+|[{}:]", text), [2]  # past `query {`

    def fields():
        out = []
        while toks[at[0]] != "}":
            name, arg, kids = toks[at[0]], "", None
            at[0] += 1
            if toks[at[0]] == ":":  # an alias: the field is the name after it
                name, at[0] = toks[at[0] + 1], at[0] + 2
            if toks[at[0]].startswith("ARGS"):
                arg, at[0] = args[int(toks[at[0]][4:])], at[0] + 1
            if toks[at[0]] == "{":
                at[0] += 1
                kids = fields()
            out.append((name, arg, kids))
        at[0] += 1
        return out

    def cost(name, arg, kids, top):
        if kids is None:
            return GL_LEAF.get(name, 1)
        inner = sum(cost(*k, False) for k in kids)
        if name == "nodes":
            return 1 + inner
        if any(k[0] == "nodes" for k in kids):
            first = re.search(r"first\s*:\s*(\d+)", arg)
            page = 0 if name in GL_FLAT else min(int(first[1]) if first else 100, 100)
            return int((inner + 1 + top) * (1 + page * (0.05 if "iids" in arg else 0.01)))
        return 1 + inner + (name == "diffStats")

    return sum(1 + sum(cost(*k, True) for k in project[2]) for project in fields())


def gl_connections(query: str) -> list[str]:
    """The top-level connections a body asks for: issues, named (merge requests by iid), merged, or detail (named, with jobs)."""
    return [kind if kind == "issues" else "merged" if re.match(r"\s*state\s*:\s*merged", args)
            else "detail" if "discussions" in query else "named"
            for kind, args in re.findall(r"\b(issues|mergeRequests)\s*\(([^)]*)\)", query)]


TRANSPORT, NO_DATA, BESIDE = "transport", "no data", "beside data"
ERR = "Query has complexity of 281"


class SelectingGitLab(FakeGitLab):
    """FakeGitLab that answers only the fields a body names, as GitLab does."""

    def __call__(self, method, url, token, timeout, payload=None):
        body, headers, err = super().__call__(method, url, token, timeout, payload)
        asked = set(re.findall(r"\w+", payload["query"]))
        for project in body["data"].values():
            for connection in (project or {}).values():
                connection["nodes"] = [{k: v for k, v in n.items() if k in asked} for n in connection["nodes"]]
        return body, headers, err


class FailingGitLab(FakeGitLab):
    """FakeGitLab that answers a body asking for the `fail` connection (issues, named, merged, detail) with an error
    in `mode`: a transport error, a GraphQL error body with no data, or GraphQL errors beside the data. Every body
    it was sent, the failing one too, is in `queries`."""

    def __init__(self, fail, mode, **kw):
        super().__init__(**kw)
        self.fail, self.mode = fail, mode

    def __call__(self, method, url, token, timeout, payload=None):
        if self.fail not in gl_connections(payload["query"]):
            return super().__call__(method, url, token, timeout, payload)
        if self.mode == BESIDE:
            body, headers, err = super().__call__(method, url, token, timeout, payload)
            return {**body, "errors": [{"message": ERR}]}, headers, err
        self.queries.append(payload["query"])
        return (None, {}, "HTTP 500") if self.mode == TRANSPORT else ({"errors": [{"message": ERR}]}, {}, "")


class DownGitLab(FakeGitLab):
    """A host that answers nothing: every body fails as a transport error, or as an error body with no data."""

    def __init__(self, mode, **kw):
        super().__init__(**kw)
        self.mode = mode

    def __call__(self, method, url, token, timeout, payload=None):
        self.queries.append(payload["query"])
        return (None, {}, "HTTP 502") if self.mode == TRANSPORT else ({"errors": [{"message": ERR}]}, {}, "")


# The board's selections as of #341's fix, frozen, with what gitlab.com answered for a body made of each (its probe's
# own 3 taken off): the scorer is tested against them, and the bodies main sent in one query are rebuilt from them.
FROZEN_ISSUE = "iid webUrl title state description labels { nodes { title } } closedAt"
FROZEN_CHANGE = ("iid webUrl title state draft mergedAt targetBranch description approved conflicts "
                 "approvedBy { nodes { username } } headPipeline { status } diffStats { path additions deletions } diffHeadSha")
FROZEN_DETAIL = ("iid headPipeline { jobs { nodes { name status duration } } } discussions { nodes { resolvable resolved "
                 "notes(first: 1) { nodes { author { username } body createdAt url } } } }")
FROZEN_IIDS = json.dumps([str(n) for n in range(1, 101)])
FROZEN_ISSUES = f"issues(iids: {FROZEN_IIDS}) {{ nodes {{ {FROZEN_ISSUE} }} }}"
FROZEN_NAMED = f"mergeRequests(iids: {FROZEN_IIDS}) {{ nodes {{ {{CHANGE}} }} }}"
FROZEN_MERGED = ('merged: mergeRequests(state: merged, mergedAfter: "2026-09-26T00:00:00-05:00") '
                 f"{{ nodes {{ {FROZEN_CHANGE} }} pageInfo {{ hasNextPage endCursor }} }}")


def frozen_body(*fields):
    return 'query { p0: project(fullPath: "team/app") { ' + " ".join(fields) + " } }"


def frozen_named(extra=""):
    return FROZEN_NAMED.replace("{CHANGE}", FROZEN_CHANGE + extra)


FROZEN = {"issues": (frozen_body(FROZEN_ISSUES), 91), "named": (frozen_body(frozen_named()), 181),
          "merged": (frozen_body(FROZEN_MERGED), 67),
          "detail": (frozen_body(f"mergeRequests(iids: {FROZEN_IIDS}) {{ nodes {{ {FROZEN_DETAIL} }} }}"), 157),
          "main's one query": (frozen_body(FROZEN_ISSUES, frozen_named(), FROZEN_MERGED), 337)}


class GitLabComplexityModelTest(unittest.TestCase):
    """The scorer the next tests rely on is GitLab's, not a figure the board's author picked."""

    def test_the_scorer_gives_the_scores_gitlab_answered(self):
        self.assertEqual({k: gl_score(q) for k, (q, _) in FROZEN.items()}, {k: score for k, (_, score) in FROZEN.items()})

    def test_the_query_main_sent_was_over_the_cap(self):
        # GitLab answered 340 (337 and its probe's 3) from gitlab.com's 200 limit, and the live board 281 of 250.
        self.assertGreater(gl_score(FROZEN["main's one query"][0]), GL_CAP)

    def test_a_named_body_with_eleven_more_fields_is_over_the_cap(self):
        # The model must not pass what GitLab rejects: 11 more merge request fields (nine at 1, two at 2) took gitlab.com's
        # answer to 262, so 259 here.
        extra = " createdAt updatedAt reference sourceBranch squashOnMerge userNotesCount upvotes downvotes commitCount " \
                "mergeableDiscussionsState detailedMergeStatus"
        self.assertGreater(gl_score(frozen_body(frozen_named(extra))), GL_CAP)


class GitLabOneQueryPerConnectionTest(unittest.TestCase):
    """#341: issues, named merge requests and the merged page go in a query each, so none passes the cap."""

    ROWS = [(f"#{n}", f"!{n + 1000}") for n in range(1, 101)]  # a hundred named issues, a hundred named merge requests
    MERGED = [gl_mr(n, f"{DAY}T06:{n % 60:02d}:00+00:00") for n in range(3001, 3131)]  # and two merged pages

    def gitlab(self, cls=FakeGitLab, *args):
        return cls(*args, issues=[gl_issue(n) for n in range(1, 101)], mrs=[gl_mr(n) for n in range(1001, 1101)] + self.MERGED)

    def test_each_connection_has_its_own_post(self):
        fake = self.gitlab()
        refresh_rows(self.ROWS, fake)
        asked = [gl_connections(q) for q in fake.queries]
        self.assertTrue(all(len(a) == 1 for a in asked), asked)
        self.assertTrue({"issues", "named", "merged", "detail"} <= {a[0] for a in asked}, asked)

    def test_every_post_is_under_the_complexity_cap(self):
        fake = self.gitlab()
        refresh_rows(self.ROWS, fake)
        self.assertEqual({(tuple(gl_connections(q)), gl_score(q)) for q in fake.queries if gl_score(q) > GL_CAP}, set())

    def test_every_iids_connection_asks_for_exactly_its_chunk(self):
        # With no `first:` GitLab prices a connection at 100 nodes whatever it names: a named body scored 184 for 1 iid
        # or 100, and 35 with `first: 1`.
        for count, chunks in ((1, [1]), (7, [7]), (100, [100]), (150, [100, 50])):
            with self.subTest(iids=count):
                fake = FakeGitLab(issues=[gl_issue(n) for n in range(1, count + 1)], mrs=[gl_mr(n) for n in range(1001, 1001 + count)])
                refresh_rows([(f"#{n}", f"!{n + 1000}") for n in range(1, count + 1)], fake)
                asked = [(gl_connections(q)[0], len(re.findall(r'"\d+"', m[1])), (re.search(r"first\s*:\s*(\d+)", m[1]) or [0, None])[1])
                         for q in fake.queries for m in re.finditer(r"\(([^)]*iids[^)]*)\)", q)]
                self.assertEqual([a for a in asked if a[2] is None or int(a[2]) != a[1]], [])
                self.assertEqual([a[1] for a in asked if a[0] == "named"], chunks)

    def test_a_merged_change_keeps_its_head_commit(self):
        # The jobs-and-threads query runs for open changes only: a merged change's head comes from the merged body.
        merged = gl_mr(3001, f"{DAY}T06:00:00+00:00")
        fake = SelectingGitLab(issues=[gl_issue(1)], mrs=[gl_mr(1001), merged])
        got = refresh_rows([("#1", "!1001")], fake)
        by_kind = {gl_connections(q)[0]: q for q in fake.queries}
        self.assertIn("diffHeadSha", by_kind["named"])
        self.assertIn("diffHeadSha", by_kind["merged"])
        self.assertEqual((got.changes["!1001"].head, got.changes["!3001"].head), (HEAD, HEAD))

    def test_the_assembled_board_is_the_same(self):
        open_mr = {**gl_mr(1001), "headPipeline": {"status": "RUNNING", "jobs": {"nodes": [{"name": "unit-tests", "status": "SUCCESS", "duration": 4}]}},
                   "discussions": {"nodes": [{"resolvable": True, "resolved": False, "notes": {"nodes": [
                       {"author": {"username": "reviewer-a"}, "body": "Rename", "createdAt": "2026-09-26T06:30:00Z", "url": "u"}]}}]}}
        merged = self.MERGED[0]
        got = refresh_rows([("#1", "!1001"), ("#2", "")], FakeGitLab(issues=[gl_issue(1), gl_issue(2)], mrs=[open_mr, merged]))
        self.assertEqual(got.errors, {})
        self.assertEqual(sorted(got.issues), ["#1", "#2"])
        self.assertEqual(got.issues["#1"], bs.Issue("#1", gl_issue(1)["webUrl"], "Issue 1", "open", (), "", None))
        jobs, threads = bs._gl_detail(open_mr)
        self.assertEqual(got.changes["!1001"], replace(bs._gl_change(open_mr, "robin"), jobs=jobs, threads=threads))
        self.assertEqual(([j.name for j in jobs], [t.body for t in threads]), (["unit-tests"], ["Rename"]))
        self.assertEqual(got.changes["!3001"], bs._gl_change(merged, "robin"))  # a merged change asks for no jobs or threads
        self.assertEqual(sorted(got.changes), ["!1001", "!3001"])


class GitLabPanelsTest(unittest.TestCase):
    """A refresh after a good one. The kanban panel is the issues, the merge requests panel is the changes (named, merged
    and their jobs and threads); each keeps its last good cards and fetched time when its own connection fails, and
    carries only its own error."""

    LATER = NOW + timedelta(minutes=5)
    CONNECTIONS = ["issues", "named", "merged", "detail"]
    PANEL = {"issues": "kanban", "named": "merge requests", "merged": "merge requests", "detail": "merge requests"}
    UNIT = {"headPipeline": {"status": "RUNNING", "jobs": {"nodes": [{"name": "unit-tests", "status": "SUCCESS", "duration": 4}]}},
            "discussions": {"nodes": []}}

    def fake(self, cls=FakeGitLab, *args, title="Issue 1"):
        return cls(*args, issues=[{**gl_issue(1), "title": title}, gl_issue(2)],
                   mrs=[{**gl_mr(1001), **self.UNIT, "title": title}, gl_mr(1002), gl_mr(3001, f"{DAY}T06:00:00+00:00")])

    def setUp(self):
        self.root = gitlab_workspace([("#1", "!1001"), ("#2", "!1002")])
        self.first = refresh_gitlab(self.root, NOW, self.fake())
        self.assertEqual((self.first.errors, sorted(self.first.changes)), ({}, ["!1001", "!1002", "!3001"]))
        self.assertEqual([j.name for j in self.first.changes["!1001"].jobs], ["unit-tests"])

    def again(self, cls=FakeGitLab, *args):
        """A refresh five minutes on, with issue 1 and merge request 1001 renamed: what a panel that updated shows."""
        fake = self.fake(cls, *args, title="Renamed")
        got = refresh_gitlab(self.root, self.LATER, fake)
        self.assertEqual(bs.load(self.root / "pages"), got)
        return got, fake

    def kinds(self, fake):
        return [gl_connections(q)[0] for q in fake.queries]

    def test_a_failed_connection_leaves_its_panel_as_it_was_and_the_other_fresh(self):
        for fail in ("issues", "named", "merged"):
            with self.subTest(fail=fail):
                stale = self.PANEL[fail]
                fresh = "kanban" if stale == "merge requests" else "merge requests"
                self.setUp()  # each case starts from its own workspace and good cache
                got, _ = self.again(FailingGitLab, fail, NO_DATA)
                self.assertEqual((got.fetched[stale], got.fetched[fresh]), (NOW, self.LATER), f"{fail} fails: (stale panel, fresh panel) fetched")
                self.assertEqual(set(got.errors), {stale})
                self.assertIn("complexity", got.errors[stale])
                if stale == "kanban":
                    self.assertEqual((got.issues, got.changes["!1001"].title), (self.first.issues, "Renamed"))
                else:
                    self.assertEqual((got.changes, got.issues["#1"].title), (self.first.changes, "Renamed"))

    def test_every_connection_failing_keeps_the_last_good_cache(self):
        for mode in (TRANSPORT, NO_DATA):
            with self.subTest(mode=mode):
                got, fake = self.again(DownGitLab, mode)
                self.assertEqual((got.issues, got.changes), (self.first.issues, self.first.changes))
                self.assertEqual((got.fetched["kanban"], got.fetched["merge requests"], got.fetched["workers"]), (NOW, NOW, self.LATER))
                self.assertEqual(set(got.errors), {"kanban", "merge requests"})
                self.assertTrue(all(got.errors[p] in ("HTTP 502", ERR) for p in ("kanban", "merge requests")), got.errors)

    def test_graphql_errors_beside_data_keep_the_cards_and_name_their_panel(self):
        for fail in self.CONNECTIONS:
            with self.subTest(fail=fail):
                got, _ = self.again(FailingGitLab, fail, BESIDE)
                self.assertEqual(set(got.errors), {self.PANEL[fail]})
                self.assertIn("complexity", got.errors[self.PANEL[fail]])
                self.assertEqual((got.issues["#1"].title, got.changes["!1001"].title), ("Renamed", "Renamed"))
                self.assertEqual(sorted(got.changes), ["!1001", "!1002", "!3001"])
                self.assertEqual((got.fetched["kanban"], got.fetched["merge requests"]), (self.LATER, self.LATER))

    def test_a_failed_detail_request_leaves_the_open_changes_without_jobs_or_threads(self):
        for mode in (TRANSPORT, NO_DATA):
            with self.subTest(mode=mode):
                got, _ = self.again(FailingGitLab, "detail", mode)
                self.assertEqual(sorted(got.changes), ["!1001", "!1002", "!3001"])
                self.assertEqual([(c.state, c.jobs, c.threads) for c in map(got.changes.get, ("!1001", "!1002"))], [("open", (), ())] * 2)
                self.assertEqual((got.issues["#1"].title, got.changes["!1001"].title), ("Renamed", "Renamed"))
                self.assertEqual(set(got.errors), {"merge requests"})
                self.assertEqual((got.fetched["kanban"], got.fetched["merge requests"]), (self.LATER, self.LATER))

    def test_the_requests_are_issues_named_merged_then_detail(self):
        self.assertEqual(self.kinds(self.again()[1]), self.CONNECTIONS)

    def test_a_graphql_error_on_one_connection_does_not_stop_the_others(self):
        for fail in self.CONNECTIONS:
            for mode in (NO_DATA, BESIDE):
                with self.subTest(fail=fail, mode=mode):
                    asked = [k for k in self.CONNECTIONS if not (fail == "named" and mode == NO_DATA and k == "detail")]  # no open change to detail
                    self.assertEqual(self.kinds(self.again(FailingGitLab, fail, mode)[1]), asked)

    def test_after_a_transport_error_no_further_request_is_sent(self):
        # A host that is down costs one timeout, not one for each request left.
        for sent, fail in enumerate(self.CONNECTIONS, 1):
            with self.subTest(fail=fail):
                self.assertEqual(self.kinds(self.again(FailingGitLab, fail, TRANSPORT)[1]), self.CONNECTIONS[:sent])
        self.assertEqual(self.kinds(self.again(DownGitLab, TRANSPORT)[1]), ["issues"])


class FakePulls:
    """`gh api graphql` asked for pull requests by number, as GitHub answers it: a number that is not a pull
    request is a null node, a NOT_FOUND error at its alias, and exit 1 with the message on stderr. A field
    outside FIELDS, what GitHub answered the audit's read, is an `undefinedField` error and no data, as a
    field GitHub does not have is.

    `fail`, a `(code, out, err)` or an exception, is the answer instead; `errors` are errors of any other
    kind, beside the data, and one at a pull request's alias is that node's answer in place of NOT_FOUND;
    `missing_repo` answers as a repository that is not there."""

    FIELDS = {"number", "state", "baseRefName", "mergeCommit", "oid"}
    REPOSITORY = re.compile(r"(?:(\w+)\s*:\s*)?repository\s*\(")
    ALIAS = re.compile(r"(\w+)\s*:\s*(?:issueOrPullRequest|pullRequest)\s*\(\s*number\s*:\s*(\d+)\s*\)")
    SELECTION = re.compile(r"(\w+)\s*:\s*pullRequest\s*\([^)]*\)\s*\{((?:[^{}]|\{[^{}]*\})*)\}")

    def __init__(self, *pulls, fail=None, errors=(), missing_repo=False):
        self.pulls, self.fail, self.errors = {p["number"]: p for p in pulls}, fail, list(errors)
        self.missing_repo, self.calls, self.queries = missing_repo, [], []

    def __call__(self, args, input_text=None):
        self.calls.append(list(args))
        if isinstance(self.fail, Exception):
            raise self.fail
        if self.fail:
            return self.fail
        query = args[args.index("-f") + 1]
        self.queries.append(query)
        numbers = dict(self.ALIAS.findall(query))
        repository = self.REPOSITORY.search(query)[1] or "repository"
        undefined = [{"path": ["query", repository, alias, field], "message": f"Field '{field}' doesn't exist on type 'PullRequest'",
                      "extensions": {"code": "undefinedField", "typeName": "PullRequest", "fieldName": field}}
                     for alias, selection in self.SELECTION.findall(query)
                     for field in sorted(set(re.findall(r"\w+", selection)) - self.FIELDS)]
        if undefined:
            return self.answer({"errors": undefined})
        if self.missing_repo:
            return self.answer({"data": {repository: None}, "errors": [
                {"type": "NOT_FOUND", "path": [repository], "message": "Could not resolve to a Repository with the name 'o/gone'."}]})
        errors = self.errors + [{"type": "NOT_FOUND", "path": [repository, alias],
                                 "message": f"Could not resolve to a PullRequest with the number of {n}."}
                                for alias, n in numbers.items()
                                if int(n) not in self.pulls and not any(e.get("path") == [repository, alias] for e in self.errors)]
        body = {"data": {repository: {alias: self.pulls.get(int(n)) for alias, n in numbers.items()}}}
        return self.answer({**body, "errors": errors} if errors else body)

    @staticmethod
    def answer(body: dict) -> tuple[int, str, str]:
        errors = body.get("errors") or []
        return (1 if errors else 0), json.dumps(body), "".join(f"gh: {e['message']}\n" for e in errors)

    @property
    def asked(self) -> set[int]:
        return {int(n) for query in self.queries for _, n in self.ALIAS.findall(query)}


class StrictGitLab(FakeGitLab):
    """FakeGitLab for the audit's read: a MergeRequest field outside FIELDS, what gitlab.com answered that read,
    is an error and no data, as a field GitLab does not have is."""

    FIELDS = {"iid", "state", "targetBranch", "mergeCommitSha"}
    SELECTION = re.compile(r"mergeRequests\s*\([^)]*\)\s*\{\s*nodes\s*\{((?:[^{}]|\{[^{}]*\})*)\}")

    def __call__(self, method, url, token, timeout, payload=None):
        undefined = sorted({field for selection in self.SELECTION.findall(payload["query"])
                            for field in re.findall(r"\w+", selection)} - self.FIELDS)
        if undefined:
            self.queries.append(payload["query"])
            self.posts.append((method, url, token))
            return {"errors": [{"message": f"Field '{field}' doesn't exist on type 'MergeRequest'"} for field in undefined]}, {}, ""
        return super().__call__(method, url, token, timeout, payload)


def gh_pull(number, state="MERGED", base="main"):
    return {"number": number, "state": state, "baseRefName": base, "mergeCommit": {"oid": MERGE} if state == "MERGED" else None}


def home(backlog: str):
    return bs.read_config(workspace(backlog)).backlog


class ChangeStatesTest(unittest.TestCase):
    """#45: `change_states(home, numbers, gh, call)` asks the Backlog's forge "ONLY for the named change numbers
    and only for what the audit prints": each one's state, base, and merge sha, "empty unless merged"."""

    GITLAB = "GitLab issues; host https://labs.example.test; project team/app"

    def gitlab(self, numbers, call):
        with mock.patch.dict(os.environ, {"CHIEF_OF_STUFF_GITLAB_TOKEN": "tok"}):
            return bs.change_states(home(self.GITLAB), numbers, None, call)

    def test_github_answers_each_pull_request_named(self):
        # "OPEN/MERGED/CLOSED lower-cased". A number that is not a pull request "is NOT an error from this
        # function, the number is simply absent from the dict."
        gh = FakePulls(gh_pull(12, base="release"), gh_pull(13, "OPEN"), gh_pull(14, "CLOSED"))
        got, error = bs.change_states(home("GitHub issues; repo o/app"), {12, 13, 14, 999}, gh, None)
        self.assertEqual((got, error), ({"#12": bs.ChangeState("#12", "merged", "release", MERGE),
                                         "#13": bs.ChangeState("#13", "open", "main", ""),
                                         "#14": bs.ChangeState("#14", "closed", "main", "")}, ""))
        self.assertEqual([c[:2] for c in gh.calls], [["api", "graphql"]])
        self.assertEqual(gh.asked, {12, 13, 14, 999})
        for asked in ("pullRequest(number: 12)", "mergeCommit", "baseRefName", '"o"', '"app"'):
            self.assertIn(asked, gh.queries[0])
        for unasked in ("issueOrPullRequest", "search(", "files", "reviewThreads", "statusCheckRollup"):
            self.assertNotIn(unasked, gh.queries[0])

    def test_github_any_other_failure_is_an_error(self):
        # "Any other error, or no `data`, is an error."
        cases = (("gh fails", {"fail": (1, "", "gh: not logged in")}, "not logged in"),
                 ("no data", {"fail": (0, json.dumps({"message": "Bad credentials"}), "")}, ""),
                 ("another error beside the data", {"errors": [{"type": "FORBIDDEN", "message": "Resource not accessible"}]},
                  "Resource not accessible"),
                 # Only NOT_FOUND at a requested pull request is not an error: any other kind there is one.
                 ("another error at a pull request named", {"errors": [
                     {"type": "FORBIDDEN", "path": ["repository", "n999"], "message": "Resource not accessible"}]}, "Resource not accessible"),
                 # NOT_FOUND on the repository itself is no clean, empty answer.
                 ("the repository is not there", {"missing_repo": True}, "Could not resolve to a Repository"))
        for what, answer, said in cases:
            with self.subTest(what):
                _, error = bs.change_states(home("GitHub issues; repo o/app"), {12, 999}, FakePulls(gh_pull(12), **answer), None)
                self.assertTrue(error)
                self.assertIn(said, error)

    def test_gitlab_answers_each_merge_request_named_in_one_post(self):
        # "GitLab `opened`→open, `locked`→closed". "GitLab merge sha is `mergeCommitSha` only": a fast-forward
        # merge answers null, and `diffHeadSha` "is a commit that never reached the base branch".
        # "A missing iid is simply absent from nodes (no error) and so absent from the dict."
        merged_at = f"{DAY}T06:00:00+00:00"
        call = StrictGitLab(mrs=[{**gl_mr(12, merged_at), "mergeCommitSha": MERGE, "targetBranch": "release"},
                               {**gl_mr(13), "mergeCommitSha": None},
                               {**gl_mr(14), "state": "locked", "mergeCommitSha": None},
                               {**gl_mr(15, merged_at), "mergeCommitSha": None},
                               {**gl_mr(16), "state": "closed", "mergeCommitSha": None}])
        got, error = self.gitlab({12, 13, 14, 15, 16, 99}, call)
        self.assertEqual((got, error), ({"!12": bs.ChangeState("!12", "merged", "release", MERGE),
                                         "!13": bs.ChangeState("!13", "open", "main", ""),
                                         "!14": bs.ChangeState("!14", "closed", "main", ""),
                                         "!15": bs.ChangeState("!15", "merged", "main", ""),
                                         "!16": bs.ChangeState("!16", "closed", "main", "")}, ""))
        self.assertEqual(call.posts, [("POST", "https://labs.example.test/api/graphql", "tok")])
        for asked in ('"team/app"', "mergeRequests(iids:", "targetBranch", "mergeCommitSha"):
            self.assertIn(asked, call.queries[0])
        # GitLab's MergeRequest has no `squashCommitSha`, and the query stays under its complexity cap.
        for unasked in ("squashCommitSha", "diffHeadSha", "issues", "mergedAfter", "jobs", "discussions", "diffStats"):
            self.assertNotIn(unasked, call.queries[0])

    def test_gitlab_a_project_the_token_cannot_see_is_an_error(self):
        # "A null project (token cannot see it) is an error, not an empty answer."
        got, error = self.gitlab({12}, StrictGitLab(mrs=[gl_mr(12)], hidden={"team/app"}))
        self.assertTrue(error)
        self.assertFalse(got)

    def test_gitlab_errors_beside_the_data_come_back_with_the_states(self):
        # "GitLab errors beside data: `change_states` returns the states AND the error."
        call = StrictGitLab(mrs=[gl_mr(12)], errors=[{"message": "Internal server error"}])
        got, error = self.gitlab({12, 99}, call)
        self.assertEqual((sorted(got), error), (["!12"], "Internal server error"))

    def test_a_field_the_forge_does_not_have_is_an_error_from_either_fake(self):
        # The fakes answer a field outside what the real forges answered as those forges do: an error, no data.
        code, out, err = FakePulls(gh_pull(12))(["api", "graphql", "-f", "query=query { repository(owner: \"o\", name: \"app\") "
                                                 "{ n12: pullRequest(number: 12) { number state mergeSha } } }"])
        self.assertEqual((code, "data" in json.loads(out)), (1, False))
        self.assertIn("mergeSha", err)
        body, _, _ = StrictGitLab(mrs=[gl_mr(12)])("POST", "", "tok", 1, {
            "query": 'query { project(fullPath: "team/app") { mergeRequests(iids: ["12"]) { nodes { iid squashCommitSha } } } }'})
        self.assertNotIn("data", body)
        self.assertIn("squashCommitSha", body["errors"][0]["message"])

    def test_gitlab_a_post_that_fails_is_an_error(self):
        _, error = self.gitlab({12}, lambda *a, **k: (None, {}, "timed out"))
        self.assertIn("timed out", error)

    def test_gitlab_no_token_is_the_error_the_board_gives(self):
        # "No token is the same error `gitlab()` gives."
        backlog = home(self.GITLAB)
        with mock.patch.object(bs.backlog, "token", return_value=""):
            _, error = bs.change_states(backlog, {12}, None, lambda *a, **k: self.fail("no call without a token"))
        self.assertEqual(error, backlog.missing_token)


class WorkersTest(unittest.TestCase):
    def test_tracker_sessions_and_live_one_shots(self):
        root = workspace("GitHub issues; repo o/app")
        tree = root / "trees" / "rate-limit" / ".chief-of-stuff"
        tree.mkdir(parents=True)
        (tree / "one-shot.pid").write_text(f"{os.getpid()} Rate limit headers, PR #58\n")
        # (#199) reviewer-2 owns an open task row: its task is that row's ref and name, never the
        # Sessions row's `doing` prose ("review of #58").
        text = TRACKER.replace(
            "| Search | Search pagination | Robin | waiting | 01:00 |  | S | {issue} | {mr} |\n",
            "| Search | Search pagination | Robin | waiting | 01:00 |  | S | {issue} | {mr} |\n"
            "| Review round | Review PR #58 | reviewer-2 | open | 01:00 |  | S | #119 |  |\n",
        ).replace("{issue}", "#115").replace("{mr}", "")
        (root / "daily" / f"{DAY}-tracker.md").write_text(text)
        got = bs.refresh(root, NOW, gh=FakeGh())
        one = next(w for w in got.workers if w.kind == "one-shot")
        # The task is named by its row's name, not the item the launch recorded (#182).
        self.assertEqual((one.name, one.runtime, one.model, one.task, one.tree, one.started.strftime("%H:%M")),
                         ("impl-1", "claude", "opus", "Rate limit", "rate-limit", "01:28"))
        session = next(w for w in got.workers if w.kind == "session")
        self.assertEqual(session.name, "reviewer-2")
        self.assertIn("Review round", session.task)
        self.assertNotIn("review of #58", session.task)
        self.assertEqual(session.last.strftime("%H:%M"), "01:58")

    def test_an_effort_is_not_part_of_the_model(self):
        # (#52) `started: <runtime> [<model>] effort=<level>`: the worker keeps its runtime and model (none when the
        # line names none), and the same name, task, tree and start time as when the line names no effort.
        for started, model in (("codex gpt-test effort=medium", "gpt-test"), ("codex effort=medium", "")):
            with self.subTest(started=started):
                root = workspace("GitHub issues; repo o/app")
                tree = root / "trees" / "rate-limit" / ".chief-of-stuff"
                tree.mkdir(parents=True)
                (tree / "one-shot.pid").write_text(f"{os.getpid()} Rate limit headers, PR #58\n")
                text = TRACKER.replace("started: claude opus,", f"started: {started},").replace("{issue}", "#115").replace("{mr}", "")
                (root / "daily" / f"{DAY}-tracker.md").write_text(text)
                one = next(w for w in bs.refresh(root, NOW, gh=FakeGh()).workers if w.kind == "one-shot")
                self.assertEqual((one.name, one.runtime, one.model, one.task, one.tree, one.started.strftime("%H:%M")),
                                 ("impl-1", "codex", model, "Rate limit", "rate-limit", "01:28"))


    def test_a_long_item_or_one_with_a_no_break_space_still_joins_its_log_line_and_row(self):
        # The board joins a live one-shot to its Log `started:` line and Tasks row by the task text the launcher
        # wrote, so `running_trees` hands it that text as written: not cut, not cleaned.
        for what, item in (("280 characters", "Rate limit headers, PR #58 " + "w" * 253),
                           ("no-break space", "Rate limit\u00a0headers, PR #58")):
            with self.subTest(what):
                root = workspace("GitHub issues; repo o/app")
                tree = root / "trees" / "rate-limit" / ".chief-of-stuff"
                tree.mkdir(parents=True)
                (tree / "one-shot.pid").write_text(f"{os.getpid()} {item}\n")
                text = TRACKER.replace("Rate limit headers, PR #58", item).replace("{issue}", "#115").replace("{mr}", "")
                (root / "daily" / f"{DAY}-tracker.md").write_text(text)
                one = next(w for w in bs.refresh(root, NOW, gh=FakeGh()).workers if w.kind == "one-shot")
                self.assertEqual((one.name, one.runtime, one.model, one.task, one.tree, one.started and one.started.strftime("%H:%M")),
                                 ("impl-1", "claude", "opus", "Rate limit", "rate-limit", "01:28"))


class LaunchedWorkerNameTest(unittest.TestCase):
    """A live one-shot is named by its task row even after the row's item stops matching its launch (#182)."""

    def setUp(self):
        root = workspace("GitHub issues; repo o/app")
        text = fc_test.launched_tracker()
        (root / "daily" / f"{DAY}-tracker.md").write_text(text)
        for _, prompt, _, tree, _, _ in fc_test.LAUNCHES:
            pid = root / "trees" / tree / ".chief-of-stuff" / "one-shot.pid"
            pid.parent.mkdir(parents=True)
            pid.write_text(f"{os.getpid()} {prompt}\n")
        cfg = parse_coordinator((root / "CLAUDE.md").read_text(), today=NOW.date())
        self.workers = bs.workers(root, cfg, rb.parse_tracker(text), text, NOW.date())
        self.by_tree = {w.tree: w for w in self.workers if w.kind == "one-shot"}
        self.html = workers_page.render(text, replace(bs.EMPTY, workers=self.workers), NOW, root / "trees", None)

    def test_the_workers_page_names_the_row_a_launch_maps_to(self):
        # Rules 1–2: a started line whose task begins with a row's item, or carries the row's issue ref, shows that
        # row's name; the prompt's later sentences appear nowhere.
        for tree, worker, name in (("upload", "impl-1", "Upload size limit"), ("keys", "impl-2", "Key rotation")):
            with self.subTest(task=name):
                self.assertEqual(self.by_tree[tree].name, worker)
                self.assertIn(name, self.html)
        for sentence in fc_test.PROMPT_SENTENCES:
            with self.subTest(sentence=sentence):
                self.assertNotIn(sentence, repr(self.workers))
                self.assertNotIn(sentence, self.html)

    def test_no_workers_fact_is_longer_than_its_rows_name(self):
        # Rule 4: no Workers fact is longer than the row name it maps to.
        for name, _, _, tree, _, after in fc_test.LAUNCHES:
            if after:
                with self.subTest(task=name):
                    self.assertLessEqual(len(self.by_tree[tree].task), len(name))

    def test_a_launch_with_no_row_keeps_a_short_name(self):
        # Rule 5: a started line that maps to no row is named by its first line, cut to a fixed length.
        task = self.by_tree["sweep"].task
        self.assertTrue(task)
        self.assertLessEqual(len(task), 80)
        self.assertTrue(fc_test.SWEEP_PROMPT.splitlines()[0].startswith(task), task)


class ManyLaunchedWorkersTest(unittest.TestCase):
    """The Workers panel names thousands of live one-shots by their rows within seconds (#184)."""

    N = 2000

    @classmethod
    def setUpClass(cls):
        root = workspace("GitHub issues; repo o/app")
        text, cls.expected = fc_test.many_launched_tracker(cls.N)
        (root / "daily" / f"{DAY}-tracker.md").write_text(text)
        cls.tree_of = {}
        for i, (name, (_, _, task)) in enumerate(cls.expected.items()):
            pid = root / "trees" / f"t{i}" / ".chief-of-stuff" / "one-shot.pid"
            pid.parent.mkdir(parents=True)
            pid.write_text(f"{os.getpid()} {task}\n")
            cls.tree_of[name] = f"t{i}"
        cfg = parse_coordinator((root / "CLAUDE.md").read_text(), today=NOW.date())
        tracker = rb.parse_tracker(text)
        start = time.perf_counter()
        cls.workers = bs.workers(root, cfg, tracker, text, NOW.date())
        cls.took = time.perf_counter() - start

    def test_the_workers_panel_builds_within_seconds(self):
        self.assertLess(self.took, 5, f"{self.N} live one-shots took {self.took:.1f}s")

    def test_every_live_one_shot_is_named_by_its_own_row(self):
        got = {w.tree: w.task for w in self.workers if w.kind == "one-shot"}
        self.assertEqual(got, {tree: name for name, tree in self.tree_of.items()})


class LoadTest(unittest.TestCase):
    def test_absent_or_unreadable_cache_is_empty(self):
        pages = Path(tempfile.mkdtemp())
        self.assertIs(bs.load(pages), bs.EMPTY)
        (pages / ".sources.json").write_text("{not json")
        self.assertIs(bs.load(pages), bs.EMPTY)

    def test_a_cache_from_before_milestones_reads_none(self):
        # #230: an older release wrote no `milestone`; a milestone with no due date round-trips as None.
        root = workspace("GitHub issues; repo o/app")
        no_due = {"title": "v2", "url": "https://github.com/o/app/milestone/3", "dueOn": None, "state": "CLOSED",
                  "issues": {"totalCount": 0}}
        got = bs.refresh(root, NOW, gh=FakeGh(n118={**gh_issue(118), "milestone": no_due}))
        self.assertEqual(bs.load(root / "pages"), got)
        self.assertEqual(got.issues["#118"].milestone.due, None)
        cache = root / "pages" / bs.CACHE
        old = json.loads(cache.read_text())
        for issue in old["issues"].values():
            issue.pop("milestone")
        cache.write_text(json.dumps(old))
        self.assertEqual({k: i.milestone for k, i in bs.load(root / "pages").issues.items()}, {"#118": None, "#115": None})

    def test_no_coordinator_block_never_raises(self):
        got = bs.refresh(Path(tempfile.mkdtemp()), NOW)
        self.assertIn("config", got.errors)

    def test_cli_prints_a_summary(self):
        root = workspace("GitHub issues; repo o/app")
        bs.refresh(root, NOW, gh=FakeGh())
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(bs.main(["--root", str(root)]), 0)
        self.assertIn("changes: 2", out.getvalue())


class CarriedStateKeepsItsHomeTest(unittest.TestCase):
    """#409 review: a refresh whose forge call fails carries the last good issues and changes forward. Their keys are
    relative to the Backlog that wrote them, so they keep that home, not the one the config names now."""

    def refreshed(self):
        root = workspace("GitHub issues; repo o/app")
        closed = {**gh_issue(115, ()), "state": "CLOSED", "closedAt": "2026-09-25T20:15:00Z"}
        first = bs.refresh(root, NOW, gh=FakeGh(n115=closed))
        self.assertEqual(first.issues["#115"].state, "closed")
        return root, first

    def test_a_failed_refresh_after_the_backlog_changes_keeps_the_old_home(self):
        root, _ = self.refreshed()
        claude = root / "CLAUDE.md"
        claude.write_text(claude.read_text().replace("repo o/app", "repo acme/web"))
        got = bs.refresh(root, NOW, gh=FakeGh(fail=True))
        self.assertEqual(got.issues["#115"].state, "closed")  # carried over
        self.assertEqual(got.home, bs.backlog.GitHubBacklog("o/app"))
        self.assertEqual(bs.load(root / "pages").home, bs.backlog.GitHubBacklog("o/app"))

    def test_a_cache_with_no_home_gains_none_through_a_failed_refresh(self):
        root, _ = self.refreshed()
        cache = root / "pages" / bs.CACHE
        old = json.loads(cache.read_text())
        old.pop("home")  # a cache written before the home was stored
        cache.write_text(json.dumps(old))
        got = bs.refresh(root, NOW, gh=FakeGh(fail=True))
        self.assertEqual(got.issues["o/app#115"].state, "closed")
        self.assertNotIn("#115", got.issues)
        self.assertIsNone(got.home)
        self.assertIsNone(bs.load(root / "pages").home)

    def test_a_good_refresh_stamps_the_current_home(self):
        root, _ = self.refreshed()
        claude = root / "CLAUDE.md"
        claude.write_text(claude.read_text().replace("repo o/app", "repo acme/web"))
        got = bs.refresh(root, NOW, gh=FakeGh())
        self.assertEqual(got.home, bs.backlog.GitHubBacklog("acme/web"))

    def test_page_view_uses_configured_identity_without_destroying_stale_cache(self):
        root, first = self.refreshed()
        claude = root / "CLAUDE.md"
        claude.write_text(claude.read_text().replace("repo o/app", "repo acme/web"))
        stale = bs.refresh(root, NOW, gh=FakeGh(fail=True))
        home = bs.backlog.GitHubBacklog("acme/web")
        view = bs.for_home(stale, home)
        self.assertEqual(view.home, home)
        self.assertEqual((view.issues, view.changes), ({}, {}))
        self.assertEqual((view.errors, view.fetched), (stale.errors, stale.fetched))
        self.assertEqual(bs.load(root / "pages").issues, first.issues)
        self.assertEqual(bs.for_home(stale, None).issues, {})
        self.assertEqual(bs.for_home(stale, None).changes, {})
        self.assertEqual(bs.for_home(stale, first.home).issues, first.issues)
        new_forge = FakeGh()
        def new_gh(args):
            code, out, err = new_forge(args)
            return code, out.replace("https://github.com/o/app/", "https://github.com/acme/web/"), err
        recovered = bs.refresh(root, NOW, gh=new_gh)
        self.assertEqual(bs.for_home(recovered, home).issues["#115"].state, "open")

    def test_one_failed_panel_after_home_change_is_not_carried(self):
        for failed in ("issues", "changes"):
            with self.subTest(failed=failed):
                root, _ = self.refreshed()
                claude = root / "CLAUDE.md"
                claude.write_text(claude.read_text().replace("repo o/app", "repo acme/web"))
                panels = (None, {}) if failed == "issues" else ({}, None)
                with mock.patch.object(bs, "forge", return_value=(*panels, {failed: "unavailable"})):
                    got = bs.refresh(root, NOW)
                self.assertEqual((got.issues, got.changes), ({}, {}))
                self.assertEqual(got.home, bs.backlog.GitHubBacklog("acme/web"))

    def test_credential_only_change_keeps_cached_facts(self):
        home = bs.backlog.Backlog("https://forge.example", "group/project", env="OLD")
        current = replace(home, env="NEW", account="other")
        cached = bs.Sources({}, {"#1": bs.Issue("#1", "https://forge.example/group/project/-/issues/1", "Old", "open", ())}, {}, (), {"kanban": "failed"}, home)
        view = bs.for_home(cached, current)
        self.assertEqual(view.issues, cached.issues)
        self.assertEqual(view.home, current)

    def test_board_issue_flow_and_stage_do_not_attach_old_home(self):
        root, _ = self.refreshed()
        claude = root / "CLAUDE.md"
        claude.write_text(claude.read_text().replace("repo o/app", "repo acme/web"))
        stale = bs.refresh(root, NOW, gh=FakeGh(fail=True))
        tracker = (root / "daily" / f"{DAY}-tracker.md").read_text().replace(
            "| size | issue |", "| size | lane | stage | issue |").replace(
            "| M | #118 |", "| M | build | implement | #118 |").replace(
            "| S | #115 |", "| S | build | implement | #115 |")
        cfg = parse_coordinator(claude.read_text(), today=NOW.date())
        lanes = {"build": st.Lane(("implement", "merge"), ())}
        with mock.patch.object(rb.flow_chart, "build", wraps=rb.flow_chart.build) as flow:
            board = rb.render(tracker, cfg, NOW, lanes=lanes, sources=stale)
        self.assertEqual(flow.call_args.kwargs["home"], cfg.backlog)
        self.assertEqual(flow.call_args.kwargs["ended"], {})
        self.assertNotIn("PR 41", board)
        self.assertIn("not logged in", board)
        page = ip.render(115, [(NOW.date(), tracker)], stale, NOW, lanes, cfg)
        self.assertNotIn("Issue 115", page)
        self.assertNotIn("PR 41", page)
        task = next(t for t in parse_tracker(tracker).tasks if t.issue == "#118")
        view = bs.for_home(stale, cfg.backlog)
        self.assertEqual(task_forge.effective_stage(task, view, lanes), "implement")

    def test_write_reports_changed_home_as_unavailable_without_old_fetch_times(self):
        root, first = self.refreshed()
        claude = root / "CLAUDE.md"
        claude.write_text(claude.read_text().replace("repo o/app", "repo acme/web"))
        for errors in ({}, {"kanban": "offline", "merge requests": "offline"}):
            with self.subTest(errors=errors):
                bs._write(root / "pages" / bs.CACHE, replace(first, errors=errors))
                html = rb.write(root, day=DAY, now=NOW)[0].read_text().split("</style>", 1)[1]
                self.assertIn("kanban: unavailable", html)
                self.assertIn("merge requests: unavailable", html)
                self.assertNotIn("kanban " + first.fetched["kanban"].astimezone(NOW.tzinfo).strftime("%H:%M"), html)
                self.assertNotIn("merge requests " + first.fetched["merge requests"].astimezone(NOW.tzinfo).strftime("%H:%M"), html)

    def test_rendering_follows_each_refresh_outcome(self):
        root = workspace("GitLab; host https://forge.example; project group/old; token env OLD")
        path = root / "daily" / f"{DAY}-tracker.md"
        tracker = ("## Tasks\n\n| name | item | owner | state | since | due | size | lane | stage | issue | checklist |\n"
                   "|---|---|---|---|---|---|---|---|---|---|---|\n"
                   "| Build | Build thing | Robin | running 01:00 | 01:00 | | S | build | implement | #7 | |\n"
                   "\n## Log\n\n- 01:00 stage: Build → implement\n")
        path.write_text(tracker)
        lanes = {"build": st.Lane(("implement", "merge"), ())}
        old_issue = bs.Issue("#7", "https://forge.example/group/old/-/issues/7", "Old issue", "closed", (), closed_at=NOW)
        old_change = bs.Change("!1", "https://forge.example/group/old/-/merge_requests/1", "Old change", "merged",
                               False, "passed", True, 1, NOW, "main", (), ("#7",))
        new_issue = replace(old_issue, url="https://forge.example/group/new/-/issues/7", title="New issue", state="open", closed_at=None)
        new_change = replace(old_change, url="https://forge.example/group/new/-/merge_requests/1", title="New change", state="open", merged_at=None)
        def refresh(issues, changes):
            errors = {name: "offline" for name, panel in (("kanban", issues), ("merge requests", changes)) if panel is None}
            with mock.patch.object(bs, "forge", return_value=(issues, changes, errors)):
                return bs.refresh(root, NOW)
        def check(got, title, merged):
            cfg = parse_coordinator((root / "CLAUDE.md").read_text(), today=NOW.date())
            with mock.patch.object(rb.flow_chart, "build", wraps=rb.flow_chart.build) as flow:
                board = rb.render(tracker, cfg, NOW, lanes=lanes, sources=got)
                board_call = flow.call_args
                page = ip.render(7, [(NOW.date(), tracker)], got, NOW, lanes, cfg)
                page_call = flow.call_args
            for call in (board_call, page_call):
                self.assertEqual(call.kwargs["home"], cfg.backlog)
                self.assertEqual(bool(call.kwargs["ended"]), merged)
            for candidate in ("Old issue", "New issue"):
                self.assertEqual(candidate in page, candidate == title)
            self.assertEqual("Old change" in board, title == "Old issue")
            view = bs.for_home(got, cfg.backlog)
            task = parse_tracker(tracker).tasks[0]
            self.assertEqual(task_forge.effective_stage(task, view, lanes), "merge" if merged else "implement")
            if got.errors:
                self.assertIn("offline", board)
        old = refresh({"#7": old_issue}, {"!1": old_change})
        check(old, "Old issue", True)
        claude = root / "CLAUDE.md"
        claude.write_text(claude.read_text().replace("token env OLD", "token env NEW"))
        check(refresh(None, None), "Old issue", True)
        claude.write_text(claude.read_text().replace("group/old", "group/new"))
        stale = refresh(None, None)
        check(stale, "", False)
        configured = claude.read_text()
        claude.write_text("\n".join(line for line in configured.splitlines() if "Backlog:" not in line) + "\n")
        check(stale, "", False)
        claude.write_text(configured)
        check(refresh({"#7": new_issue}, None), "New issue", False)
        check(refresh({"#7": new_issue}, {"!1": new_change}), "New issue", False)

    def test_source_page_does_not_read_or_render_old_home_change(self):
        from types import SimpleNamespace
        import source_page
        root, _ = self.refreshed()
        claude = root / "CLAUDE.md"
        claude.write_text(claude.read_text().replace("repo o/app", "repo acme/web"))
        bs.refresh(root, NOW, gh=FakeGh(fail=True))
        cfg = bs.read_config(root)
        tracker = (root / "daily" / f"{DAY}-tracker.md").read_text()
        with mock.patch.object(source_page, "inputs", return_value=(cfg, NOW, [(NOW.date(), tracker)], SimpleNamespace(graph=None))), \
             mock.patch.object(source_page, "notes") as notes:
            page = source_page.write(root, root / "pages", 118)
        notes.assert_not_called()
        self.assertNotIn("PR 58", page.read_text())


class IssueKeyTest(unittest.TestCase):
    """#409: the key a tracker's issue cell reads its forge state by names the host and project, not the number alone."""

    GITLAB = "https://example.com/o/app/-/issues/8"
    OTHER_PROJECT = "https://example.com/acme/web/-/issues/8"
    OTHER_HOST = "https://gitlab.example.org/o/app/-/issues/8"

    def test_the_same_number_in_another_project_or_host_is_another_key(self):
        cells = (self.GITLAB, self.OTHER_PROJECT, self.OTHER_HOST,
                 "https://github.com/o/app/issues/8", "https://github.com/acme/web/issues/8")
        self.assertEqual(len({issue_key(c) for c in cells}), len(cells))

    def test_one_issue_has_one_key(self):
        self.assertEqual(issue_key(self.GITLAB + "/"), issue_key(self.GITLAB))
        # a GitHub issue's URL and its `owner/repo#N` cell are the same issue
        self.assertEqual(issue_key("https://github.com/acme/web/issues/8"), issue_key("acme/web#8"))
        self.assertNotEqual(issue_key("o/app#8"), issue_key("acme/web#8"))

    def test_a_short_ref_with_no_project_keeps_its_short_key(self):
        self.assertEqual(issue_key("#8"), "#8")  # the Backlog's own project: how the sources file has always keyed it


class StoreWorkspaceSourcesTest(unittest.TestCase):
    """#996: store-backed workspaces fetch their store issues, recorded and historical merged change requests,
    linked closing issues (Development links and full URLs), and verified live branch URLs."""

    def make_store(self, root: Path):
        from store import Store, migrate

        (root / "daily" / f"{DAY}-tracker.md").write_text("")
        migrate(root / "store.db")
        db = Store(root / "store.db")
        self.addCleanup(db.engine.dispose)
        self.addCleanup(shutil.rmtree, root, True)
        return db

    def test_unreadable_store_is_reported_as_unknown_not_an_empty_backlog(self):
        from tracker import parse_tracker
        from workspace import read_config
        root = workspace("GitHub issues; repo o/app")
        self.addCleanup(shutil.rmtree, root, True)
        (root / "store.db").write_bytes(b"not a sqlite database")
        gh = mock.Mock()
        issues, changes, errors = bs.forge(root, read_config(root), parse_tracker(""), NOW, gh, mock.Mock())
        self.assertIsNone(issues)
        self.assertIsNone(changes)
        self.assertIn("store", errors["kanban"].lower())
        self.assertIn("store", errors["merge requests"].lower())
        gh.assert_not_called()

    def test_store_targets_handles_uri_characters_in_workspace_path(self):
        import backlog
        import sqlite3
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "workspace #1?"
            root.mkdir()
            db_path = root / "store.db"
            with sqlite3.connect(db_path) as db:
                db.execute("CREATE TABLE work_item (issue_key TEXT)")
                db.execute("CREATE TABLE resume (issue_key TEXT, data TEXT)")
                db.execute("INSERT INTO work_item VALUES (?)", ("github:o/app#92",))
            wanted, _ = bs._store_targets(db_path, backlog.GitHubBacklog("o/app"))
            self.assertEqual(wanted, {"o/app": {92}})

    def test_github_store_fetches_store_issues_recorded_merges_closing_refs_and_branches(self):
        root = workspace("GitHub issues; repo o/app")
        db = self.make_store(root)
        db.upsert_item("github:o/app#92", title="Open store task", stage="review")
        db.upsert_item("github:o/app#5", title="Merged days ago", stage="merge", forge_state="closed")
        db.set_resume("github:o/app#5", {"merges": [{"request": 33, "commit": MERGE}]})
        db.upsert_item("github:o/app#6", title="Merged yesterday without resume", stage="merge", forge_state="closed")
        db.upsert_item("github:acme/other#7", title="Cross-repo GitHub task", stage="implement")
        db.upsert_item("gitlab:labs.example.test/team/app#8", title="Foreign forge task", stage="implement")

        open_pr = {
            **gh_pr(140, state="OPEN"),
            "body": "Linked through GitHub Development sidebar only.",
            "headRefName": "feat/upload-limit",
            "headRef": {"name": "feat/upload-limit"},
            "headRepository": {"url": "https://github.com/o/app"},
            "closingIssuesReferences": {"nodes": [{"number": 92, "repository": {"name": "app", "owner": {"login": "o"}}}]},
        }
        recorded_merged = {
            **gh_pr(33, merged_at="2026-09-20T18:00:00Z", state="MERGED"),
            "body": "Closes https://github.com/o/app/issues/5",
            "headRefName": "feat/deleted-five",
            "headRef": None,  # deleted after merge: must never fabricate a /tree/ URL
            "headRepository": {"url": "https://github.com/o/app"},
        }
        recent_merged = {
            **gh_pr(34, merged_at="2026-09-25T15:00:00Z", state="MERGED"),
            "body": "Fixes #6",
            "headRefName": "feat/fork#6",
            "headRef": {"name": "feat/fork#6"},
            "headRepository": {"url": "https://github.com/forker/app"},
        }
        queries = []

        def gh(args, input_text=None):
            query = args[args.index("-f") + 1]
            queries.append(query)
            # Repos sorted alphabetically: r0 is acme/other, r1 is o/app.
            data = {
                "r0": {"n7": {**gh_issue(7), "url": "https://github.com/acme/other/issues/7"}},
                "r1": {
                    "n92": gh_issue(92),
                    "n5": {**gh_issue(5, ()), "state": "CLOSED", "closedAt": "2026-09-20T18:00:00Z"},
                    "n6": {**gh_issue(6, ()), "state": "CLOSED", "closedAt": "2026-09-25T15:00:00Z"},
                    "n33": recorded_merged,
                },
                "merged": {"nodes": [recent_merged], "pageInfo": {"hasNextPage": False, "endCursor": None}},
                "open": {"nodes": [open_pr], "pageInfo": {"hasNextPage": False, "endCursor": None}},
                "rateLimit": {"cost": 1, "remaining": 4900, "limit": 5000},
            }
            return 0, json.dumps({"data": data}), ""

        got = bs.refresh(root, NOW, gh=gh)
        self.assertEqual(got.errors, {})
        self.assertEqual(len(queries), 4)
        self.assertNotIn("team/app", " ".join(queries))
        self.assertNotIn("n8:", " ".join(queries))
        self.assertEqual(bs.load(root / "pages"), got)

        self.assertEqual(got.issues["#92"].url, "https://github.com/o/app/issues/92")
        self.assertEqual(got.issues["#5"].state, "closed")
        self.assertEqual(got.issues["acme/other#7"].url, "https://github.com/acme/other/issues/7")

        self.assertEqual(got.changes["#140"].issues, ("#92",))
        self.assertEqual(got.changes["#140"].branch, "feat/upload-limit")
        self.assertEqual(got.changes["#140"].branch_url, "https://github.com/o/app/tree/feat/upload-limit")

        self.assertEqual(got.changes["#33"].issues, ("#5",))
        self.assertEqual(got.changes["#33"].branch, "feat/deleted-five")
        self.assertEqual(got.changes["#33"].branch_url, "")

        self.assertEqual(got.changes["#34"].issues, ("#6",))
        self.assertEqual(got.changes["#34"].branch, "feat/fork#6")
        self.assertEqual(got.changes["#34"].branch_url, "https://github.com/forker/app/tree/feat/fork%236")

    def test_gitlab_cross_project_closing_refs_use_the_store_cache_key(self):
        home = bs.backlog.Backlog("https://labs.example.test", "team/app")
        mr = {**gl_mr(88), "description": "Fixes team/other#7 and https://labs.example.test/team/third/-/issues/8"}
        change = bs._gl_change(mr, "", home)
        self.assertEqual(change.issues, ("gitlab:labs.example.test/team/other#7", "gitlab:labs.example.test/team/third#8"))
        for key in ("gitlab:labs.example.test/team/other#7", "gitlab:labs.example.test/team/third#8"):
            self.assertIn(bs.store_label(key, home), change.issues)

    def test_gitlab_store_fetches_store_issues_recorded_merges_closing_forms_and_branches(self):
        root = workspace("GitLab issues; host https://labs.example.test; project team/app")
        db = self.make_store(root)
        db.upsert_item("gitlab:labs.example.test/team/app#92", title="Open GitLab task", stage="review")
        db.upsert_item("gitlab:labs.example.test/team/app#5", title="Merged days ago", stage="merge", forge_state="closed")
        db.set_resume("gitlab:labs.example.test/team/app#5", {"merges": [{"request": 12, "commit": MERGE}]})
        db.upsert_item("gitlab:labs.example.test/team/app#6", title="Merged yesterday", stage="merge", forge_state="closed")
        db.upsert_item("gitlab:other.example.org/team/app#99", title="Other GitLab host", stage="implement")
        db.set_resume("gitlab:other.example.org/team/app#99", {"merges": [{"request": 999}]})

        open_mr = {
            **gl_mr(88),
            "description": "Implements #92",
            "sourceBranch": "feat/gl-limit",
            "sourceBranchExists": True,
            "sourceProject": {"webUrl": "https://labs.example.test/team/app"},
        }
        recorded_merged = {
            **gl_mr(12, merged_at="2026-09-20T18:00:00+00:00"),
            "description": "Closing https://labs.example.test/team/app/-/issues/5",
            "sourceBranch": "feat/gl-deleted",
            "sourceBranchExists": False,  # deleted branch must not link
            "sourceProject": {"webUrl": "https://labs.example.test/team/app"},
        }
        recent_merged = {
            **gl_mr(13, merged_at="2026-09-25T15:00:00+00:00"),
            "description": "Fixes #6",
            "sourceBranch": "feat/gl-six",
            "sourceBranchExists": True,
            "sourceProject": None,  # deleted source fork must not link
        }
        fake = FakeGitLab(
            issues=[gl_issue(92), {**gl_issue(5), "state": "closed"}, {**gl_issue(6), "state": "closed"}],
            mrs=[open_mr, recorded_merged, recent_merged],
        )
        got = refresh_gitlab(root, NOW, fake)
        self.assertEqual(got.errors, {})
        for q in fake.queries:
            self.assertLessEqual(gl_score(q), GL_CAP, q)
            self.assertNotIn("other.example.org", q)
            self.assertNotIn('"999"', q)

        self.assertEqual(got.issues["#92"].url, "https://labs.example.test/team/app/-/issues/92")
        self.assertEqual(got.issues["#5"].state, "closed")
        self.assertNotIn("#99", got.issues)

        self.assertEqual(got.changes["!88"].issues, ("#92",))
        self.assertEqual(got.changes["!88"].branch, "feat/gl-limit")
        self.assertEqual(got.changes["!88"].branch_url, "https://labs.example.test/team/app/-/tree/feat/gl-limit")

        self.assertEqual(got.changes["!12"].issues, ("#5",))
        self.assertEqual(got.changes["!12"].branch, "feat/gl-deleted")
        self.assertEqual(got.changes["!12"].branch_url, "")

        self.assertEqual(got.changes["!13"].issues, ("#6",))
        self.assertEqual(got.changes["!13"].branch, "feat/gl-six")
        self.assertEqual(got.changes["!13"].branch_url, "")

    def test_unreliable_or_hostile_branch_inputs_produce_no_branch_url(self):
        for repo_url, branch in (
            ("javascript:alert(1)", "feat/x"),
            ("https://github.com/o/app", ""),
            ("https://github.com/o/app", "../settings"),
            ("https://github.com/o/app", "feat/../admin"),
            ("https://github.com/o/app", "feat//double"),
            ("https://github.com/o/app", "feat space"),
            ("https://github.com/o/app", "/leading"),
            ("https://github.com/o/app", "trailing.lock"),
        ):
            with self.subTest(repo_url=repo_url, branch=branch):
                pr = {
                    **gh_pr(58),
                    "headRefName": branch,
                    "headRef": {"name": branch} if branch else None,
                    "headRepository": {"url": repo_url},
                }
                got = bs._gh_change("#58", pr, "robin", "o/app")
                self.assertEqual(got.branch_url, "")


if __name__ == "__main__":
    unittest.main()
