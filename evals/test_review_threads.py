"""Review comments the user leaves on a pull request come back to the coordinator as work, and workers answer them."""

from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import review_threads as rt  # noqa: E402


def comment(login, body, cid):
    return {"author": {"login": login}, "body": body, "databaseId": cid,
            "url": f"https://github.com/o/app/pull/12#discussion_r{cid}"}


def thread(comments, resolved=False, path="src/app.py", line=4):
    return {"isResolved": resolved, "path": path, "line": line, "comments": {"nodes": comments}}


GRAPH = {"data": {"repository": {"pullRequests": {"nodes": [
    {"number": 12, "title": "Cache warmup", "url": "https://github.com/o/app/pull/12", "reviewThreads": {"nodes": [
        thread([comment("robin", "This leaks the pool on error.", 101)]),
        thread([comment("robin", "Rename this.", 102), comment("worker-bot", "a41c9e2: renamed", 103)]),
        thread([comment("robin", "Still wrong.", 104), comment("worker-bot", "b1", 105),
                comment("robin", "No, the other branch.", 106)]),
        thread([comment("robin", "Done already.", 107)], resolved=True),
        thread([comment("peer-bot", "Nit: spacing.", 108)]),
    ]}},
]}}}}


class FakeGh:
    def __init__(self):
        self.calls = []

    def __call__(self, args, input_text=None):
        self.calls.append(args)
        if args[:2] == ["api", "graphql"]:
            return 0, json.dumps(GRAPH), ""
        if args[:1] == ["api"]:
            return 0, "{}", ""
        return 1, "", "unexpected"


class GitHubThreadsTest(unittest.TestCase):
    def test_the_users_open_threads_are_needs_work_until_answered(self):
        got = {t.ref: t.state for t in rt.github_threads("o/app", "robin", FakeGh())}
        self.assertEqual(got, {"#12/101": "needs work", "#12/102": "awaiting you", "#12/104": "needs work"})

    def test_a_thread_carries_the_users_last_words_and_its_link(self):
        one = next(t for t in rt.github_threads("o/app", "robin", FakeGh()) if t.ref == "#12/104")
        self.assertEqual(one.said, "No, the other branch.")
        self.assertEqual(one.url, "https://github.com/o/app/pull/12#discussion_r106")
        self.assertEqual(one.where, "src/app.py:4")


    def test_a_read_that_reaches_the_cap_is_an_error_never_a_whole_answer(self):
        full = [thread([comment("robin", "x", n)]) for n in range(rt.LIMIT)]
        graph = {"data": {"repository": {"pullRequests": {"nodes": [
            {"number": 12, "title": "t", "url": "u", "reviewThreads": {"nodes": full}}]}}}}
        with self.assertRaisesRegex(RuntimeError, "#12 review threads"):
            rt.github_threads("o/app", "robin", lambda args, input_text=None: (0, json.dumps(graph), ""))


    def test_a_partial_answer_with_graphql_errors_is_an_error(self):
        partial = {**GRAPH, "errors": [{"message": "timeout"}]}
        with self.assertRaisesRegex(RuntimeError, "timeout"):
            rt.github_threads("o/app", "robin", lambda args, input_text=None: (0, json.dumps(partial), ""))


class GitLabThreadsTest(unittest.TestCase):
    def test_gitlab_discussions_follow_the_same_rule(self):
        def note(user, body, nid, **kw):
            return {"id": nid, "author": {"username": user}, "body": body, "resolvable": True,
                    "resolved": False, "system": False, "position": {"new_path": "a.py", "new_line": 9}, **kw}
        discussions = [
            {"id": "d1", "notes": [note("robin", "Fix this.", 1)]},
            {"id": "d2", "notes": [note("robin", "Fix that.", 2), note("worker-bot", "c3: fixed", 3)]},
            {"id": "d3", "notes": [note("robin", "Resolved.", 4, resolved=True)]},
            {"id": "d4", "notes": [note("robin", "General remark.", 5, resolvable=False)]},
        ]

        def call(method, url, token, timeout, payload=None):
            path = url.split("/api/v4/projects/team%2Fapp", 1)[1]
            if path.startswith("/merge_requests?"):
                return [{"iid": 5, "title": "MR", "web_url": "https://labs.example.test/team/app/-/merge_requests/5"}], {}, ""
            if path.startswith("/merge_requests/5/discussions"):
                return discussions, {}, ""
            return None, {}, "unexpected"

        home = rt.backlog.Backlog("https://labs.example.test", "team/app")
        got = {t.ref: (t.state, t.url) for t in rt.gitlab_threads(home, "team/app", "robin", "t", call)}
        self.assertEqual(got, {
            "!5/d1": ("needs work", "https://labs.example.test/team/app/-/merge_requests/5#note_1"),
            "!5/d2": ("awaiting you", "https://labs.example.test/team/app/-/merge_requests/5#note_3"),
        })


    def test_a_merge_request_list_that_reaches_the_cap_is_an_error(self):
        listed = [{"iid": n, "title": "MR", "web_url": "u"} for n in range(rt.LIMIT)]
        home = rt.backlog.Backlog("https://labs.example.test", "team/app")
        with self.assertRaisesRegex(RuntimeError, "merge requests"):
            rt.gitlab_threads(home, "team/app", "robin", "t", lambda *a, **k: (listed, {}, ""))


class MainTest(unittest.TestCase):
    def workspace(self, toml='[workflow]\napprover = "robin"\n'):
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
            code = rt.main(["--root", str(root), *args], gh=gh)
        return code, out.getvalue(), err.getvalue()

    def test_the_listing_puts_needs_work_first(self):
        code, out, _ = self.run_main(self.workspace(), gh=FakeGh())
        self.assertEqual(code, 0)
        lines = out.splitlines()
        self.assertTrue(lines[0].startswith("#12/101 needs work"), lines)
        self.assertTrue(lines[-1].startswith("#12/102 awaiting you"), lines)

    def test_reply_posts_to_the_threads_first_comment_and_never_resolves(self):
        gh = FakeGh()
        code, _, _ = self.run_main(self.workspace(), "--reply", "#12/101", "--body", "a41c9e2: closes the pool", gh=gh)
        self.assertEqual(code, 0)
        post = gh.calls[-1]
        self.assertIn("repos/o/app/pulls/12/comments/101/replies", post)
        self.assertIn("body=a41c9e2: closes the pool", post)
        self.assertFalse(any("resolveReviewThread" in " ".join(c) for c in gh.calls))

    def test_without_the_users_forge_name_it_says_which_setting(self):
        code, _, err = self.run_main(self.workspace(""), gh=FakeGh())
        self.assertEqual(code, 2)
        self.assertIn("approver", err)


if __name__ == "__main__":
    unittest.main()
