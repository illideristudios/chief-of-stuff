"""Issue #189: every page opens with the section tab bar (Flow, Decisions, Task, Decision and Source48 .dc.html open
with `<nav aria-label="Board sections">`).

The user's decision: show all seven tabs; a tab whose page does not exist yet is shown but is not a link.

Markup contract these tests read (keep to it; everything else is free):

- Each page holds exactly one `<nav aria-label="Board sections">`. Its direct child elements are the tabs, in order.
- A tab's name is its text, less the text of any `<span title="…">` inside it; that span is the tab's count badge.
- A built tab holds one `<a href>`: Board `href="/"`, Workers `href="/workers"` (#195), Decisions `href="/decisions#pending-decisions"`. An unbuilt tab holds no `<a>`
  and no `href` at all; any other element will do (a `<span>`, say).
- The page's own tab carries `aria-current="page"`. A detail page's parent tab carries the class `tab-parent`
  instead (Task and Source under Board, Decision under Decisions), and nothing in the nav is `aria-current`.
- At phone width the nav wraps or scrolls inside itself: its inline style, or a `<style>` rule whose selector names
  `nav` or one of the nav's classes or its id, sets `flex-wrap`/`flex-flow` to wrap or `overflow`/`overflow-x` to
  auto or scroll.
"""

import json
import re
import shutil
import sys
import tempfile
import threading
import unittest
from unittest import mock
from datetime import datetime, timedelta
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path[:0] = [str(Path(__file__).resolve().parent.parent / "scripts"), str(Path(__file__).resolve().parent)]
import pages as pg  # noqa: E402
from test_issue_page import ROUTE_TRACKER, ZONE  # noqa: E402
from test_pages import decision, get  # noqa: E402

TABS = ("Board", "Workers", "Dispatch", "Since you last looked", "Epics", "Decisions", "Limits")
BUILT = {"Board": "/", "Workers": "/workers", "Decisions": "/decisions#pending-decisions"}
# The five page kinds, by route; the decision and the task (#109) are the fixture's.
ROUTES = {"board": "/", "decisions index": "/decisions", "decision": "/decisions/alpha", "task": "/issues/109",
          "source": "/issues/109/source"}
OWN = {"board": "Board", "decisions index": "Decisions"}
PARENT = {"task": "Board", "source": "Board", "decision": "Decisions"}
PARENT_HOOK = "tab-parent"
VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}


class Node:
    def __init__(self, tag: str, attrs: dict):
        self.tag, self.attrs, self.children, self.text = tag, attrs, [], []

    def walk(self):
        yield self
        for child in self.children:
            yield from child.walk()

    def all_text(self) -> str:
        return "".join(self.text) + "".join(c.all_text() for c in self.children)


class Navs(HTMLParser):
    """Every `<nav aria-label="Board sections">` as a tree, and the page's `<style>` text."""

    def __init__(self):
        super().__init__()
        self.navs: list[Node] = []
        self.stack: list[Node] = []
        self.styles: list[str] = []
        self.in_style = False

    def handle_starttag(self, tag, attrs):
        attrs = {k: v or "" for k, v in attrs}
        if tag == "style":
            self.in_style = True
            self.styles.append("")
        if self.stack:
            node = Node(tag, attrs)
            self.stack[-1].children.append(node)
            if tag not in VOID:
                self.stack.append(node)
        elif tag == "nav" and attrs.get("aria-label") == "Board sections":
            node = Node(tag, attrs)
            self.navs.append(node)
            self.stack.append(node)

    def handle_endtag(self, tag):
        if tag == "style":
            self.in_style = False
        # Close up to the matching open element; a stray end tag closes nothing.
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i].tag == tag:
                del self.stack[i:]
                break

    def handle_data(self, data):
        if self.in_style:
            self.styles[-1] += data
        if self.stack:
            self.stack[-1].text.append(data)


def parse(html: str) -> Navs:
    p = Navs()
    p.feed(html)
    return p


