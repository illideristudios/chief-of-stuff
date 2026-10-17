"""Clock dependency contract and opt-in real worker proof (M3.8, #636).

CHIEF_CLOCK_LIVE=1 [CHIEF_CLOCK_PROFILE=secondary] python3 -m unittest evals.test_worker_clock.LiveClockTest -v
The live test makes two billed calls with the production worker profile; unit tests never call a model.
"""

import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
import uuid
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import claude_adapter
from model_port import Reply, Request
from shell_setup import ShellError

CLOCK = {"id": "datetime-inject@ai-additions", "scope": "user", "enabled": True, "version": "1.0.0"}


class ClockDependencyTest(unittest.TestCase):
    def test_both_manifests_declare_the_owner_approved_dependency(self):
        plugin = json.loads((ROOT / ".claude-plugin/plugin.json").read_text())
        marketplace = json.loads((ROOT / ".claude-plugin/marketplace.json").read_text())
        entry = next(p for p in marketplace["plugins"] if p["name"] == plugin["name"])
        dependency = {"name": "datetime-inject", "marketplace": "ai-additions", "version": "^1.0"}
        self.assertIn(dependency, plugin.get("dependencies", []))
        self.assertIn(dependency, entry.get("dependencies", []))
        self.assertIn("ai-additions", marketplace.get("allowCrossMarketplaceDependenciesOn", []))


class ClockPreflightTest(unittest.TestCase):
    def setUp(self):
        patch = mock.patch.object(claude_adapter, "resolve", return_value="/bin/host")
        patch.start()
        self.addCleanup(patch.stop)
        self.request = Request(prompt="work", worker=True, cwd="/worktree", plugin_dirs=("/guard",))

    def result(self, plugins):
        return subprocess.CompletedProcess([], 0, json.dumps(plugins), "")

    def test_preflight_uses_the_same_account_shell_and_setting_sources_as_the_worker(self):
        for profile, lead in (("", []), ("secondary", ["run", "secondary"])):
            with self.subTest(profile=profile), mock.patch.object(
                    claude_adapter.subprocess, "run", return_value=self.result([CLOCK])) as run:
                command = claude_adapter.command(self.request, profile)
                run.assert_called_once_with(
                    claude_adapter.login_argv(["/bin/host", *lead, "--setting-sources", "user", "plugin", "list", "--json"]),
                    env=claude_adapter.clean_env(), cwd="/worktree", stdin=subprocess.DEVNULL,
                    capture_output=True, text=True, timeout=15)
                self.assertEqual(command, claude_adapter.login_argv(claude_adapter.argv(self.request, "/bin/host", lead)))

    def test_missing_disabled_wrong_marketplace_and_project_only_clocks_refuse_launch(self):
        for plugins in ([], [{**CLOCK, "enabled": False}], [{**CLOCK, "scope": "project"}],
                        [{**CLOCK, "id": "datetime-inject@other"}]):
            with self.subTest(plugins=plugins), mock.patch.object(
                    claude_adapter.subprocess, "run", return_value=self.result(plugins)):
                with self.assertRaisesRegex(ShellError, "datetime-inject@ai-additions.*enabled.*user"):
                    claude_adapter.command(self.request)

    def test_preflight_enforces_the_declared_range(self):
        for version in ("0.9.0", "2.0.0", "1.1.0-beta.1", "", "garbage"):
            with self.subTest(version=version), mock.patch.object(
                    claude_adapter.subprocess, "run", return_value=self.result([{**CLOCK, "version": version}])):
                with self.assertRaisesRegex(ShellError, r"\^1.0"):
                    claude_adapter.command(self.request)
        with mock.patch.object(claude_adapter.subprocess, "run", return_value=self.result([{**CLOCK, "version": "1.12.3"}])):
            claude_adapter.command(self.request)

    def test_an_unreadable_inventory_refuses_launch(self):
        outcomes = [subprocess.CompletedProcess([], 1, "", "failed"),
                    subprocess.CompletedProcess([], 0, "not JSON", ""), self.result({}), self.result([None]),
                    subprocess.TimeoutExpired("host", 15), OSError("cannot launch")]
        for outcome in outcomes:
            with self.subTest(outcome=outcome), mock.patch.object(claude_adapter.subprocess, "run") as run:
                if isinstance(outcome, Exception):
                    run.side_effect = outcome
                else:
                    run.return_value = outcome
                with self.assertRaisesRegex(ShellError, "clock preflight"):
                    claude_adapter.command(self.request)

    def test_a_tool_free_judgement_does_not_require_a_clock_plugin(self):
        with mock.patch.object(claude_adapter.subprocess, "run") as run:
            claude_adapter.command(Request(prompt="judge"))
        run.assert_not_called()


