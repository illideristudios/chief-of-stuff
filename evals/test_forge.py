"""The one forge adapter (1.0 plan, M2): GitHub and GitLab issues, requests, CI and review state, stage write-back
(labels, or GitHub Project Status when configured), and the snapshots the store is rebuilt from. Writes preview by
default; a read that fails is an error, never an empty forge."""

from __future__ import annotations

import contextlib
import json
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "evals"))
sys.path.insert(0, str(ROOT / "scripts"))

import backlog  # noqa: E402
import forge  # noqa: E402
import kanban  # noqa: E402
import store  # noqa: E402
import test_merge_approved as tma  # noqa: E402  (module-qualified: don't re-collect its TestCases)
import test_merge_ready as tmr  # noqa: E402
import test_reconcile as tr  # noqa: E402
from evals.project_cli import validate as validate_project_cli  # noqa: E402
from settings import GitHubProject, Kanban  # noqa: E402

REPO = "example/app"
GH = backlog.GitHubBacklog(REPO)
GL = backlog.Backlog("https://gitlab.example.test", "team/app")
FLOW = Kanban(stages=("stage: ready", "stage: doing", "stage: review"), human_review_label="needs human",
              stage_map={"ready": 0, "doing": 1, "review": 2}, terminal_stages=("done",))
PROJECT = GitHubProject("example", 3, "Status", ("Ready", "Doing", "Review"))
BOARD = Kanban(FLOW.stages, FLOW.human_review_label, FLOW.stage_map, terminal_stages=FLOW.terminal_stages,
               github_project=PROJECT)
# Shared board column: exact rebuild must refuse, never pick the first stage.
SHARED = Kanban(stages=("stage: shared",), human_review_label="needs human",
                stage_map={"review": 0, "merge": 0}, terminal_stages=("done",))


def gh_issue(number, title, labels=(), state="OPEN"):
    return {"number": number, "title": title, "state": state, "labels": [{"name": n} for n in labels],
            "url": f"https://github.com/{REPO}/issues/{number}", "updatedAt": "", "closedAt": None, "body": "the body"}


class FakeGh(tma.FakeGhGraphql):
    """`gh` for issues, labels, projects and pull requests. Issue labels and project Status change on write, so a
    round-trip can be verified. `fail` names the `gh` subcommands (`("issue", "list")`) that answer an error."""

    def __init__(self, issues=(), prs=(), merged=(), bodies=None, fail=(), status=None, project=PROJECT):
        super().__init__([{**p, "body": (bodies or {}).get(p["number"], "")} for p in prs])
        self.issues = {i["number"]: i for i in issues}
        self.merged = [{**p, "body": (bodies or {}).get(p["number"], p.get("body", ""))} for p in merged]
        self.fail = set(fail)
        self.all_calls: list[list[str]] = []
        self.project = project
        self.status = dict(status or {})  # issue url -> Status option name
        self.present = set(self.status)

    def __call__(self, args, input_text=None):
        self.all_calls.append(list(args))
        validate_project_cli(args)
        if tuple(args[:2]) in self.fail:
            return 1, "", "rate limited"
        if args[0] == "api" and "--paginate" in args and any("/comments?" in arg for arg in args):
            return 0, "[]", ""
        if args[:2] == ["issue", "list"]:
            state = args[args.index("--state") + 1] if "--state" in args else "open"
            rows = [i for i in self.issues.values()
                    if state == "all" or (state == "open" and i["state"] == "OPEN")
                    or (state == "closed" and i["state"] == "CLOSED")]
            if "--search" in args:
                assert args[args.index("--search") + 1] == "sort:updated-desc"
                rows.sort(key=lambda i: i["updatedAt"], reverse=True)
            return 0, json.dumps(rows[:int(args[args.index("--limit") + 1])]), ""
        if args[:2] == ["issue", "view"]:
            if int(args[2]) not in self.issues:
                return 1, "", "Could not resolve to an issue"
            return 0, json.dumps(self.issues[int(args[2])]), ""
        if args[:2] == ["issue", "edit"]:
            row = self.issues[int(args[2])]
            names = [x["name"] for x in row["labels"]]
            add = args[args.index("--add-label") + 1].split(",") if "--add-label" in args else []
            remove = args[args.index("--remove-label") + 1].split(",") if "--remove-label" in args else []
            row["labels"] = [{"name": n} for n in [*(n for n in names if n not in remove), *add]]
            return 0, "", ""
        if args[:2] == ["label", "list"]:
            names = (*FLOW.stages, FLOW.human_review_label, *FLOW.terminal_stages)
            return 0, json.dumps([{"name": n} for n in names]), ""
        if args[:2] == ["issue", "create"]:
            self.issues[101] = gh_issue(101, args[args.index("--title") + 1])
            return 0, f"https://github.com/{REPO}/issues/101\n", ""
        if args[:2] == ["pr", "list"]:
            state = args[args.index("--state") + 1] if "--state" in args else "open"
            fields = args[args.index("--json") + 1].split(",")
            if state == "merged":
                assert "--search" in args, "merged PRs need search ordering by update recency"
                assert args[args.index("--search") + 1] == "sort:updated-desc"
                assert args[args.index("--limit") + 1] == str(forge.forge_review.REQUEST_LIMIT)
                rows = sorted(self.merged, key=lambda p: p.get("updatedAt", ""), reverse=True)
                rows = rows[:forge.forge_review.REQUEST_LIMIT]
            else:
                rows = self.prs
            return 0, json.dumps([{k: v for k, v in row.items() if k in fields} for row in rows]), ""
        if args[:2] == ["project", "field-list"]:
            opts = [*self.project.options, *FLOW.terminal_stages]
            return 0, json.dumps({"fields": [{"id": "PVTSSF_status", "name": self.project.field,
                                              "options": [{"id": f"option_{n}", "name": n} for n in opts]}]}), ""
        if args[:2] == ["project", "view"]:
            return 0, json.dumps({"id": "PVT_board"}), ""
        if args[:2] == ["project", "item-list"]:
            items = [{"id": f"PVTI_{u.rsplit('/', 1)[1]}", "content": {"url": u},
                      self.project.field[0].lower() + self.project.field[1:]: self.status.get(u)}
                     for u in sorted(self.present)]
            return 0, json.dumps({"items": items, "totalCount": len(items)}), ""
        if args[:2] == ["project", "item-add"]:
            url = args[args.index("--url") + 1]
            self.present.add(url)
            return 0, json.dumps({"id": f"PVTI_{url.rsplit('/', 1)[1]}"}), ""
        if args[:2] == ["project", "item-edit"]:
            assert args[args.index("--project-id") + 1] == "PVT_board"
            assert args[args.index("--field-id") + 1] == "PVTSSF_status"
            item_id = args[args.index("--id") + 1]
            url = next(u for u in self.present if f"PVTI_{u.rsplit('/', 1)[1]}" == item_id)
            if "--clear" in args:
                self.status.pop(url, None)
            else:
                option = args[args.index("--single-select-option-id") + 1]
                self.status[url] = next(n for n in (*self.project.options, *FLOW.terminal_stages)
                                        if f"option_{n}" == option)
            return 0, "{}", ""
        return super().__call__(args, input_text)


def github():
    """Issue 3 in review with an approved, green pull request that closes it; issue 4 being worked, with none."""
    return FakeGh(issues=[gh_issue(3, "Fix the parser", ["stage: review", "bug"]), gh_issue(4, "Add a flag", ["stage: doing"])],
                  prs=[tma.pr(21, [tma.review("robin", "APPROVED")])], bodies={21: "Closes #3"})


class KeyTest(unittest.TestCase):
    def test_the_issue_key_names_the_forge_the_host_the_repo_and_the_number(self):
        self.assertEqual(forge.Forge(GH).key(7), "github:example/app#7")
        self.assertEqual(forge.Forge(GL).key(5), "gitlab:gitlab.example.test/team/app#5")

    def test_mark_names_a_github_pull_or_gitlab_merge_request(self):
        self.assertEqual(forge.Forge(GH).mark(21), "#21")
        self.assertEqual(forge.Forge(GL).mark(5), "!5")


