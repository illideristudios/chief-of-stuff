"""The coordinator merges only what the configured approver approved, on a passing, mergeable head."""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import merge_approved as ma  # noqa: E402
import settings as st  # noqa: E402

HEAD = "a" * 40
OLD = "b" * 40
OK = [{"__typename": "CheckRun", "status": "COMPLETED", "conclusion": "SUCCESS"}]


def review(login, state, oid=HEAD, at="2026-09-26T01:00:00Z"):
    return {"author": {"login": login}, "state": state, "commit": {"oid": oid}, "submittedAt": at}


def pr(number, reviews, checks=OK, mergeable="MERGEABLE", head=HEAD, draft=False, state="CLEAN"):
    """`gh pr list --json` for one pull request; `draft` is isDraft and `state` is mergeStateStatus (#419 review: R5, R6)."""
    return {"number": number, "title": f"PR {number}", "url": f"https://github.com/o/app/pull/{number}",
            "headRefOid": head, "reviews": reviews, "statusCheckRollup": checks, "mergeable": mergeable,
            "isDraft": draft, "mergeStateStatus": state}


PRS = [
    pr(12, [review("robin", "APPROVED")]),
    pr(13, [review("peer-bot", "APPROVED")]),
    pr(14, [review("robin", "APPROVED", oid=OLD)]),
    pr(15, [review("robin", "APPROVED", at="2026-09-26T01:00:00Z"),
            review("robin", "CHANGES_REQUESTED", at="2026-09-26T02:00:00Z")]),
    pr(16, [review("robin", "APPROVED")], checks=[{"__typename": "CheckRun", "status": "COMPLETED", "conclusion": "FAILURE"}]),
    pr(17, [review("robin", "APPROVED")], mergeable="CONFLICTING"),
    pr(18, [review("robin", "APPROVED")], checks=[{"__typename": "CheckRun", "status": "IN_PROGRESS", "conclusion": ""}]),
    pr(19, [review("robin", "APPROVED"), review("robin", "COMMENTED", at="2026-09-26T03:00:00Z")]),
]


ALL_MERGE_METHODS = {"allow_merge_commit": True, "allow_squash_merge": True, "allow_rebase_merge": True}


class FakeGh:
    def __init__(self, prs, repo_settings=ALL_MERGE_METHODS, api_repos_exit=0, api_repos_err="", api_repos_body=None):
        self.prs, self.calls = prs, []
        self.repo_settings, self.api_repos_exit = repo_settings, api_repos_exit
        self.api_repos_err, self.api_repos_body = api_repos_err, api_repos_body

    def __call__(self, args, input_text=None):
        self.calls.append(args)
        if args[:2] == ["pr", "list"]:
            return 0, json.dumps(self.prs), ""
        if args[:1] == ["api"] and len(args) == 2 and args[1].startswith("repos/"):
            if self.api_repos_exit:
                return self.api_repos_exit, "", self.api_repos_err or "api failed"
            body = self.api_repos_body if self.api_repos_body is not None else json.dumps(self.repo_settings)
            return 0, body, ""
        if args[:2] == ["pr", "merge"]:
            return 0, "", ""
        return 1, "", "unexpected"

    @property
    def merges(self):
        return [c for c in self.calls if c[:2] == ["pr", "merge"]]

    @property
    def repo_api_calls(self):
        return [c for c in self.calls if c[:1] == ["api"] and len(c) == 2 and c[1].startswith("repos/")]


def graphql_node(number, threads=(), sha=HEAD, run=None, more=False):
    """What the one graphql call of merge-ready answers for an open pull request (#419): its review threads, and the
    commit its checks ran on with the id of the workflow run that made them. `threads` is one isResolved flag per thread;
    `more` is reviewThreads.pageInfo.hasNextPage (the first 100 are all that came back); `run=False` is a commit with no checks.
    FakeGhGraphql adds isDraft and mergeStateStatus from the list's row if the query asks for them."""
    runs = [] if run is False else [{"__typename": "CheckRun", "checkSuite": {"workflowRun": {"databaseId": run or 9000 + number}}}]
    return {"number": number,
            "reviewThreads": {"nodes": [{"isResolved": t} for t in threads], "pageInfo": {"hasNextPage": more}},
            "commits": {"nodes": [{"commit": {"oid": sha, "statusCheckRollup": {"contexts": {"nodes": runs}} if runs else None}}]}}


