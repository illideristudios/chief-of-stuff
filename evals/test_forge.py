"""The one forge adapter (1.0 plan, M2): GitHub and GitLab issues, requests, CI and review state, stage write-back as labels,
and the snapshots the store is rebuilt from. Writes preview by default; a read that fails is an error, never an empty forge."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "evals"))
sys.path.insert(0, str(ROOT / "scripts"))

import backlog  # noqa: E402
import forge  # noqa: E402
import store  # noqa: E402
import test_merge_approved as tma  # noqa: E402  (module-qualified: don't re-collect its TestCases)
from settings import GitHubProject, Kanban  # noqa: E402

REPO = "example/app"
GH = backlog.GitHubBacklog(REPO)
GL = backlog.Backlog("https://gitlab.example.test", "team/app")
FLOW = Kanban(stages=("stage: ready", "stage: doing", "stage: review"), human_review_label="needs human",
              stage_map={"ready": 0, "doing": 1, "review": 2})


def gh_issue(number, title, labels=()):
    return {"number": number, "title": title, "state": "OPEN", "labels": [{"name": n} for n in labels],
            "url": f"https://github.com/{REPO}/issues/{number}", "updatedAt": "", "closedAt": None, "body": "the body"}


class FakeGh(tma.FakeGhGraphql):
    """`gh` for issues, labels and pull requests. Issue labels change on `issue edit`, so a write can be verified.
    `fail` names the `gh` subcommands (`("issue", "list")`) that answer an error."""

    def __init__(self, issues=(), prs=(), bodies=None, fail=()):
        super().__init__([{**p, "body": (bodies or {}).get(p["number"], "")} for p in prs])
        self.issues = {i["number"]: i for i in issues}
        self.fail = set(fail)
        self.all_calls: list[list[str]] = []

    def __call__(self, args, input_text=None):
        self.all_calls.append(list(args))
        if tuple(args[:2]) in self.fail:
            return 1, "", "rate limited"
        if args[:2] == ["issue", "list"]:
            return 0, json.dumps(list(self.issues.values())), ""
        if args[:2] == ["issue", "view"]:
            return 0, json.dumps(self.issues[int(args[2])]), ""
        if args[:2] == ["issue", "edit"]:
            row = self.issues[int(args[2])]
            names = [x["name"] for x in row["labels"]]
            add = args[args.index("--add-label") + 1].split(",") if "--add-label" in args else []
            remove = args[args.index("--remove-label") + 1].split(",") if "--remove-label" in args else []
            row["labels"] = [{"name": n} for n in [*(n for n in names if n not in remove), *add]]
            return 0, "", ""
        if args[:2] == ["label", "list"]:
            return 0, json.dumps([{"name": n} for n in (*FLOW.stages, FLOW.human_review_label)]), ""
        if args[:2] == ["issue", "create"]:
            return 0, f"https://github.com/{REPO}/issues/101\n", ""
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
    def test_snapshots_carry_identity_stage_review_and_merge_state(self):
        got = forge.Forge(GH, approver="robin", kanban=FLOW, gh=github()).snapshots()
        self.assertEqual(got, [
            {"issue_key": "github:example/app#3", "title": "Fix the parser", "forge_state": "open", "stage": "review",
             "review_state": "approved", "merge_state": "ready"},
            {"issue_key": "github:example/app#4", "title": "Add a flag", "forge_state": "open", "stage": "doing",
             "review_state": "", "merge_state": ""},
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

    def test_the_stage_goes_to_labels_even_when_a_project_is_configured(self):
        gh = github()
        board = Kanban(FLOW.stages, FLOW.human_review_label, FLOW.stage_map,
                       github_project=GitHubProject("example", 3, "Status", ("Ready", "Doing", "Review")))
        got = forge.Forge(GH, kanban=board, gh=gh).write_stage(4, "review", expected="doing", commit=True)
        self.assertTrue(got.done, got.error)
        self.assertEqual([c for c in gh.all_calls if c[0] == "project"], [])
        self.assertEqual(len(self.edits(gh)), 1)

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


def gitlab_call(issues, mrs, fail=""):
    """GitLab's REST answers: the issue list and one issue, labels, and merge requests with their approvals and discussions."""
    calls = []

    def call(method, url, token, timeout, payload=None):
        calls.append((method, url, payload))
        path = urlsplit(url).path.split("/api/v4/projects/team%2Fapp", 1)[1]
        if fail and path.startswith(fail):
            return None, {}, "HTTP 500 from GitLab"
        if path == "/issues":
            return [issues[n] for n in sorted(issues)], {}, ""
        if path.startswith("/issues/"):
            row = issues[int(path.split("/")[2])]
            if method == "PUT":
                drop = set((payload.get("remove_labels") or "").split(","))
                row["labels"] = [x for x in row["labels"] if x not in drop] + [
                    x for x in (payload.get("add_labels") or "").split(",") if x]
            return row, {}, ""
        if path == "/labels":
            return [{"name": n} for n in FLOW.stages], {}, ""
        if path == "/merge_requests":
            return [{"iid": i} for i in mrs], {}, ""
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


def gl_issue(iid, title, labels=()):
    return {"iid": iid, "title": title, "state": "opened", "labels": list(labels), "web_url": "", "updated_at": "", "closed_at": ""}


class GitLabTest(unittest.TestCase):
    def setUp(self):
        self.call = gitlab_call({3: gl_issue(3, "Fix the parser", ["stage: review"]), 4: gl_issue(4, "Add a flag")},
                                {5: {"approved_by": ["robin"], "description": "Fixes #3"}})
        patcher = mock.patch.object(backlog, "_call", self.call)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.forge = forge.Forge(GL, approver="robin", kanban=FLOW, token="t")

    def test_snapshots_carry_identity_stage_review_and_merge_state(self):
        self.assertEqual(self.forge.snapshots(), [
            {"issue_key": "gitlab:gitlab.example.test/team/app#3", "title": "Fix the parser", "forge_state": "open",
             "stage": "review", "review_state": "approved", "merge_state": "ready"},
            {"issue_key": "gitlab:gitlab.example.test/team/app#4", "title": "Add a flag", "forge_state": "open",
             "stage": "", "review_state": "", "merge_state": ""},
        ])

    def test_a_failed_read_is_an_error_never_an_empty_forge(self):
        for broken in ("/issues", "/merge_requests"):
            with self.subTest(broken=broken), mock.patch.object(backlog, "_call", gitlab_call({}, {}, fail=broken)):
                with self.assertRaises(forge.ForgeError):
                    self.forge.snapshots()

    def test_a_committed_stage_write_swaps_the_stage_label(self):
        got = self.forge.write_stage(3, "doing", expected="review", commit=True)
        self.assertTrue(got.done, got.error)
        self.assertEqual([m for m, *_ in self.call.calls].count("PUT"), 1)


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


if __name__ == "__main__":
    unittest.main()
