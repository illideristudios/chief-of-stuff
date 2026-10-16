"""#90: launcher-enforced stack cleanup and process-backed stale-stack discovery."""

import contextlib
import io
import json
import os
import shlex
import subprocess
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

from evals import git_taint, test_one_shot as fixtures, test_worker as engine_fixtures

import audit_tasks
import dispatch_prompt
import one_shot
import settings
import shell_setup
import worker
from _vendor.toon_format import decode


class WorkerTeardownTest(unittest.TestCase):
    def setUp(self):
        self.fx = fixtures.RunTest("runTest")
        self.fx.setUp()
        self.addCleanup(self.fx.doCleanups)
        self.root, self.tree = self.fx.root, self.fx.tree
        self.enterContext(git_taint.private_git_views(self.root / "audit-views"))
        (self.root / "CLAUDE.md").write_text(fixtures.CLAUDE + '- Settings: `settings.toml`\n')
        # Exercise a real shell and command, independent of the developer's shell configuration.
        self.enterContext(mock.patch.object(shell_setup, "login_shell", return_value="/bin/sh"))

    def configure(self, **workers):
        (self.root / "settings.toml").write_text(
            "[workers]\n" + "".join(f"{key} = {json.dumps(value)}\n" for key, value in workers.items()))

    def cleanup_command(self, exit_code=0):
        script = self.root / "cleanup.py"
        script.write_text(
            "import os, sys\nfrom pathlib import Path\n"
            f"with Path({str(self.root / 'cleanup.log')!r}).open('a') as out:\n"
            "    out.write(str(Path.cwd()) + ' ' + os.environ['CHIEF_OF_STUFF_WORKER'] + '\\n')\n"
            f"sys.exit({exit_code})\n")
        return shlex.join([sys.executable, str(script)])

    def launch(self, status="done", codes=(0,)):
        def worker(*args):
            if status:
                (self.tree / one_shot.RESULT).write_text(f"status: {status}\nreason: checked\nchanges: none\n")
            return next(exits)
        exits = iter(codes)
        with mock.patch.object(one_shot, "_run_worker", side_effect=worker), \
             contextlib.redirect_stdout(io.StringIO()):
            return self.fx._run(self.fx._fake(None, write_partial=False))

    def report(self):
        return decode((self.root / dispatch_prompt.REPORTS_DIR / "worker.toon").read_text())

    def test_settings_are_optional_and_commands_are_validated(self):
        self.assertEqual(settings.load(self.root, None).workers.teardown, "")
        for key in ("teardown", "teardown_list"):
            for value in (False, 1, [], "", "  ", "bad\u0000command"):
                with self.subTest(key=key, value=value):
                    self.configure(**{key: value})
                    with self.assertRaisesRegex(settings.SettingsError, key):
                        settings.load(self.root, "settings.toml")
        self.configure(teardown="python3 cleanup.py", teardown_list="python3 stacks.py")
        got = settings.load(self.root, "settings.toml").workers
        self.assertEqual((got.teardown, got.teardown_list), ("python3 cleanup.py", "python3 stacks.py"))

    def test_cleanup_runs_in_the_tree_with_the_worker_name_before_reconciliation(self):
        self.configure(teardown=self.cleanup_command())
        original = one_shot.reconcile
        def reconcile(*args, **kwargs):
            self.assertEqual((self.root / "cleanup.log").read_text(), f"{self.tree.resolve()} worker01\n")
            return original(*args, **kwargs)
        with mock.patch.object(one_shot, "reconcile", side_effect=reconcile):
            self.assertEqual(self.launch(), 0)

    def test_cleanup_runs_for_review_relaunch_crash_and_both_timeout_attempts(self):
        for status, codes in (("human_review", (0,)), ("relaunch", (0,)), (None, (127,)),
                              (None, (124, 124))):
            with self.subTest(status=status, codes=codes):
                self.fx.tracker.write_text(fixtures.TRACKER)
                (self.tree / dispatch_prompt.DISPATCH_FILE).unlink(missing_ok=True)
                (self.tree / one_shot.RESULT).unlink(missing_ok=True)
                (self.root / "cleanup.log").unlink(missing_ok=True)
                self.configure(teardown=self.cleanup_command())
                self.launch(status, codes)
                self.assertEqual((self.root / "cleanup.log").read_text().splitlines(),
                                 [f"{self.tree.resolve()} worker01"] * len(codes))

    def test_failed_cleanup_is_reported_and_logged_without_failing_done(self):
        self.configure(teardown=self.cleanup_command(7))
        self.assertEqual(self.launch(), 0)
        report = self.report()
        self.assertEqual(report["status"], "done")
        self.assertTrue(any("teardown:" in e and "7" in e for e in report["errors"]))
        self.assertRegex(self.fx.tracker.read_text(), r"one-shot worker01 teardown failed: .*7")
        self.assertIn("completed; awaiting integration", self.fx.tracker.read_text())

    def test_unstartable_and_timed_out_cleanup_still_reconcile(self):
        self.configure(teardown=self.cleanup_command())
        for error in (OSError("cannot execute shell"), subprocess.TimeoutExpired("cleanup", 120)):
            with self.subTest(error=error):
                self.fx.tracker.write_text(fixtures.TRACKER)
                (self.tree / dispatch_prompt.DISPATCH_FILE).unlink(missing_ok=True)
                with mock.patch.object(shell_setup, "run_command", side_effect=error):
                    self.assertEqual(self.launch(), 0)
                self.assertEqual(self.report()["status"], "done")
                self.assertTrue(any(e.startswith("teardown:") for e in self.report()["errors"]))

    def test_interactive_headers_carry_the_command_for_every_runtime(self):
        command = "python3 cleanup.py --volumes"
        self.configure(teardown=command)
        for runtime in ("claude", "codex", "cursor", "agy"):
            with self.subTest(runtime=runtime):
                body = dispatch_prompt.compose(self.root, "2026-09-18", "Security audit",
                                               self.tree, name="worker01", runtime=runtime)
                self.assertIn(command, body)
                self.assertIn("CHIEF_OF_STUFF_WORKER=worker01", body)
                self.assertIn("before reporting finished or stopping", body)

    def test_unconfigured_workers_do_not_run_a_cleanup_command(self):
        with mock.patch.object(shell_setup, "run_command") as run:
            self.assertEqual(self.launch(), 0)
        run.assert_not_called()

    def test_shell_timeout_also_stops_background_children(self):
        command = shlex.join([sys.executable, "-c", "import time; time.sleep(2)"]) + " & wait"
        started = time.monotonic()
        with self.assertRaises(subprocess.TimeoutExpired):
            shell_setup.run_command(command, self.tree, workspace=self.root, timeout=0.1)
        self.assertLess(time.monotonic() - started, 1.5)

    def test_adopted_run_also_cleans_up_before_reconciliation(self):
        self.configure(teardown=self.cleanup_command())
        one_shot.record_launch(self.fx.tracker, "Security audit", "worker01", "worktree `worker` (main)",
                               "codex", "", "10:00")
        (self.tree / ".chief-of-stuff").mkdir(exist_ok=True)
        (self.tree / one_shot.RESULT).write_text("status: done\nreason: checked\nchanges: none\n")
        one_shot.reconcile(self.root, "2026-09-18", "Security audit", "worker01", self.tree, 0, adopted=True)
        self.assertEqual((self.root / "cleanup.log").read_text(), f"{self.tree.resolve()} worker01\n")

    def test_engine_workers_clean_up_and_keep_done_when_cleanup_fails(self):
        fx = engine_fixtures.WorkerTest("runTest")
        fx.setUp()
        self.addCleanup(fx.doCleanups)
        (fx.root / "CLAUDE.md").write_text(fixtures.CLAUDE + '- Settings: `settings.toml`\n')
        (fx.root / "settings.toml").write_text('[workers]\nteardown = "exit 7"\n')
        with mock.patch.object(shell_setup, "run_command", return_value=subprocess.CompletedProcess([], 7, "", "failed")) as run:
            segment = fx.finish("done")
        self.assertEqual(segment.outcome, "done")
        run.assert_called_once()
        self.assertEqual(run.call_args.args, ("exit 7", fx.tree))
        self.assertEqual(run.call_args.kwargs["worker"], fx.tree.name)
        self.assertTrue(any("teardown failed" in log.message for log in fx.db.log(engine_fixtures.KEY)))

    def test_engine_launch_registers_its_process_owner_for_stack_audit(self):
        fx = engine_fixtures.WorkerTest("runTest")
        fx.setUp()
        self.addCleanup(fx.doCleanups)
        fx.launched()
        command = self.list_command([{"stack": "engine-vm", "worktree": str(fx.tree)}])
        with mock.patch.object(audit_tasks, "process_exists", return_value=True):
            self.assertEqual(audit_tasks.stale_stacks(fx.root, "trees", command), [])

    def list_command(self, rows):
        script = self.root / "stacks.py"
        script.write_text(f"print({json.dumps(rows)!r})\n")
        return shlex.join([sys.executable, str(script)])

    def test_audit_lists_only_stacks_without_live_process_owners(self):
        live = self.root / "trees/live"
        registered = self.root / "trees/registered"
        rows = [{"stack": name, "worktree": str(tree)} for name, tree in (
            ("stale-vm", self.tree), ("active-vm", live), ("registered-vm", registered),
            ("removed-vm", self.root / "trees/removed"))]
        self.configure(teardown=self.cleanup_command(), teardown_list=self.list_command(rows))
        registry = {os.getpid(): {"worktree": str(registered), "name": "live-worker"}}
        with mock.patch.object(audit_tasks, "running_trees", return_value={live: "task"}), \
             mock.patch.object(audit_tasks, "registrations", return_value=registry):
            report = audit_tasks.audit(self.root, "2026-09-18", check_issues=False)
        lines = "\n".join(report.lines)
        self.assertIn("stale-vm", lines)
        self.assertIn("removed-vm", lines)
        self.assertNotIn("active-vm", lines)
        self.assertNotIn("registered-vm", lines)

    def test_audit_does_not_treat_invalid_or_failed_discovery_as_no_stacks(self):
        for command in ("exit 9", "printf 'not json'", self.list_command([{"stack": "missing-tree"}])):
            with self.subTest(command=command):
                self.configure(teardown=self.cleanup_command(), teardown_list=command)
                lines = audit_tasks.audit(self.root, "2026-09-18", check_issues=False).lines
                self.assertTrue(any("stack discovery failed:" in line for line in lines), lines)