@unittest.skipUnless(os.environ.get("CHIEF_CLOCK_LIVE") == "1", "opt-in: two real Claude worker calls")
class LiveClockTest(unittest.TestCase):
    def test_worker_receives_one_fresh_bracketed_timestamp_per_prompt(self):
        import guard_binding
        import store

        with tempfile.TemporaryDirectory(prefix="chief-clock-") as tmp:
            root = Path(tmp).resolve()
            session = str(uuid.uuid4())
            now = datetime.now(timezone.utc)
            binding = guard_binding.Binding("github:example/repo#1", "implement", 1, session, root, now)
            store.migrate(binding.store_path)
            db = store.Store(binding.store_path)
            try:
                db.upsert_item(binding.issue_key, title="Clock delivery probe", stage="implement")
                request = Request(
                    prompt="Read the UserPromptSubmit hook output attached to THIS prompt. Return every bracketed "
                           "clock timestamp line from that output, verbatim, in the lines array. Preserve duplicates. "
                           "Exclude earlier prompts and your own replies. Use no tools and do nothing else.",
                    model="haiku", worker=True, cwd=str(root), session_id=session,
                    plugin_dirs=(str(guard_binding.guard_plugin_dir()),), timeout_s=120,
                    schema={"type": "object", "properties": {"lines": {"type": "array", "items": {"type": "string"}}},
                            "required": ["lines"], "additionalProperties": False})
                previous = None
                for _ in range(2):
                    # Production workers start a fresh session for each assignment. Keep this proof
                    # independent of session persistence (which may be unwritable in a sandbox).
                    session = str(uuid.uuid4())
                    binding = guard_binding.Binding(binding.issue_key, binding.stage, 1, session, root, now)
                    request = replace(request, session_id=session)
                    # The probe's setup satisfies Stop; the model only reads its hook context.
                    db.add_report(binding.issue_key, binding.stage, 1, session, "done", "Clock probe setup", [])
                    with mock.patch.dict(os.environ, guard_binding.guard_env(binding)):
                        before = datetime.now(timezone.utc)
                        reply = claude_adapter.call(request,
                                                    claude_as_profile=os.environ.get("CHIEF_CLOCK_PROFILE", ""))
                        after = datetime.now(timezone.utc)
                        self.assertIsInstance(reply, Reply, reply)
                        lines = reply.data["lines"]
                        self.assertEqual(len(lines), 1, reply)
                        match = re.fullmatch(
                            r"\[Current TimeStamp: .+ \| (\d{4}-\d\d-\d\d \d\d:\d\d:\d\d) .+ \(UTC([+-]\d\d:\d\d)\)\]", lines[0])
                        self.assertIsNotNone(match, lines)
                        stamp = datetime.fromisoformat(match[1] + match[2])
                        self.assertGreaterEqual(stamp.timestamp(), before.timestamp() - 1)
                        self.assertLessEqual(stamp.timestamp(), after.timestamp())
                        if previous is not None:
                            self.assertGreater(stamp, previous, "each worker prompt must get a new timestamp")
                        previous = stamp
            finally:
                db.engine.dispose()


if __name__ == "__main__":
    unittest.main()
