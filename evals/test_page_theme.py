"""The canonical page theme, vendored here and carried by the board and decision pages.

Stage 2 of the page-theme plan (the owner, 2026-10-07): frank-lloyd-aight's `docs/page-theme.css` is the
canonical theme, `assets/page-theme.css` is its verbatim vendored copy, and `scripts/sync_page_theme.py`
re-copies it from the pinned source and exits non-zero on any drift. The board and decision pages inline
the whole theme ahead of their own rules: the theme rides at the top of each page's own one `<style>` block —
the one the other tests' `split("<style>")` slices — with a marker comment where it ends, and cascade order
(theme first, page rules second) is all that decides what wins.
"""

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(HERE))

import decision_page as dp  # noqa: E402
import pages as pg  # noqa: E402
import render_board as rb  # noqa: E402
import sync_page_theme as theme_sync  # noqa: E402

from test_decision_page import NOW as DP_NOW, decision  # noqa: E402
from test_pages import LANES, ZONE, get  # noqa: E402
from test_pages import TRACKER as PAGE_TRACKER  # noqa: E402
from test_render_board import CLAUDE_MD, NOW, TRACKER, parse_coordinator  # noqa: E402

THEME_ASSET = ROOT / "assets" / "page-theme.css"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def assert_carries_the_theme(test: unittest.TestCase, page: str) -> None:
    """The page carries the whole theme, inlined verbatim at the top of its own one `<style>` block — and caps no width."""
    theme = THEME_ASSET.read_text()
    test.assertIn(theme, page)
    test.assertLess(page.index(theme), page.index("/* the canonical theme ends here"))
    test.assertIn("color-scheme: dark", page)
    for mark in ("--ground: #eef1f5", "--f-head: \"IBM Plex Sans Condensed\"", "--f-body: \"Source Sans 3\"",
                 "--f-mono: \"JetBrains Mono\"", "--deployed: #2b3542", "--inflight: #a8660f",
                 "--designed: #2c58a0", "--warn: #b23a2a", ':root[data-theme="dark"] {',
                 "@media (prefers-color-scheme: dark) {"):
        test.assertIn(mark, page, mark)
    for rule in ("body{", "main{"):  # the pages' own rules; the theme's are spaced and cap nothing
        for body in re.findall(re.escape(rule) + r"([^}]*)}", page):
            test.assertNotIn("max-width", body, rule)
    test.assertNotIn("max-width:1280px", page)


class VendoredCopyTest(unittest.TestCase):
    def test_the_vendored_copy_hash_matches_the_recorded_hash(self):
        self.assertEqual(sha256(THEME_ASSET), theme_sync.PINNED_SHA256)

    def test_the_recorded_hash_is_tied_to_the_pinned_source(self):
        self.assertEqual(theme_sync.SOURCE,
                         "https://raw.githubusercontent.com/locriani/frank-lloyd-aight/a981dfb/docs/page-theme.css")


class SyncScriptTest(unittest.TestCase):
    """The script works on a copy of the repo tree, so tests never touch the real vendored copy."""

    def setUp(self):
        self.tree = Path(tempfile.mkdtemp())
        shutil.copytree(ROOT / "scripts", self.tree / "scripts")
        shutil.copytree(ROOT / "assets", self.tree / "assets")
        self.addCleanup(shutil.rmtree, self.tree, True)

    def sync(self, *argv: str) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, str(self.tree / "scripts" / "sync_page_theme.py"), *argv],
                              capture_output=True, text=True, timeout=60)

    def test_sync_from_canonical_is_a_no_op(self):
        vendored = (self.tree / "assets" / "page-theme.css").read_bytes()
        done = self.sync("--source", (ROOT / "assets" / "page-theme.css").as_uri())
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual((self.tree / "assets" / "page-theme.css").read_bytes(), vendored)

    def test_sync_replaces_a_drifted_vendored_copy(self):
        (self.tree / "assets" / "page-theme.css").write_text("/* drifted */\n")
        done = self.sync("--source", (ROOT / "assets" / "page-theme.css").as_uri())
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(sha256(self.tree / "assets" / "page-theme.css"), theme_sync.PINNED_SHA256)

    def test_sync_exits_non_zero_when_the_source_drifts(self):
        drifted = self.tree / "drifted.css"
        drifted.write_text(":root { /* not the theme */ }\n")
        done = self.sync("--source", drifted.as_uri())
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("sha256", done.stderr.lower())
        self.assertEqual((self.tree / "assets" / "page-theme.css").read_bytes(),
                         (ROOT / "assets" / "page-theme.css").read_bytes())

    def test_check_exits_non_zero_when_the_vendored_copy_drifts(self):
        (self.tree / "assets" / "page-theme.css").write_text("/* drifted */\n")
        done = self.sync("--check")
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("sha256", done.stderr.lower())

    def test_check_passes_when_the_vendored_copy_matches(self):
        self.assertEqual(self.sync("--check").returncode, 0)


