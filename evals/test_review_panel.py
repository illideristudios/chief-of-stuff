"""Policy-driven review panels, per-finding burn-down and commit-bound approval."""
import sys
from types import SimpleNamespace
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import policy
import reconcile
import store
from evals.test_engine import KEY, NOW


def finding(title="Fix boundary"):
    return dict(severity="minor", title=title, evidence="Observed failure", suggestion="Fix it")


class ReviewPanelTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        path = Path(self.tmp.name) / "store.db"
        store.migrate(path)
        self.db = store.Store(path)
        self.addCleanup(self.db.engine.dispose)
        self.db.upsert_item(KEY, stage="review", merge_state="ready")
        self.p = policy.parse({
            "default_workflow": "standard",
            "personas": {n: {"role": n, "model": "example"} for n in ("builder", "critic", "security", "architect")},
            "stages": {"implement": {"persona": "builder"}, "fix": {"persona": "builder"},
                       "review": {"persona": "critic", "policy": {"reviewers": ["critic", "security"]}}, "merge": {}},
            "workflows": {"standard": {"start": "implement", "edges": [
                {"from": "implement", "to": "review"}, {"from": "review", "to": "fix"},
                {"from": "fix", "to": "review"}, {"from": "review", "to": "merge"}]}}})

    def segment(self, stage, attempt=1, **kw):
        self.db.record_segment(KEY, stage, attempt, (NOW - timedelta(minutes=2)).isoformat(),
                               NOW.isoformat(), "done", **kw)

    def review(self, attempt, persona, findings=(), head="head", round=1):
        self.segment("review", attempt, persona=persona, reviewed_head=head, review_round=round)
        self.db.add_report(KEY, "review", attempt, f"session-{attempt}", "done", "Reviewed", list(findings))

    def plan(self, head="head", slots=4):
        history = self.db.segments(KEY)
        findings = reconcile.review_findings(self.db.reports(KEY), history,
                                              sessions={s.attempt: f"session-{s.attempt}" for s in history if s.stage == "review"})
        return reconcile.plan(self.db.items(), {KEY: history}, self.p, {}, slots, NOW,
                              {} if findings is None else {KEY: findings}, heads={KEY: head},
                              architect_replies=getattr(self, "replies", {}),
                              consult_requests=getattr(self, "requests", {}))

    def test_launches_each_policy_reviewer_and_respects_slots(self):
        actions = self.plan()
        self.assertEqual([a.persona for a in actions], ["critic", "security"])
        self.assertEqual([a.attempt for a in actions], [1, 2])
        self.assertEqual(sum(isinstance(a, reconcile.Review) for a in self.plan(slots=1)), 1)

    def test_partial_panel_resumes_without_relaunching_completed_persona(self):
        self.review(1, "critic")
        actions = self.plan()
        self.assertEqual([a.persona for a in actions if isinstance(a, reconcile.Review)], ["security"])
        self.assertFalse(any(isinstance(a, reconcile.Merge) for a in actions))

    def test_every_required_review_must_match_current_head(self):
        self.review(1, "critic")
        self.review(2, "security", head="old")
        self.assertFalse(any(isinstance(a, reconcile.Merge) for a in self.plan()))
        self.db.record_segment(KEY, "review", 2, NOW.isoformat(), reviewed_head="head")
        self.assertEqual(self.plan(), [reconcile.Merge(KEY)])
        self.assertFalse(any(isinstance(a, reconcile.Merge) for a in self.plan(head="")))

    def test_reports_aggregate_panel_and_dispatch_each_finding_serially(self):
        self.review(1, "critic", [finding("One"), finding("Two")])
        self.review(2, "security", [finding("Three")])
        titles = []
        for attempt in range(1, 4):
            action, = self.plan()
            self.assertIsInstance(action, reconcile.Dispatch)
            titles.append(action.finding["title"])
            self.segment("fix", attempt, finding_key=action.finding_key)
        self.assertEqual(titles, ["One", "Two", "Three"])
        self.assertTrue(all(isinstance(a, reconcile.Review) for a in self.plan(head="new")))

    def test_failed_or_missing_report_is_unknown(self):
        self.review(1, "critic")
        self.segment("review", 2, persona="security", reviewed_head="head", review_round=1)
        self.assertIsNone(reconcile.review_findings(self.db.reports(KEY), self.db.segments(KEY)))
        self.db.add_report(KEY, "review", 2, "session-2", "failed", "Incomplete", [])
        self.assertIsNone(reconcile.review_findings(self.db.reports(KEY), self.db.segments(KEY)))

    def test_architect_flag_routes_a_consultation(self):
        self.p.stages["review"].policy.update(architect=True, architect_persona="architect")
        self.review(1, "critic", [finding()])
        self.review(2, "security")
        action, = self.plan()
        self.assertIsInstance(action, reconcile.Consult)

    def test_the_reserved_architect_is_never_launched_as_an_engine_worker(self):
        from dataclasses import replace
        self.p.personas["architect"] = replace(self.p.personas["architect"], agent="frank-lloyd-aight")
        self.p.stages["review"].policy["reviewers"] = ("architect",)
        self.assertEqual(self.plan(), [reconcile.Wait(KEY, "architect runs require Zach's request")])

    def test_three_unsuccessful_fix_rounds_ping(self):
        for round in range(1, 5):
            self.review(2 * round - 1, "critic", [finding()], round=round)
            self.review(2 * round, "security", round=round)
            action, = self.plan()
            if round <= 3:
                self.assertIsInstance(action, reconcile.Dispatch)
                self.segment("fix", round, finding_key=action.finding_key)
            else:
                self.assertIsInstance(action, reconcile.Escalate)

    def test_partial_panel_with_findings_launches_the_missing_peer_before_fixing(self):
        self.review(1, "critic", [finding()])
        action, = self.plan()
        self.assertIsInstance(action, reconcile.Review)
        self.assertEqual(action.persona, "security")

    def test_a_running_reviewer_does_not_block_launching_its_missing_peer(self):
        self.db.record_segment(KEY, "review", 1, NOW.isoformat(), persona="critic",
                               reviewed_head="head", review_round=1)
        action, = self.plan()
        self.assertEqual((action.persona, action.attempt, action.review_round), ("security", 2, 1))

    def test_a_failed_peer_retries_without_repeating_the_clean_review(self):
        self.review(1, "critic")
        self.review(2, "security")
        self.db.record_segment(KEY, "review", 2, NOW.isoformat(), outcome="failed")
        action, = self.plan()
        self.assertEqual((action.persona, action.attempt, action.review_round), ("security", 3, 1))

    def test_each_reviewer_uses_its_own_open_finding_threshold(self):
        self.p.personas["critic"].policy["blocking_severity"] = "important"
        self.p.personas["security"].policy["blocking_severity"] = "minor"
        self.review(1, "critic", [finding("Ignored minor")])
        self.review(2, "security", [finding("Security minor")])
        action, = self.plan()
        self.assertEqual(action.finding["title"], "Security minor")

    def test_each_reviewers_architect_policy_can_route_a_consult(self):
        self.p.personas["security"].policy.update(architect=True, architect_persona="architect")
        self.review(1, "critic", [finding()])
        self.review(2, "security")
        action, = self.plan()
        self.assertIsInstance(action, reconcile.Consult)

    def test_room_overrides_the_panel_and_architect_requirement(self):
        self.p.rooms["lab"] = policy.Place("lab", policy={"reviewers": ("security",), "architect": False})
        self.p.stages["review"].policy.update(architect=True, architect_persona="architect")
        self.review(1, "security")
        actions = reconcile.plan(self.db.items(), {KEY: self.db.segments(KEY)}, self.p, {}, 2, NOW,
                                 {KEY: []}, heads={KEY: "head"}, rooms={KEY: "lab"})
        self.assertEqual(actions, [reconcile.Merge(KEY)])

    def test_wrong_session_report_cannot_replace_a_review(self):
        self.review(1, "critic", [finding()])
        self.review(2, "security")
        self.db.add_report(KEY, "review", 1, "unrelated-session", "done", "No findings", [])
        findings = reconcile.review_findings(self.db.reports(KEY), self.db.segments(KEY),
                                              sessions={1: "session-1", 2: "session-2"})
        self.assertEqual(len(findings), 1)
        self.assertIsNone(reconcile.review_findings(self.db.reports(KEY), self.db.segments(KEY), sessions={}))

    def test_a_review_from_before_the_upgrade_is_rerun_under_the_panel_not_trusted_or_dropped(self):
        # test_store pins that a migrated 0003 segment has no persona, head or round.
        self.segment("review", 1)
        self.db.upsert_item(KEY, review_state="changes_requested")
        import engine_facts
        self.db.set_resume(KEY, {"runs": {"review-1": {"session_id": "legacy-session"}}})
        inputs = engine_facts.gather(self.db, self.db.items())
        actions = reconcile.plan(self.db.items(), policies=self.p, free_slots=4, now=NOW,
                                 **{**inputs, "heads": {KEY: "head"}})
        self.assertEqual([(a.persona, a.review_round) for a in actions], [("critic", 1), ("security", 1)])

    def test_an_earlier_failed_peer_with_a_later_clean_peer_is_retried(self):
        self.review(1, "critic")
        self.review(2, "security")
        # Keep the original start: the failed segment must not become the newest, or _item retries it unaided.
        self.db.record_segment(KEY, "review", 1, (NOW - timedelta(minutes=2)).isoformat(), outcome="failed")
        action, = self.plan()
        self.assertEqual((action.persona, action.attempt, action.review_round), ("critic", 3, 1))

    def test_a_failed_review_that_never_pinned_a_head_stays_in_its_round_until_the_retry_limit(self):
        self.review(1, "critic")
        for attempt in range(2, 6):
            self.db.record_segment(KEY, "review", attempt, NOW.isoformat(), NOW.isoformat(), "failed",
                                   persona="security", review_round=1)
        action, = self.plan()
        self.assertEqual(action, reconcile.Escalate(KEY, "retry limit reached for review (security)"))

    def test_merge_with_one_finished_reviewer_launches_only_the_missing_one_in_the_same_round(self):
        self.db.upsert_item(KEY, stage="merge")
        self.review(1, "critic")
        action, = self.plan()
        self.assertEqual((action.persona, action.review_round), ("security", 1))

    def keys(self):
        """The panel's findings, as the request for them lists."""
        return reconcile.review_findings(self.db.reports(KEY), self.db.segments(KEY))

    def consulted(self, *dispositions):
        self.p.stages["review"].policy.update(architect=True, architect_persona="architect")
        self.review(1, "critic", [finding(title) for title, _ in dispositions])
        self.review(2, "security")
        keys = [f["finding_key"] for f in reconcile.review_findings(self.db.reports(KEY), self.db.segments(KEY))]
        self.replies = {KEY: reconcile.ArchitectReply("head", "CHANGES", NOW.isoformat(),
                        dict(zip(keys, [d for _, d in dispositions])))}

    def test_the_architect_decides_which_findings_get_a_fix(self):
        self.consulted(("Keep me", "keep"), ("Fix me", "fix"), ("Not real", "discard"), ("Later", "file"))
        action, = self.plan()
        self.assertEqual((action.stage, action.finding["title"]), ("fix", "Fix me"))
        self.segment("fix", 1, finding_key=action.finding_key, reviewed_head="head")
        self.assertTrue(all(isinstance(a, reconcile.Review) for a in self.plan()))

    def test_only_current_head_approval_allows_merge(self):
        self.consulted(("Keep me", "keep"), ("Later", "file"))
        self.replies[KEY] = reconcile.ArchitectReply("head", "APPROVED", NOW.isoformat(), self.replies[KEY].dispositions)
        self.assertEqual(self.plan(), [reconcile.Merge(KEY)])
        self.replies[KEY] = reconcile.ArchitectReply("old", "APPROVED", NOW.isoformat(), self.replies[KEY].dispositions)
        self.assertIsInstance(self.plan()[0], reconcile.Consult)

    def test_a_reply_that_leaves_a_finding_undisposed_waits(self):
        self.consulted(("Fix me", "fix"))
        self.replies[KEY] = reconcile.ArchitectReply("head", "CHANGES", NOW.isoformat(), {})
        self.requests = {KEY: [reconcile.request_token("head", self.keys())]}
        self.assertIsInstance(self.plan()[0], reconcile.Wait)

    def test_a_fresh_round_at_the_same_head_asks_again_for_its_new_findings(self):
        self.consulted(("One", "fix"))
        asked = reconcile.request_token("head", self.keys())
        action, = self.plan()
        self.segment("fix", 1, finding_key=action.finding_key, reviewed_head="head")
        # The fix pushed nothing, so the next panel reviews the same head and finds something new.
        self.review(3, "critic", [finding("Two")], round=2)
        self.review(4, "security", round=2)
        self.requests = {KEY: [asked]}
        action = self.plan()[0]
        self.assertIsInstance(action, reconcile.Consult)
        self.assertEqual((action.head, [f["title"] for f in action.findings]), ("head", ["Two"]))
        self.requests = {KEY: [asked, reconcile.request_token("head", self.keys())]}
        self.assertIsInstance(self.plan()[0], reconcile.Wait)

    def test_a_rework_stage_whose_head_moved_keeps_its_dispatch(self):
        self.review(1, "critic", [finding()], head="old")
        self.review(2, "security", head="old")
        self.db.upsert_item(KEY, stage="implement")
        actions = self.plan(head="new")
        self.assertFalse(any(isinstance(a, reconcile.Review) for a in actions), actions)

    def test_reviewer_dispositions_cannot_suppress_or_authorize_findings(self):
        for disposition in ("file", "keep", "discard", "fix"):
            with self.subTest(disposition=disposition):
                self.p.stages["review"].policy.update(architect=True, architect_persona="architect")
                self.review(1, "critic", [{**finding(), "disposition": disposition}])
                self.review(2, "security")
                self.assertIsInstance(self.plan()[0], reconcile.Consult)
                raw = reconcile.review_findings(self.db.reports(KEY), self.db.segments(KEY))
                self.assertNotIn("disposition", raw[0])

    def test_a_request_waits_without_consuming_a_slot_or_launching_an_architect(self):
        self.p.stages["review"].policy.update(architect=True, architect_persona="architect")
        self.review(1, "critic", [finding()])
        self.review(2, "security")
        self.requests = {KEY: [reconcile.request_token("head", self.keys())]}
        self.assertIsInstance(self.plan(slots=0)[0], reconcile.Wait)
        self.assertFalse(any(s.stage == "architect" for s in self.db.segments(KEY)))

    def test_a_late_panel_member_gets_its_own_findings_disposed(self):
        self.consulted(("One", "fix"))
        asked = reconcile.request_token("head", self.keys())
        # The clean run of "security" is superseded by a retry that reports a finding the reply never saw.
        self.db.record_segment(KEY, "review", 2, (NOW - timedelta(minutes=2)).isoformat(), outcome="failed")
        self.review(3, "security", [finding("Two")])
        self.requests = {KEY: [asked]}
        action = self.plan()[0]
        self.assertIsInstance(action, reconcile.Consult)
        self.assertEqual([f["title"] for f in action.findings], ["Two"])

    def test_failed_or_quota_fix_does_not_explain_an_external_push(self):
        self.review(1, "critic", [finding("One"), finding("Two")])
        self.review(2, "security")
        action, = self.plan()
        for outcome in ("quota", "failed"):
            with self.subTest(outcome=outcome):
                self.segment("fix", 1, finding_key=action.finding_key, reviewed_head="external")
                self.db.record_segment(KEY, "fix", 1, NOW.isoformat(), outcome=outcome)
                self.assertTrue(all(isinstance(a, reconcile.Review) for a in self.plan(head="external")))
        self.segment("fix", 1, finding_key=action.finding_key, reviewed_head="external")
        self.db.record_segment(KEY, "fix", 1, NOW.isoformat(), outcome="done")
        self.assertEqual(self.plan(head="external")[0].finding["title"], "Two")

    def test_a_gated_fix_is_not_rereviewed_when_the_head_moves(self):
        from dataclasses import replace
        workflow = self.p.workflows["standard"]
        self.p.workflows["standard"] = replace(workflow, edges=tuple(
            replace(edge, gate="plan") if edge.source == "review" and edge.target == "fix" else edge
            for edge in workflow.edges))
        self.p.stages["review"].policy["zach_gates"] = ("plan",)
        self.review(1, "critic", [finding()], head="old")
        self.review(2, "security", head="old")
        self.assertEqual(self.plan(head="new"), [reconcile.Ask(KEY, "plan", "gate requires Zach's approval")])

    def test_a_fix_push_keeps_dispositions_and_asks_approval_for_the_new_head(self):
        self.consulted(("Keep me", "keep"), ("Fix me", "fix"), ("Also", "fix"))
        action, = self.plan()
        self.assertEqual(action.finding["title"], "Fix me")
        self.segment("fix", 1, finding_key=action.finding_key, reviewed_head="produced")
        self.requests = {KEY: [reconcile.request_token("head", [])]}
        nxt = self.plan(head="produced")[0]
        self.assertIsInstance(nxt, reconcile.Consult)
        self.assertEqual((nxt.head, list(nxt.findings)), ("produced", []))
        self.replies[KEY] = reconcile.ArchitectReply("produced", "CHANGES", NOW.isoformat(), self.replies[KEY].dispositions)
        self.requests = {KEY: [reconcile.request_token("head", []), reconcile.request_token("produced", [])]}
        nxt = self.plan(head="produced")[0]
        self.assertEqual((nxt.stage, nxt.finding["title"]), ("fix", "Also"))

    def test_approval_of_an_older_head_requests_only_the_new_head(self):
        self.consulted(("Keep me", "keep"))
        self.replies[KEY] = reconcile.ArchitectReply("old", "APPROVED", NOW.isoformat(), self.replies[KEY].dispositions)
        self.requests = {KEY: [reconcile.request_token("old", [])]}
        action = self.plan()[0]
        self.assertIsInstance(action, reconcile.Consult)
        self.assertEqual((action.head, list(action.findings)), ("head", []))

    def test_an_unreadable_current_head_blocks_a_finding_fix(self):
        self.review(1, "critic", [finding()])
        self.review(2, "security")
        self.assertEqual(self.plan(head=None), [reconcile.Wait(KEY, "change head is unknown")])

    def test_completed_fix_output_head_allows_the_next_finding(self):
        self.review(1, "critic", [finding("One"), finding("Two")])
        self.review(2, "security")
        action, = self.plan()
        self.segment("fix", 1, finding_key=action.finding_key, reviewed_head="produced")
        self.assertEqual(self.plan(head="produced")[0].finding["title"], "Two")
        self.assertTrue(all(isinstance(a, reconcile.Review) for a in self.plan(head="external")))

    def test_identical_prose_at_distinct_locations_has_distinct_finding_keys(self):
        self.review(1, "critic", [dict(finding(), path="a.py", line=1), dict(finding(), path="b.py", line=2)])
        self.review(2, "security")
        first, = self.plan()
        self.segment("fix", 1, finding_key=first.finding_key, reviewed_head="head")
        second, = self.plan()
        self.assertNotEqual(first.finding_key, second.finding_key)
        self.assertEqual(second.finding["path"], "b.py")

    def test_findings_from_a_superseded_head_are_re_reviewed_not_fixed(self):
        self.review(1, "critic", [finding()], head="old")
        self.review(2, "security", head="old")
        actions = self.plan(head="new")
        self.assertEqual([(type(a), a.persona, a.review_round) for a in actions],
                         [(reconcile.Review, "critic", 2), (reconcile.Review, "security", 2)])

    def test_a_head_that_moved_under_a_running_reviewer_does_not_relaunch_that_reviewer(self):
        self.db.record_segment(KEY, "review", 1, NOW.isoformat(), persona="critic", reviewed_head="old", review_round=1)
        action, = self.plan(head="new")
        self.assertEqual((action.persona, action.review_round), ("security", 2))

    def test_finding_keys_follow_the_finding_not_its_position_in_the_report(self):
        a, b = finding("A"), finding("B")
        self.segment("review", 1, persona="critic", reviewed_head="head", review_round=1)
        self.db.add_report(KEY, "review", 1, "session-1", "done", "Reviewed", [a, b])
        first = {f["title"]: f["finding_key"] for f in reconcile.review_findings(self.db.reports(KEY), self.db.segments(KEY))}
        self.db.add_report(KEY, "review", 1, "session-1", "done", "Revised", [b])
        second, = reconcile.review_findings(self.db.reports(KEY), self.db.segments(KEY))
        self.assertEqual(second["finding_key"], first["B"])
        self.assertNotEqual(first["A"], first["B"])