class FakeGhGraphql(FakeGh):
    """FakeGh that also answers merge-ready's graphql call. `nodes` replaces the default node of a pull request, by number."""

    def __init__(self, prs, nodes=(), only_given=False):
        super().__init__(prs)
        self.nodes = {n["number"]: n for n in nodes}
        self.only_given = only_given  # True: answer only the nodes given, so a pull request can be missing from the read

    def __call__(self, args, input_text=None):
        if args[:2] == ["api", "graphql"]:
            self.calls.append(args)
            query = next((a for a in args if a.startswith("query=")), "")
            # Faithful to the query: a node carries `isDraft` and `mergeStateStatus` only if the query asks for them. The list row's
            # value is the default and a node given in `nodes` overrides it (`isDraft: True` against a list row of False), so a draft
            # seen by only one of the two reads is separable (#419 second review, R5).
            nodes = [{k: v for k, v in {"isDraft": p.get("isDraft", False), "mergeStateStatus": p.get("mergeStateStatus", "CLEAN"),
                                        **(self.nodes.get(p["number"]) or graphql_node(p["number"], sha=p["headRefOid"]))}.items()
                      if k in ("number", "reviewThreads", "commits") or k in query}
                     for p in self.prs if p["number"] in self.nodes or not self.only_given]
            return 0, json.dumps({"data": {"repository": {"pullRequests": {"nodes": nodes}}}}), ""
        return super().__call__(args, input_text)


class GitHubReadinessTest(unittest.TestCase):
    def test_only_the_approvers_approval_on_the_head_with_passing_checks_and_no_conflict_is_ready(self):
        got = {r.number: r.blockers for r in ma.github_requests("o/app", "robin", FakeGh(PRS))}
        self.assertEqual(got[12], [])
        self.assertEqual(got[19], [], "a later comment does not withdraw an approval")
        self.assertEqual(got[13], ["no approval from robin"])
        self.assertEqual(got[14], ["robin approved an earlier commit"])
        self.assertEqual(got[15], ["no approval from robin"])
        self.assertEqual(got[16], ["pipeline failed"])
        self.assertEqual(got[17], ["conflict"])
        self.assertEqual(got[18], ["pipeline running"])


class GitLabReadinessTest(unittest.TestCase):
    def fake(self, mrs):
        calls = []

        def call(method, url, token, timeout, payload=None):
            calls.append((method, url, payload))
            path = url.split("/api/v4/projects/team%2Fapp", 1)[1]
            if method == "GET" and path.startswith("/merge_requests?"):
                return [{"iid": m["iid"]} for m in mrs], {}, ""
            iid = int(path.split("/")[2])
            m = next(x for x in mrs if x["iid"] == iid)
            if path.endswith("/approvals"):
                return {"approved_by": [{"user": {"username": u}} for u in m["approved_by"]]}, {}, ""
            if method == "PUT":
                return {"state": "merged"}, {}, ""
            return {"iid": iid, "title": "MR", "web_url": "u", "sha": HEAD, "has_conflicts": m.get("conflict", False),
                    "detailed_merge_status": m.get("status", "mergeable"),
                    "head_pipeline": {"status": m.get("pipeline", "success")}}, {}, ""
        return call, calls

    def test_a_gitlab_mr_needs_the_approver_a_passing_pipeline_and_mergeable(self):
        call, _ = self.fake([{"iid": 5, "approved_by": ["robin"]}, {"iid": 6, "approved_by": ["peer-bot"]},
                             {"iid": 7, "approved_by": ["robin"], "pipeline": "running"},
                             {"iid": 8, "approved_by": ["robin"], "conflict": True, "status": "conflict"}])
        home = ma.backlog.Backlog("https://labs.example.test", "team/app")
        got = {r.number: r.blockers for r in ma.gitlab_requests(home, "team/app", "robin", "t", call)}
        self.assertEqual(got, {5: [], 6: ["no approval from robin"], 7: ["pipeline running"], 8: ["conflict"]})


