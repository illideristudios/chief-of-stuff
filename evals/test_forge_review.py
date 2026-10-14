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


class GitLabPipelineEvidenceTest(unittest.TestCase):
    def test_merged_results_keeps_the_pipeline_commit_and_records_the_source_head(self):
        call = tmr.gitlab_fake({26: tmr.GITLAB[26][0]})
        (r,) = fr.gitlab_requests(tmr.GitLabReadyTest.HOME, "team/app", "", "t", call)
        self.assertEqual(r.pipeline_sha, "c" * 40)
        self.assertEqual(r.head, tmr.HEAD)
        self.assertEqual(r.pipeline_head, tmr.HEAD)
        self.assertEqual(dataclasses.replace(r, threads=0).verdict, "ready")
        self.assertEqual(dataclasses.replace(r, head=tmr.OLD, threads=0).verdict, "stale pipeline")


class ClosesTest(unittest.TestCase):
    def test_closes_records_same_repo_and_cross_repository_refs(self):
        self.assertEqual(fr.closes("Fixes #3\nFixes other/repo#99"), ("#3", "other/repo#99"))
        self.assertEqual(fr.closes("Closes other/repo#99"), ("other/repo#99",))
        self.assertEqual(fr.closes("Resolves #3\nSee other/repo#99"), ("#3",))


if __name__ == "__main__":
    unittest.main()
