"""source_page.py: the "Source & diff" page for a task's change, `/issues/<n>/source` (Source48.dc.html, "Source & diff —
task !48"), unified diff only. Out of scope here: split view, coverage gutter, inline review notes, compare modes, tags.

The module the implementer creates is `scripts/source_page.py` with

    render(number: int, trackers: list[tuple[date, str]], sources: board_sources.Sources, now: datetime,
           graph: settings.Graph | None = None, pages: Path | None = None, root: Path | None = None) -> str | None
    write(root: Path, pages_dir: Path, number: int) -> Path | None

`render` returns None when no task in `trackers` names issue `number` (as issue_page.render). The change is the one the
sources say closes the issue; its diff is read from the clone at `root / graph.clone` when settings name a `[graph]`
table, otherwise from `root` when that path is a git repository, fetched and merge-based as module_graph.draw does.
With no clone path and no workspace repo, the page lists files from the sources cache only. `write` writes
`<pages_dir>/issue-<n>-source.html`, or removes it and returns None when `render` does. pages.py serves it at
`GET /issues/<n>/source`.

Markup contract these tests read (keep to it; everything else is free):

- The header is one `<header>` element.
- The file list is one `<ul class="files">` holding one `<li>` per changed file.
- Each file's diff is one `<table>` carrying `id="<anchor>"` and `data-path="<the file's path>"`; the task page's file
  links point at `/issues/<n>/source#<anchor>`. A hunk header's text (`@@ -a,b +c,d @@`) sits inside that table.
- Each diff line is one `<tr>` whose class list holds `add`, `del` or `ctx`, with the cells `<td class="o">` (old line
  number, empty on an added line), `<td class="n">` (new line number, empty on a removed line), `<td class="sign">`
  (`+`, `-` or `−`, empty on context) and `<td class="tx">` (the line's text, escaped).
- The page's own CSS is one `<style id="source">` block (decision_page.HEAD's shared CSS is not checked); inside it, and
  in any inline `style` attribute, a hex colour appears only as a custom-property declaration (`--name:#hex`).
"""

import re
import shutil
import sys
import tempfile
import threading
import unittest
from dataclasses import replace
from datetime import datetime
from html import unescape
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import board_sources as bs  # noqa: E402
import issue_page as ip  # noqa: E402
import make_repo  # noqa: E402
import pages as pg  # noqa: E402
import task_forge  # noqa: E402
from settings import Graph  # noqa: E402
from test_flow_chart import BoardTest, LANES, NOW, TODAY, YESTERDAY  # noqa: E402
from test_issue_page import ISSUE, OTHER, ROUTE_TRACKER, TODAY_START, YESTERDAY_TRACKER, ZONE  # noqa: E402
from test_pages import get  # noqa: E402

try:
    import source_page as sp  # noqa: E402
except ModuleNotFoundError:  # red until the implementer creates it; each test then fails on the import
    sp = None

TRACKERS = [(YESTERDAY, YESTERDAY_TRACKER), (TODAY, TODAY_START)]  # both name #109 and #111

# The change for #109 (!48): limits.py changed, quota.py new, legacy.py removed. One changed line holds markup.
LIMITS, QUOTA, LEGACY = "src/app/limits.py", "src/app/quota.py", "src/app/legacy.py"
BASE_FILES = {LIMITS: "MAX_MB = 10\n\n\ndef allowed(size):\n    return size <= MAX_MB * 1024 * 1024\n",
              LEGACY: "OLD_MB = 5\nOLD_NAME = 'legacy'\nOLD_ON = False\n",
              "src/app/form.py": "FIELDS = ()\n"}
MARKUP = 'HINT = "<script>alert(1)</script> is refused"'
CHANGED_LIMITS = ("MAX_MB = 25\n\n\ndef allowed(size):\n    return size <= MAX_MB * 1024 * 1024\n"
                  f"{MARKUP}\nLIMIT_NAME = 'upload'\n")
NEW_QUOTA = "QUOTA_MB = 500\nUSERS = 10\n"
BADGE = {LIMITS: "CHANGED", QUOTA: "NEW", LEGACY: "REMOVED"}
BADGES = ("NEW", "CHANGED", "REMOVED")


