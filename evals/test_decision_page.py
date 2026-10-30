"""decision_page.py: a decision's full context as a page on the local pages server (Zach, 2026-09-25: "make sure
in the future you don't serve it as an artifact but serve it using the localhost server thing")."""

import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import date, datetime, timedelta
from dataclasses import replace
from html import escape, unescape
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import make_repo  # noqa: E402
import board_sources as bs  # noqa: E402
import decision_page as dp  # noqa: E402
import render_board as rb  # noqa: E402
import gantt  # noqa: E402
from backlog import Backlog, GitHubBacklog  # noqa: E402

BLOCK = """# Workspace

## Coordinator

- User: Robin
- Daily log dir: `daily/`
- Tracker: `daily/<date>-tracker.md` (sections: Tasks, Decisions, File ownership, Log)
- Timezone: America/Chicago
{board}
"""
BOARD = "- Board: self-hosted; URL http://127.0.0.1:8765/; dir `pages/`"


def assert_settled(case: unittest.TestCase, page: str) -> None:
    """An answered decision page shows the recorded answer and nothing that can post another."""
    case.assertIn('<div class="card answered">', page)
    case.assertIn('<div class="options">', page)
    case.assertIn('<div class="opt">', page)
    for absent in ("<label", "<form", 'type="radio"', 'name="words"', "Your own answer", "<button",
                   "Answer here or in chat", "<script"):
        case.assertNotIn(absent, page)


def decision(**over) -> dict:
    d = {
        "topic": "Storage",
        "headline": "The plan and the architecture disagree on where uploads go",
        "ask": "Should the worker follow the architecture?",
        "yes": "A yes tells impl-uploads to use the volume service.",
        "sections": [{"heading": "What the plan says", "text": ["Issue #7 writes to `s3://bucket` <now>."]}],
        "sides": [{"label": "Plan", "status": "open", "text": "S3."}, {"label": "Architecture", "status": "done", "text": "Volume."}],
        "timeline": [{"when": "09:40", "text": "impl-uploads stopped."}],
        "options": [{"key": "A", "title": "Follow the architecture", "text": "The bucket is dead."},
                    {"key": "B", "title": "Follow the plan", "text": "**Breaks** the architecture."}],
        "recommended": "A",
        "why": "The architecture is the newer decision.",
        "default": "The worker waits.",
        "references": [{"label": "Plan", "value": "https://github.com/o/backlog/issues/7"}],
    }
    d.update(over)
    return d


class RenderTest(unittest.TestCase):
    def test_a_full_document_holding_every_part(self):
        page = dp.render(dp.parse(decision()), "2026-09-25")
        self.assertTrue(page.startswith("<!doctype html>"))
        self.assertIn('<meta name="viewport"', page)
        for text in ("Decision · Storage · Fri 25 Sep", "Should the worker follow the architecture?",
                     "What the plan says", "Where it stands", "How it got here", "Options",
                     "Recommendation", "The architecture is the newer decision.", "If you don't answer",
                     "The worker waits.", "References", "Answer here or in chat: A, B, or C in your own words."):
            self.assertIn(text, page)

    def test_text_is_escaped_and_marked_up(self):
        page = dp.render(dp.parse(decision()), "2026-09-25")
        self.assertIn("<code>s3://bucket</code> &lt;now&gt;", page)
        self.assertIn("<b>Breaks</b>", page)
        self.assertIn('<a href="https://github.com/o/backlog/issues/7" target="_blank" rel="noopener">https://github.com/o/backlog/issues/7</a>', page)
        self.assertNotIn("<now>", page)

    def test_no_option_is_selected_until_the_user_selects_one(self):
        # The user, 2026-09-26: "I want the decisions page to NOT have something selected until I actually select it
        # for options, and to always have a write in option." Recommended is a text tag, not a filled or bold row.
        page = dp.render(dp.parse(decision()), "2026-09-25", slug="uploads")
        self.assertIn('<form class="options" method="post" action="/decisions/uploads">', page)
        self.assertIn('<label class="opt"><span class="k"><input type="radio" name="key" value="A"> A</span>'
                      '<b>Follow the architecture<span class="tag">recommended</span></b>', page)
        self.assertIn('<input type="radio" name="key" value="B"> B</span><b>Follow the plan</b>', page)
        self.assertIn('<input type="text" name="words"', page)
        self.assertIn('<button type="submit">save</button>', page)
        self.assertNotIn("checked", page)
        self.assertNotIn("opt rec", page)
        self.assertNotRegex(page, r"<script[^>]* src=")

    def test_the_write_in_is_the_next_option_in_one_grouped_card(self):
        # Canvas Decision artboard, 2026-09-26: "make this just label "D" with the same look as the other items", and
        # "all OPTIONS are a grouped thing kind of like HOW IT GOT HERE is a grouped thing".
        page = dp.render(dp.parse(decision()), "2026-09-25", slug="uploads")
        form = page[page.index('<form class="options'):page.index("</form>", page.index('<form class="options'))]
        self.assertEqual(form.count('<label class="opt">'), 3)
        wi = form[form.rindex('<label class="opt">'):]
        self.assertRegex(wi, r'<span class="k"><input type="radio" name="key" value=""> C</span><b>Your own answer</b>')
        self.assertIn('<input type="text" name="words"', wi)
        self.assertIn('<button type="submit">save</button>', wi)
        self.assertNotIn("write in", page)
        css = dp.HEAD
        self.assertRegex(css, r"\.options\{[^}]*border-top:3px solid var\(--brass\)")
        self.assertRegex(css, r"\.opt\+\.opt\{[^}]*border-top:1px dotted var\(--line\)")
        self.assertNotRegex(css, r"(?<![\w-])\.opt\{[^}]*border:1px solid")

    def test_the_list_pages_write_in_keeps_its_field_and_button_styling(self):
        # The decisions list's inline write-in (`.row .wi`) sizes its field and button; their box comes from `.wi`.
        self.assertRegex(dp.HEAD, r"(?<![\w-])\.wi input\[type=text\]\{[^}]*border:1px solid var\(--line\)")
        self.assertRegex(dp.HEAD, r"(?<![\w-])\.wi button\{[^}]*border:1px solid var\(--brass\)")

    def test_choosing_the_write_in_does_not_post_an_empty_answer(self):
        # The radios post on change; the write-in has no key, so it posts only through save with its words.
        self.assertIn("e.target.value", dp.ANSWER_JS)

    def test_a_saved_page_answer_shows_the_decision_answered(self):
        saved = {"key": "B", "words": "B, and log it", "at": "2026-09-25T02:08:00-05:00"}
        page = dp.render(dp.parse(decision(answer=saved)), "2026-09-25", slug="uploads")
        self.assertIn(">ANSWERED · B<", page)
        self.assertIn("answered, saved 02:08", page)
        self.assertIn("“B, and log it”", page)
        self.assertIn("Follow the plan", page)
        self.assertIn("<b>Breaks</b> the architecture.", page)
        self.assertIn("Follow the architecture", page)
        self.assertIn("The bucket is dead.", page)
        assert_settled(self, page)
        written = dp.render(dp.parse(decision(answer={"key": None, "words": "neither; ask the owner", "at": saved["at"]})),
                            "2026-09-25", slug="uploads")
        self.assertIn(">ANSWERED<", written)
        self.assertIn("“neither; ask the owner”", written)
        assert_settled(self, written)

    def test_a_saved_answer_is_checked(self):
        for bad, word in (({"key": "Z", "words": "", "at": "2026-09-25T02:08:00-05:00"}, "Z"),
                          ({"key": None, "words": " ", "at": "2026-09-25T02:08:00-05:00"}, "words"),
                          ({"key": "A", "words": ""}, "at")):
            with self.subTest(word=word), self.assertRaisesRegex(dp.DecisionError, word):
                dp.parse(decision(answer=bad))

    def test_a_section_may_be_one_paragraph(self):
        page = dp.render(dp.parse(decision(sections=[{"heading": "What happened", "text": "It stopped."}])), "2026-09-25")
        self.assertIn("<p>It stopped.</p>", page)

    def test_optional_parts_are_left_out(self):
        d = decision()
        for key in ("sections", "sides", "timeline", "references", "topic", "yes"):
            del d[key]
        page = dp.render(dp.parse(d), "2026-09-25")
        self.assertNotIn("Where it stands", page)
        self.assertNotIn("How it got here", page)
        self.assertNotIn("References", page)


class WriteIfChangedTest(unittest.TestCase):
    """write_if_changed keeps an unchanged page's mtime (#330) by comparing exact bytes (#338)."""

    def test_a_page_that_differs_only_by_trailing_whitespace_is_rewritten(self):
        # Must fail against a mutant that compares with rstrip(): trailing spaces are a real change.
        path = Path(tempfile.mkdtemp()) / "page.html"
        path.write_bytes(b"<p>x</p>\n")
        os.utime(path, (1_000_000, 1_000_000))
        dp.write_if_changed(path, "<p>x</p>\n  ")
        self.assertEqual(path.read_bytes(), b"<p>x</p>\n  ")
        self.assertNotEqual(path.stat().st_mtime, 1_000_000)

    def test_exact_same_bytes_keep_the_mtime(self):
        path = Path(tempfile.mkdtemp()) / "page.html"
        path.write_bytes(b"<p>x</p>\n")
        os.utime(path, (1_000_000, 1_000_000))
        dp.write_if_changed(path, "<p>x</p>\n")
        self.assertEqual(path.stat().st_mtime, 1_000_000)


