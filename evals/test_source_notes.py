"""source_page.py, review notes: each review thread of the change sits inline under the diff line it is on
(Source48.dc.html, the `hasNote` rows). Split view, coverage, compare modes, tags and overlap notes are out of scope.

Interface the implementer adds to `scripts/source_page.py`:

    @dataclass(frozen=True)
    class Note:
        path: str                           # the file the thread is on
        line: int | None                    # its line number on that side; None when the forge gives none (outdated)
        side: str                           # "new" or "old"
        thread: board_sources.ReviewThread

    notes(change, home, gh=backlog.run_gh, call=backlog._call) -> tuple[list[Note], str]
        # (notes, error); never raises; error "" on success. One GraphQL call for this one change: GitLab through
        # `call` with backlog.token(home), GitHub through `gh api graphql`. The cache refresh's queries are unchanged.
    render(..., root=None, notes: list[Note] | None = None, notes_error: str = "")
    write()  # calls notes(change, <the workspace's Backlog>) when the workspace has a Backlog line

Markup contract (on top of test_source_page's; everything else is free):

- A note on a line in the diff is one `<tr>` whose class list holds `note`, directly after that line's row (the `<tr>`
  whose `o` cell holds the line number for side "old", whose `n` cell holds it for side "new") in the file's
  `<table data-path=…>`. It holds the thread's state (open or resolved), author, time (HH:MM in the page's zone), first
  comment, and `<a href="<thread url>">`.
- A note whose line is not in the shown diff sits between the previous file's diff table (or the file list) and that
  file's `<table data-path=…>`.
- When the notes cannot be read, the page says why in one element and still renders the diff.
"""

import json
import os
import re
import sys
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, time, timezone
from html import escape, unescape
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_source_page as tsp  # noqa: E402  (puts scripts/ on the path)
import backlog  # noqa: E402
import board_sources as bs  # noqa: E402
from test_issue_page import ROUTE_TRACKER, ZONE  # noqa: E402
from test_source_page import LEGACY, LIMITS, NOW, QUOTA, TRACKERS, SourcePage, rows, tables, text  # noqa: E402

sp = tsp.sp


def thread(resolved: bool, hhmm: str, author: str, body: str, n: int) -> bs.ReviewThread:
    return bs.ReviewThread(resolved, author, body, datetime.combine(NOW.date(), time.fromisoformat(hhmm), NOW.tzinfo),
                           f"https://forge.example/grp/app/-/merge_requests/48#note_{n}")


OPEN_ADD = thread(False, "00:18", "reviewer-a", "Make the cap a setting", 1)            # on limits.py new 7 (added)
RESOLVED_DEL = thread(True, "01:05", "reviewer-b", "Old cap was documented", 2)         # on limits.py old 1 (removed)
OUTDATED = thread(False, "00:40", "reviewer-a", "Was this constant renamed", 3)         # limits.py, no line
OFF_HUNK = thread(False, "00:50", "reviewer-c", "Quota needs a unit", 4)                # quota.py new 99, outside hunks
MARKED = thread(False, "01:20", "<b>reviewer-d</b>", '<img src=x onerror="alert(2)"> drop this', 5)  # legacy.py old 2