def forge(root: Path) -> tuple[str, str, dict[str, tuple[int, int]]]:
    """origin.git with main and !48 pushed to refs/merge-requests/48/head, main moved on since, and the workspace's
    clone at repos/app, which does not hold the change. Returns (merge base, head, {path: (additions, deletions)})."""
    dev = make_repo.build(root / "forge", {"clone": "dev", "files": BASE_FILES})
    base = make_repo.git(["rev-parse", "HEAD"], dev)
    make_repo.git(["checkout", "-q", "-b", "change"], dev)
    (dev / LIMITS).write_text(CHANGED_LIMITS)
    (dev / QUOTA).write_text(NEW_QUOTA)
    (dev / LEGACY).unlink()
    make_repo.git(["add", "-A"], dev)
    make_repo.git(["commit", "-q", "-m", "change"], dev)
    head = make_repo.git(["rev-parse", "HEAD"], dev)
    make_repo.git(["push", "-q", "origin", "change:refs/merge-requests/48/head"], dev)
    make_repo.git(["checkout", "-q", "main"], dev)
    (dev / "src" / "app" / "form.py").write_text("FIELDS = ('size',)\n")
    make_repo.git(["add", "-A"], dev)
    make_repo.git(["commit", "-q", "-m", "main moves on"], dev)
    make_repo.git(["push", "-q", "origin", "main"], dev)
    make_repo.git(["clone", "-q", "--no-local", str(root / "forge" / "origin.git"), str(root / "repos" / "app")], root)
    numstat = [line.split("\t") for line in make_repo.git(["diff", "--numstat", base, head], dev).splitlines()]
    return base, head, {p: (int(a), int(d)) for a, d, p in numstat}


def text(html: str) -> str:
    return re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]+>", " ", html))).strip()


def header(html: str) -> str:
    m = re.search(r"<header\b.*?</header>", html, re.S)
    if not m:
        raise AssertionError("the page has no <header>")
    return m[0]


def items(html: str) -> dict[str, str]:
    """The file list's entries by the path each names (contract: `<ul class="files">`, one `<li>` per file)."""
    m = re.search(r'<ul\b[^>]*class="[^"]*\bfiles\b[^"]*"[^>]*>(.*?)</ul>', html, re.S)
    if not m:
        raise AssertionError('the page has no <ul class="files"> file list')
    out = {}
    for li in re.findall(r"<li\b.*?</li>", m[1], re.S):
        got = text(li)
        path = max((p for p in (LIMITS, QUOTA, LEGACY) if p in got), key=len, default=got)
        out[path] = got
    return out


def tables(html: str) -> dict[str, tuple[str, str]]:
    """Each file's diff by path: (its anchor id, its table). Contract: `<table id=… data-path=…>`."""
    out = {}
    for m in re.finditer(r"<table\b([^>]*)>.*?</table>", html, re.S):
        attrs = dict(re.findall(r'([\w-]+)="([^"]*)"', m[1]))
        if "data-path" in attrs:
            out[unescape(attrs["data-path"])] = (unescape(attrs.get("id", "")), m[0])
    return out


def rows(table: str) -> list[dict]:
    """Each diff line: kind (add | del | ctx) and its cells o, n, sign, tx as text."""
    out = []
    for m in re.finditer(r"<tr\b([^>]*)>(.*?)</tr>", table, re.S):
        cls = re.search(r'class="([^"]*)"', m[1])
        kind = next((k for k in ("add", "del", "ctx") if cls and k in cls[1].split()), None)
        if kind is None:
            continue
        cells = {c: unescape(re.sub(r"<[^>]+>", "", body))
                 for c, body in re.findall(r'<td\b[^>]*class="(o|n|sign|tx)"[^>]*>(.*?)</td>', m[2], re.S)}
        out.append({"kind": kind, "raw": m[2], **{c: cells.get(c) for c in ("o", "n", "sign", "tx")}})
    return out


def count(a: int, d: int) -> str:
    """`+A −D`, where a zero part may be left out (Task.dc.html writes a new file's `+41`), but not both."""
    plus = rf"\+{a}" if a else r"(?:\+0)?"
    minus = rf"[−-]{d}" if d else r"(?:[−-]0)?"
    return rf"(?<![\w+−-]){plus}\s*{minus}(?![\w])"