class ParseTest(unittest.TestCase):
    def test_holds_and_references_must_be_lists(self):
        # #228 review: a hold is read on every board and task page, so a wrong type is an unreadable decision, not a crash.
        for bad in ({"holds": 109}, {"holds": "#109"}, {"references": ["#109"]}, {"references": {"value": "#109"}}):
            with self.subTest(bad=bad), self.assertRaises(dp.DecisionError):
                dp.parse(decision(**bad))

    def test_a_decision_missing_what_the_user_needs_is_refused(self):
        for key in ("headline", "ask", "options", "recommended", "why", "default"):
            d = decision()
            del d[key]
            with self.subTest(key=key), self.assertRaisesRegex(dp.DecisionError, key):
                dp.parse(d)

    def test_one_option_is_not_a_decision(self):
        with self.assertRaisesRegex(dp.DecisionError, "two options"):
            dp.parse(decision(options=decision()["options"][:1]))

    def test_the_recommendation_names_an_option(self):
        with self.assertRaisesRegex(dp.DecisionError, "recommended"):
            dp.parse(decision(recommended="C"))

    def test_a_side_is_open_or_done(self):
        with self.assertRaisesRegex(dp.DecisionError, "status"):
            dp.parse(decision(sides=[{"label": "x", "status": "maybe", "text": "y"}]))


GH = GitHubBacklog(repo="o/app")
GL = Backlog(host="https://gl.example", project="grp/app")


def refs(text: str, forge=None, root=None, at=None) -> str:
    return dp._md(text, dp.Linker(forge, root, at))


class LinkTest(unittest.TestCase):
    """References in the text link to the configured forge; nothing is linked that the forge cannot place."""

    def test_github(self):
        out = refs("#12, !58, a41c9e2, src/cache/pool.py:24-58, config/db.toml:14, pipeline #8812, src/gateway/ "
                   "and docs/architecture.md#connection-budgets", GH)
        for kind, href, label in (
                ("issue", "https://github.com/o/app/issues/12", "#12"),
                ("PR", "https://github.com/o/app/pull/58", "!58"),
                ("commit", "https://github.com/o/app/commit/a41c9e2", "a41c9e2"),
                ("code", "https://github.com/o/app/blob/HEAD/src/cache/pool.py#L24-L58", "src/cache/pool.py:24-58"),
                ("code", "https://github.com/o/app/blob/HEAD/config/db.toml#L14", "config/db.toml:14"),
                ("pipeline", "https://github.com/o/app/actions/runs/8812", "#8812"),
                ("code", "https://github.com/o/app/tree/HEAD/src/gateway/", "src/gateway/"),
                ("doc", "https://github.com/o/app/blob/HEAD/docs/architecture.md#connection-budgets",
                 "docs/architecture.md#connection-budgets")):
            with self.subTest(label=label):
                self.assertIn(f'<a class="ref" data-k="{kind}" href="{href}" target="_blank" rel="noopener">{label}</a>', out)

    def test_gitlab(self):
        out = refs("#12, !58, !58#note_2291, a41c9e2, src/cache/pool.py:24-58, pipeline #8812", GL)
        for kind, href in (("issue", "https://gl.example/grp/app/-/issues/12"),
                           ("MR", "https://gl.example/grp/app/-/merge_requests/58"),
                           ("note", "https://gl.example/grp/app/-/merge_requests/58#note_2291"),
                           ("commit", "https://gl.example/grp/app/-/commit/a41c9e2"),
                           ("code", "https://gl.example/grp/app/-/blob/HEAD/src/cache/pool.py#L24-58"),
                           ("pipeline", "https://gl.example/grp/app/-/pipelines/8812")):
            with self.subTest(kind=kind):
                self.assertIn(f'data-k="{kind}" href="{href}"', out)

    def test_no_forge_links_nothing(self):
        self.assertNotIn("<a", refs("#12, !58, a41c9e2, src/cache/pool.py:24, pipeline #8812"))

    def test_what_is_not_a_reference_stays_text(self):
        text = "colour #c9533a, e.g. this, on 2026-09-25, a defaced cafe, o/backlog#7, v1.2 and 3.5 hours"
        self.assertEqual(refs(text, GH), text)

    def test_a_code_span_that_is_one_reference_links(self):
        self.assertEqual(refs("`src/app.py:3`", GH),
                         '<a class="ref" data-k="code" href="https://github.com/o/app/blob/HEAD/src/app.py#L3" target="_blank" rel="noopener">src/app.py:3</a>')
        self.assertEqual(refs("`s3://bucket` and `make test`", GH), "<code>s3://bucket</code> and <code>make test</code>")

    def test_sections_and_symbols_resolve_against_a_local_copy(self):
        root = Path(tempfile.mkdtemp())
        (root / "docs").mkdir()
        (root / "docs" / "architecture.md").write_text("# Arch\n\n## 4.1 Queues\n\n## 4.2 Connection budgets\n")
        (root / "src").mkdir()
        (root / "src" / "pool.py").write_text("import os\n\n\ndef warm(n):\n    pass\n")
        self.assertIn('href="https://github.com/o/app/blob/HEAD/docs/architecture.md#42-connection-budgets" target="_blank" rel="noopener">'
                      'docs/architecture.md §4.2</a>', refs("docs/architecture.md §4.2", GH, root))
        self.assertIn('href="https://github.com/o/app/blob/HEAD/src/pool.py#L4" target="_blank" rel="noopener">src/pool.py:warm</a>',
                      refs("src/pool.py:warm", GH, root))
        # Without the copy the file is still the right place; the section is not guessed.
        self.assertIn('href="https://github.com/o/app/blob/HEAD/docs/architecture.md" target="_blank" rel="noopener">docs/architecture.md §4.2</a>',
                      refs("docs/architecture.md §4.2", GH))

    def test_a_pinned_reference_links_at_its_commit(self):
        self.assertIn("https://github.com/o/app/blob/a41c9e2/src/cache/pool.py#L24-L58",
                      refs("src/cache/pool.py:24-58", GH, at="a41c9e2"))


ARCH = {
    "summary": "Requests reach the cache, which the warm pool keeps connected to the database.",
    "nodes": [
        {"id": "clients", "name": "Clients", "state": "external", "detail": "browser, mobile"},
        {"id": "cache", "name": "Cache", "state": "deployed", "detail": "src/cache/client.py"},
        {"id": "db", "name": "Database", "state": "external", "detail": "limit 320 connections", "hot": True},
        {"id": "pool", "name": "Warm pool", "state": "inflight", "detail": "src/cache/pool.py"},
        {"id": "budget", "name": "Connection budget", "state": "designed", "detail": "docs/architecture.md §4.2"},
    ],
    "edges": [
        {"from": "clients", "to": "cache", "label": "get / set"},
        {"from": "cache", "to": "db", "label": "miss: query"},
        {"from": "pool", "to": "db", "label": "holds 20 open", "state": "inflight"},
        {"from": "pool", "to": "cache", "label": "warms", "state": "inflight"},
        {"from": "budget", "to": "pool", "label": "caps", "state": "designed"},
    ],
    "not_drawn": "auth, the job queue.",
}


class DesignTest(unittest.TestCase):
    def page(self, forge=None, **over) -> str:
        return dp.render(dp.parse(decision(**over)), "2026-09-25", forge)

    def test_where_it_sits_draws_the_architecture_in_the_review_encoding(self):
        page = self.page(GH, architecture=ARCH)
        self.assertIn("Where it sits", page)
        self.assertIn(f'aria-label="{ARCH["summary"]}"', page)
        for cls in ('class="ext"', 'class="n"', 'class="n-inflight"', 'class="n-designed"', 'class="ext hot"',
                    'class="e"', 'class="e-inflight"', 'class="e-designed"'):
            self.assertIn(cls, page)
        for word in ("get / set", "holds 20 open", "Warm pool", "Not drawn: auth, the job queue."):
            self.assertIn(word, page)
        self.assertIn('<a href="https://github.com/o/app/blob/HEAD/src/cache/pool.py" target="_blank" rel="noopener">', page)
        for chip in ("DEPLOYED", "IN FLIGHT", "DESIGNED", "EXTERNAL", "WAITING ON THIS"):
            self.assertIn(chip, page)

    def test_every_edge_of_an_acyclic_figure_points_right(self):
        page = self.page(architecture=ARCH)
        x = {m[1]: float(m[2]) for m in re.finditer(r'<g data-node="([^"]+)"><rect[^>]* x="([\d.]+)"', page)}
        self.assertEqual(set(x), {n["id"] for n in ARCH["nodes"]})
        for e in ARCH["edges"]:
            with self.subTest(edge=e["label"]):
                self.assertLess(x[e["from"]], x[e["to"]])

    def test_an_architecture_is_checked(self):
        for arch, word in (({"nodes": [{"id": "a", "name": "A", "state": "shipped"}], "edges": []}, "state"),
                           ({"nodes": [{"id": "a", "name": "A"}], "edges": [{"from": "a", "to": "z"}]}, "z")):
            with self.subTest(word=word), self.assertRaisesRegex(dp.DecisionError, word):
                dp.parse(decision(architecture=arch))

    def test_how_it_got_here_colours_each_step(self):
        page = self.page(GH, timeline=[{"when": "Fri 16:20", "text": "Deploy timed out.", "kind": "hot"},
                                       {"when": "01:41", "text": "Held in triage. #111", "kind": "triage"},
                                       {"when": "02:00", "text": "No kind."}])
        self.assertIn('<span class="dot" data-k="hot"></span>', page)
        self.assertIn('<span class="dot" data-k="triage"></span>', page)
        self.assertIn('<span class="dot"></span>', page)
        self.assertIn('href="https://github.com/o/app/issues/111"', page)

    def test_references_carry_kind_status_and_pin(self):
        page = self.page(GH, references=[{"kind": "code", "value": "src/cache/pool.py:24-58", "at": "a41c9e2"},
                                         {"kind": "MR", "value": "!58", "label": "Warm the cache pool", "status": "open · passed"},
                                         {"label": "Plan", "value": "https://github.com/o/backlog/issues/7"}])
        self.assertIn("blob/a41c9e2/src/cache/pool.py#L24-L58", page)
        self.assertIn("@ a41c9e2", page)
        self.assertIn("Warm the cache pool", page)
        self.assertIn("open · passed", page)
        self.assertRegex(page, r'<span class="cap">Plan</span>')

    def test_the_chip_says_pending_until_the_answer_is_recorded(self):
        self.assertIn(">PENDING<", self.page())
        answered = self.page(answer="B")
        self.assertIn(">ANSWERED · B<", answered)
        self.assertIn('<div class="card answered"><span><span class="key">B</span>Follow the plan</span>'
                      '<span class="saved">answered</span></div>', answered)
        assert_settled(self, answered)
        with self.assertRaisesRegex(dp.DecisionError, "answer"):
            dp.parse(decision(answer="Z"))

    def test_the_page_wears_the_board_scheme(self):
        page = self.page()
        self.assertIn(rb.FONTS, page)
        board, mine = scheme(board_css()), scheme(page)
        for theme in ("light", "dark"):
            for token in SHARED:
                with self.subTest(theme=theme, token=token):
                    self.assertEqual(mine[theme][token], board[theme][token])

    def test_a_node_name_is_never_stroked(self):
        page = self.page(architecture=ARCH)
        stroked = {"n", "n-inflight", "n-designed", "ext"}
        for classes in re.findall(r'<text class="([^"]*)"', page):
            with self.subTest(classes=classes):
                self.assertFalse(stroked & set(classes.split()))


