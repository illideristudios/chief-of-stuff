"""`chief-of-stuff merge-ready` prints one verdict per open request, from the head pipeline, never the forge's merge status (#419).

The coordinator wrote "mergeable" from the forge's detailed merge status, which says only that there are no conflicts, so a red
pipeline and a request with open findings were both called mergeable. The command reads the head pipeline (id, status, the sha it
ran on), conflicts and unresolved review threads, and prints one verdict: ready | red | running | conflicts | findings open |
stale pipeline | no pipeline. It never merges and needs no approver or merge_owner setting.
"""

from __future__ import annotations

import dataclasses
import io
import json
import re
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "evals"))
sys.path.insert(0, str(ROOT / "scripts"))

import chief_of_stuff as cli  # noqa: E402
import forge_review as fr  # noqa: E402
import merge_approved as ma  # noqa: E402
import merge_ready as mr  # noqa: E402
import run  # noqa: E402
import test_merge_approved as tma  # noqa: E402  (module-qualified: don't re-collect its TestCases)

HEAD, OLD, OK = tma.HEAD, tma.OLD, tma.OK
RED = [{"__typename": "CheckRun", "status": "COMPLETED", "conclusion": "FAILURE"}]
RUNNING = [{"__typename": "CheckRun", "status": "IN_PROGRESS", "conclusion": ""}]

# The one line per request, as merge-ready prints it. A request with no pipeline says `pipeline none`. `unverified` and `blocked`
# carry one clause in parentheses (#419 review): `unverified (threads cut off at 100)`, `blocked (BEHIND)`; `draft` carries none.
# What was not read prints `unread`: the pipeline's sha (`on unread`) and the thread count (`unresolved threads unread`).
LINE = re.compile(
    r"^(?P<ref>[#!]\d+) (?P<verdict>ready|red|running|conflicts|findings open|stale pipeline|no pipeline|draft|unverified|blocked)"
    r"(?: \((?P<why>[^()]+)\))?"
    r" · head (?P<head>[0-9a-f]{7})"
    r" · pipeline (?:(?P<pid>\S+) (?P<status>\S+) on (?P<psha>[0-9a-f]{7}|unread)|none)"
    r" · conflicts (?P<conflicts>yes|no)"
    r" · unresolved threads (?P<threads>\d+|unread)"
    r" · (?P<url>\S+)$")


class FailsAt:
    """Answers as `inner` does, except the nth call (1-based), which answers `failure`. A fake that fails only the first call
    never reaches the later reads (the #419 review's R1: the graphql failure was unpinned for that reason)."""

    def __init__(self, inner, n, failure):
        self.inner, self.n, self.failure, self.count = inner, n, failure, 0

    def __call__(self, *args, **kwargs):
        self.count += 1
        return self.failure if self.count == self.n else self.inner(*args, **kwargs)


def gpr(number, checks=OK, mergeable="MERGEABLE", draft=False, state="CLEAN"):
    return tma.pr(number, [], checks=checks, mergeable=mergeable, draft=draft, state=state)


def without(row, key):
    return {k: v for k, v in row.items() if k != key}


# number -> (the pull request, the graphql node, the verdict)
GITHUB = {
    21: (gpr(21), tma.graphql_node(21), "ready"),
    22: (gpr(22, RED), tma.graphql_node(22), "red"),
    23: (gpr(23, RUNNING), tma.graphql_node(23), "running"),
    24: (gpr(24, mergeable="CONFLICTING"), tma.graphql_node(24), "conflicts"),
    25: (gpr(25, RED, "CONFLICTING"), tma.graphql_node(25, [False]), "conflicts"),
    26: (gpr(26), tma.graphql_node(26, sha=OLD), "stale pipeline"),
    27: (gpr(27, []), tma.graphql_node(27, run=False), "no pipeline"),
    28: (gpr(28), tma.graphql_node(28, [False, True, False]), "findings open"),
    29: (gpr(29, RED), tma.graphql_node(29, [False, False], sha=OLD), "red"),
    30: (gpr(30, RUNNING), tma.graphql_node(30, [False]), "running"),
    31: (gpr(31, []), tma.graphql_node(31, [False], run=False), "no pipeline"),
    32: (gpr(32), tma.graphql_node(32, [False], sha=OLD), "stale pipeline"),
    33: (gpr(33, mergeable="UNKNOWN"), tma.graphql_node(33), "running"),
    34: (gpr(34), tma.graphql_node(34, [True, True]), "ready"),
    # #419 review: a draft, a branch the forge will not merge, and threads that were cut off are never ready (R2, R5, R6)
    35: (gpr(35, draft=True), tma.graphql_node(35), "draft"),
    36: (gpr(36, state="BEHIND"), tma.graphql_node(36), "blocked"),
    37: (gpr(37, RED, draft=True), tma.graphql_node(37, [False]), "draft"),
    38: (gpr(38, state="BLOCKED"), tma.graphql_node(38, [False]), "findings open"),  # #430: open threads beat blocked
    39: (gpr(39), tma.graphql_node(39, [True] * 100, more=True), "unverified"),
    40: (gpr(40, mergeable="CONFLICTING", draft=True), tma.graphql_node(40), "conflicts"),
    41: (gpr(41, state="UNKNOWN"), tma.graphql_node(41), "blocked"),
    # #419 second review: no mergeStateStatus at all is UNKNOWN (R3); a draft seen by one read only is a draft (R5); a thread
    # read that says more follow and carries none is cut off (R6)
    42: (without(gpr(42), "mergeStateStatus"), tma.graphql_node(42), "blocked"),
    43: ({**gpr(43), "mergeStateStatus": None}, tma.graphql_node(43), "blocked"),
    44: (gpr(44), {**tma.graphql_node(44), "isDraft": True}, "draft"),
    45: (gpr(45, draft=True), {**tma.graphql_node(45), "isDraft": False}, "draft"),
    46: (gpr(46), tma.graphql_node(46, [], more=True), "unverified"),
    48: (gpr(48, state="BLOCKED"), tma.graphql_node(48), "blocked"),  # #430: BLOCKED with no threads stays blocked
}