class SourcePage(unittest.TestCase):
    """One real forge per class: a bare origin with !48 at refs/merge-requests/48/head and a --no-local clone."""

    @classmethod
    def setUpClass(cls):
        cls.root = Path(tempfile.mkdtemp())
        cls.base, cls.head, cls.numstat = forge(cls.root)
        (cls.root / "pages").mkdir()
        cls.graph = Graph(clone="repos/app", root="src")
        stats = tuple(bs.FileStat(p, a, d) for p, (a, d) in cls.numstat.items())
        cls.mr = bs.Change("!48", "https://forge.example/grp/app/-/merge_requests/48", "Cap uploads at 25 MB", "open",
                           False, "passed", False, 0, None, "main", tuple(cls.numstat), ("#109",), head=cls.head,
                           stats=stats)
        # !50 is open and also edits limits.py; !30 is merged and touched quota.py, so it is no overlap.
        cls.other = replace(OTHER, files=(LIMITS, "src/cache/pool.py"))
        cls.merged = bs.Change("!30", "https://forge.example/grp/app/-/merge_requests/30", "Old quota", "merged", False,
                               "passed", True, 1, NOW, "main", (QUOTA,), ("#131",))
        cls.cache = None

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.root, True)

    def sources(self, *changes: bs.Change) -> bs.Sources:
        changes = changes or (self.mr, self.other, self.merged)
        return bs.Sources({}, {"#109": ISSUE}, {c.ref: c for c in changes}, (), {},
                          bs.backlog.Backlog('https://forge.example', 'grp/app'))

    def render(self, number: int = 109, src: bs.Sources | None = None, graph: bool = True) -> str | None:
        if sp is None:
            raise AssertionError("scripts/source_page.py does not exist yet")
        if graph:
            return sp.render(number, TRACKERS, src or self.sources(), NOW, self.graph, self.root / "pages", self.root)
        return sp.render(number, TRACKERS, src or self.sources(), NOW)

    def page(self) -> str:
        """The page for #109 with the clone configured, rendered once per class."""
        if type(self).cache is None:
            type(self).cache = self.render()
        return type(self).cache


