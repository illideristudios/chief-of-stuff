"""Issue 1177: routine work advances; automated worker safeguards still apply."""
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import dispatch_prompt
import guard_commands
import policy
import reconcile
import settings
import spawn_session
import worker
from types import SimpleNamespace
from unittest import mock
from evals.test_reconcile import Item, Segment, NOW, KEY, policies


class AutonomyTest(unittest.TestCase):
    def plan(self, history=(), *, stage="implement", findings=None, configured=None):
        p = configured or policies()
        return reconcile.plan([Item(stage=stage, merge_state="ready")], {KEY: history}, p, {}, 1, NOW,
                              {KEY: findings or {}})

    def test_legacy_stage_gates_do_not_ask_even_without_standing_approval(self):
        p = policies()
        self.assertEqual(self.plan([Segment("implement")], configured=p), [reconcile.Review(KEY, 1)])
        self.assertEqual(self.plan([Segment("review")], stage="merge", configured=p), [reconcile.Merge(KEY)])

    def test_saved_legacy_gate_decisions_do_not_hold_a_workflow_that_has_removed_its_gates(self):
        import decision_escalation
        row = SimpleNamespace(topic="merge", question="legacy owner approval", answer=None, decided_by="owner",
                              record={"decision": {"gate": True, "options": ["approve", "hold"]}})
        self.assertFalse(decision_escalation.pending_decisions([row], ()))
        self.assertEqual(decision_escalation.owner_waits([row], (), ()), [])

    def test_legacy_policy_gate_names_do_not_refuse_launch_after_edges_lose_their_gates(self):
        import tomllib
        data = tomllib.loads((ROOT / "assets/policy.toml").read_text())
        data.setdefault("policy", {})["zach_gates"] = ["merge"]
        with self.assertWarnsRegex(UserWarning, "zach_gates.*ignored"):
            parsed = policy.parse(data)
        self.assertNotIn("zach_gates", vars(parsed.base))

    def test_retired_settings_warn_once_and_do_not_enter_runtime_configuration(self):
        import tomllib
        data = tomllib.loads((ROOT / "assets/policy.toml").read_text())
        data.setdefault("policy", {})["zach_gates"] = ["retired-stage"]
        with self.assertWarnsRegex(UserWarning, "zach_gates.*ignored") as warning:
            parsed = policy.parse(data)
        self.assertEqual(len(warning.warnings), 1)
        self.assertNotIn("zach_gates", vars(parsed.base))
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = root / "cos.toml"
            for table, key, value in (("workflow", "merge_owner", '"worker"'),
                                      ("kanban", "hold_stages", '["retired-stage"]')):
                text = f'[{table}]\n{key} = {value}\n'
                if table == "kanban":
                    text += 'stages = ["doing"]\nhuman_review_label = "problem"\n[kanban.map]\nimplement = 0\n'
                path.write_text(text)
                with self.assertWarnsRegex(UserWarning, f"{key}.*ignored") as warning:
                    config = settings.load(root, path)
                self.assertEqual(len(warning.warnings), 1)
                self.assertNotIn("merge_owner", vars(config.workflow))
                if config.kanban:
                    self.assertFalse(config.kanban.holds("implement"))

    def test_two_failures_across_stages_park_the_issue(self):
        history = [Segment("implement", outcome="failed"), Segment("review", started_at=NOW-timedelta(minutes=30), ended_at=NOW-timedelta(minutes=20), outcome="failed")]
        action, = self.plan(history)
        self.assertIsInstance(action, reconcile.Escalate)
        self.assertIn("two failures", action.reason)

    def test_one_failure_retries_without_parking(self):
        action, = self.plan([Segment("implement", outcome="failed")])
        self.assertEqual(action, reconcile.Dispatch(KEY, "implement", "builder", 2))

    def test_three_completed_fix_rounds_with_findings_park_before_a_fourth(self):
        history = []
        for i in range(4):
            history.append(Segment("review", i + 1, NOW - timedelta(minutes=20-i*2), NOW - timedelta(minutes=19-i*2)))
            if i < 3:
                history.append(Segment("implement", i + 1, NOW - timedelta(minutes=19-i*2), NOW - timedelta(minutes=18-i*2)))
        action, = self.plan(history, stage="review", findings={"minor": 1})
        self.assertIsInstance(action, reconcile.Escalate)
        self.assertIn("three fix rounds", action.reason)

    def test_legacy_merge_owner_modes_normalize_to_coordinator_without_an_approver(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = root / "cos.toml"
            for mode in (None, "user", "approval", "worker", "coordinator"):
                with self.subTest(mode=mode):
                    path.write_text('[workflow]\n' + (f'merge_owner = "{mode}"\n' if mode else ""))
                    self.assertNotIn("merge_owner", vars(settings.load(root, path).workflow))

    def test_interactive_launches_do_not_start_in_owner_plan_gates(self):
        for runtime, template in spawn_session.TEMPLATES.items():
            with self.subTest(runtime=runtime):
                self.assertNotIn("plan", template)
                self.assertNotIn("read-only", template)

    def test_commit_delivery_keeps_merges_with_the_engine_without_an_owner_wait(self):
        for mode in ("user", "approval", "worker", "coordinator"):
            text = dispatch_prompt.commit_rule(settings.Workflow(), ROOT)
            self.assertIn("engine", text)
            self.assertNotIn("user approves", text)
            self.assertNotIn("user's alone", text)
            self.assertNotIn("asks the user", text)
            self.assertNotIn("resolving is the user's", text)

    def test_retry_moves_to_the_next_agent_after_a_failure(self):
        first = settings.ModelEntry("claude", "first")
        second = settings.ModelEntry("claude", "second")
        import store
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "store.db"
            store.migrate(path)
            db = store.Store(path)
            self.addCleanup(db.engine.dispose)
            db.upsert_item(KEY, stage="implement")
            db.record_segment(KEY, "implement", 1, NOW.isoformat(), NOW.isoformat(), "failed",
                              runtime="claude", model="first")
            chosen = worker._model(db, KEY, "implement", {}, SimpleNamespace(model="default", effort=""),
                                   {"implement": (first, second)}, NOW.isoformat(), attempt=2)
            self.assertEqual(chosen, second)

    def test_merge_approved_lists_all_legacy_modes_without_owner_approval(self):
        import merge_approved
        import io
        from contextlib import redirect_stdout
        request = SimpleNamespace(number=12, verdict="ready")
        for mode in ("user", "approval", "worker", "coordinator"):
            with self.subTest(mode=mode), mock.patch.object(merge_approved, "forge", return_value=(
                    settings.Workflow(), None, True, "example/repo")), \
                    mock.patch("merge_ready.read", return_value=[request]), \
                    mock.patch("merge_ready.line", return_value="ready"), redirect_stdout(io.StringIO()):
                self.assertEqual(merge_approved.main(["--root", "."], gh=lambda *args: (1, "", "unexpected call")), 0)

    def test_worker_guard_keeps_engine_merges_and_stage_writes(self):
        for command in ("git merge topic", "git push origin HEAD:main", "gh pr merge 12", "gh issue close 12"):
            with self.subTest(command=command):
                self.assertIsNotNone(guard_commands.denial_reason(command, ("main", "develop")))

    def test_coordinator_eval_permissions_do_not_impose_the_removed_git_ban(self):
        from evals import run
        self.assertEqual(run.DISALLOWED, [])
        self.assertNotIn("--disallowedTools", run.claude_options({}))

    def test_main_suite_evidence_does_not_wait_for_an_owner_report(self):
        agent = (ROOT / "agents/chief-of-stuff.md").read_text()
        self.assertNotIn("the owner reports the suite result", agent)
        self.assertIn("the coordinator or worker records the suite result", agent)

    def test_no_live_case_grades_a_yes_gate_or_an_allow_ask(self):
        # These two still graded the retired behaviour: no launch before a yes, and a permission block ending "allow?".
        import json
        for name in ("dispatch-names-a-type", "blocked-becomes-one-line"):
            with self.subTest(case=name):
                self.assertTrue(json.loads((ROOT / "evals/cases" / name / "case.json").read_text()).get("superseded_by"))

    def test_obsolete_owner_stop_cases_are_not_selected_for_live_evals(self):
        from evals import run
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = root / "cases" / "legacy-gate"
            path.mkdir(parents=True)
            (path / "case.json").write_text('{"prompt":"wait","graders":[],"superseded_by":"autonomy-interactive-dispatch"}')
            with mock.patch.object(run, "EVALS", root):
                self.assertEqual(run.load_cases([]), [])
