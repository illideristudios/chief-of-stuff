"""Forge review evidence keeps the commit that ran separate from the source head it covers."""

from __future__ import annotations

import dataclasses
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "evals"))

import forge_review as fr  # noqa: E402
import test_merge_ready as tmr  # noqa: E402


def closing_issue(number, repo="example/app"):
    owner, name = repo.split("/", 1)
    return {"number": number, "url": f"https://github.com/{repo}/issues/{number}",
            "repository": {"name": name, "owner": {"login": owner}}}


class GitHubClosingTest(unittest.TestCase):
    def test_open_requests_union_sidebar_links_with_body_and_filter_other_repositories(self):
        rows = [{**tmr.tma.pr(21, []), "body": "Closes #3; fixes #4",
                 "closingIssuesReferences": [closing_issue(3), closing_issue(5, "Example/App"),
                                             closing_issue(6, "other/app"), closing_issue(7, "example/other")]},
                {**tmr.tma.pr(22, []), "body": "", "closingIssuesReferences": [closing_issue(8)]},
                {**tmr.tma.pr(23, []), "body": "Closes #9"}]

        def gh(args):
            fields = args[args.index("--json") + 1].split(",")
            return 0, json.dumps([{k: v for k, v in row.items() if k in fields} for row in rows]), ""

        got = fr.github_requests("example/app", "", gh)
        self.assertEqual([r.closes for r in got], [("#3", "#4", "#5"), ("#8",), ("#9",)])


class ClosesTest(unittest.TestCase):
    def test_native_closing_forms_name_every_issue_once(self):
        for body, want in (("Closes #3", ("#3",)), ("Closes: #3", ("#3",)), ("fixes:  #7", ("#7",)),
                           ("Closes #3, #4 and #5", ("#3", "#4", "#5")), ("Resolves #3 and #4", ("#3", "#4")),
                           ("Closes #3. Fixes #3", ("#3",)), ("closing #3", ()), ("See #3, #4", ())):
            with self.subTest(body=body):
                self.assertEqual(fr.closes(body), want)

    def test_gitlab_closing_forms_include_its_ing_and_implement_keywords(self):
        for body, want in (("Closing #3", ("#3",)), ("Fixing #3", ("#3",)), ("Resolving: #3", ("#3",)),
                           ("Implements #3 and #4", ("#3", "#4")), ("Implemented issue #5", ("#5",)),
                           ("Closes #3", ("#3",)), ("See #3", ())):
            with self.subTest(body=body):
                self.assertEqual(fr.closes(body, gitlab=True), want)
        self.assertEqual(fr.closes("Implements #3"), ())  # GitHub does not close on it

    def test_gitlab_scopes_url_and_project_references_to_the_configured_project(self):
        for ref, want in (("https://gitlab.example.test/Team/App/-/issues/3", ("#3",)),
                          ("https://gitlab.example.test/team/other/-/issues/3", ()),
                          ("https://other.example.test/team/app/-/issues/3", ()),
                          ("Team/App#3", ("#3",)), ("other/app#3", ()), ("team/other#3", ()),
                          ("#3", ("#3",)),
                          ("team/other#3, #4 and https://gitlab.example.test/team/app/-/issues/5", ("#4", "#5")),
                          ("team/app#3, #3 and https://gitlab.example.test/team/app/-/issues/3", ("#3",))):
            with self.subTest(ref=ref):
                self.assertEqual(fr.closes(f"Closes {ref}", gitlab=True, project="team/app",
                                           host="https://gitlab.example.test"), want)


class GitLabPipelineEvidenceTest(unittest.TestCase):
    def test_merged_results_keeps_the_pipeline_commit_and_records_the_source_head(self):
        call = tmr.gitlab_fake({26: tmr.GITLAB[26][0]})
        (r,) = fr.gitlab_requests(tmr.GitLabReadyTest.HOME, "team/app", "", "t", call)
        self.assertEqual(r.pipeline_sha, "c" * 40)
        self.assertEqual(r.head, tmr.HEAD)
        self.assertEqual(r.pipeline_head, tmr.HEAD)
        self.assertEqual(dataclasses.replace(r, threads=0).verdict, "ready")
        self.assertEqual(dataclasses.replace(r, head=tmr.OLD, threads=0).verdict, "stale pipeline")


if __name__ == "__main__":
    unittest.main()