class RenderTest(SourcePage):
    def test_offline_warm_diff_is_shown_with_stale_warning(self):
        from unittest import mock
        import module_graph
        self.render()  # populate a verified comparison
        with mock.patch.object(module_graph, 'fetch', side_effect=ValueError('offline')):
            html = self.render()
        self.assertIn('Stale cached diff: the current base could not be verified.', html)
        self.assertIn('data-path="src/app/limits.py"', html)

    def test_no_header_prints_a_rendered_clock(self) -> None:
        # #215: the source crumb keeps the task and board links and the sources' read times, not a render clock.
        self.assertNotRegex(self.page(), r"\brendered \d{1,2}:\d{2}")

    def test_an_issue_no_task_names_has_no_page(self):
        # Interface: "`render` returns None when no task in `trackers` names issue `number`".
        self.assertIsNone(self.render(110))

    def test_the_header_names_the_change_and_the_issue_and_links_back(self):
        # "The source page's header names the change and the issue and links back: the task page `/issues/<n>` and
        # the board `/`."
        head = header(self.page())
        got = text(head)
        for want in ("!48", self.mr.title, "#109"):
            self.assertIn(want, got)
        self.assertIn('href="/issues/109"', head)
        self.assertIn('href="/"', head)

    def test_the_compare_box_names_the_merge_base_and_the_head(self):
        # "The compare box names the merge base and the head it diffs: `<base branch>@<short base sha> → <short head sha>`."
        self.assertRegex(text(self.page()), rf"\bmain@{self.base[:7]}\s*→\s*{self.head[:7]}\b")

    def test_the_file_list_shows_counts_badges_and_overlap(self):
        # "The file list shows each changed file with its +additions −deletions, a NEW, CHANGED or REMOVED badge, and
        # ALSO <ref> when another open change touches the same file."
        got = items(self.page())
        self.assertEqual(set(got), {LIMITS, QUOTA, LEGACY})
        for path, entry in got.items():
            with self.subTest(path=path):
                self.assertRegex(entry, count(*self.numstat[path]))
                self.assertEqual([b for b in BADGES if re.search(rf"\b{b}\b", entry)], [BADGE[path]], entry)
                if path == LIMITS:
                    self.assertRegex(entry, r"(?i)\balso\W+!50\b")
                else:
                    self.assertNotRegex(entry, r"(?i)\balso\b")  # !30 touched quota.py but is merged

    def test_the_list_count_is_files_and_totals(self):
        # "The list's count is the number of files and the totals: `N · +A −D`."
        a = sum(a for a, _ in self.numstat.values())
        d = sum(d for _, d in self.numstat.values())
        self.assertRegex(text(self.page()), rf"\b{len(self.numstat)}\s*·\s*\+{a}\s*[−-]{d}\b")

    def test_each_diff_shows_hunk_headers_and_numbered_signed_lines(self):
        # "Each file's diff shows its hunk headers and, per line, the old line number, the new line number, the sign
        # and the text; added and removed lines are marked apart from context."
        diffs = tables(self.page())
        self.assertEqual(set(diffs), {LIMITS, QUOTA, LEGACY})
        dev = self.root / "forge" / "dev"
        for path, (_, table) in diffs.items():
            with self.subTest(path=path):
                hunks = re.findall(r"^@@ [^@]* @@", make_repo.git(["diff", self.base, self.head, "--", path], dev), re.M)
                self.assertTrue(hunks)
                for hunk in hunks:
                    self.assertIn(hunk, text(table))
        lines = rows(diffs[LIMITS][1])
        want = [("del", "1", "", "MAX_MB = 10"), ("add", "", "1", "MAX_MB = 25"), ("ctx", "4", "4", "def allowed(size):"),
                ("add", "", "7", "LIMIT_NAME = 'upload'")]
        for kind, o, n, tx in want:
            with self.subTest(line=tx):
                row = next((r for r in lines if r["tx"] == tx), None)
                self.assertIsNotNone(row, f"no row whose tx is {tx!r}: {lines}")
                self.assertEqual(row["kind"], kind)
                self.assertEqual((row["o"] or "").strip(), o)
                self.assertEqual((row["n"] or "").strip(), n)
                self.assertIn((row["sign"] or "").strip(), {"add": ("+",), "del": ("-", "−"), "ctx": ("",)}[kind])
        self.assertEqual({r["kind"] for r in rows(diffs[QUOTA][1])}, {"add"})
        self.assertEqual({r["kind"] for r in rows(diffs[LEGACY][1])}, {"del"})

    def test_diff_text_is_shown_as_text(self):
        # "Diff text is shown as text, never as markup."
        html = self.page()
        self.assertNotIn("<script>alert(1)", html)
        row = next((r for r in rows(tables(html).get(LIMITS, ("", ""))[1]) if r["tx"] == MARKUP), None)
        self.assertIsNotNone(row, "the changed line holding markup is not a row whose text is that line")
        self.assertEqual(row["kind"], "add")
        self.assertIn("&lt;script&gt;", row["raw"])

    def test_without_a_graph_table_the_files_are_listed_and_the_clone_is_named(self):
        # "With no `[graph]` table and no workspace root passed, the page lists files from the sources cache, says the
        # diff needs the clone, and does not link file paths to missing anchors."
        html = self.render(graph=False)
        got = items(html)
        self.assertEqual(set(got), {LIMITS, QUOTA, LEGACY})
        for path, entry in got.items():
            with self.subTest(path=path):
                self.assertRegex(entry, count(*self.numstat[path]))
        self.assertRegex(text(html), r"(?i)\bclone\b|\[graph\]")
        self.assertEqual([r for _, t in tables(html).values() for r in rows(t)], [])
        self.assertIsNone(re.search(r'<tr\b[^>]*class="[^"]*\b(?:add|del|ctx)\b', html))
        m = re.search(r'<ul\b[^>]*class="[^"]*\bfiles\b[^"]*"[^>]*>(.*?)</ul>', html, re.S)
        self.assertIsNotNone(m)
        self.assertNotIn('href="#', m[1])

    def test_a_workspace_repo_without_a_graph_table_shows_its_diff(self):
        # "When settings name no `[graph]` table but the workspace root is the change's clone, the page shows the diff."
        pages = self.root / "fresh-pages"
        pages.mkdir()
        html = sp.render(109, TRACKERS, self.sources(), NOW, None, pages, self.root / "repos" / "app")
        self.assertIsNotNone(html)
        diffs = tables(html)
        self.assertEqual(set(diffs), {LIMITS, QUOTA, LEGACY})
        self.assertNotRegex(text(html), r"\[graph\]")
        m = re.search(r'<ul\b[^>]*class="[^"]*\bfiles\b[^"]*"[^>]*>(.*?)</ul>', html, re.S)
        self.assertIsNotNone(m)
        for path in (LIMITS, QUOTA, LEGACY):
            with self.subTest(path=path):
                anchor_id = diffs[path][0]
                self.assertRegex(m[1], rf'href="#{re.escape(anchor_id)}"[^>]*>\s*{re.escape(path)}\s*</a>')

    def test_workspace_fallback_keeps_stale_diff_notes_when_offline(self):
        from unittest import mock
        import module_graph
        clone, pages = self.root / "repos" / "app", self.root / "pages"
        sp.render(109, TRACKERS, self.sources(), NOW, None, pages, clone)
        with mock.patch.object(module_graph, "fetch", side_effect=ValueError("offline")):
            html = sp.render(109, TRACKERS, self.sources(), NOW, None, pages, clone)
        self.assertIn("Stale cached diff: the current base could not be verified.", html)
        self.assertIn('data-path="src/app/limits.py"', html)

    def test_graph_without_a_workspace_root_remains_a_file_listing(self):
        html = sp.render(109, TRACKERS, self.sources(), NOW, self.graph, self.root / "pages")
        self.assertIn(LIMITS, html)
        self.assertNotIn('href="#f-', html)

    def test_no_change_naming_the_issue_says_nothing_to_diff(self):
        # "With no change naming the issue the page says there is nothing to diff."
        html = self.render(src=self.sources(self.other))
        self.assertIsNotNone(html)
        self.assertRegex(text(html), r"(?i)\bnothing to diff\b")
        self.assertNotIn("!50", text(html))

    def test_own_colours_are_palette_tokens(self):
        # "The page's own colours are palette tokens, so the adopted dark scheme reaches them."
        html = self.page()
        own = re.search(r'<style\b[^>]*\bid="source"[^>]*>(.*?)</style>', html, re.S)
        self.assertIsNotNone(own, 'the page\'s own CSS is not in one <style id="source"> block')
        hexes = re.compile(r"#[0-9a-fA-F]{3,8}\b")
        raw = [f"{prop.strip()}:{value.strip()}" for block in re.findall(r"\{([^{}]*)\}", own[1])
               for prop, _, value in (d.partition(":") for d in block.split(";"))
               if hexes.search(value) and not prop.strip().startswith("--")]
        raw += [s for s in re.findall(r'\bstyle="([^"]*)"', html) if hexes.search(s)]
        self.assertEqual(raw, [], "a rule paints with a raw hex instead of a palette token")