TZ = ZoneInfo("America/Chicago")
NOW = datetime(2026, 9, 26, 2, 10, tzinfo=TZ)


def at(hhmm: str, day: int = 26) -> datetime:
    h, m = map(int, hhmm.split(":"))
    return datetime(2026, 9, day, h, m, tzinfo=TZ)


def row(hhmm: str, item: str, words: str, day: int = 26) -> dp.Row:
    return dp.Row(date(2026, 9, day), at(hhmm, day), item, words)


def task(name: str, item: str, stage: str = "", issue: str = "") -> rb.Task:
    return rb.Task(item=item, owner="impl", state="running 01:00", since="01:00", due="", checklist="", name=name,
                   issue=issue, lane="build" if stage else "", stage=stage)


def change(ref: str, state: str = "open", pipeline: str | None = "passed", merged: datetime | None = None,
           issues: tuple[str, ...] = ()) -> bs.Change:
    return bs.Change(ref, f"https://x/{ref}", "t", state, False, pipeline, True, 1, merged, "main", (), issues)


def issue(ref: str, state: str = "open", labels: tuple[str, ...] = ()) -> bs.Issue:
    return bs.Issue(ref, f"https://x/{ref}", "t", state, labels)


SOURCES = bs.Sources({}, {"#111": issue("#111", labels=("status::hold",)), "#96": issue("#96", "closed")},
                     {"!58": change("!58"), "!52": change("!52", pipeline="failed"),
                      "!60": change("!60", "merged", merged=at("01:15"), issues=("#99",)),
                      "!61": change("!61", pipeline="running", issues=("#109",))}, (), {})


class DerivationTest(unittest.TestCase):
    """Every field on the decisions pages comes from a source: the JSON, the tracker, or the forge."""

    def ctx(self, **over) -> dp.Context:
        return dp.Context(**{"now": NOW, **over})

    def test_asked_is_the_json_then_the_log_line_naming_the_page_then_the_file(self):
        mtime = at("00:30").timestamp()
        log = ((at("23:50", 25), "asked Robin about the cache"), (at("01:41"), "asked decision-cache-warmup"),
               (at("01:50"), "again decision-cache-warmup"))
        ctx = self.ctx(log=log)
        self.assertEqual(ctx.asked("cache-warmup", {"asked": "01:05"}, mtime), at("01:05"))
        self.assertEqual(ctx.asked("cache-warmup", {}, mtime), at("01:41"))
        self.assertEqual(ctx.asked("other", {}, mtime), at("00:30"))
        self.assertIsNone(ctx.asked("other", {}, None))

    def test_the_answer_is_the_latest_decisions_row_naming_the_page(self):
        ctx = self.ctx(rows=(row("00:10", "option A of decision-cache-warmup-v2", '"A"'),
                             row("00:20", "option B of decision-cache-warmup", '"B, smaller"'),
                             row("00:40", "decision-cache-warmup: changed to C", '"make it C"')))
        self.assertEqual(ctx.answer("cache-warmup").words, '"make it C"')
        self.assertIsNone(ctx.answer("cache"))

    def test_the_key_is_the_option_the_words_name(self):
        ctx = self.ctx()
        keys = ["A", "B", "C"]
        self.assertEqual(ctx.key(row("01:00", "decision-x", '"B, go"'), keys), "B")
        self.assertEqual(ctx.key(row("01:00", "decision-x", '"go with C please"'), keys), "C")
        self.assertEqual(ctx.key(row("01:00", "option A of decision-x", '"yes"'), keys), "A")
        self.assertEqual(ctx.key(row("01:00", "decision-x", '"a fine idea"'), keys), "")

    def test_holding_is_the_json_refs_and_the_tasks_that_name_the_page(self):
        d = decision(holds=["#111"], references=[{"kind": "MR", "value": "!58"}, {"kind": "code", "value": "src/a.py:3"},
                                                 {"kind": "issue", "value": "https://github.com/o/app/issues/111"}])
        tasks = (task("Cache", "Warm pool; waits on decision-cache-warmup. https://github.com/o/app/pull/61", "review", "#110"),
                 task("Other", "Unrelated #5", "fix", "#5"))
        self.assertEqual(self.ctx(tasks=tasks).holding("cache-warmup", d), ["#111", "!58", "#110", "#61"])
        self.assertEqual(self.ctx().holding("cache-warmup", decision()), ["#7"])  # the plan's issue URL
        self.assertEqual(self.ctx().holding("cache-warmup", decision(references=[])), [])

    def test_where_a_ref_stands_joins_the_tracker_stage_and_the_forge(self):
        tasks = (task("Budget", "Connection budget", "triage", "#111"), task("Pool", "Warm pool !58", "merge"),
                 task("Upload", "Upload limit", "review", "#109"))
        ctx = self.ctx(tasks=tasks, sources=SOURCES, hold="status::hold")
        self.assertEqual(ctx.where("#111"), "triage · hold")
        self.assertEqual(ctx.where("!58"), "merge · passed")
        self.assertEqual(ctx.where("!52"), "open · failed")
        self.assertEqual(ctx.where("#99"), "merged 01:15")
        self.assertEqual(ctx.where("#109"), "review · running")
        self.assertEqual(ctx.where("#96"), "closed")
        # With no sources the tracker alone speaks, and a ref it does not hold says nothing.
        self.assertEqual(self.ctx(tasks=tasks).where("#111"), "triage")
        self.assertEqual(self.ctx().where("#7"), "")

    def test_a_github_pr_written_with_a_bang_finds_its_change(self):
        ctx = self.ctx(sources=bs.Sources({}, {}, {"#58": change("#58")}, (), {}))
        self.assertEqual(ctx.where("!58"), "open · passed")

    def test_spans_read_as_the_design_writes_them(self):
        for minutes, text in ((29, "29m"), (78, "1h 18m"), (240, "4h"), (125, "2h 05m"), (1500, "1d 1h")):
            with self.subTest(minutes=minutes):
                self.assertEqual(dp.span(timedelta(minutes=minutes)), text)


def pages_dir(**decisions) -> Path:
    pages = Path(tempfile.mkdtemp())
    for n, (slug, d) in enumerate(decisions.items()):
        (pages / f"decision-{slug}.json").write_text(d if isinstance(d, str) else json.dumps(d))
        os.utime(pages / f"decision-{slug}.json", (at("00:00").timestamp() + 60 * n,) * 2)
    return pages


class HeldRefsTest(unittest.TestCase):
    """#228 review: held_refs keys each held issue as the board files it (tracker.issue_key), so a task's hold compares
    like with like and another project's #12 is not this project's #12."""

    def held(self, *holds) -> set[str]:
        ctx = dp.Context(NOW, sources=replace(bs.EMPTY, home=GitHubBacklog("o/app")))
        return set(dp.held_refs([("x", decision(holds=list(holds), references=[]), None, "")], ctx))

    def test_another_projects_issue_is_held_under_its_own_key(self):
        self.assertEqual(self.held("other/lib#12"), {"other/lib#12"})
        self.assertEqual(self.held("https://github.com/other/lib/issues/12"), {"other/lib#12"})

    def test_a_home_issue_in_any_form_is_one_key(self):
        self.assertEqual(self.held("#12", "o/app#12", "https://github.com/o/app/issues/12"), {"#12"})

    def test_a_change_stays_a_change(self):
        self.assertEqual(self.held("!58"), {"!58"})

    def test_a_held_ref_dates_from_the_first_decision_asked_that_holds_it(self):
        # #229: when the hold began; a decision with no known time does not move it.
        ctx = dp.Context(NOW, sources=replace(bs.EMPTY, home=GitHubBacklog("o/app")))
        d = decision(holds=["#12"], references=[])
        asked = [("late", d, NOW, ""), ("unknown", d, None, ""), ("early", d, NOW - timedelta(hours=1), "")]
        self.assertEqual(dp.held_refs(asked, ctx), {"#12": NOW - timedelta(hours=1)})
        self.assertEqual(dp.held_refs([("unknown", d, None, "")], ctx), {"#12": None})


