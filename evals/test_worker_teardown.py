"""#90: launcher-enforced stack cleanup."""

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
import one_shot
import settings
import shell_setup
import worker
import engine
import process_status
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

    def test_cleanup_runs_in_the_workspace_with_the_worker_name_before_reconciliation(self):
        self.configure(teardown=self.cleanup_command())
        original = one_shot.reconcile
        def reconcile(*args, **kwargs):
            self.assertEqual((self.root / "cleanup.log").read_text(), f"{self.root.resolve()} worker01\n")
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
                                 [f"{self.root.resolve()} worker01"] * len(codes))

    def test_failed_cleanup_is_reported_and_logged_without_failing_done(self):
        self.configure(teardown=self.cleanup_command(7))
        self.assertEqual(self.launch(), 0)
        report = self.report()
        self.assertEqual(report["status"], "done")
        self.assertTrue(any("teardown:" in e and "7" in e for e in report["errors"]))
        self.assertRegex(self.fx.tracker.read_text(), r"(?m)^- \d{2}:\d{2} one-shot worker01: teardown failed: .*7, task Security audit$")
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
        (self.tree / one_shot.PIDFILE).write_text("999999999 Security audit\n")
        def teardown(*args):
            self.assertEqual((self.tree / one_shot.PIDFILE).read_text(), f"{os.getpid()} Security audit\n")
            self.assertEqual(one_shot.adopt(self.root, day="2026-09-18", teardown=shell_setup.teardown), [])
            return shell_setup.teardown(*args)
        reports = one_shot.adopt(self.root, day="2026-09-18", teardown=teardown)
        self.assertEqual(len(reports), 1)
        self.assertEqual((self.root / "cleanup.log").read_text(), f"{self.root.resolve()} worker01\n")

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
        with mock.patch.object(shell_setup, "run_command", return_value=subprocess.CompletedProcess([], 7, "", "failed")) as run:
            with self.assertRaisesRegex(RuntimeError, "collection finished"):
                engine.run_pass(forge=forge, store=fx.db, policies=fx.p, root=fx.root,
                                trees=fx.trees, launch=fx.launch, teardown_command="exit 7")
        segment = fx.db.segments()[0]
        self.assertEqual(segment.outcome, "done")
        run.assert_called_once()
        self.assertEqual(run.call_args.args, ("exit 7", fx.root))
        self.assertEqual(run.call_args.kwargs["worker"], fx.tree.name)
        self.assertEqual(run.call_args.kwargs["worktree"], str(fx.tree.resolve()))
        self.assertTrue(any("teardown failed" in log.message for log in fx.db.log(engine_fixtures.KEY)))


    def test_teardown_warning_does_not_modify_a_reassigned_task(self):
        one_shot.record_launch(self.fx.tracker, "Security audit", "worker01", "worktree `worker` (main)", "codex", "", "10:00")
        self.fx.tracker.write_text(self.fx.tracker.read_text().replace("worker01 | running", "other | running"))
        before = self.fx.tracker.read_text()
        one_shot.reconcile(self.root, "2026-09-18", "Security audit", "worker01", self.tree, 0,
                           teardown_errors=["teardown: exit 7"])
        self.assertEqual(self.fx.tracker.read_text(), before)

    def test_teardown_logging_failure_is_a_nonblocking_warning(self):
        self.configure(teardown="exit 7")
        original = one_shot.tracker_write.edit
        def edit(path, change):
            after = change(path.read_text())
            if "teardown failed:" in after:
                raise OSError("log unavailable")
            return original(path, change)
        with mock.patch.object(one_shot.tracker_write, "edit", side_effect=edit):
            self.assertEqual(self.launch(), 0)
        self.assertEqual(self.report()["status"], "done")

    def test_missing_tree_and_shell_lookup_errors_are_cleanup_warnings(self):
        missing = self.root / "trees/removed"
        self.assertEqual(shell_setup.teardown(self.root, missing, "removed", self.cleanup_command()), [])
        self.assertEqual((self.root / "cleanup.log").read_text(), f"{self.root.resolve()} removed\n")
        with mock.patch.object(shell_setup, "login_shell", side_effect=KeyError("no user")):
            self.assertTrue(shell_setup.teardown(self.root, self.tree, "worker01", "exit 0"))
        with mock.patch.object(shell_setup, "run_command", return_value=subprocess.CompletedProcess([], 1, "diagnostic", "\n")):
            self.assertIn("diagnostic", shell_setup.teardown(self.root, self.tree, "worker01", "exit 1")[0])

    def test_worker_restores_launcher_pid_and_kills_timeout_group(self):
        logs = self.tree / dispatch_prompt.PROMPT_DIR
        logs.mkdir(exist_ok=True)
        self.assertEqual(one_shot._run_worker(self.tree, logs, [sys.executable, "-c", "pass"], os.environ.copy(), "Security audit", 1), 0)
        self.assertEqual((self.tree / one_shot.PIDFILE).read_text(), f"{os.getpid()} Security audit\n")
        with mock.patch.object(one_shot.os, "killpg", wraps=os.killpg) as kill:
            self.assertEqual(one_shot._run_worker(self.tree, logs, [sys.executable, "-c", "import time; time.sleep(30)"], os.environ.copy(), "Security audit", 0.001), 124)
        kill.assert_called_once()
        self.assertEqual((self.tree / one_shot.PIDFILE).read_text(), f"{os.getpid()} Security audit\n")


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


    def test_worker_pid_restoration_tolerates_removed_metadata_and_symlinks(self):
        logs = self.tree / dispatch_prompt.PROMPT_DIR
        logs.mkdir(exist_ok=True)
        target = self.root / "untouched"
        target.write_text("original")
        child = mock.Mock(pid=123, returncode=0)
        def finished(*args, **kwargs):
            (self.tree / one_shot.PIDFILE).unlink()
            (self.tree / one_shot.PIDFILE).symlink_to(target)
            return 0
        child.wait.side_effect = finished
        child.poll.return_value = 0
        with mock.patch.object(one_shot.subprocess, "Popen", return_value=child):
            self.assertEqual(one_shot._run_worker(self.tree, logs, ["worker"], {}, "task", 1), 0)
        self.assertEqual(target.read_text(), "original")
        (self.tree / one_shot.PIDFILE).unlink()
        external_logs = self.root / "external-logs"
        external_logs.mkdir()
        def removed(*args, **kwargs):
            (self.tree / one_shot.PIDFILE).unlink()
            for path in logs.iterdir():
                path.unlink()
            logs.rmdir()
            return 0
        child.wait.side_effect = removed
        with mock.patch.object(one_shot.subprocess, "Popen", return_value=child):
            self.assertEqual(one_shot._run_worker(self.tree, external_logs, ["worker"], {}, "task", 1), 0)

    def test_interrupted_worker_keeps_its_own_pid_for_adoption(self):
        child = mock.Mock(pid=123)
        child.wait.side_effect = KeyboardInterrupt
        child.poll.return_value = None
        real_popen = subprocess.Popen
        def popen(argv, *args, **kwargs):
            if argv and argv[0] == "git":
                return real_popen(argv, *args, **kwargs)
            return child
        with mock.patch.object(one_shot.subprocess, "Popen", side_effect=popen), \
             contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(KeyboardInterrupt):
                self.fx._run(self.fx._fake(None, write_partial=False))
        self.assertEqual((self.tree / one_shot.PIDFILE).read_text(), "123 Security audit\n")

    def test_interrupted_teardown_keeps_the_dead_worker_pid_for_adoption(self):
        child = mock.Mock(pid=123, returncode=0)
        child.wait.return_value = 0
        child.poll.return_value = 0
        real_popen = subprocess.Popen

        def popen(argv, *args, **kwargs):
            if argv and argv[0] == "git":
                return real_popen(argv, *args, **kwargs)
            return child

        with mock.patch.object(one_shot.subprocess, "Popen", side_effect=popen), \
             mock.patch.object(shell_setup, "teardown", side_effect=KeyboardInterrupt), \
             contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(KeyboardInterrupt):
                self.fx._run(self.fx._fake(None, write_partial=False))
        self.assertEqual((self.tree / one_shot.PIDFILE).read_text(), "123 Security audit\n")

    def test_finished_command_does_not_wait_for_daemon_stderr(self):
        script = "import os,time; pid=os.fork(); (os.setsid(),time.sleep(2),os._exit(0)) if pid == 0 else None"
        result = shell_setup.run_command(shlex.join([sys.executable, "-c", script]), self.tree,
                                         workspace=self.root, timeout=0.5)
        self.assertEqual(result.returncode, 0)


    def test_worker_start_failure_and_disappeared_process_preserve_exit_code(self):
        logs = self.tree / dispatch_prompt.PROMPT_DIR
        logs.mkdir(exist_ok=True)
        with mock.patch.object(one_shot.subprocess, "Popen", side_effect=OSError("cannot start")):
            self.assertEqual(one_shot._run_worker(self.tree, logs, ["worker"], {}, "task", 1), 127)
        child = mock.Mock(pid=123)
        child.poll.return_value = 0
        write = Path.write_text
        def fail_worker_pid(path, text, *args, **kwargs):
            if path == self.tree / one_shot.PIDFILE and text.startswith("123 "):
                raise OSError("cannot register worker")
            return write(path, text, *args, **kwargs)
        with mock.patch.object(one_shot.subprocess, "Popen", return_value=child), \
             mock.patch.object(Path, "write_text", new=fail_worker_pid), \
             mock.patch.object(one_shot.os, "killpg", side_effect=ProcessLookupError):
            self.assertEqual(one_shot._run_worker(self.tree, logs, ["worker"], {}, "task", 1), 127)
        child.wait.assert_called_once_with()

    def test_worker_pid_restoration_does_not_follow_metadata_directory_symlink(self):
        logs = self.tree / dispatch_prompt.PROMPT_DIR
        logs.mkdir(exist_ok=True)
        outside = self.root / "outside"
        outside.mkdir()
        external_logs = self.root / "external-logs"
        external_logs.mkdir()
        target = outside / one_shot.PIDFILE.name
        target.write_text("original")
        child = mock.Mock(pid=123)
        child.poll.return_value = 0
        def finished(*args, **kwargs):
            (self.tree / one_shot.PIDFILE).unlink()
            logs.rmdir()
            logs.symlink_to(outside, target_is_directory=True)
            return 0
        child.wait.side_effect = finished
        with mock.patch.object(one_shot.subprocess, "Popen", return_value=child):
            self.assertEqual(one_shot._run_worker(self.tree, external_logs, ["worker"], {}, "task", 1), 0)
        self.assertEqual(target.read_text(), "original")

    def test_a_failed_tracker_update_is_reconciled_on_the_next_adoption(self):
        one_shot.record_launch(self.fx.tracker, "Security audit", "worker01", "worktree `worker` (main)",
                               "codex", "", "10:00")
        (self.tree / ".chief-of-stuff").mkdir(exist_ok=True)
        (self.tree / one_shot.RESULT).write_text("status: done\nreason: checked\nchanges: none\n")
        pidfile = self.tree / one_shot.PIDFILE
        pidfile.parent.mkdir(exist_ok=True)
        pidfile.write_text("999999999 Security audit\n")
        original = one_shot.tracker_write.edit
        def fail_once(path, change):
            fail_once.calls += 1
            if fail_once.calls == 1:
                raise OSError("disk full")
            return original(path, change)
        fail_once.calls = 0
        with mock.patch.object(one_shot.tracker_write, "edit", side_effect=fail_once):
            first = one_shot.adopt(self.root, day="2026-09-18")
        self.assertEqual(len(first), 1)
        self.assertTrue(any(error.startswith(dispatch_prompt.TRACKER_ERROR) for error in first[0]["errors"]))
        self.assertFalse(one_shot.launcher_copy(self.root, self.tree).exists())
        self.assertEqual(pidfile.read_text(), "999999999 Security audit\n")
        self.assertIn("| worker01 | running", self.fx.tracker.read_text())
        second = one_shot.adopt(self.root, day="2026-09-18")
        self.assertEqual(len(second), 1)
        self.assertFalse(any(str(error).startswith(dispatch_prompt.TRACKER_ERROR) for error in second[0]["errors"]))
        self.assertNotIn("| worker01 | running", self.fx.tracker.read_text())

    def test_teardown_executes_the_workspace_copy_of_a_relative_command(self):
        log = self.root / "which.txt"
        script = "import os\nfrom pathlib import Path\nPath({log!r}).write_text(os.getcwd() + ' ' + os.environ['CHIEF_OF_STUFF_WORKTREE'] + ' ' + os.environ['CHIEF_OF_STUFF_WORKER'] + ' {word}\\n')\n"
        for base, word in ((self.root, "trusted"), (self.tree, "untrusted")):
            (base / "tools").mkdir(exist_ok=True)
            (base / "tools" / "stack.py").write_text(script.format(log=str(log), word=word))
        command = shlex.join([sys.executable, "tools/stack.py"])
        self.assertEqual(shell_setup.teardown(self.root, self.tree, "worker01", command), [])
        self.assertEqual(log.read_text(), f"{self.root.resolve()} {self.tree.resolve()} worker01 trusted\n")

    def test_a_non_utf8_pidfile_does_not_escape_the_launch_lock(self):
        pidfile = self.tree / one_shot.PIDFILE
        pidfile.parent.mkdir(exist_ok=True)
        pidfile.write_bytes(b"\xff")
        self.assertIsNone(one_shot._pidfile_pid(pidfile))
        with one_shot.slot(self.tree.parent, self.tree, "Security audit", None):
            self.assertTrue(pidfile.read_text().startswith(str(os.getpid())))

    def test_a_launch_refuses_a_tree_holding_another_tasks_dead_worker(self):
        pidfile = self.tree / one_shot.PIDFILE
        pidfile.parent.mkdir(exist_ok=True)
        pidfile.write_text("999999999 Other task\n")
        (self.tree / dispatch_prompt.DISPATCH_FILE).write_text("task: Other task\n")
        with self.assertRaisesRegex(ValueError, "unreconciled"):
            self.fx._run(self.fx._fake("status: done\nreason: r\nchanges: c\n", write_partial=False))
        self.assertEqual(pidfile.read_text(), "999999999 Other task\n")


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
                              teardown_command="down")
        self.assertEqual(calls, [(fx.root, fx.tree, fx.tree.name, "down")])

    def test_a_launch_refuses_a_tree_that_already_names_a_live_worker(self):
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        self.addCleanup(lambda: child.poll() is None and (child.kill(), child.wait()))
        pidfile = self.tree / one_shot.PIDFILE
        pidfile.parent.mkdir(exist_ok=True)
        pidfile.write_text(f"{child.pid} Security audit\n")
        with self.assertRaisesRegex(ValueError, "live one-shot worker"):
            self.fx._run(self.fx._fake('status: done\nreason: r\nchanges: c\n', write_partial=False))
        self.assertEqual(pidfile.read_text(), f"{child.pid} Security audit\n")

    def test_failed_adoption_releases_claim_for_a_later_retry(self):
        self.configure(teardown="cleanup")
        one_shot.record_launch(self.fx.tracker, "Security audit", "worker01", "worktree `worker` (main)",
                               "codex", "", "10:00")
        pidfile = self.tree / one_shot.PIDFILE
        pidfile.parent.mkdir(exist_ok=True)
        pidfile.write_text("999999999 Security audit\n")
        with self.assertRaisesRegex(RuntimeError, "interrupted cleanup"):
            one_shot.adopt(self.root, day="2026-09-18",
                           teardown=mock.Mock(side_effect=RuntimeError("interrupted cleanup")))
        self.assertEqual(pidfile.read_text(), "999999999 Security audit\n")
        self.assertEqual(len(one_shot.adopt(self.root, day="2026-09-18")), 1)

    def test_interrupted_cleanup_after_spawn_failure_keeps_recovery_evidence(self):
        fake = self.fx._fake(None, write_partial=False)
        real_popen = subprocess.Popen
        def popen(argv, *args, **kwargs):
            if argv and argv[0] == "git":
                return real_popen(argv, *args, **kwargs)
            raise OSError("cannot spawn")
        with mock.patch.object(one_shot.subprocess, "Popen", side_effect=popen), \
             mock.patch.object(shell_setup, "teardown", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.fx._run(fake)
        self.assertTrue((self.tree / one_shot.PIDFILE).exists())
        self.assertIn("| worker01 | running", self.fx.tracker.read_text())
        # The failed launcher has now exited; a restart can discover and reconcile it.
        with mock.patch.object(one_shot, "process_exists", return_value=False):
            self.assertEqual(len(one_shot.adopt(self.root, day="2026-09-18")), 1)

    def test_interruption_after_reconciliation_releases_launch_and_adoption_markers(self):
        original = one_shot.reconcile
        def interrupted(*args, **kwargs):
            original(*args, **kwargs)
            raise KeyboardInterrupt
        child = mock.Mock(pid=123, returncode=0)
        child.wait.return_value = child.poll.return_value = 0
        real_popen = subprocess.Popen
        def popen(argv, *args, **kwargs):
            return real_popen(argv, *args, **kwargs) if argv[0] == "git" else child
        with mock.patch.object(one_shot, "reconcile", side_effect=interrupted), \
             mock.patch.object(one_shot.subprocess, "Popen", side_effect=popen):
            with self.assertRaises(KeyboardInterrupt):
                self.fx._run(self.fx._fake(None, write_partial=False))
        self.assertFalse((self.tree / one_shot.PIDFILE).exists())
        self.fx.tracker.write_text(fixtures.TRACKER)
        one_shot.launcher_copy(self.root, self.tree).unlink(missing_ok=True)
        one_shot.record_launch(self.fx.tracker, "Security audit", "worker01", "worktree `worker` (main)", "codex", "", "10:00")
        (self.tree / one_shot.PIDFILE).write_text("999999999 Security audit\n")
        with mock.patch.object(one_shot, "reconcile", side_effect=interrupted):
            with self.assertRaises(KeyboardInterrupt):
                one_shot.adopt(self.root, day="2026-09-18")
        self.assertFalse((self.tree / one_shot.PIDFILE).exists())
        with one_shot.slot(self.tree.parent, self.tree, "Another task", None):
            pass

    def test_unreadable_tracker_cannot_discard_an_interrupted_runs_marker(self):
        fake = self.fx._fake(None, write_partial=False)
        real_popen, real_read = subprocess.Popen, Path.read_text
        interrupted = False
        def popen(argv, *args, **kwargs):
            if argv[0] == "git":
                return real_popen(argv, *args, **kwargs)
            raise OSError("cannot spawn")
        def teardown(*args):
            nonlocal interrupted
            interrupted = True
            raise KeyboardInterrupt
        def read(path, *args, **kwargs):
            if interrupted and path == self.fx.tracker:
                raise PermissionError("tracker unavailable")
            return real_read(path, *args, **kwargs)
        with mock.patch.object(one_shot.subprocess, "Popen", side_effect=popen), \
             mock.patch.object(shell_setup, "teardown", side_effect=teardown), \
             mock.patch.object(Path, "read_text", read):
            with self.assertRaises(KeyboardInterrupt):
                self.fx._run(fake)
        self.assertTrue((self.tree / one_shot.PIDFILE).exists())

    def test_invalid_utf8_tracker_preserves_the_interrupt_and_recovery_marker(self):
        def teardown(*args):
            self.fx.tracker.write_bytes(b"\xff")
            raise KeyboardInterrupt
        with mock.patch.object(one_shot, "_run_worker", return_value=127), \
             mock.patch.object(shell_setup, "teardown", side_effect=teardown):
            with self.assertRaises(KeyboardInterrupt):
                self.fx._run(self.fx._fake(None, write_partial=False))
        self.assertEqual((self.tree / one_shot.PIDFILE).read_text(), f"{os.getpid()} Security audit\n")

    def test_unreadable_adoption_tracker_keeps_evidence_and_retries_after_recovery(self):
        one_shot.record_launch(self.fx.tracker, "Security audit", "worker01", "worktree `worker` (main)",
                               "codex", "", "10:00")
        pidfile = self.tree / one_shot.PIDFILE
        pidfile.parent.mkdir(exist_ok=True)
        marker = "999999999 Security audit\n"
        original = Path.read_text
        for failure in (PermissionError("tracker unavailable"), OSError("I/O error"),
                        UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid byte")):
            with self.subTest(failure=failure):
                pidfile.write_text(marker)
                def read(path, *args, **kwargs):
                    if path == self.fx.tracker:
                        raise failure
                    return original(path, *args, **kwargs)
                with mock.patch.object(Path, "read_text", read), \
                     mock.patch.object(one_shot, "reconcile") as reconcile, \
                     self.assertRaises(type(failure)):
                    one_shot.adopt(self.root, day="2026-09-18")
                reconcile.assert_not_called()
                self.assertEqual(pidfile.read_text(), marker)
        self.assertIn("| worker01 | running", self.fx.tracker.read_text())
        self.assertEqual(len(one_shot.adopt(self.root, day="2026-09-18")), 1)
        self.assertFalse(pidfile.exists())
        self.assertNotIn("| worker01 | running", self.fx.tracker.read_text())

    def test_nested_cwd_uses_the_configured_worktrees_lock_for_pid_updates(self):
        nested = self.tree / "nested"
        nested.mkdir()
        locks = []
        original = one_shot.trees_lock
        def lock(path):
            locks.append(path)
            return original(path)
        with mock.patch.object(one_shot, "trees_lock", side_effect=lock), \
             contextlib.redirect_stdout(io.StringIO()):
            self.fx._run(self.fx._fake(None, write_partial=False), tree=nested)
        self.assertGreaterEqual(len(locks), 4)
        self.assertEqual(set(locks), {self.tree.parent})

    def test_adoption_retries_do_not_repeat_cleanup_warnings_or_issue_comments(self):
        (self.root / "CLAUDE.md").write_text((self.root / "CLAUDE.md").read_text() +
            "- Backlog: GitHub issues; repo https://github.com/team/repo (private)\n")
        self.fx.tracker.write_text(
            "## Tasks\n| name | item | owner | state | since | due | size | lane | stage | issue | checklist |\n"
            "|---|---|---|---|---|---|---|---|---|---|---|\n"
            "| Security audit | Security audit | unassigned | open | 09:00 | | M | build | implement | #7 | Inspect |\n"
            "\n## File ownership\n| context | paths |\n|---|---|\n| Security audit | `src/a/` |\n\n## Log\n")
        one_shot.record_launch(self.fx.tracker, "Security audit", "worker01", "worktree `worker` (main)", "codex", "", "10:00")
        pidfile = self.tree / one_shot.PIDFILE
        pidfile.parent.mkdir(exist_ok=True)
        pidfile.write_text("999999999 Security audit\n")
        original = one_shot.update_tracker
        calls = 0
        def update(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls < 3:
                raise OSError("tracker busy")
            return original(*args, **kwargs)
        with mock.patch.object(one_shot, "update_tracker", side_effect=update), \
             mock.patch.object(one_shot.backlog, "comment", return_value=mock.Mock(done=True)) as comment:
            for _ in range(3):
                self.assertEqual(len(one_shot.adopt(self.root, day="2026-09-18", teardown=lambda *a: ["teardown: failed"])), 1)
        self.assertEqual(comment.call_count, 1)
        self.assertEqual(self.fx.tracker.read_text().count("teardown failed:"), 1)