def badges(tab: Node) -> list[Node]:
    return [n for n in tab.walk() if n is not tab and n.tag == "span" and "title" in n.attrs]


def name(tab: Node) -> str:
    text = tab.all_text()
    for b in badges(tab):
        text = text.replace(b.all_text(), "", 1)
    return " ".join(unescape(text).split())


def tabs(html: str) -> dict[str, Node]:
    """The first Board sections nav's tabs by name; empty when the page has no such nav."""
    navs = parse(html).navs
    return {name(t): t for t in navs[0].children} if navs else {}


def served(pending: int, answered: int, lead_counts: bool = False) -> dict[str, str]:
    """The five pages as the server renders them for a workspace with `pending` open decisions and `answered` ones
    a Decisions row has answered. The decision page shown is `alpha`, the first."""
    root = Path(tempfile.mkdtemp())
    try:
        day = datetime.now(ZoneInfo(ZONE)).date().isoformat()
        (root / "CLAUDE.md").write_text(
            "## Coordinator\n- User: Robin\n- Daily log dir: `daily/`\n- Tracker: `daily/<date>-tracker.md`\n"
            f"- Timezone: {ZONE}\n- Board: self-hosted; URL http://127.0.0.1:8765/; dir `pages/`\n")
        slugs = ["alpha", "beta", "gamma", "delta"][:pending + answered]
        rows = "".join(f"| 00:05 | decision-{s} | A |\n" for s in slugs[pending:])
        (root / "daily").mkdir()
        (root / "daily" / f"{day}-tracker.md").write_text(
            ROUTE_TRACKER.format(day=day) + f"\n## Decisions\n\n| time | item | Robin's words |\n|---|---|---|\n{rows}")
        pages = root / "pages"
        pages.mkdir()
        for s in slugs:
            (pages / f"decision-{s}.json").write_text(decision(s))
        if lead_counts:
            import store
            path = root / "store.db"
            store.migrate(path)
            db = store.Store(path)
            try:
                db.upsert_item("github:example/repo#109", title="Uploads", stage="implement")
                db.upsert_item("github:example/repo#110", title="Await choice", human_review=True)
                db.add_decision("github:example/repo#110", "scope", "First choice")
                db.add_decision("github:example/repo#110", "design", "Second choice")
            finally:
                db.engine.dispose()
        server = pg.make_server(pages, 0, root=root, refresh=lambda root, now: None, every=3600)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            out = {}
            for kind, route in {**ROUTES, **({"workers": "/workers"} if lead_counts else {})}.items():
                resp = get(server.server_address[1], route)
                assert resp.status == 200, f"{route} answered {resp.status}"
                out[kind] = resp.body.decode()
            return out
        finally:
            server.shutdown()
            server.server_close()
    finally:
        shutil.rmtree(root, True)


class TwoPending(unittest.TestCase):
    PENDING, ANSWERED = 2, 0

    @classmethod
    def setUpClass(cls):
        cls.pages = served(cls.PENDING, cls.ANSWERED)