class TaskPageLinkTest(SourcePage):
    def test_the_files_changed_card_links_to_the_source_page(self):
        # "The task page's Files changed card links to the source page: `open source & diff` goes to
        # `/issues/<n>/source`, and each file path links to that file on the source page."
        task = ip.render(109, TRACKERS, self.sources(), NOW, LANES,
                         replace(BoardTest().cfg(), backlog=self.sources().home))
        m = re.search(r"<h2[^>]*>\s*Files changed\b.*?(?=<h2|\Z)", task, re.S)
        self.assertIsNotNone(m, "the task page has no Files changed section")
        card = m[0]
        opens = [a for href, a in re.findall(r'<a\b[^>]*href="([^"]*)"[^>]*>(.*?)</a>', card, re.S)
                 if href == "/issues/109/source"]
        self.assertTrue(any(text(a).lower() == "open source & diff" for a in opens), card)
        diffs = tables(self.page())
        for path in (LIMITS, QUOTA, LEGACY):
            with self.subTest(path=path):
                link = re.search(rf'<a\b[^>]*href="/issues/109/source#([^"]+)"[^>]*>\s*{re.escape(path)}\s*</a>', card)
                self.assertIsNotNone(link, f"{path} does not link to the source page: {card}")
                self.assertIn(path, diffs)
                self.assertEqual(unescape(link[1]), diffs[path][0], "the link's anchor is not that file's diff")


