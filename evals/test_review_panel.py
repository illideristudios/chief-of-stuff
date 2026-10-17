"""Policy-driven review panels, per-finding burn-down and commit-bound approval."""
import sys
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
        findings = reconcile.review_findings(self.db.reports(KEY), history)
        return reconcile.plan(self.db.items(), {KEY: history}, self.p, {}, slots, NOW,
                              {} if findings is None else {KEY: findings}, heads={KEY: head})

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
        self.assertEqual((action.stage, action.persona), ("architect", "architect"))

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
        self.assertEqual((action.stage, action.persona), ("architect", "architect"))

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

    def test_store_upgrade_preserves_review_identity_on_completion(self):
        self.review(1, "critic")
        store.migrate(self.db.engine.url.database)
        self.db.end_segment(KEY, "review", 1, NOW.isoformat(), "done")
        segment, = self.db.segments(KEY)
        self.assertEqual((segment.persona, segment.reviewed_head, segment.review_round), ("critic", "head", 1))