class NotesPage(SourcePage):
    """!48's page rendered with notes against the real forge fixture of test_source_page."""

    notes_cache = None

    def notes_page(self) -> str:
        if sp is None or not hasattr(sp, "Note"):
            raise AssertionError("source_page.Note does not exist yet")
        if type(self).notes_cache is None:
            n = sp.Note
            type(self).notes_cache = sp.render(
                109, TRACKERS, self.sources(), NOW, self.graph, self.root / "pages", self.root,
                notes=[n(LIMITS, 7, "new", OPEN_ADD), n(LIMITS, 1, "old", RESOLVED_DEL), n(LIMITS, None, "new", OUTDATED),
                       n(QUOTA, 99, "new", OFF_HUNK), n(LEGACY, 2, "old", MARKED)])
        return type(self).notes_cache

    def after(self, path: str, side: str, line: int) -> tuple[str, str]:
        """(attributes, body) of the `<tr>` right after the row of `line` on `side` in `path`'s table."""
        trs = list(re.finditer(r"<tr\b([^>]*)>(.*?)</tr>", tables(self.notes_page())[path][1], re.S))
        cell = "o" if side == "old" else "n"
        kind = {"old": ("del", "ctx"), "new": ("add", "ctx")}[side]
        at = next((i for i, m in enumerate(trs) if (r := rows(m[0])) and r[0]["kind"] in kind
                   and (r[0][cell] or "").strip() == str(line)), None)
        self.assertIsNotNone(at, f"{path} has no row for {side} line {line}")
        self.assertLess(at + 1, len(trs), f"nothing follows {path} {side} line {line}")
        return trs[at + 1][1], trs[at + 1][2]

    # "A review thread sits inline under the diff line it is on: its state (open or resolved), author, time, first
    # comment, and a link to the thread."
    def test_a_thread_sits_inline_under_its_line(self):
        for side, line, t, state, other in (("new", 7, OPEN_ADD, "open", "resolved"),
                                            ("old", 1, RESOLVED_DEL, "resolved", "open")):
            with self.subTest(side=side, line=line):
                attrs, body = self.after(LIMITS, side, line)
                cls = re.search(r'class="([^"]*)"', attrs)
                self.assertTrue(cls and "note" in cls[1].split(), f"the row after {side} {line} is not a note: {attrs}")
                got = text(body)
                for want in (t.author, t.body, f"{t.at.astimezone(NOW.tzinfo):%H:%M}"):
                    self.assertIn(want, got)
                self.assertRegex(got, rf"(?i)\b{state}\b")
                self.assertNotRegex(got, rf"(?i)\b{other}\b")
                self.assertIn(f'href="{escape(t.url)}"', body)

    # "A thread whose line is not in the shown diff is still shown, listed with its file above that file's diff."
    def test_a_thread_off_the_diff_is_listed_above_its_files_diff(self):
        html = self.notes_page()
        spans = sorted((m.start(), m.end(), unescape(m[1])) for m in
                       re.finditer(r'<table\b[^>]*data-path="([^"]*)"[^>]*>.*?</table>', html, re.S))
        files_end = re.search(r'<ul\b[^>]*class="[^"]*\bfiles\b[^"]*"[^>]*>.*?</ul>', html, re.S).end()
        for path, t in ((LIMITS, OUTDATED), (QUOTA, OFF_HUNK)):
            with self.subTest(path=path):
                start = next(s for s, _, p in spans if p == path)
                prev = max((e for _, e, _ in spans if e <= start), default=files_end)
                self.assertEqual(html.count(t.body), 1, f"{t.body!r} is not shown exactly once")
                at = html.index(t.body)
                self.assertTrue(prev < at < start, f"{t.body!r} is not between the previous diff and {path}'s diff")
                self.assertIn(f'href="{escape(t.url)}"', html[prev:start])

    # "Thread text is shown as text, never as markup."
    def test_thread_text_is_shown_as_text(self):
        html = self.notes_page()
        for raw in (MARKED.body, MARKED.author):
            with self.subTest(raw=raw):
                self.assertNotIn(raw, html)
        _, body = self.after(LEGACY, "old", 2)
        self.assertIn(escape(MARKED.body, quote=False), body.replace("&quot;", '"').replace("&#x27;", "'"))
        self.assertIn(MARKED.author, text(body))

    def test_a_note_drops_a_trailing_hidden_html_comment(self):
        if sp is None:
            raise AssertionError("scripts/source_page.py does not exist yet")
        body = "Useful? React with 👍 / 👎.\n\n<!-- codex-code-review-finding:v1 -->"
        noted = thread(True, "02:10", "chatgpt-codex-connector", body, 9)
        html = sp.render(109, TRACKERS, self.sources(), NOW, self.graph, self.root / "pages", self.root,
                         notes=[sp.Note(LIMITS, 7, "new", noted)])
        self.assertIn("Useful? React with 👍 / 👎.", html)
        self.assertNotIn("codex-code-review-finding", html)

    # "When the notes cannot be read the page still renders its diff and says why in one line."
    def test_unreadable_notes_leave_the_diff_and_say_why(self):
        if sp is None:
            raise AssertionError("scripts/source_page.py does not exist yet")
        why = "GitLab: Query has complexity of 298, which exceeds max complexity of 250"
        html = sp.render(109, TRACKERS, self.sources(), NOW, self.graph, self.root / "pages", self.root,
                         notes=[], notes_error=why)
        self.assertTrue(re.search(rf">[^<]*{re.escape(escape(why))}[^<]*<", html), "the reason is not in one element")
        self.assertTrue(rows(tables(html).get(LIMITS, ("", ""))[1]), "the diff rows are gone")

    # "The page's own colours are palette tokens, so the adopted dark scheme reaches them." (note markup included)
    def page(self) -> str:
        return self.notes_page()

    test_own_colours_are_palette_tokens = tsp.RenderTest.test_own_colours_are_palette_tokens

    # Interface: "`write()` calls `notes()` with the workspace's Backlog when it has one."
    def test_write_asks_for_the_notes_of_the_pages_change(self):
        if sp is None or not hasattr(sp, "notes"):
            raise AssertionError("source_page.notes does not exist yet")
        root = self.root
        home = backlog.Backlog("https://forge.example", "grp/app")
        (root / "CLAUDE.md").write_text(
            "## Coordinator\n- User: Robin\n- Daily log dir: `daily/`\n- Tracker: `daily/<date>-tracker.md`\n"
            f"- Timezone: {ZONE}\n- Board: self-hosted; URL http://127.0.0.1:8765/; dir `pages/`\n"
            f"- Settings: `cos.toml`\n- Backlog: GitLab issues; host {home.host}; project {home.project}\n")
        (root / "cos.toml").write_text('[graph]\nclone = "repos/app"\nroot = "src"\n')
        (root / "daily").mkdir(exist_ok=True)
        day = datetime.now(ZoneInfo(ZONE)).date().isoformat()
        (root / "daily" / f"{day}-tracker.md").write_text(ROUTE_TRACKER.format(day=day))
        bs._write(root / "pages" / bs.CACHE, replace(self.sources(), home=home))
        asked = []

        def fake(change, home, *a, **k):
            asked.append((change.ref, home))
            return [sp.Note(LIMITS, 7, "new", OPEN_ADD)], ""

        with mock.patch.object(sp, "notes", fake):
            out = sp.write(root, root / "pages", 109)
        self.assertEqual(asked, [("!48", home)])
        self.assertIn(OPEN_ADD.body, out.read_text())

    def test_changed_home_failed_refresh_then_recovery_queries_only_the_new_change(self):
        old = backlog.Backlog("https://forge.example", "grp/app")
        new = backlog.Backlog("https://forge.example", "grp/other")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "daily").mkdir()
            (root / "cos.toml").write_text("")
            config = ("## Coordinator\n- User: Robin\n- Daily log dir: `daily/`\n"
                      "- Tracker: `daily/<date>-tracker.md`\n"
                      f"- Timezone: {ZONE}\n- Board: self-hosted; URL http://127.0.0.1:8765/; dir `pages/`\n"
                      "- Settings: `cos.toml`\n"
                      f"- Backlog: GitLab issues; host {old.host}; project {old.project}\n")
            (root / "CLAUDE.md").write_text(config)
            now = datetime.now(ZoneInfo(ZONE))
            (root / "daily" / f"{now.date().isoformat()}-tracker.md").write_text(
                ROUTE_TRACKER.format(day=now.date().isoformat()))
            bs._write(root / "pages" / bs.CACHE, self.sources())
            (root / "CLAUDE.md").write_text(config.replace(old.project, new.project))
            with mock.patch.object(bs, "forge", return_value=(None, None, {"kanban": "offline", "merge requests": "offline"})), \
                    mock.patch.object(bs, "workers", return_value=()):
                failed = bs.refresh(root, now)
            self.assertEqual(failed.home, old)
            with mock.patch.object(sp, "notes") as query:
                out = sp.write(root, root / "pages", 109)
                query.assert_not_called()
            html = out.read_text()
            self.assertNotIn("Cap uploads", html)
            self.assertIn("Nothing to diff", html)

            fresh = replace(self.mr, url=f"{new.host}/{new.project}/-/merge_requests/48", title="New project change")
            with mock.patch.object(bs, "forge", return_value=({}, {fresh.ref: fresh}, {})), \
                    mock.patch.object(bs, "workers", return_value=()):
                recovered = bs.refresh(root, now)
            self.assertEqual(recovered.home, new)
            with mock.patch.object(sp, "notes", return_value=([sp.Note(LIMITS, 7, "new", OPEN_ADD)], "")) as query:
                out = sp.write(root, root / "pages", 109)
                query.assert_called_once_with(fresh, new)
            self.assertIn(OPEN_ADD.body, out.read_text())
            self.assertNotIn("Review notes unavailable", out.read_text())

    def test_unknown_cache_home_does_not_query_notes(self):
        root = self.root
        home = backlog.Backlog("https://forge.example", "grp/app")
        (root / "CLAUDE.md").write_text(
            "## Coordinator\n- User: Robin\n- Daily log dir: `daily/`\n- Tracker: `daily/<date>-tracker.md`\n"
            f"- Timezone: {ZONE}\n- Board: self-hosted; URL http://127.0.0.1:8765/; dir `pages/`\n"
            f"- Settings: `cos.toml`\n- Backlog: GitLab issues; host {home.host}; project {home.project}\n")
        (root / "cos.toml").write_text("")
        (root / "daily").mkdir(exist_ok=True)
        day = datetime.now(ZoneInfo(ZONE)).date().isoformat()
        (root / "daily" / f"{day}-tracker.md").write_text(
            ROUTE_TRACKER.format(day=day).replace("| #109 |", "| https://forge.example/grp/app/-/issues/109 |"))
        # A legacy cache can still have an explicitly keyed issue and change, without a verified home.
        src = replace(self.sources(), home=None,
                      changes={"!48": replace(self.mr, issues=("gitlab:forge.example/grp/app#109",))})
        with mock.patch.object(bs, "load", return_value=src), mock.patch.object(sp, "notes") as query:
            out = sp.write(root, root / "pages", 109)
            query.assert_not_called()
        self.assertIn("Nothing to diff", out.read_text())