def github_gh(numbers=None):
    keep = [GITHUB[n] for n in (numbers or GITHUB)]
    return tma.FakeGhGraphql([p for p, _, _ in keep], [n for _, n, _ in keep])


class GitHubReadyTest(unittest.TestCase):
    def test_graphql_query_has_balanced_braces(self):
        # The response fakes never parse QUERY; catch unmatched braces before calling GitHub.
        depth = 0
        for offset, char in enumerate(mr.QUERY):
            if char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                self.assertGreaterEqual(depth, 0, f"unmatched closing brace at offset {offset}")
        self.assertEqual(depth, 0, "unclosed GraphQL selection set")

    def test_every_verdict_in_the_precedence_list(self):
        got = {r.number: r.verdict for r in mr.github_ready("o/app", github_gh())}
        self.assertEqual(got, {n: v for n, (_, _, v) in GITHUB.items()})

    def test_the_request_carries_the_head_pipeline_id_and_the_sha_it_ran_on(self):
        by = {r.number: r for r in mr.github_ready("o/app", github_gh())}
        self.assertEqual((by[21].head, by[21].pipeline_id, by[21].pipeline_sha, by[21].pipeline), (HEAD, "9021", HEAD, "passed"))
        self.assertEqual((by[22].pipeline_id, by[22].pipeline), ("9022", "failed"))
        self.assertEqual((by[26].head, by[26].pipeline_sha), (HEAD, OLD), "a green pipeline on an old sha is stale")
        # R4: the sha is the last commit's oid whenever that commit exists, even when it has no checks yet
        self.assertEqual((by[27].pipeline, by[27].pipeline_id, by[27].pipeline_sha), ("none", "", HEAD))

    def test_only_unresolved_threads_count(self):
        by = {r.number: r.threads for r in mr.github_ready("o/app", github_gh())}
        self.assertEqual((by[21], by[28], by[34]), (0, 2, 0))

    def test_a_pipeline_that_passed_on_an_old_sha_is_never_ready(self):
        by = {r.number: r for r in mr.github_ready("o/app", github_gh([26]))}
        self.assertEqual(by[26].pipeline, "passed")
        self.assertEqual(by[26].verdict, "stale pipeline")

    def test_a_failed_read_raises_so_the_command_can_say_so(self):
        class Down:
            def __call__(self, args, input_text=None):
                return 1, "", "HTTP 502"
        with self.assertRaises(RuntimeError):
            mr.github_ready("o/app", Down())

    def test_a_read_that_fails_at_any_call_raises_and_never_hands_back_a_request(self):
        # R1(a): both Down fakes of the first version failed the first call, so the graphql failure (the second) was never reached.
        for n in (1, 2):
            with self.subTest(failing_call=n):
                with self.assertRaises(RuntimeError):
                    mr.github_ready("o/app", FailsAt(github_gh([21]), n, (1, "", "HTTP 502")))

    def test_a_pull_request_missing_from_the_graphql_read_is_never_ready(self):
        # R1(b): with the node missing, `continue` dropped the request and an empty default node printed it ready.
        gh = tma.FakeGhGraphql([GITHUB[21][0], GITHUB[22][0]], [GITHUB[22][1]], only_given=True)
        try:
            got = {r.number: r.verdict for r in mr.github_ready("o/app", gh)}
        except RuntimeError:
            return
        self.assertNotEqual(got.get(21), "ready", got)
        self.assertIn(21, got, "a request the forge listed must not vanish from the output")

    def test_a_graphql_answer_that_carries_errors_is_a_failed_read(self):
        class Partial(tma.FakeGhGraphql):
            def __call__(self, args, input_text=None):
                code, out, err = super().__call__(args, input_text)
                if args[:2] == ["api", "graphql"]:
                    out = json.dumps({**json.loads(out), "errors": [{"message": "RATE_LIMITED"}]})
                return code, out, err
        with self.assertRaises(RuntimeError):
            mr.github_ready("o/app", Partial([GITHUB[21][0]], [GITHUB[21][1]]))

    def test_threads_that_were_cut_off_at_100_are_unverified_never_ready(self):
        # R2: reviewThreads(first: 100) with 100 resolved and more behind them read as zero unresolved.
        by = {r.number: r for r in mr.github_ready("o/app", github_gh([34, 39]))}
        self.assertEqual(by[39].verdict, "unverified")
        self.assertEqual(LINE.match(mr.line(by[39], "#"))["why"], "threads cut off at 100")
        self.assertTrue(mr.line(by[39], "#").startswith("#39 unverified (threads cut off at 100) · head "), mr.line(by[39], "#"))
        # a full page with nothing behind it is a complete read
        full = tma.FakeGhGraphql([GITHUB[21][0]], [tma.graphql_node(21, [True] * 100, more=False)])
        self.assertEqual(mr.github_ready("o/app", full)[0].verdict, "ready")

    def test_the_head_that_moved_between_the_two_reads_is_never_ready_even_with_no_checks_on_the_new_head(self):
        # R4: the list says head A with passing checks; the last commit is B and has no statusCheckRollup yet. The sha was set to ""
        # for that commit, and "" read as "never stale".
        gh = tma.FakeGhGraphql([GITHUB[21][0]], [tma.graphql_node(21, sha=OLD, run=False)])
        (r,) = mr.github_ready("o/app", gh)
        self.assertEqual(r.pipeline_sha, OLD, "the sha is the last commit's oid whenever the commit exists")
        self.assertIn(r.verdict, ("stale pipeline", "unverified"))
        self.assertNotEqual(r.verdict, "ready")

    def test_the_read_asks_for_the_cut_off_the_draft_and_the_merge_state_and_passes_owner_and_name_as_strings(self):
        gh = github_gh([21])
        mr.github_ready("o/app", gh)
        listed = next(c for c in gh.calls if c[:2] == ["pr", "list"])
        fields = listed[listed.index("--json") + 1].split(",")
        self.assertIn("isDraft", fields)
        self.assertIn("mergeStateStatus", fields)
        graph = next(c for c in gh.calls if c[:2] == ["api", "graphql"])
        query = next(a for a in graph if a.startswith("query="))
        for word in ("pageInfo", "hasNextPage", "isDraft"):
            self.assertIn(word, query)
        # R12(c): -F types its value (a digits-only owner becomes a number, `@path` reads a file); -f is the string form.
        # cursor uses -F null on the first page; owner and name stay -f strings.
        self.assertEqual(graph[graph.index("cursor=null") - 1], "-F")
        for pair in ("owner=o", "name=app"):
            self.assertEqual(graph[graph.index(pair) - 1], "-f", pair)

    def test_a_draft_a_blocked_branch_and_a_check_that_is_not_a_pass_are_never_ready(self):
        by = {r.number: r.verdict for r in mr.github_ready("o/app", github_gh())}
        self.assertEqual((by[35], by[37], by[40]), ("draft", "draft", "conflicts"))
        self.assertEqual((by[36], by[38], by[41], by[48]), ("blocked", "findings open", "blocked", "blocked"))
        self.assertEqual(by[21], "ready", "CLEAN with a passing check stays ready")
        for conclusion in ("STALE", ""):
            check = [{"__typename": "CheckRun", "status": "COMPLETED", "conclusion": conclusion}]
            with self.subTest(conclusion=conclusion):
                (r,) = mr.github_ready("o/app", tma.FakeGhGraphql([gpr(50, check)], [tma.graphql_node(50)]))
                self.assertNotEqual(r.verdict, "ready")
                self.assertNotEqual(r.pipeline, "passed")

    def test_the_check_conclusions_that_are_a_pass_stay_a_pass(self):
        # R6 must not over-correct: NEUTRAL and SKIPPED are fine; STALE and an empty conclusion on a completed run are not.
        for conclusion, want in (("SUCCESS", True), ("NEUTRAL", True), ("SKIPPED", True), ("STALE", False), ("", False),
                                 ("FAILURE", False), ("CANCELLED", False)):
            with self.subTest(conclusion=conclusion):
                got = fr.github_pipeline([{"__typename": "CheckRun", "status": "COMPLETED", "conclusion": conclusion}])
                self.assertEqual(got == "passed", want, got)
        self.assertEqual(fr.github_pipeline([{"__typename": "CheckRun", "status": "IN_PROGRESS", "conclusion": ""}]), "running")

    def test_a_pull_request_with_no_merge_state_is_blocked_as_unknown_and_never_ready(self):
        # second review R3: absent and null both read as UNKNOWN; defaulting to CLEAN would print `ready`.
        by = {r.number: r for r in mr.github_ready("o/app", github_gh([42, 43]))}
        for n in (42, 43):
            with self.subTest(n=n):
                self.assertEqual((by[n].verdict, by[n].merge_state), ("blocked", "UNKNOWN"))
                self.assertTrue(mr.line(by[n], "#").startswith(f"#{n} blocked (UNKNOWN) · head "), mr.line(by[n], "#"))

    def test_a_draft_seen_by_only_one_of_the_two_reads_is_a_draft(self):
        # second review R5: the list's isDraft and the graphql node's isDraft are each enough; the fake gives the node only what the
        # query asks for, so the graphql-only draft is not the list's copied across.
        gh = github_gh([44, 45])
        by = {r.number: r.verdict for r in mr.github_ready("o/app", gh)}
        self.assertEqual(by, {44: "draft", 45: "draft"})
        query = next(a for c in gh.calls if c[:2] == ["api", "graphql"] for a in c if a.startswith("query="))
        self.assertIn("isDraft", query)
        self.assertNotIn("mergeStateStatus", query, "the fake answers only what the query asks for; merge state comes from the list")

    def test_a_cut_off_thread_read_that_carries_no_thread_is_still_unverified(self):
        # second review R6(a): the count of threads read doubled as the cut-off flag, so `hasNextPage` with zero nodes was `ready`.
        (r,) = mr.github_ready("o/app", github_gh([46]))
        self.assertEqual(r.verdict, "unverified")
        self.assertEqual(LINE.match(mr.line(r, "#"))["why"], "threads cut off at 100")

    def test_the_list_asks_for_every_page_up_to_the_shared_cap(self):
        # #425: --limit 100 dropped older open requests; use the backlog's full-read cap and refuse a full one.
        gh = github_gh([21])
        mr.github_ready("o/app", gh)
        listed = next(c for c in gh.calls if c[:2] == ["pr", "list"])
        self.assertEqual(listed[listed.index("--limit") + 1], str(ma.backlog.GH_LIMIT))

    def test_graphql_pages_open_pull_requests_until_a_short_page(self):
        # #425: one graphql page of 100 left the rest of the list blind.
        class Paging(tma.FakeGhGraphql):
            def __call__(self, args, input_text=None):
                if args[:2] != ["api", "graphql"]:
                    return super().__call__(args, input_text)
                self.calls.append(args)
                cursor = next((a.split("=", 1)[1] for a in args if a.startswith("cursor=")), "null")
                def node(n):
                    return {**tma.graphql_node(n), "isDraft": False}
                if cursor == "null":
                    nodes = [node(n) for n in range(1, 101)]
                    return 0, json.dumps({"data": {"repository": {"pullRequests": {
                        "pageInfo": {"hasNextPage": True, "endCursor": "c1"}, "nodes": nodes}}}}), ""
                if cursor != "c1":
                    return 1, "", f"unexpected cursor {cursor!r}"
                return 0, json.dumps({"data": {"repository": {"pullRequests": {
                    "pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": [node(101)]}}}}), ""

        prs = [gpr(n) for n in range(1, 102)]
        got = mr.github_ready("o/app", Paging(prs))
        self.assertEqual(sorted(r.number for r in got), list(range(1, 102)))