class ArchitectCommentsTest(unittest.TestCase):
    def test_latest_reply_uses_comment_order_for_equal_timestamps(self):
        import engine_facts
        timestamp = NOW.isoformat()
        reply, requested = engine_facts.architect_comments([
            SimpleNamespace(body='Architect review requested: head', time=timestamp),
            SimpleNamespace(body='Architect: APPROVED head', time=timestamp),
            SimpleNamespace(body='Architect: CHANGES head\n```json\n{"dispositions":{"1/1/key":"fix"}}\n```', time=timestamp)])
        self.assertEqual((reply.status, reply.dispositions, requested), ('CHANGES', {'1/1/key': 'fix'}, ['head']))

    def test_malformed_reply_cannot_reuse_old_dispositions(self):
        import engine_facts
        for payload in ('{"dispositions":{"key":"ignore"}}', '{invalid', '[]'):
            with self.subTest(payload=payload):
                reply, _ = engine_facts.architect_comments([
                    SimpleNamespace(body='Architect: APPROVED head\n' + payload, time=NOW.isoformat())])
                self.assertEqual(reply.dispositions, {})

    def test_newer_timestamp_wins_over_source_order_and_untimed_replies_are_ignored(self):
        import engine_facts
        reply, _ = engine_facts.architect_comments([
            SimpleNamespace(body='Architect: CHANGES new', time=NOW.isoformat()),
            SimpleNamespace(body='Architect: APPROVED old', time=(NOW - timedelta(seconds=1)).isoformat()),
            SimpleNamespace(body='Architect: APPROVED forged', time='')])
        self.assertEqual(reply.reviewed_head, 'new')

    def test_dispositions_fold_oldest_first_whatever_the_list_order(self):
        import engine_facts
        body = 'Architect: CHANGES head\n```json\n{"dispositions":{"1/1/a":"%s"}}\n```'
        reply, _ = engine_facts.architect_comments([
            SimpleNamespace(body=body % "keep", time=NOW.isoformat()),
            SimpleNamespace(body=body % "fix", time=(NOW - timedelta(seconds=5)).isoformat())], head="head")
        self.assertEqual(dict(reply.dispositions), {"1/1/a": "keep"})

    def test_a_later_reply_for_another_head_does_not_hide_this_head(self):
        import engine_facts
        reply, _ = engine_facts.architect_comments([
            SimpleNamespace(author="frank", body='Architect: CHANGES head\n```json\n{"dispositions":{"1/1/a":"keep"}}\n```',
                            time=(NOW - timedelta(seconds=1)).isoformat()),
            SimpleNamespace(author="worker", body='Architect: APPROVED other', time=NOW.isoformat())],
            head="head", author="frank")
        self.assertEqual((reply.status, reply.reviewed_head, dict(reply.dispositions)),
                         ("CHANGES", "head", {"1/1/a": "keep"}))