class TabBarTest(TwoPending):
    def test_every_page_has_one_bar_with_the_seven_tabs_in_order(self):
        # Rule 1: "Each of the five page kinds renders exactly one `<nav aria-label="Board sections">`. It holds
        # exactly the seven tab names in the design order."
        for kind, html in self.pages.items():
            with self.subTest(page=kind):
                navs = parse(html).navs
                self.assertEqual(len(navs), 1, f"{ROUTES[kind]} has {len(navs)} Board sections navs, not one")
                self.assertEqual(tuple(name(t) for t in navs[0].children), TABS)

    def test_built_tabs_link_and_unbuilt_tabs_do_not(self):
        # Rule 2: "Board links to `/`. Decisions links to `/decisions`. The other five are not links: there is no
        # `href` inside their tab element." #195: Workers links to `/workers` on every page. The user: "don't use `aria-disabled` on an `<a>` without an href".
        for kind, html in self.pages.items():
            with self.subTest(page=kind):
                got = tabs(html)
                for tab in TABS:
                    t = got.get(tab)
                    self.assertIsNotNone(t, f"{ROUTES[kind]} has no {tab} tab")
                    hrefs = [n.attrs["href"] for n in t.walk() if "href" in n.attrs]
                    anchors = [n for n in t.walk() if n.tag == "a"]
                    if tab in BUILT:
                        self.assertEqual(hrefs, [BUILT[tab]], f"{ROUTES[kind]}: the {tab} tab does not link to {BUILT[tab]}")
                    else:
                        self.assertEqual((hrefs, len(anchors)), ([], 0),
                                         f"{ROUTES[kind]}: the {tab} tab has no page yet, but is a link or an <a>")

    def test_a_section_page_marks_its_own_tab_and_only_it(self):
        # Rule 3: "Board has `aria-current="page"` on the Board tab. The decisions index has it on the Decisions tab.
        # No other tab on those pages has it."
        for kind, own in OWN.items():
            with self.subTest(page=kind):
                got = tabs(self.pages[kind])
                current = [tab for tab, t in got.items() if any(n.attrs.get("aria-current") == "page" for n in t.walk())]
                self.assertEqual(current, [own], f"{ROUTES[kind]}: aria-current=\"page\" is on {current}, not only {own}")

    def test_a_detail_page_marks_its_parent_tab_without_aria_current(self):
        # Rule 4: "Task and source pages mark Board as parent. Decision pages mark Decisions as parent. […] These pages
        # have no `aria-current="page"` in the nav." The hook is the class `tab-parent` on the parent's tab element.
        for kind, parent in PARENT.items():
            with self.subTest(page=kind):
                navs = parse(self.pages[kind]).navs
                self.assertTrue(navs, f"{ROUTES[kind]} has no Board sections nav")
                marked = [name(t) for t in navs[0].children if PARENT_HOOK in t.attrs.get("class", "").split()]
                self.assertEqual(marked, [parent], f"{ROUTES[kind]}: class {PARENT_HOOK} is on {marked}, not only {parent}")
                current = [n.tag for n in navs[0].walk() if n.attrs.get("aria-current") == "page"]
                self.assertEqual(current, [], f"{ROUTES[kind]}: a detail page's nav has aria-current=\"page\"")

    def test_the_decisions_tab_counts_the_pending_decisions(self):
        # Rule 5: "With 2 pending decisions in the fixture, the Decisions tab carries a badge reading `2`, with a title
        # naming "pending". […] No other tab carries a badge, because nothing supplies their counts yet."
        for kind, html in self.pages.items():
            with self.subTest(page=kind):
                got = tabs(html)
                t = got.get("Decisions")
                self.assertIsNotNone(t, f"{ROUTES[kind]} has no Decisions tab")
                got_badges = [(" ".join(b.all_text().split()), b.attrs["title"]) for b in badges(t)]
                self.assertEqual(len(got_badges), 1, f"{ROUTES[kind]}: the Decisions tab's badges are {got_badges}")
                [(count, title)] = got_badges
                self.assertEqual(count, str(self.PENDING))
                self.assertIn("pending", title.lower())
                others = {tab: [b.attrs["title"] for b in badges(n)] for tab, n in got.items() if tab != "Decisions"}
                self.assertEqual({tab: b for tab, b in others.items() if b}, {}, "a tab nothing counts yet carries a badge")

    def test_the_bar_wraps_or_scrolls_at_phone_width(self):
        # Rule 6: "the nav's CSS rule includes either `flex-wrap: wrap` or `overflow-x: auto`". Loose on purpose.
        allows = re.compile(r"flex-(?:wrap|flow)\s*:[^;}]*\bwrap\b|overflow(?:-x)?\s*:\s*(?:auto|scroll)\b", re.I)
        for kind, html in self.pages.items():
            with self.subTest(page=kind):
                p = parse(html)
                self.assertTrue(p.navs, f"{ROUTES[kind]} has no Board sections nav")
                nav = p.navs[0].attrs
                names = [r"\bnav\b", *(rf"\.{re.escape(c)}\b" for c in nav.get("class", "").split()),
                         *([rf"#{re.escape(nav['id'])}\b"] if nav.get("id") else [])]
                rules = [body for css in p.styles for sel, body in re.findall(r"([^{}]+)\{([^{}]*)\}", css)
                         if any(re.search(n, sel) for n in names)]
                self.assertTrue(any(allows.search(b) for b in [nav.get("style", ""), *rules]),
                                f"{ROUTES[kind]}: no CSS lets the nav wrap or scroll inside itself")