ABSENT = object()  # a head_pipeline with no `sha` key at all (None is a `sha` that is null)


def gitlab_fake(mrs, discussions_error=""):
    """The merge request, approvals and discussions endpoints; `mrs` maps iid to its spec. Discussions are paged as GitLab
    pages them: `page` and `per_page` select a slice, so a reader that asks once sees only the first page. `call.urls` lists the
    discussions urls asked, in order. `discussions_error` makes that endpoint answer an error."""
    urls = []

    def call(method, url, token, timeout, payload=None):
        path = url.split("/api/v4/projects/team%2Fapp", 1)[1]
        if path.startswith("/merge_requests?") or path == "/merge_requests":
            query = parse_qs(urlsplit(url).query)
            size, page = int(query.get("per_page", ["100"])[0]), int(query.get("page", ["1"])[0])
            iids = sorted(mrs)
            return [{"iid": i} for i in iids[(page - 1) * size:page * size]], {}, ""
        iid = int(path.split("/")[2].split("?")[0])
        m = mrs[iid]
        if path.split("?", 1)[0].endswith("/approvals"):
            return {"approved_by": []}, {}, ""
        if "/discussions" in path:
            urls.append(path)
            if discussions_error:
                return None, {}, discussions_error
            query = parse_qs(urlsplit(path).query)
            size, page = int(query.get("per_page", ["20"])[0]), int(query.get("page", ["1"])[0])
            return m.get("discussions", [])[(page - 1) * size:page * size], {}, ""
        pipeline = m.get("pipeline", "success")
        head = None if pipeline is None else {"id": 7000 + iid, "status": pipeline}
        if head is not None and m.get("pipeline_sha", HEAD) is not ABSENT:
            head["sha"] = m.get("pipeline_sha", HEAD)
        if head is not None:
            head["ref"] = m.get("pipeline_ref", "feature")
        return ({"iid": iid, "title": "MR", "web_url": f"https://labs.example.test/team/app/-/merge_requests/{iid}", "sha": HEAD,
                 "has_conflicts": m.get("conflict", False), "detailed_merge_status": m.get("status", "mergeable"),
                 "draft": m.get("draft", False), "merge_commit_sha": m.get("merge_commit_sha"),
                 "head_pipeline": head}, {}, "")
    call.urls = urls
    return call