class MainTest(unittest.TestCase):
    DEFAULT_CLAUDE = (
        "## Coordinator\n- User: Robin\n- Timezone: America/Chicago\n"
        "- Daily log dir: `daily/`\n- Tracker: `daily/<date>-tracker.md`\n"
        "- Settings: `chief-of-stuff.toml`\n- Backlog: GitHub issues; repo o/app\n"
    )

    def workspace(self, toml, claude=None):
        root = Path(tempfile.mkdtemp())
        (root / "CLAUDE.md").write_text(claude or self.DEFAULT_CLAUDE)
        (root / "chief-of-stuff.toml").write_text(toml)
        return root

    def run_main(self, root, *args, gh=None, call=None):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = ma.main(["--root", str(root), *args], gh=gh, call=call)
        return code, out.getvalue(), err.getvalue()

    OPT_IN = '[workflow]\nmerge_owner = "approval"\napprover = "robin"\n'

    def test_merge_merges_a_ready_pr_at_its_checked_head(self):
        gh = FakeGh(PRS)
        code, out, _ = self.run_main(self.workspace(self.OPT_IN), "--merge", "12", gh=gh)
        self.assertEqual(code, 0)
        self.assertEqual(len(gh.merges), 1)
        self.assertIn("12", gh.merges[0])
        self.assertIn("--merge", gh.merges[0])
        self.assertNotIn("--squash", gh.merges[0])
        self.assertNotIn("--rebase", gh.merges[0])
        self.assertEqual(gh.merges[0][gh.merges[0].index("--match-head-commit") + 1], HEAD)
        self.assertIn("merged", out)

    def test_merge_refuses_a_peer_approval_and_says_why(self):
        gh = FakeGh(PRS)
        code, _, err = self.run_main(self.workspace(self.OPT_IN), "--merge", "13", gh=gh)
        self.assertEqual(code, 1)
        self.assertEqual(gh.merges, [])
        self.assertIn("no approval from robin", err)

    def test_the_listing_marks_each_ready_or_blocked(self):
        code, out, _ = self.run_main(self.workspace(self.OPT_IN), gh=FakeGh(PRS))
        self.assertEqual(code, 0)
        line = {l.split()[0]: l for l in out.splitlines() if l.startswith("#")}
        self.assertIn("ready", line["#12"])
        self.assertIn("pipeline failed", line["#16"])

    def test_without_the_opt_in_merging_stays_the_users(self):
        gh = FakeGh(PRS)
        code, _, err = self.run_main(self.workspace('[workflow]\nmerge_owner = "user"\n'), "--merge", "12", gh=gh)
        self.assertEqual(code, 2)
        self.assertEqual(gh.calls, [])
        self.assertIn("merge_owner", err)

    GITLAB_CLAUDE = (
        "## Coordinator\n- User: Robin\n- Timezone: America/Chicago\n- Daily log dir: `daily/`\n"
        "- Tracker: `daily/<date>-tracker.md`\n- Settings: `chief-of-stuff.toml`\n"
        "- Backlog: GitLab; host https://labs.example.test; project team/app\n"
    )

    def test_gitlab_merge_puts_only_the_head_sha(self):
        mrs = [{"iid": 5, "approved_by": ["robin"]}]
        call, calls = GitLabReadinessTest().fake(mrs)

        root = self.workspace(self.OPT_IN, claude=self.GITLAB_CLAUDE)
        with mock.patch.dict(os.environ, {"CHIEF_OF_STUFF_GITLAB_TOKEN": "tok"}):
            code, out, err = self.run_main(root, "--merge", "5", call=call)
        self.assertEqual(code, 0, err)
        put = next(c for c in calls if c[0] == "PUT")
        self.assertEqual(put[2], {"sha": HEAD})
        self.assertIn("merged", out)


