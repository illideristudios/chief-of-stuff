"""A planned merge verifies the forge again and logs success or refusal on its issue."""

from __future__ import annotations

import sys
import tempfile
import unittest
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "evals"))
sys.path.insert(0, str(ROOT / "scripts"))

import engine_merge  # noqa: E402
import forge  # noqa: E402
import store  # noqa: E402
import test_forge as tf  # noqa: E402
import test_merge_approved as tma  # noqa: E402
import test_merge_ready as tmr  # noqa: E402


@dataclass(frozen=True)
class Merge:
    issue_key: str


class ExecutorTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "store.db"
        store.migrate(path)
        self.db = store.Store(path)
        self.addCleanup(self.db.engine.dispose)
        self.gh = tf.github()
        self.forge = forge.Forge(tf.GH, gh=self.gh)
        self.key = self.forge.key(3)
        self.db.upsert_item(self.key, title="Fix the parser")
        self.action = Merge(self.key)
        self.at = datetime.now(timezone.utc).isoformat()

    def execute(self):
        return engine_merge.execute(self.action, self.forge, self.db, at=self.at)

    def message(self):
        entries = self.db.log(self.key)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].at, self.at)
        return entries[0].message

    def test_success_is_logged_on_the_issue_with_the_verified_head_and_pipeline(self):
        self.assertTrue(self.execute())
        self.assertEqual(len(self.gh.merges), 1)
        message = self.message()
        for evidence in ("merged", "#21", tma.HEAD, "9021"):
            self.assertIn(evidence, message)
        self.assertEqual(len(self.db.items()), 1)

    def test_a_refusal_is_logged_once_with_no_retry_or_crash(self):
        with mock.patch.object(forge.Forge, "merge", side_effect=forge.ForgeError("head changed")) as merge:
            self.assertFalse(self.execute())
        merge.assert_called_once_with(21, commit=True, issue_key=self.key)
        self.assertIn("merge refused", self.message())
        self.assertIn("head changed", self.db.log(self.key)[0].message)

    def test_a_queued_github_request_is_logged_once_and_returns_false(self):
        def queued(args, input_text=None):
            if args[:2] == ["pr", "view"]:
                return 0, '{"state": "OPEN"}', ""
            return self.gh(args, input_text)

        self.forge = forge.Forge(tf.GH, gh=queued)
        self.assertFalse(self.execute())
        self.assertEqual(len(self.gh.merges), 1)
        self.assertTrue(self.message().startswith("queued #21 "))

    def test_a_body_edited_to_close_another_issue_at_verification_is_refused(self):
        reads = 0

        def changed(args, input_text=None):
            nonlocal reads
            if args[:2] == ["pr", "list"]:
                reads += 1
                if reads == 2:
                    self.gh.prs[0]["body"] = "Closes #4"
            return self.gh(args, input_text)

        self.forge = forge.Forge(tf.GH, gh=changed)
        self.assertFalse(self.execute())
        self.assertEqual(reads, 2)
        self.assertEqual(self.gh.merges, [])
        self.assertIn("no open request closes", self.message())

    def test_a_newer_closing_request_at_verification_supersedes_the_selected_one(self):
        reads = 0

        def changed(args, input_text=None):
            nonlocal reads
            if args[:2] == ["api", "graphql"]:
                reads += 1
                if reads == 2:
                    self.gh.prs.append({**tma.pr(22, []), "body": "Closes #3"})
            return self.gh(args, input_text)

        self.forge = forge.Forge(tf.GH, gh=changed)
        self.assertFalse(self.execute())
        self.assertEqual(reads, 2)
        self.assertEqual(self.gh.merges, [])
        self.assertIn("merge refused", self.message())
        self.assertIn("#22", self.db.log(self.key)[0].message)

    def test_no_request_closing_the_issue_is_a_logged_refusal(self):
        self.gh.prs[0]["body"] = "Closes #4"
        self.assertFalse(self.execute())
        self.assertEqual(self.gh.merges, [])
        self.assertIn("no open request closes", self.message())

    def test_an_issue_from_another_forge_is_not_matched_by_number(self):
        self.key = "github:other/app#3"
        self.db.upsert_item(self.key)
        self.action = Merge(self.key)
        self.assertFalse(self.execute())
        self.assertEqual(self.gh.merges, [])
        self.assertIn("no open request closes", self.message())

    def test_failed_request_lookup_is_logged_with_no_write(self):
        self.gh.fail = {("pr", "list")}
        self.assertFalse(self.execute())
        self.assertEqual(self.gh.merges, [])
        self.assertIn("rate limited", self.message())

    def test_ready_at_lookup_but_red_at_merge_is_refused_and_logged(self):
        calls = 0

        def changed(args, input_text=None):
            nonlocal calls
            if args[:2] == ["pr", "list"]:
                calls += 1
                if calls == 2:
                    self.gh.prs[0]["statusCheckRollup"] = tmr.RED
            return self.gh(args, input_text)

        self.forge = forge.Forge(tf.GH, gh=changed)
        self.assertFalse(self.execute())
        self.assertEqual(calls, 2)
        self.assertEqual(self.gh.merges, [])
        self.assertIn("red", self.message())

    def test_several_closing_requests_use_the_newest_like_the_forge_snapshot(self):
        self.gh.prs.append({**tma.pr(22, []), "body": "Closes #3", "statusCheckRollup": tmr.RED})
        self.assertFalse(self.execute())
        self.assertEqual(self.gh.merges, [])
        message = self.message()
        self.assertIn("#22", message)
        self.assertIn("red", message)

    def test_gitlab_request_links_are_used_for_a_logged_merge(self):
        self.forge = forge.Forge(tf.GL, token="t")
        self.key = self.forge.key(3)
        self.action = Merge(self.key)
        self.db.upsert_item(self.key)
        calls = []
        read = tmr.gitlab_fake({5: {}})

        def call(method, url, token, timeout, payload=None):
            if method == "PUT":
                calls.append(payload)
                return {"state": "merged"}, {}, ""
            row, headers, error = read(method, url, token, timeout, payload)
            if url.endswith("/merge_requests/5"):
                row = {**row, "description": "Closes #3"}
            return row, headers, error

        with mock.patch.object(forge.backlog, "_call", call):
            self.assertTrue(self.execute())
        self.assertEqual(calls, [{"sha": tma.HEAD}])
        self.assertIn("!5", self.message())

    def test_a_queued_gitlab_request_is_logged_once_and_returns_false(self):
        self.forge = forge.Forge(tf.GL, token="t")
        self.key = self.forge.key(3)
        self.action = Merge(self.key)
        self.db.upsert_item(self.key)
        writes = []
        read = tmr.gitlab_fake({5: {}})

        def call(method, url, token, timeout, payload=None):
            if method == "PUT":
                writes.append(payload)
                return {"state": "opened", "merge_when_pipeline_succeeds": True}, {}, ""
            row, headers, error = read(method, url, token, timeout, payload)
            if url.endswith("/merge_requests/5"):
                row = {**row, "description": "Closes #3"}
            return row, headers, error

        with mock.patch.object(forge.backlog, "_call", call):
            self.assertFalse(self.execute())
        self.assertEqual(writes, [{"sha": tma.HEAD}])
        self.assertTrue(self.message().startswith("queued !5 "))


if __name__ == "__main__":
    unittest.main()
