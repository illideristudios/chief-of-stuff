"""A planned merge verifies the forge again and logs success or refusal on its issue."""

from __future__ import annotations

import sys
import tempfile
import unittest
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "evals"))
sys.path.insert(0, str(ROOT / "scripts"))

import backlog  # noqa: E402
import engine  # noqa: E402
import engine_merge  # noqa: E402
import forge  # noqa: E402
import policy  # noqa: E402
import reconcile  # noqa: E402
import store  # noqa: E402
import test_forge as tf  # noqa: E402
import test_merge_approved as tma  # noqa: E402
import test_merge_ready as tmr  # noqa: E402
from evals import test_engine as te  # noqa: E402
import test_reconcile as tr  # noqa: E402


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
        self.p = tr.policies()
        self.ready()

    def ready(self):
        self.db.upsert_item(self.key, stage="merge", review_state="approved", merge_state="ready")
        self.db.record_segment(self.key, "review", 1,
                               (datetime.fromisoformat(self.at) - timedelta(minutes=2)).isoformat(),
                               self.at, "done")
        te.review_report(self.db, self.key)

    def planned(self):
        item = next(i for i in self.db.items() if i.issue_key == self.key)
        actions = reconcile.plan([item], policies=self.p, free_slots=0, now=datetime.fromisoformat(self.at),
                                 **engine._planning_inputs(self.db, [item]))
        self.assertEqual(actions, [reconcile.Merge(self.key)])
        self.action = actions[0]

    def refused(self, reason):
        with mock.patch.object(forge.Forge, "merge", return_value="merged unexpectedly") as merge:
            self.assertFalse(self.execute())
        merge.assert_not_called()
        self.assertIn("merge refused", self.message())
        self.assertIn(reason, self.db.log(self.key)[0].message)

    def test_review_withdrawn_after_planning_refuses_without_a_merge_call(self):
        self.planned()
        segment, = self.db.segments(self.key)
        self.db.record_segment(self.key, "review", 1, segment.started_at, self.at, "failed")
        self.refused("review")

    def test_skipped_review_hook_refuses_even_with_a_green_approved_request(self):
        with self.db.session() as session:
            segment, = session.query(store.Segment).all()
            session.delete(segment)
        self.refused("review")

    def test_architect_required_by_policy_after_planning_refuses_when_absent(self):
        self.planned()
        self.p.stages["review"] = policy.Place("review", "critic", {"architect": True})
        self.refused("architect")

    def test_required_architect_and_answered_decision_allow_merge(self):
        self.p.stages["review"] = policy.Place("review", "critic", {"architect": True})
        self.db.record_segment(self.key, "architect", 1, self.at, self.at, "done")
        self.db.add_decision(self.key, "design", "Split the parser?", answer="yes", decided_by="trio")
        self.planned()
        self.assertTrue(self.execute())
        self.assertEqual(len(self.gh.merges), 1)
        self.assertIn("merged", self.message())

    def test_a_successful_merge_records_its_commit_structurally(self):
        # §13 (c): a non-Claude run's check excuses only commits the engine recorded merging, never log prose.
        self.planned()
        with mock.patch.object(forge.Forge, "merge_commit", return_value="c" * 40):
            self.assertTrue(self.execute())
        merges = self.db.resume(self.key)["merges"]
        self.assertEqual([(m["sha"], m["at"]) for m in merges], [("c" * 40, self.at)])
        self.assertIsInstance(merges[0]["request"], int)
        self.assertIn("review-1", self.db.resume(self.key)["runs"])

    def test_room_policy_changed_after_planning_is_rechecked(self):
        self.db.set_resume(self.key, {**self.db.resume(self.key), "room": "lab"})
        self.planned()
        self.p.rooms["lab"] = policy.Place("lab", policy={"architect": True})
        self.refused("architect")

    def test_a_decision_pending_after_planning_refuses_without_a_merge_call(self):
        self.planned()
        self.db.add_decision(self.key, "design", "Split the parser?")
        self.refused("decision")

    def test_changed_review_severity_policy_is_rechecked(self):
        self.p.stages["review"] = policy.Place("review", "critic", {"blocking_severity": "important"})
        te.review_report(self.db, self.key, counts={"minor": 1})
        self.planned()
        self.p.stages["review"] = policy.Place("review", "critic", {"blocking_severity": "minor"})
        self.refused("blocking review findings")

    def test_findings_missing_at_execution_are_not_assumed_clean(self):
        self.planned()
        self.db.set_resume(self.key, {})
        self.refused("review findings are unknown")

    def test_new_blocking_findings_after_planning_refuse_merge(self):
        self.planned()
        te.review_report(self.db, self.key, counts={"important": 1})
        self.refused("blocking review findings")

    def test_an_expired_merge_approval_is_rechecked_at_execution_time(self):
        now = datetime.fromisoformat(self.at)
        self.p.stages["review"] = policy.Place("review", "critic", {
            "zach_gates": ("merge",), "approvals": (policy.Approval("merge", now + timedelta(seconds=1)),)})
        self.planned()
        self.db.clock = lambda: now + timedelta(seconds=1)  # the pass's `at` is still the planning time
        self.refused("gate requires Zach's approval")

    def test_an_issue_closed_after_planning_refuses(self):
        self.planned()
        self.gh.issues[3]["state"] = "CLOSED"
        self.refused("no longer open")

    def test_an_issue_moved_off_its_stage_mark_after_planning_refuses(self):
        board = tf.Kanban(stages=("stage: doing", "stage: merge"), human_review_label="needs human",
                          stage_map={"implement": 0, "review": 0, "merge": 1})
        self.forge = forge.Forge(tf.GH, kanban=board, gh=self.gh)
        self.planned()
        self.gh.issues[3]["labels"] = [{"name": "stage: doing"}]
        self.refused("dispatch required")

    def test_a_stage_label_shared_with_an_earlier_stage_keeps_the_stored_stage(self):
        board = tf.Kanban(stages=("stage: doing", "stage: review"), human_review_label="needs human",
                          stage_map={"implement": 0, "review": 1, "merge": 0})
        self.forge = forge.Forge(tf.GH, kanban=board, gh=self.gh)
        self.gh.issues[3]["labels"] = [{"name": "stage: doing"}]
        self.planned()
        self.assertTrue(self.execute())
        self.assertEqual(len(self.gh.merges), 1)

    def at_unlabelled_merge_stage(self, stage="merge"):
        board = tf.Kanban(stages=("stage: doing", "stage: review"), human_review_label="needs human",
                          stage_map={"implement": 0, "review": 1}, terminal_stages=("merge",))
        self.forge = forge.Forge(tf.GH, kanban=board, gh=self.gh)
        # Terminal stages leave a literal mark (the stage name); a blank stage has none.
        self.gh.issues[3]["labels"] = [{"name": "bug"}] + ([{"name": stage}] if stage else [])
        self.db.upsert_item(self.key, stage=stage)
        if not stage:
            self.p.workflows["standard"] = policy.Workflow("standard", "merge")
            self.forge = replace(self.forge, approver="robin")
        self.planned()

    def test_a_terminal_stage_moved_to_implementation_after_planning_refuses(self):
        self.at_unlabelled_merge_stage()
        self.gh.issues[3]["labels"] = [{"name": "stage: doing"}]
        self.refused("dispatch required")

    def test_a_terminal_stage_moved_to_completed_review_after_planning_keeps_merge_when_eligible(self):
        # A different mapped mark adopts the forge stage; review + approved + ready is still a Merge.
        self.at_unlabelled_merge_stage()
        self.gh.issues[3]["labels"] = [{"name": "stage: review"}]
        self.assertTrue(self.execute())
        self.assertEqual(len(self.gh.merges), 1)

    def test_a_blank_merge_stage_with_a_stage_label_added_after_planning_refuses(self):
        self.at_unlabelled_merge_stage("")
        self.gh.issues[3]["labels"] = [{"name": "stage: review"}]
        self.refused("left its stage")

    def test_a_terminal_stage_moved_during_the_policy_check_refuses(self):
        self.at_unlabelled_merge_stage()
        with self.during_policy_check(lambda: self.gh.issues[3].update(labels=[{"name": "stage: review"}])):
            self.assertFalse(self.execute())
        self.assertEqual(self.gh.merges, [])
        self.assertIn("left its stage", self.message())

    def test_a_blank_merge_stage_moved_during_the_policy_check_refuses(self):
        self.at_unlabelled_merge_stage("")
        with self.during_policy_check(lambda: self.gh.issues[3].update(labels=[{"name": "stage: doing"}])):
            self.assertFalse(self.execute())
        self.assertEqual(self.gh.merges, [])
        self.assertIn("left its stage", self.message())

    def test_an_unchanged_terminal_stage_with_unrelated_labels_merges(self):
        self.at_unlabelled_merge_stage()
        self.assertTrue(self.execute())
        self.assertEqual(len(self.gh.merges), 1)

    def test_an_unchanged_blank_merge_stage_with_unrelated_labels_merges(self):
        self.at_unlabelled_merge_stage("")
        self.assertTrue(self.execute())
        self.assertEqual(len(self.gh.merges), 1)

    def during_policy_check(self, change):
        real = reconcile.plan

        def plan(*args, **kwargs):
            found = real(*args, **kwargs)
            change()  # the forge changes after the issue read, before the merge's final read
            return found
        return mock.patch.object(reconcile, "plan", plan)

    def test_an_issue_closed_during_the_policy_check_refuses(self):
        self.planned()
        with self.during_policy_check(lambda: self.gh.issues[3].update(state="CLOSED")):
            self.assertFalse(self.execute())
        self.assertEqual(self.gh.merges, [])
        self.assertIn("no longer open", self.message())

    def test_an_issue_moved_off_its_stage_during_the_policy_check_refuses(self):
        board = tf.Kanban(stages=("stage: doing", "stage: merge"), human_review_label="needs human",
                          stage_map={"implement": 0, "review": 0, "merge": 1})
        self.forge = forge.Forge(tf.GH, kanban=board, gh=self.gh)
        self.gh.issues[3]["labels"] = [{"name": "stage: merge"}]
        self.planned()
        with self.during_policy_check(lambda: self.gh.issues[3].update(labels=[{"name": "stage: doing"}])):
            self.assertFalse(self.execute())
        self.assertEqual(self.gh.merges, [])
        self.assertIn("left its stage", self.message())

    HOLD = [{"name": "stage: merge"}, {"name": "needs human"}]

    def at_merge_stage(self):
        board = tf.Kanban(stages=("stage: doing", "stage: merge"), human_review_label="needs human",
                          stage_map={"implement": 0, "review": 0, "merge": 1})
        self.forge = forge.Forge(tf.GH, kanban=board, gh=self.gh)
        self.gh.issues[3]["labels"] = [{"name": "stage: merge"}]
        self.planned()

    def test_a_forge_human_review_hold_added_after_planning_refuses(self):
        self.at_merge_stage()
        self.gh.issues[3]["labels"] = self.HOLD
        self.refused("human review")

    def test_a_forge_human_review_hold_added_during_the_policy_check_refuses(self):
        self.at_merge_stage()
        with self.during_policy_check(lambda: self.gh.issues[3].update(labels=self.HOLD)):
            self.assertFalse(self.execute())
        self.assertEqual(self.gh.merges, [])
        self.assertIn("needs input on the forge", self.message())

    def test_a_stale_cached_approval_is_replaced_by_the_fresh_request(self):
        no_review = policy.parse({"default_workflow": "direct", "personas": {}, "stages": {"merge": {}},
                                  "workflows": {"direct": {"start": "merge", "edges": []}}})
        self.p = no_review
        self.refused("review has not run")

    def test_approval_withdrawn_between_the_policy_check_and_the_merge_read_refuses(self):
        self.planned()
        real = forge.Forge.requests
        reads = []

        def requests(forge_self):
            found = real(forge_self)
            reads.append(1)
            # The second read is the merge's final one: the approver has just requested changes.
            return [replace(r, approval="changes_requested") for r in found] if len(reads) > 1 else found

        with mock.patch.object(forge.Forge, "requests", requests):
            self.assertFalse(self.execute())
        self.assertEqual(self.gh.merges, [])
        self.assertIn("approval changed", self.message())

    def test_a_push_between_the_policy_check_and_the_merge_read_refuses(self):
        self.planned()
        real = forge.Forge.requests
        reads = []

        def requests(forge_self):
            found = real(forge_self)
            reads.append(1)
            # The final read sees a new head whose CI is already green: the stored review never covered it.
            return [replace(r, head="f" * 40, pipeline_sha="f" * 40) for r in found] if len(reads) > 1 else found

        with mock.patch.object(forge.Forge, "requests", requests):
            self.assertFalse(self.execute())
        self.assertEqual(self.gh.merges, [])
        self.assertIn("head changed", self.message())

    def test_a_human_review_hold_added_after_planning_refuses(self):
        self.planned()
        self.db.set_human_review(self.key, True)
        self.refused("human review")

    def test_fresh_forge_changes_requested_refuses_after_planning(self):
        self.planned()
        self.forge = replace(self.forge, approver="robin")
        self.gh.prs[0]["reviews"] = [tma.review("robin", "CHANGES_REQUESTED")]
        self.refused("changes requested on the forge")

    def test_issue_type_without_engine_review_requires_current_forge_approval(self):
        self.p.workflows["short"] = policy.Workflow("short", "merge", issue_types=("bug",))
        self.db.set_resume(self.key, {**self.db.resume(self.key), "issue_type": "bug"})
        self.forge = replace(self.forge, approver="robin")
        self.planned()
        self.gh.prs[0]["reviews"] = []
        self.refused("review has not run")

    def execute(self):
        item = next(i for i in self.db.items() if i.issue_key == self.key)
        return engine_merge.execute(self.action, self.forge, self.db, at=self.at, policies=self.p,
                                    inputs=engine._planning_inputs(self.db, [item]),
                                    hold_stages=getattr(self.forge.kanban, "hold_stages", ()))

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
        merge.assert_called_once_with(21, commit=True, issue_key=self.key, approval=mock.ANY, head=tma.HEAD,
                                      stage_mark=None)
        self.assertIn("merge refused", self.message())
        self.assertIn("head changed", self.db.log(self.key)[0].message)

    def test_a_merge_that_is_not_confirmed_is_not_logged_as_a_merge(self):
        with mock.patch.object(forge.Forge, "merge", return_value="queued #21 at abc"):
            self.assertFalse(self.execute())
        self.assertEqual(self.db.log(self.key)[0].kind, "")

    def test_a_queued_github_request_is_logged_once_and_returns_false(self):
        def queued(args, input_text=None):
            if args[:2] == ["pr", "view"]:
                return 0, '{"state": "OPEN"}', ""
            return self.gh(args, input_text)

        self.forge = forge.Forge(tf.GH, gh=queued)
        self.assertFalse(self.execute())
        self.assertEqual(len(self.gh.merges), 1)
        self.assertTrue(self.message().startswith("queued #21 "))

    def test_a_failed_state_read_after_a_successful_merge_is_logged_as_unknown_without_retry(self):
        self.gh.fail = {("pr", "view")}
        self.assertFalse(self.execute())
        self.assertEqual(len(self.gh.merges), 1)
        self.assertEqual(sum(c[:2] == ["pr", "view"] for c in self.gh.all_calls), 1)
        self.assertEqual(self.message(), f"unknown #21 at {tma.HEAD} · pipeline 9021 on {tma.HEAD} · "
                                        "https://github.com/o/app/pull/21: rate limited")

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
        self.ready()
        self.enterContext(mock.patch.object(forge.Forge, "issue", return_value=backlog.Read(
            backlog.Issue(3, "Fix the parser", backlog.OPEN))))  # the request fake serves no issue rows
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
        self.ready()
        self.enterContext(mock.patch.object(forge.Forge, "issue", return_value=backlog.Read(
            backlog.Issue(3, "Fix the parser", backlog.OPEN))))  # the request fake serves no issue rows
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