class BoardPageThemeTest(unittest.TestCase):
    def setUp(self):
        self.cfg = parse_coordinator(CLAUDE_MD, today=NOW.date())
        self.page = rb.render(TRACKER, self.cfg, NOW)

    def test_the_board_carries_the_whole_theme(self):
        assert_carries_the_theme(self, self.page)


class DecisionPagesThemeTest(unittest.TestCase):
    def setUp(self):
        self.pages = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.pages, True)
        (self.pages / "decision-uploads-storage.json").write_text(json.dumps(decision()))
        dp.write_all(self.pages, dp.Context(DP_NOW), "2026-09-26")
        self.single = (self.pages / "decision-uploads-storage.html").read_text()
        self.listing = (self.pages / "decisions.html").read_text()

    def test_a_decision_page_carries_the_whole_theme(self):
        assert_carries_the_theme(self, self.single)

    def test_the_decisions_list_carries_the_whole_theme(self):
        assert_carries_the_theme(self, self.listing)


class DecisionPageSkillThemeTest(unittest.TestCase):
    """Stage 4 of the page-theme plan: the hand-written decision pages carry the theme through the skill's template.
    The JSON the skill teaches is what the coordinator writes by hand; the page it renders reads its theme from the
    vendored asset the server holds. The skill names that asset (as the canonical page specs name theirs), carries no
    styling of its own, and the asset it names is the pinned bytes wherever the install puts it."""

    SKILL = ROOT / "skills" / "decision-page" / "SKILL.md"
    NAMED_ASSET = re.compile(r"\$\{CLAUDE_PLUGIN_ROOT\}/(assets/page-theme\.css)")

    def setUp(self):
        self.skill = self.SKILL.read_text()

    def test_the_skill_names_the_vendored_theme_asset(self):
        """The template the agent copies ties its page to the canonical theme by naming the vendored asset the way
        the canonical page specs name theirs (frank-lloyd-aight docs/review-page.md §Stylesheet), so the hand-written
        flow cannot drift from the renderers' copy."""
        self.assertIsNotNone(self.NAMED_ASSET.search(self.skill),
                             "the skill never names ${CLAUDE_PLUGIN_ROOT}/assets/page-theme.css")

    def test_the_skill_instructs_no_inline_css(self):
        """The schema carries no styling and neither does the skill: no `<style>` block, no css fence, no hex colour —
        a hand-written decision page gets the theme only from the vendored asset the server reads."""
        self.assertNotIn("<style>", self.skill)
        self.assertNotIn("```css", self.skill)
        self.assertNotRegex(self.skill, r"#[0-9a-fA-F]{6}\b")

    def test_the_named_asset_is_the_pinned_bytes_in_a_standalone_install(self):
        """The standalone release copies skills, scripts and assets beside each other (start_coordinator.py), so the
        asset the skill names is the file the renderers read there too, and `sync_page_theme.py --check` stays honest
        in the release tree, not just in the checkout."""
        named = self.NAMED_ASSET.search(self.skill)
        release = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, release, True)
        for item in ("skills", "scripts", "assets"):
            shutil.copytree(ROOT / item, release / item)
        self.assertIsNotNone(named, "the skill names no theme asset to resolve in the release tree")
        self.assertEqual(sha256(release / named[1]), theme_sync.PINNED_SHA256)
        check = subprocess.run([sys.executable, str(release / "scripts" / "sync_page_theme.py"), "--check"],
                               capture_output=True, text=True, timeout=60)
        self.assertEqual(check.returncode, 0, check.stderr)

    def test_the_skill_template_renders_a_page_that_carries_the_whole_theme(self):
        """A decision JSON holding every part the skill's schema names renders, through the server's renderer, into a
        page that carries the whole theme — the carriage stage 2 landed, tied here to the skill's own template."""
        d = decision(timeline=[{"when": "09:40", "text": "impl-uploads stopped.", "kind": "pr"}])
        d["architecture"] = {"summary": "The parts the ask touches.",
                             "nodes": [{"id": "pool", "name": "pool.py", "state": "inflight", "detail": "src/cache/pool.py", "hot": True},
                                      {"id": "client", "name": "client.py", "state": "deployed"}],
                             "edges": [{"from": "client", "to": "pool", "label": "draws", "state": "deployed"}]}
        pages = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, pages, True)
        (pages / "decision-skill-template.json").write_text(json.dumps(d))
        dp.write(pages, pages, "skill-template", "2026-09-26")
        assert_carries_the_theme(self, (pages / "decision-skill-template.html").read_text())