class NonePendingTest(TwoPending):
    PENDING, ANSWERED = 0, 1

    def test_with_nothing_pending_the_decisions_tab_has_no_badge(self):
        # Rule 5: "With 0 pending, it carries no badge."
        for kind, html in self.pages.items():
            with self.subTest(page=kind):
                got = tabs(html)
                t = got.get("Decisions")
                self.assertIsNotNone(t, f"{ROUTES[kind]} has no Decisions tab")
                self.assertEqual([b.attrs["title"] for b in badges(t)], [], f"{ROUTES[kind]}: a badge with nothing pending")
                self.assertEqual(name(t), "Decisions")



class EpicsBadgeTest(unittest.TestCase):
    """#230: the Epics tab reads `N at risk · M late`, zeros left out; with neither it has no badge."""

    def test_the_badge_leaves_out_its_zeros(self):
        import fragment
        for epics, text in (((2, 1), "2 at risk · 1 late"), ((2, 0), "2 at risk"), ((0, 1), "1 late")):
            with self.subTest(epics=epics):
                t = tabs(fragment.tab_bar("Board", 0, epics=epics))["Epics"]
                self.assertEqual([(b.all_text(), b.attrs["title"]) for b in badges(t)], [(text, f"milestones: {text}")])
                self.assertEqual(name(t), "Epics")
        self.assertEqual(badges(tabs(fragment.tab_bar("Board", 0))["Epics"]), [])


