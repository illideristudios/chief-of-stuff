"""An install brings the Claude Code plugin to the release it installs, or says where it stands (§14, C27, #1003)."""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import claude_adapter
from shell_setup import ShellError


class FakePlugins:
    """`claude plugin …` as the CLI answers it, with each installed scope's version as state. No test runs the real one."""

    def __init__(self, scopes, folder, market, fail=()):
        self.scopes, self.folder, self.market, self.fail = dict(scopes), folder, str(market), tuple(fail)
        self.calls = []

    def __call__(self, *args, cwd=None):
        self.calls.append((args, cwd))
        if self.fail and args[:len(self.fail)] == self.fail:
            raise ShellError(f"claude plugin {' '.join(args)}: boom")
        if args[:2] == ("marketplace", "list"):
            return json.dumps([{"name": "other", "path": "/elsewhere"},
                               {"name": "chief-of-stuff", "source": "directory", "path": self.market}])
        if args[:2] == ("marketplace", "update"):
            return ""
        if args[0] == "list":
            return json.dumps([{"id": "other@other", "version": "9.9.9", "scope": "user"}] + [
                {"id": claude_adapter.PLUGIN, "version": version, "scope": scope,
                 **({"projectPath": "/work"} if scope == "project" else {})} for scope, version in self.scopes.items()])
        if args[0] == "update":
            self.scopes[args[args.index("--scope") + 1]] = self.folder
            return ""
        raise AssertionError(f"unexpected claude plugin {args}")


class UpdatePluginTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.current = Path(tmp.name) / "current"

    def update(self, fake):
        with mock.patch.object(claude_adapter, "_plugin", fake):
            return claude_adapter.update_plugin(self.current, "1.0.0")

    def test_every_scope_behind_is_updated_to_the_release(self):
        fake = FakePlugins({"user": "0.9.0", "project": "0.9.0"}, "1.0.0", self.current)
        result = self.update(fake)
        self.assertEqual(result["plugin"], "1.0.0")
        self.assertIn("restart", result["note"])  # a running session loads the new plugin only when it restarts
        self.assertIn((("marketplace", "update", "chief-of-stuff"), None), fake.calls)
        self.assertIn((("update", claude_adapter.PLUGIN, "--scope", "project"), "/work"), fake.calls)
        self.assertEqual(fake.scopes, {"user": "1.0.0", "project": "1.0.0"})

    def test_a_plugin_left_behind_is_reported_with_both_versions_and_the_marketplace_source(self):
        fake = FakePlugins({"user": "0.9.0"}, "0.9.0", "/old/worktree")
        result = self.update(fake)
        self.assertIn("1.0.0", result["plugin"])
        self.assertIn("user at 0.9.0", result["plugin"])
        self.assertIn("/old/worktree", result["plugin"])  # the marketplace does not read `current`, so the owner can fix it

    def test_a_failed_update_is_reported_never_silent(self):
        fake = FakePlugins({"user": "0.9.0"}, "1.0.0", self.current, fail=("marketplace", "update"))
        result = self.update(fake)
        self.assertTrue(result["plugin"].startswith("not updated"), result)
        self.assertIn("boom", result["plugin"])
        self.assertIn("1.0.0", result["plugin"])
        self.assertIn("user at 0.9.0", result["plugin"])

    def test_a_partial_failure_reports_the_versions_after_the_attempt(self):
        fake = FakePlugins({"user": "0.9.0", "project": "0.9.0"}, "1.0.0", self.current,
                           fail=("update", claude_adapter.PLUGIN, "--scope", "project"))
        result = self.update(fake)
        self.assertTrue(result["plugin"].startswith("not updated"))
        self.assertIn("release 1.0.0", result["plugin"])
        self.assertIn("user at 1.0.0", result["plugin"])
        self.assertIn("project at 0.9.0", result["plugin"])

    def test_no_installed_plugin_is_said_plainly(self):
        self.assertEqual(self.update(FakePlugins({}, "1.0.0", self.current))["plugin"], "not installed")

    def test_a_missing_claude_is_reported(self):
        with mock.patch.object(claude_adapter, "available", return_value=None):
            result = claude_adapter.update_plugin(self.current, "1.0.0")
        self.assertTrue(result["plugin"].startswith("not updated"), result)
        self.assertIn("release 1.0.0", result["plugin"])
        self.assertIn("unknown", result["plugin"])


if __name__ == "__main__":
    unittest.main()