def iso(hhmm: str) -> str:
    return datetime.combine(NOW.date(), time.fromisoformat(hhmm), NOW.tzinfo).astimezone(timezone.utc).isoformat()


def answer(query: str, chain: tuple[str, ...], leaf: dict) -> dict:
    """The forge's answer shaped by the query: each field of `chain` under the alias the query gives it, if any."""
    out = leaf
    for field in reversed(chain):
        m = re.search(rf"(?:(\w+)\s*:\s*)?\b{field}\b\s*[({{]", query)
        out = {(m[1] if m and m[1] else field): out}
    return {"data": out}


GL = backlog.Backlog("https://labs.example.test", "team/app")
GL_MR = bs.Change("!48", f"{GL.host}/{GL.project}/-/merge_requests/48", "Cap uploads", "open", False, None, False, 0,
                  None, "main", (LIMITS,), ("#109",))
GH = backlog.GitHubBacklog("o/app")
GH_PR = bs.Change("#58", "https://github.com/o/app/pull/58", "Cap uploads", "open", False, None, False, 0, None, "main",
                  (LIMITS,), ("#109",))


def gl_note(author, body, hhmm, n, old, new):
    return {"author": {"username": author}, "body": body, "createdAt": iso(hhmm), "url": f"{GL_MR.url}#note_{n}",
            "position": {"filePath": LIMITS, "oldPath": LIMITS, "newPath": LIMITS, "oldLine": old, "newLine": new}}