def thread(resolved=False, resolvable=True, system=False):
    return {"id": f"d{id(object())}", "notes": [{"resolvable": resolvable, "resolved": resolved, "system": system}]}


def discussion(*notes):
    """One discussion holding several notes, as a diff discussion does: a system note ("changed this line in version 2") beside
    the resolvable note, in either order. A note is `(resolved, resolvable, system)`."""
    return {"id": f"d{id(notes)}", "notes": [{"resolved": r, "resolvable": rv, "system": sy} for r, rv, sy in notes]}


SYSTEM, UNRESOLVED, RESOLVED = (False, False, True), (False, True, False), (True, True, False)

GITLAB = {
    5: ({}, "ready"),
    6: ({"pipeline": "failed"}, "red"),
    7: ({"pipeline": "running"}, "running"),
    8: ({"conflict": True, "status": "conflict"}, "conflicts"),
    9: ({"pipeline_sha": OLD}, "stale pipeline"),
    10: ({"pipeline": None}, "no pipeline"),
    11: ({"discussions": [thread(), thread(), thread(resolved=True)]}, "findings open"),
    12: ({"status": "checking"}, "running"),
    13: ({"pipeline": "failed", "discussions": [thread()]}, "red"),
    14: ({"discussions": [thread(resolvable=False), thread(system=True)]}, "ready"),
    # #419 review: a draft, a canceled pipeline, a head pipeline whose sha was not given, and a thread on the second page
    15: ({"draft": True, "status": "draft_status"}, "draft"),
    16: ({"pipeline_sha": ABSENT}, "unverified"),
    17: ({"pipeline_sha": None}, "unverified"),
    18: ({"pipeline": "canceled"}, "red"),
    19: ({"discussions": [thread(resolved=True) for _ in range(100)] + [thread()]}, "findings open"),
    20: ({"draft": True, "pipeline": "failed", "status": "draft_status"}, "draft"),
    21: ({"discussions": [thread(resolved=True) for _ in range(100)]}, "ready"),
    # #419 second review R4: a discussion's system note beside its resolvable note, in either order
    22: ({"discussions": [discussion(SYSTEM, UNRESOLVED)]}, "findings open"),
    23: ({"discussions": [discussion(RESOLVED, SYSTEM)]}, "ready"),
    24: ({"discussions": [discussion(UNRESOLVED, SYSTEM)]}, "findings open"),
    # #430: settled non-mergeable statuses name themselves; merged-results pipeline sha is not stale
    25: ({"status": "discussions_not_resolved"}, "blocked"),
    26: ({"pipeline_sha": "c" * 40, "pipeline_ref": "refs/merge-requests/26/merge", "merge_commit_sha": None}, "ready"),
    # #425: waiting / skipped pipelines name their real state (not a coarse "running" with no why)
    27: ({"pipeline": "skipped"}, "running"),
    28: ({"pipeline": "manual"}, "running"),
    29: ({"pipeline": "created"}, "running"),
    30: ({"pipeline": "scheduled"}, "running"),
    31: ({"pipeline": "draft"}, "running"),
}


