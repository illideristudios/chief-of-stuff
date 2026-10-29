"""#967: every served board page names its workspace in its <title> and first <h1>, so tabs of different boards differ.

```
w := the workspace a board serves      B := its Backlog line names a repository/project      N := title and h1 name w
1.  B → label = the repository/project's last segment      [authoritative config]
2.  ¬B → label = "<root folder> (folder)"                  [accurate fallback: never passed off as a project]
3.  label is escaped wherever it lands
∴   w1 ≠ w2 → titles and headings differ
```
"""
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import page_common  # noqa: E402
import pages as pg  # noqa: E402
from test_pages import ZONE, _PagesFixture, get  # noqa: E402

TITLE, H1 = re.compile(r"<title>(.*?)</title>", re.S), re.compile(r"<h1[^>]*>(.*?)</h1>", re.S)


class IdentifyTest(unittest.TestCase):
    def test_prefixes_the_title_and_the_first_heading_only(self):
        page = b'<title>Board 2026-10-19</title><h1 class="x">Board</h1><h1>Other</h1>'
        self.assertEqual(page_common.identify(page, "drago"),
                         b'<title>drago \xc2\xb7 Board 2026-10-19</title><h1 class="x">drago \xc2\xb7 Board</h1><h1>Other</h1>')

    def test_escapes_the_label_and_leaves_a_page_without_either_tag_alone(self):
        out = page_common.identify(b"<title>T</title><h1>H</h1>", '<script>alert(1)</script>&"').decode()
        self.assertNotIn("<script>", out)
        self.assertEqual(out.count("&lt;script&gt;alert(1)&lt;/script&gt;&amp;"), 2)
        self.assertEqual(page_common.identify(b"<p>x</p>", "drago"), b"<p>x</p>")


class WorkspaceLabelTest(unittest.TestCase):
    def label(self, claude: str | None, name: str = "ws") -> str:
        import tempfile
        with tempfile.TemporaryDirectory() as parent:
            root = Path(parent) / name
            root.mkdir()
            if claude is not None:
                (root / "CLAUDE.md").write_text(claude)
            return pg.workspace_label(root)

    def test_github_gitlab_and_the_labelled_fallbacks(self):
        base = ("## Coordinator\n- User: Robin\n- Daily log dir: `daily/`\n- Tracker: `daily/<date>-tracker.md`\n"
                f"- Timezone: {ZONE}\n- Board: self-hosted; URL http://127.0.0.1:8765/; dir `pages/`\n")
        for claude, want in (
            (base + "- Backlog: GitHub issues; repo https://github.com/o/chief-of-stuff (private)\n", "chief-of-stuff"),
            (base + "- Backlog: GitLab; host https://gitlab.example.com; project grp/sub/drago; token env X\n", "drago"),
            (base, "ws (folder)"),  # a config with no Backlog line
            ("not a config", "ws (folder)"),  # a config that does not parse
            (None, "ws (folder)"),  # no config file at all
        ):
            with self.subTest(want=want):
                self.assertEqual(self.label(claude), want)

    def test_the_fallback_is_the_folder_name_unescaped_for_the_caller_to_escape(self):
        self.assertEqual(self.label(None, name="a<b>&c"), "a<b>&c (folder)")


class ServedIdentityTest(_PagesFixture, unittest.TestCase):
    BACKLOG = "- Backlog: GitHub issues; repo {repo}\n"

    def set_backlog(self, repo: str | None):
        claude = self.root / "CLAUDE.md"
        text = "".join(line for line in claude.read_text().splitlines(True) if not line.startswith("- Backlog:"))
        claude.write_text(text + (self.BACKLOG.format(repo=repo) if repo else ""))

    def names(self, route: str) -> tuple[str, str]:
        resp = get(self.port, route)
        self.assertEqual(resp.status, 200, route)
        body = resp.body.decode()
        return TITLE.search(body)[1], H1.search(body)[1]

    def test_every_page_type_names_its_workspace_and_two_workspaces_differ(self):
        seen = {}
        for repo, label in (("example/alpha", "alpha"), ("example/beta", "beta")):
            self.set_backlog(repo)
            for route in self.PAGES:
                with self.subTest(repo=repo, route=route):
                    title, heading = self.names(route)
                    self.assertTrue(title.startswith(f"{label} · "), title)
                    self.assertIn(f"{label} · ", heading)
                    seen.setdefault(route, set()).add(title)
        for route, titles in seen.items():
            self.assertEqual(len(titles), 2, f"{route}: the two workspaces' titles are identical")

    def test_the_board_title_and_heading_read_as_the_issue_asks(self):
        self.set_backlog("example/alpha")
        title, heading = self.names("/")
        self.assertRegex(title, r"^alpha · Board \d{4}-\d{2}-\d{2}$")
        self.assertRegex(heading, r"^alpha · Board · ")

    def test_missing_configuration_is_labelled_as_a_folder(self):
        self.set_backlog(None)
        for route in self.PAGES:
            with self.subTest(route=route):
                self.assertTrue(self.names(route)[0].startswith("ws (folder) · "))
        (self.root / "CLAUDE.md").unlink()  # the last good page is still served, still labelled
        self.assertTrue(self.names("/workers")[0].startswith("ws (folder) · "))

    def test_head_matches_get_and_the_file_on_disk_stays_unlabelled(self):
        self.set_backlog("example/alpha")
        for route in self.PAGES:
            with self.subTest(route=route):
                got, head = get(self.port, route), get(self.port, route, method="HEAD")
                self.assertEqual(head.getheader("Content-Length"), str(len(got.body)))
                self.assertEqual(head.getheader("Last-Modified"), got.getheader("Last-Modified"))
                self.assertNotIn("alpha", self.file(route).read_text())

    def test_a_page_an_agent_wrote_is_served_as_it_is(self):
        self.set_backlog("example/alpha")
        (self.pages / "arch-review.html").write_text("<title>Review</title><h1>Review</h1>")
        title, heading = self.names("/arch-review.html")
        self.assertEqual((title, heading), ("Review", "Review"))


if __name__ == "__main__":
    unittest.main()
