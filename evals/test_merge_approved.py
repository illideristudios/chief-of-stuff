"""The coordinator merges only what the configured approver approved, on a passing, mergeable head."""

from __future__ import annotations

import io
import json
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


SQUASH_ONLY = json.dumps({"allow_merge_commit": False, "allow_squash_merge": True, "allow_rebase_merge": False})


class FakeGh:
    def __init__(self, prs, settings=SQUASH_ONLY):
        self.prs, self.calls, self.settings = prs, [], settings  # #864: the repository's allowed merge methods

    def __call__(self, args, input_text=None):
        self.calls.append(args)
        if len(args) == 2 and args[0] == "api" and args[1].startswith("repos/"):
            return 0, self.settings, ""
        if args[:2] == ["pr", "list"]:
            return 0, json.dumps(self.prs), ""
        if args[:2] == ["pr", "merge"]:
            return 0, "", ""
        if args[:2] == ["pr", "view"]:
            state = "MERGED" if any(c[2] == args[2] for c in self.merges) else "OPEN"
            return 0, json.dumps({"state": state}), ""
        return 1, "", "unexpected"

    @property
    def merges(self):
        return [c for c in self.calls if c[:2] == ["pr", "merge"]]


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
            return 0, json.dumps({"data": {"repository": {"pullRequests": {
                "pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": nodes}}}}), ""
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
    def workspace(self, toml):
        root = Path(tempfile.mkdtemp())
        (root / "CLAUDE.md").write_text(
            "## Coordinator\n- User: Robin\n- Timezone: America/Chicago\n"
            "- Daily log dir: `daily/`\n- Tracker: `daily/<date>-tracker.md`\n"
            "- Settings: `chief-of-stuff.toml`\n- Backlog: GitHub issues; repo o/app\n")
        (root / "chief-of-stuff.toml").write_text(toml)
        return root

    def run_main(self, root, *args, gh):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = ma.main(["--root", str(root), *args], gh=gh)
        return code, out.getvalue(), err.getvalue()

    OPT_IN = '[workflow]\nmerge_owner = "approval"\napprover = "robin"\n'









def request(**kw):
    """A request as merge-ready's readers fill it: the head pipeline's id and sha, and the unresolved thread count (#419)."""
    base = dict(number=1, title="t", url="u", head=HEAD, approval="approved", pipeline="passed", mergeable="yes",
                approver="robin", pipeline_id="9001", pipeline_sha=HEAD, threads=0, draft=False, merge_state="CLEAN")
    return ma.Request(**{**base, **kw})


class MergeHeadTest(unittest.TestCase):
    def test_failed_or_unreadable_github_state_reads_after_a_successful_write_are_unknown(self):
        for read, error in (((1, "", "rate limited"), "rate limited"),
                            ((2, "", ""), "gh pr view exited 2"),
                            ((0, "{}", ""), str(KeyError("state")))):
            with self.subTest(read=read):
                gh = mock.Mock(side_effect=[(0, SQUASH_ONLY, ""), (0, "", ""), read])  # #864: settings, merge, state
                self.assertEqual(ma.merge_head(request(), None, True, "o/app", gh=gh),
                                 (0, f"unknown: {error}"))
                self.assertEqual(gh.call_count, 3)

    def test_github_merges_with_a_method_the_repository_allows_never_a_preferred_one(self):
        # the owner, 2026-10-18: "we shouldn't prefer a merge strategy" (#864). Several allowed: GitHub's own order.
        for allowed, flag in (({"allow_merge_commit": True}, "--merge"), ({"allow_squash_merge": True}, "--squash"),
                              ({"allow_rebase_merge": True}, "--rebase"),
                              ({"allow_merge_commit": True, "allow_squash_merge": True, "allow_rebase_merge": True}, "--merge"),
                              ({"allow_squash_merge": True, "allow_rebase_merge": True}, "--squash")):
            with self.subTest(allowed=allowed):
                gh = FakeGh([], settings=json.dumps(allowed))
                self.assertEqual(ma.merge_head(request(), None, True, "o/app", gh=gh)[0], 0)
                merge, = gh.merges
                self.assertIn(flag, merge)
                self.assertEqual(sum(f in merge for f in ("--merge", "--squash", "--rebase")), 1)

    def test_no_allowed_or_unreadable_merge_settings_refuse_without_a_merge_call(self):
        for gh, why in ((FakeGh([], settings=json.dumps({"allow_merge_commit": False})), "allows no merge method"),
                        (FakeGh([], settings="not json"), "repository merge settings"),
                        (mock.Mock(return_value=(1, "", "rate limited")), "rate limited")):
            with self.subTest(why=why):
                code, result = ma.merge_head(request(), None, True, "o/app", gh=gh)
                self.assertEqual(code, 1)
                self.assertIn(why, result)
                calls = gh.calls if isinstance(gh, FakeGh) else [c.args[0] for c in gh.call_args_list]
                self.assertFalse(any(c[:2] == ["pr", "merge"] for c in calls))