class IndexTest(unittest.TestCase):
    """decisions.html is a list of pending and completed decisions (Decisions.dc.html)."""

    def test_no_header_prints_a_rendered_clock(self) -> None:
        # #215: the Decisions header keeps its pending, saved and completed counts, not a render clock.
        page = self.render(dp.Context(NOW), **{"cache-warmup": decision()})
        self.assertNotRegex(page, r"\brendered \d{1,2}:\d{2}")
        self.assertIn("1 pending", page)

    def render(self, ctx: dp.Context, **decisions) -> str:
        return dp.index(pages_dir(**decisions), ctx, "2026-09-26")[0]

    def test_a_pending_row_carries_every_column(self):
        warm = decision(topic="cache", headline="Cache warmup", ask="Keep the pool?", holds=["#111"], references=[{"kind": "MR", "value": "!58"}],
                        options=[{"key": "A", "title": "keep the pool"}, {"key": "B", "title": "start cold"}],
                        default="The pool stays.")
        ctx = dp.Context(NOW, log=((at("01:41"), "asked decision-cache-warmup"),),
                         tasks=(task("Budget", "Connection budget", "triage", "#111"), task("Pool", "Warm pool !58", "merge")),
                         sources=SOURCES, hold="status::hold")
        page = self.render(ctx, **{"cache-warmup": warm})
        pending = page.split("COMPLETED")[0]
        self.assertIn("<h1>Decisions · Sat 26 Sep</h1>", page)
        for text in ('<span class="when">01:41</span>', '<span class="age">29m</span>', '<a class="headline" href="/decisions/cache-warmup">Cache warmup</a>',
                     '<span class="topic">cache</span>', "Keep the pool?",
                     '<label class="o"><input type="radio" name="key" value="A"><span class="key">A</span>keep the pool<span class="tag">recommended</span></label>',
                     '<label class="o"><input type="radio" name="key" value="B"><span class="key">B</span>start cold</label>',
                     '<form class="stack" method="post" action="/decisions/cache-warmup">', '<input type="hidden" name="back" value="/decisions">',
                     '<input type="text" name="words"', '<button type="submit">save</button>', '<a class="ref" href="https://x/#111" target="_blank" rel="noopener">#111</a> triage · hold', '<a class="ref" href="https://x/!58" target="_blank" rel="noopener">!58</a> merge · passed',
                     "The pool stays."):
            with self.subTest(text=text):
                self.assertIn(text, pending)
        self.assertNotIn("checked", pending)
        self.assertNotRegex(pending, r'class="[^"]*\brec\b')
        self.assertIn("1 pending · oldest 29m · holding 1 issue, 1 merge request · 0 completed today", page)  # (#215) no render clock

    def test_two_decisions_holding_one_issue_hold_it_once(self):
        page = self.render(dp.Context(NOW), a=decision(holds=["#111"], references=[]), b=decision(holds=["#111"], references=[]))
        self.assertRegex(page, r"holding 1 issue\b")

    def test_a_saved_page_answer_is_an_answered_row_with_the_words(self):
        saved = {"key": "B", "words": "B, and log each retry", "at": at("02:08").isoformat()}
        page = self.render(dp.Context(NOW), retry=decision(answer=saved), other=decision())
        pending = page.split("COMPLETED")[0]
        self.assertIn('<div class="row pend saved">', pending)
        self.assertIn('<span class="key">B</span>Follow the plan', pending)
        self.assertIn('<span class="words">“B, and log each retry”</span>', pending)
        self.assertIn('<span class="saved">answered, saved 02:08</span>', pending)
        self.assertEqual(pending.count("<form"), 1)
        self.assertIn("2 pending · 1 saved ·", page)  # (#215) no render clock before the counts

    def test_without_a_topic_there_is_no_chip(self):
        page = self.render(dp.Context(NOW), x=decision(topic=None))
        self.assertNotIn('class="topic"', page)

    def test_an_unreadable_file_is_a_dashed_row_naming_it_and_why(self):
        d = decision()
        del d["default"]
        page = self.render(dp.Context(NOW), good=decision(), bad=d, broken="{")
        self.assertIn('<div class="row pend unread">', page)
        self.assertIn("decision-bad.json", page)
        self.assertIn("unreadable: the decision has no default", page)
        self.assertIn("decision-broken.json", page)
        self.assertIn("· 2 unreadable ·", page)
        self.assertIn("1 pending", page)

    def test_a_completed_row_carries_every_column(self):
        warm = decision(headline="Cache warmup", options=[{"key": "A", "title": "keep the pool"}, {"key": "B", "title": "start cold"}],
                        holds=["#99"])
        ctx = dp.Context(NOW, rows=(row("23:00", "decision-cache-warmup old", '"A"', 25),
                                    row("00:20", "Upload size limit #109", '"yes, dispatch it"'),
                                    row("01:15", "option B of decision-cache-warmup", '"B, start cold"')),
                         log=((at("23:10", 25), "asked decision-cache-warmup"),), sources=SOURCES,
                         tasks=(task("Upload", "Upload limit", "review", "#109"),))
        page = self.render(ctx, **{"cache-warmup": warm})
        done = page.split("COMPLETED")[1]
        self.assertLess(done.index("01:15"), done.index("00:20"))
        for text in ('<span class="when">01:15</span>', '<span class="took">in 2h 05m</span>',
                     '<a class="headline" href="/decisions/cache-warmup">Cache warmup</a>', '<span class="page">decision-cache-warmup</span>',
                     '<span class="key">B</span>start cold', '<span class="words">&quot;B, start cold&quot;</span>',
                     '<a class="ref">#99</a> merged 01:15',
                     "Upload size limit #109", '<span class="page">Decisions row</span>', '<span class="key">yes</span>dispatch it',
                     '<a class="ref">#109</a> review · running'):
            with self.subTest(text=text):
                self.assertIn(text, done)
        self.assertNotIn("decision-cache-warmup old", done)
        self.assertIn("0 pending", page)
        self.assertIn("2 completed today", page)

    def test_completed_rows_run_newest_first_whatever_the_tracker_order(self):
        # The user, 2026-09-26: "decisions list page needs to go in reverse chronological order". The tracker's
        # Decisions table is not kept in time order, so the list sorts by each row's time.
        ctx = dp.Context(NOW, rows=(row("01:15", "Second #1", '"yes"'), row("00:05", "First #2", '"yes"'),
                                    row("01:40", "Third #3", '"yes"'), row("00:20", "Fourth #4", '"yes"')))
        done = self.render(ctx).split("COMPLETED")[1]
        whens = re.findall(r'<span class="when">(\d\d:\d\d)</span>', done)
        self.assertEqual(whens, ["01:40", "01:15", "00:20", "00:05"])

    def test_it_renders_with_empty_sources(self):
        page = self.render(dp.Context(NOW, sources=bs.EMPTY), x=decision(holds=["#111"]))
        self.assertIn('<a class="ref">#111</a>', page)

    def test_a_source_that_could_not_be_read_is_named_in_the_header(self):
        page = self.render(dp.Context(NOW, sources=bs.Sources({}, {}, {}, (), {"merge requests": "gh: not logged in"})))
        self.assertIn("merge requests: gh: not logged in", page)

    def test_text_is_escaped(self):
        page = self.render(dp.Context(NOW, rows=(row("01:00", "<b>x</b>", "<i>"),)), x=decision(headline="<script>", topic="<t>"))
        self.assertEqual(page.count("<script>"), 1)  # the page's own answer script; the headline is escaped
        self.assertNotIn("<b>x</b>", page)
        self.assertNotIn("<t>", page)

    def test_the_page_wears_the_board_scheme(self):
        page = self.render(dp.Context(NOW))
        board, mine = scheme(board_css()), scheme(page)
        for token in SHARED:
            self.assertEqual(mine["light"][token], board["light"][token])