GL_DISCUSSIONS = {"nodes": [
    {"resolvable": True, "resolved": False, "notes": {"nodes": [gl_note("reviewer-a", "Make it a setting", "00:18", 1, None, 7)]}},
    {"resolvable": True, "resolved": True, "notes": {"nodes": [gl_note("reviewer-b", "Old cap documented", "01:05", 2, 1, None)]}},
    {"resolvable": False, "resolved": False, "notes": {"nodes": [gl_note("reviewer-a", "Looks close", "01:30", 3, None, 2)]}}]}


def gh_thread(resolved, path, line, original, side, author, body, hhmm, n):
    return {"isResolved": resolved, "path": path, "line": line, "originalLine": original, "diffSide": side,
            "comments": {"nodes": [{"author": {"login": author}, "body": body, "createdAt": iso(hhmm),
                                    "url": f"{GH_PR.url}#discussion_r{n}"}]}}


GH_THREADS = {"nodes": [gh_thread(False, LIMITS, 7, 7, "RIGHT", "reviewer-a", "Make it a setting", "00:18", 1),
                        gh_thread(True, LIMITS, 1, 1, "LEFT", "reviewer-b", "Old cap documented", "01:05", 2),
                        gh_thread(False, QUOTA, None, 3, "RIGHT", "reviewer-c", "Quota needs a unit", "00:50", 3)]}

HEAVY = ("headPipeline", "diffStats", "approvedBy", "statusCheckRollup", "latestReviews", "files(")
LISTS = ("mergeRequests", "pullRequests", "issueOrPullRequest", "search(")