class PagesServerThemeTest(unittest.TestCase):
    """The server's own render path — a GET re-rendering from the workspace — serves themed pages too."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, True)
        self.day = datetime.now(ZoneInfo(ZONE)).date().isoformat()
        (self.root / "CLAUDE.md").write_text(
            "## Coordinator\n- User: Robin\n- Daily log dir: `daily/`\n- Tracker: `daily/<date>-tracker.md`\n"
            f"- Timezone: {ZONE}\n- Board: self-hosted; URL http://127.0.0.1:8765/; dir `pages/`\n"
            "- Settings: `cos.toml`\n")
        (self.root / "daily").mkdir()
        (self.root / "daily" / f"{self.day}-tracker.md").write_text(PAGE_TRACKER.format(day=self.day, task="Security audit"))
        self.pages = self.root / "pages"
        self.pages.mkdir()
        (self.root / "cos.toml").write_text(LANES)
        (self.pages / "decision-ship-it.json").write_text(json.dumps(decision()))
        self.refreshed = threading.Event()
        server = pg.make_server(self.pages, 0, root=self.root, refresh=self.fake_refresh, every=3600)
        self.port = server.server_address[1]
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.assertTrue(self.refreshed.wait(10))

    def fake_refresh(self, root, now):
        import board_sources
        try:
            return board_sources.refresh(root, now)
        finally:
            self.refreshed.set()

    def test_the_served_board_and_decision_pages_carry_the_theme(self):
        theme = THEME_ASSET.read_text().encode()
        for path in ("/", "/decisions", "/decisions/ship-it"):
            resp = get(self.port, path)
            self.assertEqual(resp.status, 200, path)
            self.assertIn(theme, resp.body, path)
            self.assertIn(b"color-scheme: dark", resp.body, path)
            self.assertNotIn(b"max-width:1280px", resp.body, path)

    def test_a_resynced_theme_reaches_the_served_pages(self):
        """sync_page_theme.py replaces the file; the next look serves the new theme — no restart, no unrelated edit."""
        was = THEME_ASSET.read_bytes()
        THEME_ASSET.write_bytes(was + b"/* probe: a resynced theme carries this marker */\n")
        try:
            resp = get(self.port, "/")
        finally:
            THEME_ASSET.write_bytes(was)
        self.assertIn(b"/* probe: a resynced theme carries this marker */", resp.body)
