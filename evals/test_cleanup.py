"""Engine-owned worktree cleanup (1.0 plan M3 item 5, ledger E19) past every finding-27 guard.

Each test builds a real origin, clone and linked worktrees. A tree is removed only when it is a finished store item's,
no worker claims it or awaits reconciliation, `git status --ignored` lists nothing but build output or engine metadata,
no container binds it, and nothing on it is unpushed or unmerged. A guard that cannot be verified keeps the tree.
"""

from __future__ import annotations

import contextlib
import fcntl
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from sqlalchemy.exc import SQLAlchemyError

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "evals"))

import cleanup  # noqa: E402
import dispatch_prompt  # noqa: E402
import git_trees  # noqa: E402
import make_repo  # noqa: E402
import one_shot  # noqa: E402
import session_exec  # noqa: E402
import shell_setup  # noqa: E402
import store  # noqa: E402
from evals import git_taint as taint  # noqa: E402
from tree_state import unsaved  # noqa: E402

KEY = "github:example/repo#7"
CLAUDE = "# Workspace\n\n## Coordinator\n\n- Worktrees: `trees/`\n"


class CleanupTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.enterContext(taint.private_git_views(self.root / "private-views"))
        self.enterContext(mock.patch.object(cleanup, "audit_env", side_effect=git_trees.audit_env))
        (self.root / "CLAUDE.md").write_text(CLAUDE)
        self.clone = make_repo.build(self.root, {"files": {".gitignore": "*.db\nnode_modules/\n"}, "worktrees": [
            {"name": "done", "branch": "done"},
            {"name": "unpushed", "branch": "unpushed", "merged": False},
            {"name": "dirty", "branch": "dirty", "dirty": True},
        ]})
        self.trees = self.root / "trees"
        self.db = self.root / "store.db"
        store.migrate(self.db)
        self.store = store.Store(self.db)
        self.addCleanup(self.store.engine.dispose)
        for name in ("done", "unpushed", "dirty"):
            self.finish(name)
        self.bin = self.root / "bin"  # no container CLI unless a test plants one
        self.bin.mkdir()
        path = mock.patch.dict(os.environ, {"PATH": f"{self.bin}:/usr/bin:/bin"})
        path.start()
        self.addCleanup(path.stop)
        self.lsof = mock.Mock(return_value=subprocess.CompletedProcess(["lsof"], 1, "", ""))
        real_run = subprocess.run
        self.ps = mock.Mock(wraps=real_run)

        def run(args, *positional, **kwargs):
            if args[0] == "lsof":
                return self.lsof(args, *positional, **kwargs)
            if args[0] == "ps":
                return self.ps(args, *positional, **kwargs)
            return real_run(args, *positional, **kwargs)

        processes = mock.patch.object(cleanup.subprocess, "run", side_effect=run)
        processes.start()
        self.addCleanup(processes.stop)

    def finish(self, name: str, state: str = "closed") -> None:
        key = f"{KEY}-{name}"
        self.store.upsert_item(key, forge_state=state)
        self.store.set_ownership(key, str(self.trees / name))

    def run_cleanup(self, *flags: str) -> tuple[int, str]:
        out = io.StringIO()
        with redirect_stdout(out):
            code = cleanup.main(["--root", str(self.root), "--store", str(self.db), *flags])
        return code, out.getvalue()

    def kept(self, out: str) -> dict[str, str]:
        return {line.split()[1]: line for line in out.splitlines() if line.startswith("keep ")}

    def removable(self, out: str) -> list[str]:
        return [line.split()[1] for line in out.splitlines() if line.startswith(("remove ", "removed "))]

    def worktrees(self) -> str:
        return make_repo.git(["worktree", "list", "--porcelain"], self.clone)

    def plant(self, name: str, body: str) -> None:
        exe = self.bin / name
        exe.write_text(f"#!{sys.executable}\n{body}")
        exe.chmod(0o755)

    # the preview

    def test_the_preview_lists_the_clean_finished_tree_and_why_each_other_was_kept_and_removes_nothing(self) -> None:
        code, out = self.run_cleanup()
        self.assertEqual(code, 0, out)
        self.assertEqual(self.removable(out), ["done"])
        kept = self.kept(out)
        self.assertIn("not on origin/main", kept["unpushed"])
        self.assertIn("uncommitted", kept["dirty"])
        self.assertTrue((self.trees / "done").is_dir())
        self.assertIn(str(self.trees / "done"), self.worktrees())
        self.assertEqual(self.store.log(f"{KEY}-done"), [])

    # removal

    def configure_teardown(self) -> None:
        (self.root / "CLAUDE.md").write_text(CLAUDE + "- Settings: `settings.toml`\n")
        (self.root / "settings.toml").write_text('[workers]\nteardown = "stack-stop"\n')

    def test_teardown_stops_a_stack_under_the_launch_lock_before_the_mount_check_and_removal(self) -> None:
        self.configure_teardown()
        tree = self.trees / "done"
        mounted = True

        def stop(command, cwd, **kwargs):
            nonlocal mounted
            self.assertEqual((command, cwd), ("stack-stop", self.root))
            self.assertEqual(kwargs, {"workspace": self.root, "worker": "done", "worktree": str(tree)})
            probe = os.open(self.trees, os.O_RDONLY)
            try:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
            finally:
                os.close(probe)
            mounted = False
            return subprocess.CompletedProcess([], 0, "", "")

        with mock.patch.object(shell_setup, "run_command", side_effect=stop) as command, \
                mock.patch.object(cleanup, "containers", side_effect=lambda: [tree / "data"] if mounted else []):
            code, out = self.run_cleanup("--remove")
        self.assertEqual(code, 0, out)
        command.assert_called_once()
        self.assertEqual(self.removable(out), ["done"])
        self.assertFalse(tree.exists())
        self.assertIn("removed done", self.store.log(f"{KEY}-done")[0].message)

    def test_failed_teardown_reports_and_logs_the_error_and_the_mount_still_keeps_the_tree(self) -> None:
        self.configure_teardown()
        tree = self.trees / "done"
        with mock.patch.object(shell_setup, "run_command", return_value=
                               subprocess.CompletedProcess([], 7, "", "stack service unavailable")) as command, \
                mock.patch.object(cleanup, "containers", return_value=[tree / "data"]):
            _, out = self.run_cleanup("--remove")
        command.assert_called_once()
        line = self.kept(out)["done"]
        self.assertIn("teardown: exit 7: stack service unavailable", line)
        self.assertIn("a container binds a path in it", line)
        self.assertTrue(tree.is_dir())
        self.assertEqual(self.store.log(f"{KEY}-done")[0].message, line)

    def test_a_teardown_warning_does_not_block_guarded_removal(self) -> None:
        self.configure_teardown()
        with mock.patch.object(shell_setup, "teardown", return_value=["teardown: exit 7"]) as stop, \
                mock.patch.object(cleanup, "containers", return_value=[]):
            _, out = self.run_cleanup("--remove")
        stop.assert_called_once_with(self.root, self.trees / "done", "done", "stack-stop")
        self.assertEqual(self.removable(out), ["done"])
        self.assertIn("teardown: exit 7", out)
        self.assertIn("teardown: exit 7", self.store.log(f"{KEY}-done")[0].message)

    def test_successful_teardown_does_not_bypass_a_remaining_or_unreadable_mount(self) -> None:
        self.configure_teardown()
        tree = self.trees / "done"
        for number, mounts in enumerate(([tree / "data"], None)):
            (self.root / "settings.toml").write_text(f'[workers]\nteardown = "stack-stop {number}"\n')
            with self.subTest(mounts=mounts), \
                    mock.patch.object(shell_setup, "teardown", return_value=[]) as stop, \
                    mock.patch.object(cleanup, "containers", return_value=mounts):
                _, out = self.run_cleanup("--remove")
                stop.assert_called_once()
                self.assertIn("container", self.kept(out)["done"])
                self.assertTrue(tree.is_dir())

    def test_a_retained_tree_is_torn_down_once_across_cleanup_restarts(self) -> None:
        self.configure_teardown()
        tree = self.trees / "done"
        for mounts in ([tree / "data"], None):
            with self.subTest(mounts=mounts), \
                    mock.patch.object(shell_setup, "teardown", return_value=[]) as stop, \
                    mock.patch.object(cleanup, "containers", return_value=mounts):
                for _ in range(2):
                    code, out = self.run_cleanup("--remove")  # a new Store on each pass
                    self.assertEqual(code, 0, out)
                    self.assertIn("container", self.kept(out)["done"])
                self.assertEqual(stop.call_count, 1 if mounts is not None else 0)
        self.assertEqual(len(self.store.log(f"{KEY}-done")), 2)  # the guard reason changed
        with mock.patch.object(shell_setup, "teardown") as stop, \
                mock.patch.object(cleanup, "containers", return_value=[]):
            code, out = self.run_cleanup("--remove")
        self.assertEqual(code, 0, out)
        stop.assert_not_called()
        self.assertEqual(self.removable(out), ["done"])

    def test_new_tree_at_the_same_path_gets_its_own_teardown(self) -> None:
        self.configure_teardown()
        tree = self.trees / "done"
        with mock.patch.object(shell_setup, "teardown", return_value=[]) as stop, \
                mock.patch.object(cleanup, "containers", return_value=[tree / "data"]):
            self.run_cleanup("--remove")
            make_repo.git(["worktree", "remove", str(tree)], self.clone)
            make_repo.git(["worktree", "add", str(tree), "done"], self.clone)
            self.run_cleanup("--remove")
        self.assertEqual(stop.call_count, 2)

    def test_tree_recreated_after_the_outer_attempt_check_gets_its_own_teardown(self) -> None:
        self.configure_teardown()
        tree = self.trees / "done"
        with mock.patch.object(shell_setup, "teardown", return_value=[]), \
                mock.patch.object(cleanup, "containers", return_value=[tree / "data"]):
            self.run_cleanup("--remove")
        real_lock = cleanup.trees_lock

        @contextlib.contextmanager
        def recreate(trees):
            with real_lock(trees):
                make_repo.git(["worktree", "remove", str(tree)], self.clone)
                make_repo.git(["worktree", "add", str(tree), "done"], self.clone)
                yield

        with mock.patch.object(cleanup, "trees_lock", recreate), \
                mock.patch.object(cleanup, "containers", return_value=[]), \
                mock.patch.object(shell_setup, "teardown", return_value=[]) as stop:
            code, out = self.run_cleanup("--remove")
        self.assertEqual(code, 0, out)
        stop.assert_called_once()
        self.assertFalse(tree.exists())

    def test_an_attempt_recorded_by_another_pass_still_checks_owners_after_mounts(self) -> None:
        self.configure_teardown()
        tree = self.trees / "done"
        real_lock = cleanup.trees_lock
        locked = False

        @contextlib.contextmanager
        def another_pass(trees):
            nonlocal locked
            with real_lock(trees):
                layout = cleanup.git_view.locate(tree)
                self.store.claim_teardown(str(tree), cleanup.teardown_identity(layout, "stack-stop"))
                locked = True
                yield
                locked = False

        def reopen():
            if locked:
                self.finish("done", state="open")
            return []

        with mock.patch.object(cleanup, "trees_lock", another_pass), \
                mock.patch.object(cleanup, "containers", side_effect=reopen), \
                mock.patch.object(shell_setup, "teardown") as stop:
            code, out = self.run_cleanup("--remove")
        self.assertEqual(code, 0, out)
        stop.assert_not_called()
        self.assertEqual(self.kept(out)["done"], f"keep done — {KEY}-done is not finished")
        self.assertTrue(tree.is_dir())

    def test_each_new_teardown_warning_is_logged_while_unchanged_guard_refusals_are_deduplicated(self) -> None:
        self.configure_teardown()
        tree = self.trees / "done"
        with mock.patch.object(shell_setup, "run_command", side_effect=[
                subprocess.TimeoutExpired(["shell", "chief-command-first"], 120),
                subprocess.TimeoutExpired(["shell", "chief-command-second"], 120)]) as command, \
                mock.patch.object(cleanup, "containers", return_value=[tree / "data"]):
            _, first = self.run_cleanup("--remove")
            # An explicitly changed command permits another attempt, even for this same tree.
            (self.root / "settings.toml").write_text('[workers]\nteardown = "stack-stop --retry"\n')
            _, second = self.run_cleanup("--remove")
            _, repeated = self.run_cleanup("--remove")
        self.assertEqual(command.call_count, 2)
        self.assertIn("chief-command-first", first)
        self.assertIn("chief-command-second", second)
        self.assertNotIn("teardown:", self.kept(repeated)["done"])
        self.assertEqual([entry.message for entry in self.store.log(f"{KEY}-done")],
                         [self.kept(first)["done"], self.kept(second)["done"]])

    def test_teardown_is_one_tree_attempt_even_when_an_owner_resume_write_would_fail(self) -> None:
        self.configure_teardown()
        tree = self.trees / "done"
        second = f"{KEY}-second"
        self.store.upsert_item(second, forge_state="closed")
        self.store.set_ownership(second, str(tree / "shared.txt"))
        real_set = store.Store.set_resume
        writes = 0

        def fail_second(db, key, state):
            nonlocal writes
            writes += 1
            if writes == 2:
                raise SQLAlchemyError("second owner write failed")
            real_set(db, key, state)

        with mock.patch.object(store.Store, "set_resume", fail_second), \
                mock.patch.object(shell_setup, "teardown", return_value=[]) as stop, \
                mock.patch.object(cleanup, "containers", return_value=[tree / "data"]):
            self.run_cleanup("--remove")
            self.run_cleanup("--remove")
        stop.assert_called_once()
        self.assertIsNone(self.store.resume(f"{KEY}-done"))
        self.assertIsNone(self.store.resume(second))

    def test_engine_resume_updates_and_cleanup_attempts_cannot_overwrite_each_other(self) -> None:
        self.configure_teardown()
        tree = self.trees / "done"
        key = f"{KEY}-done"
        stale = {"pending_stage": "implement"}
        recovered = {"pending_stage": "review"}
        self.store.set_resume(key, stale)

        with mock.patch.object(shell_setup, "teardown", return_value=[]) as stop, \
                mock.patch.object(cleanup, "containers", return_value=[tree / "data"]):
            with mock.patch.object(store.Store, "resume", autospec=True, side_effect=store.Store.resume) as read, \
                    mock.patch.object(store.Store, "set_resume", autospec=True,
                                      side_effect=store.Store.set_resume) as write:
                self.run_cleanup("--remove")
                read.assert_not_called()
                write.assert_not_called()
            # Cleanup must neither read nor replace the engine's resume document.
            self.assertEqual(self.store.resume(key), stale)
            self.store.set_resume(key, recovered)  # a later engine writer replaces its entire document
            self.run_cleanup("--remove")
        stop.assert_called_once()
        self.assertEqual(self.store.resume(key), recovered)

    def test_recorded_attempts_with_mount_refusals_skip_the_launch_lock_and_rechecks(self) -> None:
        self.configure_teardown()
        tree = self.trees / "done"
        with mock.patch.object(shell_setup, "teardown", return_value=[]), \
                mock.patch.object(cleanup, "containers", return_value=[tree / "data"]):
            self.run_cleanup("--remove")
        for mounts in ([tree / "data"], None):
            with self.subTest(mounts=mounts), \
                    mock.patch.object(cleanup, "trees_lock") as lock, \
                    mock.patch.object(cleanup, "owned_items", wraps=cleanup.owned_items) as owners, \
                    mock.patch.object(cleanup, "containers", return_value=mounts) as containers, \
                    mock.patch.object(shell_setup, "teardown") as stop:
                code, out = self.run_cleanup("--remove")
            self.assertEqual(code, 0, out)
            self.assertIn("container", self.kept(out)["done"])
            stop.assert_not_called()
            lock.assert_not_called()
            owners.assert_called_once()
            containers.assert_called_once()

    def test_replacing_an_owner_resume_document_does_not_forget_the_teardown(self) -> None:
        self.configure_teardown()
        tree = self.trees / "done"
        key = f"{KEY}-done"
        with mock.patch.object(shell_setup, "teardown", return_value=[]) as stop, \
                mock.patch.object(cleanup, "containers", return_value=[tree / "data"]):
            self.run_cleanup("--remove")
            self.store.set_resume(key, {"pending_stage": "review"})
            self.run_cleanup("--remove")
        stop.assert_called_once()
        self.assertEqual(self.store.resume(key), {"pending_stage": "review"})

    def test_attempt_store_failures_skip_teardown_and_postchecks_but_retain_mount_diagnostics(self) -> None:
        self.configure_teardown()
        tree = self.trees / "done"
        for method in ("teardown_attempt", "claim_teardown"):
            with self.subTest(method=method), \
                    mock.patch.object(store.Store, method, side_effect=SQLAlchemyError("unreadable"), create=True), \
                    mock.patch.object(cleanup, "owned_items", wraps=cleanup.owned_items) as owners, \
                    mock.patch.object(cleanup, "containers", return_value=[tree / "data"]) as containers, \
                    mock.patch.object(shell_setup, "teardown") as stop:
                code, out = self.run_cleanup("--remove")
            self.assertEqual(code, 1, out)
            self.assertIn("store teardown attempt cannot be checked", self.kept(out)["done"])
            self.assertIn("a container binds a path in it", self.kept(out)["done"])
            self.assertEqual(owners.call_count, 1 if method == "teardown_attempt" else 2)
            containers.assert_called_once()
            stop.assert_not_called()
            self.assertTrue(tree.is_dir())

    def test_locked_teardown_identity_failure_keeps_its_diagnostic_and_skips_teardown(self) -> None:
        self.configure_teardown()
        tree = self.trees / "done"
        identity = cleanup.teardown_identity(cleanup.git_view.locate(tree), "stack-stop")
        with mock.patch.object(cleanup, "teardown_identity", side_effect=[identity, OSError("unreadable")]), \
                mock.patch.object(cleanup, "containers", return_value=[tree / "data"]), \
                mock.patch.object(shell_setup, "teardown") as stop:
            code, out = self.run_cleanup("--remove")
        self.assertEqual(code, 1, out)
        self.assertEqual(self.kept(out)["done"],
                         "keep done — store teardown attempt cannot be checked (OSError); "
                         "a container binds a path in it")
        stop.assert_not_called()
        self.assertTrue(tree.is_dir())
        self.assertEqual(self.store.log(f"{KEY}-done")[-1].message, self.kept(out)["done"])

    def test_locked_precheck_refusals_retain_mount_diagnostics_without_running_teardown(self) -> None:
        self.configure_teardown()
        tree = self.trees / "done"
        real_lock = cleanup.trees_lock
        admin = cleanup.git_view.locate(tree).gitdir
        for refusal in ("layout", "claim", "git", "owner", "store"):
            for mounts, reason in (([tree / "data"], "a container binds a path in it"),
                                   (None, "containers cannot be checked")):
                with self.subTest(refusal=refusal, mounts=mounts):
                    @contextlib.contextmanager
                    def refuse(trees):
                        with real_lock(trees):
                            if refusal == "layout":
                                (admin / "locked").touch()
                            elif refusal == "claim":
                                self.lsof.return_value = subprocess.CompletedProcess(
                                    ["lsof"], 0, f"p12345\nn{tree}\n", "")
                            elif refusal == "git":
                                (tree / "new.txt").write_text("unsaved\n")
                            elif refusal == "owner":
                                self.finish("done", state="open")
                            with contextlib.ExitStack() as stack:
                                if refusal == "store":
                                    stack.enter_context(mock.patch.object(
                                        cleanup, "owned_items", side_effect=SQLAlchemyError("unreadable")))
                                yield

                    with mock.patch.object(cleanup, "trees_lock", refuse), \
                            mock.patch.object(cleanup, "containers", return_value=mounts) as containers, \
                            mock.patch.object(shell_setup, "teardown") as stop:
                        code, out = self.run_cleanup("--remove")
                    (admin / "locked").unlink(missing_ok=True)
                    (tree / "new.txt").unlink(missing_ok=True)
                    self.lsof.return_value = subprocess.CompletedProcess(["lsof"], 1, "", "")
                    self.finish("done")
                    stop.assert_not_called()
                    containers.assert_called_once()
                    self.assertEqual(code, 1 if refusal == "store" else 0, out)
                    line = self.kept(out)["done"]
                    self.assertIn(reason, line)
                    self.assertEqual(self.store.log(f"{KEY}-done")[-1].message, line)
                    if refusal == "owner":
                        self.assertEqual(line, f"keep done — {KEY}-done is not finished; {reason}")

    def test_teardown_deleting_the_tree_or_its_git_link_reports_partial_removal(self) -> None:
        self.configure_teardown()
        tree = self.trees / "done"
        admin = cleanup.git_view.locate(tree).gitdir
        original = (tree / ".git").read_text()
        for target in (tree / ".git", tree):
            with self.subTest(target=target):
                def stop(*args):
                    if target == tree:
                        cleanup.shutil.rmtree(tree)
                    else:
                        target.unlink()
                    return []

                with mock.patch.object(shell_setup, "teardown", side_effect=stop) as teardown:
                    code, out = self.run_cleanup("--remove")
                teardown.assert_called_once()
                self.assertEqual(code, 1, out)
                line = next(line for line in out.splitlines() if line.startswith("partially removed done:"))
                self.assertIn(f"admin dir remains ({admin})", line)
                self.assertTrue(admin.is_dir())
                self.assertEqual(self.store.log(f"{KEY}-done")[-1].message, line)
                if target != tree:
                    (tree / ".git").write_text(original)
                    # A changed command represents an explicit second attempt for the next subcase.
                    (self.root / "settings.toml").write_text('[workers]\nteardown = "stack-stop --retry"\n')

    def test_outer_refusals_retain_mount_reasons_with_teardown_configured(self) -> None:
        self.configure_teardown()
        self.finish("done", state="open")
        for mounts, reason in (([self.trees / "done" / "data"], "a container binds a path in it"),
                               (None, "containers cannot be checked")):
            with self.subTest(mounts=mounts), \
                    mock.patch.object(cleanup, "containers", return_value=mounts), \
                    mock.patch.object(shell_setup, "teardown") as stop:
                _, preview = self.run_cleanup()
                _, out = self.run_cleanup("--remove")
            stop.assert_not_called()
            self.assertEqual(self.kept(out), self.kept(preview))
            self.assertIn(reason, self.kept(out)["done"])
            self.assertEqual(self.store.log(f"{KEY}-done")[-1].message, self.kept(out)["done"])

    def test_preview_explains_that_teardown_can_make_a_mounted_tree_removable(self) -> None:
        self.configure_teardown()
        with mock.patch.object(cleanup, "containers", return_value=[self.trees / "done" / "data"]):
            _, out = self.run_cleanup()
        self.assertIn("--remove runs teardown before rechecking mounts", out)

    def test_preview_with_teardown_configured_never_runs_it(self) -> None:
        self.configure_teardown()
        tree = self.trees / "done"
        with mock.patch.object(shell_setup, "teardown") as stop, \
                mock.patch.object(cleanup, "containers", return_value=[tree / "data"]):
            code, out = self.run_cleanup()
        self.assertEqual(code, 0, out)
        stop.assert_not_called()
        self.assertIn("container", self.kept(out)["done"])
        self.assertTrue(tree.is_dir())

    def test_unconfigured_teardown_is_not_called(self) -> None:
        with mock.patch.object(shell_setup, "teardown") as stop:
            code, out = self.run_cleanup("--remove")
        self.assertEqual(code, 0, out)
        stop.assert_not_called()
        self.assertEqual(self.removable(out), ["done"])

    def test_teardown_never_runs_for_unfinished_unowned_dirty_or_unlanded_trees(self) -> None:
        self.configure_teardown()
        self.finish("done", state="open")
        make_repo.git(["worktree", "add", "-b", "stray", str(self.trees / "stray"), "main"], self.clone)
        with mock.patch.object(shell_setup, "teardown") as stop:
            _, out = self.run_cleanup("--remove")
        stop.assert_not_called()
        self.assertEqual(self.removable(out), [])

    def test_a_worker_claim_in_the_locked_recheck_prevents_teardown(self) -> None:
        self.configure_teardown()
        real_lock = cleanup.trees_lock

        @contextlib.contextmanager
        def claim_then_lock(trees):
            with real_lock(trees):
                self.lsof.return_value = subprocess.CompletedProcess(["lsof"], 0, f"p12345\nn{trees / 'done'}\n", "")
                yield

        with mock.patch.object(cleanup, "trees_lock", claim_then_lock), \
                mock.patch.object(shell_setup, "teardown") as stop:
            _, out = self.run_cleanup("--remove")
        stop.assert_not_called()
        self.assertIn("live process in tree", self.kept(out)["done"])

    def test_an_owner_reopened_before_the_locked_recheck_prevents_teardown(self) -> None:
        self.configure_teardown()
        real_lock = cleanup.trees_lock

        @contextlib.contextmanager
        def reopen_then_lock(trees):
            with real_lock(trees):
                self.finish("done", state="open")
                yield

        with mock.patch.object(cleanup, "trees_lock", reopen_then_lock), \
                mock.patch.object(shell_setup, "teardown") as stop:
            _, out = self.run_cleanup("--remove")
        stop.assert_not_called()
        self.assertEqual(self.kept(out)["done"], f"keep done — {KEY}-done is not finished")

    def test_teardown_cannot_invalidate_another_removal_guard(self) -> None:
        self.configure_teardown()
        tree = self.trees / "done"
        admin = cleanup.git_view.locate(tree).gitdir

        def stop(*args):
            (tree / "keep.txt").write_text("unsaved teardown output\n")
            (admin / "locked").touch()
            self.finish("done", state="open")
            self.lsof.return_value = subprocess.CompletedProcess(["lsof"], 0, f"p12345\nn{tree}\n", "")
            return []

        with mock.patch.object(shell_setup, "teardown", side_effect=stop) as teardown, \
                mock.patch.object(cleanup, "containers", return_value=[]):
            _, out = self.run_cleanup("--remove")
        teardown.assert_called_once()
        for reason in ("keep.txt", "locked", "not finished", "live process in tree"):
            self.assertIn(reason, self.kept(out)["done"])
        self.assertTrue(tree.is_dir())

    def test_a_tree_redirected_to_another_repository_is_kept(self) -> None:
        (self.root / "CLAUDE.md").write_text(CLAUDE + "- Settings: `settings.toml`\n")
        (self.root / "settings.toml").write_text('[repos]\nproject = "repo"\n')
        other_root = self.root / "other"
        other = make_repo.build(other_root, {"files": {".gitignore": "*.db\nnode_modules/\n"},
                                            "worktrees": [{"name": "decoy", "branch": "decoy"}]})
        tree = self.trees / "done"
        original = (tree / ".git").read_text()
        admin = cleanup.git_view.locate(other_root / "trees" / "decoy").gitdir
        (admin / "gitdir").write_text(str(tree / ".git") + "\n")
        (tree / ".git").write_text(f"gitdir: {admin}\n")
        with mock.patch.object(cleanup.git_view, "_opened", wraps=cleanup.git_view._opened) as opened:
            code, out = self.run_cleanup("--remove")
        self.assertEqual(code, 0, out)
        self.assertIn("git cannot read this tree", self.kept(out)["done"])
        self.assertTrue(tree.is_dir() and admin.is_dir())
        self.assertFalse(any(call.args[0] == tree for call in opened.call_args_list))
        self.assertTrue((other / ".git").is_dir())
        (tree / ".git").write_text(original)
        lock = cleanup.trees_lock

        @contextlib.contextmanager
        def redirect(trees):
            with lock(trees):
                (tree / ".git").write_text(f"gitdir: {admin}\n")
                yield

        with mock.patch.object(cleanup, "trees_lock", redirect):
            code, out = self.run_cleanup("--remove")
        self.assertEqual(code, 0, out)
        self.assertIn("git cannot read this tree", self.kept(out)["done"])
        self.assertTrue(tree.is_dir() and admin.is_dir())


    def test_remove_deletes_the_clean_finished_tree_and_its_build_output_and_logs_it(self) -> None:
        admin = cleanup.git_view.locate(self.trees / "done").gitdir
        (self.trees / "done" / "node_modules" / "pkg").mkdir(parents=True)
        (self.trees / "done" / "node_modules" / "pkg" / "index.js").write_text("x\n")
        code, out = self.run_cleanup("--remove")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.removable(out), ["done"])
        self.assertFalse((self.trees / "done").exists())
        self.assertFalse(admin.exists())
        self.assertNotIn(str(self.trees / "done"), self.worktrees())
        self.assertTrue((self.trees / "unpushed").is_dir() and (self.trees / "dirty").is_dir())
        self.assertIn("removed", self.store.log(f"{KEY}-done")[0].message)
        self.assertTrue(self.store.log(f"{KEY}-unpushed")[0].message.startswith("keep unpushed"))

    def test_finished_worker_metadata_and_build_output_are_removable(self) -> None:
        tree = self.trees / "done"
        dispatch_prompt.write_dispatch(tree, "finished assignment")
        (tree / ".chief-of-stuff" / "worker-stderr.log").write_text("finished\n")
        (tree / "node_modules").mkdir()
        (tree / "node_modules" / "index.js").write_text("generated\n")
        _, out = self.run_cleanup("--remove")
        self.assertEqual(self.removable(out), ["done"])
        self.assertFalse(tree.exists())

    def test_a_failing_admin_dir_removal_reports_partial_removal_and_logs_it(self) -> None:
        tree = self.trees / "done"
        admin = cleanup.git_view.locate(tree).gitdir
        real = cleanup.shutil.rmtree

        def remove(path: Path, *args, **kwargs) -> None:
            if path == admin:
                raise PermissionError("admin dir is busy")
            real(path, *args, **kwargs)

        with mock.patch.object(cleanup.shutil, "rmtree", side_effect=remove):
            code, out = self.run_cleanup("--remove")
        self.assertEqual(code, 1, out)
        self.assertFalse(tree.exists())
        self.assertTrue(admin.is_dir())
        line = next(line for line in out.splitlines() if line.startswith("partially removed done:"))
        self.assertIn("admin dir remains", line)
        self.assertIn(str(admin), line)
        self.assertEqual(self.store.log(f"{KEY}-done")[0].message, line)

    def test_a_tree_removal_that_fails_partway_reports_what_remains(self) -> None:
        tree = self.trees / "done"
        admin = cleanup.git_view.locate(tree).gitdir
        (tree / "node_modules").mkdir()
        real = cleanup.shutil.rmtree

        def remove(path: Path, *args, **kwargs) -> None:
            if path == tree:
                (tree / "node_modules").rmdir()
                raise PermissionError("tree is busy")
            real(path, *args, **kwargs)

        with mock.patch.object(cleanup.shutil, "rmtree", side_effect=remove):
            code, out = self.run_cleanup("--remove")
        self.assertEqual(code, 1, out)
        self.assertTrue(tree.is_dir() and admin.is_dir())
        line = next(line for line in out.splitlines() if line.startswith("partially removed done:"))
        self.assertIn("tree remains", line)
        self.assertIn("admin dir remains", line)
        self.assertEqual(self.store.log(f"{KEY}-done")[0].message, line)

    def test_cleanup_logs_use_the_stores_injected_clock(self) -> None:
        instant = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
        with mock.patch.object(cleanup, "Store", side_effect=lambda path: store.Store(path, clock=lambda: instant)):
            code, out = self.run_cleanup("--remove")
        self.assertEqual(code, 0, out)
        for name in ("done", "unpushed", "dirty"):
            self.assertEqual(self.store.log(f"{KEY}-{name}")[0].at, instant.isoformat())

    def test_two_passes_over_a_retained_tree_log_once_despite_an_unrelated_log(self) -> None:
        key = f"{KEY}-done"
        self.finish("done", state="open")
        with redirect_stdout(io.StringIO()):
            self.assertEqual(cleanup.execute(self.root, self.trees, self.store, remove=True), 0)
            self.store.add_log(key, "wait: human review", self.store.clock().isoformat())
            self.assertEqual(cleanup.execute(self.root, self.trees, self.store, remove=True), 0)
        self.assertTrue((self.trees / "done").is_dir())
        self.assertEqual([entry.message for entry in self.store.log(key)],
                         [f"keep done — {key} is not finished", "wait: human review"])

    def test_changed_cleanup_results_and_removal_still_log(self) -> None:
        key = f"{KEY}-done"
        self.finish("done", state="open")
        with redirect_stdout(io.StringIO()):
            self.assertEqual(cleanup.execute(self.root, self.trees, self.store, remove=True), 0)
            snapshot = self.trees / "done" / "snapshot.db"
            snapshot.write_text("only copy\n")
            self.assertEqual(cleanup.execute(self.root, self.trees, self.store, remove=True), 0)
            snapshot.unlink()
            self.assertEqual(cleanup.execute(self.root, self.trees, self.store, remove=True), 0)
            self.finish("done")
            self.assertEqual(cleanup.execute(self.root, self.trees, self.store, remove=True), 0)
        messages = [entry.message for entry in self.store.log(key)]
        self.assertEqual(len(messages), 4)
        self.assertIn("snapshot.db", messages[1])
        self.assertEqual(messages[0], messages[2])
        self.assertEqual(messages[3], f"removed done — {key}")
        self.assertFalse((self.trees / "done").exists())

    def test_a_recreated_tree_removed_again_logs_the_second_removal(self) -> None:
        key = f"{KEY}-done"
        self.finish("done")
        self.store.add_log(key, f"removed done — {key}", self.store.clock().isoformat())  # an earlier tree of this name
        with redirect_stdout(io.StringIO()):
            self.assertEqual(cleanup.execute(self.root, self.trees, self.store, remove=True), 0)
        self.assertEqual([entry.message for entry in self.store.log(key)], [f"removed done — {key}"] * 2)

    def test_a_failed_removal_log_prints_the_result_and_continues_with_other_trees(self) -> None:
        real = store.Store.add_log
        err = io.StringIO()

        def log(db, key: str, message: str, at: str) -> None:
            if key == f"{KEY}-done":
                raise cleanup.SQLAlchemyError("store is locked")
            real(db, key, message, at)

        # Put the removed tree first so the failure must not skip the remaining trees' results or logs.
        with mock.patch.object(cleanup, "discover", return_value=[self.trees / name for name in
                                                                ("done", "unpushed", "dirty")]), \
                mock.patch.object(store.Store, "add_log", log), redirect_stderr(err):
            code, out = self.run_cleanup("--remove")
        self.assertEqual(code, 1, out)
        self.assertEqual(self.removable(out), ["done"])
        self.assertFalse((self.trees / "done").exists())
        self.assertIn(f"{KEY}-done", err.getvalue())
        self.assertIn("SQLAlchemyError", err.getvalue())
        for name in ("unpushed", "dirty"):
            self.assertEqual(self.store.log(f"{KEY}-{name}")[0].message, self.kept(out)[name])

    def test_a_finished_one_shot_without_a_launcher_report_keeps_its_evidence(self) -> None:
        tree = self.trees / "done"
        dispatch_prompt.write_dispatch(tree, "orphaned assignment")
        (tree / one_shot.PIDFILE).write_text(f"2147483647 {KEY}\n")  # the worker is gone
        result = tree / one_shot.RESULT
        result.write_text("status: done\nreason: finished\nchanges: checked\n")
        _, out = self.run_cleanup("--remove")
        self.assertIn("one-shot awaiting reconciliation", self.kept(out)["done"])
        self.assertTrue(result.is_file())

    def test_a_finished_one_shot_with_a_launcher_report_is_removable(self) -> None:
        tree = self.trees / "done"
        dispatch_prompt.write_dispatch(tree, "adopted assignment")
        (tree / one_shot.PIDFILE).write_text(f"2147483647 {KEY}\n")
        one_shot.write_report(self.root, tree, {"status": "done", "task": KEY, "worker": "worker-1",
                                              "reason": "finished", "changes": "checked", "errors": []})
        _, out = self.run_cleanup("--remove")
        self.assertEqual(self.removable(out), ["done"])
        self.assertFalse(tree.exists())
        self.assertTrue(one_shot.launcher_copy(self.root, tree).is_file())

    def test_a_launcher_report_not_newer_than_the_pid_file_keeps_the_current_runs_evidence(self) -> None:
        tree = self.trees / "done"
        dispatch = dispatch_prompt.write_dispatch(tree, "new orphaned assignment")
        one_shot.write_report(self.root, tree, {"status": "done", "task": KEY, "worker": "worker-1",
                                              "reason": "earlier run", "changes": "checked", "errors": []})
        report = one_shot.launcher_copy(self.root, tree)
        pidfile = tree / one_shot.PIDFILE
        pidfile.write_text(f"2147483647 {KEY}\n")
        result = tree / one_shot.RESULT
        result.write_text("status: done\nreason: current run\nchanges: checked\n")
        stamp = pidfile.stat().st_mtime_ns
        for report_stamp in (stamp - 1_000_000_000, stamp):
            with self.subTest(report_stamp=report_stamp):
                os.utime(report, ns=(report_stamp, report_stamp))
                _, out = self.run_cleanup("--remove")
                self.assertIn("one-shot awaiting reconciliation", self.kept(out)["done"])
                self.assertTrue(dispatch.is_file() and result.is_file())

    # each guard keeps the tree

    def test_unpushed_commits_keep_the_tree(self) -> None:
        _, out = self.run_cleanup("--remove")
        self.assertIn("1 not on origin/main; no upstream", self.kept(out)["unpushed"])
        self.assertTrue((self.trees / "unpushed" / "unpushed.txt").is_file())

    def test_uncommitted_files_keep_the_tree(self) -> None:
        _, out = self.run_cleanup("--remove")
        self.assertIn("1 uncommitted", self.kept(out)["dirty"])
        self.assertTrue((self.trees / "dirty" / "scratch.txt").is_file())

    def test_an_ignored_file_that_is_not_build_output_keeps_the_tree(self) -> None:
        (self.trees / "done" / "snapshot.db").write_text("only copy\n")
        _, out = self.run_cleanup("--remove")
        self.assertIn("snapshot.db", self.kept(out)["done"])
        self.assertTrue((self.trees / "done" / "snapshot.db").is_file())

    def test_a_clean_tree_mid_bisect_keeps_its_operation_state(self) -> None:
        tree = self.trees / "done"
        make_repo.git(["bisect", "start"], tree)
        admin = cleanup.git_view.locate(tree).gitdir
        self.assertEqual(make_repo.git(["status", "--porcelain", "--ignored"], tree), "")
        self.assertEqual(cleanup.git_guards(tree), [])
        code, out = self.run_cleanup("--remove")
        self.assertEqual(code, 0, out)
        self.assertIn("git operation in progress (BISECT_LOG)", self.kept(out).get("done", out))
        self.assertTrue(tree.is_dir() and (admin / "BISECT_LOG").is_file())

    def test_each_git_operation_marker_keeps_the_layout(self) -> None:
        tree = self.trees / "done"
        admin = cleanup.git_view.locate(tree).gitdir
        for name in ("BISECT_LOG", "rebase-merge", "rebase-apply", "MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD"):
            with self.subTest(name=name):
                marker = admin / name
                if name.startswith("rebase-"):
                    marker.mkdir()
                else:
                    marker.write_text(make_repo.git(["rev-parse", "HEAD"], tree) + "\n")
                try:
                    _, kept = cleanup.layout_guards(tree)
                    self.assertEqual(kept, [f"git operation in progress ({name})"])
                finally:
                    if marker.is_dir():
                        marker.rmdir()
                    else:
                        marker.unlink()

    def test_an_edited_assume_unchanged_file_keeps_the_tree(self) -> None:
        tree = self.trees / "done"
        make_repo.git(["update-index", "--assume-unchanged", "README.md"], tree)
        (tree / "README.md").write_text("only copy\n")
        self.assertEqual(make_repo.git(["status", "--porcelain", "--ignored"], tree), "")
        code, out = self.run_cleanup("--remove")
        self.assertEqual(code, 0, out)
        self.assertIn("1 files hidden from git status by index flags", self.kept(out).get("done", out))
        self.assertEqual((tree / "README.md").read_text(), "only copy\n")

    def test_a_skip_worktree_file_keeps_the_tree(self) -> None:
        tree = self.trees / "done"
        make_repo.git(["update-index", "--skip-worktree", "README.md"], tree)
        self.assertEqual(make_repo.git(["status", "--porcelain", "--ignored"], tree), "")
        code, out = self.run_cleanup("--remove")
        self.assertEqual(code, 0, out)
        self.assertIn("1 files hidden from git status by index flags", self.kept(out).get("done", out))
        self.assertTrue(tree.is_dir())

    def test_unreadable_index_flags_keep_the_tree(self) -> None:
        tree = self.trees / "done"
        real = cleanup.git

        def git(args, cwd, view=None):
            if args == ["ls-files", "-v"]:
                self.assertIsNotNone(view)
                return 128, "unreadable index"
            return real(args, cwd, view)

        with mock.patch.object(cleanup, "git", side_effect=git):
            code, out = self.run_cleanup("--remove")
        self.assertEqual(code, 0, out)
        self.assertIn("git ls-files -v is unreadable", self.kept(out).get("done", out))
        self.assertTrue(tree.is_dir())

    def test_cache_error_keeps_the_tree_and_names_the_configuration_problem(self) -> None:
        reason = "cannot prepare view cache base '/unusable-cache'"
        with mock.patch.object(cleanup.git_view, "opened", side_effect=cleanup.git_view.CacheError(reason)):
            self.assertEqual(cleanup.git_guards(self.trees / "done"), [f"git view cache configuration error: {reason}"])

    def test_relative_default_cache_keeps_every_tree_without_a_traceback(self) -> None:
        real_base = cleanup.git_view._base
        for flags in ((), ("--remove",)):
            with self.subTest(flags=flags), mock.patch.dict(os.environ, {"XDG_CACHE_HOME": "relative-cache"}), \
                    mock.patch.object(cleanup.git_view, "_base", side_effect=lambda layout, base: real_base(layout, None)):
                code, out = self.run_cleanup(*flags)
                self.assertEqual(code, 0, out)
                self.assertEqual(self.removable(out), [])
                for name in ("done", "unpushed", "dirty"):
                    self.assertIn("git view cache configuration error: view cache base 'relative-cache' is relative",
                                  self.kept(out)[name])
                    self.assertTrue((self.trees / name).is_dir())
                self.assertNotIn("Traceback", out)

    def test_a_git_operation_started_in_the_final_locked_recheck_keeps_the_tree(self) -> None:
        tree = self.trees / "done"
        admin = cleanup.git_view.locate(tree).gitdir
        real_lock = cleanup.trees_lock

        @contextlib.contextmanager
        def bisect_then_check(trees: Path):
            with real_lock(trees):
                make_repo.git(["bisect", "start"], tree)
                yield

        with mock.patch.object(cleanup, "trees_lock", bisect_then_check):
            _, out = self.run_cleanup("--remove")
        self.assertIn("git operation in progress (BISECT_LOG)", self.kept(out).get("done", out))
        self.assertTrue(tree.is_dir() and (admin / "BISECT_LOG").is_file())

    def test_index_flags_added_in_the_final_locked_recheck_keep_hidden_edits(self) -> None:
        tree = self.trees / "done"
        real_lock = cleanup.trees_lock

        @contextlib.contextmanager
        def hide_edits_then_check(trees: Path):
            with real_lock(trees):
                make_repo.git(["update-index", "--skip-worktree", "README.md"], tree)
                make_repo.git(["update-index", "--assume-unchanged", "README.md"], tree)
                make_repo.git(["update-index", "--assume-unchanged", ".gitignore"], tree)
                (tree / "README.md").write_text("only copy\n")
                self.assertIn("s README.md", make_repo.git(["ls-files", "-v"], tree))
                self.assertEqual(make_repo.git(["status", "--porcelain", "--ignored"], tree), "")
                yield

        with mock.patch.object(cleanup, "trees_lock", hide_edits_then_check):
            _, out = self.run_cleanup("--remove")
        self.assertIn("2 files hidden from git status by index flags", self.kept(out).get("done", out))
        self.assertEqual((tree / "README.md").read_text(), "only copy\n")

    def test_initialized_submodule_metadata_keeps_ignored_evidence_without_a_gitlink(self) -> None:
        tree = self.trees / "done"
        make_repo.git(["-c", "protocol.file.allow=always", "submodule", "add", "-f",
                       str(self.clone), "node_modules"], tree)
        # Removing the gitlink leaves the initialized module behind in otherwise removable build output.
        make_repo.git(["rm", "--cached", "node_modules", ".gitmodules"], tree)
        (tree / ".gitmodules").unlink()
        snapshot = tree / "node_modules" / "snapshot.db"
        snapshot.write_text("only copy\n")
        self.assertEqual(make_repo.git(["status", "--porcelain", "--ignored"], tree), "!! node_modules/")
        self.assertEqual(cleanup.git_guards(tree), [])
        for flags in ((), ("--remove",)):
            with self.subTest(flags=flags):
                code, out = self.run_cleanup(*flags)
                self.assertEqual(code, 0, out)
                self.assertIn("initialized submodules", self.kept(out).get("done", out))
                self.assertEqual(snapshot.read_text(), "only copy\n")

    def test_empty_submodule_metadata_does_not_keep_the_tree(self) -> None:
        tree = self.trees / "done"
        (cleanup.git_view.locate(tree).gitdir / "modules").mkdir()
        _, out = self.run_cleanup("--remove")
        self.assertEqual(self.removable(out), ["done"])
        self.assertFalse(tree.exists())

    def test_submodule_metadata_added_after_the_first_pass_keeps_the_tree(self) -> None:
        tree = self.trees / "done"
        modules = cleanup.git_view.locate(tree).gitdir / "modules"
        real_lock = cleanup.trees_lock

        @contextlib.contextmanager
        def initialize_then_lock(trees: Path):
            (modules / "mod").mkdir(parents=True)
            with real_lock(trees):
                yield

        with mock.patch.object(cleanup, "trees_lock", initialize_then_lock):
            _, out = self.run_cleanup("--remove")
        self.assertIn("initialized submodules", self.kept(out).get("done", out))
        self.assertTrue(tree.is_dir())

    def test_a_live_worker_keeps_the_tree(self) -> None:
        tree = self.trees / "done"
        worker = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)", "claude", str(tree)])
        self.addCleanup(worker.wait)
        self.addCleanup(worker.kill)
        self.register(worker.pid, tree)
        # Keep the real PID existence check; provide command evidence without a sandbox-blocked ps.
        self.ps.return_value = subprocess.CompletedProcess(
            ["ps"], 0, f"{worker.pid} {sys.executable} -c sleep claude {tree}\n", "")
        _, out = self.run_cleanup("--remove")
        self.assertIn("live worker", self.kept(out)["done"])
        self.assertTrue(tree.is_dir())

    def test_a_live_process_with_its_cwd_at_or_inside_the_tree_keeps_it(self) -> None:
        tree = self.trees / "done"
        for cwd in (tree, tree / "node_modules" / "pkg"):
            with self.subTest(cwd=cwd):
                self.lsof.return_value = subprocess.CompletedProcess(["lsof"], 0, f"p12345\nfcwd\nn{cwd}\n", "")
                _, out = self.run_cleanup("--remove")
                self.assertIn("live process in tree (pid 12345)", self.kept(out).get("done", out))
                self.assertTrue(tree.is_dir())
        self.lsof.assert_called_with(["lsof", "-a", "-d", "cwd", "-Fpn"],
                                     capture_output=True, text=True, timeout=cleanup.TIMEOUT)

    def test_a_process_in_a_sibling_with_the_same_prefix_does_not_keep_the_tree(self) -> None:
        cwd = self.trees / "done-other"
        self.lsof.return_value = subprocess.CompletedProcess(["lsof"], 0, f"p12345\nn{cwd}\n", "")
        _, out = self.run_cleanup("--remove")
        self.assertEqual(self.removable(out), ["done"])
        self.assertFalse((self.trees / "done").exists())
        self.lsof.assert_called()

    def test_lsof_failure_keeps_the_tree(self) -> None:
        for failure in (subprocess.CompletedProcess(["lsof"], 2, "", "failed"),
                        subprocess.CompletedProcess(["lsof"], 1, "p12345\nn/\n", ""),
                        FileNotFoundError("lsof"), subprocess.TimeoutExpired("lsof", cleanup.TIMEOUT),
                        subprocess.CompletedProcess(["lsof"], 1, "", "failed")):
            with self.subTest(failure=failure):
                self.lsof.side_effect = failure if isinstance(failure, Exception) else None
                self.lsof.return_value = failure
                _, out = self.run_cleanup("--remove")
                self.assertIn("open processes cannot be checked", self.kept(out).get("done", out))
                self.assertTrue((self.trees / "done").is_dir())

    def test_unparseable_lsof_output_keeps_the_tree(self) -> None:
        for output in ("garbage\n", "n/tmp\n", "pbad\nn/tmp\n", "p12345\n", "p12345\nnrelative\n"):
            with self.subTest(output=output):
                self.lsof.return_value = subprocess.CompletedProcess(["lsof"], 0, output, "")
                _, out = self.run_cleanup("--remove")
                self.assertIn("open processes cannot be checked", self.kept(out).get("done", out))
                self.assertTrue((self.trees / "done").is_dir())

    def test_lsof_exit_one_with_no_output_adds_no_claim(self) -> None:
        _, out = self.run_cleanup("--remove")
        self.assertEqual(self.removable(out), ["done"])
        self.assertFalse((self.trees / "done").exists())
        self.assertNotIn("open processes cannot be checked", out)
        self.lsof.assert_called()

    def test_cleanup_excludes_its_own_cwd_claim(self) -> None:
        tree = self.trees / "done"
        self.lsof.return_value = subprocess.CompletedProcess(["lsof"], 0, f"p{os.getpid()}\nn{tree}\n", "")
        _, out = self.run_cleanup("--remove")
        self.assertEqual(self.removable(out), ["done"])
        self.assertFalse(tree.exists())
        self.lsof.assert_called()

    def test_a_process_cwd_claim_appearing_in_the_final_locked_recheck_keeps_the_tree(self) -> None:
        tree = self.trees / "done"
        real_lock = cleanup.trees_lock

        @contextlib.contextmanager
        def launch_then_lock(trees: Path):
            with real_lock(trees):
                def processes(*args, **kwargs):
                    probe = os.open(trees, os.O_RDONLY)
                    try:
                        with self.assertRaises(BlockingIOError):
                            fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    finally:
                        os.close(probe)
                    return subprocess.CompletedProcess(["lsof"], 0, f"p12345\nn{tree}\n", "")

                self.lsof.side_effect = processes
                yield
                self.lsof.side_effect = None

        with mock.patch.object(cleanup, "trees_lock", launch_then_lock):
            _, out = self.run_cleanup("--remove")
        self.assertIn("live process in tree (pid 12345)", self.kept(out).get("done", out))
        self.assertTrue(tree.is_dir())

    def test_a_reused_pid_is_not_a_live_worker(self) -> None:
        self.register(os.getpid(), self.trees / "done")  # alive, but its command is this test, not the worker
        self.ps.return_value = subprocess.CompletedProcess(
            ["ps"], 0, f"{os.getpid()} {sys.executable} -m unittest evals.test_cleanup\n", "")
        _, out = self.run_cleanup("--remove")
        self.assertEqual(self.removable(out), ["done"])
        self.assertFalse((self.trees / "done").exists())

    def test_a_worker_that_cannot_be_verified_keeps_the_tree(self) -> None:
        self.register(os.getpid(), self.trees / "done")
        with mock.patch.object(cleanup, "check", return_value=[{"pid": os.getpid(), "status": "unverified"}]):
            _, out = self.run_cleanup("--remove")
        self.assertIn("cannot be verified", self.kept(out)["done"])
        self.assertTrue((self.trees / "done").is_dir())

    def test_an_unreadable_registration_keeps_every_tree(self) -> None:
        sessions = self.root / ".chief-of-stuff" / "sessions"
        sessions.mkdir(parents=True)
        (sessions / "broken.json").write_text("{not json")
        _, out = self.run_cleanup("--remove")
        self.assertIn("registry", self.kept(out)["done"])
        self.assertTrue((self.trees / "done").is_dir())

    def test_a_live_one_shot_keeps_the_tree(self) -> None:
        pidfile = self.trees / "done" / ".chief-of-stuff" / "one-shot.pid"
        pidfile.parent.mkdir()
        pidfile.write_text(f"{os.getpid()} {KEY}\n")
        _, out = self.run_cleanup("--remove")
        self.assertIn("one-shot", self.kept(out)["done"])
        self.assertTrue(pidfile.is_file())

    def test_a_claim_made_after_the_checks_but_before_the_delete_keeps_the_tree(self) -> None:
        # A one-shot launch writes its pid file under the Worktrees dir's lock: once cleanup holds that lock, the
        # launch has either claimed the tree already, and the re-check sees it, or waits until the tree is gone.
        pidfile = self.trees / "done" / ".chief-of-stuff" / "one-shot.pid"
        real = cleanup.trees_lock

        @contextlib.contextmanager
        def launch_then_lock(trees: Path):
            self.assertEqual(trees, self.trees)
            pidfile.parent.mkdir(exist_ok=True)
            pidfile.write_text(f"{os.getpid()} {KEY}\n")
            with real(trees):
                yield

        with mock.patch.object(cleanup, "trees_lock", launch_then_lock):
            _, out = self.run_cleanup("--remove")
        self.assertIn("one-shot", self.kept(out)["done"])
        self.assertTrue(pidfile.is_file())

    def test_removal_holds_the_lock_one_shot_launches_take(self) -> None:
        import one_shot
        self.assertIs(cleanup.trees_lock, one_shot.trees_lock)

    def test_interactive_registration_after_the_first_pass_holds_the_lock_and_keeps_the_tree(self) -> None:
        tree = self.trees / "done"
        registry = self.root / ".chief-of-stuff" / "sessions" / "worker.json"
        real_lock, real_replace = cleanup.trees_lock, Path.replace

        def publish(path: Path, target: Path) -> Path:
            if target == registry:
                probe = os.open(self.trees, os.O_RDONLY)
                try:
                    with self.assertRaises(BlockingIOError):
                        fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
                finally:
                    os.close(probe)
            return real_replace(path, target)

        @contextlib.contextmanager
        def register_then_lock(trees: Path):
            session_exec.register(registry, runtime="claude", name="worker", worktree=str(tree), pid=os.getpid())
            with real_lock(trees):
                yield

        with mock.patch.object(Path, "replace", publish), \
                mock.patch.object(cleanup, "trees_lock", register_then_lock), \
                mock.patch.object(cleanup, "check", return_value=[{"pid": os.getpid(), "status": "running"}]):
            _, out = self.run_cleanup("--remove")
        self.assertIn("live worker", self.kept(out)["done"])
        self.assertTrue(tree.is_dir())

    def test_registration_refuses_to_launch_when_cleanup_wins_the_lock(self) -> None:
        tree = self.trees / "done"
        registry = self.root / ".chief-of-stuff" / "sessions" / "worker.json"
        real_lock = session_exec.trees_lock

        @contextlib.contextmanager
        def cleanup_then_lock(trees: Path):
            self.assertEqual(trees, self.trees)
            self.assertTrue(tree.is_dir())
            code, out = self.run_cleanup("--remove")
            self.assertEqual(code, 0, out)
            self.assertFalse(tree.exists())
            with real_lock(trees):
                yield

        with mock.patch.object(session_exec, "trees_lock", cleanup_then_lock), \
                mock.patch.object(session_exec.os, "execve", side_effect=AssertionError("worker launched")) as launch, \
                self.assertRaisesRegex(FileNotFoundError, "worktree"):
            session_exec.main(["--registry", str(registry), "--runtime", "cursor", "--name", "worker",
                               "--worktree", str(tree), "--", "/bin/fake-agent"])
        launch.assert_not_called()
        self.assertFalse(registry.exists())
        self.assertFalse(registry.with_suffix(".tmp").exists())

    def test_an_open_item_keeps_the_tree(self) -> None:
        self.finish("done", state="open")
        _, out = self.run_cleanup("--remove")
        self.assertIn("not finished", self.kept(out)["done"])
        self.assertTrue((self.trees / "done").is_dir())

    def test_an_open_owner_added_during_the_final_checks_keeps_the_tree_and_gets_the_log(self) -> None:
        tree = self.trees / "done"
        key = f"{KEY}-new"
        calls = 0

        def mounts() -> list[Path]:
            nonlocal calls
            calls += 1
            if calls == 2:  # the last container check, while cleanup holds the tree lock
                self.store.upsert_item(key, forge_state="open")
                self.store.set_ownership(key, "trees/done/new.txt")
            return []

        with mock.patch.object(cleanup, "containers", side_effect=mounts):
            _, out = self.run_cleanup("--remove")
        self.assertIn(f"{key} is not finished", self.kept(out)["done"])
        self.assertTrue(tree.is_dir())
        self.assertEqual(self.store.log(key)[0].message, self.kept(out)["done"])

    def test_an_owner_reopened_during_the_final_checks_keeps_the_tree(self) -> None:
        calls = 0

        def mounts() -> list[Path]:
            nonlocal calls
            calls += 1
            if calls == 2:
                self.finish("done", state="open")
            return []

        with mock.patch.object(cleanup, "containers", side_effect=mounts):
            _, out = self.run_cleanup("--remove")
        self.assertIn(f"{KEY}-done is not finished", self.kept(out)["done"])
        self.assertTrue((self.trees / "done").is_dir())

    def test_a_tree_no_store_item_owns_is_kept(self) -> None:
        make_repo.git(["worktree", "add", "-b", "stray", str(self.trees / "stray"), "main"], self.clone)
        _, out = self.run_cleanup("--remove")
        self.assertIn("no store item", self.kept(out)["stray"])
        self.assertTrue((self.trees / "stray").is_dir())

    def test_a_container_bound_inside_the_tree_keeps_it(self) -> None:
        mount = self.trees / "done" / "data"
        self.plant("docker", "import sys\nprint('c1') if sys.argv[1] == 'ps' else print(%r)\n" % str(mount))
        _, out = self.run_cleanup("--remove")
        self.assertIn("container", self.kept(out)["done"])
        self.assertTrue((self.trees / "done").is_dir())

    def test_a_container_runtime_that_cannot_answer_keeps_every_tree(self) -> None:
        self.plant("docker", "import sys\nsys.exit('Cannot connect to the Docker daemon')\n")
        _, out = self.run_cleanup("--remove")
        self.assertIn("containers cannot be checked", self.kept(out)["done"])
        self.assertTrue((self.trees / "done").is_dir())

    def test_a_container_mount_that_appears_before_the_delete_keeps_the_tree(self) -> None:
        tree = self.trees / "done"
        real = cleanup.trees_lock

        @contextlib.contextmanager
        def mount_then_lock(trees: Path):
            mounts.return_value = [tree / "data"]
            with real(trees):
                yield

        with mock.patch.object(cleanup, "containers", return_value=[]) as mounts, \
                mock.patch.object(cleanup, "trees_lock", mount_then_lock):
            _, out = self.run_cleanup("--remove")
        self.assertIn("a container binds a path in it", self.kept(out)["done"])
        self.assertTrue(tree.is_dir())

    def test_a_failing_final_container_query_keeps_the_tree(self) -> None:
        tree = self.trees / "done"
        real = cleanup.trees_lock

        @contextlib.contextmanager
        def runtime_fails_then_lock(trees: Path):
            mounts.return_value = None
            with real(trees):
                yield

        with mock.patch.object(cleanup, "containers", return_value=[]) as mounts, \
                mock.patch.object(cleanup, "trees_lock", runtime_fails_then_lock):
            _, out = self.run_cleanup("--remove")
        self.assertIn("containers cannot be checked", self.kept(out)["done"])
        self.assertTrue(tree.is_dir())

    def test_a_tree_git_cannot_read_is_kept(self) -> None:
        (self.trees / "done" / ".git").write_text("gitdir: /nowhere\n")
        _, out = self.run_cleanup("--remove")
        self.assertIn("git cannot read", self.kept(out)["done"])
        self.assertTrue((self.trees / "done").is_dir())

    def test_a_locked_tree_is_kept(self) -> None:
        make_repo.git(["worktree", "lock", str(self.trees / "done")], self.clone)
        _, out = self.run_cleanup("--remove")
        self.assertIn("locked", self.kept(out)["done"])
        self.assertTrue((self.trees / "done").is_dir())

    def test_a_locked_marker_added_after_the_first_pass_keeps_the_tree(self) -> None:
        tree = self.trees / "done"
        real = cleanup.trees_lock

        @contextlib.contextmanager
        def lock_then_check(trees: Path):
            make_repo.git(["worktree", "lock", str(tree)], self.clone)
            with real(trees):
                yield

        with mock.patch.object(cleanup, "trees_lock", lock_then_check):
            _, out = self.run_cleanup("--remove")
        self.assertIn("locked", self.kept(out)["done"])
        self.assertTrue(tree.is_dir())

    def test_a_layout_changed_after_the_first_pass_is_revalidated(self) -> None:
        tree = self.trees / "done"
        real = cleanup.trees_lock

        @contextlib.contextmanager
        def replace_layout_then_lock(trees: Path):
            (tree / ".git").unlink()
            cleanup.shutil.copytree(self.clone / ".git", tree / ".git",
                                    ignore=cleanup.shutil.ignore_patterns("worktrees"))
            with real(trees):
                yield

        with mock.patch.object(cleanup, "trees_lock", replace_layout_then_lock):
            _, out = self.run_cleanup("--remove")
        self.assertIn("not a linked worktree", self.kept(out)["done"])
        self.assertTrue(tree.is_dir())

    def test_the_main_clone_is_never_removed(self) -> None:
        make_repo.git(["clone", "-q", str(self.root / "origin.git"), str(self.trees / "mirror")], self.root)
        self.finish("mirror")
        _, out = self.run_cleanup("--remove")
        self.assertIn("not a linked worktree", self.kept(out)["mirror"])
        self.assertTrue((self.trees / "mirror").is_dir())

    def test_a_missing_store_removes_nothing(self) -> None:
        self.db.unlink()
        code, out = self.run_cleanup("--remove")
        self.assertNotEqual(code, 0)
        self.assertFalse(self.db.exists())
        self.assertTrue((self.trees / "done").is_dir())

    def register(self, pid: int, tree: Path) -> None:
        sessions = self.root / ".chief-of-stuff" / "sessions"
        sessions.mkdir(parents=True, exist_ok=True)
        (sessions / f"{pid}.json").write_text(json.dumps(
            {"pid": pid, "name": "worker-1", "runtime": "claude", "worktree": str(tree)}))


class UnsavedTest(unittest.TestCase):
    def test_only_an_ignored_build_output_directory_is_not_unsaved(self) -> None:
        status = "!! node_modules/\n!! web/node_modules/\n!! scripts/__pycache__/\n!! plan.db\n?? notes.md\nM a.py\n" \
                 "!! node_modules\n!! \"odd\\tname/node_modules/\"\n"
        self.assertEqual(unsaved(status), ("plan.db", "notes.md", "a.py", "node_modules", '"odd\\tname/node_modules/"'))


if __name__ == "__main__":
    unittest.main()
