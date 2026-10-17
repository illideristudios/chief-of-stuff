"""#90: engine-enforced stack cleanup and its shell runner."""

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

import dispatch_prompt
import adopt_one_shots
import one_shot
import settings
import shell_setup
import worker
import engine
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
            f"sys.stderr.write(\"cleanup exit {exit_code}\\n\")\n"
            f"sys.exit({exit_code})\n")
        return shlex.join([sys.executable, str(script)])

    def launch(self, status="done", codes=(0,)):
        def worker(*args, **kwargs):
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

    def test_one_shot_does_not_run_workspace_teardown(self):
        self.configure(teardown=self.cleanup_command())
        self.assertEqual(self.launch(), 0)
        self.assertFalse((self.root / "cleanup.log").exists())
        self.assertEqual(self.report()["status"], "done")
        self.assertIn("completed; awaiting integration", self.fx.tracker.read_text())

    def test_unconfigured_workers_do_not_run_a_cleanup_command(self):
        with mock.patch.object(shell_setup, "run_command") as run:
            self.assertEqual(shell_setup.teardown(self.root, self.tree, "worker01", ""), [])
        run.assert_not_called()

    def test_shell_timeout_also_stops_background_children(self):
        command = shlex.join([sys.executable, "-c", "import time; time.sleep(2)"]) + " & wait"
        started = time.monotonic()
        with self.assertRaises(subprocess.TimeoutExpired):
            shell_setup.run_command(command, self.tree, workspace=self.root, timeout=0.1)
        self.assertLess(time.monotonic() - started, 1.5)

    def test_engine_workers_clean_up_and_keep_done_when_cleanup_fails(self):
        fx = engine_fixtures.WorkerTest("runTest")
        fx.setUp()
        self.addCleanup(fx.doCleanups)
        run_dir = fx.launched()
        fx.report(run_dir)
        (run_dir / "stdout").write_text('{"is_error": false, "result": "done"}')
        (run_dir / "stderr").write_text("")
        (run_dir / "exit.json").write_text('{"exit_code": 0}')
        # Stop after collection so this test needs no forge/planner stand-ins.
        forge = mock.Mock()
        forge.snapshots.side_effect = RuntimeError("collection finished")
        with mock.patch.object(shell_setup, "run_command", return_value=subprocess.CompletedProcess([], 7, "", "failed")) as run, \
             mock.patch.object(engine, "_record_limits"):
            with self.assertRaisesRegex(RuntimeError, "collection finished"):
                engine.run_pass(forge=forge, store=fx.db, policies=fx.p, root=fx.root,
                                trees=fx.trees, launch=fx.launch, workers=settings.Workers(), teardown_command="exit 7")
        segment = fx.db.segments()[0]
        self.assertEqual(segment.outcome, "done")
        run.assert_called_once()
        self.assertEqual(run.call_args.args, ("exit 7", fx.root))
        self.assertEqual(run.call_args.kwargs["worker"], fx.tree.name)
        self.assertEqual(run.call_args.kwargs["worktree"], str(fx.tree.resolve()))
        self.assertTrue(any("teardown failed" in log.message for log in fx.db.log(engine_fixtures.KEY)))

    def test_failed_start_still_tears_down_when_cancel_raises(self):
        fx = engine_fixtures.WorkerTest("runTest")
        fx.setUp()
        self.addCleanup(fx.doCleanups)
        config = settings.Settings(workers=settings.Workers(teardown="exit 0"))
        with mock.patch.object(worker.make_worktree, "build", return_value=mock.Mock(path=fx.tree)), \
             mock.patch.object(worker.claude_adapter, "start", return_value=mock.Mock(pid=78, poll=lambda: 1)), \
             mock.patch.object(worker.claude_adapter, "cancel", side_effect=RuntimeError("survived SIGKILL")), \
             mock.patch.object(worker, "teardown", return_value=[]) as torn:
            with self.assertRaises(RuntimeError):
                worker.launch(engine_fixtures.KEY, "implement", 1, None, store=fx.db, root=fx.root, trees=fx.trees,
                              policies=fx.p, forge=fx.forge, clock=lambda: engine_fixtures.NOW, config=config)
        torn.assert_called_once_with(fx.root, fx.tree, fx.tree.name, "exit 0")

    def test_missing_tree_and_shell_lookup_errors_are_cleanup_warnings(self):
        missing = self.root / "trees/removed"
        self.assertEqual(shell_setup.teardown(self.root, missing, "removed", self.cleanup_command()), [])
        self.assertEqual((self.root / "cleanup.log").read_text(), f"{self.root.resolve()} removed\n")
        with mock.patch.object(shell_setup, "login_shell", side_effect=KeyError("no user")):
            self.assertTrue(shell_setup.teardown(self.root, self.tree, "worker01", "exit 0"))
        with mock.patch.object(shell_setup, "run_command", return_value=subprocess.CompletedProcess([], 1, "diagnostic", "\n")):
            self.assertIn("diagnostic", shell_setup.teardown(self.root, self.tree, "worker01", "exit 1")[0])

    def test_shell_startup_banner_does_not_corrupt_discovery_stdout(self):
        (self.root / ".zshrc").write_text("echo startup-banner\necho loaded > startup-marker\n")
        with mock.patch.object(shell_setup, "clean_env", return_value={**shell_setup.clean_env(), "ZDOTDIR": str(self.root)}), \
             mock.patch.object(shell_setup, "login_shell", return_value="/bin/zsh"):
            result = shell_setup.run_command("printf '[]'", self.root, workspace=self.root)
        self.assertEqual(json.loads(result.stdout), [])
        self.assertEqual((self.root / "startup-marker").read_text(), "loaded\n")

    def test_shell_rejects_unknown_shell_and_cleans_up_interrupts(self):
        with mock.patch.object(shell_setup, "login_shell", return_value="/bin/echo"):
            with self.assertRaises(shell_setup.ShellError):
                shell_setup.run_command("exit 0", self.root, workspace=self.root)
        child = mock.MagicMock(pid=123)
        child.__enter__.return_value = child
        child.wait.side_effect = [KeyboardInterrupt, 0]
        child.poll.return_value = None
        with mock.patch.object(shell_setup.subprocess, "Popen", return_value=child), \
             mock.patch.object(shell_setup.os, "killpg") as kill:
            with self.assertRaises(KeyboardInterrupt):
                shell_setup.run_command("exit 0", self.root, workspace=self.root)
        kill.assert_called_once()
        self.assertIsNotNone(child.wait.call_args)

    def test_shell_timeout_does_not_drain_escaped_child_pipes(self):
        script = "import os,time; pid=os.fork(); (os.setsid(),time.sleep(2)) if pid == 0 else time.sleep(2)"
        started = time.monotonic()
        with self.assertRaises(subprocess.TimeoutExpired):
            shell_setup.run_command(shlex.join([sys.executable, "-c", script]), self.tree,
                                    workspace=self.root, timeout=0.1)
        self.assertLess(time.monotonic() - started, 1.5)

    def test_finished_command_does_not_wait_for_daemon_stderr(self):
        script = "import os,time; pid=os.fork(); (os.setsid(),time.sleep(2),os._exit(0)) if pid == 0 else None"
        result = shell_setup.run_command(shlex.join([sys.executable, "-c", script]), self.tree,
                                         workspace=self.root, timeout=0.5)
        self.assertEqual(result.returncode, 0)

    def test_teardown_executes_the_workspace_copy_of_a_relative_command(self):
        log = self.root / "which.txt"
        script = "import os\nfrom pathlib import Path\nPath({log!r}).write_text(os.getcwd() + ' ' + os.environ['CHIEF_OF_STUFF_WORKTREE'] + ' ' + os.environ['CHIEF_OF_STUFF_WORKER'] + ' {word}\\n')\n"
        for base, word in ((self.root, "trusted"), (self.tree, "untrusted")):
            (base / "tools").mkdir(exist_ok=True)
            (base / "tools" / "stack.py").write_text(script.format(log=str(log), word=word))
        command = shlex.join([sys.executable, "tools/stack.py"])
        self.assertEqual(shell_setup.teardown(self.root, self.tree, "worker01", command), [])
        self.assertEqual(log.read_text(), f"{self.root.resolve()} {self.tree.resolve()} worker01 trusted\n")

    def test_unverified_engine_start_tears_the_stack_down(self):
        fx = engine_fixtures.WorkerTest("runTest")
        fx.setUp()
        self.addCleanup(fx.doCleanups)
        def start(request, run_dir, **kwargs):
            (run_dir / "started.json").write_text(json.dumps({"pid": 77, "session_id": request.session_id}))
            return mock.Mock(pid=78, poll=lambda: 0)
        calls = []
        with mock.patch.object(worker.make_worktree, "build", return_value=type("Tree", (), {"path": fx.tree})()), \
             mock.patch.object(worker.claude_adapter, "start", side_effect=start), \
             mock.patch.object(worker.claude_adapter, "cancel"), \
             mock.patch.object(worker.process_status, "identity", return_value=None), \
             mock.patch.object(worker, "teardown", side_effect=lambda *args: calls.append(args) or []):
            with self.assertRaisesRegex(RuntimeError, "could not verify worker start"):
                worker.launch(engine_fixtures.KEY, "implement", 1, None, store=fx.db, root=fx.root,
                              trees=fx.trees, policies=fx.p, forge=fx.forge, clock=lambda: engine_fixtures.NOW,
                              config=settings.Settings(workers=settings.Workers(teardown="down")))
        self.assertEqual(calls, [(fx.root, fx.tree, fx.tree.name, "down")])

    def test_adoption_cli_reconciles_existing_evidence_without_teardown(self):
        # The architect leaves cleanup after a dead launcher to the engine at cutover.
        self.configure(teardown=self.cleanup_command())
        one_shot.record_launch(self.fx.tracker, "Security audit", "worker01", "worktree `worker` (main)",
                               "codex", "", "10:00")
        self.fx._stopped_tree()  # A launched tree ignores its own dispatch/result metadata.
        (self.tree / one_shot.RESULT).write_text("status: done\nreason: checked\nchanges: none\n")
        (self.tree / one_shot.PIDFILE).write_text("999999999 Security audit\n")
        out = io.StringIO()
        with mock.patch.object(one_shot, "datetime", wraps=one_shot.datetime) as clock, \
             contextlib.redirect_stdout(out):
            clock.now.return_value = one_shot.datetime.fromisoformat("2026-09-18T12:00:00+00:00")
            self.assertEqual(adopt_one_shots.main(["--root", str(self.root)]), 0)
        self.assertFalse((self.root / "cleanup.log").exists())
        self.assertEqual(decode(out.getvalue())[0]["task"], "Security audit")
        self.assertEqual(self.report()["status"], "done")
        self.assertNotIn("| worker01 | running", self.fx.tracker.read_text())