class GitLabReadyTest(unittest.TestCase):
    HOME = ma.backlog.Backlog("https://labs.example.test", "team/app")

    def read(self, numbers=None):
        mrs = {n: GITLAB[n][0] for n in (numbers or GITLAB)}
        return {r.number: r for r in mr.gitlab_ready(self.HOME, "team/app", "t", gitlab_fake(mrs))}

    def test_every_verdict_in_the_precedence_list(self):
        self.assertEqual({n: r.verdict for n, r in self.read().items()}, {n: v for n, (_, v) in GITLAB.items()})

    def test_the_request_carries_the_head_pipeline_id_and_the_sha_it_ran_on(self):
        got = self.read()
        self.assertEqual((got[5].head, got[5].pipeline_id, got[5].pipeline_sha, got[5].pipeline), (HEAD, "7005", HEAD, "passed"))
        self.assertEqual((got[9].pipeline_id, got[9].pipeline_sha), ("7009", OLD))
        self.assertEqual((got[10].pipeline, got[10].pipeline_id, got[10].pipeline_sha), ("none", "", ""))

    def test_only_unresolved_resolvable_threads_count(self):
        got = self.read()
        self.assertEqual((got[5].threads, got[11].threads, got[14].threads), (0, 2, 0))

    def test_discussions_are_paged_until_a_short_page(self):
        # R3: one page of 100 read, 100 resolved discussions, an unresolved one on page 2 read as zero.
        call = gitlab_fake({19: GITLAB[19][0]})
        (r,) = mr.gitlab_ready(self.HOME, "team/app", "t", call)
        self.assertEqual((r.threads, r.verdict), (1, "findings open"))
        pages = [parse_qs(urlsplit(u).query).get("page", ["1"])[0] for u in call.urls]
        self.assertEqual(pages, ["1", "2"], "page 2 is short (1 of 100): stop there")

    def test_a_full_last_page_is_followed_by_one_more_that_comes_back_empty(self):
        call = gitlab_fake({21: GITLAB[21][0]})
        (r,) = mr.gitlab_ready(self.HOME, "team/app", "t", call)
        self.assertEqual((r.threads, r.verdict), (0, "ready"))
        self.assertEqual([parse_qs(urlsplit(u).query).get("page", ["1"])[0] for u in call.urls], ["1", "2"])

    def test_a_discussion_counts_once_if_any_of_its_notes_is_an_unresolved_resolvable_one_whatever_the_system_notes(self):
        # second review R4: every other thread has one note, so any() was never told from all(), first-note-only, or last-note-only.
        got = self.read([22, 23, 24])
        self.assertEqual({n: r.threads for n, r in got.items()}, {22: 1, 23: 0, 24: 1})
        mixed = gitlab_fake({5: {"discussions": [discussion(SYSTEM, UNRESOLVED), discussion(RESOLVED, SYSTEM), thread()]}})
        (r,) = mr.gitlab_ready(self.HOME, "team/app", "t", mixed)
        self.assertEqual((r.threads, r.verdict), (2, "findings open"))

    def test_more_than_max_pages_of_discussions_is_an_error_and_never_ready(self):
        # second review R2: replacing the raise with `pass` proceeded with MAX_PAGES full pages read and printed `ready`.
        many = {"discussions": [thread(resolved=True)] * (ma.backlog.MAX_PAGES * ma.backlog.PER_PAGE + 1)}
        with self.assertRaises(RuntimeError):
            mr.gitlab_ready(self.HOME, "team/app", "t", gitlab_fake({5: many}))

    def test_a_canceled_pipeline_is_red_and_never_ready(self):
        # R1(d): forge_review's GITLAB_PIPELINE maps canceled to failed; "canceled" mapped to "passed" survived every test.
        (r,) = mr.gitlab_ready(self.HOME, "team/app", "t", gitlab_fake({18: GITLAB[18][0]}))
        self.assertEqual((r.pipeline, r.verdict), ("failed", "red"))

    def test_a_draft_is_a_draft_whatever_the_pipeline_says(self):
        got = self.read([15, 20])
        self.assertEqual((got[15].verdict, got[20].verdict), ("draft", "draft"))

    def test_a_head_pipeline_with_no_sha_is_unverified_and_never_ready(self):
        # R7: a missing or null sha read as "" and "" read as "not stale", so a pipeline on an unknown commit said ready.
        got = self.read([16, 17])
        for n in (16, 17):
            with self.subTest(n=n):
                self.assertIsNone(got[n].pipeline_sha)
                self.assertEqual(got[n].verdict, "unverified")
                why = LINE.match(mr.line(got[n], "!"))["why"]
                self.assertRegex(why, r"(?i)sha")

    def test_a_settled_non_mergeable_status_is_blocked_not_running(self):
        # #430: detailed_merge_status like discussions_not_resolved used to print running.
        (r,) = mr.gitlab_ready(self.HOME, "team/app", "t", gitlab_fake({25: GITLAB[25][0]}))
        self.assertEqual(r.verdict, "blocked")
        self.assertEqual(LINE.match(mr.line(r, "!"))["why"], "discussions_not_resolved")

    def test_a_merged_results_pipeline_sha_is_not_stale(self):
        # An opened MR has no merge_commit_sha; its merged-results pipeline runs on the temporary merge ref.
        (r,) = mr.gitlab_ready(self.HOME, "team/app", "t", gitlab_fake({26: GITLAB[26][0]}))
        self.assertEqual((r.pipeline, r.pipeline_sha, r.verdict), ("passed", HEAD, "ready"))

    def test_only_this_merge_requests_merge_ref_can_normalize_a_pipeline_sha(self):
        for ref in (None, "feature", "refs/merge-requests/26/head", "refs/merge-requests/27/merge"):
            with self.subTest(ref=ref):
                spec = {"pipeline_sha": OLD, "pipeline_ref": ref, "merge_commit_sha": OLD}
                (r,) = mr.gitlab_ready(self.HOME, "team/app", "t", gitlab_fake({26: spec}))
                self.assertEqual((r.pipeline_sha, r.verdict), (OLD, "stale pipeline"))

    def test_a_merged_results_pipeline_with_an_unread_sha_stays_unverified(self):
        for sha in (ABSENT, None):
            with self.subTest(sha=sha):
                spec = {"pipeline_sha": sha, "pipeline_ref": "refs/merge-requests/26/merge"}
                (r,) = mr.gitlab_ready(self.HOME, "team/app", "t", gitlab_fake({26: spec}))
                self.assertIsNone(r.pipeline_sha)
                self.assertEqual(r.verdict, "unverified")

    def test_transient_merge_statuses_are_running_and_name_themselves(self):
        for status in ("approvals_syncing", "preparing"):
            with self.subTest(status=status):
                (r,) = mr.gitlab_ready(self.HOME, "team/app", "t", gitlab_fake({5: {"status": status}}))
                self.assertEqual((r.pipeline, r.verdict), ("passed", "running"))
                self.assertEqual(LINE.match(mr.line(r, "!"))["why"], status)

    def test_callback_and_canceling_pipelines_preserve_their_status_and_reason(self):
        for status in ("waiting_for_callback", "canceling"):
            with self.subTest(status=status):
                (r,) = mr.gitlab_ready(self.HOME, "team/app", "t", gitlab_fake({5: {"pipeline": status}}))
                self.assertEqual(r.verdict, "running")
                self.assertEqual(r.pipeline, status)
                printed = LINE.match(mr.line(r, "!"))
                self.assertEqual((printed["status"], printed["why"]), (status, status))

    def test_waiting_and_skipped_pipelines_name_themselves(self):
        # #425: skipped/manual/created/scheduled/draft all printed bare running.
        got = self.read([27, 28, 29, 30, 31])
        for n, status in ((27, "skipped"), (28, "manual"), (29, "created"), (30, "scheduled"), (31, "draft")):
            with self.subTest(status=status):
                self.assertEqual(got[n].verdict, "running")
                self.assertEqual(LINE.match(mr.line(got[n], "!"))["why"], status)
                self.assertEqual(LINE.match(mr.line(got[n], "!"))["status"], status)

    def test_every_page_of_open_merge_requests_is_read(self):
        # #425: more than one page used to be dropped silently.
        mrs = {n: {} for n in range(1, ma.backlog.PER_PAGE + 2)}
        call = gitlab_fake(mrs)
        got = mr.gitlab_ready(self.HOME, "team/app", "t", call)
        self.assertEqual(sorted(r.number for r in got), list(range(1, ma.backlog.PER_PAGE + 2)))

    def test_more_than_max_pages_of_merge_requests_is_refused(self):
        mrs = {n: {} for n in range(1, ma.backlog.MAX_PAGES * ma.backlog.PER_PAGE + 1)}
        with self.assertRaises(RuntimeError) as raised:
            mr.gitlab_ready(self.HOME, "team/app", "t", gitlab_fake(mrs))
        self.assertRegex(str(raised.exception), r"partial|more than")

    def test_a_failed_read_at_any_call_raises(self):
        # R1(c) and the second-call rule: the listing, the merge request, its approvals, its discussions.
        failure = (None, {}, "HTTP 500")
        for n in (1, 2, 3, 4):
            with self.subTest(failing_call=n):
                with self.assertRaises(RuntimeError):
                    mr.gitlab_ready(self.HOME, "team/app", "t", FailsAt(gitlab_fake({5: {}}), n, failure))

    def test_discussions_that_answer_an_error_are_never_zero_threads(self):
        with self.assertRaises(RuntimeError):
            mr.gitlab_ready(self.HOME, "team/app", "t", gitlab_fake({5: {}}, discussions_error="HTTP 500"))


