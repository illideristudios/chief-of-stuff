"""No work without an issue (design decision 3, plan M2 item 2). Work with no forge issue files the issue first, then writes
exactly one store row keyed by it; a workspace with no `Backlog:` line is refused, and the setup is offered."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "evals"))
sys.path.insert(0, str(ROOT / "scripts"))

import backlog  # noqa: E402
import forge  # noqa: E402
import issue_gate  # noqa: E402
import store  # noqa: E402
import test_forge as tf  # noqa: E402  (module-qualified: don't re-collect its TestCases)

BLOCK = "# Workspace\n\n## Coordinator\n\n- User: Robin\n- Timezone: UTC\n{backlog}\n## After\n"


class GateTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        store.migrate(self.root / "store.db")
        self.store = store.Store(self.root / "store.db")
        self.addCleanup(self.store.engine.dispose)

    def workspace(self, backlog_line: str) -> Path:
        (self.root / "CLAUDE.md").write_text(BLOCK.format(backlog=backlog_line))
        return self.root

    def test_work_without_an_issue_files_the_issue_first_then_writes_exactly_one_row(self):
        gh = tf.FakeGh()
        f = forge.for_workspace(self.workspace("- Backlog: GitHub issues; repo example/app"), gh=gh)
        got = issue_gate.start(f, self.store, "Add a flag", body="why", commit=True)
        self.assertEqual(got, issue_gate.Started(issue_key="github:example/app#101"))
        self.assertEqual([c[:2] for c in gh.all_calls], [["issue", "create"]])
        self.assertEqual([(i.issue_key, i.title) for i in self.store.items()], [("github:example/app#101", "Add a flag")])
        # Starting the same issue again is the same row.
        issue_gate.start(f, self.store, "Add a flag", issue=101, commit=True)
        self.assertEqual(len(self.store.items()), 1)
        self.assertEqual(len(gh.all_calls), 1, "work on an issue that exists files nothing")

    def test_on_gitlab_too(self):
        def call(method, url, token, timeout, payload=None):
            return {"iid": 7, "web_url": "https://gitlab.example.test/team/app/-/issues/7"}, {}, ""

        f = forge.for_workspace(self.workspace(
            "- Backlog: GitLab; host https://gitlab.example.test; project team/app; token env SOME_TOKEN"), token="t")
        with mock.patch.object(backlog, "_call", call):
            got = issue_gate.start(f, self.store, "Add a flag", commit=True)
        self.assertEqual(got.issue_key, "gitlab:gitlab.example.test/team/app#7")
        self.assertEqual([i.issue_key for i in self.store.items()], ["gitlab:gitlab.example.test/team/app#7"])

    def test_a_create_that_fails_writes_no_row(self):
        gh = tf.FakeGh(fail={("issue", "create")})
        got = issue_gate.start(forge.Forge(tf.GH, gh=gh), self.store, "Add a flag", commit=True)
        self.assertIn("rate limited", got.refused)
        self.assertEqual(self.store.items(), [])

    def test_a_create_that_names_no_number_writes_no_row(self):
        f = forge.Forge(backlog.Backlog("https://gitlab.example.test", "team/app"), token="t")
        with mock.patch.object(backlog, "_call", lambda *a, **k: ({}, {}, "")):
            got = issue_gate.start(f, self.store, "Add a flag", commit=True)
        self.assertIn("no issue number", got.refused)
        self.assertEqual(self.store.items(), [])

    def test_a_preview_files_nothing_and_writes_no_row(self):
        gh = tf.FakeGh()
        got = issue_gate.start(forge.Forge(tf.GH, gh=gh), self.store, "Add a flag")
        self.assertEqual(got.preview, "would create: Add a flag")
        self.assertEqual((gh.all_calls, self.store.items()), ([], []))

    def test_no_backlog_is_refused_and_setup_is_offered(self):
        for root in (self.workspace(""), self.root / "no-workspace-here"):
            with self.subTest(root=root):
                got = issue_gate.start(forge.for_workspace(root), self.store, "Add a flag", commit=True)
                self.assertIn("no `Backlog:` line", got.refused)
                self.assertIn("- Backlog: GitHub issues; repo OWNER/NAME", got.setup)
                self.assertIn("chief-of-stuff init", got.setup)
                self.assertEqual(self.store.items(), [])


if __name__ == "__main__":
    unittest.main()
