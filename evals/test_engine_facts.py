"""One pass's facts are gathered before the planner runs."""

import subprocess
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import engine_facts
import forge_review
import forge
import readiness
import store
import backlog
from tree_state import TreeState

NOW = datetime(2000, 1, 1, tzinfo=timezone.utc)
KEY = "github:example/project#1"
OTHER = "github:example/project#2"


class GatherTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
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

    def released_request(self, branch="work/issue-1", closes=("#1",), number=10, **kwargs):
        return forge_review.Request(number, "Work", "", "head", "none", "running", "yes",
                                    branch=branch, closes=closes, **kwargs)

    def released_segment(self, key=KEY, attempt=1, branch="work/issue-1"):
        self.db.upsert_item(key, stage="implement", forge_state="open", human_review=False)
        for segment in self.db.segments(key):
            if segment.ended_at is None:
                self.db.drop_segment(key, segment.stage, segment.attempt)
        self.db.record_segment(key, "implement", attempt, started_at=(NOW-timedelta(hours=2)).isoformat(),
                               ended_at=(NOW-timedelta(hours=1)).isoformat(), outcome="human_review")
        self.db.set_resume(key, {"runs": {f"implement-{attempt}": {"branch": branch}}})

    def release_forge(self, requests, home=None):
        f = forge.Forge(home or backlog.GitHubBacklog("example/project"))
        return SimpleNamespace(requests=mock.Mock(return_value=requests), blocked_by=lambda: {},
                               branch_head=lambda branch: None, key=f.key,
                               closing_request=mock.Mock(wraps=f.closing_request))

    def release_gather(self, f, observations=((KEY, False),)):
        return engine_facts.gather(forge=f, store=self.db, root=self.root, trees=self.trees, now=NOW,
                                   hold_observations=observations)

    def test_released_request_uses_closing_selector_for_both_forges(self):
        from evals import test_forge as tf
        for home in (backlog.GitHubBacklog("example/project"), tf.GL):
            f = self.release_forge([self.released_request(draft=True)], home)
            key = f.key(1)
            self.db.upsert_item(KEY, forge_state="closed")
            self.released_segment(key)
            facts = self.release_gather(f, ((key, False),))
            self.assertEqual(dict(facts.implementation_requests)[(key, "implement", 1)], True)
            f.requests.assert_called_once_with()
            f.closing_request.assert_called_once_with(key, f.requests.return_value)

    def test_empty_wrong_branch_wrong_issue_and_multiple_closes_are_absent(self):
        self.released_segment()
        for requests in ([], [self.released_request(branch="other")], [self.released_request(closes=("#2",))],
                         [self.released_request(closes=("#1", "#2"))],
                         [self.released_request(closes=("other/project#1",))],
                         [self.released_request(), self.released_request(branch="other", number=11)]):
            with self.subTest(requests=requests):
                facts = self.release_gather(self.release_forge(requests))
                self.assertIs(dict(facts.implementation_requests)[(KEY, "implement", 1)], False)

    def test_request_failures_and_malformed_reads_are_unknown(self):
        self.released_segment()
        for requests in (None, [object()], [self.released_request(closes=None)],
                         [self.released_request(number="10")], [self.released_request(branch=None)],
                         [self.released_request(), object()]):
            with self.subTest(requests=requests):
                facts = self.release_gather(self.release_forge(requests))
                self.assertIsNone(dict(facts.implementation_requests).get((KEY, "implement", 1)))
        f = self.release_forge([])
        for exc in (forge.ForgeError("partial read"), OSError("offline"), ValueError("bad response")):
            f.requests.side_effect = exc
            self.assertIsNone(dict(self.release_gather(f).implementation_requests).get((KEY, "implement", 1)))

    def test_branch_provenance_and_safe_legacy_fallback(self):
        self.released_segment(attempt=2)
        f = self.release_forge([self.released_request()])
        for data, expected in (({}, None), ({"work_branch": "  "}, None),
                               ({"work_branch": "work/issue-1"}, True),
                               ({"work_branch": "other", "runs": {"implement-2": {"branch": "work/issue-1"}}}, True),
                               ({"work_branch": "work/issue-1", "runs": {"implement-2": {"branch": 42}}}, None)):
            with self.subTest(data=data):
                self.db.set_resume(KEY, data)
                self.assertIs(dict(self.release_gather(f).implementation_requests).get((KEY, "implement", 2)), expected)
        self.db.record_segment(KEY, "implement", 3, started_at=NOW.isoformat(), ended_at=NOW.isoformat(), outcome="failed")
        f.requests.reset_mock()
        self.assertEqual(self.release_gather(f).implementation_requests, ())
        f.requests.assert_not_called()

    def test_no_request_read_without_an_eligible_release(self):
        self.released_segment()
        for observations in ((), ((KEY, None),), ((KEY, True),)):
            with self.subTest(observations=observations):
                f = self.release_forge([])
                self.assertEqual(self.release_gather(f, observations).implementation_requests, ())
                f.requests.assert_not_called()
        for fields, data in (({"human_review": True}, {}), ({"stage": "review"}, {}),
                              ({"forge_state": "closed"}, {}), ({}, {"suite_hold": {}})):
            self.db.upsert_item(KEY, stage="implement", forge_state="open", human_review=False)
            self.db.upsert_item(KEY, **fields)
            self.db.set_resume(KEY, data)
            f = self.release_forge([])
            self.assertEqual(self.release_gather(f).implementation_requests, ())
            f.requests.assert_not_called()

    def test_multiple_releases_share_one_request_read_and_isolate_unknown_branch(self):
        self.released_segment()
        self.released_segment(OTHER, branch="work/issue-2")
        requests = [self.released_request(), self.released_request(branch="work/issue-2", closes=("#2",), number=11)]
        f = self.release_forge(requests)
        observations = ((KEY, False), (OTHER, False))
        facts = self.release_gather(f, observations)
        self.assertEqual(dict(facts.implementation_requests), {(KEY, "implement", 1): True, (OTHER, "implement", 1): True})
        f.requests.assert_called_once_with()
        self.assertEqual(f.closing_request.call_count, 2)
        for call in f.closing_request.call_args_list:
            self.assertIs(call.args[1], requests)
        self.db.set_resume(KEY, {})
        facts = self.release_gather(f, observations)
        self.assertIsNone(dict(facts.implementation_requests).get((KEY, "implement", 1)))
        self.assertTrue(dict(facts.implementation_requests)[(OTHER, "implement", 1)])

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
        self.db.end_segment(KEY, "implement", 1, NOW.isoformat(), "done")
        self.db.record_segment(KEY, "implement", 2, started_at=NOW.isoformat(), ended_at=NOW.isoformat(), outcome="done")
        self.db.record_segment(KEY, "review", 1, started_at=NOW.isoformat())
        self.db.add_report(KEY, "implement", 2, "sess", "done", "ok", [])
        self.db.set_resume(KEY, {"runs": {"review-1": {"session_id": "sess"}}})
        self.db.add_report(KEY, "review", 1, "sess", "blocked", "stuck", [])
        self.assertEqual([(r.stage, r.attempt) for r in self.gather().reports][-1], ("review", 1))

    def test_a_report_written_while_liveness_is_read_is_seen(self):
        def appear(*args, **kwargs):
            self.db.add_report(KEY, "implement", 1, "sess", "done", "ok", [])
            return "other"
        with mock.patch.object(engine_facts.process_status, "identity", side_effect=appear):
            self.assertEqual(self.gather().reports[-1].outcome, "done")

    def test_reports_and_suite_counts_use_only_the_recorded_session(self):
        red = {"passed": 0, "failed": 1, "skipped": 0, "xfailed": 0}
        self.db.add_report(KEY, "implement", 1, "sess", "done", "current red", [], suite=red)
        self.db.add_report(KEY, "implement", 1, "foreign", "done", "foreign green", [],
                           suite={**red, "passed": 5, "failed": 0})
        self.db.add_report(KEY, "implement", 1, "foreign", "blocked", "foreign stop", [])
        facts = self.gather()
        self.assertEqual([r.outcome for r in facts.reports], ["blocked", "done"])
        self.assertEqual(dict(facts.suites)[(KEY, "implement", 1)], (red, "current red"))

    def test_a_worker_with_an_exit_record_is_left_to_collect(self):
        run = self.root / "run"
        run.mkdir()
        (run / "exit.json").write_text("{}")
        data = self.db.resume(KEY)
        data["runs"]["implement-1"]["run_dir"] = str(run)
        self.db.set_resume(KEY, data)
        with mock.patch.object(engine_facts.process_status, "identity", return_value="other"):
            self.assertEqual(self.gather().alive, ())

    def test_an_exit_appearing_during_process_probes_is_left_to_collect(self):
        run = self.root / "run"
        run.mkdir()
        data = self.db.resume(KEY)
        data["runs"]["implement-1"]["run_dir"] = str(run)
        self.db.set_resume(KEY, data)
        def appear(*args, **kwargs):
            (run / "exit.json").write_text("{}")
            return None
        with mock.patch.object(engine_facts.process_status, "identity", side_effect=appear):
            self.assertEqual(self.gather().alive, ())

    def test_missing_or_root_level_recorded_trees_contribute_unknown_evidence(self):
        for trees in (self.trees, self.root):
            with self.subTest(trees=trees), mock.patch.object(engine_facts.process_status, "identity", return_value=None):
                facts = engine_facts.gather(forge=SimpleNamespace(blocked_by=lambda: {}), store=self.db,
                                            root=self.root, trees=trees, now=NOW)
                self.assertEqual(facts.changed, ((KEY, None),))
                self.assertEqual(facts.progress, ((KEY, None),))

    def test_recorded_trees_are_read_even_when_discovery_omits_them(self):
        tree = self.root / "issue-1"
        tree.mkdir()
        data = self.db.resume(KEY)
        data["runs"]["implement-1"]["tree"] = str(tree)
        self.db.set_resume(KEY, data)
        for trees in (self.trees, self.root):
            with self.subTest(trees=trees), \
                 mock.patch.object(engine_facts.git_trees, "discover", return_value=[]), \
                 mock.patch.object(engine_facts, "_tree_facts", return_value=(TreeState("work", 0, 0, 0), True, NOW)) as read:
                facts = engine_facts.gather(forge=SimpleNamespace(blocked_by=lambda: {}), store=self.db,
                                            root=self.root, trees=trees, now=NOW)
                read.assert_called_once_with(tree)
                self.assertEqual(facts.changed, ((KEY, False),))
                self.assertEqual(facts.progress, ((KEY, NOW),))
                self.assertEqual(facts.on_main, ((KEY, True),))

    def test_recorded_tree_outside_the_workspace_is_unknown_without_reading_git(self):
        data = self.db.resume(KEY)
        data["runs"]["implement-1"]["tree"] = str(self.root.parent / "other-workspace")
        self.db.set_resume(KEY, data)
        with mock.patch.object(engine_facts, "_tree_facts") as read:
            self.assertEqual(self.gather().changed, ((KEY, None),))
        read.assert_not_called()

    def test_symlink_loop_is_unknown_and_a_valid_symlink_is_resolved_before_reading(self):
        tree = self.trees / "loop"
        tree.symlink_to(tree)
        data = self.db.resume(KEY)
        data["runs"]["implement-1"]["tree"] = str(tree)
        self.db.set_resume(KEY, data)
        with mock.patch.object(engine_facts, "_tree_facts") as read:
            self.assertEqual(self.gather().changed, ((KEY, None),))
            read.assert_not_called()
        tree.unlink()
        target = self.trees / "real"
        target.mkdir()
        tree.symlink_to(target)
        with mock.patch.object(engine_facts, "_tree_facts", return_value=(None, None, None)) as read:
            self.gather()
            read.assert_called_once_with(target.resolve())

    def test_missing_baseline_is_distinct_from_an_unreadable_tree_and_recovers_durable_head(self):
        tree = self.trees / "review"
        tree.mkdir()
        run = self.root / "run"
        run.mkdir()
        self.db.set_ownership(KEY, "src/a.py", "read")
        data = self.db.resume(KEY)
        data["runs"]["implement-1"].update(tree=str(tree), run_dir=str(run))
        self.db.set_resume(KEY, data)
        with mock.patch.object(engine_facts, "_tree_facts", return_value=(TreeState("HEAD", 0, 2, 0), False, NOW)), \
             mock.patch.object(engine_facts.git_trees, "git", return_value=(0, "launch-head")):
            facts = self.gather(SimpleNamespace(blocked_by=lambda: {}))
            self.assertNotIn(KEY, facts.unreadable)
            self.assertEqual(dict(facts.changed)[KEY], None)
            (run / "starting_head").write_text("launch-head\n")
            self.assertEqual(dict(self.gather().changed)[KEY], False)

    def test_invalid_baseline_evidence_remains_unknown_without_aborting_gather(self):
        tree = self.trees / "review"
        tree.mkdir()
        run = self.root / "run"
        run.mkdir()
        (run / "starting_head").write_bytes(b"\xff")
        self.db.set_ownership(KEY, "src/a.py", "read")
        for run_dir in (str(run), "bad\x00path"):
            data = self.db.resume(KEY)
            data["runs"]["implement-1"].update(tree=str(tree), run_dir=run_dir)
            self.db.set_resume(KEY, data)
            with self.subTest(run_dir=run_dir), \
                 mock.patch.object(engine_facts, "_alive", return_value=False), \
                 mock.patch.object(engine_facts, "_tree_facts", return_value=(TreeState("HEAD", 0, 2, 0), False, NOW)):
                facts = self.gather(SimpleNamespace(blocked_by=lambda: {}))
                self.assertIsNone(dict(facts.changed)[KEY])
                self.assertNotIn(KEY, facts.unreadable)

    def test_read_only_changes_are_relative_to_starting_head_in_a_real_detached_tree(self):
        from evals import git_taint
        cache = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(git_taint.private_git_views(cache))
        tree = self.trees / "review"
        tree.mkdir()
        env = {**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull}
        def git(*args):
            return subprocess.run(["git", "-C", str(tree), *args], env=env, check=True,
                                  text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout.strip()
        git("init", "-b", "main")
        git("config", "user.name", "Example")
        git("config", "user.email", "example@example.test")
        (tree / "file").write_text("base")
        git("add", ".")
        git("commit", "-m", "base")
        git("update-ref", "refs/remotes/origin/main", git("rev-parse", "HEAD"))
        git("checkout", "-b", "feature")
        (tree / "file").write_text("feature")
        git("commit", "-am", "feature")
        head = git("rev-parse", "HEAD")
        git("update-ref", "refs/remotes/origin/feature", head)
        git("checkout", "--detach")
        self.db.set_ownership(KEY, "src/a.py", "read")
        data = self.db.resume(KEY)
        data["runs"]["implement-1"].update(tree=str(tree), starting_head=head)
        self.db.set_resume(KEY, data)
        facts = self.gather(SimpleNamespace(blocked_by=lambda: {}))
        self.assertEqual(dict(facts.changed)[KEY], False)
        self.assertGreater(dict(facts.trees)[KEY].off_origin, 0)
        self.assertEqual(dict(facts.trees)[KEY].unpushed, 0)
        (tree / "file").write_text("changed during review")
        git("commit", "-am", "review changed a file")
        self.assertEqual(dict(self.gather().changed)[KEY], True)
        self.assertEqual(dict(self.gather().trees)[KEY].unpushed, 1)
        git("reset", "--hard", head)
        (tree / "untracked").write_text("left behind")
        self.assertEqual(dict(self.gather().changed)[KEY], True)

    def test_late_reports_from_an_older_run_do_not_replace_the_latest_stop(self):
        self.db.end_segment(KEY, "implement", 1, NOW.isoformat(), "done")
        self.db.record_segment(KEY, "review", 1, started_at=NOW.isoformat())
        self.db.set_resume(KEY, {"runs": {"review-1": {"session_id": "new-session"}}})
        self.db.add_report(KEY, "review", 1, "new-session", "blocked", "stuck", [])
        self.db.add_report(KEY, "implement", 1, "sess", "done", "late", [])
        self.assertEqual([(r.stage, r.attempt) for r in self.gather().reports], [("review", 1)])

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

    def test_suite_on_main_requires_passing_ci_and_the_worker_head_in_that_main_head(self):
        tree = self.trees / "issue-1"
        tree.mkdir()
        data = self.db.resume(KEY)
        data["runs"]["implement-1"]["tree"] = str(tree)
        self.db.set_resume(KEY, data)
        for status, code, expected in (("passed", 0, True), ("failed", 0, False), ("none", 0, False),
                                        ("passed", 1, None), ("passed", 128, None)):
            with self.subTest(status=status, code=code), \
                 mock.patch.object(engine_facts.git_trees, "discover", return_value=[tree]), \
                 mock.patch.object(engine_facts, "_tree_facts", return_value=(TreeState("work", 0, 0, 0), True, NOW)), \
                 mock.patch.object(engine_facts.git_trees, "git", return_value=(code, "")) as git:
                source = SimpleNamespace(blocked_by=lambda: {}, branch_head=lambda ref: forge.Pipeline(1, ref, "main-sha", status, ""))
                self.assertEqual(dict(self.gather(source).suite_on_main)[KEY], expected)
                git.assert_called_once_with(["merge-base", "--is-ancestor", "HEAD", "main-sha"], tree)

    def test_finished_run_tree_evidence_remains_available_for_read_only_enforcement(self):
        self.db.set_ownership(KEY, "src/a.py", "read")
        self.db.end_segment(KEY, "implement", 1, NOW.isoformat(), "done")
        self.assertIn(KEY, self.gather().read_only)
        self.assertEqual(self.gather().changed, ((KEY, None),))

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

    def test_a_foreign_github_blocker_keeps_its_repository(self):
        from evals.test_forge import FakeGh, gh_issue
        issue = gh_issue(1, "Blocked")
        issue["blockedBy"] = {"nodes": [{"number": 2, "state": "OPEN",
                                             "repository": {"nameWithOwner": "elsewhere/lib"}}], "totalCount": 1}
        source = forge.Forge(backlog.GitHubBacklog("example/project"), gh=FakeGh(issues=[issue]))
        self.assertEqual(source.blocked_by(), {KEY: ("github:elsewhere/lib#2",)})

    def test_a_foreign_gitlab_blocker_keeps_its_project(self):
        source = forge.Forge(backlog.Backlog("https://gitlab.example.test", "example/project"), token="t")
        link = {"iid": 2, "link_type": "is_blocked_by", "state": "opened",
                "web_url": "https://gitlab.example.test/elsewhere/lib/-/issues/2"}
        with mock.patch.object(forge.Forge, "issues", return_value=SimpleNamespace(ok=True, issues=[SimpleNamespace(iid=1)])), \
             mock.patch.object(backlog, "_get", return_value=([link], {}, "")):
            self.assertEqual(source.blocked_by(), {"gitlab:gitlab.example.test/example/project#1":
                                                   ("gitlab:gitlab.example.test/elsewhere/lib#2",)})