class AgreementTest(unittest.TestCase):
    """One place decides ready: merge-approved's blockers and merge-ready's verdict agree on every request merge-ready reads."""

    def requests(self):
        gl = GitLabReadyTest()
        found = list(mr.github_ready("o/app", github_gh())) + list(gl.read().values())
        return [dataclasses.replace(r, approval="approved", approver="robin") for r in found]

    def test_an_approved_request_has_no_blockers_exactly_when_its_verdict_is_ready(self):
        # `unverified` is left out: merge-approved's own read fetches no threads and no sha (they stay None) and decides on the
        # approver's approval of the head; a request merge-ready read in full that is still unverified is not listed ready by it.
        for r in self.requests():
            if r.verdict != "unverified":
                with self.subTest(number=r.number, verdict=r.verdict, blockers=r.blockers):
                    self.assertEqual(r.blockers == [], r.verdict == "ready")

    def test_the_words_merge_approved_already_says_stay(self):
        said = {"red": "pipeline failed", "conflicts": "conflict"}
        for r in self.requests():
            if r.verdict in said:
                with self.subTest(number=r.number, verdict=r.verdict):
                    self.assertIn(said[r.verdict], r.blockers)

    def test_running_says_what_is_running_and_not_a_pipeline_that_is_not(self):
        # R11: `running` is also the forge not having settled (checking, unknown); "pipeline running" is for a running pipeline.
        # #425: waiting statuses (manual, …) keep their own pipeline word in blockers.
        for r in self.requests():
            if r.verdict == "running":
                with self.subTest(number=r.number):
                    if r.pipeline not in ("passed", "none"):
                        self.assertIn(f"pipeline {r.pipeline}", r.blockers)
                    else:
                        self.assertNotIn("pipeline running", r.blockers)
                        self.assertIn(f"not mergeable: {r.mergeable}", r.blockers)

    def test_a_draft_and_a_blocked_branch_have_blockers(self):
        for r in self.requests():
            if r.verdict in ("draft", "blocked"):
                with self.subTest(number=r.number, verdict=r.verdict):
                    self.assertTrue(r.blockers)