class StoreIndexTest(unittest.TestCase):
    """Issue #895: on an engine workspace (store.db present), decisions.html lists open store decisions in PENDING and
    today's answered store decisions in COMPLETED, matching decision_actions.tab_counts."""

    def setUp(self):
        import store
        import task_forge
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.pages = self.tmp / "pages"
        self.pages.mkdir()
        self.db_path = self.tmp / "store.db"
        store.migrate(self.db_path)
        self.store = store.Store(self.db_path)
        self.addCleanup(self.store.engine.dispose)
        self.page_data = lambda: task_forge.page_data(self.store)

    def test_pending_store_decisions_render_and_match_tab_counts(self):
        import decision_actions
        self.store.upsert_item("github:example/repo#10", title="Auth flow", stage="implement", forge_state="open")
        self.store.upsert_item("github:example/repo#11", title="Closed item", stage="merge", forge_state="closed")
        self.store.add_decision("github:example/repo#10", "scope", "Split auth token refresh?",
                                asked_at="2026-09-26T01:15:00-05:00")
        self.store.add_decision("github:example/repo#10", "plan", "Approve rollout gate?",
                                asked_at="2026-09-26T01:45:00-05:00",
                                record={"status": "blocked", "blocked_reason": "awaiting owner"})
        self.store.add_decision("github:example/repo#11", "scope", "Stale question on closed issue?",
                                asked_at="2026-09-26T01:50:00-05:00")
        data = self.page_data()
        nav = decision_actions.tab_counts(data.items(), data.decisions(), data.segments())
        self.assertEqual(nav["decisions"], 2)

        dp.write_all(self.pages, dp.Context(NOW), "2026-09-26", db=data, navigation=nav)
        page = (self.pages / "decisions.html").read_text()
        pending, done = page.split("COMPLETED")
        self.assertIn("2 pending", page)
        self.assertIn("PENDING · 2", page)
        self.assertIn('<span class="badge" title="2 pending">2</span>', page)
        self.assertLess(pending.index("Approve rollout gate?"), pending.index("Split auth token refresh?"))
        self.assertIn("BLOCKED · awaiting owner", pending)
        self.assertIn('href="/issues/github%3Aexample%2Frepo%2310"', pending)
        self.assertIn("example/repo#10", pending)
        self.assertIn('<form method="post" action="/" class="decision-form">', pending)
        # #1124: blocked means an intent failed permanently and a newer answer clears it, so it keeps its form.
        blocked_row = re.search(r'<div class="row pend" data-status="blocked">.*?</div>', pending, re.S)
        self.assertIsNotNone(blocked_row)
        self.assertIn('<form method="post" action="/" class="decision-form">', blocked_row.group(0))
        self.assertNotIn("Answered · engine waiting", page)
        self.assertNotIn("Stale question on closed issue?", page)
        self.assertIn("0 completed today", page)
        self.assertIn(" · 0</h2>", done)

    def test_completed_store_decisions_answered_today_in_workspace_zone_appear_under_completed(self):
        self.store.upsert_item("github:example/repo#10", title="Auth flow", stage="implement", forge_state="open")
        # 2026-09-26T06:40:00Z is 01:40 CDT on 2026-09-26 in America/Chicago.
        self.store.add_decision("github:example/repo#10", "permission", "Permission requested for Bash",
                                answer="allow", decided_by="trio", asked_at="2026-09-26T00:30:00-05:00",
                                record={"status": "answered", "answered_at": "2026-09-26T06:40:00Z"})
        self.store.add_decision("github:example/repo#10", "design", "Use volume service?",
                                answer="option A", decided_by="user", asked_at="2026-09-26T01:00:00-05:00",
                                record={"status": "answered", "answered_at": "2026-09-26T01:20:00-05:00"})
        # 2026-09-26T03:30:00Z is 22:30 CDT on 2026-09-25 in America/Chicago (yesterday).
        self.store.add_decision("github:example/repo#10", "scope", "Yesterday in Chicago",
                                answer="yes", decided_by="trio", asked_at="2026-09-25T22:00:00-05:00",
                                record={"status": "answered", "answered_at": "2026-09-26T03:30:00Z"})
        data = self.page_data()
        page, _ = dp.index(self.pages, dp.Context(NOW), "2026-09-26", db=data)
        done = page.split("COMPLETED")[1]
        self.assertIn("0 pending", page)
        self.assertIn("2 completed today", page)
        self.assertIn(" · 2</h2>", done)
        self.assertLess(done.index("Permission requested for Bash"), done.index("Use volume service?"))
        for text in ("Permission requested for Bash", "allow", "trio", "Use volume service?", "option A", "user",
                     'href="/issues/github%3Aexample%2Frepo%2310"', "example/repo#10"):
            with self.subTest(text=text):
                self.assertIn(text, done)
        self.assertNotIn("Yesterday in Chicago", done)
        self.assertNotIn("<form", done)  # #924: an answered row is read-only

    def test_store_rows_label_the_home_repo_short_and_a_foreign_one_by_name(self):
        """#903: a row for the workspace backlog reads `#N`; another project names itself. The href stays the full key."""
        from fragment import issue_href
        own, foreign = "github:team/app#9", "github:o/n#4"
        for key, question in ((own, "Ship?"), (foreign, "Wait?")):
            self.store.upsert_item(key, title=question, stage="implement", forge_state="open")
            self.store.add_decision(key, "scope", question, asked_at=NOW.isoformat())
        page, _ = dp.index(self.pages, dp.Context(NOW, forge=GitHubBacklog("team/app")), NOW.date().isoformat(),
                           db=self.page_data())
        self.assertIn(f'href="{escape(issue_href(own))}">#9</a>', page)
        self.assertIn(f'href="{escape(issue_href(foreign))}">o/n#4</a>', page)
        self.assertNotIn(">github:", page)

    def test_a_store_decision_shows_its_recorded_default_and_an_old_row_says_none_was_recorded(self):
        """#1128: "If you don't answer" is the default recorded with the decision, on its page and in the list."""
        key = "github:example/repo#10"
        self.store.upsert_item(key, title="Auth flow", stage="implement", forge_state="open")
        new = self.store.add_decision(key, "scope", "Approve this file ownership?", None, "zach", asked_at=NOW.isoformat(),
                                      record={"default": "Not written: nothing is written unapproved.",
                                              "status": "blocked", "blocked_reason": "awaiting owner"})
        old = self.store.add_decision(key, "design", "Split the parser?", asked_at=NOW.isoformat())
        rows = self.page_data().decision_rows
        page = dp.store_page(rows, new.id, dp.Context(NOW))
        fallback = page.split("If you don't answer")[1]
        self.assertIn("Not written: nothing is written unapproved.", fallback)
        self.assertNotIn("awaiting owner", fallback)
        self.assertNotIn("worker waits", page)
        self.assertIn("default not recorded", dp.store_page(rows, old.id, dp.Context(NOW)).split("If you don't answer")[1])
        listed, _ = dp.index(self.pages, dp.Context(NOW), NOW.date().isoformat(), db=self.page_data())
        self.assertIn('<span class="fallback">Not written: nothing is written unapproved.</span>', listed)
        self.assertIn('<span class="fallback">default not recorded</span>', listed)


class DerivedPageTest(unittest.TestCase):
    """The decision page applies the same derivations when its JSON does not carry them."""

    def test_asked_answer_and_the_chip_come_from_the_tracker(self):
        ctx = dp.Context(NOW, rows=(row("01:15", "option B of decision-x", '"B"'),), log=((at("00:40"), "asked decision-x"),))
        page = dp.render(dp.parse(decision()), "2026-09-26", ctx=ctx, slug="x")
        self.assertIn("asked 00:40", page)
        self.assertIn(">ANSWERED · B<", page)
        self.assertIn('<span class="key">B</span>Follow the plan', page)
        self.assertIn("“B”", page)
        self.assertIn('<span class="saved">answered 01:15</span>', page)
        assert_settled(self, page)
        page = dp.render(dp.parse(decision(asked="00:10")), "2026-09-26", ctx=dp.Context(NOW), slug="x", mtime=at("00:05").timestamp())
        self.assertIn("asked 00:10", page)
        self.assertIn(">PENDING<", page)

    def test_a_reference_without_a_status_takes_it_from_the_sources(self):
        refs = [{"kind": "MR", "value": "!58"}, {"kind": "issue", "value": "#111"}, {"kind": "issue", "value": "#96", "status": "said"}]
        ctx = dp.Context(NOW, tasks=(task("Budget", "Connection budget", "triage", "#111"),), sources=SOURCES, hold="status::hold")
        page = dp.render(dp.parse(decision(references=refs)), "2026-09-26", ctx=ctx, slug="x")
        self.assertIn('<span class="st">open · passed</span>', page)
        self.assertIn('<span class="st">triage · hold</span>', page)
        self.assertIn('<span class="st">said</span>', page)

    def test_without_sources_the_page_still_renders(self):
        page = dp.render(dp.parse(decision(references=[{"kind": "MR", "value": "!58"}])), "2026-09-26", ctx=dp.Context(NOW), slug="x")
        self.assertIn('<span class="st"></span>', page)

    def test_a_decisions_row_renders_read_only_and_outranks_a_saved_page_answer(self):
        words = "let's go ahead and fix the toml issues preventing page rendering please"
        ctx = dp.Context(NOW, rows=(row("01:15", "decision-x", f'"{words}"'),))
        page = dp.render(dp.parse(decision()), "2026-09-26", ctx=ctx, slug="x")
        self.assertIn(">ANSWERED<", page)
        self.assertNotIn("ANSWERED ·", page)
        card = page[page.index('<div class="card answered">'):page.index("</div>", page.index('<div class="card answered">'))]
        # markdown_inline HTML-escapes the apostrophe in the recorded words.
        self.assertIn("“let&#x27;s go ahead and fix the toml issues preventing page rendering please”", card)
        self.assertIn("answered 01:15", card)
        assert_settled(self, page)
        saved = {"key": "A", "words": "old", "at": "2026-09-26T01:00:00-05:00"}
        ctx = dp.Context(NOW, rows=(row("01:15", "option B of decision-x", '"B, from the tracker"'),))
        page = dp.render(dp.parse(decision(answer=saved)), "2026-09-26", ctx=ctx, slug="x")
        self.assertIn(">ANSWERED · B<", page)
        card = page[page.index('<div class="card answered">'):page.index("</div>", page.index('<div class="card answered">'))]
        self.assertIn('<span class="key">B</span>Follow the plan', card)
        self.assertIn("“B, from the tracker”", card)
        self.assertNotIn("old", card)
        self.assertNotIn(">ANSWERED · A<", page)
        # A free-form row, and a keyed row without words, take nothing from the superseded page save.
        ctx = dp.Context(NOW, rows=(row("01:15", "decision-x", '"something else"'),))
        page = dp.render(dp.parse(decision(answer=saved)), "2026-09-26", ctx=ctx, slug="x")
        card = page[page.index('<div class="card answered">'):page.index("</div>", page.index('<div class="card answered">'))]
        self.assertIn(">ANSWERED<", page)
        self.assertIn("“something else”", card)
        self.assertNotIn('class="key"', card)
        self.assertNotIn("old", card)
        ctx = dp.Context(NOW, rows=(row("01:15", "option B of decision-x", ""),))
        page = dp.render(dp.parse(decision(answer=saved)), "2026-09-26", ctx=ctx, slug="x")
        card = page[page.index('<div class="card answered">'):page.index("</div>", page.index('<div class="card answered">'))]
        self.assertIn('<span class="key">B</span>', card)
        self.assertNotIn("old", card)
        self.assertNotIn("class=\"words\"", card)

    def test_the_decisions_list_shows_a_string_answer_without_a_form(self):
        d = dp.parse(decision(answer="B"))
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "decision-x.json").write_text(json.dumps(d))
            page, _ = dp.index(Path(tmp), dp.Context(NOW), "2026-09-26")
        self.assertIn('<span class="key">B</span>Follow the plan', page)
        self.assertNotIn("<form", page)

    def test_is_answered_reads_the_page_and_the_trackers_and_fails_loudly(self):
        self.assertTrue(dp.is_answered(dp.parse(decision(answer="B"))))
        self.assertFalse(dp.is_answered(dp.parse(decision()), "x", None))
        self.assertIsNone(dp.tracker_answer(None, "x"))
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises((OSError, dp.ConfigError)):
                dp.tracker_answer(Path(tmp), "x")


