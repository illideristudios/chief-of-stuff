"""Worker guard gates, exercised through the same JSON command interface as the host."""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import store

KEY = "github:example/repo#12"


class GuardTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        store.migrate(self.root / "store.db")
        self.db = store.Store(self.root / "store.db")
        self.addCleanup(self.db.engine.dispose)
        self.db.upsert_item(KEY, title="Implement a widget", stage="implement")
        self.env = {**os.environ, "CHIEF_ISSUE_KEY": KEY, "CHIEF_STAGE": "implement",
                    "CHIEF_ATTEMPT": "2", "CHIEF_SESSION_ID": "session-two", "CHIEF_ROOT": str(self.root),
                    "CHIEF_LAUNCHED_AT": datetime.now(timezone.utc).isoformat(),
                    "CHIEF_PYTHON": sys.executable, "CHIEF_PROTECTED_BRANCHES": '["main", "master", "develop", "release"]'}

    def hook(self, event, **fields):
        proc = subprocess.run([sys.executable, str(ROOT / "scripts/worker_guard.py"), event],
                              input=json.dumps({"hook_event_name": event, **fields}),
                              text=True, capture_output=True, env=self.env, timeout=10)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return json.loads(proc.stdout) if proc.stdout.strip() else {}

    def denied(self, command):
        result = self.hook("PreToolUse", tool_name="Bash", tool_input={"command": command})
        self.assertEqual(result["hookSpecificOutput"]["permissionDecision"], "deny", command)

    def test_merge_wrappers_and_chains_cannot_pass(self):
        for command in ("gh pr merge 12 --squash", "glab mr merge 12", "git merge topic",
                        "env FLAG=yes /usr/bin/gh pr merge 12", "sh -c 'gh pr merge 12'",
                        "bash -lc 'env FLAG=yes gh pr merge 12'", "true && gh pr merge 12",
                        "true\ngh pr merge 12", "command gh pr merge 12", "sudo -n gh pr merge 12",
                        "gh pr merge 'unterminated", "eval 'gh pr merge 12'",
                        "g'h' pr merge 12", "$(printf gh) pr merge 12"):
            with self.subTest(command=command):
                self.denied(command)

    def test_protected_or_ambiguous_push_cannot_pass(self):
        for command in ("git push origin main", "git push origin HEAD:refs/heads/develop",
                        "git -C /tmp push origin +topic:release", "git push", "git push --all",
                        "git push origin :main", "git push --mirror origin", "git push origin 'refs/heads/*:refs/heads/*'"):
            with self.subTest(command=command):
                self.denied(command)

    def test_review_command_bypasses_cannot_pass(self):
        for command in ('git rebase -x "gh pr merge 12" HEAD~1',
                        'gh pr create --title x --body y',
                        'gh pr create --head main --title x --body y',
                        'gh pr create --head feat/x --title x --body y --project Roadmap',
                        'glab mr create --push --title x', 'glab mr create --fill --title x'):
            with self.subTest(command=command):
                self.denied(command)
        result = self.hook("PreToolUse", tool_name="Bash",
                           tool_input={"command": 'gh pr create --title x --body y'})
        self.assertIn('Push with git first', result["hookSpecificOutput"]["permissionDecisionReason"])
        self.assertIn('--head', result["hookSpecificOutput"]["permissionDecisionReason"])

    def test_forge_label_status_close_and_ready_cannot_pass(self):
        for command in ("gh issue edit 12 --add-label stage:done", "gh pr edit 12 --remove-label stage:review",
                        "glab issue update 12 --label stage:done", "glab mr update 12 --unlabel stage:review",
                        "gh project item-edit --id item --field-id status --single-select-option-id done",
                        "gh issue close 12", "glab issue close 12", "gh pr ready 12", "glab mr update 12 --ready",
                        "gh api repos/example/repo/issues/12 -X PATCH -f state=closed",
                        "gh api graphql -f query='mutation { updateProjectV2ItemFieldValue }'"):
            with self.subTest(command=command):
                self.denied(command)

    def test_review_findings_and_unknown_forge_verbs_cannot_pass(self):
        from guard_commands import OWNED
        for command in ('gh label delete stage:done --yes', 'gh label edit stage:done --name x',
                        'gh project item-archive 1 --owner owner --id item',
                        'gh project item-add 1 --owner owner --url https://github.com/o/r/issues/12',
                        'gh repo sync owner/repo --branch main --force',
                        '/usr/lib/git-core/git-push origin HEAD:main', 'git-merge topic',
                        'gh pr frobnicate', 'gh pr review 12 --approve', 'gh pr review 12 -a',
                        'gh pr merge --help', 'gh pr create --label stage:done --title --help --body x',
                        'gh pr create -dlstage:done --title x --body y', 'glab mr create -ad --title x'):
            with self.subTest(command=command):
                result = self.hook("PreToolUse", tool_name="Bash", tool_input={"command": command})
                output = result["hookSpecificOutput"]
                self.assertEqual(output["permissionDecision"], "deny", command)
                self.assertEqual(output["permissionDecisionReason"], OWNED, command)

    def test_read_and_feature_work_remain_available(self):
        for command in ("gh pr view 12", "glab issue view 12", "git status && git diff",
                        "git push -u origin HEAD:feat/widget", "git push origin feat/widget",
                        "python -m unittest evals.test_store", "gh api repos/example/repo/issues/12",
                        "gh label list", "gh project item-list 1 --owner owner",
                        "gh pr create --head feat/widget --title widget --body details", "gh pr comment 12 --body feedback",
                        "gh issue comment 12 --body feedback", "gh pr review 12 --comment --body feedback",
                        "gh pr review 12 --comment --body feedback", "gh pr checkout 12",
                        "glab mr create --title widget", "glab mr note 12 --message feedback",
                        "glab issue note 12 --message feedback", "glab mr checkout 12",
                        "git-push origin HEAD:feat/widget", "gh pr create --help", "gh pr create -H feat/x -t x -b y",
                        "git rebase origin/develop"):
            with self.subTest(command=command):
                self.assertEqual(self.hook("PreToolUse", tool_name="Bash", tool_input={"command": command}), {})

    def test_plain_http_forge_access_cannot_pass(self):
        for command in ('curl -X PATCH https://api.github.com/repos/o/r/issues/12 -d state=closed',
                        'wget https://uploads.github.com/repos/o/r/releases/12/assets',
                        'http GET https://github.example.com/api/v3/repos/o/r/issues/12',
                        'https GET https://github.example.com/api/graphql',
                        'python request.py https://labs.example.test/api/v4/projects/12/issues/3',
                        'node request.js https://api.github.com/repos/o/r/issues/12'):
            with self.subTest(command=command):
                self.denied(command)
        for command in ('curl https://example.com', 'echo https://api.github.com',
                        'printf "%s" https://uploads.github.com'):
            with self.subTest(command=command):
                self.assertEqual(self.hook("PreToolUse", tool_name="Bash", tool_input={"command": command}), {})

    def test_ask_user_question_is_relayed_to_mcp(self):
        result = self.hook("PreToolUse", tool_name="AskUserQuestion", tool_input={"questions": []})
        self.assertEqual(result["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertIn("ask", result["hookSpecificOutput"]["permissionDecisionReason"])

    def test_unrelated_tools_are_ignored(self):
        self.assertEqual(self.hook("PreToolUse", tool_name="Read", tool_input={"file_path": "README.md"}), {})

    def test_permission_is_an_ask_and_never_an_approval(self):
        result = self.hook("PermissionRequest", tool_name="Bash", tool_input={"command": "touch /outside/file"})
        self.assertEqual(result["hookSpecificOutput"]["decision"]["behavior"], "deny")
        asks = self.db.asks(KEY)
        self.assertEqual(len(asks), 1)
        self.assertEqual((asks[0].stage, asks[0].attempt, asks[0].session_id, asks[0].answer),
                         ("implement", 2, "session-two", None))
        self.assertIn("touch /outside/file", asks[0].context)
        self.assertNotIn("ask tool", json.dumps(result))  # already recorded; a second ask would duplicate it

    def test_stop_requires_this_exact_run_report_and_cannot_loop(self):
        self.assertEqual(self.hook("Stop")["decision"], "block")
        self.db.add_report(KEY, "implement", 1, "session-one", "done", "old", [])
        self.db.add_report(KEY, "implement", 2, "another-session", "done", "other", [])
        self.assertEqual(self.hook("Stop")["decision"], "block")
        self.assertEqual(self.hook("Stop", stop_hook_active=True), {})
        self.db.add_report(KEY, "implement", 2, "session-two", "blocked", "waiting", [])
        self.assertEqual(self.hook("Stop"), {})

    def test_session_start_injects_state_reports_questions_and_answers(self):
        self.db.add_report(KEY, "implement", 1, "session-one", "failed", "earlier result", [])
        ask_id = self.db.add_ask(KEY, "implement", 1, "session-one", "Which widget?", ["small"], "size")
        with self.db.session() as session:
            session.get(store.Ask, ask_id).answer = "small"
        self.db.add_ask(KEY, "implement", 2, "session-two", "What colour?", [], "appearance")
        result = self.hook("SessionStart", session_id="forged")
        state = json.loads(result["hookSpecificOutput"]["additionalContext"])
        self.assertEqual((state["issue_key"], state["title"], state["stage"], state["attempt"]),
                         (KEY, "Implement a widget", "implement", 2))
        self.assertEqual([a["answer"] for a in state["asks"]], ["small", None])
        self.assertEqual(state["reports"][0]["summary"], "earlier result")

    def test_stop_requires_a_report_since_the_latest_launch(self):
        self.env["CHIEF_LAUNCHED_AT"] = "2026-10-11T12:00:00+00:00"
        # Its local time sorts later than the launch string, but this instant is earlier.
        self.db.clock = lambda: datetime.fromisoformat("2026-10-11T13:59:59+02:00")
        self.db.add_report(KEY, "implement", 2, "session-two", "done", "before resume", [])
        self.assertEqual(self.hook("Stop")["decision"], "block")
        # Its local time sorts earlier than the launch string, but this instant is later.
        self.db.clock = lambda: datetime.fromisoformat("2026-10-11T07:00:01-05:00")
        self.db.add_report(KEY, "implement", 2, "session-two", "done", "after resume", [])
        self.assertEqual(self.hook("Stop"), {})

    def test_stop_accepts_a_report_at_the_launch_instant(self):
        self.env["CHIEF_LAUNCHED_AT"] = "2026-10-11T12:00:00Z"
        self.db.clock = lambda: datetime.fromisoformat("2026-10-11T12:00:00+00:00")
        self.db.add_report(KEY, "implement", 2, "session-two", "blocked", "waiting", [])
        self.assertEqual(self.hook("Stop"), {})

    def test_missing_binding_denies_instead_of_skipping_gate(self):
        for field in ("CHIEF_ISSUE_KEY", "CHIEF_LAUNCHED_AT"):
            with self.subTest(field=field):
                value = self.env.pop(field)
                try:
                    self.denied("git status")
                    result = self.hook("Stop")
                    self.assertEqual(result["decision"], "block")
                    self.assertIn(field, result["reason"])
                    self.assertEqual(self.hook("Stop", stop_hook_active=True), {})
                finally:
                    self.env[field] = value

    def test_invalid_launch_time_denies_instead_of_skipping_gate(self):
        for value in ("", "not-a-time", "2026-10-11T12:00:00"):
            with self.subTest(value=value):
                self.env["CHIEF_LAUNCHED_AT"] = value
                self.denied("git status")
                self.assertEqual(self.hook("Stop")["decision"], "block")

    def test_guard_env_uses_the_engine_clock_for_each_launch(self):
        import guard_binding
        for instant in (datetime(2026, 10, 11, 12, tzinfo=timezone.utc),
                        datetime(2026, 10, 11, 13, tzinfo=timezone.utc)):
            with self.subTest(instant=instant), patch("clock.system", return_value=instant):
                env = guard_binding.guard_env(KEY, "implement", 2, "session-two", self.root)
                self.assertEqual(env["CHIEF_LAUNCHED_AT"], instant.isoformat())
                self.assertEqual(guard_binding.Binding.from_env(env).launched_at, instant)

    def test_plugin_uses_engine_python_and_exposes_launch_contract(self):
        import guard_binding
        self.assertEqual(guard_binding.guard_plugin_dir(), ROOT / "guard")
        import start_coordinator
        shipped = start_coordinator.files(ROOT)
        for name in ("guard/.mcp.json", "guard/hooks/hooks.json", "guard/.claude-plugin/plugin.json"):
            self.assertIn(ROOT / name, shipped)  # installed releases carry the plugin guard_plugin_dir names
        env = guard_binding.guard_env(KEY, "implement", 2, "session-two", self.root)
        self.assertEqual(env["CHIEF_PYTHON"], sys.executable)
        self.assertEqual(guard_binding.Binding.from_env(env).issue_key, KEY)
        config = json.loads((ROOT / "guard/.mcp.json").read_text())
        self.assertEqual(config["engine"]["command"], "${CHIEF_PYTHON}")
        hooks = json.loads((ROOT / "guard/hooks/hooks.json").read_text())["hooks"]
        self.assertEqual(set(hooks), {"PreToolUse", "PermissionRequest", "Stop", "SessionStart"})
        self.assertEqual(hooks["PreToolUse"][0]["matcher"], "Bash|AskUserQuestion")
        for event, entries in hooks.items():
            self.assertIn("${CHIEF_PYTHON}", entries[0]["hooks"][0]["command"])


if __name__ == "__main__":
    unittest.main()