class GitHubMergeFlagTest(unittest.TestCase):
    def gh(self, **repo_settings):
        return FakeGh([], repo_settings=repo_settings)

    def test_it_picks_the_first_allowed_method_in_githubs_order(self):
        table = (
            ({"allow_merge_commit": False, "allow_squash_merge": True, "allow_rebase_merge": False}, "--squash"),
            ({"allow_merge_commit": False, "allow_squash_merge": False, "allow_rebase_merge": True}, "--rebase"),
            ({"allow_merge_commit": False, "allow_squash_merge": True, "allow_rebase_merge": True}, "--squash"),
            ({"allow_merge_commit": True, "allow_squash_merge": False, "allow_rebase_merge": True}, "--merge"),
        )
        for settings, want in table:
            with self.subTest(**settings):
                flag, why = ma.github_merge_flag("o/app", self.gh(**settings))
                self.assertEqual((flag, why), (want, ""))

    def test_it_refuses_when_no_method_is_allowed_or_settings_are_unreadable(self):
        gh_none = self.gh(allow_merge_commit=False, allow_squash_merge=False, allow_rebase_merge=False)
        self.assertEqual(ma.github_merge_flag("o/app", gh_none), (None, "repository allows no merge method"))
        gh_fail = FakeGh([], api_repos_exit=1)
        self.assertEqual(ma.github_merge_flag("o/app", gh_fail)[0], None)
        gh_bad_json = FakeGh([], api_repos_body="not-json")
        self.assertEqual(ma.github_merge_flag("o/app", gh_bad_json)[0], None)
        missing = FakeGh([], repo_settings={"allow_merge_commit": True, "allow_squash_merge": True})
        self.assertEqual(ma.github_merge_flag("o/app", missing)[0], None)
        non_bool = self.gh(allow_merge_commit="true", allow_squash_merge=False, allow_rebase_merge=False)
        self.assertEqual(ma.github_merge_flag("o/app", non_bool)[0], None)

    def test_a_blocked_merge_never_reads_settings_or_merges(self):
        gh = FakeGh(PRS)
        main = MainTest()
        code, _, err = main.run_main(main.workspace(MainTest.OPT_IN), "--merge", "13", gh=gh)
        self.assertEqual(code, 1)
        self.assertEqual(gh.repo_api_calls, [])
        self.assertEqual(gh.merges, [])
        self.assertIn("no approval from robin", err)

    def test_unreadable_settings_refuse_without_merging(self):
        main = MainTest()
        for gh in (FakeGh(PRS, api_repos_exit=1),
                   FakeGh(PRS, repo_settings={"allow_merge_commit": False, "allow_squash_merge": False,
                                              "allow_rebase_merge": False})):
            with self.subTest(gh=gh):
                code, _, err = main.run_main(main.workspace(MainTest.OPT_IN), "--merge", "12", gh=gh)
                self.assertEqual(code, 1)
                self.assertEqual(gh.merges, [])
                self.assertEqual(len(gh.repo_api_calls), 1)
                self.assertIn("not merged", err)


def request(**kw):
    """A request as merge-ready's readers fill it: the head pipeline's id and sha, and the unresolved thread count (#419)."""
    base = dict(number=1, title="t", url="u", head=HEAD, approval="approved", pipeline="passed", mergeable="yes",
                approver="robin", pipeline_id="9001", pipeline_sha=HEAD, threads=0, draft=False, merge_state="CLEAN")
    return ma.Request(**{**base, **kw})


