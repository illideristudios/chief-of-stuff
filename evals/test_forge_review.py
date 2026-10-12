"""Forge review evidence keeps the commit that ran separate from the source head it covers."""

from __future__ import annotations

import dataclasses
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "evals"))

import forge_review as fr  # noqa: E402
import test_merge_ready as tmr  # noqa: E402


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
