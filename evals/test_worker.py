"""Worktree workers, verified starts and completion from engine-owned evidence."""

import json
import os
import sys
import subprocess
import tempfile
import time
import unittest
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import engine
import claude_adapter
import cleanup
import forge_review
import process_status
import performance
import reconcile
import slots
import store
import supervisor
import worker
from evals.test_engine import KEY, NOW, FakeForge, policies


class WorkerTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.trees = self.root / "trees"
        self.trees.mkdir()
        self.tree = self.trees / "issue-1-implement-1"
        self.tree.mkdir()
        path = self.root / "store.db"
        store.migrate(path)
        self.db = store.Store(path, clock=lambda: NOW)
        self.addCleanup(self.db.engine.dispose)
        self.db.upsert_item(KEY, title="Build a thing", stage="implement")
        self.p = policies()
        self.persona = self.p.personas["builder"]
        self.forge = SimpleNamespace(
            issue=lambda n: SimpleNamespace(ok=True, issue=SimpleNamespace(title="Forge title", body="Issue body")),
            closing_request=lambda key: SimpleNamespace(branch="feature/existing", head="head", number=9),
            request_base=lambda number: "trunk",
            protected_branches=lambda: ("trunk", "main"))
        git = mock.patch.object(worker.make_worktree, "git", return_value=(0, "head"))
        git.start()
        self.addCleanup(git.stop)

    def launch(self, stage="implement", attempt=1):
        return worker.launch(KEY, stage, attempt, None, store=self.db, root=self.root,
                             trees=self.trees, policies=self.p, forge=self.forge, clock=lambda: NOW)

    def start(self, request, run_dir, **kwargs):
        (run_dir / "started.json").write_text(json.dumps({
            "pid": 77, "runner_pid": 78, "session_id": request.session_id}))
        return SimpleNamespace(pid=78, poll=lambda: None)

    def launched(self, stage="implement", attempt=1):
        with mock.patch.object(worker.make_worktree, "build", return_value=SimpleNamespace(path=self.tree)), \
             mock.patch.object(worker.claude_adapter, "start", side_effect=self.start), \
             mock.patch.object(worker.process_status, "identity", return_value="process-start"):
            self.launch(stage, attempt)
        slots.claim(self.trees, self.db, issue_key=KEY, stage=stage, attempt=attempt,
                    max_concurrency=1, clock=lambda: NOW, launch=lambda: ("example", ""))
        return Path(self.db.resume(KEY)["runs"][f"{stage}-{attempt}"]["run_dir"])

    def finish(self, outcome):
        run = self.launched()
        text = "done" if outcome == "done" else "bad" if outcome == "failed" else "429 quota exceeded; resets in 2 hours"
        (run / "stdout").write_text(json.dumps({"is_error": outcome != "done", "result": text}))
        (run / "stderr").write_text("")
        if outcome == "done":
            self.report(run)
        (run / "exit.json").write_text(json.dumps({"exit_code": 0 if outcome == "done" else 1}))
        with mock.patch.object(worker.process_status, "identity", return_value=None):
            worker.collect(self.db, self.root, lambda: NOW + timedelta(minutes=1))
        return self.db.segments(KEY)[0]

    def test_assignment_has_fixed_header_row_issue_job_and_persona_rules(self):
        persona = SimpleNamespace(role="builder", rules=("Tests first.",))
        row = self.db.items()[0]
        prompt = worker.dispatch_prompt.compose_issue(row, "Forge title", "Issue body", "implement", persona)
        self.assertTrue(prompt.startswith(worker.dispatch_prompt.ISSUE_HEADER))
        for text in (KEY, row.title, "Forge title", "Issue body", "implement", "builder", "Tests first.",
                     "commit small and often, never merge"):
            self.assertIn(text, prompt)

    def test_assignment_refuses_peer_row(self):
        self.db.upsert_item(KEY, title="Build (via peer)")
        with self.assertRaisesRegex(ValueError, "via"):
            worker.dispatch_prompt.compose_issue(self.db.items()[0], "title", "body", "implement", self.persona)

    def test_issue_assignments_include_prevalidated_workspace_rules_for_every_stage(self):
        dp = worker.dispatch_prompt
        for stage in ("plan", "implement", "review", "fix"):
            with self.subTest(stage=stage):
                prompt = dp.compose_issue(self.db.items()[0], "title", "body", stage, self.persona,
                                          rules=("Keep tests.", "Keep <your_ref_or_name> literal."))
                self.assertIn("\n\nStanding worker rules:\n\n- Keep tests.\n- Keep <your_ref_or_name> literal.\n\n", prompt)
                self.assertIn("workspace settings (`[workers] rules`)", prompt)
                self.assertLess(prompt.index("- Keep tests."), prompt.index("Issue:"))

    def test_issue_assignment_header_still_bounds_standing_rules(self):
        dp = worker.dispatch_prompt
        with self.assertRaisesRegex(dp.RefusedError, "over the .* cap"):
            dp.compose_issue(self.db.items()[0], "title", "body", "implement", self.persona, rules=("x" * 2000,) * 9)

    def test_large_forge_issues_are_preserved_with_or_without_standing_rules(self):
        dp = worker.dispatch_prompt
        title = "Forge title " + "t" * dp.BODY_CAP
        body = "Issue body " + "b" * dp.BODY_CAP
        for stage in ("plan", "implement", "review", "fix"):
            for rules in ((), ("Keep tests.",)):
                with self.subTest(stage=stage, rules=rules):
                    prompt = dp.compose_issue(self.db.items()[0], title, body, stage, self.persona, rules=rules)
                    self.assertTrue(prompt.endswith(f"\n## Forge issue\n\n{title}\n\n{body}\n"))
                    self.assertEqual("Standing worker rules:" in prompt, bool(rules))

    def test_launch_passes_workspace_rules_to_the_host_assignment(self):
        with mock.patch.object(worker.make_worktree, "build", return_value=SimpleNamespace(path=self.tree)), \
             mock.patch.object(worker.claude_adapter, "start", side_effect=self.start) as start, \
             mock.patch.object(worker.process_status, "identity", return_value="start"):
            worker.launch(KEY, "implement", 1, None, store=self.db, root=self.root,
                          trees=self.trees, policies=self.p, forge=self.forge,
                          config=engine.settings.Settings(workers=engine.settings.Workers(rules=("Keep tests.",))),
                          clock=lambda: NOW)
        self.assertIn("- Keep tests.\n", start.call_args.args[0].prompt)

    def test_launch_uses_pass_snapshot_after_settings_change_on_disk(self):
        (self.root / "CLAUDE.md").write_text(
            "## Coordinator\n- Settings: `settings.toml`\n- Agent: builder task\n")
        path = self.root / "settings.toml"
        path.write_text('[lanes.code]\nstages = ["implement"]\n[workers]\nclaude_as_profile = "secondary"\nrules = ["Keep tests."]\n'
                        '[stages.implement]\nagent = "builder"\nskills = ["original-skill"]\n')
        config = engine.settings.load(self.root, "settings.toml")
        path.write_text('[lanes.code]\nstages = ["implement"]\n[workers]\nclaude_as_profile = "other"\nrules = ["Changed rule."]\n'
                        '[stages.implement]\nagent = "other"\nskills = ["changed-skill"]\n')
        with mock.patch.object(worker.make_worktree, "build", return_value=SimpleNamespace(path=self.tree)) as build, \
             mock.patch.object(worker.claude_adapter, "start", side_effect=self.start) as start, \
             mock.patch.object(worker.process_status, "identity", return_value="start"):
            worker.launch(KEY, "implement", 1, None, store=self.db, root=self.root,
                          trees=self.trees, policies=self.p, forge=self.forge, config=config, clock=lambda: NOW)
        request = start.call_args.args[0]
        self.assertEqual(start.call_args.kwargs["claude_as_profile"], "secondary")
        self.assertEqual(request.agent, "builder")
        self.assertEqual(build.call_args.args[5], "builder")
        self.assertIn("Skills: load and follow original-skill", request.prompt)
        self.assertIn("- Keep tests.\n", request.prompt)
        self.assertNotIn("Changed rule.", request.prompt)
        self.assertNotIn("changed-skill", request.prompt)

    def test_launch_without_a_snapshot_uses_the_configured_profile(self):
        (self.root / "CLAUDE.md").write_text("## Coordinator\n- Settings: `settings.toml`\n")
        (self.root / "settings.toml").write_text('[workers]\nclaude_as_profile = "secondary"\n')
        with mock.patch.object(worker.make_worktree, "build", return_value=SimpleNamespace(path=self.tree)), \
             mock.patch.object(worker.claude_adapter, "start", side_effect=self.start) as start, \
             mock.patch.object(worker.process_status, "identity", return_value="start"):
            self.launch()
        self.assertEqual(start.call_args.kwargs["claude_as_profile"], "secondary")

    def test_launch_explicit_empty_profile_uses_default_login(self):
        config = engine.settings.Settings(workers=engine.settings.Workers(claude_as_profile="secondary"))
        with mock.patch.object(worker.make_worktree, "build", return_value=SimpleNamespace(path=self.tree)), \
             mock.patch.object(worker.claude_adapter, "start", side_effect=self.start) as start, \
             mock.patch.object(worker.process_status, "identity", return_value="start"):
            worker.launch(KEY, "implement", 1, None, store=self.db, root=self.root,
                          trees=self.trees, policies=self.p, forge=self.forge, config=config,
                          claude_as_profile="", clock=lambda: NOW)
        self.assertEqual(start.call_args.kwargs["claude_as_profile"], "")

    def test_implement_assignment_requires_delivery_before_reporting_done(self):
        prompt = worker.dispatch_prompt.compose_issue(self.db.items()[0], "title", "body", "implement", self.persona)
        self.assertIn(worker.dispatch_prompt.COMMITS, prompt)
        self.assertIn("Closes #1", prompt)
        self.assertIn("before reporting done", prompt)
        review = worker.dispatch_prompt.compose_issue(self.db.items()[0], "title", "body", "review", self.persona)
        self.assertNotIn(worker.dispatch_prompt.COMMITS, review)

    def test_fix_assignment_pushes_existing_request_before_reporting_done(self):
        prompt = worker.dispatch_prompt.compose_issue(self.db.items()[0], "title", "body", "fix", self.persona)
        self.assertIn("push the existing request branch", prompt)
        self.assertIn("do not open a new request", prompt)
        self.assertIn("before reporting done", prompt)

    def test_review_assignment_names_code_review_skill_and_report_rule(self):
        prompt = worker.dispatch_prompt.compose_issue(self.db.items()[0], "title", "body", "review", self.persona)
        self.assertIn("Review: run the extras:code-review skill on the request; report findings and do not fix.",
                      prompt)
        self.assertIn(worker.dispatch_prompt.REVIEW_RULE, prompt)
        self.assertIn("review-report.md", prompt)

    def test_launch_implement_and_fix_receive_workspace_architecture(self):
        self.p.stages["fix"] = self.p.stages["implement"]
        (self.root / "CLAUDE.md").write_text(
            "## Architecture\n- Canonical document: `design/ARCHITECTURE.md`; see `other.md`.\n")
        # A worker checkout's declaration must not replace the workspace's document.
        (self.tree / "CLAUDE.md").write_text(
            "## Architecture\n- Canonical document: `wrong.md`\n")
        with mock.patch.object(worker.make_worktree, "build", return_value=SimpleNamespace(path=self.tree)), \
             mock.patch.object(worker.make_worktree, "git", return_value=(0, "work/issue-1-implement-1")), \
             mock.patch.object(worker.claude_adapter, "start", side_effect=self.start) as start, \
             mock.patch.object(worker.process_status, "identity", return_value="start"):
            for stage in ("implement", "fix"):
                with self.subTest(stage=stage):
                    self.launch(stage)
                    prompt = start.call_args.args[0].prompt
                    self.assertIn(f"Architecture: {self.root / 'design/ARCHITECTURE.md'}", prompt)
                    self.assertIn("read it before changing code", prompt)
                    self.assertNotIn("wrong.md", prompt)

    def test_assignment_omits_architecture_without_a_canonical_document(self):
        for text in (None, "", "## Coordinator\n- Canonical document: `wrong.md`\n",
                     "## Architecture\n- Architecture directory: `design/`\n",
                     "## Architecture\n- Canonical document: not a backticked path\n"):
            with self.subTest(text=text):
                if text is not None:
                    (self.root / "CLAUDE.md").write_text(text)
                prompt = worker.dispatch_prompt.compose_issue(self.db.items()[0], "title", "body", "implement",
                                                             self.persona, root=self.root)
                self.assertNotIn("Architecture:", prompt)

    def test_runner_spawn_failure_allows_retry_on_the_same_tree_and_attempt(self):
        with mock.patch.object(worker.make_worktree, "build", return_value=SimpleNamespace(path=self.tree)) as build, \
             mock.patch.object(claude_adapter, "command", return_value=["/fake"]), \
             mock.patch.object(claude_adapter.subprocess, "Popen", side_effect=OSError("spawn failed")):
            with self.assertRaisesRegex(worker.StartFailed, "spawn failed") as caught:
                self.launch()
        self.assertIsInstance(caught.exception.__cause__, OSError)
        self.assertEqual(caught.exception.chosen.runtime, "claude")
        self.assertEqual(self.db.segments(), [])
        self.assertFalse(self.db.resume(KEY).get("runs"))
        with mock.patch.object(worker.make_worktree, "build") as rebuild, \
             mock.patch.object(worker.make_worktree, "git", return_value=(0, "work/issue-1-implement-1")), \
             mock.patch.object(worker.claude_adapter, "start", side_effect=self.start) as start, \
             mock.patch.object(worker.process_status, "identity", return_value="start"):
            self.launch()
        build.assert_called_once()
        rebuild.assert_not_called()
        self.assertEqual(start.call_args.args[0].cwd, str(self.tree))

    def test_retry_may_cut_a_branch_when_no_earlier_attempt_started_a_worker(self):
        # A transient issue-read failure records attempt 1 as failed before any worktree exists.
        with mock.patch.object(worker.make_worktree, "build", return_value=SimpleNamespace(path=self.tree)) as build, \
             mock.patch.object(worker.claude_adapter, "start", side_effect=self.start), \
             mock.patch.object(worker.process_status, "identity", return_value="start"):
            self.launch(attempt=2)
        build.assert_called_once()

    def test_retry_refuses_a_fresh_branch_after_an_earlier_worker_started(self):
        self.db.set_resume(KEY, {"runs": {"implement-1": {"pid": 1}}})
        with mock.patch.object(worker.make_worktree, "build") as build:
            with self.assertRaisesRegex(RuntimeError, "no existing worktree"):
                self.launch(attempt=2)
        build.assert_not_called()

    def test_verification_failure_records_no_segment_or_run(self):
        with mock.patch.object(worker.make_worktree, "build", return_value=SimpleNamespace(path=self.tree)), \
             mock.patch.object(worker.claude_adapter, "start", side_effect=self.start), \
             mock.patch.object(worker.claude_adapter, "cancel"), \
             mock.patch.object(worker.time, "monotonic", side_effect=[0, 10]), \
             mock.patch.object(worker.process_status, "identity", return_value=None):
            with self.assertRaisesRegex(RuntimeError, "verify"):
                slots.claim(self.trees, self.db, issue_key=KEY, stage="implement", attempt=1,
                            max_concurrency=1, clock=lambda: NOW, launch=self.launch)
        self.assertEqual(self.db.segments(), [])
        self.assertFalse((self.db.resume(KEY) or {}).get("runs"))

    def test_launch_uses_policy_and_engine_run_dir_outside_tree(self):
        run = self.launched()
        record = self.db.resume(KEY)["runs"]["implement-1"]
        self.assertEqual(run, supervisor.state_dir(self.root) / "runs" / "1-implement-1")
        self.assertFalse(run.is_relative_to(self.tree))
        self.assertEqual(record["pid"], 77)
        self.assertEqual(record["identity"], "process-start")
        self.assertEqual(record["started_at"], NOW.isoformat())
        self.assertTrue(record["session_id"])

    def launch_with_settings(self, settings, stage="implement", data=None):
        (self.root / "CLAUDE.md").write_text("## Coordinator\n- Settings: `chief-of-stuff.toml`\n")
        (self.root / "chief-of-stuff.toml").write_text(settings)
        if data is not None:
            self.db.set_resume(KEY, data)
        with mock.patch.object(worker.make_worktree, "build", return_value=SimpleNamespace(path=self.tree)), \
             mock.patch.object(worker.claude_adapter, "start", side_effect=self.start) as start, \
             mock.patch.object(worker.process_status, "identity", return_value="start"):
            used = self.launch(stage)
        return used, start.call_args.args[0]

    def test_launch_takes_the_first_claude_entry_of_the_stage_rotation(self):
        # #851: 1.0 launches Claude only; earlier hosts are skipped and logged until 1.1 (#701).
        used, request = self.launch_with_settings('[models.implement]\nrotation = ["codex:gpt-6.1-sol@high", '
                                                  '"cursor:auto", "agy:gemini-4-argon-high", "claude:sonnet@high"]\n')
        self.assertEqual(used, ("sonnet", "high"))
        self.assertEqual((request.model, request.effort), ("sonnet", "high"))
        self.assertEqual(self.db.resume(KEY)["model"], {"implement": "claude:sonnet@high"})
        logged = " ".join(entry.message for entry in self.db.log(KEY))
        for skipped in ("codex:gpt-6.1-sol@high", "cursor:auto", "agy:gemini-4-argon-high", "#701"):
            self.assertIn(skipped, logged)

    def test_launch_without_a_rotation_uses_the_persona_model(self):
        used, request = self.launch_with_settings("[workers]\nmode = \"one-shot\"\n")
        self.assertEqual(used, (self.persona.model, self.persona.effort or ""))
        self.assertEqual(request.model, self.persona.model)

    def test_a_quota_stop_advances_to_the_next_claude_entry(self):
        used, request = self.launch_with_settings(
            '[models.implement]\nrotation = ["claude:sonnet@high", "codex:x@high", "claude:opus@medium"]\n',
            data={"quota": {"reset": "later", "retry_at": NOW.isoformat()}, "model": {"implement": "claude:sonnet@high"}})
        self.assertEqual(used, ("opus", "medium"))
        self.assertEqual(self.db.resume(KEY)["model"], {"implement": "claude:opus@medium"})

    def host_launched(self, protected=True):
        """#700: a codex start through host_adapter, recorded and claimed like any other."""
        self.forge.default_branch_protected = lambda: protected
        entry = worker.ModelEntry("codex", "gpt-6.1-sol", "high")
        with mock.patch.object(worker, "_model", return_value=entry), \
             mock.patch.object(worker.make_worktree, "build", return_value=SimpleNamespace(path=self.tree)), \
             mock.patch.object(worker.host_adapter, "branch_heads",
                               side_effect=lambda tree, branches: {b: "d" * 40 for b in branches}), \
             mock.patch.object(worker.host_adapter, "start",
                               side_effect=lambda runtime, request, run_dir, env: (self.start(request, run_dir), "")) as host, \
             mock.patch.object(worker.claude_adapter, "start") as claude, \
             mock.patch.object(worker.process_status, "identity", return_value="process-start"):
            used = self.launch()
        slots.claim(self.trees, self.db, issue_key=KEY, stage="implement", attempt=1,
                    max_concurrency=1, clock=lambda: NOW, launch=lambda: used)
        return used, host, claude

    def test_a_non_claude_entry_needs_the_forge_to_protect_the_default_branch(self):
        with self.assertRaisesRegex(RuntimeError, "protects the default branch"):
            self.host_launched(protected=False)
        self.assertFalse((self.db.resume(KEY) or {}).get("runs"))

    def test_a_non_claude_start_goes_through_its_host_and_keeps_the_default_head(self):
        used, host, claude = self.host_launched()
        claude.assert_not_called()
        self.assertEqual(host.call_args.args[0], "codex")
        self.assertEqual(used, ("gpt-6.1-sol", "high", "codex"))
        self.assertEqual(self.db.segments(KEY)[0].runtime, "codex")
        record = self.db.resume(KEY)["runs"]["implement-1"]
        # #860: every protected branch's head, not only the default's.
        self.assertEqual((record["runtime"], record["branch_heads"]), ("codex", {"trunk": "d" * 40, "main": "d" * 40}))

    def test_a_non_claude_entry_needs_the_branch_its_request_targets_protected(self):
        # §13: chief-of-stuff's requests target develop while its default is main; both must be protected.
        self.forge.request_base = lambda number: "release"
        with self.assertRaisesRegex(RuntimeError, "the branch its request targets"):
            self.host_launched()
        self.assertFalse((self.db.resume(KEY) or {}).get("runs"))

    def test_a_non_claude_run_holds_when_the_default_branch_moved_outside_the_engine(self):
        self.host_launched()
        run = Path(self.db.resume(KEY)["runs"]["implement-1"]["run_dir"])
        (run / "stdout").write_text("finished")
        (run / "stderr").write_text("")
        self.report(run)
        (run / "exit.json").write_text(json.dumps({"exit_code": 0}))
        with mock.patch.object(worker.process_status, "identity", return_value=None), \
             mock.patch.object(worker.host_adapter, "cleanup") as cleanup, \
             mock.patch.object(worker.host_adapter, "moved_outside_engine", return_value="trunk moved") as moved:
            worker.collect(self.db, self.root, lambda: NOW + timedelta(minutes=1))
        cleanup.assert_called_once()
        moved.assert_called_once()
        self.assertEqual(self.db.segments(KEY)[0].outcome, "human_review")
        self.assertIn("trunk moved", " ".join(entry.message for entry in self.db.log(KEY)))

    def t3_launched(self, protected=True):
        """#1178: a Claude entry started as a T3 thread, recorded and claimed like any other."""
        self.forge.default_branch_protected = lambda: protected

        def t3_start(request, run_dir, **kwargs):
            (run_dir / "thread.json").write_text(json.dumps({"thread_id": "mcp:t-1",
                                                             "thread_link": "t3code://threads/env-1/mcp:t-1"}))
            return self.start(request, run_dir)
        with mock.patch.object(worker, "_model", return_value=worker.ModelEntry("claude", "sonnet", "high")), \
             mock.patch.object(worker.make_worktree, "build", return_value=SimpleNamespace(path=self.tree)), \
             mock.patch.object(worker.host_adapter, "branch_heads",
                               side_effect=lambda tree, branches: {b: "d" * 40 for b in branches}), \
             mock.patch.object(worker.t3_adapter, "start", side_effect=t3_start) as t3, \
             mock.patch.object(worker.claude_adapter, "start") as claude, \
             mock.patch.object(worker.process_status, "identity", return_value="process-start"):
            used = worker.launch(KEY, "implement", 1, None, store=self.db, root=self.root, trees=self.trees,
                                 policies=self.p, forge=self.forge, clock=lambda: NOW, launcher="t3")
        slots.claim(self.trees, self.db, issue_key=KEY, stage="implement", attempt=1,
                    max_concurrency=1, clock=lambda: NOW, launch=lambda: used)
        return t3, claude

    def test_a_t3_launch_stores_the_thread_id_and_link_in_the_run_record(self):
        t3, claude = self.t3_launched()
        claude.assert_not_called()
        t3.assert_called_once()
        record = self.db.resume(KEY)["runs"]["implement-1"]
        self.assertEqual((record["runtime"], record["launcher"]), ("claude", "t3"))
        self.assertEqual(record["thread_id"], "mcp:t-1")
        self.assertEqual(record["thread_link"], "t3code://threads/env-1/mcp:t-1")
        self.assertEqual(record["branch_heads"], {"trunk": "d" * 40, "main": "d" * 40})  # §13: no guard plugin loads, as for any host
        self.assertEqual(self.db.segments(KEY)[0].runtime, "claude")  # still a claude worker: T3 is the launcher

    def test_a_t3_launch_needs_the_forge_to_protect_the_default_branch(self):
        with self.assertRaisesRegex(RuntimeError, "protects the default branch"):
            self.t3_launched(protected=False)
        self.assertFalse((self.db.resume(KEY) or {}).get("runs"))

    def test_t3_selection_never_silently_launches_an_unsupported_runtime_locally(self):
        self.forge.default_branch_protected = lambda: True
        with mock.patch.object(worker, "_model", return_value=worker.ModelEntry("codex", "gpt-6.1-sol")), \
             mock.patch.object(worker.make_worktree, "build", return_value=SimpleNamespace(path=self.tree)), \
             mock.patch.object(worker.host_adapter, "start") as local:
            with self.assertRaisesRegex(RuntimeError, "T3.*codex"):
                worker.launch(KEY, "implement", 1, None, store=self.db, root=self.root, trees=self.trees,
                              policies=self.p, forge=self.forge, clock=lambda: NOW, launcher="t3")
        local.assert_not_called()

    def test_t3_does_not_ignore_a_configured_account_profile(self):
        self.forge.default_branch_protected = lambda: True
        with mock.patch.object(worker, "_model", return_value=worker.ModelEntry("claude", "sonnet")), \
             mock.patch.object(worker.make_worktree, "build", return_value=SimpleNamespace(path=self.tree)), \
             mock.patch.object(worker.t3_adapter, "start") as start:
            with self.assertRaisesRegex(RuntimeError, "provider instance"):
                worker.launch(KEY, "implement", 1, None, store=self.db, root=self.root, trees=self.trees,
                              policies=self.p, forge=self.forge, clock=lambda: NOW, launcher="t3", claude_as_profile="alternate")
        start.assert_not_called()

    def test_a_t3_rotation_with_only_unsupported_hosts_does_not_invent_a_claude_entry(self):
        (self.root / "CLAUDE.md").write_text("## Coordinator\n- Settings: `chief-of-stuff.toml`\n")
        (self.root / "chief-of-stuff.toml").write_text('[models.implement]\nrotation = ["codex:gpt-6.1-sol@high"]\n')
        self.forge.default_branch_protected = lambda: True
        with mock.patch.object(worker.make_worktree, "build", return_value=SimpleNamespace(path=self.tree)), \
             mock.patch.object(worker.t3_adapter, "available", side_effect=lambda runtime: runtime == "claude"), \
             mock.patch.object(worker, "below_limit", return_value=True), \
             mock.patch.object(worker.t3_adapter, "start") as start:
            with self.assertRaisesRegex(RuntimeError, "T3.*rotation"):
                worker.launch(KEY, "implement", 1, None, store=self.db, root=self.root, trees=self.trees,
                              policies=self.p, forge=self.forge, clock=lambda: NOW, launcher="t3")
        start.assert_not_called()

    def test_dead_t3_runner_is_adopted_without_starting_a_second_worker(self):
        self.t3_launched()
        data = self.db.resume(KEY)
        run = Path(data["runs"]["implement-1"]["run_dir"])
        with mock.patch.object(worker.process_status, "identity", return_value=None), \
             mock.patch.object(worker.t3_adapter, "observe", return_value={"status": "running"}):
            adopted = worker._existing_run(data, run, "implement", 1, self.trees, lambda: NOW)
        self.assertIsNotNone(adopted)

    def test_t3_remote_liveness_wins_over_runner_exit_and_defers_teardown(self):
        self.t3_launched()
        run = Path(self.db.resume(KEY)["runs"]["implement-1"]["run_dir"])
        (run / "exit.json").write_text('{"exit_code": -15}')
        with mock.patch.object(worker.t3_adapter, "refresh", return_value={"status": "running"}), \
             mock.patch.object(worker.t3_adapter, "observe", return_value=worker.t3_adapter.Evidence(alive=False, launcher="t3")), \
             mock.patch.object(worker, "teardown_once") as teardown:
            worker.collect(self.db, self.root, lambda: NOW)
        teardown.assert_not_called()
        self.assertIsNone(self.db.segments(KEY)[0].ended_at)

    def test_incomplete_terminal_t3_evidence_preserves_the_attempt_before_refresh(self):
        self.t3_launched()
        run = Path(self.db.resume(KEY)["runs"]["implement-1"]["run_dir"])
        (run / "t3.json").write_text(json.dumps({"cli": "/t3", "home": ""}))
        (run / "exit.json").write_text('{"exit_code": 0}')
        shown = json.dumps({"thread": {"threadId": "mcp:t-1", "status": "completed"}})
        with mock.patch.object(worker.t3_adapter, "_t3", return_value=subprocess.CompletedProcess([], 0, shown, "")), \
             mock.patch.object(worker.t3_adapter, "refresh") as refresh, \
             mock.patch.object(worker, "teardown_once") as teardown:
            worker.collect(self.db, self.root, lambda: NOW)
        refresh.assert_not_called()
        teardown.assert_not_called()
        self.assertIsNone(self.db.segments(KEY)[0].ended_at)

    def test_clearing_a_tree_cannot_kill_a_runner_and_abandon_its_remote_thread(self):
        run = self.root / ".chief-of-stuff/runs/old"
        run.mkdir(parents=True)
        (run / "command.json").write_text(json.dumps({"cwd": str(self.tree), "session_id": "old"}))
        (run / "t3.json").write_text("{}")
        with mock.patch.object(worker.t3_adapter, "observe", return_value={"status": "running"}), \
             mock.patch.object(worker.process_status, "carriers", return_value=[]), \
             mock.patch.object(worker.process_status, "stop_session") as stop:
            with self.assertRaisesRegex(RuntimeError, "T3.*live"):
                worker._clear_tree(self.root, self.tree, self.db, KEY, NOW.isoformat())
        stop.assert_not_called()

    def test_a_t3_run_is_classified_as_a_host_without_a_claude_envelope_and_checked_for_outside_merges(self):
        self.t3_launched()
        run = Path(self.db.resume(KEY)["runs"]["implement-1"]["run_dir"])
        (run / "stdout").write_text("plain reply text, not a Claude envelope")
        (run / "stderr").write_text("")
        self.report(run)
        (run / "exit.json").write_text(json.dumps({"exit_code": 0}))
        with mock.patch.object(worker.process_status, "identity", return_value=None), \
             mock.patch.object(worker.t3_adapter, "refresh", return_value={"status": "completed"}), \
             mock.patch.object(worker.t3_adapter, "observe", return_value=worker.t3_adapter.Evidence(alive=False, launcher="t3")), \
             mock.patch.object(worker.host_adapter, "moved_outside_engine", return_value="trunk moved") as moved:
            worker.collect(self.db, self.root, lambda: NOW + timedelta(minutes=1))
        moved.assert_called_once()
        self.assertEqual(self.db.segments(KEY)[0].outcome, "human_review")

    def test_an_adopted_t3_start_keeps_its_launcher_thread_and_branch_heads_and_takes_nothing_else_from_thread_json(self):
        self.t3_launched()
        data = self.db.resume(KEY)
        run = Path(data.pop("runs")["implement-1"]["run_dir"])
        self.assertEqual(json.loads((run / "branch_heads.json").read_text()), {"trunk": "d" * 40, "main": "d" * 40})
        (run / "thread.json").write_text(json.dumps({"thread_id": "mcp:t-1", "thread_link": "t3code://threads/env-1/mcp:t-1",
                                                     "pid": -1, "runtime": "codex", "branch_heads": {}}))
        (run / "exit.json").write_text('{"exit_code": 0}')
        record = worker._existing_run(data, run, "implement", 1, self.trees, lambda: NOW)
        self.assertEqual((record["launcher"], record["runtime"], record["thread_id"]), ("t3", "claude", "mcp:t-1"))
        self.assertEqual(record["branch_heads"], {"trunk": "d" * 40, "main": "d" * 40})
        self.assertEqual(record["pid"], 77)
        # A heads file that is not branch -> 40-hex sha is dropped, so the run holds for want of heads (no option injection).
        (run / "branch_heads.json").write_text(json.dumps({"main": "--output=/x"}))
        self.assertNotIn("branch_heads", worker._existing_run(data, run, "implement", 1, self.trees, lambda: NOW))

    ROTATION = ('[models.implement]\nrotation = ["codex:gpt-6.1-sol@high", "cursor:auto", '
                '"agy:gemini-4-argon-high", "claude:sonnet@high"]\n')

    def launch_rotation(self, on_path=("codex", "agent", "agy"), protected=True, data=None, start_error=None):
        """#701: a launch from the owner's mixed rotation; returns (used, the host the start went to)."""
        (self.root / "CLAUDE.md").write_text("## Coordinator\n- Settings: `chief-of-stuff.toml`\n")
        (self.root / "chief-of-stuff.toml").write_text(self.ROTATION)
        if data is not None:
            self.db.set_resume(KEY, data)
        self.forge.default_branch_protected = lambda: protected
        went = []

        def host(runtime, request, run_dir, env):
            went.append(runtime)
            if start_error is not None:
                raise start_error
            return self.start(request, run_dir), ""

        def claude(request, run_dir, **kwargs):
            went.append("claude")
            return self.start(request, run_dir)
        with mock.patch.object(worker.make_worktree, "build", return_value=SimpleNamespace(path=self.tree)), \
             mock.patch.object(worker.host_adapter, "resolve", side_effect=lambda b: f"/bin/{b}" if b in on_path else None), \
             mock.patch.object(worker.host_adapter, "branch_heads",
                               side_effect=lambda tree, branches: {b: "d" * 40 for b in branches}), \
             mock.patch.object(worker.host_adapter, "start", side_effect=host), \
             mock.patch.object(worker.claude_adapter, "start", side_effect=claude), \
             mock.patch.object(worker.process_status, "identity", return_value="start"):
            used = self.launch()
        return used, went[0]

    def test_the_first_launchable_entry_of_any_runtime_launches(self):
        used, went = self.launch_rotation()
        self.assertEqual((used, went), (("gpt-6.1-sol", "high", "codex"), "codex"))
        self.assertEqual(self.db.resume(KEY)["model"], {"implement": "codex:gpt-6.1-sol@high"})

    def test_failed_rotation_pick_keeps_the_selected_entry_and_original_cause(self):
        self.ROTATION = ('[models.implement]\nrotation = ["claude:sonnet@high", '
                         '"cursor:composer-2.5"]\npick = "random"\n')
        error = OSError("host refused")
        with mock.patch.object(worker.random, "choice", side_effect=lambda entries: entries[-1]) as choose:
            with self.assertRaisesRegex(worker.StartFailed, "host refused") as caught:
                self.launch_rotation(start_error=error)
        choose.assert_called_once()
        self.assertEqual(caught.exception.chosen, worker.ModelEntry("cursor", "composer-2.5"))
        self.assertIs(caught.exception.__cause__, error)
        self.assertFalse((self.db.resume(KEY) or {}).get("runs"))

    def test_failed_quota_fallback_keeps_the_new_host_instead_of_previous_model(self):
        self.ROTATION = ('[models.implement]\nrotation = ["claude:sonnet@high", '
                         '"cursor:composer-2.5"]\n')
        with self.assertRaises(worker.StartFailed) as caught:
            self.launch_rotation(data={"quota": {"retry_at": NOW.isoformat()},
                                       "model": {"implement": "claude:sonnet@high"}},
                                 start_error=OSError("host refused"))
        self.assertEqual(caught.exception.chosen, worker.ModelEntry("cursor", "composer-2.5"))

    def test_failure_before_selection_does_not_borrow_the_previous_model(self):
        self.db.set_resume(KEY, {"model": {"implement": "claude:opus@high"}})
        with mock.patch.object(worker, "_model") as choose, \
             mock.patch.object(self.forge, "issue", side_effect=OSError("forge unavailable")):
            with self.assertRaisesRegex(OSError, "forge unavailable"):
                self.launch()
        choose.assert_not_called()

    def test_failed_claude_start_retains_model_and_effort(self):
        with mock.patch.object(self, "start", side_effect=OSError("spawn refused")):
            with self.assertRaisesRegex(worker.StartFailed, "spawn refused") as caught:
                self.launch_with_settings('[models.implement]\nrotation = ["claude:sonnet@high"]\n')
        self.assertEqual(caught.exception.chosen, worker.ModelEntry("claude", "sonnet", "high"))
        self.assertIsInstance(caught.exception.__cause__, OSError)

    def test_launch_does_not_wrap_process_control_exceptions(self):
        for exception in (KeyboardInterrupt, SystemExit):
            with self.subTest(exception=exception), mock.patch.object(self, "start", side_effect=exception):
                self.db.set_resume(KEY, {})
                with self.assertRaises(exception):
                    self.launch_with_settings('[models.implement]\nrotation = ["claude:sonnet@high"]\n')

    def test_a_host_whose_cli_is_missing_is_skipped_for_the_next_entry(self):
        used, went = self.launch_rotation(on_path=("agent", "agy"))
        self.assertEqual((used, went), (("auto", "", "cursor"), "cursor"))
        self.assertIn("codex:gpt-6.1-sol@high", " ".join(e.message for e in self.db.log(KEY)))

    def test_without_a_protected_default_branch_only_claude_launches(self):
        used, went = self.launch_rotation(protected=False)
        self.assertEqual((used, went), (("sonnet", "high"), "claude"))

    def test_a_quota_stop_advances_across_runtimes(self):
        used, went = self.launch_rotation(data={"quota": {"reset": "later", "retry_at": NOW.isoformat()},
                                                "model": {"implement": "codex:gpt-6.1-sol@high"}})
        self.assertEqual(went, "cursor")

    def test_random_pick_chooses_only_among_launchable_entries(self):
        # #877, Zach: "the goal of this is to be evaluate performance of all the different models over time".
        self.ROTATION = self.ROTATION + 'pick = "random"\n'
        offered = []

        def last(entries):
            offered.append([str(e) for e in entries])
            return entries[-1]
        with mock.patch.object(worker.random, "choice", side_effect=last):
            used, went = self.launch_rotation(on_path=("agent", "agy"))  # codex is not installed here
        self.assertEqual(offered, [["cursor:auto", "agy:gemini-4-argon-high", "claude:sonnet@high"]])
        self.assertEqual((used, went), (("sonnet", "high"), "claude"))

    def test_first_round_robin_turn_starts_at_the_first_launchable_entry(self):
        with mock.patch.object(worker.random, "choice") as never:
            self.assertEqual(self.launch_rotation()[1], "codex")
        never.assert_not_called()

    def test_launch_skips_near_cap_and_records_workflow_with_the_selected_entry(self):
        self.ROTATION = ('[models.implement]\nrotation = ["claude:sonnet@high", "codex:next@medium"]\n'
                         '[limits]\nwarn_at = 0.65\n')
        with mock.patch.object(worker, "below_limit", side_effect=lambda runtime, *args: runtime != "claude") as probe:
            used, went = self.launch_rotation()
        self.assertEqual((used, went), (("next", "medium", "codex"), "codex"))
        self.assertIn(mock.call("claude", "", NOW, .65), probe.call_args_list)
        slots.claim(self.trees, self.db, issue_key=KEY, stage="implement", attempt=1,
                    max_concurrency=1, clock=lambda: NOW, launch=lambda: used)
        self.assertEqual(self.db.segments(KEY)[0].workflow, self.p.default_workflow)

    def test_a_quota_stop_advances_even_when_picking_at_random(self):
        self.ROTATION = self.ROTATION + 'pick = "random"\n'
        with mock.patch.object(worker.random, "choice", side_effect=lambda entries: entries[-1]):
            used, went = self.launch_rotation(data={"quota": {"reset": "later", "retry_at": NOW.isoformat()},
                                                    "model": {"implement": "codex:gpt-6.1-sol@high"}})
        self.assertEqual(went, "cursor")

    def test_a_successful_launch_clears_the_quota_wait(self):
        self.db.set_resume(KEY, {"quota": {"reset": "later", "retry_at": NOW.isoformat()}})
        self.launched()
        self.assertNotIn("quota", self.db.resume(KEY))

    def test_review_checks_out_request_branch(self):
        with mock.patch.object(worker.make_worktree, "build", return_value=SimpleNamespace(path=self.tree)) as build, \
             mock.patch.object(worker.claude_adapter, "start", side_effect=self.start), \
             mock.patch.object(worker.process_status, "identity", return_value="start"):
            self.launch("review")
        self.assertEqual(build.call_args.kwargs["from_branch"], "feature/existing")

    def test_starting_head_is_saved_before_the_worker_can_change_the_tree_and_survives_adoption(self):
        def start(request, run_dir, **kwargs):
            self.assertEqual((run_dir / "starting_head").read_text(), "head")
            return self.start(request, run_dir, **kwargs)
        with mock.patch.object(worker.make_worktree, "build", return_value=SimpleNamespace(path=self.tree)), \
             mock.patch.object(worker.claude_adapter, "start", side_effect=start), \
             mock.patch.object(worker.process_status, "identity", return_value="process-start"):
            self.launch()
            data = self.db.resume(KEY)
            record = data["runs"]["implement-1"]
            self.assertEqual(record.get("starting_head"), "head")
            data["runs"] = {}
            self.db.set_resume(KEY, data)
            self.launch()
        self.assertEqual(self.db.resume(KEY)["runs"]["implement-1"]["starting_head"], "head")

    def test_collect_recovers_starting_head_for_an_existing_run_record(self):
        run = self.launched()
        data = self.db.resume(KEY)
        data["runs"]["implement-1"].pop("starting_head")
        self.db.set_resume(KEY, data)
        worker.collect(self.db, self.root, lambda: NOW, trees=self.trees)
        self.assertEqual(self.db.resume(KEY)["runs"]["implement-1"].get("starting_head"), "head")

    def test_reviewed_head_round_trips_through_run_and_collected_result(self):
        run = self.launched("review")
        record = self.db.resume(KEY)["runs"]["review-1"]
        self.assertEqual(record.get("reviewed_head"), "head")
        # Completion must use the launch's pinned SHA, even after the request moves.
        self.forge.closing_request = mock.Mock(return_value=SimpleNamespace(branch="feature/existing", head="new-head"))
        data = self.db.resume(KEY)
        data["review_trees"]["1"]["head"] = "new-head"
        data["runs"]["review-1"].pop("starting_head")  # Recover the durable launch baseline during collect.
        self.db.set_resume(KEY, data)
        (run / "stdout").write_text('{"is_error": false, "result": "reviewed"}')
        (run / "stderr").write_text("")
        self.report(run, stage="review")
        (run / "exit.json").write_text('{"exit_code": 0}')
        with mock.patch.object(worker.process_status, "identity", return_value=None):
            worker.collect(self.db, self.root, lambda: NOW + timedelta(minutes=1))
        reopened = store.Store(self.root / "store.db")
        self.addCleanup(reopened.engine.dispose)
        self.assertEqual(reopened.resume(KEY)["runs"]["review-1"]["result"], {
            "reviewed_head": "head", "outcome": "done", "ended_at": (NOW + timedelta(minutes=1)).isoformat()})
        self.assertEqual(reopened.segments(KEY)[0].outcome, "done")
        self.forge.closing_request.assert_not_called()

    def test_generated_trees_have_cleanup_ownership_even_after_refused_start(self):
        for stage in ("implement", "review"):
            with self.subTest(stage=stage):
                tree = self.trees / f"owned-{stage}"
                tree.mkdir()
                with mock.patch.object(worker.make_worktree, "build", return_value=SimpleNamespace(path=tree)), \
                     mock.patch.object(worker.claude_adapter, "start", side_effect=RuntimeError("refused")):
                    with self.assertRaisesRegex(RuntimeError, "refused"):
                        self.launch(stage)
                self.assertIn(str(tree), [row.path for row in self.db.trees(KEY)])
                self.db.upsert_item(KEY, forge_state="closed")
                items, guards = cleanup.owner_guards(tree, cleanup.owned_items(self.root, self.db))
                self.assertEqual([item.issue_key for item in items], [KEY])
                self.assertEqual(guards, [])

    def test_restart_adopts_live_run_before_slot_record_with_or_without_resume_record(self):
        for recorded in (True, False):
            with self.subTest(recorded=recorded):
                if self.db.resume(KEY):
                    self.db.engine.dispose()
                    self.setUp()
                with mock.patch.object(worker.make_worktree, "build", return_value=SimpleNamespace(path=self.tree)), \
                     mock.patch.object(worker.claude_adapter, "start", side_effect=self.start), \
                     mock.patch.object(worker.process_status, "identity", return_value="process-start"):
                    self.launch()
                data = self.db.resume(KEY)
                record = data["runs"]["implement-1"]
                run = Path(record["run_dir"])
                if not recorded:
                    data.pop("runs")
                    self.db.set_resume(KEY, data)
                worker._runners.pop(str(run), None)
                with mock.patch.object(worker.make_worktree, "build") as build, \
                     mock.patch.object(worker.make_worktree, "git", return_value=(0, record["branch"])) as git, \
                     mock.patch.object(worker.claude_adapter, "start", side_effect=self.start) as start, \
                     mock.patch.object(worker.process_status, "identity", return_value="process-start") as identity:
                    slots.claim(self.trees, self.db, issue_key=KEY, stage="implement", attempt=1,
                                max_concurrency=1, clock=lambda: NOW, launch=self.launch)
                start.assert_not_called()
                build.assert_not_called()
                git.assert_not_called()
                identity.assert_any_call(77, record["session_id"], host=True)
                identity.assert_any_call(78, record["session_id"])
                self.assertEqual(slots.running(self.db), 1)
                self.assertEqual(self.db.resume(KEY)["runs"]["implement-1"], record)
                self.assertEqual(list(run.parent.glob(f"{run.name}-refused-*")), [])
                (run / "stdout").write_text('{"is_error": false, "result": "done"}')
                (run / "stderr").write_text("")
                self.report(run)
                (run / "exit.json").write_text('{"exit_code": 0}')
                with mock.patch.object(worker.process_status, "identity", return_value=None):
                    worker.collect(self.db, self.root, lambda: NOW)
                self.assertEqual(self.db.segments()[0].outcome, "done")

    def test_recovered_start_uses_reserved_entry_when_model_save_was_interrupted(self):
        used, _ = self.launch_rotation()
        data = self.db.resume(KEY)
        data.pop("model")
        data.pop("runs")
        self.db.set_resume(KEY, data)
        with mock.patch.object(worker.process_status, "identity", return_value="start"), \
             mock.patch.object(worker.host_adapter, "start") as start:
            recovered = self.launch()
        start.assert_not_called()
        self.assertEqual(recovered, used)

    def test_dead_or_reused_run_is_preserved_as_refused_before_relaunch(self):
        for recorded, old_identity in ((True, None), (True, "different-birth"), (False, None)):
            with self.subTest(recorded=recorded, old_identity=old_identity):
                if self.db.resume(KEY):
                    self.db.engine.dispose()
                    self.setUp()
                with mock.patch.object(worker.make_worktree, "build", return_value=SimpleNamespace(path=self.tree)), \
                     mock.patch.object(worker.claude_adapter, "start", side_effect=self.start), \
                     mock.patch.object(worker.process_status, "identity", return_value="process-start"):
                    self.launch()
                old = self.db.resume(KEY)["runs"]["implement-1"]
                run = Path(old["run_dir"])
                if not recorded:
                    data = self.db.resume(KEY)
                    data.pop("runs")
                    self.db.set_resume(KEY, data)
                (run / "exit.json").write_text('{"exit_code": "unknown"}')
                with mock.patch.object(worker.make_worktree, "git", return_value=(0, old["branch"])), \
                     mock.patch.object(worker.claude_adapter, "start", side_effect=self.start) as start, \
                     mock.patch.object(worker.process_status, "identity", side_effect=[old_identity, old_identity,
                                                                                          "new-start", "new-start"]):
                    self.launch()
                self.assertEqual(start.call_count, 1)
                refused, = run.parent.glob(f"{run.name}-refused-*")
                self.assertTrue((refused / "exit.json").exists())
                self.assertNotEqual(self.db.resume(KEY)["runs"]["implement-1"]["session_id"], old["session_id"])

    def test_restart_adopts_completed_run_before_slot_record(self):
        for stage in ("implement", "review"):
            for recorded, outcome in ((True, "done"), (False, "done"), (False, "failed"), (False, "quota")):
                with self.subTest(stage=stage, recorded=recorded, outcome=outcome):
                    if self.db.resume(KEY):
                        self.db.engine.dispose()
                        self.setUp()
                    with mock.patch.object(worker.make_worktree, "build", return_value=SimpleNamespace(path=self.tree)), \
                         mock.patch.object(worker.claude_adapter, "start", side_effect=self.start), \
                         mock.patch.object(worker.process_status, "identity", return_value="process-start"):
                        self.launch(stage)
                    data = self.db.resume(KEY)
                    record = data["runs"][f"{stage}-1"]
                    run = Path(record["run_dir"])
                    if not recorded:
                        data.pop("runs")
                        self.db.set_resume(KEY, data)
                    worker._runners.pop(str(run), None)
                    text = "429 quota exceeded; resets in 2 hours" if outcome == "quota" else outcome
                    (run / "stdout").write_text(json.dumps({"is_error": outcome != "done", "result": text}))
                    (run / "stderr").write_text("")
                    if outcome == "done":
                        self.report(run, stage=stage)
                    (run / "exit.json").write_text(json.dumps({"exit_code": 0 if outcome == "done" else 1}))
                    with mock.patch.object(worker.make_worktree, "build", return_value=SimpleNamespace(path=self.tree)) as build, \
                         mock.patch.object(worker.make_worktree, "git", return_value=(0, "head" if stage == "review" else record["branch"])) as git, \
                         mock.patch.object(worker.claude_adapter, "start", side_effect=AssertionError("completed worker must not be relaunched")) as start, \
                         mock.patch.object(worker.process_status, "identity", return_value=None):
                        slots.claim(self.trees, self.db, issue_key=KEY, stage=stage, attempt=1,
                                    max_concurrency=1, clock=lambda: NOW, launch=lambda: self.launch(stage))
                        start.assert_not_called()
                        build.assert_not_called()
                        git.assert_not_called()
                        worker.collect(self.db, self.root, lambda: NOW + timedelta(minutes=1))
                    self.assertEqual(list(run.parent.glob(f"{run.name}-refused-*")), [])
                    self.assertEqual(self.db.resume(KEY)["runs"][f"{stage}-1"]["session_id"], record["session_id"])
                    self.assertEqual(self.db.segments(KEY)[0].outcome, outcome)
                    if outcome == "quota":
                        self.assertEqual(self.db.resume(KEY)["quota"]["retry_at"],
                                         (NOW + timedelta(hours=2, minutes=1)).isoformat())
                    if stage == "review":
                        self.assertEqual(self.db.resume(KEY)["runs"]["review-1"]["result"]["reviewed_head"], "head")

    def test_unverified_existing_run_keeps_evidence_and_refuses_second_worker(self):
        run = self.launched()
        with mock.patch.object(worker.claude_adapter, "start") as start, \
             mock.patch.object(worker.process_status, "identity", side_effect=RuntimeError("ps blocked")):
            with self.assertRaisesRegex(RuntimeError, "ps blocked"):
                self.launch()
        start.assert_not_called()
        self.assertTrue((run / "started.json").exists())
        self.assertEqual(list(run.parent.glob(f"{run.name}-refused-*")), [])

    def test_adopted_host_with_dead_runner_can_be_collected_after_exit(self):
        with mock.patch.object(worker.make_worktree, "build", return_value=SimpleNamespace(path=self.tree)), \
             mock.patch.object(worker.claude_adapter, "start", side_effect=self.start), \
             mock.patch.object(worker.process_status, "identity", return_value="process-start"):
            self.launch()
        data = self.db.resume(KEY)
        run = Path(data.pop("runs")["implement-1"]["run_dir"])
        self.db.set_resume(KEY, data)
        with mock.patch.object(worker.claude_adapter, "start") as start, \
             mock.patch.object(worker.process_status, "identity", side_effect=["process-start", None]):
            slots.claim(self.trees, self.db, issue_key=KEY, stage="implement", attempt=1,
                        max_concurrency=1, clock=lambda: NOW, launch=self.launch)
        start.assert_not_called()
        (run / "stdout").write_text('{"is_error": false, "result": "done"}')
        (run / "stderr").write_text("")
        self.report(run)
        (run / "exit.json").write_text('{"exit_code": 0}')
        with mock.patch.object(worker.process_status, "identity", return_value=None):
            worker.collect(self.db, self.root, lambda: NOW)
        self.assertEqual(self.db.segments()[0].outcome, "done")

    def test_refused_review_tree_is_rebuilt_when_request_head_changes(self):
        with mock.patch.object(worker.make_worktree, "build", return_value=SimpleNamespace(path=self.tree)), \
             mock.patch.object(worker.claude_adapter, "start", side_effect=RuntimeError("refused")):
            with self.assertRaisesRegex(RuntimeError, "refused"):
                self.launch("review")
        self.assertEqual(self.db.resume(KEY)["review_trees"]["1"].get("head"), "head")
        self.forge.closing_request = lambda key: SimpleNamespace(branch="feature/existing", head="new-head")
        fresh = self.trees / "fresh-review"
        fresh.mkdir()
        with mock.patch.object(worker.make_worktree, "build", return_value=SimpleNamespace(path=fresh)) as build, \
             mock.patch.object(worker.make_worktree, "git", side_effect=[(0, "head"), (0, "new-head"), (0, "new-head")]), \
             mock.patch.object(worker.claude_adapter, "start", side_effect=self.start) as start, \
             mock.patch.object(worker.process_status, "identity", return_value="start"):
            self.launch("review")
        build.assert_called_once()
        self.assertNotEqual(build.call_args.args[3], self.tree.name)
        self.assertEqual(start.call_args.args[0].cwd, str(fresh))
        self.assertEqual(self.db.resume(KEY)["review_trees"]["1"]["head"], "new-head")
        self.assertEqual({row.path for row in self.db.trees(KEY)}, {str(self.tree), str(fresh)})

    def test_live_review_is_adopted_before_changed_head_can_rebuild_its_tree(self):
        with mock.patch.object(worker.make_worktree, "build", return_value=SimpleNamespace(path=self.tree)), \
             mock.patch.object(worker.claude_adapter, "start", side_effect=self.start), \
             mock.patch.object(worker.process_status, "identity", return_value="process-start"):
            self.launch("review")
        self.forge.closing_request = mock.Mock(return_value=SimpleNamespace(branch="feature/existing", head="new-head"))
        with mock.patch.object(worker.make_worktree, "build") as build, \
             mock.patch.object(worker.claude_adapter, "start") as start, \
             mock.patch.object(worker.process_status, "identity", return_value="process-start"):
            slots.claim(self.trees, self.db, issue_key=KEY, stage="review", attempt=1,
                        max_concurrency=1, clock=lambda: NOW, launch=lambda: self.launch("review"))
        build.assert_not_called()
        start.assert_not_called()
        self.forge.closing_request.assert_not_called()
        self.assertEqual(slots.running(self.db), 1)

    def test_review_refuses_a_checkout_that_does_not_match_the_request_sha(self):
        with mock.patch.object(worker.make_worktree, "build", return_value=SimpleNamespace(path=self.tree)), \
             mock.patch.object(worker.make_worktree, "git", return_value=(0, "unexpected-head")), \
             mock.patch.object(worker.claude_adapter, "start") as start:
            with self.assertRaisesRegex(RuntimeError, "does not match the request head"):
                self.launch("review")
        start.assert_not_called()
        self.assertEqual([row.path for row in self.db.trees(KEY)], [str(self.tree)])

    def test_rework_reuses_tree_and_branch_without_build(self):
        self.launched()
        self.db.end_segment(KEY, "implement", 1, NOW.isoformat(), "done")
        with mock.patch.object(worker.make_worktree, "build") as build, \
             mock.patch.object(worker.make_worktree, "git", return_value=(0, "work/issue-1-implement-1")), \
             mock.patch.object(worker.claude_adapter, "start", side_effect=self.start) as start, \
             mock.patch.object(worker.process_status, "identity", return_value="start"):
            self.launch("implement", 2)
        build.assert_not_called()
        self.assertEqual(start.call_args.args[0].cwd, str(self.tree))

    def test_collect_done_failed_and_quota(self):
        for outcome in ("done", "failed", "quota"):
            with self.subTest(outcome=outcome):
                if self.db.segments():
                    # Independent store for each outcome.
                    self.db.engine.dispose()
                    self.setUp()
                seg = self.finish(outcome)
                self.assertEqual(seg.outcome, outcome)
                self.assertEqual(seg.ended_at, (NOW + timedelta(minutes=1)).isoformat())
                if outcome == "quota":
                    quota = self.db.resume(KEY)["quota"]
                    self.assertIn("resets in 2 hours", quota["reset"])
                    self.assertEqual(quota["retry_at"], (NOW + timedelta(hours=2, minutes=1)).isoformat())

    def report(self, run, outcome="done", stage="implement", suite=None):
        session = json.loads((run / "started.json").read_text())["session_id"]
        kwargs = {"suite": suite} if suite is not None else {}
        self.db.add_report(KEY, stage, 1, session, outcome, f"reported {outcome}", [], **kwargs)

    def test_opted_in_assignment_names_the_suite_hold(self):
        prompt = worker.dispatch_prompt.compose_issue(
            self.db.items()[0], "title", "body", "implement", self.persona, root=self.root, require_green=True)
        self.assertIn("require_green_tests", prompt)
        self.assertIn("failed work", prompt)
        self.assertIn("passed, failed, skipped and xfailed are all known", prompt)
        self.assertIn("passed > 0", prompt)
        self.assertIn("failed, skipped and xfailed all zero", prompt)
        off = worker.dispatch_prompt.compose_issue(
            self.db.items()[0], "title", "body", "implement", self.persona, root=self.root)
        self.assertNotIn("require_green_tests", off)

    def test_opted_in_collect_holds_done_unless_the_suite_is_green(self):
        green = {"passed": 4, "failed": 0, "skipped": 0, "xfailed": 0}
        cases = (
            (True, {**green, "failed": 1}, "human_review", True),
            (True, {**green, "passed": 0}, "human_review", True),
            (True, {**green, "skipped": 2}, "human_review", True),
            (True, {**green, "xfailed": 1}, "human_review", True),
            (True, {"passed": True, "failed": 0, "skipped": 0, "xfailed": 0}, "human_review", True),
            (True, {}, "human_review", True),
            (True, green, "done", False),
            (False, {**green, "failed": 1}, "done", False),
            (True, None, "human_review", True),
        )
        for enabled, suite, expected, held in cases:
            with self.subTest(enabled=enabled, suite=suite):
                if self.db.segments():
                    self.db.engine.dispose()
                    self.setUp()
                (self.root / "CLAUDE.md").write_text("## Coordinator\n- Settings: `chief-of-stuff.toml`\n")
                (self.root / "chief-of-stuff.toml").write_text(
                    f"[workers]\nrequire_green_tests = {str(enabled).lower()}\n")
                run = self.launched()
                self.report(run, suite=suite)
                (run / "stdout").write_text('{"is_error": false, "result": "stopped"}')
                (run / "stderr").write_text("")
                (run / "exit.json").write_text('{"exit_code": 0}')
                with mock.patch.object(worker.process_status, "identity", return_value=None):
                    worker.collect(self.db, self.root, lambda: NOW + timedelta(minutes=1), require_green=enabled)
                self.assertEqual(self.db.segments(KEY)[0].outcome, expected)
                self.assertEqual(self.db.items()[0].human_review, held)
                self.assertEqual("suite_hold" in self.db.resume(KEY), held)
                if held:
                    log = self.db.log(KEY)[-1].message
                    self.assertIn("suite is not green", log)
                    self.assertIn("reported done", log)

    def suite_run(self, stage="implement", outcome="done"):
        (self.root / "CLAUDE.md").write_text("## Coordinator\n- Settings: `chief-of-stuff.toml`\n")
        (self.root / "chief-of-stuff.toml").write_text("[workers]\nrequire_green_tests = true\n")
        self.db.upsert_item(KEY, stage=stage)
        if stage == "fix":
            self.p.stages["fix"] = replace(self.p.stages["implement"], name="fix")
            flow = self.p.workflows["standard"]
            self.p.workflows["standard"] = replace(flow, edges=flow.edges + (
                reconcile.Edge("review", "fix"), reconcile.Edge("fix", "review")))
            self.db.set_resume(KEY, {"worktree": str(self.tree), "work_branch": "head"})
        run = self.launched(stage)
        self.report(run, stage=stage, outcome=outcome,
                    suite={"passed": 0, "failed": 1, "skipped": 0, "xfailed": 0})
        (run / "stdout").write_text('{"is_error": false, "result": "stopped"}')
        (run / "stderr").write_text("")
        (run / "exit.json").write_text('{"exit_code": 0}')
        forge = FakeForge()
        forge.rows[0].update(stage=stage, human_review=False)
        return forge

    def suite_pass(self, forge, launch, tree=(reconcile.TreeState("head", 0, 0, 0), False, NOW)):
        with mock.patch.object(engine.cleanup, "execute", return_value=0), mock.patch.object(engine, "_record_limits"), \
             mock.patch.object(engine.engine_facts, "_tree_facts", return_value=tree):
            return engine.run_pass(store=self.db, root=self.root, trees=self.trees,
                                   forge=forge, policies=self.p, launch=launch,
                                   workers=engine.settings.Workers(),
                                   clock=lambda: NOW + timedelta(minutes=1), require_green=True)

    def test_a_red_suite_does_not_replace_a_completion_hold(self):
        forge = self.suite_run()
        forge.add_human_hold = mock.Mock(return_value="")
        self.suite_pass(forge, mock.Mock(return_value=("example", "")),
                        tree=(reconcile.TreeState("head", 3, 0, 0), False, NOW))
        self.assertNotIn("suite_hold", self.db.resume(KEY) or {})
        self.assertTrue(self.db.items()[0].human_review)
        self.assertEqual(self.db.segments(KEY)[0].outcome, "done")
        self.assertTrue(any("uncommitted" in row.message for row in self.db.log(KEY)))

    def test_an_adopted_completed_start_finishes_on_the_same_pass(self):
        forge = self.suite_run()
        data = self.db.resume(KEY)
        run = Path(data["runs"]["implement-1"]["run_dir"])
        self.report(run, suite={"passed": 4, "failed": 0, "skipped": 0, "xfailed": 0})
        data.pop("runs")
        self.db.set_resume(KEY, data)
        worker._runners.pop(str(run), None)
        with mock.patch.object(engine.engine_facts, "gather", wraps=engine.engine_facts.gather) as gather:
            self.suite_pass(forge, mock.Mock(return_value=("example", "")))
        gather.assert_called_once()
        self.assertIsNotNone(self.db.segments(KEY)[0].ended_at)
        self.assertEqual(self.db.segments(KEY)[0].outcome, "done")
        self.assertFalse(self.db.items()[0].human_review)
        self.assertNotIn("suite_hold", self.db.resume(KEY))
        self.assertEqual(self.db.resume(KEY)["runs"]["implement-1"]["starting_head"], "head")

    def test_an_exit_published_during_gather_waits_until_the_next_pass(self):
        run = self.launched()
        self.report(run)
        (run / "stdout").write_text('{"is_error": false, "result": "done"}')
        (run / "stderr").write_text("")
        facts = reconcile.Facts()

        def gather():
            (run / "exit.json").write_text('{"exit_code": 0}')
            return facts

        finish = mock.Mock()
        with mock.patch.object(worker.process_status, "identity", return_value="process-start"):
            self.assertIs(worker.collect(self.db, self.root, lambda: NOW, gather=gather, finish=finish), facts)
        finish.assert_not_called()
        worker.collect(self.db, self.root, lambda: NOW, gather=lambda: facts, finish=finish)
        finish.assert_called_once()

    def test_failed_close_stays_open_logs_once_and_other_collection_and_planning_continue(self):
        forge = self.suite_run()
        forge.kanban = SimpleNamespace(stage_map={}, hold_stages=())
        forge.add_human_hold = mock.Mock(side_effect=lambda number: "label unavailable" if number == 1 else "")
        other = KEY.rpartition("#")[0] + "#2"
        fresh = KEY.rpartition("#")[0] + "#3"
        forge.rows.extend([{"issue_key": other, "stage": "implement", "forge_state": "open"},
                           {"issue_key": fresh, "stage": "implement", "forge_state": "open"}])
        self.db.upsert_item(other, stage="implement")
        self.db.record_segment(other, "implement", 1, NOW.isoformat())
        launch = mock.Mock(return_value=("example", ""))
        end = self.db.end_segment
        def close(key, *args, **kwargs):
            if key == KEY:
                raise RuntimeError("store temporarily unavailable")
            return end(key, *args, **kwargs)
        with mock.patch.object(self.db, "end_segment", side_effect=close):
            for _ in range(2):
                self.suite_pass(forge, launch)
                self.assertIsNone(self.db.segments(KEY)[0].ended_at)
        self.assertIsNotNone(self.db.segments(other)[0].ended_at)
        self.assertIn(fresh, [call.args[0] for call in launch.call_args_list])
        errors = [row.message for row in self.db.log(KEY) if "close failed" in row.message]
        self.assertEqual(len(errors), 1)
        self.assertIn("store temporarily unavailable", errors[0])

    def test_a_foreign_session_cannot_replace_the_current_red_suite(self):
        for exitless in (False, True):
            with self.subTest(exitless=exitless):
                if self.db.segments():
                    self.db.engine.dispose()
                    self.setUp()
                forge = self.suite_run()
                self.db.add_report(KEY, "implement", 1, "foreign", "done", "foreign green", [],
                                   suite={"passed": 5, "failed": 0, "skipped": 0, "xfailed": 0})
                if exitless:
                    run = Path(self.db.resume(KEY)["runs"]["implement-1"]["run_dir"])
                    (run / "exit.json").unlink()
                with mock.patch.object(worker.process_status, "identity", return_value=None):
                    self.suite_pass(forge, mock.Mock(return_value=("example", "")))
                expected = "human_review" if exitless else "failed"
                self.assertEqual(self.db.segments(KEY)[0].outcome, expected)
                self.assertEqual("suite_hold" in self.db.resume(KEY), exitless)

    def test_a_dead_worker_without_an_exit_retries_a_red_suite(self):
        forge = self.suite_run()
        run_dir = Path(self.db.resume(KEY)["runs"]["implement-1"]["run_dir"])
        (run_dir / "exit.json").unlink()
        forge.add_human_hold = mock.Mock(return_value="")
        with mock.patch.object(worker.process_status, "identity", return_value=None), \
             mock.patch.object(engine.engine_facts.process_status, "identity", return_value=None):
            self.suite_pass(forge, mock.Mock(return_value=("example", "")))
        self.assertEqual(self.db.segments(KEY)[0].outcome, "human_review")
        self.assertEqual(self.db.resume(KEY)["suite_hold"]["stage"], "implement")
        launch = mock.Mock(return_value=("example", ""))
        self.suite_pass(forge, launch)
        launch.assert_called_once_with(KEY, "implement", 2, "builder")
        self.assertEqual(self.db.segments(KEY)[0].outcome, "failed")
        self.assertNotIn("suite_hold", self.db.resume(KEY))

    def test_a_non_done_report_is_not_suite_gated(self):
        forge = self.suite_run(outcome="blocked")
        forge.add_human_hold = mock.Mock(return_value="")
        with mock.patch.object(engine.engine_facts, "_tree_facts",
                               return_value=(reconcile.TreeState("head", 0, 0, 0), False, NOW)):
            self.suite_pass(forge, mock.Mock(return_value=("example", "")))
        self.assertEqual(self.db.segments(KEY)[0].outcome, "blocked")
        self.assertFalse(self.db.items()[0].human_review)
        self.assertNotIn("suite_hold", self.db.resume(KEY))
        forge.add_human_hold.assert_not_called()




    def test_closed_suite_holds_retire_without_publication_or_acceptance(self):
        for published in (False, True):
            with self.subTest(published=published):
                self.db.record_segment(KEY, "implement", 1, NOW.isoformat(),
                                       ended_at=NOW.isoformat(), outcome="human_review")
                self.db.set_resume(KEY, {"suite_hold": {"stage": "implement", "attempt": 1,
                                                        "published": published}})
                forge = FakeForge()
                forge.rows[0].update(forge_state="closed", human_review=False)
                forge.add_human_hold = mock.Mock(return_value="")
                launch = mock.Mock()
                self.suite_pass(forge, launch)
                forge.add_human_hold.assert_not_called()
                launch.assert_not_called()
                self.assertNotIn("suite_hold", self.db.resume(KEY))
                self.assertEqual(self.db.segments(KEY)[0].outcome, "human_review")

    def test_malformed_suite_holds_fail_closed_without_blocking_other_issues(self):
        self.db.record_segment(KEY, "implement", 1, NOW.isoformat(),
                               ended_at=NOW.isoformat(), outcome="human_review")
        invalid_record = "suite hold: invalid suite hold record"
        missing_segment = "suite hold: suite hold must reference a completed human-review segment"
        bad_holds = [({}, invalid_record), ("broken", invalid_record),
                     ({"published": True}, invalid_record),
                     ({"stage": "implement", "attempt": 99, "published": True}, missing_segment),
                     ({"stage": "implement", "attempt": True, "published": True}, invalid_record),
                     ({"stage": "implement", "attempt": 1, "published": "yes"}, invalid_record)]
        other = KEY.rpartition("#")[0] + "#2"
        for hold, message in bad_holds:
            with self.subTest(hold=hold):
                self.db.set_resume(KEY, {"suite_hold": hold})
                forge = FakeForge()
                forge.rows[0]["human_review"] = False
                forge.rows.append({"issue_key": other, "stage": "implement", "forge_state": "open"})
                forge.add_human_hold = mock.Mock(return_value="")
                launch = mock.Mock(return_value=("example", ""))
                # slots would otherwise keep the fake second worker's reservation on the next case.
                self.db.drop_segment(other, "implement", 1)
                self.suite_pass(forge, launch)
                self.assertTrue(self.db.items()[0].human_review)
                self.assertEqual(self.db.resume(KEY)["suite_hold"], hold)
                self.assertEqual(self.db.segments(KEY)[0].outcome, "human_review")
                self.assertEqual(launch.call_args.args[0], other)
                forge.add_human_hold.assert_not_called()
                hold_logs = [entry.message for entry in self.db.log(KEY)
                             if entry.message.startswith("suite hold:")]
                self.assertEqual(hold_logs[-1], message)

    def test_missing_hold_observations_retain_hold_and_original_outcome(self):
        self.db.record_segment(KEY, "implement", 1, NOW.isoformat(),
                               ended_at=NOW.isoformat(), outcome="human_review")
        hold = {"stage": "implement", "attempt": 1, "published": True}
        for rows in ([], [{"issue_key": KEY}]):
            with self.subTest(rows=rows):
                self.db.record_segment(KEY, "implement", 1, NOW.isoformat(), NOW.isoformat(), "human_review")
                self.db.set_resume(KEY, {"suite_hold": hold})
                engine._sync_suite_holds(FakeForge(), self.db, rows, NOW.isoformat())
                self.assertNotIn("suite_hold", self.db.resume(KEY))
                self.assertEqual(self.db.segments(KEY)[0].outcome, "failed")
                self.assertEqual(self.db.resume(KEY)["launcher_hold"], {"stage": "implement", "attempt": 1})

    def test_collect_uses_passed_policy_when_workspace_configuration_breaks(self):
        self.suite_run()
        (self.root / "CLAUDE.md").write_bytes(b"\xff")
        (self.root / "chief-of-stuff.toml").write_text("invalid toml [")
        worker.collect(self.db, self.root, lambda: NOW, require_green=True)
        self.assertEqual(self.db.segments(KEY)[0].outcome, "human_review")

    def test_successful_host_takes_the_segment_outcome_from_its_session_report(self):
        for outcome, expected in (("done", "done"), ("blocked", "blocked"), ("failed", "failed"), (None, "failed")):
            with self.subTest(report=outcome):
                if self.db.segments():
                    self.db.engine.dispose()
                    self.setUp()
                run = self.launched()
                if outcome:
                    self.report(run, outcome)
                # A stale report from another session must not count.
                self.db.add_report(KEY, "implement", 1, "other-session", "done", "stale", [])
                (run / "stdout").write_text('{"is_error": false, "result": "stopped"}')
                (run / "stderr").write_text("")
                (run / "exit.json").write_text('{"exit_code": 0}')
                with mock.patch.object(worker.process_status, "identity", return_value=None):
                    worker.collect(self.db, self.root, lambda: NOW + timedelta(minutes=1))
                self.assertEqual(self.db.segments(KEY)[0].outcome, expected)

    def test_collect_unknown_backs_off_like_quota(self):
        run = self.launched()
        (run / "stdout").write_text("not json")
        (run / "stderr").write_text("connection reset")
        (run / "exit.json").write_text(json.dumps({"exit_code": 1}))
        with mock.patch.object(worker.process_status, "identity", return_value=None):
            worker.collect(self.db, self.root, lambda: NOW + timedelta(minutes=1))
        seg = self.db.segments(KEY)[0]
        self.assertEqual(seg.outcome, "unknown")
        self.assertEqual(self.db.resume(KEY)["quota"]["retry_at"],
                         (NOW + timedelta(minutes=2)).isoformat())

    def test_alive_worker_stays_open(self):
        self.launched()
        with mock.patch.object(worker.process_status, "identity", return_value="process-start"):
            worker.collect(self.db, self.root, lambda: NOW)
        self.assertIsNone(self.db.segments()[0].ended_at)

    def test_missing_and_unreadable_run_records_end_failed(self):
        self.db.record_segment(KEY, "implement", 1, NOW.isoformat())
        self.db.record_segment(KEY, "review", 1, NOW.isoformat())
        self.db.set_resume(KEY, {"runs": {"review-1": {"run_dir": "/missing"}}})
        worker.collect(self.db, self.root, lambda: NOW)
        self.assertEqual([s.outcome for s in self.db.segments()], ["failed", "failed"])
        self.assertEqual(len(self.db.log(KEY)), 2)

    def test_a_gone_worker_with_no_exit_record_stays_open_for_the_planner(self):
        self.launched()
        with mock.patch.object(worker.process_status, "identity", return_value=None):
            worker.collect(self.db, self.root, lambda: NOW)
        self.assertIsNone(self.db.segments()[0].ended_at)

    def test_malformed_run_records_and_result_end_failed(self):
        self.db.record_segment(KEY, "implement", 1, NOW.isoformat())
        self.db.set_resume(KEY, {"runs": ["invalid"]})
        worker.collect(self.db, self.root, lambda: NOW)
        self.assertEqual(self.db.segments()[0].outcome, "failed")
        self.db.set_resume(KEY, {})
        run = self.launched("review")
        (run / "stdout").write_text("broken json")
        (run / "stderr").write_text("adapter error")
        (run / "exit.json").write_text('{"exit_code": 1}')
        with mock.patch.object(worker.process_status, "identity", return_value=None):
            worker.collect(self.db, self.root, lambda: NOW)
        # No JSON envelope is a retryable unknown (Codex P1), not a hard failed.
        self.assertEqual(self.db.segments()[-1].outcome, "unknown")
        self.assertIn("adapter error", self.db.log(KEY)[-1].message)
        self.assertIn("retry_at", self.db.resume(KEY)["quota"])

    def test_empty_identity_is_an_unreadable_record_not_a_live_worker(self):
        self.launched()
        data = self.db.resume(KEY)
        data["runs"]["implement-1"]["identity"] = None
        self.db.set_resume(KEY, data)
        with mock.patch.object(worker.process_status, "identity", return_value=None):
            worker.collect(self.db, self.root, lambda: NOW)
        self.assertEqual(self.db.segments()[0].outcome, "failed")

    def test_quota_waits_then_retries_without_consuming_limit(self):
        self.finish("quota")
        for attempt in range(2, 6):
            self.db.record_segment(KEY, "implement", attempt, (NOW + timedelta(minutes=attempt)).isoformat(),
                                   ended_at=(NOW + timedelta(minutes=attempt + 1)).isoformat(), outcome="quota")
        planned = reconcile.plan(self.db.items(), {KEY: self.db.segments()}, self.p, {}, 1, NOW, {},
                                 quota_resets={KEY: NOW + timedelta(hours=2)})
        self.assertIsInstance(planned[0], reconcile.Wait)
        planned = reconcile.plan(self.db.items(), {KEY: self.db.segments()}, self.p, {}, 1,
                                 NOW + timedelta(hours=3), {}, quota_resets={KEY: NOW + timedelta(hours=2)})
        self.assertEqual(planned, [reconcile.Dispatch(KEY, "implement", "builder", 6)])

    def test_engine_syncs_forge_before_collecting(self):
        order = []
        self.forge.snapshots = mock.Mock(side_effect=lambda: order.append("forge") or [])
        with mock.patch.object(engine.worker, "collect", side_effect=lambda *a, **kw: order.append("collect") or kw["gather"]()), \
             mock.patch.object(engine, "_record_limits"), \
             mock.patch.object(engine.cleanup, "execute", return_value=0):
            engine.run_pass(forge=self.forge, store=self.db, policies=self.p, root=self.root,
                            trees=self.trees, launch=self.launch, workers=engine.settings.Workers(), clock=lambda: NOW)
        self.assertEqual(order[:2], ["forge", "collect"])

    def test_failed_review_start_can_reuse_its_prepared_tree(self):
        with mock.patch.object(worker.make_worktree, "build", return_value=SimpleNamespace(path=self.tree)) as build, \
             mock.patch.object(worker.claude_adapter, "start", side_effect=self.start) as start, \
             mock.patch.object(worker.claude_adapter, "cancel"):
            with mock.patch.object(worker.time, "monotonic", side_effect=[0, 10]), \
                 mock.patch.object(worker.process_status, "identity", return_value=None), \
                 self.assertRaises(RuntimeError):
                self.launch("review")
            with mock.patch.object(worker.process_status, "identity", side_effect=[None, None, "start", "start"]):
                self.launch("review")
        self.assertEqual(build.call_count, 1)
        self.assertEqual(start.call_count, 2)
        self.assertEqual(start.call_args.args[0].cwd, str(self.tree))

    def test_collect_does_not_count_a_reused_pid_as_live(self):
        run = self.launched()
        (run / "stdout").write_text('{"is_error": false, "result": "done"}')
        (run / "stderr").write_text("")
        self.report(run)
        (run / "exit.json").write_text('{"exit_code": 0}')
        with mock.patch.object(worker.process_status, "identity", return_value="another-birth-time"):
            worker.collect(self.db, self.root, lambda: NOW)
        self.assertEqual(self.db.segments()[0].outcome, "done")

    def test_unavailable_process_view_waits_without_ending_a_live_worker(self):
        self.launched()
        with mock.patch.object(worker.process_status, "identity", side_effect=RuntimeError("ps blocked")):
            worker.collect(self.db, self.root, lambda: NOW)
        self.assertIsNone(self.db.segments()[0].ended_at)

    def test_unavailable_runner_view_waits_then_collects_durable_completion(self):
        run = self.launched()
        for error in (OSError("ps failed"), RuntimeError("ps blocked")):
            with self.subTest(error=error):
                with mock.patch.object(worker.process_status, "identity", side_effect=[None, error]):
                    worker.collect(self.db, self.root, lambda: NOW)
                segment = self.db.segments()[-1]
                self.assertIsNone(segment.ended_at)
                self.assertIsNone(segment.outcome)
                self.assertEqual(self.db.log(KEY)[-1].message, f"worker identity unverified: {error}")
        (run / "stdout").write_text('{"is_error": false, "result": "done"}')
        (run / "stderr").write_text("")
        self.report(run)
        (run / "exit.json").write_text('{"exit_code": 0}')
        worker.collect(self.db, self.root, lambda: NOW + timedelta(minutes=1))
        self.assertEqual(self.db.segments()[-1].outcome, "done")

    def test_collect_refuses_evidence_redirected_inside_the_tree(self):
        self.launched()
        data = self.db.resume(KEY)
        data["runs"]["implement-1"]["run_dir"] = str(self.tree)
        self.db.set_resume(KEY, data)
        worker.collect(self.db, self.root, lambda: NOW)
        self.assertEqual(self.db.segments()[0].outcome, "failed")

    def test_real_detached_adapter_is_adopted_and_collected_after_engine_restart(self):
        fake = Path(__file__).parent / "fixtures" / "fake_claude.py"
        cli = self.root / "fake-cli"
        cli.write_text(f"#!{sys.executable}\nimport time, runpy\ntime.sleep(0.5)\n"
                       f"runpy.run_path({str(fake.resolve())!r}, run_name='__main__')\n")
        cli.chmod(0o755)
        # The adapter still resolves through its registry, and uses its real argv builder and runner.
        with mock.patch.object(worker.make_worktree, "build", return_value=SimpleNamespace(path=self.tree)), \
             mock.patch.object(claude_adapter, "resolve", return_value=str(cli)), \
             mock.patch.object(worker.process_status, "identity", return_value="fake-start"), \
             mock.patch.dict(os.environ, {"SHELL": "/bin/sh"}):
            self.launch()
        record = self.db.resume(KEY)["runs"]["implement-1"]
        runner = worker._runners.pop(record["run_dir"])
        self.addCleanup(lambda: claude_adapter.cancel(runner) if runner.poll() is None else None)
        data = self.db.resume(KEY)
        data.pop("runs")
        self.db.set_resume(KEY, data)
        with mock.patch.object(worker.claude_adapter, "start") as start, \
             mock.patch.object(worker.process_status, "identity", return_value="fake-start"):
            slots.claim(self.trees, self.db, issue_key=KEY, stage="implement", attempt=1,
                        max_concurrency=1, clock=lambda: NOW, launch=self.launch)
        start.assert_not_called()
        self.assertEqual(self.db.resume(KEY)["runs"]["implement-1"], record)
        runner.wait(timeout=5)
        self.report(Path(record["run_dir"]))
        with mock.patch.object(worker.process_status, "identity", return_value=None):
            worker.collect(self.db, self.root, lambda: NOW + timedelta(minutes=1))
        self.assertEqual(self.db.segments()[0].outcome, "done")
        command = json.loads((Path(record["run_dir"]) / "command.json").read_text())["argv"]
        for token in ("-p", "--output-format", "stream-json", "--input-format", "--replay-user-messages",
                      "--model", "example", "--session-id", record["session_id"]):
            self.assertIn(token, command)
        self.assertNotIn("--safe-mode", command)

    def test_worker_launch_attaches_guard_plugin_and_chief_binding(self):
        guard = (Path(worker.guard_binding.__file__).resolve().parent.parent / "guard").resolve()
        self.assertTrue(guard.is_dir())
        with mock.patch.object(worker.make_worktree, "build", return_value=SimpleNamespace(path=self.tree)), \
             mock.patch.object(worker.claude_adapter, "start", side_effect=self.start) as start, \
             mock.patch.object(worker.process_status, "identity", return_value="start"):
            self.launch()
        request = start.call_args.args[0]
        env = start.call_args.kwargs["env"]
        self.assertEqual(request.plugin_dirs, (str(guard),))
        self.assertEqual(env["CHIEF_OF_STUFF_WORKSPACE"], str(self.root))
        self.assertEqual(env["CHIEF_ISSUE_KEY"], KEY)
        self.assertEqual(env["CHIEF_STAGE"], "implement")
        self.assertEqual(env["CHIEF_ATTEMPT"], "1")
        self.assertEqual(env["CHIEF_ROOT"], str(self.root))
        self.assertEqual(env["CHIEF_SESSION_ID"], request.session_id)
        self.assertEqual(env["CHIEF_LAUNCHED_AT"], NOW.isoformat())
        self.assertEqual(env["CHIEF_PYTHON"], sys.executable)
        self.assertEqual(env["CHIEF_PROTECTED_BRANCHES"], '["main", "master", "develop", "trunk"]')
        argv = claude_adapter.argv(request, "claude", [])
        self.assertEqual([argv[i + 1] for i, x in enumerate(argv) if x == "--plugin-dir"], [str(guard)])
        self.assertEqual(env["CHIEF_ISSUE_KEY"], KEY)

    def test_unreadable_protected_branches_refuse_the_start(self):
        def broken():
            raise worker.ForgeError("protected branches: rate limited")
        self.forge.protected_branches = broken
        with mock.patch.object(worker.make_worktree, "build", return_value=SimpleNamespace(path=self.tree)), \
             mock.patch.object(worker.claude_adapter, "start") as start:
            with self.assertRaisesRegex(worker.ForgeError, "protected branches"):
                self.launch()
        start.assert_not_called()

    def test_collect_recovers_a_reservation_whose_run_record_was_never_saved(self):
        for exited in (False, True):
            with self.subTest(exited=exited):
                if self.db.resume(KEY):
                    self.db.engine.dispose()
                    self.setUp()
                run = self.launched()
                data = self.db.resume(KEY)
                saved = data.pop("runs")["implement-1"]
                self.db.set_resume(KEY, data)
                if exited:
                    (run / "stdout").write_text('{"is_error": false, "result": "done"}')
                    (run / "stderr").write_text("")
                    self.report(run)
                    (run / "exit.json").write_text('{"exit_code": 0}')
                with mock.patch.object(worker.process_status, "identity", return_value="process-start"):
                    worker.collect(self.db, self.root, lambda: NOW, trees=self.trees)
                segment = self.db.segments()[0]
                self.assertEqual(segment.outcome, "done" if exited else None)
                self.assertEqual(self.db.resume(KEY)["runs"]["implement-1"]["session_id"], saved["session_id"])
                self.assertEqual(list(run.parent.glob(f"{run.name}-refused-*")), [])

    def test_collect_keeps_a_command_only_start_reserved(self):
        # Engine restart between Popen and started.json must not fail the reservation.
        run = worker._run_dir(self.root, KEY, "implement", 1)
        run.mkdir(parents=True)
        (run / "command.json").write_text("{}")
        self.db.record_segment(KEY, "implement", 1, NOW.isoformat())
        worker.collect(self.db, self.root, lambda: NOW, trees=self.trees)
        seg = self.db.segments(KEY)[0]
        self.assertIsNone(seg.ended_at)
        self.assertIsNone(seg.outcome)

    def test_collect_still_fails_a_reservation_with_no_start_evidence(self):
        self.db.record_segment(KEY, "implement", 1, NOW.isoformat())
        worker.collect(self.db, self.root, lambda: NOW, trees=self.trees)
        self.assertEqual(self.db.segments()[0].outcome, "failed")

    def test_worker_launch_refuses_without_the_guard_plugin(self):
        with mock.patch.object(worker.guard_binding, "guard_plugin_dir", return_value=None), \
             mock.patch.object(worker.make_worktree, "build", return_value=SimpleNamespace(path=self.tree)), \
             mock.patch.object(worker.claude_adapter, "start") as start:
            with self.assertRaisesRegex(RuntimeError, "guard/ plugin"):
                self.launch()
        start.assert_not_called()

    def test_adapter_runner_gives_the_host_the_chief_env(self):
        run = self.root / "run"
        run.mkdir()
        out = self.root / "seen"
        code = f"import os; open({str(out)!r}, 'w').write(os.environ['CHIEF_ISSUE_KEY'])"
        (run / "command.json").write_text(json.dumps({
            "argv": [sys.executable, "-c", code], "cwd": str(self.root), "session_id": "s",
            "env": {"CHIEF_ISSUE_KEY": KEY}}))
        claude_adapter._worker(run)
        self.assertEqual(out.read_text(), KEY)

    def test_agent_effort_profile_are_passed_to_adapter(self):
        (self.root / "CLAUDE.md").write_text(
            "## Coordinator\n- User: Robin\n- Agent: specialist task\n")
        self.p.personas["builder"] = SimpleNamespace(name="builder", role="build", rules=(),
                                                       agent="specialist", model="opus", effort="high")
        with mock.patch.object(worker.make_worktree, "build", return_value=SimpleNamespace(path=self.tree)), \
             mock.patch.object(worker.claude_adapter, "start", side_effect=self.start) as start, \
             mock.patch.object(worker.process_status, "identity", return_value="start"):
            used = worker.launch(KEY, "implement", 1, "builder", store=self.db, root=self.root,
                                 trees=self.trees, policies=self.p, forge=self.forge, claude_as_profile="secondary")
        self.assertEqual(used, ("opus", "high"))
        self.assertEqual(start.call_args.args[0].agent, "specialist")
        self.assertEqual(start.call_args.kwargs["claude_as_profile"], "secondary")

    def test_review_stage_configuration_reaches_engine_worker(self):
        for declared in (False, True):
            with self.subTest(declared=declared):
                (self.root / "CLAUDE.md").write_text(
                    "## Coordinator\n- Settings: `chief-of-stuff.toml`\n"
                    + ("- Agent: reviewer task\n" if declared else ""))
                (self.root / "chief-of-stuff.toml").write_text(
                    '[lanes.code]\nstages = ["review"]\n'
                    '[stages.review]\nagent = "reviewer"\nskills = ["security-review"]\n')
                self.p.personas["critic"] = SimpleNamespace(
                    name="critic", role="reviews", rules=(), agent="", model="example", effort="")

                def cut(*args, **kwargs):
                    worker.make_worktree.check_type((self.root / "CLAUDE.md").read_text(), args[5])
                    return SimpleNamespace(path=self.tree)

                with mock.patch.object(worker.make_worktree, "build", side_effect=cut) as build, \
                     mock.patch.object(worker.claude_adapter, "start", side_effect=self.start) as start, \
                     mock.patch.object(worker.process_status, "identity", return_value="start"):
                    self.launch("review", attempt=1 + int(declared))
                request = start.call_args.args[0]
                self.assertEqual(request.agent, "reviewer" if declared else "")
                self.assertIn("Skills: load and follow security-review", request.prompt)
                self.assertEqual(build.call_args.args[5], request.agent)

    def test_restart_adopts_run_despite_broken_launch_configuration(self):
        for completed in (False, True):
            for broken in ("settings", "claude"):
                with self.subTest(completed=completed, broken=broken):
                    if self.db.resume(KEY):
                        self.db.engine.dispose()
                        self.setUp()
                    with mock.patch.object(worker.make_worktree, "build", return_value=SimpleNamespace(path=self.tree)), \
                         mock.patch.object(worker.claude_adapter, "start", side_effect=self.start), \
                         mock.patch.object(worker.process_status, "identity", return_value="process-start"):
                        self.launch()
                    record = self.db.resume(KEY)["runs"]["implement-1"]
                    if completed:
                        (Path(record["run_dir"]) / "exit.json").write_text('{"exit_code": 0}')
                    if broken == "settings":
                        (self.root / "CLAUDE.md").write_text("## Coordinator\n- Settings: chief-of-stuff.toml\n")
                        (self.root / "chief-of-stuff.toml").write_text("invalid toml [")
                    else:
                        (self.root / "CLAUDE.md").write_bytes(b"\xff")
                    with mock.patch.object(worker.claude_adapter, "start") as start, \
                         mock.patch.object(worker.make_worktree, "build") as build, \
                         mock.patch.object(worker.process_status, "identity", return_value="process-start"):
                        slots.claim(self.trees, self.db, issue_key=KEY, stage="implement", attempt=1,
                                    max_concurrency=1, clock=lambda: NOW, launch=self.launch)
                    self.assertEqual(slots.running(self.db), 1)
                    self.assertEqual(self.db.resume(KEY)["runs"]["implement-1"], record)
                    start.assert_not_called()
                    build.assert_not_called()

    def test_undeclared_agent_is_omitted_so_default_agent_runs(self):
        (self.root / "CLAUDE.md").write_text(
            "## Coordinator\n- User: Robin\n- Agent: implementer task\n")
        self.p.personas["builder"] = SimpleNamespace(name="builder", role="build", rules=(),
                                                       agent="specialist", model="opus", effort="")

        def cut(*args, **kwargs):
            # Real check_type: empty type must pass when Agent: lines exist (Codex on #675).
            worker.make_worktree.check_type((self.root / "CLAUDE.md").read_text(), args[5])
            return SimpleNamespace(path=self.tree)

        with mock.patch.object(worker.make_worktree, "build", side_effect=cut) as build, \
             mock.patch.object(worker.claude_adapter, "start", side_effect=self.start) as start, \
             mock.patch.object(worker.process_status, "identity", return_value="start"):
            worker.launch(KEY, "implement", 1, "builder", store=self.db, root=self.root,
                          trees=self.trees, policies=self.p, forge=self.forge)
        # Cut and request share the declared-only type; never actor.name ("builder").
        self.assertEqual(build.call_args.args[5], "")
        self.assertEqual(start.call_args.args[0].agent, "")

    def test_adapter_worker_argv_enables_tools_and_named_agent(self):
        request = worker.Request("prompt", worker=True, agent="specialist", effort="high")
        args = claude_adapter.argv(request, "/fake", [])
        self.assertIn("--agent", args)
        self.assertIn("specialist", args)
        self.assertNotIn("--safe-mode", args)
        self.assertEqual(args[args.index("--setting-sources") + 1], "user")
        self.assertIn("--strict-mcp-config", args)

    def test_a_workers_own_engine_tools_need_no_permission(self):
        # Declared through --mcp-config, the report tool asked for permission; the trio answered after the guard
        # hook's 40s wait, so the worker was denied and "stopped without reporting" (drago#19 plan-4).
        guard = Path(claude_adapter.__file__).resolve().parents[1] / "guard"
        args = claude_adapter.argv(worker.Request("prompt", worker=True, plugin_dirs=(str(guard),)), "/fake", [])
        self.assertEqual(args[args.index("--allowedTools") + 1], "mcp__engine")
        self.assertLess(args.index("--allowedTools"), args.index("--"))

    def test_a_worker_keeps_its_plugins_mcp_servers_under_strict_mcp_config(self):
        # Claude Code 2.1.303 drops plugin MCP servers under --strict-mcp-config, so every worker lost the engine's
        # report tool and "stopped without reporting an outcome". The guard's servers travel in --mcp-config.
        guard = Path(claude_adapter.__file__).resolve().parents[1] / "guard"
        args = claude_adapter.argv(worker.Request("prompt", worker=True, plugin_dirs=(str(guard),)), "/fake", [])
        config = json.loads(args[args.index("--mcp-config") + 1])
        self.assertEqual(config["mcpServers"]["engine"],
                         {"command": "${CHIEF_PYTHON}", "args": [f"{guard}/../scripts/engine_mcp.py"]})
        self.assertLess(args.index("--mcp-config"), args.index("--"))

    def test_a_plugin_path_with_a_quote_or_backslash_still_gives_valid_mcp_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            plugin = Path(tmp) / 'odd "dir" \\ name'
            plugin.mkdir()
            (plugin / ".mcp.json").write_text('{"engine": {"command": "x", "args": ["${CLAUDE_PLUGIN_ROOT}/s.py"]}}')
            args = claude_adapter.argv(worker.Request("p", worker=True, plugin_dirs=(str(plugin),)), "/fake", [])
        config = json.loads(args[args.index("--mcp-config") + 1])
        self.assertEqual(config["mcpServers"]["engine"]["args"], [f"{plugin}/s.py"])

    def test_forge_retains_request_head_branch(self):
        payload = [{"number": 1, "title": "Request", "url": "url", "headRefOid": "sha",
                    "headRefName": "feature/existing"}]
        gh = mock.Mock(return_value=(0, json.dumps(payload), ""))
        self.assertEqual(forge_review.github_requests("example/project", "", gh)[0].branch, "feature/existing")
        self.assertIn("headRefName", gh.call_args.args[0][-1])

    def test_collect_records_host_spend_and_elapsed_minutes(self):
        run = self.launched()
        self.report(run)
        (run / "stdout").write_text(json.dumps({"is_error": False, "result": "done", "total_cost_usd": 0.25}))
        (run / "stderr").write_text("")
        (run / "exit.json").write_text(json.dumps({"exit_code": 0, "ended_at": (NOW + timedelta(minutes=1)).isoformat()}))
        with mock.patch.object(worker.process_status, "identity", return_value=None):
            worker.collect(self.db, self.root, lambda: NOW + timedelta(minutes=5))
        self.assertEqual(self.db.resume(KEY)["spend_usd"], 0.25)
        self.assertEqual(self.db.resume(KEY)["time_minutes"], 1)
        # A failed exit with total_cost_usd and a non-numeric prior spend still ends the segment and records spend.
        self.db.record_segment(KEY, "review", 1, NOW.isoformat())
        run2 = supervisor.state_dir(self.root) / "runs" / "1-review-1"
        run2.mkdir(parents=True)
        data = self.db.resume(KEY)
        data["spend_usd"] = "corrupt"
        data["runs"]["review-1"] = {"pid": 77, "runner_pid": 78, "session_id": "s2", "identity": "id",
                                    "runner_identity": "rid", "run_dir": str(run2), "tree": str(self.tree),
                                    "reviewed_head": "head"}
        self.db.set_resume(KEY, data)
        (run2 / "stdout").write_text(json.dumps({"is_error": True, "result": "boom", "total_cost_usd": 0.4}))
        (run2 / "stderr").write_text("")
        (run2 / "exit.json").write_text(json.dumps({"exit_code": 1}))
        with mock.patch.object(worker.process_status, "identity", return_value=None):
            worker.collect(self.db, self.root, lambda: NOW + timedelta(minutes=2))
        self.assertEqual(self.db.segments(KEY)[-1].outcome, "failed")
        self.assertEqual(self.db.resume(KEY)["spend_usd"], 0.4)

    def test_quota_reset_formats_and_backoff(self):
        from model_port import retry_at
        self.assertEqual(retry_at("resets at 5pm.", NOW), NOW + timedelta(hours=17))
        self.assertEqual(retry_at("resets in about four hours.", NOW), NOW + timedelta(hours=4))
        self.assertEqual(retry_at("reset at 2000-01-01T02:00:00Z", NOW), NOW + timedelta(hours=2))
        self.assertEqual(retry_at("unknown", NOW, 4), NOW + timedelta(minutes=8))
        self.assertEqual(retry_at("reset at 2000-99-99T02:00:00Z", NOW), NOW + timedelta(minutes=1))

    def test_collection_retries_usage_once_when_segment_completion_fails(self):
        run = self.launched()
        self.report(run)
        (run / "stdout").write_text(json.dumps({"is_error": False, "result": "done", "total_cost_usd": 0.25}))
        (run / "stderr").write_text("")
        (run / "exit.json").write_text(json.dumps({"exit_code": 0, "ended_at": (NOW + timedelta(minutes=1)).isoformat()}))
        with mock.patch.object(self.db, "end_segment", side_effect=RuntimeError("interrupted")):
            with self.assertRaises(RuntimeError):
                worker.collect(self.db, self.root, lambda: NOW + timedelta(minutes=5))
        self.assertEqual(self.db.resume(KEY)["spend_usd"], 0.25)
        worker.collect(self.db, self.root, lambda: NOW + timedelta(minutes=10))
        self.assertEqual(self.db.resume(KEY)["spend_usd"], 0.25)
        self.assertEqual(self.db.resume(KEY)["time_minutes"], 1)
        self.assertEqual(self.db.segments(KEY)[0].outcome, "done")

    def test_observed_quota_exhaustion_records_a_limit_measurement(self):
        self.finish("quota")
        worker.collect(self.db, self.root, lambda: NOW + timedelta(minutes=1))
        self.assertEqual(self.db.resume(KEY)["quota_percent"], 100)