class RequestVerdictTest(unittest.TestCase):
    """One place decides ready or not (#419: the coordinator wrote mergeable from the forge's own status, no pipeline read).
    Precedence (#419 review): conflicts > draft > red > stale pipeline > running > no pipeline > unverified > blocked > findings
    open > ready. A state that was not read is None, never "" or 0, and None is not ready."""

    def test_the_request_carries_the_head_pipeline_and_the_thread_count_and_unread_is_none(self):
        r = ma.Request(1, "t", "u", HEAD, "approved", "passed", "yes", "robin")
        self.assertEqual(r.pipeline_id, "")
        self.assertIsNone(r.pipeline_sha, "a head pipeline's sha that was not read is None, never ''")
        self.assertIsNone(r.threads, "unresolved threads that were not read are None, never 0")
        self.assertEqual((r.draft, r.merge_state), (False, ""))

    def test_a_request_whose_pipeline_sha_or_threads_were_not_read_is_never_ready(self):
        # R7: an empty string used to mean "not stale" and 0 "no findings", so a request read by the plain readers said ready.
        bare = ma.Request(1, "t", "u", HEAD, "approved", "passed", "yes", "robin")
        self.assertEqual(bare.verdict, "unverified")
        self.assertEqual(request(pipeline_sha=None).verdict, "unverified")
        self.assertEqual(request(threads=None).verdict, "unverified")
        self.assertNotEqual(request(pipeline_sha="").verdict, "ready", "an empty sha is not the head's sha")
        self.assertNotEqual(request(pipeline_sha="").verdict, "findings open")

    def test_the_plain_readers_never_hand_back_a_ready_request(self):
        got = {r.number: r.verdict for r in ma.github_requests("o/app", "robin", FakeGh(PRS))}
        self.assertEqual(got, {12: "unverified", 13: "unverified", 14: "unverified", 15: "unverified", 16: "red",
                               17: "conflicts", 18: "running", 19: "unverified"})

    def test_ready_only_when_the_pipeline_passed_on_the_head_with_no_conflict_and_no_open_thread(self):
        self.assertEqual(request().verdict, "ready")

    def test_each_verdict_and_the_precedence_between_them(self):
        table = (
            ({"mergeable": "conflict"}, "conflicts"),
            ({"mergeable": "conflict", "pipeline": "failed", "threads": 2}, "conflicts"),
            ({"pipeline": "failed"}, "red"),
            ({"pipeline": "failed", "pipeline_sha": OLD, "threads": 2}, "red"),
            ({"pipeline_sha": OLD}, "stale pipeline"),
            ({"pipeline_sha": OLD, "threads": 2}, "stale pipeline"),
            ({"pipeline": "running", "pipeline_sha": OLD}, "stale pipeline"),
            ({"pipeline": "running"}, "running"),
            ({"pipeline": "running", "threads": 2}, "running"),
            ({"mergeable": "checking"}, "running"),
            ({"mergeable": "unknown"}, "running"),
            ({"pipeline": "none", "pipeline_id": "", "pipeline_sha": ""}, "no pipeline"),
            ({"pipeline": "none", "pipeline_id": "", "pipeline_sha": "", "threads": 2}, "no pipeline"),
            ({"pipeline": "none", "pipeline_id": "", "pipeline_sha": None, "threads": None}, "no pipeline"),
            ({"threads": 2}, "findings open"),
            ({"threads": 1}, "findings open"),
            # draft: not ready, below only conflicts
            ({"draft": True}, "draft"),
            ({"draft": True, "pipeline": "failed", "pipeline_sha": OLD, "threads": 2}, "draft"),
            ({"draft": True, "pipeline": "none", "pipeline_id": "", "pipeline_sha": ""}, "draft"),
            ({"draft": True, "mergeable": "conflict"}, "conflicts"),
            # unverified: unread state, below no pipeline and above blocked and findings
            ({"pipeline_sha": None}, "unverified"),
            ({"threads": None}, "unverified"),
            ({"pipeline_sha": None, "pipeline": "running"}, "running"),
            ({"pipeline_sha": None, "pipeline": "failed"}, "red"),
            ({"threads": None, "merge_state": "BEHIND"}, "unverified"),
            ({"threads": None, "pipeline": "none", "pipeline_id": "", "pipeline_sha": ""}, "no pipeline"),
            # blocked: GitHub's mergeStateStatus is anything but CLEAN; below unverified, above findings
            ({"merge_state": "BLOCKED"}, "blocked"),
            ({"merge_state": "BEHIND"}, "blocked"),
            ({"merge_state": "DIRTY"}, "blocked"),
            ({"merge_state": "UNSTABLE"}, "blocked"),
            ({"merge_state": "HAS_HOOKS"}, "blocked"),
            ({"merge_state": "UNKNOWN"}, "blocked"),
            ({"merge_state": "BEHIND", "threads": 2}, "blocked"),
            ({"merge_state": "BEHIND", "pipeline": "running"}, "running"),
            ({"merge_state": "BEHIND", "pipeline_sha": OLD}, "stale pipeline"),
            ({"merge_state": "CLEAN"}, "ready"),
            ({"merge_state": ""}, "ready"),  # GitLab has no mergeStateStatus
        )
        for kw, want in table:
            with self.subTest(**kw):
                self.assertEqual(request(**kw).verdict, want)

    def test_the_forges_own_no_conflicts_word_alone_never_makes_a_request_ready(self):
        # The #419 defect: the forge said "mergeable" (no conflicts) with a red head pipeline, and with open findings.
        for kw in ({"pipeline": "failed"}, {"threads": 3}, {"pipeline_sha": OLD}):
            with self.subTest(**kw):
                self.assertNotEqual(request(mergeable="yes", **kw).verdict, "ready")


