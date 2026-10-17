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
        asked = self.keys()
        token = reconcile.request_token("head", asked)
        self.requests = {KEY: {token: asked}}
        self.replies = {KEY: reconcile.ArchitectReply("head", "CHANGES", NOW.isoformat(),
                        dict(zip(keys, [d for _, d in dispositions])), token)}

    def test_request_tokens_name_sets_not_order_or_duplicate_reports(self):
        one, two = {"finding_key": "1/1/a"}, {"finding_key": "1/2/b"}
        self.assertEqual(reconcile.request_token("head", [one, two]),
                         reconcile.request_token("head", [two, one, one]))

    def test_the_architect_decides_which_findings_get_a_fix(self):
        self.consulted(("Keep me", "keep"), ("Fix me", "fix"), ("Not real", "discard"), ("Later", "file"))
        action, = self.plan()
        self.assertEqual((action.stage, action.finding["title"]), ("fix", "Fix me"))
        self.segment("fix", 1, finding_key=action.finding_key, reviewed_head="head")
        filed, = self.plan()
        self.assertEqual((filed.stage, filed.finding["title"]), ("fix", "Later"))

    def test_only_current_head_approval_allows_merge(self):
        self.consulted(("Keep me", "keep"), ("Not real", "discard"))
        self.replies[KEY] = reconcile.ArchitectReply("head", "APPROVED", NOW.isoformat(), self.replies[KEY].dispositions)
        self.assertEqual(self.plan(), [reconcile.Merge(KEY)])
        self.replies[KEY] = reconcile.ArchitectReply("old", "APPROVED", NOW.isoformat(), self.replies[KEY].dispositions)
        self.assertIsInstance(self.plan()[0], reconcile.Consult)

    def test_partial_dispositions_plan_the_remaining_fixes_without_another_request(self):
        self.consulted(("One", "keep"), ("Two", "fix"))
        asked = self.keys()
        self.requests = {KEY: {reconcile.request_token("head", asked): asked}}
        first, second = asked
        for disposition, title in (("fix", "One"), ("file", "One"), ("keep", "Two"), ("discard", "Two")):
            with self.subTest(disposition=disposition):
                self.replies[KEY] = reconcile.ArchitectReply("head", "CHANGES", NOW.isoformat(),
                                                           {first["finding_key"]: disposition}, reconcile.request_token("head", asked))
                action, = self.plan()
                self.assertIsInstance(action, reconcile.Dispatch)
                self.assertEqual((action.stage, action.finding["title"]), ("fix", title))
        self.assertEqual(self.requests[KEY], {reconcile.request_token("head", asked): asked})

    def test_historical_dispositions_do_not_answer_new_head_findings(self):
        self.consulted(("Old", "keep"))
        old = self.replies[KEY]
        self.review(3, "critic", [finding("New")], head="new-head", round=2)
        self.review(4, "security", head="new-head", round=2)
        self.replies[KEY] = reconcile.ArchitectReply("new-head", "", "", old.dispositions)
        new_findings = self.keys()
        self.assertEqual(self.plan(head="new-head"), [reconcile.Consult(KEY, "new-head", new_findings)])
        token = reconcile.request_token("new-head", new_findings)
        self.requests[KEY][token] = new_findings
        self.assertEqual(self.plan(head="new-head"), [reconcile.Wait(KEY, "waiting for the architect")])
        self.replies[KEY] = reconcile.ArchitectReply("new-head", "CHANGES", NOW.isoformat(), old.dispositions, token)
        action, = self.plan(head="new-head")
        self.assertEqual((type(action), action.finding_key), (reconcile.Dispatch, new_findings[0]["finding_key"]))

    def test_all_keep_or_discard_requests_approval_once_for_the_same_head(self):
        self.consulted(("One", "keep"), ("Two", "discard"))
        asked = self.keys()
        self.requests = {KEY: {reconcile.request_token("head", asked): asked}}
        self.assertEqual(self.plan(), [reconcile.Consult(KEY, "head", [])])
        self.requests[KEY][reconcile.request_token("head", [])] = []
        self.assertEqual(self.plan(), [reconcile.Wait(KEY, "waiting for the architect")])
        self.replies[KEY] = reconcile.ArchitectReply("head", "APPROVED", NOW.isoformat(), self.replies[KEY].dispositions)
        self.assertEqual(self.plan(), [reconcile.Merge(KEY)])

    def test_head_moving_during_fix_requires_a_new_review_before_merge(self):
        self.review(1, "critic")
        self.review(2, "security")
        self.db.upsert_item(KEY, stage="merge")
        actions = self.plan(head="moved-during-fix")
        self.assertEqual(actions, [reconcile.Review(KEY, 3, "critic", 2), reconcile.Review(KEY, 4, "security", 2)])
        self.review(3, "critic", head="moved-during-fix", round=2)
        self.review(4, "security", head="moved-during-fix", round=2)
        self.assertEqual(self.plan(head="moved-during-fix"), [reconcile.Merge(KEY)])

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
        self.requests = {KEY: {asked: [], reconcile.request_token("head", self.keys()): self.keys()}}
        self.assertIsInstance(self.plan()[0], reconcile.Wait)

    def test_a_rework_stage_whose_head_moved_keeps_its_dispatch(self):
        from dataclasses import replace
        workflow = self.p.workflows["standard"]
        self.p.workflows["standard"] = replace(workflow, edges=tuple(
            replace(e, target="implement") if e.source == "review" and e.target == "fix" else e
            for e in workflow.edges))
        self.review(1, "critic", [finding()], head="old")
        self.review(2, "security", head="old")
        self.db.upsert_item(KEY, stage="implement")
        actions = self.plan(head="new")
        self.assertEqual(actions, [reconcile.Dispatch(KEY, "implement", "builder", 1,
                                                    self.keys()[0]["finding_key"], self.keys()[0])])

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
        nxt, = self.plan(head="produced")
        self.assertEqual((nxt.stage, nxt.finding["title"]), ("fix", "Also"))
        self.segment("fix", 2, finding_key=nxt.finding_key, reviewed_head="produced")
        self.assertTrue(all(isinstance(a, reconcile.Review) for a in self.plan(head="produced")))
        self.review(3, "critic", head="produced", round=2)
        self.review(4, "security", head="produced", round=2)
        for attempt in (3, 4):
            self.db.record_segment(KEY, "review", attempt, NOW.isoformat())
        self.assertEqual(self.plan(head="produced"), [reconcile.Consult(KEY, "produced", [])])

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
    def test_only_requested_sha_prefixes_and_the_architect_author_are_parsed(self):
        import engine_facts
        head = "abcdef0123456789abcdef0123456789abcdef01"
        other = "1234567890abcdef1234567890abcdef12345678"
        finding = {"finding_key": "1/1/a"}
        requests = {reconcile.request_token(head, [finding]): [finding], reconcile.request_token(other, []): []}
        body = 'Architect: CHANGES %s\n{"dispositions":{"1/1/a":"keep","unasked":"fix"}}'
        comments = [SimpleNamespace(author="frank", body=body % head[:7], time=NOW.isoformat())]
        for sha, author in ((head[:6], "frank"), ("bbbbbbb", "frank"), (head + "@bad", "frank"), (head, "worker")):
            comments.append(SimpleNamespace(author=author, body="Architect: APPROVED " + sha,
                                            time=(NOW + timedelta(seconds=1)).isoformat()))
        reply, posted = engine_facts.architect_comments(comments, head=head, author="frank", requests=requests,
                                                       engine_author="engine")
        self.assertEqual((reply.reviewed_head, reply.status, dict(reply.dispositions)),
                         (head, "CHANGES", {"1/1/a": "keep"}))
        self.assertEqual(posted, [])

    def test_requests_require_engine_author_and_local_posting_evidence(self):
        import engine_facts
        head = "a" * 40
        token = reconcile.request_token(head, [])
        def note(author, token):
            return SimpleNamespace(author=author, body="Architect review requested: " + token +
                                   '\n```json\n{"findings": []}\n```', time=NOW.isoformat())
        _, posted = engine_facts.architect_comments([note("worker", token), note("engine", "forged")],
            head=head, author="frank", engine_author="engine", requests={token: []})
        self.assertEqual(posted, [])
        _, posted = engine_facts.architect_comments([note("engine", token)], head=head, author="frank",
                                                   engine_author="engine", requests={token: []})
        self.assertEqual(posted, [token])

    def test_dispositions_fold_by_timestamp_then_source_order_across_requested_heads(self):
        import engine_facts
        head, old = "a" * 40, "b" * 40
        finding = {"finding_key": "1/1/a"}
        requests = {reconcile.request_token(h, [finding]): [finding] for h in (head, old)}
        def note(sha, disposition, when):
            return SimpleNamespace(author="frank", body='Architect: CHANGES ' + sha +
                '\n{"dispositions":{"1/1/a":"' + disposition + '"}}', time=when)
        reply, _ = engine_facts.architect_comments([
            note(head, "keep", NOW.isoformat()), note(old, "fix", (NOW - timedelta(seconds=1)).isoformat()),
            note(head, "discard", NOW.isoformat()), note(head, "fix", "")],
            head=head, author="frank", requests=requests)
        self.assertEqual((reply.status, dict(reply.dispositions)), ("CHANGES", {"1/1/a": "discard"}))