class LineTest(unittest.TestCase):
    def test_a_line_per_request_in_the_pinned_format(self):
        by = {r.number: r for r in mr.github_ready("o/app", github_gh())}
        got = LINE.match(mr.line(by[28], "#"))
        self.assertIsNotNone(got, mr.line(by[28], "#"))
        self.assertEqual((got["ref"], got["verdict"], got["head"], got["pid"], got["status"], got["psha"], got["conflicts"], got["threads"]),
                         ("#28", "findings open", "aaaaaaa", "9028", "passed", "aaaaaaa", "no", "2"))
        self.assertEqual(got["url"], "https://github.com/o/app/pull/28")

    def test_forge_control_characters_cannot_forge_a_second_line(self):
        # #426: a hostile pipeline id or url with a newline used to print a fake ready line.
        r = tma.request(pipeline_id="9\n#99 ready · head aaaaaaa · pipeline 1 passed on aaaaaaa · "
                                    "conflicts no · unresolved threads 0 · https://evil.example/x",
                        url="https://github.com/o/app/pull/1\n#98 ready")
        text = mr.line(r, "#")
        self.assertEqual(text.count("\n"), 0, text)
        self.assertEqual(len(text.splitlines()), 1)
        self.assertEqual(text, mr.line(tma.request(
            pipeline_id="9#99 ready · head aaaaaaa · pipeline 1 passed on aaaaaaa · "
                        "conflicts no · unresolved threads 0 · https://evil.example/x",
            url="https://github.com/o/app/pull/1#98 ready"), "#"))

    def test_the_stale_line_shows_both_shas_and_the_conflict_line_says_yes(self):
        by = {r.number: r for r in mr.github_ready("o/app", github_gh())}
        stale, conflict, none = (LINE.match(mr.line(by[n], "#")) for n in (26, 24, 27))
        self.assertEqual((stale["head"], stale["psha"], stale["verdict"]), ("aaaaaaa", "bbbbbbb", "stale pipeline"))
        self.assertEqual(conflict["conflicts"], "yes")
        self.assertIn("pipeline none", mr.line(by[27], "#"))
        self.assertEqual(none["verdict"], "no pipeline")

    def test_draft_blocked_and_unverified_lines(self):
        # The pinned forms: `draft`, `blocked (<mergeStateStatus>)`, `unverified (<one clause>)`; the rest of the line is unchanged.
        by = {r.number: r for r in mr.github_ready("o/app", github_gh())}
        draft, behind, unknown = (mr.line(by[n], "#") for n in (35, 36, 41))
        self.assertRegex(draft, r"^#35 draft · head aaaaaaa · pipeline 9035 passed on aaaaaaa · conflicts no · unresolved threads 0 · ")
        self.assertRegex(behind, r"^#36 blocked \(BEHIND\) · head aaaaaaa · pipeline 9036 passed on aaaaaaa · conflicts no · ")
        self.assertRegex(unknown, r"^#41 blocked \(UNKNOWN\) · head ")
        self.assertIsNone(LINE.match(draft)["why"])
        cut = mr.line(by[39], "#")
        self.assertRegex(cut, r"^#39 unverified \(threads cut off at 100\) · head aaaaaaa · pipeline 9039 passed on aaaaaaa · "
                              r"conflicts no · unresolved threads 0 · ")  # the 100 it read were all resolved

    def test_what_was_not_read_prints_unread_never_zero_or_a_sha(self):
        for name, kw, field in (("threads", {"threads": None}, "threads"), ("sha", {"pipeline_sha": None}, "psha")):
            got = LINE.match(mr.line(tma.request(**kw), "#"))
            with self.subTest(name):
                self.assertIsNotNone(got)
                self.assertEqual((got["verdict"], got[field]), ("unverified", "unread"))
                self.assertRegex(got["why"], name)