class NotesQueryTest(unittest.TestCase):
    """ "The positions come from one small query per page, for that one change only: GitLab asks one project's one merge
    request for its discussions' first note with its position; GitHub asks one pull request for its review threads'
    path and line." """

    def setUp(self):
        if sp is None or not hasattr(sp, "notes"):
            self.fail("source_page.notes does not exist yet")

    def gitlab(self, reply=None, err=""):
        calls = []

        def call(method, url, token, timeout, payload=None):
            calls.append((method, url, token, payload))
            if err:
                return None, {}, err
            return reply or answer(payload["query"], ("project", "mergeRequest", "discussions"), GL_DISCUSSIONS), {}, ""

        with mock.patch.dict(os.environ, {GL.env: "tok"}):
            got = sp.notes(GL_MR, GL, gh=lambda *a, **k: self.fail("gh on a GitLab backlog"), call=call)
        return got, calls

    def github(self, fail=False):
        calls = []

        def gh(args, input_text=None):
            calls.append(args)
            if fail:
                return 1, "", "gh: not logged in"
            query = next(a[6:] for a in args if a.startswith("query="))
            return 0, json.dumps(answer(query, ("repository", "pullRequest", "reviewThreads"), GH_THREADS)), ""

        got = sp.notes(GH_PR, GH, gh=gh, call=lambda *a, **k: self.fail("GitLab call on a GitHub backlog"))
        return got, calls

    def test_gitlab_asks_one_merge_request_for_its_discussions_positions(self):
        (got, error), calls = self.gitlab()
        self.assertEqual(error, "")
        self.assertEqual([(m, u, t) for m, u, t, _ in calls], [("POST", f"{GL.host}/api/graphql", "tok")])
        query, sent = calls[0][3]["query"], json.dumps(calls[0][3])
        self.assertIn("notes(first: 1)", query)
        for field in ("discussions", "position", "oldLine", "newLine"):
            self.assertRegex(query, rf"\b{field}\b")
        self.assertRegex(sent, r"\b48\b")
        self.assertIn(GL.project, sent)
        for field in HEAVY + LISTS:
            self.assertNotIn(field, query)
        self.assertEqual([(n.path, n.line, n.side, n.thread) for n in got], [
            (LIMITS, 7, "new", bs.ReviewThread(False, "reviewer-a", "Make it a setting", datetime.fromisoformat(iso("00:18")),
                                               f"{GL_MR.url}#note_1")),
            (LIMITS, 1, "old", bs.ReviewThread(True, "reviewer-b", "Old cap documented", datetime.fromisoformat(iso("01:05")),
                                               f"{GL_MR.url}#note_2"))])

    def test_github_asks_one_pull_request_for_its_threads_path_and_line(self):
        (got, error), calls = self.github()
        self.assertEqual(error, "")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][:2], ["api", "graphql"])
        query, sent = next(a for a in calls[0] if a.startswith("query=")), " ".join(calls[0])
        for field in ("reviewThreads", "path", "line", "diffSide", "isResolved"):
            self.assertRegex(query, rf"\b{field}\b")
        self.assertRegex(sent, r"\b58\b")
        for field in HEAVY + LISTS:
            self.assertNotIn(field, query)
        self.assertEqual([(n.path, n.line, n.side, n.thread.resolved, n.thread.author, n.thread.body, n.thread.url)
                          for n in got], [
            (LIMITS, 7, "new", False, "reviewer-a", "Make it a setting", f"{GH_PR.url}#discussion_r1"),
            (LIMITS, 1, "old", True, "reviewer-b", "Old cap documented", f"{GH_PR.url}#discussion_r2"),
            (QUOTA, None, "new", False, "reviewer-c", "Quota needs a unit", f"{GH_PR.url}#discussion_r3")])
        self.assertEqual(got[0].thread.at, datetime.fromisoformat(iso("00:18")))

    # "When the notes cannot be read the page still renders its diff and says why in one line."
    def test_a_failed_read_is_an_error_not_a_raise(self):
        cases = {
            "gh": lambda: self.github(fail=True)[0],
            "http": lambda: self.gitlab(err="HTTP 401 from GitLab")[0],
            "graphql": lambda: self.gitlab({"errors": [{"message": "Query has complexity of 298"}]})[0]}
        for name, run in cases.items():
            with self.subTest(name):
                got, error = run()
                self.assertEqual(got, [])
                self.assertTrue(error)
        self.assertIn("not logged in", cases["gh"]()[1])
        self.assertIn("401", cases["http"]()[1])
        self.assertIn("complexity", cases["graphql"]()[1])
        garbled = {"data": {"project": {"mergeRequest": {"discussions": {"nodes": [
            {"resolvable": True, "notes": {"nodes": [{"position": 7, "createdAt": "later"}]}}]}}}}}
        got, error = self.gitlab(garbled)[0]  # "never raises"
        self.assertIsInstance(got, list)
        self.assertIsInstance(error, str)
        with mock.patch.object(backlog, "token", return_value=""), mock.patch.dict(os.environ, {GL.env: ""}):
            got, error = sp.notes(GL_MR, GL, call=lambda *a, **k: self.fail("no call without a token"))
        self.assertEqual((got, bool(error)), ([], True))


if __name__ == "__main__":
    unittest.main()
