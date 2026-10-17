"""One pass's facts are gathered before the planner runs."""

import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
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
        self.assertEqual(facts.asks, (readiness.Ask(KEY, None),))
        self.assertEqual(facts.reports[0].outcome, "blocked")
        self.assertEqual(dict(facts.alive), {((KEY, "implement", 1)): False})

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

    def test_a_github_list_of_blockers_is_keyed_by_issue(self):
        from evals.test_forge import FakeGh, gh_issue
        issue = gh_issue(1, "Blocked")
        issue["blockedBy"] = {"nodes": [{"number": 2, "state": "OPEN"}], "totalCount": 1}
        source = forge.Forge(backlog.GitHubBacklog("example/project"), gh=FakeGh(issues=[issue, gh_issue(2, "Blocker")]))
        self.assertEqual(source.blocked_by(), {"github:example/project#1": ("github:example/project#2",)})

