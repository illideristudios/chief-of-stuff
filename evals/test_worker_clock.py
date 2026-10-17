"""Clock dependency contract and opt-in real worker proof (M3.8, #636).

CHIEF_CLOCK_LIVE=1 [CHIEF_CLOCK_PROFILE=secondary] python3 -m unittest evals.test_worker_clock.LiveClockTest -v
The live test makes two billed calls with the production worker profile; unit tests never call a model.
"""

import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
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
        self.payload = tempfile.TemporaryDirectory()
        self.addCleanup(self.payload.cleanup)
        hooks = Path(self.payload.name) / "hooks"
        hooks.mkdir()
        (hooks / "hooks.json").write_text("{}")
        self.clock = {**CLOCK, "installPath": self.payload.name}
        self.request = Request(prompt="work", worker=True, cwd="/worktree", plugin_dirs=("/guard",))

    def popen(self, plugins, *, code=0, stderr="", fail=None):
        def spawn(argv, **kwargs):
            self.seen = (argv, kwargs)
            if isinstance(fail, OSError):
                raise fail
            body = plugins if isinstance(plugins, str) else json.dumps(plugins)
            kwargs["stdout"].write(body.encode())
            kwargs["stderr"].write(stderr.encode())
            proc = mock.Mock(pid=44)
            proc.wait.side_effect = fail if isinstance(fail, subprocess.TimeoutExpired) else None
            proc.wait.return_value = code
            return proc
        return mock.patch.object(claude_adapter.subprocess, "Popen", side_effect=spawn)

    def declared_range(self):
        plugin = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text())
        return next(item["version"] for item in plugin["dependencies"] if item["name"] == "datetime-inject")

    def test_preflight_reads_a_file_in_its_own_session_with_the_worker_account(self):
        for profile, lead in (("", []), ("secondary", ["run", "secondary"])):
            with self.subTest(profile=profile), self.popen([self.clock]):
                command = claude_adapter.command(self.request, profile)
                argv, kwargs = self.seen
                self.assertEqual(argv, claude_adapter.login_argv(
                    ["/bin/host", *lead, "--setting-sources", "user", "plugin", "list", "--json"]))
                self.assertEqual(kwargs["env"], claude_adapter.clean_env())
                self.assertEqual(kwargs["cwd"], "/worktree")
                self.assertIs(kwargs["stdin"], subprocess.DEVNULL)
                self.assertTrue(kwargs["start_new_session"])
                self.assertIsNot(kwargs["stdout"], subprocess.PIPE)
                self.assertIsNot(kwargs["stderr"], subprocess.PIPE)
                self.assertNotIn("capture_output", kwargs)
                self.assertEqual(command, claude_adapter.login_argv(claude_adapter.argv(self.request, "/bin/host", lead)))

    def test_missing_disabled_wrong_marketplace_and_project_only_clocks_refuse_launch(self):
        for plugins in ([], [{**self.clock, "enabled": False}], [{**self.clock, "scope": "project"}],
                        [{**self.clock, "id": "datetime-inject@other"}]):
            with self.subTest(plugins=plugins), self.popen(plugins):
                with self.assertRaisesRegex(claude_adapter.ClockError, "datetime-inject@ai-additions.*enabled.*user"):
                    claude_adapter.command(self.request)

    def test_a_deleted_or_empty_install_is_refused(self):
        empty = tempfile.mkdtemp()
        for install in (None, "", "/no/such/clock", empty, __file__):
            plugins = [{**self.clock, "installPath": install}] if install is not None else [{k: v for k, v in self.clock.items() if k != "installPath"}]
            with self.subTest(install=install), self.popen(plugins):
                with self.assertRaisesRegex(claude_adapter.ClockError, "hook payload"):
                    claude_adapter.command(self.request)

    def test_preflight_enforces_the_range_declared_in_the_manifest(self):
        spec = self.declared_range()
        match = re.fullmatch(r"\^(\d+)\.(\d+)(?:\.(\d+))?", spec)
        self.assertIsNotNone(match, spec)
        major, minor, patch = int(match[1]), int(match[2]), int(match[3] or 0)
        accept = [f"{major}.{minor}.{patch}", f"{major}.{minor}.{patch}+build.1"]
        reject = ["", "garbage", f"{major}.{minor}.{patch}-beta.1", f"{major + 1}.0.0"]
        if major:
            accept.append(f"{major}.{minor + 1}.0")
            reject.append(f"{major - 1}.9.9")
        for version in reject:
            with self.subTest(version=version), self.popen([{**self.clock, "version": version}]):
                with self.assertRaises(claude_adapter.ClockError) as raised:
                    claude_adapter.command(self.request)
                self.assertIn(spec, str(raised.exception))
        for version in accept:
            with self.subTest(version=version), self.popen([{**self.clock, "version": version}]):
                claude_adapter.command(self.request)

    def test_a_plugin_list_failure_keeps_the_cli_error(self):
        with self.popen([], code=2, stderr="not logged in\n"):
            with self.assertRaisesRegex(claude_adapter.ClockError, "not logged in"):
                claude_adapter.command(self.request)

    def test_a_preflight_timeout_kills_the_inventory_process_group(self):
        with self.popen([], fail=subprocess.TimeoutExpired("host", 15)), \
             mock.patch.object(claude_adapter.os, "killpg") as killpg:
            with self.assertRaisesRegex(claude_adapter.ClockError, "clock preflight"):
                claude_adapter.command(self.request)
        killpg.assert_called_once_with(44, signal.SIGKILL)

    def test_an_unreadable_inventory_refuses_launch(self):
        outcomes = [( [], 1, "failed"), ("not JSON", 0, ""), ({}, 0, ""), ([None], 0, ""),
                    subprocess.TimeoutExpired("host", 15), OSError("cannot launch")]
        for outcome in outcomes:
            fail = outcome if isinstance(outcome, Exception) else None
            plugins, code, stderr = ([], 0, "") if fail else outcome
            with self.subTest(outcome=outcome), self.popen(plugins, code=code, stderr=stderr, fail=fail), \
                 mock.patch.object(claude_adapter.os, "killpg"):
                with self.assertRaisesRegex(claude_adapter.ClockError, "clock preflight"):
                    claude_adapter.command(self.request)

    def test_a_refused_clock_is_not_a_missing_program(self):
        with self.popen([{**self.clock, "enabled": False}]):
            got = claude_adapter.call(self.request)
        self.assertEqual((got.kind, got.retryable), ("clock", False))

    def test_a_tool_free_judgement_does_not_require_a_clock_plugin(self):
        with mock.patch.object(claude_adapter.subprocess, "Popen") as popen:
            claude_adapter.command(Request(prompt="judge"))
        popen.assert_not_called()

    def test_fake_inventory_requires_user_sources_and_an_on_disk_payload(self):
        fake = ROOT / "evals" / "fixtures" / "fake_claude.py"
        bad = subprocess.run([sys.executable, str(fake), "plugin", "list", "--json"], capture_output=True, text=True)
        self.assertNotEqual(bad.returncode, 0, bad.stdout)
        good = subprocess.run([sys.executable, str(fake), "--setting-sources", "user", "plugin", "list", "--json"],
                              capture_output=True, text=True)
        self.assertEqual(good.returncode, 0, good.stderr)
        row = json.loads(good.stdout)[0]
        self.assertTrue((Path(row["installPath"]) / "hooks" / "hooks.json").is_file())


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
                    # Production workers start a fresh session per assignment. A second prompt in one
                    # session would not prove a per-prompt stamp. Stamps are whole seconds.
                    if previous is not None:
                        time.sleep(1)
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