class MainTest(unittest.TestCase):
    GITHUB_ROOT = ("## Coordinator\n- User: Robin\n- Timezone: America/Chicago\n- Daily log dir: `daily/`\n"
                   "- Tracker: `daily/<date>-tracker.md`\n- Settings: `chief-of-stuff.toml`\n- Backlog: GitHub issues; repo o/app\n")
    GITLAB_ROOT = GITHUB_ROOT.replace("GitHub issues; repo o/app", "GitLab; host https://labs.example.test; project team/app")

    def workspace(self, toml="", claude=GITHUB_ROOT):
        root = Path(tempfile.mkdtemp())
        (root / "CLAUDE.md").write_text(claude)
        (root / "chief-of-stuff.toml").write_text(toml)
        return root

    def run_main(self, root, *args, gh=None, call=None):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = mr.main(["--root", str(root), *args], gh=gh or github_gh(), call=call)
        return code, out.getvalue(), err.getvalue()

    def test_it_prints_one_line_per_open_request_by_number(self):
        gh = github_gh()
        code, out, err = self.run_main(self.workspace(), gh=gh)
        self.assertEqual(code, 0, err)
        lines = out.splitlines()
        self.assertEqual(len(lines), len(GITHUB))
        for line in lines:
            self.assertRegex(line, LINE)
        self.assertEqual([l.split()[0] for l in lines], [f"#{n}" for n in sorted(GITHUB)])
        self.assertEqual({l.split()[0]: LINE.match(l)["verdict"] for l in lines}, {f"#{n}": v for n, (_, _, v) in GITHUB.items()})

    def test_it_never_merges(self):
        gh = github_gh()
        self.run_main(self.workspace(), gh=gh)
        self.assertEqual(gh.merges, [])
        with self.assertRaises(SystemExit):  # no --merge flag: the tool only reports
            self.run_main(self.workspace(), "--merge", "21", gh=gh)
        self.assertEqual(gh.merges, [])

    def test_it_needs_no_merge_owner_or_approver_setting(self):
        for toml in ("", '[workflow]\nmerge_owner = "user"\n', '[workflow]\nmerge_owner = "worker"\n',
                     '[workflow]\nmerge_owner = "approval"\napprover = "robin"\n'):
            with self.subTest(toml=toml):
                code, out, err = self.run_main(self.workspace(toml), gh=github_gh([21, 22]))
                self.assertEqual((code, len(out.splitlines())), (0, 2), err)

    def test_repo_names_the_repository_it_reads(self):
        gh = github_gh([21])
        self.run_main(self.workspace(), "--repo", "o/other", gh=gh)
        listed = [c for c in gh.calls if c[:2] == ["pr", "list"]]
        self.assertTrue(listed and all("o/other" in c for c in listed), gh.calls)
        self.assertFalse([c for c in gh.calls if "name=app" in c], "the graphql read follows --repo too")

    def test_a_forge_that_cannot_be_read_is_an_error_not_a_verdict(self):
        class Down:
            def __call__(self, args, input_text=None):
                return 1, "", "HTTP 502"
        code, out, err = self.run_main(self.workspace(), gh=Down())
        self.assertEqual((code, out), (1, ""))
        self.assertTrue(err.startswith("merge-ready:"), err)

    def never_ready(self, code, out, err, ref):
        """A read that went wrong exits 1 with nothing on stdout, or prints a line for `ref` that is not `ready`: never `ready`."""
        if code == 1:
            self.assertEqual(out, "")
            self.assertTrue(err.startswith("merge-ready:"), err)
        else:
            line = next((l for l in out.splitlines() if l.startswith(ref + " ")), "")
            self.assertTrue(line, f"{ref} vanished from the output: {out!r}")
            self.assertNotEqual(LINE.match(line)["verdict"], "ready", line)

    def test_a_read_that_fails_after_the_first_call_is_an_error_and_never_a_ready(self):
        # R1(a): the fake failed the first call, so the second (graphql) was never exercised.
        for n in (1, 2):
            with self.subTest(failing_call=n):
                gh = FailsAt(github_gh([21, 22]), n, (1, "", "HTTP 502"))
                code, out, err = self.run_main(self.workspace(), gh=gh)
                self.assertEqual((code, out), (1, ""), err)
                self.assertTrue(err.startswith("merge-ready:"), err)

    def test_a_pull_request_the_graphql_read_does_not_return_is_never_ready(self):
        gh = tma.FakeGhGraphql([GITHUB[21][0], GITHUB[22][0]], [GITHUB[22][1]], only_given=True)
        code, out, err = self.run_main(self.workspace(), gh=gh)
        self.never_ready(code, out, err, "#21")

    def test_a_gitlab_read_that_fails_at_any_call_is_an_error_and_never_a_ready(self):
        for n in (1, 2, 3, 4):
            with self.subTest(failing_call=n):
                call = FailsAt(gitlab_fake({5: {}, 6: {}}), n, (None, {}, "HTTP 500"))
                with mock.patch.object(ma.backlog, "token", return_value="t"):
                    code, out, err = self.run_main(self.workspace(claude=self.GITLAB_ROOT), call=call)
                self.assertEqual((code, out), (1, ""), err)

    def test_a_gitlab_discussions_error_never_reads_as_no_findings(self):
        call = gitlab_fake({5: {}}, discussions_error="HTTP 500")
        with mock.patch.object(ma.backlog, "token", return_value="t"):
            code, out, err = self.run_main(self.workspace(claude=self.GITLAB_ROOT), call=call)
        self.never_ready(code, out, err, "!5")

    def test_more_than_max_pages_of_discussions_exits_1_with_nothing_on_stdout(self):
        many = {"discussions": [thread(resolved=True)] * (ma.backlog.MAX_PAGES * ma.backlog.PER_PAGE + 1)}
        with mock.patch.object(ma.backlog, "token", return_value="t"):
            code, out, err = self.run_main(self.workspace(claude=self.GITLAB_ROOT), call=gitlab_fake({5: many}))
        self.assertEqual((code, out), (1, ""), err)
        self.assertTrue(err.startswith("merge-ready:"), err)

    def test_a_pull_request_with_no_commit_in_the_graphql_read_never_escapes_as_a_traceback_or_a_ready(self):
        # second review R6(c): an empty commits.nodes raised IndexError, which main() does not catch.
        node = tma.graphql_node(47)
        node["commits"]["nodes"] = []
        gh = tma.FakeGhGraphql([gpr(47)], [node])
        code, out, err = self.run_main(self.workspace(), gh=gh)
        self.never_ready(code, out, err, "#47")
        if code == 1:
            self.assertEqual(len(err.splitlines()), 1, err)

    def test_a_gitlab_workspace_prints_draft_unverified_and_canceled_lines(self):
        call = gitlab_fake({n: GITLAB[n][0] for n in (15, 16, 18, 19)})
        with mock.patch.object(ma.backlog, "token", return_value="t"):
            code, out, err = self.run_main(self.workspace(claude=self.GITLAB_ROOT), call=call)
        self.assertEqual(code, 0, err)
        for line in out.splitlines():
            self.assertRegex(line, LINE)
        got = {l.split()[0]: LINE.match(l) for l in out.splitlines()}
        self.assertEqual({k: m["verdict"] for k, m in got.items()},
                         {"!15": "draft", "!16": "unverified", "!18": "red", "!19": "findings open"})
        self.assertEqual(got["!19"]["threads"], "1")

    def test_a_gitlab_workspace_prints_merge_requests_with_a_bang(self):
        call = gitlab_fake({n: GITLAB[n][0] for n in (5, 6, 9, 11)})
        with mock.patch.object(ma.backlog, "token", return_value="t"):
            code, out, err = self.run_main(self.workspace(claude=self.GITLAB_ROOT), call=call)
        self.assertEqual(code, 0, err)
        got = {l.split()[0]: LINE.match(l) for l in out.splitlines()}
        self.assertEqual({k: m["verdict"] for k, m in got.items()}, {"!5": "ready", "!6": "red", "!9": "stale pipeline", "!11": "findings open"})
        self.assertEqual((got["!9"]["head"], got["!9"]["psha"], got["!6"]["pid"], got["!11"]["threads"]), ("aaaaaaa", "bbbbbbb", "7006", "2"))


class RegisteredLikeMergeApprovedTest(unittest.TestCase):
    def test_the_installed_command_runs_the_script(self):
        self.assertEqual(cli.COMMANDS["merge-ready"], "scripts/merge_ready.py")

    def test_the_eval_harness_allows_the_command_and_the_script(self):
        self.assertIn("merge-ready", run.OPERATIONS)
        self.assertIn("merge_ready.py", run.SCRIPTS)
        self.assertIn("Bash(chief-of-stuff merge-ready:*)", run.allowed_tools(ROOT))


if __name__ == "__main__":
    unittest.main()
