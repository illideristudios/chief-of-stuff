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
import engine
import process_status
import tree_claims
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

    def test_engine_ownership_is_verified_and_disappears_after_collect(self):
        fx = engine_fixtures.WorkerTest("runTest")
        fx.setUp()
        self.addCleanup(fx.doCleanups)
        def start(request, run_dir, **kwargs):
            child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)", request.session_id], start_new_session=True)
            self.addCleanup(lambda: child.poll() is None and (child.kill(), child.wait()))
            (run_dir / "command.json").write_text(json.dumps({"cwd": request.cwd, "session_id": request.session_id}))
            (run_dir / "started.json").write_text(json.dumps({"pid": child.pid, "runner_pid": child.pid,
                                                             "session_id": request.session_id}))
            return child
        with mock.patch.object(worker.make_worktree, "build", return_value=type("Tree", (), {"path": fx.tree})()), \
             mock.patch.object(worker.claude_adapter, "start", side_effect=start):
            fx.launch()
        engine_fixtures.slots.claim(fx.trees, fx.db, issue_key=engine_fixtures.KEY, stage="implement", attempt=1,
                                   max_concurrency=1, clock=lambda: engine_fixtures.NOW, launch=lambda: ("example", ""))
        run = Path(fx.db.resume(engine_fixtures.KEY)["runs"]["implement-1"]["run_dir"])
        self.assertFalse(list((fx.root / ".chief-of-stuff/sessions").glob("*.json")))
        self.assertIn(fx.tree, self.engine_live(fx.root))
        (fx.root / "CLAUDE.md").write_text(fixtures.CLAUDE + '- Settings: `settings.toml`\n')
        (fx.root / "daily").mkdir()
        (fx.root / "daily/2026-09-18-tracker.md").write_text(fixtures.TRACKER)
        command = self.list_command([{"stack": "engine-vm", "worktree": str(fx.tree)}])
        (fx.root / "settings.toml").write_text(f"[workers]\nteardown_list = {json.dumps(command)}\n")
        self.assertEqual(audit_tasks.audit(fx.root, "2026-09-18", check_issues=False).stacks, [])
        # Matching session ids on a closed segment: a dead PID is not ownership, and identity has to run.
        record = fx.db.resume(engine_fixtures.KEY)["runs"]["implement-1"]
        with mock.patch.object(process_status, "identity", return_value=None) as reused:
            claims = tree_claims.engine_claims(fx.root, process_status.identity, [("done", record)])
        reused.assert_called()
        self.assertFalse(any(claim.live for rows in claims.values() for claim in rows))
        child = worker._runners[str(run)]
        child.terminate()
        child.wait(timeout=5)
        self.assertIn(fx.tree, self.engine_live(fx.root))
        fx.report(run)
        (run / "stdout").write_text('{"is_error": false, "result": "done"}')
        (run / "stderr").write_text("")
        (run / "exit.json").write_text('{"exit_code": 0}')
        self.assertIn(fx.tree, self.engine_live(fx.root))
        def cleanup(*args):
            self.assertIn(fx.tree, self.engine_live(fx.root))
            self.assertEqual(audit_tasks.audit(fx.root, "2026-09-18", check_issues=False).stacks, [])
            return []
        with mock.patch.object(worker, "teardown", side_effect=cleanup):
            worker.collect(fx.db, fx.root, teardown_command="cleanup")
        self.assertEqual(fx.db.segments()[0].outcome, "done")
        fx.launch()  # Recovering durable exit evidence must not register the exited PID.
        self.assertFalse(list((fx.root / ".chief-of-stuff/sessions").glob("*.json")))
        self.assertNotIn(fx.tree, self.engine_live(fx.root))
        self.assertIn("engine-vm", str(audit_tasks.audit(fx.root, "2026-09-18", check_issues=False).stacks[0]))

    def _started_engine(self):
        fx = engine_fixtures.WorkerTest("runTest")
        fx.setUp()
        self.addCleanup(fx.doCleanups)
        def start(request, run_dir, **kwargs):
            child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)", request.session_id], start_new_session=True)
            self.addCleanup(lambda: child.poll() is None and (child.kill(), child.wait()))
            (run_dir / "command.json").write_text(json.dumps({"cwd": request.cwd, "session_id": request.session_id}))
            (run_dir / "started.json").write_text(json.dumps({"pid": child.pid, "runner_pid": child.pid,
                                                             "session_id": request.session_id}))
            return child
        with mock.patch.object(worker.make_worktree, "build", return_value=type("Tree", (), {"path": fx.tree})()), \
             mock.patch.object(worker.claude_adapter, "start", side_effect=start):
            fx.launch()
        engine_fixtures.slots.claim(fx.trees, fx.db, issue_key=engine_fixtures.KEY, stage="implement", attempt=1,
                                   max_concurrency=1, clock=lambda: engine_fixtures.NOW, launch=lambda: ("example", ""))
        run = Path(fx.db.resume(engine_fixtures.KEY)["runs"]["implement-1"]["run_dir"])
        return fx, run

    def test_a_closed_segment_does_not_hide_a_live_pid(self):
        fx, _run = self._started_engine()
        fx.db.end_segment(engine_fixtures.KEY, "implement", 1, "2026-09-18T00:00:00+00:00", "done")
        with mock.patch.object(process_status, "identity", return_value="still-here"):
            self.assertIn(fx.tree, self.engine_live(fx.root))

    def test_an_open_segment_with_exit_json_stays_claimed_before_resume_is_saved(self):
        fx, run = self._started_engine()
        fx.db.set_resume(engine_fixtures.KEY, {})
        (run / "exit.json").write_text('{"exit_code": 0}')
        with mock.patch.object(process_status, "identity", return_value=None):
            self.assertIn(fx.tree, self.engine_live(fx.root))

    def test_a_non_dict_resume_entry_does_not_crash_claim_lookup(self):
        fx, _run = self._started_engine()
        claims = tree_claims.engine_claims(fx.root, process_status.identity, [(None, ["not-a-dict"])])
        self.assertIsInstance(claims, dict)

    def test_unreadable_engine_store_is_a_stack_finding(self):
        self.configure(teardown_list="printf '[]'")
        (self.root / "store.db").write_bytes(b"not a database")
        report = audit_tasks.audit(self.root, "2026-09-18", check_issues=False)
        self.assertTrue(any("engine ownership unreadable" in line for line in report.lines), report.lines)

    def engine_live(self, root):
        found = audit_tasks.engine_ownership(root)
        return {path for path, rows in found.items() if any(claim.live for claim in rows)}

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
                self.assertTrue(any("stack: discovery failed:" in line for line in lines), lines)

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

    def test_discovery_keeps_valid_rows_and_rejects_base_and_missing_configuration(self):
        command = self.list_command([{"stack": "stale", "worktree": "trees/worker"},
                                     {"stack": "outside", "worktree": "../other"},
                                     {"stack": "base", "worktree": "trees"}])
        self.configure(teardown_list=command)
        lines = audit_tasks.audit(self.root, "2026-09-18", check_issues=False).lines
        self.assertIn("stack: stale: worktree trees/worker has no live owner", lines)
        self.assertEqual(sum("outside the configured Worktrees" in line for line in lines), 2)
        (self.root / "CLAUDE.md").write_text((self.root / "CLAUDE.md").read_text().replace('- Worktrees: `trees/`\n', ''))
        lines = audit_tasks.audit(self.root, "2026-09-18", check_issues=False).lines
        self.assertTrue(any("stack: discovery failed:" in line and "Worktrees" in line for line in lines))

    def test_compose_issue_uses_supplied_cleanup_without_loading_settings(self):
        from types import SimpleNamespace
        row = SimpleNamespace(issue_key="example/project#1", title="Task")
        actor = SimpleNamespace(role="builder", rules=())
        with mock.patch.object(dispatch_prompt, "load_settings", side_effect=AssertionError("unexpected settings read")):
            prompt = dispatch_prompt.compose_issue(row, "Title", "Body", "review", actor,
                                                   root=self.root, teardown="exit 7")
        self.assertIn("exit 7", prompt)

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

    def test_worker_launch_forwards_cleanup_into_assignment(self):
        fx = engine_fixtures.WorkerTest("runTest")
        fx.setUp()
        self.addCleanup(fx.doCleanups)
        with mock.patch.object(worker.make_worktree, "build", return_value=type("Tree", (), {"path": fx.tree})()), \
             mock.patch.object(worker.claude_adapter, "start", side_effect=fx.start) as start, \
             mock.patch.object(worker.process_status, "identity", return_value="process-start"):
            worker.launch(engine_fixtures.KEY, "implement", 1, None, store=fx.db, root=fx.root,
                          trees=fx.trees, policies=fx.p, forge=fx.forge, teardown_command="exit 7")
        self.assertIn("exit 7", start.call_args.args[0].prompt)

    def test_engine_claims_validate_identity_and_keep_runner_startup(self):
        run = self.root / ".chief-of-stuff/runs/1-implement-1"
        run.mkdir(parents=True)
        started = {"pid": 123, "runner_pid": 456, "session_id": "session"}
        command = {"cwd": str(self.tree), "session_id": "session"}
        (run / "started.json").write_text(json.dumps(started))
        (run / "command.json").write_text(json.dumps(command))
        with mock.patch.object(process_status, "identity", side_effect=[None, "runner-start"]) as identity:
            self.assertIn(self.tree.resolve(), self.engine_live(self.root))
            self.assertEqual(identity.call_args_list, [mock.call(123, "session", host=True), mock.call(456, "session")])
        with mock.patch.object(process_status, "identity", side_effect=RuntimeError("ps timed out")):
            with self.assertRaisesRegex(RuntimeError, "ps timed out"):
                self.engine_live(self.root)
        for field, value in (("pid", 0), ("pid", -1), ("pid", True), ("runner_pid", 0), ("cwd", "")):
            with self.subTest(field=field, value=value):
                (run / "started.json").write_text(json.dumps({**started, **({field: value} if field != "cwd" else {})}))
                (run / "command.json").write_text(json.dumps({**command, **({field: value} if field == "cwd" else {})}))
                with mock.patch.object(process_status, "identity") as identity:
                    self.assertEqual(self.engine_live(self.root), set())
                    identity.assert_not_called()

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

    def test_stack_discovery_requires_a_contained_worktrees_directory(self):
        for base in (".", "../outside"):
            with self.subTest(base=base):
                (self.root / "CLAUDE.md").write_text(fixtures.CLAUDE.replace('`trees/`', f'`{base}`') + '- Settings: `settings.toml`\n')
                self.configure(teardown_list="printf '[]'")
                with mock.patch.object(audit_tasks, "discover_stacks") as discover:
                    report = audit_tasks.audit(self.root, "2026-09-18", check_issues=False)
                discover.assert_not_called()
                self.assertTrue(any("stack: discovery failed:" in line for line in report.lines))

    def test_discovery_keeps_a_stack_whose_owner_exits_during_the_command(self):
        self.configure(teardown_list="printf '[]'")
        tree = self.tree.resolve()
        active = {tree: "task"}
        def discover(*args):
            active.clear()
            return [("vm", tree)]
        with mock.patch.object(audit_tasks, "discover_stacks", side_effect=discover), \
             mock.patch.object(audit_tasks, "running_trees", side_effect=lambda *args: dict(active)):
            self.assertEqual(audit_tasks.audit(self.root, "2026-09-18", check_issues=False).stacks, [])

    def test_discovery_samples_claims_after_command_and_sanitizes_facts(self):
        self.configure(teardown_list="printf '[]'")
        tree = self.tree.resolve()
        active = {}
        def discover(*args):
            active[tree] = "task"
            return [("vm", tree)]
        with mock.patch.object(audit_tasks, "discover_stacks", side_effect=discover), \
             mock.patch.object(audit_tasks, "running_trees", side_effect=lambda *args: dict(active)):
            self.assertEqual(audit_tasks.audit(self.root, "2026-09-18", check_issues=False).stacks, [])
        rows = audit_tasks.discover_stacks(self.root, self.list_command([{"stack": "vm\nforged", "worktree": "../other"}]))
        with mock.patch.object(audit_tasks.git_trees, "clean", side_effect=AssertionError("driver called by judgement")):
            faults = audit_tasks.stale_stacks(rows, self.root / "trees", set())
        self.assertIn(str(self.root.parent / "other"), str(faults[0]))
        self.assertNotIn("\n", str(faults[0]))

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

    def test_exit_json_does_not_claim_a_different_open_launch(self):
        fx, run = self._started_engine()
        record = dict(fx.db.resume(engine_fixtures.KEY)["runs"]["implement-1"])
        record["session_id"] = "other-session"
        (run / "exit.json").write_text('{"exit_code": 0}')
        with mock.patch.object(process_status, "identity", return_value=None):
            claims = tree_claims.engine_claims(fx.root, process_status.identity, [(None, record)])
        self.assertFalse(any(claim.live for rows in claims.values() for claim in rows))

    def test_a_resolved_tree_path_matches_the_recorded_launch(self):
        fx, _run = self._started_engine()
        link = fx.root / "tree-link"
        link.symlink_to(fx.tree)
        record = dict(fx.db.resume(engine_fixtures.KEY)["runs"]["implement-1"])
        record["tree"] = str(link)
        with mock.patch.object(process_status, "identity", return_value=None):
            claims = tree_claims.engine_claims(fx.root, process_status.identity, [(None, record)])
        self.assertIn(fx.tree.resolve(), claims)

    def test_engine_ownership_failure_keeps_registry_claims(self):
        registry = {os.getpid(): {"worktree": str(self.tree), "name": "live-worker"}}
        with mock.patch.object(audit_tasks, "registrations", return_value=registry), \
             mock.patch.object(audit_tasks, "engine_ownership", side_effect=RuntimeError("ps timed out")):
            claims, exc, kind = audit_tasks._sample_claims(self.root, "trees")
        self.assertEqual(kind, "engine")
        self.assertIsInstance(exc, RuntimeError)
        self.assertIn(self.tree.resolve(), claims)
        self.assertTrue(any(claim.live for claim in claims[self.tree.resolve()]))

    def test_a_stale_store_is_not_migrated_by_the_audit(self):
        import store
        self.configure(teardown_list="printf '[]'")
        (self.root / "store.db").write_bytes(b"not a database")
        with mock.patch.object(store, "migrate", side_effect=AssertionError("audit migrated")):
            report = audit_tasks.audit(self.root, "2026-09-18", check_issues=False)
        self.assertTrue(any("engine ownership unreadable" in line for line in report.lines), report.lines)

    def test_a_failed_engine_probe_does_not_orphan_an_at_risk_tree(self):
        (self.tree / "wip.txt").write_text("wip\n")
        with mock.patch.object(audit_tasks, "engine_ownership", side_effect=RuntimeError("ps timed out")):
            report = audit_tasks.audit(self.root, "2026-09-18", check_issues=False)
        named = [str(item) for item in (*report.orphans, *report.unclaimed)]
        self.assertFalse(any(self.tree.name in line for line in named), named)

    def test_a_failed_second_ownership_sample_does_not_orphan_an_at_risk_tree(self):
        (self.tree / "wip.txt").write_text("wip\n")
        self.configure(teardown_list="printf '[]'")
        calls = {"n": 0}

        def sample(root, trees):
            calls["n"] += 1
            if calls["n"] >= 2:
                return {}, RuntimeError("ps timed out"), "engine"
            return {}, None, ""

        with mock.patch.object(audit_tasks, "_sample_claims", side_effect=sample):
            report = audit_tasks.audit(self.root, "2026-09-18", check_issues=False)
        named = [str(item) for item in (*report.orphans, *report.unclaimed)]
        self.assertFalse(any(self.tree.name in line for line in named), named)

    def test_an_engine_probe_failure_without_stack_discovery_is_not_a_stack_finding(self):
        with mock.patch.object(audit_tasks, "engine_ownership", side_effect=RuntimeError("ps timed out")):
            report = audit_tasks.audit(self.root, "2026-09-18", check_issues=False)
        self.assertFalse(any("engine ownership unreadable" in line for line in report.lines), report.lines)

    def test_a_successful_empty_claim_sample_is_not_repeated(self):
        self.configure(teardown_list="printf '[]'")
        calls = []
        def sample(root, trees):
            calls.append(trees)
            return {}, None, ""
        with mock.patch.object(audit_tasks, "_sample_claims", side_effect=sample):
            audit_tasks.audit(self.root, "2026-09-18", check_issues=False)
        self.assertEqual(len(calls), 2)

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