class WorkerMergeOwnerTest(unittest.TestCase):
    """#420: with merge_owner = "worker" the owning worker merges, so merge-approved lists readiness and never merges."""

    WORKER = '[workflow]\nmerge_owner = "worker"\n'
    PRS = [pr(31, [], checks=[{"__typename": "CheckRun", "status": "COMPLETED", "conclusion": "FAILURE"}]),
           pr(32, []),
           pr(33, [], mergeable="CONFLICTING"),
           pr(34, [])]

    def run_main(self, toml, *args, gh=None):
        main = MainTest()
        return main.run_main(main.workspace(toml), *args, gh=gh or FakeGhGraphql(self.PRS))

    def test_it_lists_each_open_request_with_its_merge_ready_line_ready_ones_first(self):
        code, out, err = self.run_main(self.WORKER)
        self.assertEqual(code, 0, err)
        lines = [l for l in out.splitlines() if l[:1] == "#"]
        self.assertEqual([l.split()[0] for l in lines], ["#32", "#34", "#31", "#33"])
        self.assertEqual([l.split()[1] for l in lines], ["ready", "ready", "red", "conflicts"])
        # the line merge-ready prints: verdict, head sha, pipeline id with status and sha, conflicts, threads, url
        self.assertEqual(lines[0], "#32 ready · head aaaaaaa · pipeline 9032 passed on aaaaaaa · conflicts no · "
                                   "unresolved threads 0 · https://github.com/o/app/pull/32")

    def test_it_says_the_ready_ones_are_for_their_owning_worker_to_merge(self):
        code, out, _ = self.run_main(self.WORKER)
        self.assertEqual(code, 0)
        self.assertIn("ready for their owning worker to merge: #32 #34", out.splitlines())

    def test_with_nothing_ready_it_names_none_for_the_worker(self):
        code, out, _ = self.run_main(self.WORKER, gh=FakeGhGraphql([self.PRS[0], self.PRS[2]]))
        self.assertEqual((code, len(out.splitlines())), (0, 2))
        self.assertNotIn("ready for their owning worker", out)

    def test_it_never_merges_and_never_says_merging_stays_the_users(self):
        gh = FakeGhGraphql(self.PRS)
        for args in ((), ("--merge", "32")):
            code, out, err = self.run_main(self.WORKER, *args, gh=gh)
            with self.subTest(args=args):
                self.assertEqual(gh.merges, [])
                self.assertNotIn("stays the user's", out + err)
                self.assertNotIn("merging on approval is off", out + err)
        self.assertEqual(code, 2, "--merge is refused for worker: the owning worker merges")
        self.assertIn("owning worker", err)

    def test_it_needs_no_approver(self):
        code, _, err = self.run_main(self.WORKER)
        self.assertEqual(code, 0, err)

    def test_user_and_approval_behave_as_before(self):
        gh = FakeGhGraphql(self.PRS)
        code, _, err = self.run_main('[workflow]\nmerge_owner = "user"\n', gh=gh)
        self.assertEqual((code, gh.calls), (2, []))
        self.assertIn("merging stays the user's", err)
        code, out, _ = self.run_main(MainTest.OPT_IN, gh=FakeGh(PRS))
        self.assertEqual(code, 0)
        self.assertIn("ready", {l.split()[0]: l for l in out.splitlines()}["#12"])
        self.assertNotIn("owning worker", out)