class RequestVerdictTest(unittest.TestCase):
    """One place decides ready or not (#419: the coordinator wrote mergeable from the forge's own status, no pipeline read).
    Precedence (#419 review, #430): conflicts > draft > red > stale pipeline > running > no pipeline > unverified >
    findings open > blocked > ready. A state that was not read is None, never "" or 0, and None is not ready."""

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
            # blocked: GitHub's mergeStateStatus is anything but CLEAN; below findings (#430)
            ({"merge_state": "BLOCKED"}, "blocked"),
            ({"merge_state": "BEHIND"}, "blocked"),
            ({"merge_state": "DIRTY"}, "blocked"),
            ({"merge_state": "UNSTABLE"}, "blocked"),
            ({"merge_state": "HAS_HOOKS"}, "blocked"),
            ({"merge_state": "UNKNOWN"}, "blocked"),
            ({"merge_state": "BEHIND", "threads": 2}, "findings open"),
            ({"merge_state": "BLOCKED", "threads": 2}, "findings open"),
            ({"mergeable": "discussions_not_resolved"}, "blocked"),
            ({"mergeable": "discussions_not_resolved", "threads": 2}, "findings open"),
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



    def test_it_never_merges_and_never_says_merging_stays_the_users(self):
        gh = FakeGhGraphql(self.PRS)
        for args in ((), ("--merge", "32")):
            code, out, err = self.run_main(self.WORKER, *args, gh=gh)
            with self.subTest(args=args):
                self.assertEqual(gh.merges, [])
                self.assertNotIn("stays the user's", out + err)
                self.assertNotIn("merging on approval is off", out + err)
        self.assertEqual(code, 2, "--merge is refused for worker: the engine")
        self.assertIn("engine", err)

    def test_it_needs_no_approver(self):
        code, _, err = self.run_main(self.WORKER)
        self.assertEqual(code, 0, err)



class ApprovalModeKeepsItsRulesTest(unittest.TestCase):
    """approval mode keeps its own rule (the approver's approval on the head decides) and still never merges what is plainly not
    mergeable: #419 review R1(e), R5, R6, R11."""

    ROBIN = [review("robin", "APPROVED")]

    def run_main(self, prs, *args):
        gh = FakeGh(prs)
        main = MainTest()
        code, out, err = main.run_main(main.workspace(MainTest.OPT_IN), *args, gh=gh)
        return gh, code, out, err





class WorkerListingNeverNamesWhatIsNotReadyTest(unittest.TestCase):
    def test_a_draft_a_blocked_and_an_unverified_request_are_listed_but_never_named_ready(self):
        prs = [pr(51, []), pr(52, [], draft=True), pr(53, [], state="BEHIND"), pr(54, [])]
        gh = FakeGhGraphql(prs, [graphql_node(54, [False], more=True)])
        main = MainTest()
        code, out, err = main.run_main(main.workspace(WorkerMergeOwnerTest.WORKER), gh=gh)
        self.assertEqual(code, 0, err)
        lines = {l.split()[0]: l for l in out.splitlines() if l[:1] == "#"}
        self.assertEqual([lines[k].split()[1] for k in ("#51", "#52", "#53", "#54")], ["ready", "draft", "blocked", "unverified"])
        self.assertEqual([line.split()[0] for line in out.splitlines() if " ready " in line], ["#51"])
        self.assertNotRegex(out, r"owning worker to merge:.*#5[234]")


class SettingsTest(unittest.TestCase):
    def load(self, text):
        root = Path(tempfile.mkdtemp())
        (root / "s.toml").write_text(text)
        return st.load(root, "s.toml").workflow



if __name__ == "__main__":
    unittest.main()
