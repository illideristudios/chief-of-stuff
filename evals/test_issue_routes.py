"""issue_routes.py: an issue's page is named by host, project and number, never by the number alone (#412).

    /issues/<host>/<project path>/<number>[/source]      the routes
    issue-q-<sha256>[-source].html                      the file a qualified route renders into
    /issues/<n>                                         the Backlog's own issue (a redirect); with no Backlog, a local namespace
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import backlog_ref as br  # noqa: E402
import issue_routes as ir  # noqa: E402

GH = br.GitHubBacklog("o/app")
GL = br.Backlog("https://labs.example.test", "team/app")


class ResolveTest(unittest.TestCase):
    def test_every_form_of_one_issue_resolves_to_one_identity(self):
        want = br.IssueRef("o/app", 8, "github.com")
        for cell in ("#8", "o/app#8", "https://github.com/o/app/issues/8", "[#8](https://github.com/o/app/issues/8)"):
            with self.subTest(cell=cell):
                self.assertEqual(ir.resolve(cell, GH), want)

    def test_gitlab_urls_work_items_nested_groups_and_the_cache_key(self):
        nested = br.IssueRef("group/sub/app", 8, "labs.example.test")
        for cell in ("https://labs.example.test/group/sub/app/-/issues/8", "https://labs.example.test/group/sub/app/-/work_items/8",
                     "labs.example.test/group/sub/app#8"):
            with self.subTest(cell=cell):
                self.assertEqual(ir.resolve(cell, GH), nested)
        self.assertEqual(ir.resolve("#8", GL), br.IssueRef("team/app", 8, "labs.example.test"))
        self.assertEqual(ir.resolve("labs.example.test/team/app#8", GL), ir.resolve("#8", GL))

    def test_a_bare_number_with_no_backlog_is_its_own_namespace(self):
        bare = ir.resolve("#8", None)
        self.assertEqual((bare.host, bare.repo, bare.number), ("", "", 8))
        self.assertNotEqual(bare, ir.resolve("o/app#8", None))
        self.assertNotEqual(bare, ir.resolve("#8", GH))

    def test_text_that_only_ends_in_a_digit_is_no_issue(self):
        for cell in ("", "workflow", "Issue 8", "8", "step 3", "https://example.com/x/8", "#", "../x#8", "o/../x#8"):
            with self.subTest(cell=cell):
                self.assertIsNone(ir.resolve(cell, GH))

    def test_an_integer_is_the_backlogs_issue_or_the_local_one(self):
        self.assertEqual(ir.target(8, GH), ir.resolve("#8", GH))
        self.assertEqual(ir.target(8, None), ir.resolve("#8", None))
        ref = br.IssueRef("o/x", 8, "github.com")
        self.assertIs(ir.target(ref, GH), ref)

    def test_the_cache_key_is_relative_to_the_backlog(self):
        self.assertEqual(ir.key(ir.target(8, GH), GH), "#8")
        self.assertEqual(ir.key(br.IssueRef("o/x", 8, "github.com"), GH), "o/x#8")
        self.assertEqual(ir.key(br.IssueRef("g/p", 8, "labs.example.test"), GH), "labs.example.test/g/p#8")
        self.assertEqual(ir.key(ir.target(8, None), None), "#8")


class RouteTest(unittest.TestCase):
    REFS = (br.IssueRef("team/app", 8, "github.com"), br.IssueRef("team/other", 8, "github.com"),
            br.IssueRef("team/app", 8, "labs.example.test"), br.IssueRef("group/sub/app", 8, "labs.example.test"),
            ir.target(8, None))

    def test_routes_name_host_project_and_number(self):
        self.assertEqual(ir.route(self.REFS[0]), "/issues/github.com/team/app/8")
        self.assertEqual(ir.route(self.REFS[3], True), "/issues/labs.example.test/group/sub/app/8/source")
        self.assertEqual(ir.route(self.REFS[4]), "/issues/8")

    def test_a_route_parses_back_to_what_built_it(self):
        for ref in self.REFS:
            for source in (False, True):
                with self.subTest(ref=ref, source=source):
                    self.assertEqual(ir.parse(ir.route(ref, source)), (ref, source))

    def test_safe_encoded_segments_decode_once(self):
        self.assertEqual(ir.parse("/issues/github.com/%74eam/app/8"), (self.REFS[0], False))

    def test_same_number_in_another_project_or_host_is_another_route_and_file(self):
        self.assertEqual(len({ir.route(r) for r in self.REFS}), len(self.REFS))
        self.assertEqual(len({ir.filename(r) for r in self.REFS}), len(self.REFS))
        self.assertEqual(len({ir.filename(r, True) for r in self.REFS}), len(self.REFS))

    def test_malformed_routes_are_none(self):
        for path in ("/issues", "/issues/", "/issues/x", "/issues/08", "/issues/-1", "/issues/٣", "/issues/8/", "/issues/8/source/x",
                     "/issues/github.com/team/app", "/issues/github.com/team/app/", "/issues/github.com/team/8",
                     "/issues/github.com/team/app/x/8", "/issues/github.com/team/app/08", "/issues/github.com/team/app/8/src",
                     "/issues/github.com/../app/8", "/issues/github.com/./app/8", "/issues/github.com//app/8",
                     "/issues/github.com/team/%2e%2e/8", "/issues/github.com/team%2Fapp/8", "/issues/github.com/team/app/8%2Fsource",
                     "/issues/github.com/team\\app/8", "/issues/labs.example.test/group/8", "/x/github.com/team/app/8"):
            with self.subTest(path=path):
                self.assertIsNone(ir.parse(path))


class FilenameTest(unittest.TestCase):
    def test_a_qualified_file_is_a_bounded_hash_apart_from_the_numeric_names(self):
        long = br.IssueRef("/".join(["very-long-group-name"] * 20), 8, "labs.example.test")
        for source, tail in ((False, ".html"), (True, "-source.html")):
            name = ir.filename(long, source)
            self.assertRegex(name, r"issue-q-[0-9a-f]{64}" + tail.replace(".", r"\."))
        self.assertEqual(ir.filename(ir.target(8, None)), "issue-8.html")
        self.assertEqual(ir.filename(ir.target(8, None), True), "issue-8-source.html")

    def test_the_hash_separates_what_a_slug_would_run_together(self):
        a, b = br.IssueRef("a/b-c", 1, "h.test"), br.IssueRef("a-b/c", 1, "h.test")
        self.assertNotEqual(ir.filename(a), ir.filename(b))

    def test_a_numeric_file_name_is_the_local_namespace_and_a_hash_is_not_read_back(self):
        self.assertEqual(ir.from_filename("issue-8.html"), ir.target(8, None))
        self.assertEqual(ir.from_filename("issue-8-source.html"), ir.target(8, None))
        self.assertIsNone(ir.from_filename(ir.filename(br.IssueRef("a/b", 1, "github.com"))))


if __name__ == "__main__":
    unittest.main()
