"""The one forge adapter (1.0 plan, M2): GitHub and GitLab issues, requests, CI and review state, stage write-back
(labels, or GitHub Project Status when configured), and the snapshots the store is rebuilt from. Writes preview by
default; a read that fails is an error, never an empty forge."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "evals"))
sys.path.insert(0, str(ROOT / "scripts"))

import backlog  # noqa: E402
import forge  # noqa: E402
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
        if args[:2] == ["issue", "list"]:
            state = args[args.index("--state") + 1] if "--state" in args else "open"
            rows = [i for i in self.issues.values()
                    if state == "all" or (state == "open" and i["state"] == "OPEN")
                    or (state == "closed" and i["state"] == "CLOSED")]
            return 0, json.dumps(rows), ""
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
            if state == "merged":
                assert "--search" in args, "merged PRs need search ordering by update recency"
                assert args[args.index("--search") + 1] == "sort:updated-desc"
                assert args[args.index("--limit") + 1] == str(forge.forge_review.REQUEST_LIMIT)
                rows = sorted(self.merged, key=lambda p: p.get("updatedAt", ""), reverse=True)
                return 0, json.dumps(rows[:forge.forge_review.REQUEST_LIMIT]), ""
            return super().__call__(args, input_text)
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


class GitHubTest(unittest.TestCase):
    def test_snapshots_report_configured_human_review_label(self):
        gh = FakeGh(issues=[gh_issue(1, "held", ["stage: review", "needs human"]),
                            gh_issue(2, "free", ["stage: review", "other hold"])])
        snapshots = forge.Forge(GH, kanban=FLOW, gh=gh).snapshots()
        self.assertEqual([row.get("human_review") for row in snapshots], [True, False])
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
                item = store.WorkItem(**snapshot)
                for stage in ("review", "merge"):
                    item.stage = stage
                    self.assertEqual(tr.reconcile.plan([item],
                                     {item.issue_key: [tr.Segment("review")]}, tr.policies(), {}, 0, tr.NOW,
                                     {item.issue_key: {}}),
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
             "human_review": False, "review_state": "approved", "merge_state": "ready"},
            {"issue_key": "github:example/app#4", "title": "Add a flag", "forge_state": "open", "stage": "doing",
             "human_review": False, "review_state": "", "merge_state": ""},
        ])

    def test_what_was_not_configured_is_left_out_so_a_sync_never_blanks_it(self):
        got = forge.Forge(GH, gh=github()).snapshots()
        self.assertEqual([sorted(s) for s in got], [["forge_state", "issue_key", "merge_state", "title"]] * 2)

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
        got = forge.Forge(GH, kanban=BOARD, gh=gh).write_stage(4, "doing", commit=True)
        self.assertTrue(got.done, got.error)
        self.assertTrue(any(c[:2] == ["project", "item-edit"] for c in gh.all_calls), gh.all_calls)
        self.assertEqual(gh.status[f"https://github.com/{REPO}/issues/4"], "Doing")
        self.assertNotIn("stage: doing", [x["name"] for x in gh.issues[4]["labels"]])

    def test_snapshots_read_stage_from_project_status(self):
        url = gh_issue(4, "Add a flag")["url"]
        gh = FakeGh(issues=[gh_issue(4, "Add a flag")], status={url: "Doing"})
        got = forge.Forge(GH, kanban=BOARD, gh=gh).snapshots()
        self.assertEqual(got, [{"issue_key": "github:example/app#4", "title": "Add a flag", "forge_state": "open",
                                "stage": "doing", "merge_state": ""}])

    def test_unknown_project_status_option_is_an_error(self):
        url = gh_issue(4, "x")["url"]
        gh = FakeGh(issues=[gh_issue(4, "x")], status={url: "Invented"})
        with self.assertRaisesRegex(forge.ForgeError, "Invented"):
            forge.Forge(GH, kanban=BOARD, gh=gh).snapshots()

    def test_a_shared_label_never_reads_back_as_a_different_stage(self):
        gh = FakeGh(issues=[gh_issue(4, "x", ["stage: shared"])])
        with self.assertRaisesRegex(forge.ForgeError, "more than one stage"):
            forge.Forge(GH, kanban=SHARED, gh=gh).snapshots()

    def test_stage_round_trips_through_project_status_including_terminal(self):
        gh = FakeGh(issues=[gh_issue(4, "Add a flag")], status={})
        f = forge.Forge(GH, kanban=BOARD, gh=gh)
        self.assertTrue(f.write_stage(4, "review", commit=True).done, "init")
        self.assertEqual(f.snapshots()[0]["stage"], "review")
        self.assertTrue(f.write_stage(4, "done", expected="review", commit=True).done)
        self.assertEqual(f.snapshots()[0]["stage"], "done")

    def test_stage_round_trips_through_labels_including_terminal(self):
        gh = FakeGh(issues=[gh_issue(4, "Add a flag")])
        f = forge.Forge(GH, kanban=FLOW, gh=gh)
        self.assertTrue(f.write_stage(4, "review", commit=True).done)
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

    def test_a_long_merged_history_does_not_refuse_the_snapshot(self):
        # Merged requests only annotate closed issues; a busy repo has far more than one read lists.
        many = [tma.pr(n, []) for n in range(1, forge.forge_review.REQUEST_LIMIT + 1)]
        gh = FakeGh(issues=[gh_issue(3, "x", ["stage: doing"])], prs=[], merged=many)
        self.assertEqual(forge.Forge(GH, kanban=FLOW, gh=gh).snapshots()[0]["merge_state"], "")

    def test_a_new_issue_takes_its_first_stage_but_a_moved_card_is_not_overwritten(self):
        gh = FakeGh(issues=[gh_issue(5, "new"), gh_issue(6, "moved by hand", ["stage: review"])])
        f = forge.Forge(GH, kanban=FLOW, gh=gh)
        self.assertTrue(f.write_stage(5, "ready", commit=True).done)
        self.assertIn("reconcile", f.write_stage(6, "doing", commit=True).error)

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
            return rows, {}, ""
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


class GitLabTest(unittest.TestCase):
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
             "stage": "review", "human_review": False, "review_state": "approved", "merge_state": "ready"},
            {"issue_key": "gitlab:gitlab.example.test/team/app#4", "title": "Add a flag", "forge_state": "open",
             "stage": "", "human_review": False, "review_state": "", "merge_state": ""},
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
            self.assertTrue(f.write_stage(4, "review", commit=True).done)
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


if __name__ == "__main__":
    unittest.main()
