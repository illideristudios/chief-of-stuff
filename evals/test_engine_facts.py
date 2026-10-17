"""One pass's facts are gathered before the planner runs."""

import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import engine_facts
import forge
import readiness
import store
import backlog

NOW = datetime(2000, 1, 1, tzinfo=timezone.utc)
KEY = "github:example/project#1"
OTHER = "github:example/project#2"


class GatherTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.trees = self.root / "trees"
        self.trees.mkdir()
        path = self.root / "store.db"
        store.migrate(path)
        self.db = store.Store(path, clock=lambda: NOW)
        self.addCleanup(self.db.engine.dispose)
        self.db.upsert_item(KEY, stage="implement")
        self.db.set_ownership(KEY, "src/a.py", "write")
        self.db.set_ownership(KEY, "docs/readme", "read")
        self.db.add_ask(KEY, "implement", 1, "sess", "Ship it?", ["yes"], "")
        self.db.add_report(KEY, "implement", 1, "sess", "blocked", "waiting", [])
        self.db.record_segment(KEY, "implement", 1, started_at=NOW.isoformat())
        self.db.set_resume(KEY, {"runs": {"implement-1": {
            "pid": 42, "session_id": "sess", "identity": "gone",
            "runner_pid": 43, "runner_identity": "runner-gone"}}})

    def test_gather_reads_store_rows_and_a_dead_worker(self):
        forge = mock.Mock()
        forge.blocked_by.return_value = {KEY: (OTHER,)}
        with mock.patch.object(engine_facts.process_status, "identity", return_value="other"):
            facts = engine_facts.gather(forge=forge, store=self.db, root=self.root, trees=self.trees, now=NOW)
        self.assertIn(readiness.Claim(KEY, "src/a.py", "write"), facts.claims)
        self.assertIn(readiness.Claim(KEY, "docs/readme", "read"), facts.claims)
        self.assertEqual(dict(facts.blockers), {KEY: (OTHER,)})
        self.assertEqual(facts.asks, (readiness.Ask(KEY, None, "implement", 1),))
        self.assertEqual(facts.reports[0].outcome, "blocked")
        self.assertEqual(dict(facts.alive), {((KEY, "implement", 1)): False})

    def gather(self, forge=None):
        forge = forge or mock.Mock(**{"blocked_by.return_value": {}})
        return engine_facts.gather(forge=forge, store=self.db, root=self.root, trees=self.trees, now=NOW)

    def test_a_worktree_row_is_not_a_file_claim_and_arms_no_read_only_check(self):
        self.db.upsert_item(OTHER, stage="implement")
        self.db.set_ownership(OTHER, str(self.trees / "issue-2-implement-1"))
        with mock.patch.object(engine_facts.process_status, "identity", return_value="other"):
            facts = self.gather()
        self.assertFalse([c for c in facts.claims if c.issue_key == OTHER])
        self.assertNotIn(OTHER, facts.read_only)

    def test_reports_follow_the_order_they_were_written(self):
        self.db.add_report(KEY, "implement", 2, "sess", "done", "ok", [])
        self.db.add_report(KEY, "review", 1, "sess", "blocked", "stuck", [])
        self.assertEqual([(r.stage, r.attempt) for r in self.gather().reports][-1], ("review", 1))

    def test_a_report_written_while_liveness_is_read_is_seen(self):
        def appear(*args, **kwargs):
            self.db.add_report(KEY, "implement", 1, "sess", "done", "ok", [])
            return "other"
        with mock.patch.object(engine_facts.process_status, "identity", side_effect=appear):
            self.assertEqual(self.gather().reports[-1].outcome, "done")

    def test_a_worker_with_an_exit_record_is_left_to_collect(self):
        run = self.root / "run"
        run.mkdir()
        (run / "exit.json").write_text("{}")
        data = self.db.resume(KEY)
        data["runs"]["implement-1"]["run_dir"] = str(run)
        self.db.set_resume(KEY, data)
        with mock.patch.object(engine_facts.process_status, "identity", return_value="other"):
            self.assertEqual(self.gather().alive, ())

    def test_a_tree_git_cannot_open_is_unknown_not_a_failed_pass(self):
        tree = self.trees / "issue-1-implement-1"
        tree.mkdir()
        data = self.db.resume(KEY)
        data["runs"]["implement-1"]["tree"] = str(tree)
        self.db.set_resume(KEY, data)
        for error in (engine_facts.git_view.CacheError("cache"), subprocess.TimeoutExpired("git", 1)):
            with self.subTest(error=error), \
                 mock.patch.object(engine_facts.git_trees, "discover", return_value=[tree]), \
                 mock.patch.object(engine_facts.git_view, "opened", side_effect=error), \
                 mock.patch.object(engine_facts.process_status, "identity", return_value="other"):
                self.assertEqual(self.gather().changed, ((KEY, None),))

    def test_a_forge_that_cannot_read_blockers_is_unknown(self):
        self.assertIsNone(self.gather(SimpleNamespace()).blockers)

    def test_a_failed_relationship_read_says_why(self):
        failing = mock.Mock(**{"blocked_by.side_effect": engine_facts.ForgeError("auth required")})
        facts = self.gather(failing)
        self.assertEqual((facts.blockers, facts.blockers_error), (None, "auth required"))

    def test_a_failed_relationship_read_is_unknown(self):
        forge = mock.Mock()
        forge.blocked_by.return_value = None
        facts = engine_facts.gather(forge=forge, store=self.db, root=self.root, trees=self.trees, now=NOW)
        self.assertIsNone(facts.blockers)


class BlockedByTest(unittest.TestCase):
    def test_github_blocked_by_is_the_open_nodes(self):
        self.assertEqual(forge._blocked_numbers({"nodes": [{"number": 2, "state": "OPEN"},
                                                           {"number": 3, "state": "CLOSED"}], "totalCount": 2}), (2,))
        self.assertIsNone(forge._blocked_numbers({"nodes": [], "totalCount": 1}))
        self.assertEqual(forge._blocked_numbers(None), ())

    def test_a_github_read_failure_raises_with_its_reason(self):
        source = forge.Forge(backlog.GitHubBacklog("example/project"), gh=lambda args: (1, "", "auth required"))
        with self.assertRaisesRegex(engine_facts.ForgeError, "auth required"):
            source.blocked_by()

    def test_a_gitlab_links_list_with_a_next_page_is_partial(self):
        fake = SimpleNamespace(issues=lambda: SimpleNamespace(ok=True, issues=[SimpleNamespace(iid=1)]),
                               _token=lambda: "token", home=SimpleNamespace(issues_url="https://gl/issues"),
                               key=lambda n: f"gitlab:p#{n}")
        with mock.patch.object(backlog, "_get", return_value=([], {"x-next-page": "2"}, "")):
            with self.assertRaisesRegex(engine_facts.ForgeError, "more than one page"):
                forge._gitlab_blocked(fake)

    def test_a_github_list_of_blockers_is_keyed_by_issue(self):
        from evals.test_forge import FakeGh, gh_issue
        issue = gh_issue(1, "Blocked")
        issue["blockedBy"] = {"nodes": [{"number": 2, "state": "OPEN"}], "totalCount": 1}
        source = forge.Forge(backlog.GitHubBacklog("example/project"), gh=FakeGh(issues=[issue, gh_issue(2, "Blocker")]))
        self.assertEqual(source.blocked_by(), {"github:example/project#1": ("github:example/project#2",)})