class IdentityTest(unittest.TestCase):
    def test_host_identity_waits_for_login_shell_to_exec(self):
        output = 'S Sat Jan 1 00:00:00 2000 /bin/zsh -lic exec "$@" worker /fake --session-id session'
        with mock.patch.object(process_status, "process_exists", return_value=True), \
             mock.patch.object(process_status.subprocess, "run", return_value=SimpleNamespace(returncode=0, stdout=output)):
            self.assertIsNone(process_status.identity(12, "session", host=True))

    def test_live_process_identity(self):
        try:
            process_status.identity(os.getpid(), "probe-session")
        except PermissionError:
            self.skipTest("sandbox blocks ps, including through the configured login shell")
        session = "test-worker-session"
        process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(5)", session])
        try:
            # A launcher may re-exec (macOS venv python does), so poll as worker.launch does.
            deadline = time.monotonic() + 5
            while process_status.identity(process.pid, session) is None and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertIsNotNone(process_status.identity(process.pid, session))
            self.assertIsNone(process_status.identity(process.pid, "wrong-session"))
        finally:
            process.terminate()
            process.wait(timeout=5)

    def test_pid_exists_but_wrong_session_or_zombie_is_not_identity(self):
        for output in ("S Sat Jan 1 00:00:00 2000 other-session", "Z Sat Jan 1 00:00:00 2000 session"):
            with mock.patch.object(process_status, "process_exists", return_value=True), \
                 mock.patch.object(process_status.subprocess, "run", return_value=SimpleNamespace(returncode=0, stdout=output)):
                self.assertIsNone(process_status.identity(12, "session"))


if __name__ == "__main__":
    unittest.main()