class ApprovalModeKeepsItsRulesTest(unittest.TestCase):
    """approval mode keeps its own rule (the approver's approval on the head decides) and still never merges what is plainly not
    mergeable: #419 review R1(e), R5, R6, R11."""

    ROBIN = [review("robin", "APPROVED")]

    def run_main(self, prs, *args):
        gh = FakeGh(prs)
        main = MainTest()
        code, out, err = main.run_main(main.workspace(MainTest.OPT_IN), *args, gh=gh)
        return gh, code, out, err

    def test_an_approved_request_with_no_pipeline_stays_ready_and_merges(self):
        # R1(e): merge_approved.blocked() drops the "no pipeline" blocker; the docstring says a request with none stays ready.
        prs = [pr(40, self.ROBIN, checks=[])]
        gh, code, out, _ = self.run_main(prs)
        self.assertEqual(code, 0)
        self.assertEqual(out.split()[:2], ["#40", "ready"])
        gh, code, _, err = self.run_main(prs, "--merge", "40")
        self.assertEqual((code, len(gh.merges)), (0, 1), err)

    def test_a_pipeline_that_passed_with_the_forge_still_unsure_reads_not_mergeable_without_saying_running(self):
        # R11: the forge's own word is the blocker; "pipeline running" is for a pipeline that is running.
        r = ma.Request(1, "t", "u", HEAD, "approved", "passed", "unknown", "robin")
        self.assertEqual(r.blockers, ["not mergeable: unknown"])
        r = ma.Request(1, "t", "u", HEAD, "approved", "passed", "checking", "robin")
        self.assertEqual(r.blockers, ["not mergeable: checking"])
        r = ma.Request(1, "t", "u", HEAD, "approved", "running", "yes", "robin")
        self.assertEqual(r.blockers, ["pipeline running"])

    def test_a_draft_or_a_branch_the_forge_will_not_merge_is_not_merged_on_approval(self):
        prs = [pr(41, self.ROBIN, draft=True), pr(42, self.ROBIN, state="BEHIND"), pr(43, self.ROBIN, state="BLOCKED"),
               pr(44, self.ROBIN)]
        found = {r.number: r.blockers for r in ma.github_requests("o/app", "robin", FakeGh(prs))}
        self.assertEqual(found[44], [])
        for n in (41, 42, 43):
            with self.subTest(n=n):
                self.assertTrue(found[n], f"#{n} must name what blocks it")
                gh, code, _, err = self.run_main(prs, "--merge", str(n))
                self.assertEqual((code, gh.merges), (1, []), err)


class WorkerListingNeverNamesWhatIsNotReadyTest(unittest.TestCase):
    def test_a_draft_a_blocked_and_an_unverified_request_are_listed_but_never_named_ready(self):
        prs = [pr(51, []), pr(52, [], draft=True), pr(53, [], state="BEHIND"), pr(54, [])]
        gh = FakeGhGraphql(prs, [graphql_node(54, [False], more=True)])
        main = MainTest()
        code, out, err = main.run_main(main.workspace(WorkerMergeOwnerTest.WORKER), gh=gh)
        self.assertEqual(code, 0, err)
        lines = {l.split()[0]: l for l in out.splitlines() if l[:1] == "#"}
        self.assertEqual([lines[k].split()[1] for k in ("#51", "#52", "#53", "#54")], ["ready", "draft", "blocked", "unverified"])
        self.assertIn("ready for their owning worker to merge: #51", out.splitlines())
        self.assertNotRegex(out, r"owning worker to merge:.*#5[234]")


class SettingsTest(unittest.TestCase):
    def load(self, text):
        root = Path(tempfile.mkdtemp())
        (root / "s.toml").write_text(text)
        return st.load(root, "s.toml").workflow

    def test_approval_needs_an_approver_and_pull_requests(self):
        wf = self.load('[workflow]\nmerge_owner = "approval"\napprover = "robin"\n')
        self.assertEqual((wf.merge_owner, wf.approver), ("approval", "robin"))
        for bad in ('merge_owner = "approval"', 'merge_owner = "approval"\napprover = ""',
                    'delivery = "branch"\nmerge_owner = "approval"\napprover = "robin"'):
            with self.assertRaises(st.SettingsError, msg=bad):
                self.load("[workflow]\n" + bad + "\n")


if __name__ == "__main__":
    unittest.main()