class LeadCountsTest(unittest.TestCase):
    """#224: every page keeps the other pages' lead counts visible in its navigation."""

    @classmethod
    def setUpClass(cls):
        cls.pages = served(2, 0, lead_counts=True)

    def test_counts_are_present_on_every_page_including_unbuilt_tabs(self):
        # #965: Decisions list and each decision page carry the same lead counts as the board.
        for page in ("board", "task", "source", "workers", "decisions index", "decision"):
            html = self.pages[page]
            with self.subTest(page=page):
                got = tabs(html)
                expected = {"Workers": ("1", "need"), "Dispatch": ("1", "ready"),
                            "Decisions": ("4", "pending")}
                for label, (count, why) in expected.items():
                    found = badges(got[label])
                    self.assertEqual([b.all_text() for b in found], [count], f"{page}: {label}")
                    self.assertIn(why, found[0].attrs["title"])
                for label in ("Board", "Limits", "Epics", "Since you last looked"):
                    self.assertEqual(badges(got[label]), [])
                self.assertNotIn("href", got["Dispatch"].attrs)
                self.assertNotIn("href", got["Epics"].attrs)

    def test_unbuilt_tabs_read_as_inactive(self):
        import fragment
        unbuilt = ("Dispatch", "Since you last looked", "Epics", "Limits")
        got = tabs(fragment.tab_bar("Board", 2, workers=1, dispatch=3, since=(4, "09:00"), epics=(2, 1)))
        for label in unbuilt:
            t = got[label]
            self.assertEqual(t.tag, "span", label)
            self.assertEqual(t.attrs.get("title"), "not available yet", label)
            self.assertNotIn("data-tone", t.attrs, label)
            self.assertNotIn("href", t.attrs, label)
            self.assertEqual([n for n in t.walk() if "href" in n.attrs or n.tag == "a"], [], label)
        self.assertEqual([b.all_text() for b in badges(got["Dispatch"])], ["3"])
        self.assertIn("ready", badges(got["Dispatch"])[0].attrs["title"])
        self.assertEqual([b.all_text() for b in badges(got["Since you last looked"])], ["4"])
        self.assertEqual([(b.all_text(), b.attrs["title"]) for b in badges(got["Epics"])],
                         [("2 at risk · 1 late", "milestones: 2 at risk · 1 late")])
        self.assertEqual(badges(got["Limits"]), [])
        for label in BUILT:
            self.assertNotEqual(got[label].attrs.get("title"), "not available yet", label)
        self.assertEqual(got["Workers"].attrs.get("data-tone"), "hot")
        self.assertEqual(got["Decisions"].attrs.get("data-tone"), "pending")
        self.assertIn("nav.tabs>span[title]{opacity:.55;cursor:default}", fragment.TAB_CSS)
        self.assertIn("nav.tabs>span[title] .badge{color:var(--muted)}", fragment.TAB_CSS)
        for page, html in self.pages.items():
            with self.subTest(page=page):
                page_tabs = tabs(html)
                for label in unbuilt:
                    t = page_tabs[label]
                    self.assertEqual(t.tag, "span", f"{page}: {label}")
                    self.assertEqual(t.attrs.get("title"), "not available yet", f"{page}: {label}")
                    self.assertNotIn("data-tone", t.attrs, f"{page}: {label}")
                    self.assertNotIn("href", t.attrs, f"{page}: {label}")
                    self.assertEqual([n for n in t.walk() if "href" in n.attrs or n.tag == "a"], [], f"{page}: {label}")
                for label in BUILT:
                    self.assertNotEqual(page_tabs[label].attrs.get("title"), "not available yet", f"{page}: {label}")
        for page in ("board", "task", "source", "workers", "decisions index", "decision"):
            with self.subTest(lead_page=page):
                dispatch_tab = tabs(self.pages[page])["Dispatch"]
                self.assertEqual([b.all_text() for b in badges(dispatch_tab)], ["1"])
                self.assertNotIn("data-tone", dispatch_tab.attrs)

    def test_store_decision_page_matches_board_lead_counts(self):
        # #965: /decisions/<store-id> keeps Workers/Dispatch/Decisions badges; store nav omits since/epics.
        import store
        root = Path(tempfile.mkdtemp())
        try:
            day = datetime.now(ZoneInfo(ZONE)).date().isoformat()
            (root / "CLAUDE.md").write_text(
                "## Coordinator\n- User: Robin\n- Daily log dir: `daily/`\n- Tracker: `daily/<date>-tracker.md`\n"
                f"- Timezone: {ZONE}\n- Board: self-hosted; URL http://127.0.0.1:8765/; dir `pages/`\n")
            (root / "daily").mkdir()
            (root / "daily" / f"{day}-tracker.md").write_text(ROUTE_TRACKER.format(day=day))
            pages = root / "pages"
            pages.mkdir()
            path = root / "store.db"
            store.migrate(path)
            db = store.Store(path)
            try:
                db.upsert_item("github:example/repo#109", title="Uploads", stage="implement")
                db.upsert_item("github:example/repo#110", title="Await choice", human_review=True)
                db.add_decision("github:example/repo#110", "scope", "First choice")
                db.add_decision("github:example/repo#110", "design", "Second choice")
                row_id = next(r.id for r in db.decisions())
            finally:
                db.engine.dispose()
            server = pg.make_server(pages, 0, root=root, refresh=lambda root, now: None, every=3600)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            try:
                port = server.server_address[1]
                board, decision = (get(port, r).body.decode() for r in ("/", f"/decisions/{row_id}"))
                for page, html in (("board", board), ("decision", decision)):
                    with self.subTest(page=page):
                        got = tabs(html)
                        self.assertEqual([b.all_text() for b in badges(got["Workers"])], ["1"])
                        self.assertEqual([b.all_text() for b in badges(got["Dispatch"])], ["1"])
                        self.assertEqual([b.all_text() for b in badges(got["Decisions"])], ["2"])
                        self.assertEqual(badges(got["Since you last looked"]), [])
                        self.assertEqual(badges(got["Epics"]), [])
            finally:
                server.shutdown()
                server.server_close()
        finally:
            shutil.rmtree(root, True)

    def test_mixed_decision_sources_match_on_every_page(self):
        import store
        root = Path(tempfile.mkdtemp())
        try:
            day = datetime.now(ZoneInfo(ZONE)).date().isoformat()
            (root / "CLAUDE.md").write_text(
                "## Coordinator\n- User: Robin\n- Daily log dir: `daily/`\n- Tracker: `daily/<date>-tracker.md`\n"
                f"- Timezone: {ZONE}\n- Board: self-hosted; URL http://127.0.0.1:8765/; dir `pages/`\n")
            (root / "daily").mkdir()
            (root / "daily" / f"{day}-tracker.md").write_text(
                ROUTE_TRACKER.format(day=day) + "\n## Decisions\n\n| time | item | Robin's words |\n|---|---|---|\n| 00:05 | decision-done | A |\n")
            pages = root / "pages"
            pages.mkdir()
            for slug in ("alpha", "done"):
                (pages / f"decision-{slug}.json").write_text(decision(slug))
            (pages / "decision-broken.json").write_text("{")
            store.migrate(root / "store.db")
            db = store.Store(root / "store.db")
            try:
                db.upsert_item("github:example/repo#109", title="Uploads", stage="implement")
                db.upsert_item("github:example/repo#110", title="Choice", forge_state="open")
                db.upsert_item("github:example/repo#111", title="Closed", forge_state="closed")
                row = db.add_decision("github:example/repo#110", "scope", "Open choice")
                db.add_decision("github:example/repo#110", "design", "Answered choice", answer="yes", decided_by="user")
                db.add_decision("github:example/repo#111", "scope", "Closed issue choice")
            finally:
                db.engine.dispose()
            server = pg.make_server(pages, 0, root=root, refresh=lambda root, now: None, every=3600)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            try:
                for route in ("/", "/decisions", "/decisions/alpha", f"/decisions/{row.id}", "/workers", "/issues/109", "/issues/109/source"):
                    with self.subTest(route=route):
                        response = get(server.server_address[1], route)
                        self.assertEqual(response.status, 200)
                        self.assertEqual([b.all_text() for b in badges(tabs(response.body.decode())["Decisions"])], ["2"])
            finally:
                server.shutdown()
                server.server_close()
        finally:
            shutil.rmtree(root, True)

    def test_coordinator_decisions_pages_match_board_dispatch_count(self):
        # #965: without store.db the Decisions list and JSON decision page still share the board's Dispatch badge.
        root = Path(tempfile.mkdtemp())
        try:
            day = datetime.now(ZoneInfo(ZONE)).date().isoformat()
            (root / "CLAUDE.md").write_text(
                "## Coordinator\n- User: Robin\n- Daily log dir: `daily/`\n- Tracker: `daily/<date>-tracker.md`\n"
                f"- Timezone: {ZONE}\n- Board: self-hosted; URL http://127.0.0.1:8765/; dir `pages/`\n"
                "- Settings: `cos.toml`\n")
            (root / "cos.toml").write_text('[lanes.build]\nstages = ["implement", "review", "merge"]\n')
            (root / "daily").mkdir()
            (root / "daily" / f"{day}-tracker.md").write_text(
                f"# Tracker\n\n## Tasks\n\n"
                f"| name | item | owner | state | since | due | size | lane | stage | issue | checklist |\n"
                f"|---|---|---|---|---|---|---|---|---|---|---|\n"
                f"| Ready task | Ship it | unassigned | open | {day} |  | S | build | implement | #111 | c |\n\n"
                f"## Log\n\n- 00:01 opened the day\n- 00:02 stage: Ready task → implement\n")
            pages = root / "pages"
            pages.mkdir()
            (pages / "decision-alpha.json").write_text(decision("alpha"))
            server = pg.make_server(pages, 0, root=root, refresh=lambda root, now: None, every=3600)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            try:
                port = server.server_address[1]
                for route in ("/", "/decisions", "/decisions/alpha"):
                    with self.subTest(route=route):
                        got = tabs(get(port, route).body.decode())
                        self.assertEqual([b.all_text() for b in badges(got["Dispatch"])], ["1"])
                        self.assertIn("ready", badges(got["Dispatch"])[0].attrs["title"])
            finally:
                server.shutdown()
                server.server_close()
        finally:
            shutil.rmtree(root, True)

    def test_direct_routes_use_latest_tracker_when_today_is_missing(self):
        from test_pages import ARCHITECTURAL_HOLD, ISSUE_TRACKER, LastLookedTest
        import decision_page
        import render_board
        root = Path(tempfile.mkdtemp())
        try:
            now = datetime.now(ZoneInfo(ZONE))
            day = (now.date() - timedelta(days=1)).isoformat()
            (root / "CLAUDE.md").write_text(
                "## Coordinator\n- User: Robin\n- Daily log dir: `daily/`\n- Tracker: `daily/<date>-tracker.md`\n"
                f"- Timezone: {ZONE}\n- Board: self-hosted; URL http://127.0.0.1:8765/; dir `pages/`\n"
                "- Settings: `cos.toml`\n")
            (root / "cos.toml").write_text(LastLookedTest.SETTINGS + "\n[workers]\nmax_concurrency = 2\n")
            (root / "daily").mkdir()
            (root / "daily" / f"{day}-tracker.md").write_text(
                ISSUE_TRACKER.format(day=day, task="Security audit").replace(
                    "\n## Log", f"\n| Ready | Ship | unassigned | open | {day} |  | S | build | implement | #8 | c |\n\n## Log"))
            pages = root / "pages"
            pages.mkdir()
            (pages / "decision-alpha.json").write_text(decision("alpha"))
            (pages / ".looked-before").write_text(datetime.fromisoformat(f"{day}T08:00").replace(tzinfo=ZoneInfo(ZONE)).isoformat())
            (pages / "decision-architecture.json").write_text(ARCHITECTURAL_HOLD)
            render_board.write(root, day)
            (pages / "decision-alpha.html").unlink()  # Exercise a direct render before the board regenerates it.
            server = pg.make_server(pages, 0, root=root, refresh=lambda root, now: None, every=3600)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            try:
                port = server.server_address[1]
                with mock.patch.object(decision_page, "write", wraps=decision_page.write) as write:
                    self.assertEqual(get(port, "/decisions/alpha").status, 200)
                    self.assertEqual(write.call_args.args[3], day)
                routes = ("/decisions/alpha", "/issues/7", "/issues/7/source", "/workers", "/decisions", "/")
                for route in routes:
                    with self.subTest(route=route):
                        response = get(port, route)
                        self.assertEqual(response.status, 200)
                        got = tabs(response.body.decode())
                        self.assertEqual([b.all_text() for b in badges(got["Dispatch"])], ["1"])
                        since = badges(got["Since you last looked"])
                        self.assertEqual([b.all_text() for b in since], ["1"])
                        self.assertIn("08:00", since[0].attrs["title"])
            finally:
                server.shutdown()
                server.server_close()
        finally:
            shutil.rmtree(root, True)

    def test_fallback_navigation_uses_tracker_day_and_decision_deadlines(self):
        import decision_page
        import task_forge
        from test_pages import ISSUE_TRACKER, LastLookedTest
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            now = datetime.now(ZoneInfo(ZONE))
            day = now.date() - timedelta(days=1)
            deadline = now.date() + timedelta(days=1)
            (root / "CLAUDE.md").write_text(
                "## Coordinator\n- User: Robin\n- Daily log dir: `daily/`\n- Tracker: `daily/<date>-tracker.md`\n"
                f"- Timezone: {ZONE}\n- Board: self-hosted; URL http://127.0.0.1:8765/; dir `pages/`\n"
                "- Settings: `cos.toml`\n")
            (root / "cos.toml").write_text(LastLookedTest.SETTINGS)
            (root / "daily").mkdir()
            (root / "pages").mkdir()
            (root / "daily" / f"{day}-tracker.md").write_text(
                ISSUE_TRACKER.format(day=day, task="Security audit") +
                f"\n## Decisions\n\n| time | item | words |\n|---|---|---|\n| 09:00 | deadline: Review {deadline} 16:00 | yes |\n")
            with mock.patch.object(task_forge, "tab_counts", wraps=task_forge.tab_counts) as counts, \
                 mock.patch.object(decision_page, "decision_context", wraps=decision_page.decision_context) as context:
                pg._tracker_navigation(root, root / "pages")
            args, kwargs = counts.call_args
            self.assertEqual(kwargs["tracker_day"], day)
            self.assertEqual(args[2].date(), now.date())
            self.assertEqual((args[2].second, args[2].microsecond), (0, 0))
            self.assertEqual([(d.name, d.at.date()) for d in args[1].deadlines], [("Review", deadline)])
            self.assertEqual(context.call_args.args[1:], (day, args[2]))

    def test_navigation_omits_unavailable_counts_and_keeps_known_zeros(self):
        # Unavailable keys are absent from the nav dict; a known zero is present as 0 and still draws no badge.
        import fragment
        import store
        import task_forge
        root = Path(tempfile.mkdtemp())
        try:
            self.assertEqual(pg._navigation(root, root / "pages", None), {})
            path = root / "store.db"
            store.migrate(path)
            db = store.Store(path)
            try:
                db.upsert_item("github:example/repo#1", title="Only item", stage="implement", forge_state="closed")
                counts = pg._navigation(root, root / "pages", task_forge.page_data(db))
            finally:
                db.engine.dispose()
            self.assertEqual(counts.get("workers"), 0)
            self.assertEqual(counts.get("dispatch"), 0)
            self.assertEqual(counts.get("decisions"), 0)
            self.assertNotIn("since", counts)
            self.assertNotIn("epics", counts)
            pages = root / "pages"
            pages.mkdir()
            (pages / "decision-alpha.json").write_text(decision("alpha"))
            db = store.Store(path)
            try:
                unavailable = pg._navigation(root, pages, task_forge.page_data(db))
            finally:
                db.engine.dispose()
            self.assertNotIn("decisions", unavailable)
            html = fragment.tab_bar("Board", 0, **counts)
            self.assertTrue(all(not badges(t) for t in tabs(html).values()))
        finally:
            shutil.rmtree(root, True)

    def test_badge_colours_distinguish_attention_ready_pending_and_risk(self):
        import fragment
        got = tabs(fragment.tab_bar("Board", 2, workers=1, dispatch=3, since=(4, "09:00"), epics=(1, 0)))
        tones = {label: got[label].attrs.get("data-tone") for label in TABS}
        self.assertEqual(tones["Workers"], "hot")
        self.assertEqual(tones["Decisions"], "pending")
        self.assertNotEqual(tones["Workers"], tones["Decisions"])
        for label in ("Dispatch", "Since you last looked", "Epics"):
            self.assertIsNone(tones[label], label)
        for tone in ("hot", "ready", "pending", "risk"):
            self.assertRegex(fragment.TAB_CSS, rf'nav\.tabs>\[data-tone={tone}\] \.badge\{{color:var\(--[^)]+\)\}}')
        self.assertEqual(badges(got["Since you last looked"])[0].all_text(), "4")

    def test_zero_counts_leave_out_badges(self):
        import fragment
        self.assertTrue(all(not badges(t) for t in tabs(fragment.tab_bar("Board", 0)).values()))


if __name__ == "__main__":
    unittest.main()
