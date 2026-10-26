"""Store identities translate to cache labels without going through the URL grammar."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import backlog_ref  # noqa: E402
import task_forge  # noqa: E402

GL = backlog_ref.Backlog("https://gl.example", "g/p")
GH = backlog_ref.GitHubBacklog("o/n")


class SourceKeyTest(unittest.TestCase):
    def test_foreign_gitlab_key_with_single_segment_project_keeps_its_host(self):
        self.assertEqual(task_forge.source_key("gitlab:gl2.example/only#5", GL), "gl2.example/only#5")

    def test_foreign_gitlab_key_keeps_its_host_and_project(self):
        self.assertEqual(task_forge.source_key("gitlab:gl2.example/g/p#5", GL), "gl2.example/g/p#5")

    def test_store_keys_use_the_shared_reference_label(self):
        for key, home in (("gitlab:gl.example/g/p#5", GL),
                          ("gitlab:gl.example/acme/web#5", GL),
                          ("gitlab:gl2.example/team/nested/app#5", GL),
                          ("github:o/n#5", GH), ("github:other/repo#5", GH),
                          ("github:o/n#5", GL), ("gitlab:gl2.example/g/p#5", None)):
            with self.subTest(key=key, home=home):
                self.assertEqual(task_forge.source_key(key, home), backlog_ref.store_ref(key).label(home))

    def test_non_keys_still_use_issue_key(self):
        for key, expected in (("#5", "#5"), ("workflow", "workflow"),
                              ("https://gl.example/g/p/-/issues/5", "#5"),
                              ("https://github.com/o/n/issues/5", "o/n#5"),
                              ("gitlab:gl2.example/g/p#x", "gitlab:gl2.example/g/p#x")):
            with self.subTest(key=key):
                self.assertEqual(task_forge.source_key(key, GL), expected)