class GitHubTest(unittest.TestCase):
    def test_snapshot_is_one_issue_row_with_its_stage_mark(self):
        row = forge.Forge(GH, approver="robin", kanban=FLOW, gh=github()).snapshot("github:example/app#3")
        self.assertEqual(row, {
            "issue_key": "github:example/app#3", "title": "Fix the parser", "forge_state": "open",
            "stage": "review", "human_review": False, "review_state": "approved", "merge_state": "ready",
            "stage_mark": "stage: review",
        })

    def test_mark_for_stage_returns_the_literal_terminal_label(self):
        f = forge.Forge(GH, kanban=FLOW, gh=github())
        self.assertEqual(f.mark_for_stage("done"), "done")
        self.assertEqual(f.mark_for_stage("review"), "stage: review")
        self.assertEqual(f.mark_for_stage("unknown"), "")

    def test_merged_requests_union_sidebar_links_with_body_and_filter_other_repositories(self):
        from evals.test_forge_review import closing_issue
        gh = FakeGh(issues=[gh_issue(n, "Closed", state="CLOSED") for n in range(3, 9)],
                    merged=[{"number": 21, "body": "Closes #3; fixes #4",
                             "closingIssuesReferences": [closing_issue(3), closing_issue(5, "Example/App"),
                                                         closing_issue(6, "other/app"), closing_issue(7, "example/other")]},
                            {"number": 22, "body": "", "closingIssuesReferences": [closing_issue(8)]}])
        got = forge.Forge(GH, gh=gh).snapshots()
        self.assertEqual([s["merge_state"] for s in got], ["merged", "merged", "merged", "", "", "merged"])

    def test_snapshots_report_configured_human_review_label(self):
        gh = FakeGh(issues=[gh_issue(1, "held", ["stage: review", "needs human"]),
                            gh_issue(2, "free", ["stage: review", "other hold"])])
        snapshots = forge.Forge(GH, kanban=FLOW, gh=gh).snapshots()
        self.assertEqual([row.get("human_review") for row in snapshots], [True, False])

    def test_snapshot_hold_membership_matches_case_insensitive_publication(self):
        gh = FakeGh(issues=[gh_issue(1, "held", ["stage: review", "NEEDS HUMAN"])])
        adapter = forge.Forge(GH, kanban=FLOW, gh=gh)
        self.assertTrue(adapter.snapshots()[0]["human_review"])
        self.assertTrue(adapter.snapshot(adapter.key(1))["human_review"])

    def test_snapshot_bounds_closed_history_by_update_recency(self):
        limit = forge.forge_review.REQUEST_LIMIT
        closed = [gh_issue(n, "Old", state="CLOSED") for n in range(1, backlog.GH_LIMIT + 2)]
        recent = closed[-1]["number"]
        closed[-1]["updatedAt"] = "2026-10-11"
        gh = FakeGh(issues=[*closed, gh_issue(6000, "Open")],
                    merged=[{"number": 7000, "body": f"Closes #{recent}"}])
        got = forge.Forge(GH, gh=gh).snapshots()
        self.assertEqual(len(got), limit + 1)
        self.assertEqual(got[-2]["issue_key"], f"github:example/app#{recent}")
        self.assertEqual(got[-2]["merge_state"], "merged")
        self.assertEqual(got[-1]["forge_state"], "open")
        lists = [a for a in gh.all_calls if a[:2] == ["issue", "list"]]
        self.assertEqual([a[a.index("--state") + 1] for a in lists], ["open", "closed"])
        self.assertEqual(lists[1][lists[1].index("--limit") + 1], str(limit))
        self.assertEqual(lists[1][lists[1].index("--search") + 1], "sort:updated-desc")

    def test_merged_window_includes_a_recently_updated_long_lived_pr(self):
        limit = forge.forge_review.REQUEST_LIMIT
        merged = [{"number": n, "createdAt": "2025-01-01", "updatedAt": "2025-06-01"}
                  for n in range(1, limit + 2)]
        merged[0].update(createdAt="2020-01-01", updatedAt="2026-10-11", body="Closes #3")
        merged[-1].update(updatedAt="2024-01-01", body="Closes #4")
        gh = FakeGh(issues=[gh_issue(3, "Recent merge", state="CLOSED"),
                            gh_issue(4, "Outside window", state="CLOSED")], merged=merged)
        got = forge.Forge(GH, gh=gh).snapshots()
        self.assertEqual([s["merge_state"] for s in got], ["merged", ""])

    def test_merged_request_does_not_mark_an_open_issue_as_merged(self):
        gh = FakeGh(issues=[gh_issue(3, "Still open"), gh_issue(4, "Closed", state="CLOSED")],
                    merged=[tma.pr(21, [])], bodies={21: "Closes #3 and fixes #4"})
        got = forge.Forge(GH, gh=gh).snapshots()
        self.assertEqual([(s["forge_state"], s["merge_state"]) for s in got],
                         [("open", ""), ("closed", "merged")])

    def test_changes_requested_snapshot_prevents_merge_after_a_clean_engine_review(self):
        for oid in (tma.HEAD, tma.OLD):
            with self.subTest(oid=oid):
                reviews = [tma.review("robin", "CHANGES_REQUESTED", oid=oid, at="2026-09-26T02:00:00Z"),
                           tma.review("robin", "APPROVED")]
                gh = FakeGh(issues=[gh_issue(3, "x", ["stage: review"])],
                            prs=[tma.pr(21, reviews)], bodies={21: "Closes #3"})
                snapshot, = forge.Forge(GH, approver="robin", kanban=FLOW, gh=gh).snapshots()
                self.assertEqual((snapshot["review_state"], snapshot["merge_state"]), ("changes_requested", "ready"))
                item = store.WorkItem(**{k: v for k, v in snapshot.items() if k != "stage_mark"})
                for stage in ("review", "merge"):
                    item.stage = stage
                    self.assertEqual(tr.reconcile.plan([item],
                                     {item.issue_key: [tr.Segment("review")]}, tr.policies(), {}, 0, tr.NOW,
                                     {item.issue_key: []}),
                                     [tr.reconcile.Wait(item.issue_key, "changes requested on the forge")])

    def test_approval_consumers_keep_their_blockers_and_latest_review_rules(self):
        for reviews, approval, blockers in (
                ([], "none", ["no approval from robin"]),
                ([tma.review("robin", "APPROVED")], "approved", []),
                ([tma.review("robin", "APPROVED", oid=tma.OLD)], "stale", ["robin approved an earlier commit"]),
                ([tma.review("robin", "CHANGES_REQUESTED")], "changes_requested", ["no approval from robin"]),
                ([tma.review("robin", "CHANGES_REQUESTED"),
                  tma.review("robin", "COMMENTED", at="2026-09-26T02:00:00Z"),
                  tma.review("peer", "APPROVED", at="2026-09-26T03:00:00Z")],
                 "changes_requested", ["no approval from robin"]),
                ([tma.review("robin", "CHANGES_REQUESTED"),
                  tma.review("robin", "APPROVED", at="2026-09-26T02:00:00Z")], "approved", []),
                ([tma.review("robin", "CHANGES_REQUESTED"),
                  tma.review("robin", "DISMISSED", at="2026-09-26T02:00:00Z")], "none", ["no approval from robin"])):
            with self.subTest(reviews=reviews):
                request, = forge.Forge(GH, approver="robin", gh=FakeGh(prs=[tma.pr(21, reviews)])).requests()
                self.assertEqual((request.approval, tma.ma.blocked(request)), (approval, blockers))

    def test_snapshots_carry_identity_stage_review_and_merge_state(self):
        got = forge.Forge(GH, approver="robin", kanban=FLOW, gh=github()).snapshots()
        self.assertEqual(got, [
            {"issue_key": "github:example/app#3", "title": "Fix the parser", "forge_state": "open", "stage": "review",
             "human_review": False, "review_state": "approved", "merge_state": "ready",
             "stage_mark": "stage: review"},
            {"issue_key": "github:example/app#4", "title": "Add a flag", "forge_state": "open", "stage": "doing",
             "human_review": False, "review_state": "", "merge_state": "", "stage_mark": "stage: doing"},
        ])

    def test_what_was_not_configured_is_left_out_so_a_sync_never_blanks_it(self):
        got = forge.Forge(GH, gh=github()).snapshots()
        self.assertEqual([sorted(s) for s in got],
                         [["forge_state", "issue_key", "merge_state", "title"]] * 2)

    def test_a_failed_read_is_an_error_never_an_empty_forge(self):
        for broken in (("issue", "list"), ("pr", "list"), ("api", "graphql")):
            with self.subTest(broken=broken):
                gh = github()
                gh.fail = {broken}
                with self.assertRaises(forge.ForgeError):
                    forge.Forge(GH, approver="robin", kanban=FLOW, gh=gh).snapshots()

    def test_a_request_read_past_one_page_is_still_a_whole_answer(self):
        # #425: forge used to refuse at REQUEST_LIMIT; paging must return every open request.
        n = forge.backlog.PER_PAGE + 1
        gh = FakeGh(issues=[gh_issue(3, "x")], prs=[tma.pr(i, []) for i in range(1, n + 1)],
                    bodies={i: "Closes #3" for i in range(1, n + 1)})
        got = forge.Forge(GH, gh=gh).requests()
        self.assertEqual(len(got), n)

    def test_a_request_read_that_reaches_the_cap_is_an_error_never_a_whole_answer(self):
        gh = FakeGh(issues=[gh_issue(3, "x")], prs=[tma.pr(n, []) for n in range(1, forge.backlog.GH_LIMIT + 1)])
        with self.assertRaisesRegex(forge.ForgeError, "or more|partial"):
            forge.Forge(GH, gh=gh).snapshots()

    def test_reads_use_gh_subcommands_and_graphql_only_for_what_no_subcommand_answers(self):
        gh = github()
        forge.Forge(GH, approver="robin", kanban=FLOW, gh=gh).snapshots()
        self.assertEqual(sorted({tuple(c[:2]) for c in gh.all_calls}), [("api", "graphql"), ("issue", "list"), ("pr", "list")])

    def test_one_issue_reads_with_its_body(self):
        got = forge.Forge(GH, gh=github()).issue(3)
        self.assertEqual((got.issue.iid, got.issue.body), (3, "the body"))

    def test_the_store_is_rebuilt_from_the_forge(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "store.db"
            store.migrate(path)
            db = store.Store(path)
            self.addCleanup(db.engine.dispose)
            db.sync_from_forge(forge.Forge(GH, approver="robin", kanban=FLOW, gh=github()).snapshots())
            self.assertEqual([(i.issue_key, i.stage, i.review_state, i.merge_state) for i in db.items()],
                             [("github:example/app#3", "review", "approved", "ready"), ("github:example/app#4", "doing", "", "")])

    def test_the_store_rebuilds_closed_issues_and_merged_requests(self):
        # Was open-only; a dropped store must recover completed work (#627 / M1.3).
        gh = FakeGh(issues=[gh_issue(3, "Fix the parser", ["stage: review"], state="CLOSED"),
                            gh_issue(4, "Add a flag", ["stage: doing"])],
                    prs=[], merged=[tma.pr(21, [tma.review("robin", "APPROVED")])],
                    bodies={21: "Closes #3"})
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "store.db"
            store.migrate(path)
            db = store.Store(path)
            self.addCleanup(db.engine.dispose)
            db.sync_from_forge(forge.Forge(GH, approver="robin", kanban=FLOW, gh=gh).snapshots())
            self.assertEqual([(i.issue_key, i.forge_state, i.stage, i.merge_state) for i in db.items()],
                             [("github:example/app#3", "closed", "review", "merged"),
                              ("github:example/app#4", "open", "doing", "")])


class StageWriteBackTest(unittest.TestCase):
    def edits(self, gh):
        return [c for c in gh.all_calls if c[:2] == ["issue", "edit"]]

    def test_release_hold_is_guarded_previewable_and_idempotent_on_both_forges(self):
        for host in ("github", "gitlab"):
            for required in (False, True):
                with self.subTest(host=host, required=required):
                    config = replace(FLOW, hold_stages=("doing",) if required else ())
                    labels = ["stage: doing", "needs human", "bug"]
                    if host == "github":
                        gh = FakeGh(issues=[gh_issue(4, "Retry", labels)])
                        f = forge.Forge(GH, kanban=config, gh=gh)
                        read_labels = lambda: [x["name"] for x in gh.issues[4]["labels"]]
                        patch = contextlib.nullcontext()
                    else:
                        issues = {4: gl_issue(4, "Retry", labels)}
                        f = forge.Forge(GL, kanban=config, token="t")
                        read_labels = lambda: issues[4]["labels"]
                        patch = mock.patch.object(backlog, "_call", gitlab_call(issues, {}))
                    with patch:
                        preview = f.write_stage(4, "doing", expected="doing", release_hold=True)
                        self.assertTrue(preview.ok, preview.error)
                        self.assertEqual(read_labels(), labels)
                        for _ in range(2):
                            got = f.write_stage(4, "doing", expected="doing", release_hold=True, commit=True)
                            self.assertTrue(got.done, got.error)
                            self.assertEqual(set(read_labels()), {"stage: doing", "bug"} |
                                             ({"needs human"} if required else set()))

    def test_release_hold_refuses_missing_prior_and_stage_drift(self):
        for host in ("github", "gitlab"):
            for kwargs in ({}, {"expected": "doing"}, {"expected": "review"},
                           {"expected": "doing", "initialize": True}):
                with self.subTest(host=host, kwargs=kwargs):
                    labels = ["stage: review", "needs human"]
                    if host == "github":
                        gh = FakeGh(issues=[gh_issue(4, "Retry", labels)])
                        f = forge.Forge(GH, kanban=FLOW, gh=gh)
                        read_labels = lambda: [x["name"] for x in gh.issues[4]["labels"]]
                        patch = contextlib.nullcontext()
                    else:
                        issues = {4: gl_issue(4, "Retry", labels)}
                        f = forge.Forge(GL, kanban=FLOW, token="t")
                        read_labels = lambda: issues[4]["labels"]
                        patch = mock.patch.object(backlog, "_call", gitlab_call(issues, {}))
                    with patch:
                        got = f.write_stage(4, "doing", release_hold=True, commit=True, **kwargs)
                    self.assertFalse(got.ok)
                    self.assertEqual(read_labels(), labels)

    def test_a_stage_write_previews_by_default(self):
        gh = github()
        got = forge.Forge(GH, kanban=FLOW, gh=gh).write_stage(4, "review", expected="doing")
        self.assertEqual((got.ok, got.done, got.delta.add, got.delta.remove), (True, False, ("stage: review",), ("stage: doing",)))
        self.assertEqual(self.edits(gh), [])

    def test_a_committed_stage_write_swaps_the_stage_label_and_keeps_the_rest(self):
        gh = github()
        got = forge.Forge(GH, kanban=FLOW, gh=gh).write_stage(3, "doing", expected="review", commit=True)
        self.assertTrue(got.done, got.error)
        self.assertEqual([x["name"] for x in gh.issues[3]["labels"]], ["bug", "stage: doing"])

    def test_a_configured_project_writes_status_not_labels(self):
        # Encoded the pre-decision bug: forge stripped github_project and wrote labels anyway.
        gh = FakeGh(issues=[gh_issue(4, "Add a flag")], status={})
        got = forge.Forge(GH, kanban=BOARD, gh=gh).write_stage(4, "doing", initialize=True, commit=True)
        self.assertTrue(got.done, got.error)
        self.assertTrue(any(c[:2] == ["project", "item-edit"] for c in gh.all_calls), gh.all_calls)
        self.assertEqual(gh.status[f"https://github.com/{REPO}/issues/4"], "Doing")
        self.assertNotIn("stage: doing", [x["name"] for x in gh.issues[4]["labels"]])

    def test_snapshots_read_stage_from_project_status(self):
        url = gh_issue(4, "Add a flag")["url"]
        gh = FakeGh(issues=[gh_issue(4, "Add a flag")], status={url: "Doing"})
        got = forge.Forge(GH, kanban=BOARD, gh=gh).snapshots()
        self.assertEqual(got, [{"issue_key": "github:example/app#4", "title": "Add a flag", "forge_state": "open",
                                "stage": "doing", "human_review": False, "stage_mark": "stage: doing",
                                "merge_state": ""}])

    def test_snapshot_reads_stage_from_project_status(self):
        # Merge recheck uses snapshot(); Project mode stores the stage in Status, not labels.
        url = gh_issue(4, "Add a flag")["url"]
        gh = FakeGh(issues=[gh_issue(4, "Add a flag")], status={url: "Doing"})
        row = forge.Forge(GH, kanban=BOARD, gh=gh).snapshot("github:example/app#4")
        self.assertEqual(row["stage"], "doing")
        self.assertEqual(row["stage_mark"], "stage: doing")

    def test_snapshots_refuse_partial_project_fields_before_store_sync(self):
        issue = gh_issue(4, "Project issue", ["stage: ready"])
        gh = FakeGh(issues=[issue], status={issue["url"]: "Doing"})

        def partial(args):
            if args[:2] == ["project", "field-list"]:
                return 0, json.dumps({"fields": [{"name": "Other"}] * 100, "totalCount": 101}), ""
            if args[:2] == ["project", "item-list"]:
                # gh drops Status when its field definition is beyond the first page.
                return 0, json.dumps({"items": [{"content": {"url": issue["url"]}}], "totalCount": 1}), ""
            return gh(args)

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "store.db"
            store.migrate(path)
            db = store.Store(path)
            self.addCleanup(db.engine.dispose)
            db.upsert_item("github:example/app#4", stage="doing")
            with self.assertRaisesRegex(forge.ForgeError, "project status: .*field list was truncated"):
                db.sync_from_forge(forge.Forge(GH, kanban=BOARD, gh=partial).snapshots())
            self.assertEqual(db.items()[0].stage, "doing")

    def test_project_snapshots_match_issue_urls_case_insensitively(self):
        for repo, item_repo in (("Example/App", "example/app"), ("example/app", "Example/App")):
            with self.subTest(repo=repo, item_repo=item_repo):
                url = f"https://github.com/{item_repo}/issues/7"
                gh = FakeGh(issues=[gh_issue(7, "Add a flag")], status={url: "Doing"})
                snapshot, = forge.Forge(backlog.GitHubBacklog(repo), kanban=BOARD, gh=gh).snapshots()
                self.assertEqual(snapshot["stage"], "doing")

    def test_project_snapshots_fall_back_to_legacy_labels_without_status(self):
        for present in (False, True):
            for label, stage in (("stage: doing", "doing"), ("done", "done")):
                with self.subTest(present=present, label=label):
                    issue = gh_issue(4, "Legacy issue", [label, "needs human", "bug"])
                    gh = FakeGh(issues=[issue], status={issue["url"]: None} if present else {})
                    snapshot, = forge.Forge(GH, kanban=BOARD, gh=gh).snapshots()
                    self.assertEqual(snapshot["stage"], stage)

    def test_project_status_takes_precedence_over_legacy_labels_in_snapshots(self):
        issue = gh_issue(4, "x", ["stage: ready"])
        gh = FakeGh(issues=[issue], status={issue["url"]: "Doing"})
        snapshot, = forge.Forge(GH, kanban=BOARD, gh=gh).snapshots()
        self.assertEqual(snapshot["stage"], "doing")

    def test_project_write_migrates_legacy_labels_and_round_trips(self):
        for expected in ("doing", None):
            with self.subTest(expected=expected):
                gh = FakeGh(issues=[gh_issue(4, "Legacy issue", ["stage: doing", "bug"])])
                f = forge.Forge(GH, kanban=BOARD, gh=gh)
                got = f.write_stage(4, "review", expected=expected, initialize=expected is None, commit=True)
                self.assertTrue(got.done, got.error)
                self.assertEqual(f.snapshots()[0]["stage"], "review")
                self.assertEqual(gh.issues[4]["labels"], [{"name": "bug"}])

    def test_unknown_project_status_option_is_an_error(self):
        url = gh_issue(4, "x")["url"]
        gh = FakeGh(issues=[gh_issue(4, "x")], status={url: "Invented"})
        with self.assertRaisesRegex(forge.ForgeError, "Invented"):
            forge.Forge(GH, kanban=BOARD, gh=gh).snapshots()

    def test_a_shared_label_reads_as_its_first_stage_so_snapshots_still_load(self):
        # The documented example shares columns; the engine keeps its stored stage within the column (#651).
        gh = FakeGh(issues=[gh_issue(4, "x", ["stage: shared"])])
        self.assertEqual(forge.Forge(GH, kanban=SHARED, gh=gh).snapshots()[0]["stage"], "review")

    def test_stage_round_trips_through_project_status_including_terminal(self):
        gh = FakeGh(issues=[gh_issue(4, "Add a flag")], status={})
        f = forge.Forge(GH, kanban=BOARD, gh=gh)
        self.assertTrue(f.write_stage(4, "review", initialize=True, commit=True).done, "init")
        self.assertEqual(f.snapshots()[0]["stage"], "review")
        self.assertTrue(f.write_stage(4, "done", expected="review", commit=True).done)
        self.assertEqual(f.snapshots()[0]["stage"], "done")

    def test_stage_round_trips_through_labels_including_terminal(self):
        gh = FakeGh(issues=[gh_issue(4, "Add a flag")])
        f = forge.Forge(GH, kanban=FLOW, gh=gh)
        self.assertTrue(f.write_stage(4, "review", initialize=True, commit=True).done)
        self.assertEqual(f.snapshots()[0]["stage"], "review")
        self.assertTrue(f.write_stage(4, "done", expected="review", commit=True).done)
        self.assertEqual(f.snapshots()[0]["stage"], "done")

    def test_snapshots_include_closed_issues_and_merged_requests(self):
        gh = FakeGh(issues=[gh_issue(3, "Fix the parser", ["stage: review"], state="CLOSED"),
                            gh_issue(4, "Add a flag", ["stage: doing"])],
                    prs=[], merged=[tma.pr(21, [])], bodies={21: "Closes #3"})
        got = forge.Forge(GH, kanban=FLOW, gh=gh).snapshots()
        self.assertEqual([(s["issue_key"], s["forge_state"], s["stage"], s["merge_state"]) for s in got],
                         [("github:example/app#3", "closed", "review", "merged"),
                          ("github:example/app#4", "open", "doing", "")])

    def test_a_closed_issue_stays_merged_when_an_open_follow_up_cites_it(self):
        gh = FakeGh(issues=[gh_issue(3, "Fix the parser", ["stage: review"], state="CLOSED")],
                    prs=[tma.pr(22, [])], merged=[tma.pr(21, [])], bodies={21: "Closes #3", 22: "Closes #3"})
        self.assertEqual(forge.Forge(GH, kanban=FLOW, gh=gh).snapshots()[0]["merge_state"], "merged")

    def test_malformed_merged_request_json_is_a_forge_error(self):
        for out in ('{"number": 1}', '[1, 2]', '[{"number": 1, "body": 5}]'):
            with self.subTest(out=out):
                gh = FakeGh(issues=[gh_issue(3, "x", ["stage: doing"])], prs=[])

                def call(args, input_text=None, gh=gh, out=out):
                    if args[:2] == ["pr", "list"] and "merged" in args:
                        return 0, out, ""
                    return gh(args, input_text)

                with self.assertRaisesRegex(forge.ForgeError, "merged requests"):
                    forge.Forge(GH, kanban=FLOW, gh=call).snapshots()

    def test_a_long_merged_history_does_not_refuse_the_snapshot(self):
        # Merged requests only annotate closed issues; a busy repo has far more than one read lists.
        many = [tma.pr(n, []) for n in range(1, forge.forge_review.REQUEST_LIMIT + 1)]
        gh = FakeGh(issues=[gh_issue(3, "x", ["stage: doing"])], prs=[], merged=many)
        self.assertEqual(forge.Forge(GH, kanban=FLOW, gh=gh).snapshots()[0]["merge_state"], "")

    def test_a_new_issue_takes_its_first_stage_but_a_moved_card_is_not_overwritten(self):
        gh = FakeGh(issues=[gh_issue(5, "new"), gh_issue(6, "moved by hand", ["stage: review"])])
        f = forge.Forge(GH, kanban=FLOW, gh=gh)
        self.assertTrue(f.write_stage(5, "ready", initialize=True, commit=True).done)
        self.assertIn("reconcile", f.write_stage(6, "doing", initialize=True, commit=True).error)

    def test_no_stage_labels_configured_is_refused(self):
        self.assertIn("[kanban]", forge.Forge(GH, gh=github()).write_stage(3, "review").error)


class CreateTest(unittest.TestCase):
    def test_creating_an_issue_previews_by_default(self):
        gh = github()
        got = forge.Forge(GH, gh=gh).create_issue("A new thing")
        self.assertEqual((got.done, got.what), (False, "A new thing"))
        self.assertEqual(gh.all_calls, [])

    def test_a_committed_create_files_it_with_its_native_relationships(self):
        gh = github()
        got = forge.Forge(GH, gh=gh).create_issue("A new thing", body="why", parent=3, blocked_by=(4,), commit=True)
        self.assertEqual((got.done, got.iid), (True, 101))
        self.assertEqual(gh.all_calls[0][-4:], ["--parent", "3", "--blocked-by", "4"])


def gitlab_call(issues, mrs, fail="", merged=None):
    """GitLab's REST answers: the issue list and one issue, labels, and merge requests with their approvals and discussions."""
    calls = []
    merged = merged or {}

    def call(method, url, token, timeout, payload=None):
        calls.append((method, url, payload))
        parts = urlsplit(url)
        path = parts.path.split("/api/v4/projects/team%2Fapp", 1)[1]
        query = parse_qs(parts.query)
        if fail and path.startswith(fail):
            return None, {}, "HTTP 500 from GitLab"
        if path == "/issues":
            state = (query.get("state") or ["opened"])[0]
            rows = [issues[n] for n in sorted(issues)
                    if state == "all" or issues[n]["state"] == state]
            if query.get("order_by") == ["updated_at"]:
                assert query.get("sort") == ["desc"]
                rows.sort(key=lambda i: i["updated_at"], reverse=True)
            size = int(query["per_page"][0])
            page = int(query.get("page", ["1"])[0])
            start = (page - 1) * size
            return rows[start:start + size], {"x-next-page": str(page + 1) if start + size < len(rows) else ""}, ""
        if path.startswith("/issues/"):
            row = issues[int(path.split("/")[2])]
            if method == "PUT":
                drop = set((payload.get("remove_labels") or "").split(","))
                row["labels"] = [x for x in row["labels"] if x not in drop] + [
                    x for x in (payload.get("add_labels") or "").split(",") if x]
            return row, {}, ""
        if path == "/labels":
            return [{"name": n} for n in (*FLOW.stages, *FLOW.terminal_stages)], {}, ""
        if path == "/merge_requests":
            state = (query.get("state") or ["opened"])[0]
            pool = merged if state == "merged" else mrs
            numbers = list(pool)
            if state == "merged":
                assert query.get("order_by") == ["updated_at"], "merged MRs need ordering by update recency"
                assert query.get("sort") == ["desc"]
                assert query.get("per_page") == [str(forge.forge_review.REQUEST_LIMIT)]
                numbers.sort(key=lambda i: pool[i].get("updated_at", ""), reverse=True)
                numbers = numbers[:forge.forge_review.REQUEST_LIMIT]
            else:
                size, page = int(query.get("per_page", ["100"])[0]), int(query.get("page", ["1"])[0])
                numbers = sorted(numbers)[(page - 1) * size:page * size]
            return [{"iid": i, "description": pool[i].get("description", "")} for i in numbers], {}, ""
        iid = int(path.split("/")[2])
        if path.endswith("/approvals"):
            return {"approved_by": [{"user": {"username": u}} for u in mrs[iid]["approved_by"]]}, {}, ""
        if path.endswith("/discussions"):
            return [], {}, ""
        return {"iid": iid, "title": "MR", "web_url": "u", "sha": tma.HEAD, "description": mrs[iid]["description"],
                "has_conflicts": False, "detailed_merge_status": "mergeable", "draft": False,
                "head_pipeline": {"id": 7000 + iid, "status": "success", "sha": tma.HEAD}}, {}, ""
    call.calls = calls
    return call


def gl_issue(iid, title, labels=(), description="", state="opened"):
    return {"iid": iid, "title": title, "state": state, "labels": list(labels), "web_url": "", "updated_at": "", "closed_at": "",
            "description": description}


class ClosingRequestTest(unittest.TestCase):
    def test_only_a_request_closing_solely_the_assigned_issue_is_selected(self):
        for home in (GH, GL):
            f = forge.Forge(home, token="t")
            for closes in (("#3", "other/repo#99"), ("other/repo#99", "#3"), ("o/app#3",), ("other/repo#3",),
                           ("https://github.com/o/app/issues/3",), ("#3", "#4"), ()):
                with self.subTest(home=home, closes=closes):
                    with self.assertRaises(forge.ForgeError):
                        f.closing_request(f.key(3), [SimpleNamespace(number=21, closes=closes)])
            got = f.closing_request(f.key(3), [SimpleNamespace(number=21, closes=("#3",)),
                                               SimpleNamespace(number=22, closes=("#3",)),
                                               SimpleNamespace(number=23, closes=("#4",))])
            self.assertEqual(got.number, 22)

    def test_snapshots_and_merges_ignore_qualified_refs_instead_of_crashing_or_matching(self):
        issues = {3: gl_issue(3, "Closed", state="closed"), 4: gl_issue(4, "Open")}
        call = gitlab_call(issues, {5: {"approved_by": [], "description": "Closes other/app#4"}},
                           merged={6: {"description": "Closes other/app#4"}})
        with mock.patch.object(backlog, "_call", call):
            self.assertEqual([s["merge_state"] for s in forge.Forge(GL, token="t").snapshots()], ["", ""])


class SetStageInitializeTest(unittest.TestCase):
    def test_an_empty_or_missing_expected_stage_never_initializes(self):
        for expected in (None, ""):
            with self.subTest(expected=expected), mock.patch.object(forge, "_sync_github") as sync:
                forge.Forge(GH, kanban=FLOW, gh=FakeGh(issues=[])).write_stage(4, "review", expected=expected, commit=True)
            self.assertIs(sync.call_args.kwargs["initialize"], False)

    def test_initialize_is_passed_through_when_the_caller_sets_it(self):
        gh = FakeGh(issues=[gh_issue(4, "Add a flag")])
        self.assertTrue(forge.Forge(GH, kanban=FLOW, gh=gh).write_stage(4, "review", initialize=True, commit=True).done)
        gh = FakeGh(issues=[gh_issue(4, "Add a flag")])
        self.assertIn("--initialize", forge.Forge(GH, kanban=FLOW, gh=gh).write_stage(4, "review", commit=True).error)


class GitLabTest(unittest.TestCase):
    def test_open_and_merged_requests_scope_url_and_project_closing_references(self):
        for ref, expected in (("https://gitlab.example.test/Team/App/-/issues/3", True),
                              ("https://gitlab.example.test/team/other/-/issues/3", False),
                              ("https://other.example.test/team/app/-/issues/3", False),
                              ("Team/App#3", True), ("other/app#3", False), ("#3", True)):
            with self.subTest(ref=ref):
                call = gitlab_call({3: gl_issue(3, "Closed", state="closed")},
                                   {5: {"approved_by": [], "description": f"Closes {ref}"}},
                                   merged={6: {"description": f"Closes {ref}"}})
                with mock.patch.object(backlog, "_call", call):
                    got = self.forge.requests()[0].closes
                    self.assertEqual(got == ("#3",), expected)
                    self.assertEqual(self.forge.snapshots()[0]["merge_state"], "merged" if expected else "")

    def test_snapshot_bounds_closed_history_without_following_all_pages(self):
        limit = forge.forge_review.REQUEST_LIMIT
        issues = {n: gl_issue(n, "Old", state="closed") for n in range(1, backlog.GH_LIMIT + 2)}
        recent = max(issues)
        issues[recent]["updated_at"] = "2026-10-11"
        issues[6000] = gl_issue(6000, "Open")
        call = gitlab_call(issues, {}, merged={7000: {"description": f"Closes #{recent}"}})
        with mock.patch.object(backlog, "_call", call):
            got = forge.Forge(GL, token="t").snapshots()
        self.assertEqual(len(got), limit + 1)
        self.assertEqual(got[-2]["issue_key"], f"gitlab:gitlab.example.test/team/app#{recent}")
        self.assertEqual(got[-2]["merge_state"], "merged")
        self.assertEqual(got[-1]["forge_state"], "open")
        queries = [parse_qs(urlsplit(url).query) for _, url, _ in call.calls
                   if urlsplit(url).path.endswith("/issues")]
        self.assertEqual([q["state"] for q in queries], [["opened"], ["closed"]])
        self.assertEqual(queries[1]["order_by"], ["updated_at"])
        self.assertEqual(queries[1]["sort"], ["desc"])
        self.assertEqual(queries[1]["per_page"], [str(limit)])

    def test_merged_window_includes_a_recently_updated_long_lived_mr(self):
        limit = forge.forge_review.REQUEST_LIMIT
        merged = {n: {"created_at": "2025-01-01", "updated_at": "2025-06-01"}
                  for n in range(1, limit + 2)}
        merged[1].update(created_at="2020-01-01", updated_at="2026-10-11", description="Closes #3")
        merged[limit + 1].update(updated_at="2024-01-01", description="Closes #4")
        issues = {3: gl_issue(3, "Recent merge", state="closed"),
                  4: gl_issue(4, "Outside window", state="closed")}
        with mock.patch.object(backlog, "_call", gitlab_call(issues, {}, merged=merged)):
            got = forge.Forge(GL, token="t").snapshots()
        self.assertEqual([s["merge_state"] for s in got], ["merged", ""])

    def test_merged_request_does_not_mark_an_open_issue_as_merged(self):
        issues = {3: gl_issue(3, "Still open"), 4: gl_issue(4, "Closed", state="closed")}
        with mock.patch.object(backlog, "_call", gitlab_call(issues, {}, merged={5: {"description": "Closes #3 and fixes #4"}})):
            got = forge.Forge(GL, token="t").snapshots()
        self.assertEqual([(s["forge_state"], s["merge_state"]) for s in got],
                         [("open", ""), ("closed", "merged")])

    def setUp(self):
        self.call = gitlab_call({3: gl_issue(3, "Fix the parser", ["stage: review"], "what to fix"), 4: gl_issue(4, "Add a flag")},
                                {5: {"approved_by": ["robin"], "description": "Fixes #3"}})
        patcher = mock.patch.object(backlog, "_call", self.call)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.forge = forge.Forge(GL, approver="robin", kanban=FLOW, token="t")

    def test_snapshots_report_configured_human_review_label(self):
        issues = {1: gl_issue(1, "held", ["needs human"]), 2: gl_issue(2, "free", ["other hold"])}
        with mock.patch.object(backlog, "_call", gitlab_call(issues, {})):
            self.assertEqual([row["human_review"] for row in self.forge.snapshots()], [True, False])

    def test_snapshots_carry_identity_stage_review_and_merge_state(self):
        self.assertEqual(self.forge.snapshots(), [
            {"issue_key": "gitlab:gitlab.example.test/team/app#3", "title": "Fix the parser", "forge_state": "open",
             "stage": "review", "human_review": False, "review_state": "approved", "merge_state": "ready",
             "stage_mark": "stage: review"},
            {"issue_key": "gitlab:gitlab.example.test/team/app#4", "title": "Add a flag", "forge_state": "open",
             "stage": "", "human_review": False, "review_state": "", "merge_state": "", "stage_mark": ""},
        ])

    def test_a_failed_read_is_an_error_never_an_empty_forge(self):
        for broken in ("/issues", "/merge_requests"):
            with self.subTest(broken=broken), mock.patch.object(backlog, "_call", gitlab_call({}, {}, fail=broken)):
                with self.assertRaises(forge.ForgeError):
                    self.forge.snapshots()

    def test_one_issue_reads_with_its_description_as_its_body(self):
        got = self.forge.issue(3)
        self.assertEqual((got.issue.iid, got.issue.body), (3, "what to fix"))

    def test_a_request_read_past_one_page_is_still_a_whole_answer(self):
        mrs = {n: {"approved_by": [], "description": ""} for n in range(1, forge.backlog.PER_PAGE + 2)}
        with mock.patch.object(backlog, "_call", gitlab_call({}, mrs)):
            self.assertEqual(len(forge.Forge(GL, token="t").requests()), forge.backlog.PER_PAGE + 1)

    def test_a_request_read_that_reaches_the_cap_is_an_error_never_a_whole_answer(self):
        mrs = {n: {"approved_by": [], "description": ""} for n in range(1, forge.backlog.MAX_PAGES * forge.backlog.PER_PAGE + 1)}
        with mock.patch.object(backlog, "_call", gitlab_call({}, mrs)):
            with self.assertRaisesRegex(forge.ForgeError, "or more|partial|pages"):
                self.forge.snapshots()

    def test_a_committed_stage_write_swaps_the_stage_label(self):
        got = self.forge.write_stage(3, "doing", expected="review", commit=True)
        self.assertTrue(got.done, got.error)
        self.assertEqual([m for m, *_ in self.call.calls].count("PUT"), 1)

    def test_stage_round_trips_through_labels_including_terminal(self):
        issues = {4: gl_issue(4, "Add a flag")}
        with mock.patch.object(backlog, "_call", gitlab_call(issues, {})):
            f = forge.Forge(GL, kanban=FLOW, token="t")
            self.assertTrue(f.write_stage(4, "review", initialize=True, commit=True).done)
            self.assertEqual(f.snapshots()[0]["stage"], "review")
            self.assertTrue(f.write_stage(4, "done", expected="review", commit=True).done)
            self.assertEqual(f.snapshots()[0]["stage"], "done")

    def test_snapshots_include_closed_issues_and_merged_requests(self):
        issues = {3: gl_issue(3, "Fix the parser", ["stage: review"], state="closed"),
                  4: gl_issue(4, "Add a flag")}
        mrs, merged = {}, {5: {"description": "Fixes #3"}}
        with mock.patch.object(backlog, "_call", gitlab_call(issues, mrs, merged=merged)):
            got = forge.Forge(GL, kanban=FLOW, token="t").snapshots()
        self.assertEqual([(s["issue_key"], s["forge_state"], s["stage"], s["merge_state"]) for s in got],
                         [("gitlab:gitlab.example.test/team/app#3", "closed", "review", "merged"),
                          ("gitlab:gitlab.example.test/team/app#4", "open", "", "")])


class ThreadsTest(unittest.TestCase):
    def test_the_users_open_review_threads_are_read_as_work(self):
        answer = {"data": {"repository": {"pullRequests": {"nodes": [{"number": 21, "title": "t", "url": "u", "reviewThreads": {
            "nodes": [{"isResolved": False, "path": "a.py", "line": 3, "comments": {"nodes": [
                {"author": {"login": "robin"}, "body": "rename this", "url": "c1", "databaseId": 9}]}}]}}]}}}}
        got = forge.Forge(GH, approver="robin", gh=lambda args, input_text=None: (0, json.dumps(answer), "")).threads()
        self.assertEqual([(t.ref, t.state, t.said) for t in got], [("#21/9", "needs work", "rename this")])

    def test_threads_need_an_approver(self):
        with self.assertRaises(forge.ForgeError):
            forge.Forge(GH, gh=github()).threads()


class MergeTest(unittest.TestCase):
    def test_a_failed_state_read_after_a_successful_github_merge_is_unknown(self):
        gh = github()
        gh.fail = {("pr", "view")}
        got = forge.Forge(GH, gh=gh).merge(21, commit=True)
        self.assertEqual(got, f"unknown #21 at {tma.HEAD} · pipeline 9021 on {tma.HEAD} · "
                             "https://github.com/o/app/pull/21: rate limited")
        self.assertEqual(len(gh.merges), 1)
        self.assertEqual(sum(c[:2] == ["pr", "view"] for c in gh.all_calls), 1)

    def test_a_successful_github_merge_that_leaves_the_request_open_is_queued(self):
        gh = tmr.github_gh([21])
        reads = []

        def queued(args, input_text=None):
            if args[:2] == ["pr", "view"]:
                reads.append(args)
                return 0, json.dumps({"state": "OPEN"}), ""
            return gh(args, input_text)

        got = forge.Forge(GH, gh=queued).merge(21, commit=True)
        self.assertTrue(got.startswith("queued "), got)
        self.assertEqual(len(gh.merges), 1)
        self.assertEqual(reads, [["pr", "view", "21", "-R", REPO, "--json", "state"]])

    def test_a_ready_request_merges_pinned_to_the_verified_head(self):
        gh = tmr.github_gh([21])
        got = forge.Forge(GH, gh=gh).merge(21, commit=True)
        self.assertEqual(gh.merges, [["pr", "merge", "21", "-R", REPO, "--squash", "--match-head-commit", tma.HEAD]])
        for evidence in ("merged", "#21", tma.HEAD, "9021"):
            self.assertIn(evidence, got)

    def test_preview_names_the_verified_head_and_pipeline_and_writes_nothing(self):
        gh = tmr.github_gh([21])
        got = forge.Forge(GH, gh=gh).merge(21)
        for evidence in ("would merge", "#21", tma.HEAD, "9021"):
            self.assertIn(evidence, got)
        self.assertEqual(gh.merges, [])

    def test_every_not_ready_verdict_refuses_even_in_preview_and_writes_nothing(self):
        for number, (_, _, verdict) in tmr.GITHUB.items():
            if verdict == "ready":
                continue
            for commit in (False, True):
                with self.subTest(number=number, verdict=verdict, commit=commit):
                    gh = tmr.github_gh([number])
                    with self.assertRaisesRegex(forge.ForgeError, verdict):
                        forge.Forge(GH, gh=gh).merge(number, commit=commit)
                    self.assertEqual(gh.merges, [])

    def test_a_request_not_open_in_the_read_is_refused(self):
        gh = tmr.github_gh([21])
        with self.assertRaisesRegex(forge.ForgeError, "not an open request"):
            forge.Forge(GH, gh=gh).merge(99, commit=True)
        self.assertEqual(gh.merges, [])

    def test_a_failed_verification_writes_nothing(self):
        gh = github()
        gh.fail = {("api", "graphql")}
        with self.assertRaisesRegex(forge.ForgeError, "rate limited"):
            forge.Forge(GH, gh=gh).merge(21, commit=True)
        self.assertEqual(gh.merges, [])

    def test_merge_reads_again_instead_of_trusting_an_earlier_ready_result(self):
        gh = tmr.github_gh([21])
        f = forge.Forge(GH, gh=gh)
        self.assertEqual(f.requests()[0].verdict, "ready")
        gh.prs[0] = {**gh.prs[0], "statusCheckRollup": tmr.RED}
        with self.assertRaisesRegex(forge.ForgeError, "red"):
            f.merge(21, commit=True)
        self.assertEqual(gh.merges, [])

    def test_a_conflicting_stage_label_at_the_final_read_refuses_the_merge(self):
        for expected, labels in (("stage: review", ["stage: review", "stage: doing"]),  # expected mark stays
                                 ("", ["stage: doing", "stage: review"])):
            with self.subTest(expected=expected):
                gh = github()
                gh.issues[3]["labels"] = [{"name": n} for n in labels]
                with self.assertRaisesRegex(forge.ForgeError, "more than one stage"):
                    forge.Forge(GH, approver="robin", kanban=FLOW, gh=gh).merge(
                        21, commit=True, issue_key=f"github:{REPO}#3", stage_mark=expected)
                self.assertEqual(gh.merges, [])

    def test_an_unchanged_stage_label_still_merges_at_the_final_read(self):
        gh = github()
        forge.Forge(GH, approver="robin", kanban=FLOW, gh=gh).merge(
            21, commit=True, issue_key=f"github:{REPO}#3", stage_mark="stage: review")
        self.assertEqual(len(gh.merges), 1)

    def test_a_differently_cased_stage_label_still_merges_at_the_final_read(self):
        gh = github()
        gh.issues[3]["labels"] = [{"name": "Stage: Review"}, {"name": "bug"}]
        forge.Forge(GH, approver="robin", kanban=FLOW, gh=gh).merge(
            21, commit=True, issue_key=f"github:{REPO}#3", stage_mark="stage: review")
        self.assertEqual(len(gh.merges), 1)

    def test_project_mode_merge_accepts_status_without_stage_labels(self):
        # Project mode stores the stage in Status; stage labels are stripped from the issue.
        issue = gh_issue(3, "Fix the parser", ["bug"])
        gh = FakeGh(issues=[issue, gh_issue(4, "Add a flag", ["stage: doing"])],
                    prs=[tma.pr(21, [tma.review("robin", "APPROVED")])], bodies={21: "Closes #3"},
                    status={issue["url"]: "Review"})
        forge.Forge(GH, approver="robin", kanban=BOARD, gh=gh).merge(
            21, commit=True, issue_key=f"github:{REPO}#3", stage_mark="stage: review")
        self.assertEqual(len(gh.merges), 1)

    def test_project_mode_merge_refuses_when_status_moved(self):
        issue = gh_issue(3, "Fix the parser", ["bug"])
        gh = FakeGh(issues=[issue, gh_issue(4, "Add a flag", ["stage: doing"])],
                    prs=[tma.pr(21, [tma.review("robin", "APPROVED")])], bodies={21: "Closes #3"},
                    status={issue["url"]: "Doing"})
        with self.assertRaisesRegex(forge.ForgeError, "left its stage"):
            forge.Forge(GH, approver="robin", kanban=BOARD, gh=gh).merge(
                21, commit=True, issue_key=f"github:{REPO}#3", stage_mark="stage: review")
        self.assertEqual(gh.merges, [])

    def test_a_head_moved_after_verification_fails_the_pinned_merge(self):
        gh = tmr.github_gh([21])
        landed = []

        def moving(args, input_text=None):
            if args[:2] == ["pr", "merge"]:
                gh.calls.append(args)
                if args[args.index("--match-head-commit") + 1] != tma.OLD:
                    return 1, "", "head changed"
                landed.append(args)
                return 0, "", ""
            return gh(args, input_text)

        with self.assertRaisesRegex(forge.ForgeError, "head changed"):
            forge.Forge(GH, gh=moving).merge(21, commit=True)
        self.assertEqual(len(gh.merges), 1)
        self.assertEqual(landed, [])

    def test_a_failed_merge_with_no_stderr_is_still_a_refusal(self):
        gh = tmr.github_gh([21])

        def failing(args, input_text=None):
            return (1, "", "") if args[:2] == ["pr", "merge"] else gh(args, input_text)

        with self.assertRaises(forge.ForgeError):
            forge.Forge(GH, gh=failing).merge(21, commit=True)


class GitLabMergeTest(unittest.TestCase):
    def test_an_unreadable_response_after_a_successful_gitlab_merge_is_unknown(self):
        read = tmr.gitlab_fake({5: {}})
        for response, error in (({}, ""), (None, "GitLab did not answer with JSON")):
            with self.subTest(response=response, error=error):
                writes = []

                def call(method, url, token, timeout, payload=None):
                    if method == "PUT":
                        writes.append(payload)
                        return response, {}, error
                    return read(method, url, token, timeout, payload)

                with mock.patch.object(backlog, "_call", call):
                    got = forge.Forge(GL, token="t").merge(5, commit=True)
                self.assertEqual(got, f"unknown !5 at {tma.HEAD} · pipeline 7005 on {tma.HEAD} · "
                                     "https://labs.example.test/team/app/-/merge_requests/5: "
                                     f"{error or str(KeyError('state'))}")
                self.assertEqual(writes, [{"sha": tma.HEAD}])

    def test_gitlab_merge_when_pipeline_succeeds_is_queued_until_merged(self):
        read = tmr.gitlab_fake({5: {}})
        writes = []

        def queued(method, url, token, timeout, payload=None):
            if method == "PUT":
                writes.append(payload)
                return {"state": "opened", "merge_when_pipeline_succeeds": True}, {}, ""
            return read(method, url, token, timeout, payload)

        with mock.patch.object(backlog, "_call", queued):
            got = forge.Forge(GL, token="t").merge(5, commit=True)
        self.assertTrue(got.startswith("queued "), got)
        self.assertEqual(writes, [{"sha": tma.HEAD}])

    def fake(self, options=None, moved=False):
        read = tmr.gitlab_fake({5: options or {}})
        writes, landed = [], []

        def call(method, url, token, timeout, payload=None):
            if method == "PUT":
                writes.append((method, url, payload))
                if payload != {"sha": tma.OLD if moved else tma.HEAD}:
                    return None, {}, "HTTP 409: head changed"
                landed.append(payload)
                return {"state": "merged"}, {}, ""
            return read(method, url, token, timeout, payload)
        return call, writes, landed

    def test_ready_merges_pinned_to_the_verified_sha(self):
        call, writes, landed = self.fake()
        with mock.patch.object(backlog, "_call", call):
            got = forge.Forge(GL, token="t").merge(5, commit=True)
        self.assertEqual(writes, [("PUT", f"{GL.api(GL.project)}/merge_requests/5/merge", {"sha": tma.HEAD})])
        self.assertEqual(landed, [{"sha": tma.HEAD}])
        for evidence in ("merged", "!5", tma.HEAD, "7005"):
            self.assertIn(evidence, got)

    def test_preview_writes_nothing(self):
        call, writes, landed = self.fake()
        with mock.patch.object(backlog, "_call", call):
            got = forge.Forge(GL, token="t").merge(5)
        self.assertIn("would merge", got)
        self.assertIn(tma.HEAD, got)
        self.assertEqual((writes, landed), ([], []))

    def test_merged_results_evidence_names_the_pipeline_commit_and_merges_the_source_head(self):
        for commit in (False, True):
            with self.subTest(commit=commit):
                call, writes, landed = self.fake({"pipeline_sha": "c" * 40,
                                                  "pipeline_ref": "refs/merge-requests/5/merge"})
                with mock.patch.object(backlog, "_call", call):
                    got = forge.Forge(GL, token="t").merge(5, commit=commit)
                outcome = "merged" if commit else "would merge"
                self.assertEqual(got, f"{outcome} !5 at {tma.HEAD} · pipeline 7005 on {'c' * 40} · "
                                     "https://labs.example.test/team/app/-/merge_requests/5")
                self.assertEqual(landed, [{"sha": tma.HEAD}] if commit else [])
                self.assertEqual(len(writes), int(commit))

    def test_each_not_ready_gitlab_verdict_refuses_and_writes_nothing(self):
        for options, verdict in tmr.GITLAB.values():
            if verdict == "ready":
                continue
            for commit in (False, True):
                with self.subTest(verdict=verdict, commit=commit):
                    call, writes, landed = self.fake(options)
                    with mock.patch.object(backlog, "_call", call):
                        with self.assertRaisesRegex(forge.ForgeError, verdict):
                            forge.Forge(GL, token="t").merge(5, commit=commit)
                    self.assertEqual((writes, landed), ([], []))

    def test_a_head_moved_after_verification_fails_without_landing(self):
        call, writes, landed = self.fake(moved=True)
        with mock.patch.object(backlog, "_call", call):
            with self.assertRaisesRegex(forge.ForgeError, "head changed"):
                forge.Forge(GL, token="t").merge(5, commit=True)
        self.assertEqual(len(writes), 1)
        self.assertEqual(writes[0][2], {"sha": tma.HEAD})
        self.assertEqual(landed, [])


class ProtectedBranchesTest(unittest.TestCase):
    def test_github_reports_default_and_protected_branches(self):
        def gh(args, input_text=None):
            if args[:2] == ["api", f"repos/{REPO}"]:
                return 0, "trunk\n", ""
            if args[:2] == ["api", f"repos/{REPO}/branches?protected=true&per_page=100"]:
                return 0, "release\ntrunk\n", ""
            return 1, "", "unexpected"
        self.assertEqual(forge.Forge(GH, gh=gh).protected_branches(), ("trunk", "release"))

    def test_gitlab_reports_default_and_protected_branches(self):
        base = GL.api(GL.project)

        def call(method, url, token, timeout, payload=None):
            if url == base:
                return {"default_branch": "trunk"}, {}, ""
            if url == f"{base}/protected_branches?per_page=100":
                return [{"name": "release"}, {"name": "trunk"}], {}, ""
            return None, {}, "unexpected"
        with mock.patch.object(backlog, "_call", call):
            self.assertEqual(forge.Forge(GL, token="t").protected_branches(), ("trunk", "release"))

    def test_an_unreadable_answer_is_an_error_never_an_empty_list(self):
        with self.assertRaises(forge.ForgeError):
            forge.Forge(GH, gh=lambda args, input_text=None: (1, "", "rate limited")).protected_branches()
        with self.assertRaises(forge.ForgeError):
            forge.Forge(GH, gh=lambda args, input_text=None: (0, "\n", "")).protected_branches()
        with mock.patch.object(backlog, "_call", lambda *a, **k: (None, {}, "HTTP 500 from GitLab")):
            with self.assertRaises(forge.ForgeError):
                forge.Forge(GL, token="t").protected_branches()


# Stage map used by GitHub Project write-back tests moved from test_kanban.
BOARD_FLOW = Kanban(
    stages=("00 - PLAN", "01 - PLAN REVIEW", "02 - TEST", "03 - BUILD", "04 - REVIEW", "05 - FIX"),
    human_review_label="!! - HUMAN REVIEW REQUIRED",
    stage_map={"plan": 0, "plan review": 1, "test": 2, "implement": 3,
               "review": 4, "triage": 4, "fix": 5, "verify": 4, "merge": 4},
    hold_stages=("plan review", "triage", "merge"),
    terminal_stages=("main",),
)

class GitHubProjectWriteTest(unittest.TestCase):
    def test_release_hold_preserves_project_status_and_rejects_drift(self):
        self.labels = ["kind::code", BOARD_FLOW.human_review_label]
        self.status = "Test"
        got = forge.Forge(GH, kanban=self.config, gh=self.gh).write_stage(
            7, "test", expected="test", release_hold=True, commit=True)
        self.assertTrue(got.done, got.error)
        self.assertEqual(self.status, "Test")
        self.assertEqual(self.labels, ["kind::code"])
        self.labels.append(BOARD_FLOW.human_review_label)
        self.status = "Build"
        got = forge.Forge(GH, kanban=self.config, gh=self.gh).write_stage(
            7, "test", expected="test", release_hold=True, commit=True)
        self.assertFalse(got.ok)
        self.assertEqual(self.status, "Build")
        self.assertIn(BOARD_FLOW.human_review_label, self.labels)

    def setUp(self):
        self.ref = backlog.IssueRef("example/app", 7)
        self.item_url = self.ref.url
        self.config = Kanban(BOARD_FLOW.stages, BOARD_FLOW.human_review_label, BOARD_FLOW.stage_map,
                             BOARD_FLOW.hold_stages, BOARD_FLOW.terminal_stages,
                             GitHubProject("example", 3, "Status", ("Plan", "Plan Review", "Test", "Build", "Review", "Fix")))
        self.labels = ["kind::code"]
        self.status = "Plan"
        self.present = True
        self.calls = []

    def gh(self, args):
        self.calls.append(args)
        validate_project_cli(args)
        if args[:2] == ["issue", "view"]:
            return 0, json.dumps({"state": "OPEN", "url": self.ref.url,
                "labels": [{"name": x} for x in self.labels]}), ""
        if args[:2] == ["label", "list"]:
            return 0, json.dumps([{"name": x} for x in (*self.config.stages, self.config.human_review_label)]), ""
        if args[:2] == ["project", "field-list"]:
            opts = [*self.config.github_project.options, *self.config.terminal_stages]
            return 0, json.dumps({"fields": [{"id": "PVTSSF_status", "name": self.config.github_project.field, "options":
                [{"id": f"option_{x}", "name": x} for x in opts]}]}), ""
        if args[:2] == ["project", "view"]:
            return 0, json.dumps({"id": "PVT_board"}), ""
        if args[:2] == ["project", "item-list"]:
            field = self.config.github_project.field
            items = ([{"id": "PVTI_issue", "content": {"url": self.item_url},
                       field[0].lower() + field[1:]: self.status}] if self.present else [])
            return 0, json.dumps({"items": items, "totalCount": len(items)}), ""
        if args[:2] == ["project", "item-add"]:
            self.present = True
            return 0, json.dumps({"id": "PVTI_issue"}), ""
        if args[:2] == ["project", "item-edit"]:
            assert args[args.index("--id") + 1] == "PVTI_issue"
            assert args[args.index("--project-id") + 1] == "PVT_board"
            assert args[args.index("--field-id") + 1] == "PVTSSF_status"
            option = args[args.index("--single-select-option-id") + 1]
            self.status = next(x for x in (*self.config.github_project.options, *self.config.terminal_stages)
                               if f"option_{x}" == option)
            return 0, "{}", ""
        if args[:2] == ["issue", "edit"]:
            if "--add-label" in args:
                self.labels += args[args.index("--add-label") + 1].split(",")
            if "--remove-label" in args:
                self.labels = [x for x in self.labels if x not in args[args.index("--remove-label") + 1].split(",")]
            return 0, "", ""
        raise AssertionError(args)

    def test_project_preview_reads_status_without_writing(self):
        got = forge._sync_github(self.ref, self.config, "plan review", gh=self.gh)
        self.assertTrue(got.ok)
        self.assertFalse(got.done)
        self.assertEqual(got.project_status, "Plan Review")
        self.assertEqual(got.delta.add, (BOARD_FLOW.human_review_label,))
        self.assertFalse(any(args[1] in ("item-edit", "item-add", "edit") for args in self.calls))

    def test_project_preview_matches_issue_urls_case_insensitively(self):
        for repo, item_repo in (("Example/App", "example/app"), ("example/app", "Example/App")):
            with self.subTest(repo=repo, item_repo=item_repo):
                self.ref = backlog.issue_ref("#7", backlog.GitHubBacklog(repo))
                self.item_url = f"https://github.com/{item_repo}/issues/7"
                got = forge._sync_github(self.ref, self.config, "implement", gh=self.gh)
                self.assertTrue(got.ok, got.error)
                self.assertEqual(got.current_status, "Plan")
                self.assertFalse(got.project_add)

    def test_project_commit_does_not_readd_issue_with_mixed_case_repo(self):
        self.ref = backlog.issue_ref("#7", backlog.GitHubBacklog("Example/App"))
        got = forge._sync_github(self.ref, self.config, "implement", gh=self.gh, commit=True,
                                 expected_stage="plan")
        self.assertTrue(got.done, got.error)
        self.assertEqual(got.current_status, "Plan")
        self.assertEqual(self.status, "Build")
        self.assertFalse(got.project_add)
        self.assertFalse(any(a[:2] == ["project", "item-add"] for a in self.calls))

    def test_project_commit_sets_status_and_hold_then_verifies(self):
        got = forge._sync_github(self.ref, self.config, "plan review", gh=self.gh, commit=True,
                                 expected_stage="plan")
        self.assertTrue(got.done, got.error)
        self.assertEqual(self.status, "Plan Review")
        self.assertEqual(self.labels, ["kind::code", BOARD_FLOW.human_review_label])
        self.assertLess(next(i for i, a in enumerate(self.calls) if a[:2] == ["issue", "edit"]),
                        next(i for i, a in enumerate(self.calls) if a[:2] == ["project", "item-edit"]))

    def test_project_can_add_issue_before_setting_status(self):
        self.present = False
        got = forge._sync_github(self.ref, self.config, "implement", gh=self.gh, commit=True,
                                 initialize=True)
        self.assertTrue(got.done, got.error)
        self.assertEqual(self.status, "Build")
        self.assertTrue(got.project_add)
        self.assertTrue(any(a[:2] == ["project", "item-add"] for a in self.calls))

    def test_project_transition_migrates_the_recorded_legacy_label(self):
        for present in (False, True):
            with self.subTest(present=present):
                self.present, self.status = present, None
                self.labels = ["kind::code", "00 - PLAN"]
                self.calls = []
                got = forge._sync_github(self.ref, self.config, "plan review", gh=self.gh,
                                         commit=True, expected_stage="plan")
                self.assertTrue(got.done, got.error)
                self.assertEqual(self.status, "Plan Review")
                self.assertEqual(self.labels, ["kind::code", BOARD_FLOW.human_review_label])
                writes = [a[:2] for a in self.calls if a[1] in ("item-add", "item-edit", "edit")]
                self.assertEqual(writes, ([['project', 'item-add']] if not present else []) +
                                 [["project", "item-edit"], ["issue", "edit"]])

    def test_project_initialize_migrates_a_single_legacy_label(self):
        self.present, self.status = False, None
        self.labels = ["kind::code", "02 - TEST", BOARD_FLOW.human_review_label]
        got = forge._sync_github(self.ref, self.config, "implement", gh=self.gh,
                                 commit=True, initialize=True)
        self.assertTrue(got.done, got.error)
        self.assertEqual(self.status, "Build")
        self.assertEqual(self.labels, ["kind::code", BOARD_FLOW.human_review_label])

    def test_project_refuses_legacy_label_mismatch_or_multiple_stage_labels(self):
        for labels, expected, initialize in (
                (["02 - TEST"], "plan", False),
                (["00 - PLAN", "02 - TEST"], "plan", False),
                (["00 - PLAN", "02 - TEST"], None, True)):
            with self.subTest(labels=labels, expected=expected, initialize=initialize):
                self.present, self.status = False, None
                self.labels, self.calls = labels.copy(), []
                got = forge._sync_github(self.ref, self.config, "plan review", gh=self.gh,
                                         commit=True, expected_stage=expected, initialize=initialize)
                self.assertIn("changed outside the tracker", got.error)
                self.assertFalse(any(a[1] in ("item-add", "item-edit", "edit") for a in self.calls))
                self.assertEqual(self.labels, labels)

    def test_failed_project_migration_keeps_the_legacy_stage_label(self):
        self.present, self.status = False, None
        self.labels = ["kind::code", "00 - PLAN"]

        def gh(args):
            if args[:2] == ["project", "item-edit"]:
                return 1, "", "status update failed"
            return self.gh(args)

        got = forge._sync_github(self.ref, self.config, "plan review", gh=gh,
                                 commit=True, expected_stage="plan")
        self.assertEqual(got.error, "status update failed")
        self.assertIn("00 - PLAN", self.labels)
        self.assertFalse(any(a[:2] == ["issue", "edit"] for a in self.calls))

    def test_a_retry_finishes_labels_after_status_was_written(self):
        for status, labels, expected in (("Plan Review", [BOARD_FLOW.human_review_label], "plan review"),
                                         (None, ["00 - PLAN"], "plan")):
            with self.subTest(expected=expected):
                self.status, self.labels, self.calls = status, ["kind::code", *labels], []
                failed = []

                def gh(args):
                    if args[:2] == ["issue", "edit"] and not failed:
                        failed.append(args)
                        return 1, "", "label edit failed"
                    return self.gh(args)

                first = forge._sync_github(self.ref, self.config, "implement", gh=gh, commit=True,
                                           expected_stage=expected)
                self.assertEqual(first.error, "label edit failed")
                self.assertEqual(self.status, "Build")
                got = forge._sync_github(self.ref, self.config, "implement", gh=gh, commit=True,
                                         expected_stage=expected)
                self.assertTrue(got.done, got.error)
                self.assertEqual(self.labels, ["kind::code"])

    def test_terminal_stage_sets_status_named_like_the_stage(self):
        # Cleared Status before #627; terminal must round-trip through Project Status.
        self.status = "Review"
        got = forge._sync_github(self.ref, self.config, "main", gh=self.gh, commit=True,
                                 expected_stage="review")
        self.assertTrue(got.done, got.error)
        self.assertEqual(self.status, "main")
        self.assertTrue(any(a[:2] == ["project", "item-edit"] for a in self.calls))

    def test_missing_terminal_project_option_says_to_create_it_without_writing(self):
        self.status = "Review"

        def gh(args):
            if args[:2] == ["project", "field-list"]:
                return 0, json.dumps({"fields": [{"id": "PVTSSF_status", "name": "Status", "options": []}]}), ""
            return self.gh(args)

        got = forge._sync_github(self.ref, self.config, "main", gh=gh, commit=True, expected_stage="review")
        self.assertIn("create", got.error)
        self.assertIn("main", got.error)
        self.assertFalse(any(a[:2] in (["project", "item-edit"], ["issue", "edit"]) for a in self.calls))

    def test_missing_terminal_issue_label_says_to_create_it_without_writing(self):
        self.config = BOARD_FLOW
        self.labels = ["04 - REVIEW", "kind::code"]
        got = forge._sync_github(self.ref, self.config, "main", gh=self.gh, commit=True, expected_stage="review")
        self.assertIn("create", got.error)
        self.assertIn("main", got.error)
        self.assertFalse(any(a[:2] == ["issue", "edit"] for a in self.calls))

    def test_terminal_issue_label_transition_when_provisioned(self):
        self.config = BOARD_FLOW
        self.labels = ["04 - REVIEW", "kind::code"]

        def gh(args):
            if args[:2] == ["label", "list"]:
                return 0, json.dumps([{"name": x} for x in (*BOARD_FLOW.stages, *BOARD_FLOW.terminal_stages)]), ""
            return self.gh(args)

        got = forge._sync_github(self.ref, self.config, "main", gh=gh, commit=True, expected_stage="review")
        self.assertTrue(got.done, got.error)
        self.assertEqual(set(self.labels), {"main", "kind::code"})

    def test_github_labels_exist_case_insensitively(self):
        for wanted, present in (("main", "Main"), ("03 - BUILD", "03 - build")):
            with self.subTest(wanted=wanted, present=present):
                gh = mock.Mock(return_value=(0, json.dumps([{"name": present}]), ""))
                self.assertEqual(forge._github_labels_exist(gh, self.ref, (wanted,)), "")

    def test_a_differently_cased_managed_label_is_the_configured_one(self):
        self.labels = ["Main", "kind::code"]

        def gh(args):
            if args[:2] == ["label", "list"]:
                return 0, json.dumps([{"name": "Main"}]), ""
            return self.gh(args)

        labels_only = replace(self.config, github_project=None)
        got = forge._sync_github(self.ref, labels_only, "main", gh=gh, commit=True, expected_stage="main")
        self.assertTrue(got.done, got.error)
        self.assertEqual(set(self.labels), {"Main", "kind::code"})
        self.assertEqual(kanban.drift(labels_only, ("Main",), "main"), "")

    def test_missing_github_label_preserves_configured_name(self):
        gh = mock.Mock(return_value=(0, json.dumps([{"name": "Main"}]), ""))
        error = forge._github_labels_exist(gh, self.ref, ("main", "03 - BUILD"))
        self.assertEqual(error, "missing label in GitHub: 03 - BUILD; "
                               "create the label(s) 03 - BUILD before syncing")

    def test_a_launcher_hold_moves_with_its_project_status(self):
        self.labels = ["kind::code", BOARD_FLOW.human_review_label]
        self.status = "Test"
        got = forge._sync_github(self.ref, self.config, "review", gh=self.gh, commit=True, expected_stage="test")
        self.assertTrue(got.done, got.error)
        self.assertEqual(self.status, "Review")
        self.assertIn(BOARD_FLOW.human_review_label, self.labels)

    def test_status_is_read_without_new_item_list_flags(self):
        got = forge._sync_github(self.ref, self.config, "implement", gh=self.gh)
        self.assertTrue(got.ok, got.error)
        self.assertEqual(got.current_status, "Plan")
        call = next(a for a in self.calls if a[:2] == ["project", "item-list"])
        self.assertEqual(call, ["project", "item-list", "3", "--owner", "example",
                                "--format", "json", "--limit", "5000"])

    def test_project_commit_uses_node_ids(self):
        got = forge._sync_github(self.ref, self.config, "implement", gh=self.gh, commit=True,
                                 expected_stage="plan")
        self.assertTrue(got.done, got.error)
        call = next(a for a in self.calls if a[:2] == ["project", "item-edit"])
        self.assertEqual(call, ["project", "item-edit", "--id", "PVTI_issue", "--project-id", "PVT_board",
                                "--field-id", "PVTSSF_status", "--single-select-option-id", "option_Build"])

    def test_project_item_list_at_limit_is_refused_unless_total_count_says_it_is_whole(self):
        for total in (None, 5000, 5001):
            row = {"items": [{"content": {"url": self.ref.url}, "status": "Plan"}] * 5000}
            if total is not None:
                row["totalCount"] = total
            with self.subTest(total=total):
                def gh(args):
                    return (0, json.dumps(row), "") if args[:2] == ["project", "item-list"] else self.gh(args)

                items, error = forge.project_items(gh, self.config.github_project)
                if total == 5000:
                    self.assertEqual((error, items), ("", {self.ref.url: "Plan"}))
                else:
                    self.assertEqual(items, {})
                    self.assertIn("truncated", error)

    def test_project_reads_refuse_truncated_field_metadata(self):
        for total in (None, 100, 101):
            status = {"id": "PVTSSF_status", "name": self.config.github_project.field}
            fields = {"fields": [{"id": "PVTF_other", "name": "Other"}] * 99 + [status]}
            if total is not None:
                fields["totalCount"] = total

            def gh(args):
                if args[:2] == ["project", "field-list"]:
                    return 0, json.dumps(fields), ""
                return self.gh(args)

            with self.subTest(total=total):
                statuses, error = forge.project_items(gh, self.config.github_project)
                item, status, item_error = forge._project_item(gh, self.config.github_project, self.ref)
                if total == 100:
                    self.assertEqual((statuses, error), ({self.ref.url: "Plan"}, ""))
                    self.assertEqual((status, item_error), ("Plan", ""))
                else:
                    self.assertEqual(statuses, {})
                    self.assertEqual((item, status), (None, None))
                    self.assertIn("field list was truncated", error)
                    self.assertIn("field list was truncated", item_error)

    def test_project_reads_refuse_a_missing_configured_field(self):
        def gh(args):
            if args[:2] == ["project", "field-list"]:
                return 0, json.dumps({"fields": [{"id": "PVTF_other", "name": "Other"}], "totalCount": 1}), ""
            return self.gh(args)

        statuses, error = forge.project_items(gh, self.config.github_project)
        self.assertEqual(statuses, {})
        self.assertIn(f"no field named {self.config.github_project.field}", error)

    def test_project_field_list_at_limit_is_refused_unless_total_count_says_it_is_whole(self):
        status = {"id": "PVTSSF_status", "name": "Status", "options": [{"id": "option_Build", "name": "Build"}]}
        for total in (None, 100, 101):
            row = {"fields": [{"id": "PVTF_other", "name": "Other"}] * 99 + [status]}
            if total is not None:
                row["totalCount"] = total
            with self.subTest(total=total):
                got = forge.project_option_ids(lambda args: (0, json.dumps(row), ""),
                                                 self.config.github_project, "Build")
                if total == 100:
                    self.assertEqual(got, ("PVTSSF_status", "option_Build", ""))
                else:
                    self.assertIn("truncated", got[2])

    def test_issue_label_mode_uses_add_remove_without_project(self):
        self.config = BOARD_FLOW
        self.labels = ["00 - PLAN", "kind::code"]
        got = forge._sync_github(self.ref, self.config, "plan review", gh=self.gh, commit=True,
                                 expected_stage="plan")
        self.assertTrue(got.done, got.error)
        self.assertEqual(set(self.labels), {"01 - PLAN REVIEW", BOARD_FLOW.human_review_label, "kind::code"})
        self.assertFalse(any(a[:1] == ["project"] for a in self.calls))

    def test_project_manual_status_change_is_reported(self):
        self.status = "Fix"
        got = forge._sync_github(self.ref, self.config, "plan review", gh=self.gh, commit=True,
                                 expected_stage="plan")
        self.assertIn("changed outside the tracker", got.error)
        self.assertFalse(any(args[1] in ("item-edit", "edit") for args in self.calls))

    def test_invalid_prior_stage_is_refused_without_writing(self):
        got = forge._sync_github(self.ref, self.config, "plan review", gh=self.gh, commit=True,
                                 expected_stage="invented")
        self.assertIn("unmapped prior stage", got.error)
        self.assertFalse(self.calls)

if __name__ == "__main__":
    unittest.main()


class ChangeCommentsTest(unittest.TestCase):
    def test_github_reads_all_change_comments_and_posts_to_the_request(self):
        pages = [[{"user": {"login": "robin"}, "created_at": "2000-01-01T00:00:00Z", "body": "first"}],
                 [{"user": {"login": "robin"}, "created_at": "2000-01-01T00:01:00Z", "body": "Architect: APPROVED head"}]]
        gh = mock.Mock(return_value=(0, json.dumps(pages), ""))
        source = forge.Forge(GH, gh=gh)
        self.assertEqual([note.body for note in source.change_comments(21)], ["first", "Architect: APPROVED head"])
        self.assertIn("--paginate", gh.call_args.args[0])
        self.assertIn("--slurp", gh.call_args.args[0])
        source.comment_change(21, "Architect review requested: head")
        self.assertEqual(gh.call_args.args[0][:3], ["pr", "comment", "21"])
        self.assertEqual(gh.call_args.args[1], "Architect review requested: head")

    def test_gitlab_pages_merge_request_notes_and_omits_system_notes(self):
        source = forge.Forge(GL, token="test-token")
        def call(method, url, token, timeout, payload=None):
            self.assertIn("/merge_requests/21/notes", url)
            if method == "POST":
                self.assertEqual(payload, {"body": "Architect review requested: head"})
                return {}, {}, ""
            page = parse_qs(urlsplit(url).query)["page"][0]
            return ([{"author": {"username": "robin"}, "body": "reply " + page, "created_at": "2000-01-01T00:00:00Z"},
                     {"system": True, "body": "system"}], {"x-next-page": "2" if page == "1" else ""}, "")
        with mock.patch.object(backlog, "_call", side_effect=call):
            self.assertEqual([n.body for n in source.change_comments(21)], ["reply 1", "reply 2"])
            source.comment_change(21, "Architect review requested: head")

    def test_comment_read_or_write_failure_is_never_an_empty_success(self):
        source = forge.Forge(GH, gh=mock.Mock(return_value=(1, "", "unavailable")))
        with self.assertRaises(forge.ForgeError):
            source.change_comments(21)
        with self.assertRaises(forge.ForgeError):
            source.comment_change(21, "request")


class ForgeRetryabilityTest(unittest.TestCase):
    def test_adapter_classifies_request_failures(self):
        import merge_ready
        from unittest import mock
        source = forge.Forge(backlog.GitHubBacklog("example/project"))
        for message, retryable in (("HTTP 429", True), ("HTTP 503", True), ("connection reset", True),
                                   ("timed out", True), ("HTTP 403 rate limit exceeded", True),
                                   ("HTTP 401", False), ("HTTP 404", False), ("unreadable JSON", False)):
            with self.subTest(message=message), mock.patch.object(merge_ready, "github_ready", side_effect=RuntimeError(message)):
                with self.assertRaises(forge.ForgeError) as raised:
                    source.requests()
                self.assertEqual(raised.exception.retryable, retryable)
        with self.assertRaises(forge.ForgeError) as raised:
            source.closing_request("github:example/project#1", [])
        self.assertFalse(raised.exception.retryable)

    def test_engine_login_comes_from_the_authenticated_forge_user(self):
        from unittest import mock
        gh = mock.Mock(return_value=(0, '{"login":"engine"}', ""))
        source = forge.Forge(backlog.GitHubBacklog("example/project"), approver="human", gh=gh)
        self.assertEqual(source.login(), "engine")
        gh.assert_called_once_with(["api", "user"])
        home = backlog.Backlog("https://gitlab.example", "group/project")
        with mock.patch.object(backlog, "_call", return_value=({"username": "engine"}, {}, "")) as call:
            self.assertEqual(forge.Forge(home, token="test-token").login(), "engine")
        self.assertEqual(call.call_args.args[:2], ("GET", "https://gitlab.example/api/v4/user"))