class RouteTest(unittest.TestCase):
    """ "pages.py serves `GET /issues/<n>/source`; it is a 404 when no task names the issue, and the file
    `issue-<n>-source.html` is a route only." """

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, True)
        day = datetime.now(ZoneInfo(ZONE)).date().isoformat()
        (self.root / "CLAUDE.md").write_text(
            "## Coordinator\n- User: Robin\n- Daily log dir: `daily/`\n- Tracker: `daily/<date>-tracker.md`\n"
            f"- Timezone: {ZONE}\n- Board: self-hosted; URL http://127.0.0.1:8765/; dir `pages/`\n")
        (self.root / "daily").mkdir()
        (self.root / "daily" / f"{day}-tracker.md").write_text(ROUTE_TRACKER.format(day=day))
        self.pages = self.root / "pages"
        self.pages.mkdir()
        self.server = pg.make_server(self.pages, 0, root=self.root, refresh=lambda root, now: None, every=3600)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def test_an_issue_a_task_names_has_a_source_page(self):
        resp = get(self.port, "/issues/109/source")
        self.assertEqual(resp.status, 200)
        self.assertIn(b"#109", resp.body)

    def test_an_issue_no_task_names_is_not_found(self):
        self.assertEqual(get(self.port, "/issues/109/source").status, 200)  # the route exists for a named issue
        for path in ("/issues/110/source", "/issues/abc/source"):
            with self.subTest(path=path):
                self.assertEqual(get(self.port, path).status, 404)

    def test_the_rendered_file_is_a_route_only(self):
        self.assertEqual(get(self.port, "/issues/109/source").status, 200)
        self.assertTrue((self.pages / "issue-109-source.html").is_file(), "the route did not write issue-109-source.html")
        self.assertEqual(get(self.port, "/issue-109-source.html").status, 404)


class StoreAndCrossRepoSourcePageTest(unittest.TestCase):
    """#996: store-backed source pages resolve their own merged/open change without cross-repo bare-#N collisions
    and refuse unsafe thread URL schemes."""

    def make_db(self):
        from test_m6_store_board import make_store_workspace

        root, db = make_store_workspace()
        self.addCleanup(db.engine.dispose)
        self.addCleanup(shutil.rmtree, root, True)
        return db

    def test_store_task_finds_its_merged_change_on_numeric_and_qualified_routes(self):
        db = self.make_db()
        key = "gitlab:labs.example.test/team/app#5"
        db.upsert_item(key, title="Merged GitLab task", stage="merge", forge_state="closed")
        src = bs.Sources(
            {},
            {"#5": bs.Issue("#5", "https://labs.example.test/team/app/-/issues/5", "Merged GitLab task", "closed", ())},
            {"!12": bs.Change("!12", "https://labs.example.test/team/app/-/merge_requests/12", "Ship feature 5",
                              "merged", False, "passed", True, 1, NOW, "main", ("src/feature.py",), ("#5",))},
            (),
            {},
            home=bs.backlog.Backlog("https://labs.example.test", "team/app"),
        )
        pdata = task_forge.page_data(db)
        for route in (5, key):
            with self.subTest(route=route):
                html = sp.render(route, [], src, NOW, db=pdata)
                self.assertIsNotNone(html)
                self.assertIn("!12", text(header(html)))
                self.assertIn("src/feature.py", text(html))
                self.assertNotIn("Nothing to diff", text(html))

    def test_cross_repo_numeric_route_does_not_steal_home_repo_issue_or_change(self):
        db = self.make_db()
        db.upsert_item("github:example/other#12", title="Other-repo task", stage="implement")
        src = bs.Sources(
            {},
            {"#12": bs.Issue("#12", "https://github.com/example/repo/issues/12", "Wrong home issue", "open", (), "Wrong home body")},
            {"#55": bs.Change("#55", "https://github.com/example/repo/pull/55", "Wrong home PR", "open", False,
                              "passed", True, 1, None, "main", ("src/wrong.py",), ("#12",))},
            (),
            {},
            home=bs.backlog.GitHubBacklog("example/repo"),
        )
        html = sp.render(12, [], src, NOW, db=task_forge.page_data(db))
        self.assertIsNotNone(html)
        self.assertIn("Nothing to diff", text(html))
        for wrong in ("#55", "Wrong home issue", "Wrong home body", "Wrong home PR", "src/wrong.py"):
            self.assertNotIn(wrong, html)

    def test_unsafe_thread_url_is_not_linked(self):
        note = sp.Note(
            path="src/a.py",
            line=None,
            side="new",
            thread=bs.ReviewThread(False, "reviewer", "Check bounds", NOW, "javascript:alert(1)"),
        )
        mr = bs.Change("!48", "https://forge.example/grp/app/-/merge_requests/48", "Cap uploads", "open",
                       False, "passed", False, 0, None, "main", ("src/a.py",), ("#109",))
        html = sp.render(109, TRACKERS, bs.Sources({}, {"#109": ISSUE}, {"!48": mr}, (), {},
                                                    bs.backlog.Backlog('https://forge.example', 'grp/app')), NOW, notes=[note])
        self.assertNotRegex(html, r'(?i)href="\s*javascript:')
        self.assertNotIn('href=""', html)
        self.assertIn("Check bounds", text(html))


if __name__ == "__main__":
    unittest.main()