def board_css() -> str:
    return (Path(__file__).resolve().parent.parent / "scripts" / "render_board.py").read_text().replace("{{", "{").replace("}}", "}")


def scheme(css: str) -> dict[str, dict[str, str]]:
    """The tokens of the light `:root` and of each dark block, which must agree."""
    def tokens(block: str) -> dict[str, str]:
        return dict(re.findall(r"(--[\w-]+):([^;}]+)", block))
    light = tokens(re.search(r"^:root\{(.*?)\}$", css, re.M)[1])
    auto = re.search(r'^@media \(prefers-color-scheme:dark\)\{:root:not\(\[data-theme="light"\]\)\{(.*?)\}', css, re.M)[1]
    forced = re.search(r'^:root\[data-theme="dark"\]\{(.*?)\}', css, re.M)[1]
    assert tokens(auto) == tokens(forced), "the two dark blocks differ"
    return {"light": light, "dark": tokens(auto)}


def contrast(a: str, b: str) -> float:
    """WCAG 2 contrast ratio of two #rrggbb colours."""
    def lum(h: str) -> float:
        c = [int(h.lstrip("#")[i:i + 2], 16) / 255 for i in (0, 2, 4)]
        r, g, b_ = (x / 12.92 if x <= 0.04045 else ((x + 0.055) / 1.055) ** 2.4 for x in c)
        return 0.2126 * r + 0.7152 * g + 0.0722 * b_
    hi, lo = sorted((lum(a), lum(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


STAGES = ("implement", "pr", "review", "triage", "fix", "verify", "merge")
SHARED = ("--bg", "--surface", "--fg", "--muted", "--line", "--brass", "--dl") + tuple(f"--stage-{s}" for s in STAGES)
# (text, ground) pairs as the stylesheets use them: every one is text that must read at WCAG AA.
BOARD_TEXT = [("--fg", "--bg"), ("--fg", "--surface"), ("--muted", "--bg"), ("--muted", "--surface"), ("--brass", "--surface"),
              ("--brass", "--bg"), ("--dl", "--bg"), ("--dl", "--surface")] + [(f"--stage-{s}", "--bg") for s in STAGES]
PAGE_TEXT = [("--link", "--ref-bg"), ("--link", "--surface"), ("--surface", "--link"), ("--surface", "--now"), ("--deployed", "--surface"), ("--inflight", "--inflight-soft"),
             ("--designed", "--designed-soft"), ("--ext-ink", "--ext-soft"), ("--dl", "--ext-soft")]


class DarkSchemeTest(unittest.TestCase):
    """The user, 2026-09-26: "we need new dark mode colors for the entire scheme"."""

    def test_the_board_dark_scheme_reads_at_aa(self):
        dark = scheme(board_css())["dark"]
        for text, ground in BOARD_TEXT:
            with self.subTest(text=text, ground=ground):
                self.assertGreaterEqual(contrast(dark[text], dark[ground]), 4.5)

    def test_the_decision_page_dark_scheme_reads_at_aa(self):
        dark = scheme(dp.HEAD)["dark"]
        for text, ground in PAGE_TEXT:
            with self.subTest(text=text, ground=ground):
                self.assertGreaterEqual(contrast(dark[text], dark[ground]), 4.5)

    def test_stages_keep_okabe_ito_in_light_and_merge_has_a_dark_stand_in(self):
        themes = scheme(board_css())
        for stage, colour in gantt.PALETTE.items():
            with self.subTest(stage=stage):
                self.assertEqual(themes["light"][f"--stage-{stage}"].lower(), (colour if isinstance(colour, str) else colour[0]).lower())
        self.assertGreater(contrast(themes["dark"]["--stage-merge"], themes["dark"]["--bg"]), 7)

    def test_the_board_sets_every_gantt_colour_for_dark(self):
        css = board_css()
        colours = set(re.findall(r"(--gantt-[\w-]+):#", gantt.BASE_CSS))
        for block in (re.search(r'^@media \(prefers-color-scheme:dark\)\{.*$', css, re.M)[0],
                      "\n".join(re.findall(r'^:root\[data-theme="dark"\].*$', css, re.M))):
            set_here = set(re.findall(r"(--gantt-[\w-]+):var\(--[\w-]+\)", block))
            self.assertEqual(colours - set_here, set())


class MainTest(unittest.TestCase):
    def workspace(self, board: str = BOARD) -> Path:
        root = Path(tempfile.mkdtemp())
        (root / "CLAUDE.md").write_text(BLOCK.format(board=board))
        (root / "d.json").write_text(json.dumps(decision()))
        return root

    def run_main(self, *argv: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = dp.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_writes_into_the_pages_dir_and_prints_its_local_url(self):
        root = self.workspace()
        code, out, _ = self.run_main("--root", str(root), "--from", str(root / "d.json"), "--name", "uploads-storage",
                                     "--date", "2026-09-25")
        self.assertEqual(code, 0)
        self.assertEqual(out.strip(), "http://127.0.0.1:8765/decisions/uploads-storage")
        self.assertIn("Follow the architecture", (root / "pages" / "decision-uploads-storage.html").read_text())

    def test_reads_the_decision_beside_its_page_by_default(self):
        root = self.workspace()
        (root / "pages").mkdir()
        (root / "pages" / "decision-uploads-storage.json").write_text(json.dumps(decision()))
        code, out, _ = self.run_main("--root", str(root), "--name", "uploads-storage")
        self.assertEqual((code, out.strip()), (0, "http://127.0.0.1:8765/decisions/uploads-storage"))

    def test_a_name_that_is_not_a_slug_is_refused(self):
        root = self.workspace()
        for name in ("../x", "A B", ""):
            with self.subTest(name=name):
                code, _, err = self.run_main("--root", str(root), "--from", str(root / "d.json"), "--name", name)
                self.assertEqual(code, 2)
                self.assertIn("name", err)
        self.assertFalse((root / "pages").exists())

    def test_a_name_of_digits_alone_is_refused_as_reserved_for_store_decision_ids(self):
        """#945: /decisions/<digits> is a store decision's page, so an authored slug is never digits alone."""
        root = self.workspace()
        code, _, err = self.run_main("--root", str(root), "--from", str(root / "d.json"), "--name", "42")
        self.assertEqual(code, 2)
        self.assertIn("digit-only names are reserved for store decision ids", err)
        self.assertNotIn("is not lowercase letters", err)
        self.assertFalse((root / "pages").exists())

    def test_no_board_line_means_no_page(self):
        root = self.workspace(board="")
        code, _, err = self.run_main("--root", str(root), "--from", str(root / "d.json"), "--name", "x")
        self.assertEqual(code, 1)
        self.assertIn("Board", err)

    def test_an_invalid_decision_writes_nothing(self):
        root = self.workspace()
        (root / "d.json").write_text(json.dumps(decision(recommended="Z")))
        code, _, err = self.run_main("--root", str(root), "--from", str(root / "d.json"), "--name", "x")
        self.assertEqual(code, 2)
        self.assertIn("recommended", err)
        self.assertFalse((root / "pages").exists())

    def test_the_cli_reads_asked_and_the_answer_from_the_trackers(self):
        root = self.workspace()
        (root / "daily").mkdir()
        (root / "daily" / "2026-09-25-tracker.md").write_text(
            "# Tracker\n\n## Decisions\n\n| time | item | Robin's words |\n|---|---|---|\n"
            "| 10:05 | option B of decision-x | \"B, go\" |\n\n## Log\n\n- 09:40 asked Robin about decision-x\n")
        code, _, _ = self.run_main("--root", str(root), "--from", str(root / "d.json"), "--name", "x", "--date", "2026-09-25")
        page = (root / "pages" / "decision-x.html").read_text()
        self.assertEqual(code, 0)
        self.assertRegex(page, r"asked (?:Fri )?09:40")
        self.assertIn(">ANSWERED · B<", page)
        self.assertIn('<span class="key">B</span>Follow the plan', page)
        self.assertIn("“B, go”", page)
        self.assertRegex(page, r'<span class="saved">answered (?:Fri )?10:05</span>')
        assert_settled(self, page)

    def test_links_against_the_workspace_backlog(self):
        root = self.workspace(board=BOARD + "\n- Backlog: GitHub; repo o/app")
        code, _, _ = self.run_main("--root", str(root), "--from", str(root / "d.json"), "--name", "x")
        self.assertEqual(code, 0)
        self.assertIn('href="https://github.com/o/app/issues/7"', (root / "pages" / "decision-x.html").read_text())


MMD = 'flowchart LR\n  n0["agent/a"]\n  n1["agent/b"]:::added\n  n1 --> n0\n'
SUMMARY = "nodes +1 −0 ~0 edges +1 −0 drift=0"
# The caption reads the summary as Decision.dc.html draws it: "nodes +1 −1 ~1 · edges +2 −1 · drift 1".
CAPTION = "nodes +1 −0 ~0 · edges +1 −0 · drift 0"
# A stand-in for the branch-graph CLI, found first on PATH: it logs its arguments, and writes page.html and page.mmd
# beside --out and prints its summary line as the real one does (it does not make --out's directory either). Given
# VIEWS (a list, maybe empty) and --max-nodes, it writes page-1.mmd … page-K.mmd instead of page.mmd, each opening `%% view: <title>` and
# closing with a comment naming the cap it was drawn with, and its summary ends ` excluded=E views=K`. Without VIEWS
# it is an older branch-graph that takes the flags but still writes only page.mmd.
FAKE = """import argparse, json, pathlib, sys
p = argparse.ArgumentParser()
for flag in ("--repo", "--base", "--head", "--root", "--depth", "--rules", "--out", "--max-nodes"):
    p.add_argument(flag)
p.add_argument("--exclude", action="append")
args = p.parse_args()
with open(LOG, "a") as f:
    f.write(json.dumps(vars(args)) + "\\n")
if FAIL:
    sys.exit("branch-graph: no import root agent at " + args.head)
out = pathlib.Path(args.out)
out.write_text("<!doctype html><p>graph</p>")
if VIEWS is not None and args.max_nodes:
    for i, (title, mmd) in enumerate(VIEWS, 1):
        (out.parent / f"page-{i}.mmd").write_text(f"%% view: {title}\\n{mmd}  %% drawn with max-nodes {args.max_nodes}\\n")
    print(f"{SUMMARY} excluded=9 views={len(VIEWS)}")
else:
    out.with_suffix(".mmd").write_text(MMD)
    print(SUMMARY)
print("agent/b -> agent/a  agent/b.py:1  allowed")
"""
# Eleven views, so a page that sorts page-10 before page-2 draws them out of order; one title needs escaping.
VIEW_TITLES = [f"pkg{i}.core" for i in range(1, 12)]
VIEW_TITLES[2] = "pkg3 & <core>"
VIEWS = [(t, f'flowchart LR\n  n0["pkg{i}/core"]\n  n1["pkg{i}/edge"]:::added\n  n1 --> n0\n')
         for i, t in enumerate(VIEW_TITLES, 1)]
GRAPH = '[graph]\nclone = "repos/app"\nroot = "agent"\nrules = "docs/architecture.md"\n'


def forge_repo(root: Path, ref: str) -> tuple[Path, str, str]:
    """origin.git with main and a change pushed to `ref`, main moved on since, and the workspace's clone at
    repos/app, which does not hold the change. Returns (clone, the change's merge base, its head)."""
    dev = make_repo.build(root / "forge", {"clone": "dev", "files": {
        "agent/a.py": "x = 1\n", "docs/architecture.md": "```import-rules\nagent/b -> agent/a\n```\n"}})
    base = make_repo.git(["rev-parse", "HEAD"], dev)
    make_repo.git(["checkout", "-q", "-b", "change"], dev)
    (dev / "agent" / "b.py").write_text("from agent import a\n")
    make_repo.git(["add", "-A"], dev)
    make_repo.git(["commit", "-q", "-m", "change"], dev)
    head = make_repo.git(["rev-parse", "HEAD"], dev)
    make_repo.git(["push", "-q", "origin", f"change:{ref}"], dev)
    make_repo.git(["checkout", "-q", "main"], dev)
    (dev / "agent" / "c.py").write_text("y = 2\n")
    make_repo.git(["add", "-A"], dev)
    make_repo.git(["commit", "-q", "-m", "main moves on"], dev)
    make_repo.git(["push", "-q", "origin", "main"], dev)
    clone = root / "repos" / "app"
    make_repo.git(["clone", "-q", "--no-local", str(root / "forge" / "origin.git"), str(clone)], root)
    return clone, base, head


def rev(clone: Path, name: str) -> str:
    return subprocess.run(["git", "-C", str(clone), "rev-parse", "--verify", "-q", f"{name}^{{commit}}"],
                          capture_output=True, text=True).stdout.strip()


class ModuleGraphTest(unittest.TestCase):
    """The user: the decision page "does NOT have the module graph that is required". The page server draws it with
    the branch-graph CLI from the change the decision names (Decision.dc.html, MODULE GRAPH · !58); the coordinator
    writes nothing."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, True)
        self.pages = self.root / "pages"
        self.pages.mkdir()
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.log = self.root / "branch-graph.log"
        self.fake()
        path = mock.patch.dict(os.environ, {"PATH": f"{self.bin}{os.pathsep}{os.environ.get('PATH', '')}"})
        path.start()
        self.addCleanup(path.stop)

    def fake(self, fail: bool = False, views: list | None = None, summary: str = SUMMARY):
        tool = self.bin / "branch-graph"
        tool.write_text(f"#!{sys.executable}\nLOG, FAIL, MMD, SUMMARY, VIEWS = "
                        f"{str(self.log)!r}, {fail!r}, {MMD!r}, {summary!r}, {views!r}\n" + FAKE)
        tool.chmod(0o755)

    def runs(self) -> list[dict]:
        return [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.is_file() else []

    def workspace(self, gitlab: bool = False, graph: str = GRAPH, references: list | None = None, pushed: bool = True):
        """The forge sources know change 58 (`!58` on GitLab, `#58` on GitHub) at its head; the decision names an
        unknown change first, then 58."""
        key, kind, ref = ("!58", "MR", "refs/merge-requests/58/head") if gitlab else ("#58", "PR", "refs/pull/58/head")
        self.clone, self.base, self.head = forge_repo(self.root, ref if pushed else ref.replace("58", "59"))
        assert not rev(self.clone, self.head), "the clone must not hold the change before the page fetches it"
        self.url = ("https://gl.example.test/grp/app/-/merge_requests/58" if gitlab else "https://github.com/o/app/pull/58")
        backlog = "GitLab issues; host https://gl.example.test; project grp/app" if gitlab else "GitHub; repo o/app"
        (self.root / "CLAUDE.md").write_text(BLOCK.format(board=f"{BOARD}\n- Settings: `cos.toml`\n- Backlog: {backlog}"))
        (self.root / "cos.toml").write_text(graph)
        change = bs.Change(key, self.url, "Warm pool", "open", False, "passed", False, 0, None, "main", ("agent/b.py",), (),
                           head=self.head)
        home = Backlog("https://gl.example.test", "grp/app") if gitlab else GitHubBacklog("o/app")
        bs._write(self.pages / bs.CACHE, bs.Sources({}, {}, {key: change}, (), {}, home))
        if references is None:
            references = [{"kind": kind, "value": key[0] + "57"}, {"kind": kind, "value": key, "label": "Warm pool"}]
        (self.pages / "decision-warm-pool.json").write_text(json.dumps(decision(references=references)))

    def page(self) -> str:
        return dp.write(self.root, self.pages, "warm-pool", "2026-09-26").read_text()

    def section(self, page: str) -> str:
        m = re.search(r"<section[^>]*>\s*<h2[^>]*>\s*MODULE GRAPH\b.*?</section>", page, re.S)
        self.assertIsNotNone(m, "the page has no MODULE GRAPH section")
        return m[0]

    def caption(self, section: str) -> tuple[int, str]:
        """Where the one <figcaption> starts, and its text with tags dropped and spaces collapsed."""
        [m] = re.finditer(r"<figcaption[^>]*>(.*?)</figcaption>", section, re.S)
        return m.start(), " ".join(unescape(re.sub(r"<[^>]+>", " ", m[1])).split())

    def assert_drawn(self, section: str, ref: str):
        self.assertRegex(section, re.compile(rf'<h2[^>]*>\s*MODULE GRAPH\b.*?<a [^>]*href="{re.escape(self.url)}"[^>]*>{ref}</a>.*?</h2>', re.S))
        self.assertIn('<pre class="mermaid">', section)
        for line in MMD.splitlines():
            with self.subTest(line=line):
                self.assertTrue(escape(line, quote=False) in section or escape(line) in section)
        self.assertNotIn("n1 --> n0", section)
        # The legend and summary follow the drawing (Decision.dc.html: one shared row after the views).
        at, text = self.caption(section)
        self.assertGreater(at, section.rindex("</pre>"))
        self.assertIn(CAPTION, text)

    def test_a_github_pr_is_drawn_from_its_fetched_head_against_its_merge_base(self):
        self.workspace()
        page = self.page()
        self.assert_drawn(self.section(page), "#58")
        self.assertRegex(page, r"<script[^>]*src=\"[^\"]*mermaid|<script[^>]*>[^<]*mermaid")
        [run] = self.runs()
        self.assertEqual(rev(self.clone, run["head"]), self.head)
        self.assertEqual(rev(self.clone, run["base"]), self.base)
        self.assertEqual(Path(run["repo"]).resolve(), self.clone.resolve())
        self.assertEqual((run["root"], run["depth"]), ("agent", "2"))
        self.assertEqual(Path(run["rules"]).resolve(), (self.clone / "docs" / "architecture.md").resolve())
        out = Path(run["out"]).resolve()
        self.assertEqual(out.name, "page.html")
        # The comparison identity hashes the head, base and settings under a dotdir the server never serves.
        cache, *key, _ = out.relative_to(self.pages.resolve()).parts
        self.assertTrue(cache.startswith("."), "the cache is a dotdir, which the server never serves")
        self.assertEqual(len(key), 1)
        self.assertRegex(key[0], r"^[0-9a-f]{64}$")
        [saved] = (self.pages / ".graphs" / ".comparisons").iterdir()
        self.assertEqual(json.loads(saved.read_text()), {"base": self.base, "key": key[0]})

    def test_a_gitlab_mr_is_drawn_from_its_merge_request_ref(self):
        self.workspace(gitlab=True)
        self.assert_drawn(self.section(self.page()), "!58")
        [run] = self.runs()
        self.assertEqual(rev(self.clone, run["head"]), self.head)
        self.assertEqual(rev(self.clone, run["base"]), self.base)

    def test_the_same_head_is_drawn_once(self):
        self.workspace()
        self.page()
        self.assert_drawn(self.section(self.page()), "#58")
        self.assertEqual(len(self.runs()), 1)

    def test_the_board_rerendering_every_page_keeps_the_graph(self):
        self.workspace()
        dp.write_all(self.pages, dp.decision_context(self.root, date(2026, 9, 26)), "2026-09-26", self.root)
        self.assert_drawn(self.section((self.pages / "decision-warm-pool.html").read_text()), "#58")

    def test_a_decision_naming_no_known_change_says_so_in_one_line(self):
        self.workspace()
        for references in ([], [{"kind": "PR", "value": "#57"}], [{"kind": "issue", "value": "#12"}]):
            with self.subTest(references=references):
                (self.pages / "decision-warm-pool.json").write_text(json.dumps(decision(references=references)))
                section = self.section(self.page())
                self.assertIn("names no change", section)
                self.assertNotIn('class="mermaid"', section)
        self.assertEqual(self.runs(), [])
        # The graph is required, so the section is there even with no workspace behind the page.
        self.assertIn("names no change", self.section(dp.render(dp.parse(decision()), "2026-09-26")))

    def test_without_graph_settings_the_line_names_the_table(self):
        self.workspace(graph='[workers]\nmode = "one-shot"\n')
        section = self.section(self.page())
        self.assertIn("not configured", section)
        self.assertIn("[graph]", section)
        self.assertNotIn('class="mermaid"', section)
        self.assertEqual(self.runs(), [])

    def test_a_failed_fetch_says_unavailable_and_the_page_still_renders(self):
        self.workspace(pushed=False)
        page = self.page()
        section = self.section(page)
        self.assertIn("unavailable", section)
        self.assertNotIn('class="mermaid"', section)
        self.assertEqual(self.runs(), [])
        for text in ("The plan and the architecture disagree on where uploads go", "Follow the architecture", "The worker waits."):
            self.assertIn(text, page)

    def test_a_failed_tool_says_unavailable_with_its_reason(self):
        self.fake(fail=True)
        self.workspace()
        page = self.page()
        section = self.section(page)
        self.assertIn("unavailable", section)
        self.assertIn("no import root agent", section)
        self.assertNotIn('class="mermaid"', section)
        self.assertIn("Follow the architecture", page)

    # The user: a real MR drew dozens of tests.* modules and was unreadable. Test modules are hidden, and a big graph
    # is split so no view holds more than max_nodes (Decision.dc.html, MODULE GRAPH: "2 views · at most 12 modules
    # each", one figure per view, one legend and summary after them).

    def views(self, section: str) -> list[str]:
        """Each view's figure, from its <figure to the next one, in page order."""
        return [c for c in section.split("<figure")[1:] if '<pre class="mermaid">' in c]

    def test_the_tool_hides_test_modules_and_caps_views_by_default(self):
        self.workspace()
        self.page()
        [run] = self.runs()
        self.assertEqual(run["exclude"], ["tests.*", "evals.*"])
        self.assertEqual(run["max_nodes"], "12")

    def test_the_tool_gets_one_exclude_per_glob_and_the_cap_the_settings_name(self):
        self.workspace(graph=GRAPH + 'exclude = ["legacy.*", "vendor.*"]\nmax_nodes = 5\n')
        self.page()
        [run] = self.runs()
        self.assertEqual(run["exclude"], ["legacy.*", "vendor.*"])
        self.assertEqual(run["max_nodes"], "5")

    def test_an_empty_exclude_passes_no_exclude(self):
        self.workspace(graph=GRAPH + "exclude = []\n")
        self.page()
        [run] = self.runs()
        self.assertIsNone(run["exclude"])
        self.assertEqual(run["max_nodes"], "12")

    def test_every_view_is_its_own_titled_figure_in_numeric_order(self):
        self.fake(views=VIEWS)
        self.workspace()
        section = self.section(self.page())
        figures = self.views(section)
        self.assertEqual(len(figures), len(VIEWS))
        for n, (figure, (title, mmd)) in enumerate(zip(figures, VIEWS), 1):
            with self.subTest(view=n):
                [pre] = re.findall(r'<pre class="mermaid">(.*?)</pre>', figure, re.S)
                for line in mmd.splitlines():
                    self.assertTrue(escape(line, quote=False) in pre or escape(line) in pre, line)
                self.assertNotIn("n1 --> n0", pre)
                rest = figure.replace(pre, "")
                self.assertIn(escape(title, quote=False), rest)
                self.assertNotIn("%% view", rest)
        self.assertNotIn("<core>", section)

    def test_the_heading_counts_the_views_and_names_the_cap(self):
        self.fake(views=VIEWS)
        self.workspace()
        section = self.section(self.page())
        self.assertRegex(section, rf"\b{len(VIEWS)} views\b")
        self.assertIn("at most 12 modules each", section)
        (self.root / "cos.toml").write_text(GRAPH + "max_nodes = 5\n")
        self.assertIn("at most 5 modules each", self.section(self.page()))

    def test_one_legend_and_summary_follow_the_views(self):
        self.fake(views=VIEWS)
        self.workspace()
        section = self.section(self.page())
        self.assertEqual(len(self.views(section)), len(VIEWS))
        last = section.rindex("</pre>")
        for text in (">NEW<", ">DRIFT<"):
            with self.subTest(text=text):
                self.assertEqual(section.count(text), 1)
                self.assertGreater(section.index(text), last)
        at, text = self.caption(section)
        self.assertGreater(at, last)
        self.assertIn(CAPTION, text)

    def test_a_drawing_made_with_other_settings_is_not_served(self):
        self.fake(views=VIEWS)
        self.workspace()
        self.page()
        self.assertIn("drawn with max-nodes 12", self.section(self.page()))
        self.assertEqual(len(self.runs()), 1)
        (self.root / "cos.toml").write_text(GRAPH + "max_nodes = 5\n")
        section = self.section(self.page())
        self.assertIn("drawn with max-nodes 5", section)
        self.assertNotIn("drawn with max-nodes 12", section)
        self.assertEqual(len(self.runs()), 2)
        self.page()
        self.assertEqual(len(self.runs()), 2, "the same settings reuse the drawing")
        (self.root / "cos.toml").write_text(GRAPH + 'max_nodes = 5\nexclude = ["tests.*"]\n')
        self.page()
        self.assertEqual([r["exclude"] for r in self.runs()], [["tests.*", "evals.*"]] * 2 + [["tests.*"]])

    def test_an_older_tool_that_writes_only_page_mmd_still_draws_it(self):
        self.workspace()
        for drawn in ("drawn now", "served from the cache"):
            with self.subTest(drawn):
                section = self.section(self.page())
                self.assert_drawn(section, "#58")
                self.assertEqual(section.count('<pre class="mermaid">'), 1)
                self.assertNotIn("unavailable", section)
        [run] = self.runs()
        self.assertEqual(run["max_nodes"], "12")
        out = Path(run["out"]).parent
        self.assertEqual(sorted(f.name for f in out.glob("*.mmd")), ["page.mmd"])
        self.assertNotIn("views=", (out / "summary.txt").read_text())

    # Issue #178: when every changed module is excluded, branch-graph draws no view (`views=0`) and the section said
    # "The module graph is unavailable: [Errno 2] … page.mmd"; its caption printed the tool's raw summary line.

    def assert_nothing_outside_the_excluded_set(self, page: str, section: str):
        self.assertIn("outside the excluded set", section)
        self.assertNotIn('<pre class="mermaid">', section)
        for text in ("unavailable", "Errno", "page.mmd"):
            with self.subTest(text=text):
                self.assertNotIn(text, section)

    def test_a_change_touching_only_excluded_modules_says_so_without_an_error(self):
        self.fake(views=[])
        self.workspace()
        for drawn in ("drawn now", "served from the cache"):
            with self.subTest(drawn):
                page = self.page()
                self.assert_nothing_outside_the_excluded_set(page, self.section(page))
        self.assertEqual(len(self.runs()), 1)
        out = Path(self.runs()[0]["out"]).parent
        self.assertEqual(list(out.glob("*.mmd")), [])
        self.assertTrue((out / "summary.txt").read_text().endswith("views=0"))

    def test_the_task_page_says_so_too(self):
        import issue_page as ip
        self.fake(views=[])
        self.workspace()
        self.page()  # draws the change once; the task page is served from the same cache
        cache = self.pages / bs.CACHE
        sources = bs.load(self.pages)
        change = replace(sources.changes["#58"], issues=("#109",))
        bs._write(cache, replace(sources, changes={"#58": change}))
        (self.root / "daily").mkdir()
        today = datetime.now(ZoneInfo("America/Chicago")).date()  # the workspace's day, not the host's
        (self.root / "daily" / f"{today}-tracker.md").write_text(
            "# Tracker\n\n## Tasks\n\n| name | item | owner | state | since | due | size | lane | stage | issue | checklist |\n"
            + "|---" * 11 + f"|\n| Warm pool | Warm the pool | Robin | waiting | {today} |  | S |  |  | #109 | c |\n"
            "\n## Log\n")
        out = ip.write(self.root, self.pages, 109)
        self.assertIsNotNone(out, "no task page for #109")
        page = out.read_text()
        m = re.search(r"<section[^>]*>\s*<h2[^>]*>\s*Architecture\b.*?</section>", page, re.S)
        self.assertIsNotNone(m, "the task page has no Architecture section")
        self.assert_nothing_outside_the_excluded_set(page, m[0])
        self.assertEqual(len(self.runs()), 1)

    def test_the_caption_reads_the_summary_as_designed(self):
        self.fake(views=VIEWS[:2], summary="nodes +1 −1 ~1 edges +2 −1 drift=1")  # the fake adds excluded=9 views=2
        self.workspace()
        section = self.section(self.page())
        self.assertEqual(len(self.views(section)), 2)
        _, text = self.caption(section)
        self.assertIn("nodes +1 −1 ~1 · edges +2 −1 · drift 1", text)
        for raw in ("drift=", "excluded=", "views="):
            with self.subTest(raw=raw):
                self.assertNotIn(raw, text)


if __name__ == "__main__":
    unittest.main()
