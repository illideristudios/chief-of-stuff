"""Unit tests for scripts/make_worktree.py: the first script that writes to the user's repo.

Every git call the plugin made before this one was read-only, and that is the stated reason git
stays off the agent's Bash allowlist. So the refusals matter more than the happy path: the branch
and the path both come out of a file an agent wrote, and they are handed to git.

Repos are real. `git worktree add` against a fake proves nothing.
"""

import contextlib
import fcntl
import io
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from uuid import uuid4
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import git_trees  # noqa: E402
import git_view  # noqa: E402
import make_repo  # noqa: E402
import make_worktree as mw  # noqa: E402
from evals import git_taint as taint  # noqa: E402

CLAUDE = """# Workspace

## Coordinator

- User: Robin
- Worktrees: `trees/`
- Agent: implementer task
- Agent: fixer standing
- Timezone: America/Chicago
"""

_views = contextlib.ExitStack()


def setUpModule() -> None:
    """Reads go through git_view.run (#443): give them a private base, never the user's cache."""
    _views.enter_context(taint.private_git_views(Path(_views.enter_context(tempfile.TemporaryDirectory()))))


def tearDownModule() -> None:
    _views.close()


def workspace() -> tuple[tempfile.TemporaryDirectory, Path]:
    tmp = tempfile.TemporaryDirectory()
    root = Path(tmp.name)
    (root / "CLAUDE.md").write_text(CLAUDE)
    make_repo.build(root, {"trees": "trees", "worktrees": []})
    return tmp, root


def rev(cwd: Path, ref: str = "HEAD") -> str:
    return make_repo.git(["rev-parse", ref], cwd)


def advance_remote(root: Path) -> str:
    """Push a commit to the bare remote from a second clone, so the first clone's `main` and its
    `refs/remotes/origin/main` are both behind. Returns the remote's new head."""
    other = root / "other"
    make_repo.git(["clone", "-q", str(root / "origin.git"), str(other)], root)
    (other / "ahead.txt").write_text("ahead\n")
    make_repo.git(["add", "-A"], other)
    make_repo.git(["commit", "-q", "-m", "ahead"], other)
    make_repo.git(["push", "-q", "origin", "HEAD:main"], other)
    return rev(other)


class AgentTypeTest(unittest.TestCase):
    def test_reads_the_types_and_their_lifetimes(self):
        self.assertEqual(mw.agent_types(CLAUDE), {"implementer": "task", "fixer": "standing"})

    def test_no_agent_lines_is_no_types_not_an_error(self):
        self.assertEqual(mw.agent_types("## Coordinator\n\n- User: Robin\n"), {})

    def test_a_lifetime_that_is_not_task_or_standing_is_refused(self):
        with self.assertRaises(mw.RefusedError):
            mw.agent_types("## Coordinator\n\n- Agent: ghost immortal\n")

    def test_a_line_outside_the_block_is_not_a_type(self):
        text = CLAUDE + "\n## Notes\n\n- Agent: smuggled standing\n"
        self.assertNotIn("smuggled", mw.agent_types(text))


class BranchNameTest(unittest.TestCase):
    """git's own answer, not a regex of mine."""

    def test_a_plain_branch_is_accepted(self):
        mw.check_branch("fix/docstring")

    def test_a_branch_that_is_an_option_is_refused(self):
        for name in ("-rf", "--upload-pack=touch /tmp/x"):
            with self.assertRaises(mw.RefusedError, msg=name):
                mw.check_branch(name)

    def test_the_at_branch_name_is_refused(self):
        """#546: `@` is git's shorthand for HEAD and `@{...}` the previous-checkout form; neither names a branch."""
        for name in ("@", "@{0}", "@{-1}"):
            with self.assertRaises(mw.RefusedError, msg=name):
                mw.check_branch(name)

    def test_gits_own_reserved_shapes_are_refused(self):
        for name in ("a..b", "a@{0}", "a.lock", "a b", "a~1", ""):
            with self.assertRaises(mw.RefusedError, msg=name):
                mw.check_branch(name)


class PathTest(unittest.TestCase):
    def setUp(self):
        self.tmp, self.root = workspace()
        self.addCleanup(self.tmp.cleanup)

    def test_a_name_resolves_directly_under_the_worktrees_dir(self):
        path = mw.resolve(self.root, "trees/", "wt-new")
        self.assertEqual(path, (self.root / "trees" / "wt-new").resolve())

    def test_a_name_that_escapes_the_root_is_refused(self):
        for name in ("../outside", "../../etc", "/tmp/elsewhere"):
            with self.assertRaises(mw.RefusedError, msg=name):
                mw.resolve(self.root, "trees/", name)

    def test_a_nested_name_is_refused_because_a_tree_sits_directly_under_the_dir(self):
        with self.assertRaises(mw.RefusedError):
            mw.resolve(self.root, "trees/", "a/b")

    def test_an_existing_path_is_refused_rather_than_reused(self):
        (self.root / "trees" / "taken").mkdir(parents=True)
        with self.assertRaises(mw.RefusedError) as e:
            mw.resolve(self.root, "trees/", "taken")
        self.assertIn("already", str(e.exception))

    def test_a_symlinked_worktrees_dir_that_points_outside_is_refused(self):
        outside = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(outside, ignore_errors=True))
        link = self.root / "linked"
        link.symlink_to(outside)
        with self.assertRaises(mw.RefusedError):
            mw.resolve(self.root, "linked/", "wt-new")


class CloneCase(unittest.TestCase):
    """A workspace whose clone has a real local bare `origin` (make_repo.build makes one and pushes main)."""

    def setUp(self):
        self.tmp, self.root = workspace()
        self.addCleanup(self.tmp.cleanup)
        self.clone = self.root / "repo"
        self.tree = self.root / "trees" / "wt-new"

    def build(self, **kw):
        args = {"root": self.root, "clone": self.clone, "trees": "trees", "name": "wt-new", "branch": "feat/new", "agent_type": "implementer"}
        args.update(kw)
        return mw.build(**args)

    def run_main(self, *extra: str) -> tuple[int, str, str]:
        return self.invoke("--clone", str(self.clone), *extra)

    def invoke(self, *tail: str) -> tuple[int, str, str]:
        argv = ["--type", "implementer", "--name", "wt-new", "--branch", "feat/new", "--root", str(self.root), *tail]
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = mw.main(argv)
        return code, out.getvalue(), err.getvalue()

    def branches(self) -> str:
        return make_repo.git(["branch", "--list", "feat/new"], self.clone)


class AuditEnvTest(CloneCase):
    """make_worktree's git call reads no home, global or system config and ignores replace refs: the one shared env."""

    def setUp(self):
        super().setUp()
        self.tree = self.build().path

    def test_the_call_passes_the_shared_audit_env(self):
        done = subprocess.CompletedProcess([], 0, stdout="", stderr="")
        with mock.patch.object(git_view, "run", return_value=done) as run:
            mw.git(["status"], self.tree)
        self.assertEqual(run.call_args.args, (["status"], self.tree))
        env = run.call_args.kwargs["env"]
        self.assertEqual(env, git_trees.audit_env())
        self.assertNotEqual(env["HOME"], str(self.tree))

    def test_a_global_config_the_worker_wrote_does_not_run(self):
        marker = self.root / "marker"
        hook = f"{sys.executable} -c \\\"import pathlib; pathlib.Path('{marker}').touch()\\\""
        (self.tree / ".gitconfig").write_text(f'[core]\n\tfsmonitor = "{hook}"\n')
        control = {"PATH": make_repo.ENV["PATH"], "HOME": str(self.tree)}  # what the call used to run with
        subprocess.run(["git", "-C", str(self.tree), "status"], env=control, capture_output=True, check=False)
        self.assertTrue(marker.exists(), "the fixture must fire under the old environment, or the test proves nothing")
        marker.unlink()
        mw.git(["status"], self.tree)
        self.assertFalse(marker.exists())

    def test_a_replace_ref_does_not_hide_a_change(self):
        (self.tree / "README.md").write_text("changed\n")
        make_repo.git(["add", "README.md"], self.tree)
        replacement = make_repo.git(["commit-tree", make_repo.git(["write-tree"], self.tree), "-m", "replacement"], self.tree)
        make_repo.git(["replace", "-f", rev(self.tree), replacement], self.tree)
        # The fixture hides the change from a plain reader and shows it to one that ignores replace refs.
        self.assertEqual(make_repo.git(["status", "--short"], self.tree), "")
        self.assertIn("README.md", mw.git(["status", "--short"], self.tree)[1])


class GitHelperErrorPathTest(unittest.TestCase):
    """Reads go through git_trees.git (#449): helper failures are code 1 so git's 128 stays 'no such ref', never a raise."""

    def failing(self, exc: BaseException) -> tuple[int, str]:
        with mock.patch.object(git_view, "run", side_effect=exc):
            return mw.git(["status"], Path("."))

    def test_an_oserror_is_code_1_naming_the_exception_class(self):
        code, detail = self.failing(FileNotFoundError("no git"))
        self.assertEqual(code, 1)
        self.assertIn("FileNotFoundError", detail)

    def test_a_timeout_is_code_1_naming_the_exception_class(self):
        code, detail = self.failing(subprocess.TimeoutExpired("git", 1))
        self.assertEqual(code, 1)
        self.assertIn("TimeoutExpired", detail)

    def test_an_unviewable_tree_is_code_1_with_git_trees_unreadable_shape(self):
        """Changed with #449: the duplicate helper said `Unviewable:`; git_trees says `unreadable:`."""
        self.assertEqual(self.failing(git_view.Unviewable("metadata is not a bounded regular file")),
                         (1, "unreadable: metadata is not a bounded regular file"))

    def test_a_cache_error_is_code_1_naming_the_configuration_problem(self):
        reason = "cannot prepare view cache base '/unusable-cache'"
        self.assertEqual(self.failing(git_view.CacheError(reason)), (1, f"CacheError: {reason}"))

    def test_every_failure_is_code_1_matching_git_trees(self):
        for exc, detail in (
            (OSError("no git"), "OSError: no git"),
            (PermissionError("cache is read-only"), "PermissionError: cache is read-only"),
            (subprocess.TimeoutExpired("git", 1), f"TimeoutExpired: {subprocess.TimeoutExpired('git', 1)}"),
            (git_view.Unviewable("why"), "unreadable: why"),
            (RuntimeError("no home"), "unreadable: no home"),
            (ValueError("embedded null"), "unreadable: embedded null"),
            (subprocess.SubprocessError("odd"), "SubprocessError: odd"),
        ):
            with self.subTest(type(exc).__name__):
                self.assertEqual(self.failing(exc), (1, detail))

    def test_the_call_is_bounded_by_git_trees_timeout(self):
        """Changed with #449: reads use git_trees.git's bound, not a second constant."""
        done = subprocess.CompletedProcess([], 0, stdout="", stderr="")
        with mock.patch.object(git_view, "run", return_value=done) as run:
            mw.git(["status"], Path("."))
        self.assertEqual(run.call_args.kwargs["timeout"], git_trees.GIT_TIMEOUT)

    def test_stderr_is_the_detail_when_stdout_is_empty(self):
        done = subprocess.CompletedProcess([], 3, stdout="", stderr="boom\n")
        with mock.patch.object(git_view, "run", return_value=done):
            self.assertEqual(mw.git(["status"], Path(".")), (3, "boom"))

    def test_stdout_wins_over_stderr_when_both_are_set(self):
        done = subprocess.CompletedProcess([], 0, stdout="out\n", stderr="boom\n")
        with mock.patch.object(git_view, "run", return_value=done):
            self.assertEqual(mw.git(["status"], Path(".")), (0, "out"))

    def test_gits_128_stays_128_even_when_its_text_looks_like_a_helper_failure(self):
        for detail in ("unreadable: why", "PermissionError: denied", "OSError: disk full"):
            with self.subTest(detail=detail):
                done = subprocess.CompletedProcess([], 128, stdout="", stderr=detail)
                with mock.patch.object(git_view, "run", return_value=done):
                    self.assertEqual(mw.git(["status"], Path(".")), (128, detail))

    def test_a_branch_is_refused_when_git_cannot_run(self):
        """`check_branch` asks git directly (no repository, no view): a git that cannot run refuses."""
        with mock.patch.object(mw, "_run", return_value=(1, "x")):
            with self.assertRaises(mw.RefusedError):
                mw.check_branch("fix/plain")

    def test_non_utf8_git_output_does_not_raise(self):
        """#449: the write-side helper decodes like git_trees.fetch (backslashreplace), never raises."""
        done = subprocess.CompletedProcess([], 0, stdout="ok\\xff", stderr="")
        with mock.patch.object(mw.subprocess, "run", return_value=done) as run:
            code, detail = mw._run(["status"], Path("."), git_trees.audit_env())
        self.assertEqual((code, detail), (0, "ok\\xff"))
        self.assertEqual(run.call_args.kwargs.get("errors"), "backslashreplace")

class GitTrustedTest(unittest.TestCase):
    """The write path is unchanged by #443: the clone's own git, in the user's environment, not the view."""

    def test_it_runs_git_itself_with_the_write_pins_and_never_through_the_view(self):
        done = subprocess.CompletedProcess([], 0, stdout="ok\n", stderr="")
        with mock.patch.object(mw.subprocess, "run", return_value=done) as run, \
                mock.patch.object(git_view, "run", side_effect=AssertionError("a write went through the view")):
            self.assertEqual(mw.git_trusted(["worktree", "list"], Path("/clone")), (0, "ok"))
        self.assertEqual(run.call_args.args[0], ["git", "-C", "/clone", *git_trees.GIT_WRITE_PINS, "worktree", "list"])
        self.assertEqual(run.call_args.kwargs["env"], git_trees.write_env())
        self.assertEqual(run.call_args.kwargs["timeout"], mw.GIT_TIMEOUT)


class WorktreeAddEnvTest(CloneCase):
    """`worktree add` runs in a clone whose config and hooks a worker can write (#556): the call carries the write
    pins on its argv and runs on the write env — the audit's isolation plus only the caller's credential variables —
    so no hook or helper the clone holds can run. Every read stays on the audit env."""

    def record(self):
        calls = []
        real = subprocess.run

        def spy(argv, **kw):
            calls.append((argv, kw.get("env")))
            return real(argv, **kw)

        return calls, spy

    def plant_hook(self, body: str) -> Path:
        """A post-checkout hook in the clone's own hooks directory: exactly what a worker with the git-dir grant can
        write. The clone is a plain one, so `.git` is a directory and this is its default hooks path."""
        hook = self.clone / ".git" / "hooks" / "post-checkout"
        hook.parent.mkdir(exist_ok=True)
        hook.write_text(f"#!{sys.executable}\n" + body)
        hook.chmod(0o755)
        return hook

    def test_a_worker_written_post_checkout_hook_does_not_run(self):
        """#556, the verified repro: a worker writes `.git/hooks/post-checkout` into the shared git directory its grant
        makes writable; the launcher's next `worktree add` must not execute it."""
        marker = self.root / "hook-ran"
        self.plant_hook(f"from pathlib import Path\nPath({str(marker)!r}).write_text('fired')\n")
        made = self.build()
        self.assertTrue(made.path.is_dir())
        self.assertFalse(marker.exists(), "the launcher's worktree add ran a hook the worker could have written")

    def test_the_add_runs_even_when_a_hook_would_need_a_writable_home(self):
        """The old contract ran the clone's own hook with the user's HOME; #556 withdraws that: the tree is made with
        the hook dark, so nothing in it needs a writable home to begin with."""
        home = self.root / "user-home"
        home.mkdir()
        self.plant_hook("import os, sys\n"
                        "try:\n"
                        "    os.makedirs(os.path.join(os.environ['HOME'], '.cache-marker'))\n"
                        "except OSError:\n"
                        "    sys.exit(1)\n")
        with mock.patch.dict(os.environ, {"HOME": str(home)}):
            made = self.build()
        self.assertTrue(made.path.is_dir())
        self.assertFalse((home / ".cache-marker").exists(), "the hook ran")

    def test_the_add_call_runs_on_the_write_env_and_every_read_stays_on_the_audit_env(self):
        home = self.root / "user-home"
        home.mkdir()
        repo_vars = {"GIT_DIR": str(self.root / "nowhere"), "GIT_WORK_TREE": str(self.root / "nowhere"), "GIT_INDEX_FILE": str(self.root / "nowhere.idx")}
        calls, spy = self.record()
        views = []
        real_view = git_view.run

        def view_spy(args, path, **kw):
            views.append((args, kw.get("env")))
            return real_view(args, path, **kw)

        with mock.patch.dict(os.environ, {"HOME": str(home), "SSH_AUTH_SOCK": "/agent.sock", **repo_vars}), \
                mock.patch.object(mw.subprocess, "run", spy), mock.patch.object(git_view, "run", view_spy):
            self.build()
            expected = git_trees.write_env()  # inside the patched environment: the credentials ride along
        head = ["git", "-C", str(self.clone), *git_trees.GIT_WRITE_PINS]
        adds = [(argv, env) for argv, env in calls if argv[:len(head)] == head]
        self.assertEqual(len(adds), 1)
        argv, env = adds[0]
        self.assertEqual(argv[len(head):len(head) + 2], ["worktree", "add"])
        self.assertEqual(env, expected)
        self.assertEqual(env["HOME"], git_trees.SAFE_HOME, "no home of the caller's is read")
        self.assertEqual(env["SSH_AUTH_SOCK"], "/agent.sock", "the caller's credentials ride along")
        for var in repo_vars:
            self.assertNotIn(var, env)
        checks = [env for argv, env in calls if argv[3:4] == ["check-ref-format"]]
        self.assertEqual(checks, [git_trees.audit_env()], "the branch check is one git call on the audit env")
        clone_reads = [(argv[3:], env) for argv, env in calls if argv[:3] == ["git", "-C", str(self.clone)]
                       and argv[3:4] in (["rev-parse"], ["show-ref"])]
        self.assertEqual(clone_reads, [
            (["rev-parse", "--show-toplevel"], git_trees.audit_env()),
            (["show-ref", "--verify", "--quiet", "refs/heads/feat/new"], git_trees.audit_env()),
        ], "the root and branch-existence checks also stay on the audit env")
        self.assertEqual(sorted(args[0] for args, _ in views), ["log", "rev-parse"], "the two reads in `build` go through the view")
        for args, read_env in views:
            self.assertEqual(read_env, git_trees.audit_env(), args[0])


class ReadsThroughTheViewTest(unittest.TestCase):
    """#443 slice 5: make_worktree's reads run on a sanitised view of the repository, never on the repository itself.

    A worker writes the tree's own `.git` (config, gitfile), so none of it is a program to run. Each case has a control: the
    former read (`git -C <tree> status` on the audit env) fires the canary on the same fixture."""

    # vector -> the marker the former read fires
    CONFIG = {"fsmonitor": "fsmonitor", "filter": "filter-clean", "include": "include-fsmonitor", "wtconfig": "wtconfig-fsmonitor"}
    POINTER = {"gitfile": "gitfile-fsmonitor", "commondir": "decoy-fsmonitor"}  # a forged pointer: not viewable at all
    # characterization only: `hooks_in_common` (a post-index-change hook) is quiet under the audit env's GIT_OPTIONAL_LOCKS=0

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)

    def fixture(self, vector: str):
        fx = taint.build(Path(tempfile.mkdtemp(prefix="fx-", dir=self.root)), vector)
        fx.clear()
        subprocess.run(["git", "-C", str(fx.tree), "status", "--short"], env=git_trees.audit_env(), capture_output=True, timeout=15, check=False)
        self.assertIn(self.CONFIG.get(vector) or self.POINTER[vector], fx.fired(), "the fixture must fire under the former read, or the test proves nothing")
        fx.clear()
        return fx

    def test_make_worktree_git_runs_in_a_view(self):
        truth = taint.git(["status", "--short"], taint.build(self.root / "clean").tree).stdout.strip()
        self.assertIn("README.md", truth)
        for vector in self.CONFIG:
            with self.subTest(vector):
                fx = self.fixture(vector)
                self.assertEqual(mw.git(["status", "--short"], fx.tree), (0, truth))
                self.assertEqual(fx.fired(), [], f"{vector}: a program the worker planted ran")
        for vector in self.POINTER:
            with self.subTest(vector):
                fx = self.fixture(vector)
                code, detail = mw.git(["status", "--short"], fx.tree)
                self.assertEqual(fx.fired(), [], f"{vector}: a program the worker planted ran")
                self.assertEqual(code, 1)
                self.assertTrue(detail.startswith("unreadable: "), detail)  # #449: git_trees shape


class CloneLockTest(CloneCase):
    """#443 slice 5: the lock finds the common directory from files (`git_view.locate`), starts no git, and lands on the real one."""

    def held(self, common: Path) -> bool:
        """Is `common` locked by someone else right now?"""
        fd = os.open(common, os.O_RDONLY)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        finally:
            os.close(fd)
        return False

    def test_clone_lock_locates_without_git(self):
        linked = self.build().path
        common = (self.clone / ".git").resolve()
        boom = AssertionError("clone_lock started a git process")
        with mock.patch.object(mw.subprocess, "run", side_effect=boom), mock.patch.object(mw.subprocess, "Popen", side_effect=boom):
            for where in (self.clone, linked):  # a repository's own directory, and a linked tree of it
                with self.subTest(where.name):
                    self.assertFalse(self.held(common))
                    with mw.clone_lock(where):
                        self.assertTrue(self.held(common), "the lock is on the real common directory")
                    self.assertFalse(self.held(common), "the lock is released")

    def test_a_directory_that_is_no_repository_is_refused_without_git(self):
        plain = self.root / "plain"
        plain.mkdir()
        boom = AssertionError("clone_lock started a git process")
        with mock.patch.object(mw.subprocess, "run", side_effect=boom), mock.patch.object(mw.subprocess, "Popen", side_effect=boom):
            with self.assertRaises(mw.RefusedError) as e, mw.clone_lock(plain):
                self.fail("a plain directory was locked")
        self.assertIn("is not a git repository", str(e.exception))
        self.assertIn(str(plain), str(e.exception))

    def test_a_forged_pointer_is_refused_not_locked(self):
        for vector in ("gitfile", "commondir"):
            with self.subTest(vector):
                fx = taint.build(Path(tempfile.mkdtemp(prefix="fx-", dir=self.root)), vector)
                with self.assertRaises(mw.RefusedError) as e, mw.clone_lock(fx.tree):
                    self.fail("a worktree whose pointers were forged was locked")
                self.assertIn("is not a git repository", str(e.exception))
        fx = taint.build(Path(tempfile.mkdtemp(prefix="fx-", dir=self.root)))
        taint.forged_linked_layout(fx)  # mutually consistent pointers into a copy the worker wrote
        with self.assertRaises(mw.RefusedError) as e, mw.clone_lock(fx.tree):
            self.fail("a worktree whose repository the worker copied was locked")
        self.assertIn("is not a git repository", str(e.exception))


class FilesOnlyLayoutTest(CloneCase):
    """Characterization (#443 slice 5): the files-only layout refuses on purpose what the old `rev-parse` locked, like the codex grant."""

    def assert_refused(self, clone: Path):
        with self.assertRaises(mw.RefusedError) as e, mw.clone_lock(clone):
            self.fail("a layout the files-only locate refuses was locked")
        self.assertIn("is not a git repository", str(e.exception))
        with self.assertRaises(mw.RefusedError) as e:
            self.build(clone=clone)
        self.assertIn("is not a git repository", str(e.exception))
        self.assertFalse(self.tree.exists())

    def test_a_separate_git_dir_clone_is_refused(self):
        separate = self.root / "separate"
        make_repo.git(["clone", "-q", "--separate-git-dir", str(self.root / "separate.git"), str(self.root / "origin.git"), str(separate)], self.root)
        self.assertTrue((separate / ".git").is_file(), "the fixture is a gitfile clone whose git directory lives elsewhere")
        self.assertEqual(make_repo.git(["rev-parse", "--git-dir"], separate), str((self.root / "separate.git").resolve()))  # git itself accepts it
        self.assert_refused(separate)

    def test_a_subdirectory_of_a_repository_is_refused(self):
        sub = self.clone / "sub"
        sub.mkdir()
        self.assertEqual(make_repo.git(["rev-parse", "--show-toplevel"], sub), str(self.clone.resolve()))  # git itself accepts it
        with self.assertRaises(mw.RefusedError) as e, mw.clone_lock(sub):
            self.fail("a layout the files-only locate refuses was locked")
        self.assertIn("is not a git repository", str(e.exception))
        with self.assertRaises(mw.RefusedError) as e:
            self.build(clone=sub)
        self.assertIn("not a repository root", str(e.exception), "#311: refuse by toplevel, not only by locate")
        self.assertIn(str(sub), str(e.exception))
        self.assertFalse(self.tree.exists())


class CheckBranchNeedsNoRepositoryTest(CloneCase):
    """#443 slice 5: `check-ref-format` reads no repository, so `check_branch(branch)` takes none and runs from `/`."""

    def spy(self):
        calls = []
        real = subprocess.run

        def run(argv, **kw):
            calls.append((argv, kw))
            return real(argv, **kw)

        return calls, mock.patch.object(mw.subprocess, "run", run)

    def test_check_branch_needs_no_repository(self):
        calls, patched = self.spy()
        with patched:
            mw.check_branch("feat/x")
        self.assertEqual(len(calls), 1)
        argv, kw = calls[0]
        self.assertEqual(argv, ["git", "-C", "/", "check-ref-format", "--branch", "feat/x"])
        self.assertEqual(kw["env"], git_trees.audit_env())

    def test_it_takes_the_branch_and_nothing_else(self):
        with self.assertRaises(TypeError):
            mw.check_branch("feat/x", self.clone)

    def test_building_with_a_hostile_clone_still_makes_the_branch_check_from_root(self):
        hook = f"{sys.executable} -c \\\"import pathlib; pathlib.Path('{self.root / 'marker'}').touch()\\\""
        make_repo.git(["config", "core.fsmonitor", hook], self.clone)
        calls, patched = self.spy()
        with patched:
            self.build()
        checks = [argv for argv, _ in calls if "check-ref-format" in argv]
        self.assertEqual(checks, [["git", "-C", "/", "check-ref-format", "--branch", "feat/new"]])

    def test_it_still_refuses_what_git_refuses_and_what_would_be_an_option(self):
        calls, patched = self.spy()
        with patched:
            for name in ("-x", "--upload-pack=x", ""):
                with self.subTest(name), self.assertRaises(mw.RefusedError):
                    mw.check_branch(name)
            self.assertEqual(calls, [], "an option-like or empty name is refused before git")
            for name in ("a..b", "feat/x.lock", "a b", "a@{0}"):
                with self.subTest(name), self.assertRaises(mw.RefusedError):
                    mw.check_branch(name)
        self.assertEqual({argv[:3] == ["git", "-C", "/"] for argv, _ in calls}, {True})


class BuildReadsThroughTheViewTest(CloneCase):
    """#443 slice 5: the two reads in `build` (local main's line, the new tree's sha) go through the view."""

    def test_build_reads_main_and_head_through_the_view(self):
        # Neither read fires a planted program even raw (`log` and `rev-parse` run no fsmonitor or filter), and the write path
        # may run the clone's own config: so the proof is the call, not a marker.
        views = []
        real = git_view.run

        def view_spy(args, path, **kw):
            views.append((args, Path(path), kw.get("env"), kw.get("timeout"), kw.get("expected_common")))
            return real(args, path, **kw)

        with mock.patch.object(git_view, "run", view_spy):
            made = self.build()
        self.assertEqual([(args, path) for args, path, *_ in views], [
            (["log", "-1", "--format=%h %cr", "refs/heads/main"], self.clone),
            (["rev-parse", "--short", "HEAD"], made.path),
        ])
        for _, _, env, timeout, common in views:
            self.assertEqual((env, timeout), (git_trees.audit_env(), git_trees.GIT_TIMEOUT))
            self.assertEqual(common, (self.clone / ".git").resolve())
        self.assertEqual(made.sha, make_repo.git(["rev-parse", "--short", "HEAD"], made.path))
        self.assertEqual(made.sha, make_repo.git(["rev-parse", "--short", "refs/remotes/origin/main"], self.clone))

    def test_a_tree_the_view_cannot_read_has_sha_question_mark(self):
        """`build` never raises over the sha: an unviewable new tree reports `?`, not git's error text."""
        real = git_view.run

        def run(args, path, **kw):
            if args[0] == "rev-parse":  # only the new tree's read fails; the clone's main read must work (a failing one refuses)
                raise git_view.Unviewable("not a linked worktree of its own repository")
            return real(args, path, **kw)

        with mock.patch.object(git_view, "run", run):
            made = self.build()
        self.assertEqual(made.sha, "?")
        self.assertTrue(made.path.is_dir())


class BuildTest(CloneCase):
    def test_creates_the_tree_on_a_new_branch_off_main(self):
        made = self.build()
        self.assertTrue((made.path / ".git").exists())
        head = subprocess.run(["git", "-C", str(made.path), "rev-parse", "--abbrev-ref", "HEAD"], capture_output=True, text=True)
        self.assertEqual(head.stdout.strip(), "feat/new")

    def test_the_new_branch_starts_at_the_remotes_main(self):
        remote_head = advance_remote(self.root)
        made = self.build()
        self.assertEqual(rev(made.path), remote_head, "a dispatch starts from the remote's main, not from a stale local one")

    def test_an_unlisted_agent_type_is_refused_before_anything_is_created(self):
        with self.assertRaises(mw.RefusedError):
            self.build(agent_type="ghost")
        self.assertFalse(self.tree.exists())

    def test_no_agent_lines_means_any_type_is_allowed(self):
        (self.root / "CLAUDE.md").write_text("## Coordinator\n\n- Worktrees: `trees/`\n")
        made = self.build(agent_type="whatever")
        self.assertTrue(made.path.exists())

    def test_a_refused_branch_creates_nothing(self):
        with self.assertRaises(mw.RefusedError):
            self.build(branch="-rf")
        self.assertFalse(self.tree.exists())

    def test_it_prints_the_path_and_branch_it_actually_made(self):
        made = self.build()
        line = mw.line(made)
        self.assertIn(str(made.path), line)
        self.assertIn("feat/new", line)

    def assert_refused_before_any_fetch(self, about: str, **kw):
        with mock.patch.object(mw.git_trees, "fetch_base") as fetch:
            with self.assertRaises(mw.RefusedError) as e:
                self.build(**kw)
        fetch.assert_not_called()
        self.assertIn(about, str(e.exception), f"the refusal is about {about!r}, not a fetch")
        self.assertNotIn("fetch", str(e.exception).lower())

    def test_a_refused_type_is_refused_before_any_fetch(self):
        self.assert_refused_before_any_fetch("ghost", agent_type="ghost")

    def test_a_refused_branch_is_refused_before_any_fetch(self):
        self.assert_refused_before_any_fetch("a..b", branch="a..b")

    def test_a_bad_name_is_refused_before_any_fetch(self):
        self.assert_refused_before_any_fetch("../outside", name="../outside")

    def test_an_existing_name_is_refused_before_any_fetch(self):
        (self.root / "trees" / "taken").mkdir(parents=True)
        self.assert_refused_before_any_fetch("already exists", name="taken")

    def test_a_failed_worktree_add_is_a_refusal_and_leaves_no_tree(self):
        make_repo.git(["branch", "feat/new", "main"], self.clone)
        with self.assertRaises(mw.RefusedError) as e:
            self.build()
        self.assertIn("worktree add", str(e.exception))
        self.assertFalse(self.tree.exists())
        self.assertIn("feat/new", self.branches(), "a branch that existed before the build must stay")

    def test_main_exits_1_when_the_worktree_add_fails(self):
        make_repo.git(["branch", "feat/new", "main"], self.clone)
        code, out, err = self.run_main()
        self.assertEqual((code, out), (1, ""))
        self.assertTrue(err.startswith("refused:"), err)
        self.assertFalse(self.tree.exists())
        self.assertIn("feat/new", self.branches())

    def test_a_tree_created_by_another_repository_during_add_survives(self):
        other = make_repo.build(self.root, {"clone": "competitor", "origin": "competitor.git", "worktrees": []})
        real = mw.git_trusted
        head = rev(other)

        def competing_add(args, cwd):
            if args[:2] == ["worktree", "add"]:
                make_repo.git(["worktree", "add", "-q", "--detach", str(self.tree), "main"], other)
                (self.tree / "keep.txt").write_text("another repository's work\n")
            return real(args, cwd)

        with mock.patch.object(mw, "git_trusted", competing_add):
            with self.assertRaises(mw.RefusedError) as raised:
                self.build()
        self.assertTrue(self.tree.is_dir(), "cleanup must preserve the other repository's worktree")
        self.assertEqual((self.tree / "keep.txt").read_text(), "another repository's work\n")
        self.assertEqual(rev(self.tree), head)
        self.assertIn(str(self.tree), make_repo.git(["worktree", "list", "--porcelain"], other))
        self.assertIn(str(self.tree), str(raised.exception))
        self.assertIn("left", str(raised.exception))
        self.assertEqual(self.branches(), "")

    def test_a_nonempty_unregistered_directory_during_add_is_left_and_reported(self):
        real = mw.git_trusted

        def competing_directory(args, cwd):
            if args[:2] == ["worktree", "add"]:
                self.tree.mkdir()
                (self.tree / "keep.txt").write_text("unowned\n")
            return real(args, cwd)

        with mock.patch.object(mw, "git_trusted", competing_directory):
            with self.assertRaises(mw.RefusedError) as raised:
                self.build()
        self.assertTrue(self.tree.is_dir())
        self.assertEqual((self.tree / "keep.txt").read_text(), "unowned\n")
        self.assertIn(str(self.tree), str(raised.exception))
        self.assertIn("left", str(raised.exception))

    def test_a_failed_add_that_left_an_empty_directory_removes_it(self):
        real = mw.git_trusted

        def empty_then_fail(args, cwd):
            if args[:2] == ["worktree", "add"]:
                self.tree.mkdir()
                return 1, "planted failure after mkdir"
            return real(args, cwd)

        with mock.patch.object(mw, "git_trusted", empty_then_fail):
            with self.assertRaises(mw.RefusedError):
                self.build()
        self.assertFalse(self.tree.exists())
        self.assertEqual(self.branches(), "")

    def test_a_failed_add_that_left_a_tree_and_branch_removes_both(self):
        """#448/#376: when add succeeds then the call still refuses, scrub only what this build made."""
        real = mw.git_trusted

        def add_then_fail(args, cwd):
            if args[:2] == ["worktree", "add"]:
                code, detail = real(args, cwd)
                self.assertEqual(code, 0, detail)
                return 1, "planted failure after add"
            return real(args, cwd)

        with mock.patch.object(mw, "git_trusted", add_then_fail):
            with self.assertRaises(mw.RefusedError) as e:
                self.build()
        self.assertIn("worktree add", str(e.exception))
        self.assertFalse(self.tree.exists())
        self.assertEqual(self.branches(), "")

    def test_a_failed_add_that_left_only_a_new_branch_removes_the_branch(self):
        """#448: git can create the -b branch before the worktree path fails."""
        real = mw.git_trusted

        def branch_then_fail(args, cwd):
            if args[:2] == ["worktree", "add"]:
                make_repo.git(["branch", "feat/new", "main"], self.clone)
                return 1, "planted failure after -b"
            return real(args, cwd)

        with mock.patch.object(mw, "git_trusted", branch_then_fail):
            with self.assertRaises(mw.RefusedError):
                self.build()
        self.assertFalse(self.tree.exists())
        self.assertEqual(self.branches(), "")

    def test_a_registered_tree_is_removed_even_when_git_remove_fails(self):
        real = mw.git_trusted

        def fail_after_add(args, cwd):
            if args[:2] == ["worktree", "add"]:
                code, detail = real(args, cwd)
                self.assertEqual(code, 0, detail)
                return 1, "planted failure after add"
            if args[:2] == ["worktree", "remove"]:
                return 1, "planted remove failure"
            return real(args, cwd)

        with mock.patch.object(mw, "git_trusted", fail_after_add):
            with self.assertRaises(mw.RefusedError):
                self.build()
        self.assertFalse(self.tree.exists())

    def test_the_report_line_carries_the_short_sha_not_the_full_one(self):
        remote_head = advance_remote(self.root)
        self.assertEqual(len(remote_head), 40)
        line = mw.line(self.build())
        self.assertIn(remote_head[:7], line)
        self.assertNotIn(remote_head, line)

    def test_main_prints_the_short_sha_not_the_full_one(self):
        remote_head = advance_remote(self.root)
        code, out, err = self.run_main()
        self.assertEqual(code, 0, err)
        self.assertIn(remote_head[:7], out)
        self.assertNotIn(remote_head, out)


class BranchProbeFailureTest(CloneCase):
    """An unknown branch state must refuse before add and preserve existing branches."""

    def assert_probe_failure_preserves_branch(self, failure, reason):
        make_repo.git(["branch", "feat/new", "main"], self.clone)
        head = rev(self.clone, "refs/heads/feat/new")
        real = subprocess.run
        probes = []

        def fail_probe(argv, **kw):
            if argv[3:] in (["rev-parse", "--verify", "-q", "refs/heads/feat/new"],
                            ["show-ref", "--verify", "--quiet", "refs/heads/feat/new"]):
                probes.append(argv)
                if isinstance(failure, BaseException):
                    raise failure
                return subprocess.CompletedProcess(argv, failure, stdout="", stderr=reason)
            return real(argv, **kw)

        with mock.patch.object(mw.subprocess, "run", fail_probe), \
                mock.patch.object(mw, "git_trusted", wraps=mw.git_trusted) as trusted:
            with self.assertRaises(mw.RefusedError) as raised:
                self.build(from_local=True)
        self.assertIn("feat/new", self.branches(), "an unknown probe must never authorize branch deletion")
        self.assertEqual(rev(self.clone, "refs/heads/feat/new"), head)
        self.assertFalse(self.tree.exists())
        trusted.assert_not_called()
        self.assertEqual(len(probes), 1)
        self.assertEqual(probes[0][3:], ["show-ref", "--verify", "--quiet", "refs/heads/feat/new"])
        self.assertIn("cannot check", str(raised.exception))
        self.assertIn("feat/new", str(raised.exception))
        self.assertIn(reason, str(raised.exception))

    def test_a_fatal_branch_probe_refuses_without_deleting_the_branch(self):
        self.assert_probe_failure_preserves_branch(128, "fatal: cannot read refs")

    def test_an_other_nonzero_branch_probe_refuses_without_deleting_the_branch(self):
        self.assert_probe_failure_preserves_branch(2, "probe failed")

    def test_a_branch_probe_timeout_refuses_without_deleting_the_branch(self):
        self.assert_probe_failure_preserves_branch(subprocess.TimeoutExpired("git", 30), "TimeoutExpired")

    def test_a_branch_probe_oserror_refuses_without_deleting_the_branch(self):
        self.assert_probe_failure_preserves_branch(OSError("probe unavailable"), "OSError")


class FreshBaseTest(CloneCase):
    """Rule: the branch is cut from the remote's main, fetched first; the clone's local main is left alone."""

    def test_the_tree_head_is_the_remotes_head_not_local_mains(self):
        local_main = rev(self.clone, "main")
        remote_head = advance_remote(self.root)
        self.assertNotEqual(local_main, remote_head)
        made = self.build()
        self.assertEqual(rev(made.path), remote_head)
        self.assertNotEqual(rev(made.path), local_main)

    def test_local_main_in_the_clone_is_not_moved(self):
        local_main = rev(self.clone, "main")
        advance_remote(self.root)
        self.build()
        self.assertEqual(rev(self.clone, "main"), local_main)

    def test_the_new_branch_has_no_upstream_so_a_bare_push_cannot_target_main(self):
        advance_remote(self.root)
        made = self.build()
        upstream = subprocess.run(["git", "-C", str(made.path), "rev-parse", "--abbrev-ref", "feat/new@{u}"], capture_output=True, text=True)
        self.assertNotEqual(upstream.returncode, 0, f"feat/new tracks {upstream.stdout.strip()!r}")
        merge = subprocess.run(["git", "-C", str(self.clone), "config", "--get", "branch.feat/new.merge"], capture_output=True, text=True)
        self.assertEqual(merge.stdout.strip(), "")

    def test_the_report_names_origin_main_and_its_sha_beside_path_branch_and_type(self):
        remote_head = advance_remote(self.root)
        code, out, err = self.run_main()
        self.assertEqual(code, 0, err)
        for part in (str(self.tree.resolve()), "feat/new", "origin/main", remote_head[:7], "implementer"):
            self.assertIn(part, out)

    def test_line_carries_the_base_sha_too(self):
        remote_head = advance_remote(self.root)
        line = mw.line(self.build())
        self.assertIn("origin/main", line)
        self.assertIn(remote_head[:7], line)


class FetchFailureTest(CloneCase):
    """Rule: a failed fetch refuses and creates nothing; the message offers `--from-local` and names local main's sha."""

    def no_origin(self):
        make_repo.git(["remote", "remove", "origin"], self.clone)

    def dead_origin(self):
        make_repo.git(["remote", "set-url", "origin", str(self.root / "nowhere.git")], self.clone)

    def assert_refused_with_nothing_made(self, message: str):
        local_main = rev(self.clone, "main")
        self.assertIn("fetch", message.lower())
        self.assertIn(git_trees.fetch_base(self.clone), message, "the reason the fetch failed")
        self.assertIn(local_main[:7], message, "the sha local main would give")
        self.assertIn("--from-local", message)
        self.assertFalse(self.tree.exists())
        self.assertEqual(self.branches(), "")

    def refuse_build(self):
        with self.assertRaises(mw.RefusedError) as e:
            self.build()
        return str(e.exception)

    def test_no_origin_refuses_and_creates_nothing(self):
        self.no_origin()
        self.assert_refused_with_nothing_made(self.refuse_build())

    def test_an_origin_at_a_path_that_does_not_exist_refuses_and_creates_nothing(self):
        self.dead_origin()
        self.assert_refused_with_nothing_made(self.refuse_build())

    def test_main_exits_1_and_says_so_on_stderr_with_no_origin(self):
        self.no_origin()
        code, out, err = self.run_main()
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assert_refused_with_nothing_made(err)

    def test_main_exits_1_and_says_so_on_stderr_with_a_dead_origin(self):
        self.dead_origin()
        code, out, err = self.run_main()
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assert_refused_with_nothing_made(err)


class FromLocalTest(CloneCase):
    """Rule: `--from-local` cuts from local main, as the flag says, with or without a remote."""

    def test_succeeds_with_no_origin_and_heads_at_local_main(self):
        make_repo.git(["remote", "remove", "origin"], self.clone)
        local_main = rev(self.clone, "main")
        made = self.build(from_local=True)
        self.assertEqual(rev(made.path), local_main)

    def test_the_line_says_local_main_and_its_sha_and_not_origin_main(self):
        make_repo.git(["remote", "remove", "origin"], self.clone)
        local_main = rev(self.clone, "main")
        line = mw.line(self.build(from_local=True))
        self.assertIn(local_main[:7], line)
        self.assertIn("local", line.lower())
        self.assertNotIn("origin/main", line)

    def test_main_accepts_the_flag_and_reports_local_main(self):
        make_repo.git(["remote", "remove", "origin"], self.clone)
        local_main = rev(self.clone, "main")
        code, out, err = self.run_main("--from-local")
        self.assertEqual(code, 0, err)
        self.assertIn(local_main[:7], out)
        self.assertNotIn("origin/main", out)
        self.assertEqual(rev(self.tree), local_main)

    def test_with_an_advanced_reachable_remote_head_is_still_local_main(self):
        local_main = rev(self.clone, "main")
        remote_head = advance_remote(self.root)
        made = self.build(from_local=True)
        self.assertEqual(rev(made.path), local_main)
        self.assertNotEqual(rev(made.path), remote_head)

    def test_it_does_not_fetch(self):
        stale = rev(self.clone, "refs/remotes/origin/main")
        advance_remote(self.root)
        with mock.patch.object(mw.git_trees, "fetch_base") as fetch:
            self.build(from_local=True)
        fetch.assert_not_called()
        self.assertEqual(rev(self.clone, "refs/remotes/origin/main"), stale, "the clone's remote-tracking ref did not move")

    def test_a_dead_origin_does_not_stop_it(self):
        make_repo.git(["remote", "set-url", "origin", str(self.root / "nowhere.git")], self.clone)
        local_main = rev(self.clone, "main")
        self.assertEqual(rev(self.build(from_local=True).path), local_main)


class ConcurrentDispatchTest(CloneCase):
    """Rule: dispatches that start together against one clone do not refuse each other over the fetch's ref lock."""

    BUILDERS = 4

    def test_simultaneous_builds_all_succeed_and_all_head_at_the_remote(self):
        remote_head = advance_remote(self.root)
        start = threading.Barrier(self.BUILDERS)
        results: dict[int, object] = {}

        def dispatch(i: int):
            start.wait()
            try:
                results[i] = self.build(name=f"wt-{i}", branch=f"feat/c{i}")
            except Exception as exc:  # the test reports it, whatever it is
                results[i] = exc

        threads = [threading.Thread(target=dispatch, args=(i,)) for i in range(self.BUILDERS)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        refused = {i: str(r) for i, r in results.items() if not isinstance(r, mw.Made)}
        self.assertEqual(refused, {}, "no dispatch is refused by another")
        for made in results.values():
            self.assertEqual(rev(made.path), remote_head)


# A second dispatch is its own process: it wraps the same two calls the thread test wraps, and says when its
# `git worktree add` returned. The sleep keeps the first dispatch inside its fetch while the second one runs the git calls
# that come before its own fetch (about 0.2s each on a slow machine).
CHILD_DISPATCH = """
import sys, time
from pathlib import Path
scripts, root, clone, marks, repo, base = sys.argv[1:7]
sys.path[:0] = [scripts, repo]
import git_trees
import make_worktree as mw
from evals import git_taint as taint
views = taint.private_git_views(Path(base))
views.__enter__()  # the child reads the clone through the same private view base as the parent
real_fetch, real_add = git_trees.fetch_base, mw.git_trusted
def fetch(*a, **k):
    Path(marks, "started").touch()
    time.sleep(2)
    return real_fetch(*a, **k)
def add(args, cwd):
    out = real_add(args, cwd)
    if args[:2] == ["worktree", "add"]:
        Path(marks, "add_end").write_text(repr(time.time()))
    return out
git_trees.fetch_base, mw.git_trusted = fetch, add
mw.build(Path(root), Path(clone), "trees", "wt-child", "feat/child", "implementer")
"""


class SerializedDispatchTest(CloneCase):
    """Rule: in one clone, the fetch-and-add of one dispatch never overlaps the fetch-and-add of another. The span runs
    from the start of a build's fetch (with --from-local, of its `git worktree add`) to the end of its `git worktree add`."""

    BUILDERS = 4
    HOLD = 0.2  # seconds a build stays at the start of its span, so a build that is not serialized must overlap another

    def spans(self, from_local: bool) -> tuple[dict[str, tuple[float, float]], dict[str, object]]:
        """Run BUILDERS simultaneous builds; (thread -> (span start, span end), thread -> Made or the exception)."""
        events: list[tuple[str, str, float]] = []
        real_add, real_fetch = mw.git_trusted, git_trees.fetch_base

        def note(kind: str):
            events.append((threading.current_thread().name, kind, time.monotonic()))

        def fetch_base(tree, *a, **k):
            note("start")
            time.sleep(self.HOLD)
            return real_fetch(tree, *a, **k)

        def git_trusted(args, cwd):
            adding = args[:2] == ["worktree", "add"]
            if adding and from_local:
                note("start")
                time.sleep(self.HOLD)
            out = real_add(args, cwd)
            if adding:
                note("end")
            return out

        go = threading.Barrier(self.BUILDERS)
        results: dict[str, object] = {}

        def dispatch(i: int):
            go.wait()
            try:
                results[f"b{i}"] = self.build(name=f"wt-{i}", branch=f"feat/s{i}", from_local=from_local)
            except Exception as exc:  # the test reports it, whatever it is
                results[f"b{i}"] = exc

        with mock.patch.object(mw.git_trees, "fetch_base", fetch_base), mock.patch.object(mw, "git_trusted", git_trusted):
            threads = [threading.Thread(target=dispatch, args=(i,), name=f"b{i}") for i in range(self.BUILDERS)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        marks = {(who, kind): at for who, kind, at in events}
        return {w: (marks[w, "start"], marks[w, "end"]) for w in results if (w, "start") in marks and (w, "end") in marks}, results

    def assert_serialized(self, from_local: bool):
        spans, results = self.spans(from_local)
        ordered = sorted(spans.items(), key=lambda kv: kv[1])
        for (a, (_, a_end)), (b, (b_start, _)) in zip(ordered, ordered[1:]):
            self.assertGreaterEqual(b_start, a_end, f"{b} began its fetch-and-add {a_end - b_start:.2f}s before {a} finished its own: {ordered}")
        refused = {w: str(r) for w, r in results.items() if not isinstance(r, mw.Made)}
        self.assertEqual(refused, {}, "no dispatch is refused by another")
        self.assertEqual(len(spans), self.BUILDERS, "every build's span was recorded: the add went through the wrapped call")

    def test_threads_fetch_and_add_one_at_a_time(self):
        self.assert_serialized(from_local=False)

    def test_threads_with_from_local_add_one_at_a_time(self):
        self.assert_serialized(from_local=True)

    def test_a_second_process_waits_for_the_first_to_finish_adding(self):
        marks = self.root / "marks"
        marks.mkdir()
        scripts = Path(mw.__file__).resolve().parent
        child = subprocess.Popen(
            [sys.executable, "-c", CHILD_DISPATCH, str(scripts), str(self.root), str(self.clone), str(marks),
             str(Path(__file__).resolve().parent.parent), str(self.root / "child-views")],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            env={**os.environ, "XDG_CACHE_HOME": str(self.root / "child-cache")},  # never the user's cache; the child's view base is the explicit one above
        )
        self.addCleanup(child.kill)
        deadline = time.monotonic() + 30
        while not (marks / "started").exists():
            if child.poll() is not None:
                self.fail(f"the first dispatch ended before its fetch: {child.communicate()[1]}")
            self.assertLess(time.monotonic(), deadline, "the first dispatch never reached its fetch")
            time.sleep(0.01)
        real_fetch = git_trees.fetch_base
        began: list[float] = []

        def fetch_base(*a, **k):
            began.append(time.time())
            return real_fetch(*a, **k)

        with mock.patch.object(mw.git_trees, "fetch_base", fetch_base):
            made = self.build(name="wt-parent", branch="feat/parent")
        _, err = child.communicate(timeout=60)
        self.assertEqual(child.returncode, 0, f"the first dispatch failed: {err}")
        self.assertIsInstance(made, mw.Made)
        add_end = float((marks / "add_end").read_text())
        self.assertGreaterEqual(began[0], add_end, f"the second dispatch began its fetch {add_end - began[0]:.2f}s before the first finished its add")


class LocalMainIsTheBranchTest(CloneCase):
    """Rule: local main is `refs/heads/main`, never a tag that happens to be called `main`."""

    def tag_behind(self) -> tuple[str, str]:
        """A tag `main` at the commit the branch has now, then one more commit on the branch. (tag sha, branch sha)"""
        tag = rev(self.clone, "refs/heads/main")
        make_repo.git(["tag", "main", tag], self.clone)
        (self.clone / "local.txt").write_text("local\n")
        make_repo.git(["add", "-A"], self.clone)
        make_repo.git(["commit", "-q", "-m", "local"], self.clone)
        branch = rev(self.clone, "refs/heads/main")
        self.assertNotEqual(tag, branch)
        return tag, branch

    def test_from_local_cuts_from_the_branch_not_the_tag(self):
        _, branch = self.tag_behind()
        made = self.build(from_local=True)
        self.assertEqual(rev(made.path), branch)

    def test_a_failed_fetch_names_the_branchs_sha_not_the_tags(self):
        tag, branch = self.tag_behind()
        make_repo.git(["remote", "remove", "origin"], self.clone)
        with self.assertRaises(mw.RefusedError) as e:
            self.build()
        self.assertIn(branch[:7], str(e.exception))
        self.assertNotIn(tag[:7], str(e.exception))


class NoLocalMainTest(CloneCase):
    """Rule: a clone with no local main branch says so in one line, and does not offer what cannot work."""

    def drop_local_main(self):
        make_repo.git(["checkout", "-q", "-b", "elsewhere"], self.clone)
        make_repo.git(["branch", "-D", "main"], self.clone)
        self.assertEqual(make_repo.git(["branch", "--list", "main"], self.clone), "")

    def test_a_failed_fetch_says_there_is_no_local_main_in_one_line_and_offers_no_flag(self):
        self.drop_local_main()
        make_repo.git(["remote", "remove", "origin"], self.clone)
        with self.assertRaises(mw.RefusedError) as e:
            self.build()
        message = str(e.exception)
        self.assertNotIn("\n", message)
        self.assertIn("no local main", message)
        self.assertNotIn("--from-local", message)
        self.assertFalse(self.tree.exists())
        self.assertEqual(self.branches(), "")

    def test_from_local_is_refused_in_one_line_and_creates_nothing(self):
        self.drop_local_main()
        with self.assertRaises(mw.RefusedError) as e:
            self.build(from_local=True)
        self.assertNotIn("\n", str(e.exception))
        self.assertIn("no local main", str(e.exception))
        self.assertFalse(self.tree.exists())
        self.assertEqual(self.branches(), "")

    def test_a_reachable_origin_still_builds_from_the_remotes_head(self):
        self.drop_local_main()
        remote_head = advance_remote(self.root)
        self.assertEqual(rev(self.build().path), remote_head)


class UnviewableMainTest(CloneCase):
    """A main that cannot be read (a view failure, code 1) is not a missing main (git exit 128): it refuses naming the clone and
    the reason, before any fetch or `worktree add`, and makes nothing."""

    def refused(self, side_effect, **kw) -> str:
        with mock.patch.object(git_view, "run", side_effect=side_effect), mock.patch.object(mw.git_trees, "fetch_base") as fetch, \
                mock.patch.object(mw, "git_trusted") as trusted:
            with self.assertRaises(mw.RefusedError) as e:
                self.build(**kw)
        fetch.assert_not_called()
        trusted.assert_not_called()
        self.assertFalse(self.tree.exists())
        self.assertEqual(self.branches(), "")
        return str(e.exception)

    def assert_names_clone_and_reason(self, message: str, reason: str):
        self.assertIn("cannot read", message)
        self.assertIn(str(self.clone), message)
        self.assertIn(reason, message)
        self.assertNotIn("no local main", message, "a clone whose main cannot be read is not a clone with no main")

    def test_an_unviewable_clone_refuses_naming_the_clone_and_the_reason(self):
        for from_local in (False, True):
            with self.subTest(from_local=from_local):
                self.assert_names_clone_and_reason(self.refused(git_view.Unviewable("boom"), from_local=from_local), "unreadable: boom")

    def test_relative_default_cache_is_reported_without_creating_a_worktree(self):
        real_base = git_view._base
        for from_local in (False, True):
            with self.subTest(from_local=from_local), mock.patch.dict(os.environ, {"XDG_CACHE_HOME": "relative-cache"}), \
                    mock.patch.object(git_view, "_base", side_effect=lambda layout, base: real_base(layout, None)), \
                    mock.patch.object(mw.git_trees, "fetch_base") as fetch, mock.patch.object(mw, "git_trusted") as trusted:
                code, out, err = self.run_main(*(["--from-local"] if from_local else []))
                self.assertEqual(code, 1)
                self.assertEqual(out, "")
                self.assert_names_clone_and_reason(err, "CacheError: view cache base 'relative-cache' is relative")
                self.assertNotIn("Traceback", err)
                fetch.assert_not_called()
                trusted.assert_not_called()
                self.assertFalse(self.tree.exists())
                self.assertEqual(self.branches(), "")

    def test_an_oserror_from_the_view_refuses_naming_the_clone_and_the_reason(self):
        for from_local in (False, True):
            with self.subTest(from_local=from_local):
                self.assert_names_clone_and_reason(self.refused(OSError("disk full"), from_local=from_local), "OSError: disk full")

    def test_an_unwritable_cache_refuses_naming_the_clone_and_the_reason(self):
        readonly = self.root / "readonly"
        readonly.mkdir()
        readonly.chmod(0o500)
        self.addCleanup(readonly.chmod, 0o700)
        real = git_view.run

        def run(args, path, **kw):
            kw["base"] = readonly / "views"
            return real(args, path, **kw)

        for from_local in (False, True):
            with self.subTest(from_local=from_local):
                message = self.refused(run, from_local=from_local)
                self.assertIn("cannot read", message)
                self.assertIn(str(self.clone), message)
                self.assertNotIn("no local main", message)

    def test_a_really_missing_main_keeps_its_own_refusals(self):
        make_repo.git(["checkout", "-q", "-b", "elsewhere"], self.clone)
        make_repo.git(["branch", "-D", "main"], self.clone)
        with self.assertRaises(mw.RefusedError) as e:
            self.build(from_local=True)
        self.assertIn("no local main to cut from; drop --from-local", str(e.exception))
        self.assertNotIn("cannot read", str(e.exception))


class BaseRefTest(unittest.TestCase):
    """The choice of base, with no repo."""

    def test_a_clean_fetch_cuts_from_the_fetched_remote_ref(self):
        self.assertEqual(mw.base_ref("", False), "refs/remotes/origin/main")

    def test_from_local_cuts_from_local_main_whatever_the_fetch_said(self):
        """Not the bare `main`: a tag called `main` would shadow it. The full ref names the branch alone."""
        self.assertEqual(mw.base_ref("", True), "refs/heads/main")
        self.assertEqual(mw.base_ref("fatal: could not read from remote", True), "refs/heads/main")

    def test_a_failed_fetch_without_from_local_is_refused(self):
        with self.assertRaises(mw.RefusedError):
            mw.base_ref("fatal: could not read from remote", False)


class ArgvTest(unittest.TestCase):
    def test_git_is_called_with_a_list_and_never_through_a_shell(self):
        source = (Path(mw.__file__)).read_text()
        self.assertNotIn("shell=True", source)
        self.assertNotIn("os.system", source)

    def test_positionals_are_separated_from_options(self):
        """A branch or path that starts with a dash must not become a flag."""
        self.assertIn('"--"', (Path(mw.__file__)).read_text())


class CloneRootTest(CloneCase):
    """#311: the clone path must be the repository root git would report, and the report names that clone."""

    def test_a_plain_directory_inside_another_repository_is_refused(self):
        notes = self.root / "notes"
        notes.mkdir()
        # The workspace root is itself a repository; `git -C notes` walks up to it.
        make_repo.git(["init", "-q", "--initial-branch=main"], self.root)
        self.assertEqual(
            Path(make_repo.git(["rev-parse", "--show-toplevel"], notes)).resolve(),
            self.root.resolve(),
            "fixture: git must accept notes via the enclosing root, or the refusal proves nothing",
        )
        with self.assertRaises(mw.RefusedError) as e:
            self.build(clone=notes)
        self.assertIn("not a repository root", str(e.exception))
        self.assertIn(str(notes), str(e.exception))
        self.assertFalse(self.tree.exists())
        self.assertEqual(self.branches(), "")

    def test_the_report_line_names_the_clone(self):
        self.assertIn(str(self.clone), mw.line(self.build()))

    def test_a_symlinked_clone_keeps_the_supplied_path_in_the_report(self):
        alias = self.root / "clone-alias"
        alias.symlink_to(self.clone, target_is_directory=True)
        made = self.build(clone=alias)
        self.assertEqual(made.clone, alias)
        self.assertTrue(mw.line(made).endswith(f" from {alias}"))


class SettingsCloneTest(CloneCase):
    """#54: `--clone` is required. A name in the settings' `[repos]` table is that entry's path, relative to the workspace root;
    any other value is a path, as without `[repos]`. Settings that cannot be read refuse, whatever `--clone` is."""

    def settings(self, toml: str, line: str = "- Settings: `chief-of-stuff.toml`\n"):
        (self.root / "CLAUDE.md").write_text(CLAUDE + line)
        (self.root / "chief-of-stuff.toml").write_text(toml)

    def another_clone(self, base: Path, name: str) -> Path:
        """A second real repository with its own origin, under `base`."""
        return make_repo.build(base, {"clone": name, "origin": f"{name}.git", "trees": "trees", "worktrees": []})

    def assert_cut_from(self, clone: Path):
        common = Path(make_repo.git(["rev-parse", "--path-format=absolute", "--git-common-dir"], self.tree)).resolve()
        self.assertEqual(common, (clone / ".git").resolve(), "the tree belongs to the repository asked for")

    def assert_refused_with_nothing_made(self, code: int, out: str, err: str):
        self.assertEqual((code, out), (1, ""))
        self.assertTrue(err.startswith("refused:"), err)
        self.assertFalse(self.tree.exists())
        self.assertEqual(self.branches(), "")

    def chdir(self, path: Path):
        before = os.getcwd()
        os.chdir(path)
        self.addCleanup(os.chdir, before)

    def test_clone_is_required(self):
        self.settings('[repos]\nalder = "repo"\n')
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as raised:
            self.invoke()
        self.assertEqual(raised.exception.code, 2)
        self.assertFalse(self.tree.exists())

    def test_a_name_is_cut_from_its_own_entry_relative_to_the_workspace_root(self):
        birch = self.another_clone(self.root, "birch-checkout")
        self.settings('[repos]\nalder = "repo"\nbirch = "birch-checkout"\n')  # the one asked for is not the first
        code, out, err = self.invoke("--clone", "birch")
        self.assertEqual((code, err), (0, ""))
        self.assert_cut_from(birch)
        self.assertEqual(out, f"worktree {self.tree.resolve()} on feat/new off origin/main {rev(self.tree)[:7]} for a implementer from {birch}\n")

    def test_a_value_that_is_no_name_is_a_path_relative_to_the_current_directory(self):
        (self.root / "work").mkdir()
        mine = self.another_clone(self.root / "work", "mine")  # the root has no `mine`
        self.settings('[repos]\nalder = "repo"\n')
        self.chdir(self.root / "work")
        code, out, err = self.invoke("--clone", "mine")
        self.assertEqual((code, err), (0, ""))
        self.assert_cut_from(mine)

    def test_a_name_wins_over_a_directory_of_the_same_name_in_the_current_directory(self):
        other = self.another_clone(self.root, "other")
        self.settings('[repos]\nrepo = "other"\n')  # `repo` is also a directory under the root
        self.chdir(self.root)
        code, out, err = self.invoke("--clone", "repo")
        self.assertEqual((code, err), (0, ""))
        self.assert_cut_from(other)

    def test_the_file_the_settings_line_names_is_the_one_read(self):
        self.another_clone(self.root, "decoy")
        self.settings('[repos]\nalder = "decoy"\n')  # what a hardcoded `chief-of-stuff.toml` would read
        (self.root / "team").mkdir()
        (self.root / "team" / "prefs.toml").write_text('[repos]\nalder = "repo"\n')
        (self.root / "CLAUDE.md").write_text(CLAUDE + "- Settings: `team/prefs.toml`\n")
        code, out, err = self.invoke("--clone", "alder")
        self.assertEqual(code, 0, err)
        self.assert_cut_from(self.clone)

    def test_a_settings_line_with_no_file_is_no_entries_and_the_value_is_a_path(self):
        self.settings("")
        (self.root / "chief-of-stuff.toml").unlink()
        code, out, err = self.run_main()
        self.assertEqual((code, err), (0, ""))
        self.assert_cut_from(self.clone)

    def test_settings_that_cannot_be_used_refuse_with_their_error_for_a_name_and_for_a_path(self):
        self.chdir(self.root)  # `repo` is a name and a directory here: neither may stand in for the entry that cannot be read
        for how, write, about in (("malformed", lambda: self.settings("not toml ["), "chief-of-stuff.toml"),
                                  ("not UTF-8", lambda: (self.settings(""), (self.root / "chief-of-stuff.toml").write_bytes(b"\xff\xfe[repos]\n")),
                                   "chief-of-stuff.toml"),
                                  ("a bad entry", lambda: self.settings("[repos]\nrepo = 3\n"), "[repos] repo must be a path")):
            write()
            for given in ("repo", str(self.clone)):
                with self.subTest(how=how, given=given):
                    code, out, err = self.invoke("--clone", given)
                    self.assert_refused_with_nothing_made(code, out, err)
                    self.assertIn(about, err)

    def test_settings_that_cannot_be_read_refuse_rather_than_raise(self):
        if os.geteuid() == 0:
            self.skipTest("root reads a mode-000 file; the unreadable case cannot be made")
        self.settings('[repos]\nalder = "repo"\n')
        path = self.root / "chief-of-stuff.toml"
        path.chmod(0)
        self.addCleanup(path.chmod, 0o600)
        code, out, err = self.invoke("--clone", "alder")
        self.assert_refused_with_nothing_made(code, out, err)
        self.assertIn("chief-of-stuff.toml", err)

    def test_a_clone_that_is_not_a_directory_is_refused_naming_it(self):
        self.settings('[repos]\nalder = "no-such-clone"\n')
        for how, given, named in (("an entry", "alder", "no-such-clone"), ("a path", str(self.root / "gone"), "gone")):
            with self.subTest(how):
                code, out, err = self.invoke("--clone", given)
                self.assert_refused_with_nothing_made(code, out, err)
                self.assertIn(named, err)
                self.assertIn("not a directory", err)
                self.assertNotIn("branch name", err, "the branch is fine; the repository is what is missing")

    def test_an_empty_clone_is_refused(self):
        self.settings('[repos]\nalder = "repo"\n')
        code, out, err = self.invoke("--clone", "")
        self.assert_refused_with_nothing_made(code, out, err)
        self.assertIn("empty", err)


class FromBranchTest(CloneCase):
    """#62: `--from-branch` checks out an existing remote branch into the new tree instead of
    cutting a throwaway one off main. It fetches first, and refuses a branch the remote lacks."""

    def mr_branch(self, name: str = "feat/mr") -> str:
        """The branch on origin, one commit further than any earlier call left it; returns origin's head.
        The workspace clone has never seen the branch: only a fetch in `build` can find it."""
        other = self.root / "other"
        shutil.rmtree(other, ignore_errors=True)
        make_repo.git(["clone", "-q", str(self.root / "origin.git"), str(other)], self.root)
        if make_repo.git(["ls-remote", "--heads", "origin", name], other):
            make_repo.git(["checkout", "-q", name], other)
        else:
            make_repo.git(["checkout", "-q", "-b", name, "origin/main"], other)
        (other / "fix.txt").write_text(f"the fix {uuid4().hex}\n")
        make_repo.git(["add", "-A"], other)
        make_repo.git(["commit", "-q", "-m", "the fix"], other)
        make_repo.git(["push", "-q", "origin", name], other)
        return rev(other)

    def local_branches(self) -> set[str]:
        return set(make_repo.git(["for-each-ref", "--format=%(refname:short)", "refs/heads"], self.clone).splitlines())

    def test_a_from_branch_tree_is_on_the_remote_branchs_head(self):
        head = self.mr_branch()
        made = self.build(branch="feat/mr", from_branch="feat/mr")
        self.assertTrue((made.path / ".git").exists())
        self.assertEqual(rev(made.path), head, "the tree starts at the remote branch's head, not main's")
        self.assertEqual(make_repo.git(["rev-parse", "--abbrev-ref", "HEAD"], made.path), "HEAD")

    def test_a_from_branch_tree_starts_at_the_branchs_newest_remote_head(self):
        self.mr_branch()
        head = self.mr_branch()  # a second push the clone has not seen: the build must fetch first
        made = self.build(branch="feat/mr", from_branch="feat/mr")
        self.assertEqual(rev(made.path), head)

    def test_a_from_branch_tree_creates_no_branch(self):
        self.mr_branch()
        before = self.local_branches()
        self.build(branch="feat/mr", from_branch="feat/mr")
        self.assertEqual(self.local_branches() - before, set(),
                         "the clone creates no branch at all: the tree is detached at the remote head")

    def test_a_from_branch_tree_never_claims_the_branch_another_tree_holds(self):
        """#115: a stale tree still holding the MR's branch must not refuse a fixer's tree; the
        from-branch materialisation is detached, so the claim stays with the tree that holds it."""
        head = self.mr_branch()
        make_repo.git(["fetch", "-q", "origin"], self.clone)
        stale = self.root / "stale"
        make_repo.git(["worktree", "add", "-q", str(stale), "feat/mr"], self.clone)
        before = self.local_branches()
        made = self.build(branch="feat/mr", from_branch="feat/mr")
        self.assertEqual(rev(made.path), head, "the fixer's tree starts at the remote branch's head")
        self.assertEqual(make_repo.git(["rev-parse", "--abbrev-ref", "HEAD"], made.path), "HEAD",
                         "the fixer's tree is detached: it took no branch claim")
        self.assertEqual(make_repo.git(["rev-parse", "--abbrev-ref", "HEAD"], stale), "feat/mr",
                         "the stale tree keeps the branch it holds")
        self.assertEqual(self.local_branches() - before, set(), "no branch was cut for the fixer")

    def test_a_branch_missing_on_the_remote_is_refused_and_creates_nothing(self):
        with self.assertRaises(mw.RefusedError) as raised:
            self.build(branch="ghost", from_branch="ghost")
        self.assertFalse(self.tree.exists())
        self.assertIn("ghost", str(raised.exception))

    def test_from_branch_and_from_local_are_refused_together(self):
        self.mr_branch()
        with self.assertRaises(mw.RefusedError):
            self.build(branch="feat/mr", from_branch="feat/mr", from_local=True)
        self.assertFalse(self.tree.exists())

    def test_a_from_branch_with_a_different_branch_name_is_refused(self):
        self.mr_branch()
        with self.assertRaises(mw.RefusedError):
            self.build(branch="feat/new", from_branch="feat/mr")
        self.assertFalse(self.tree.exists())

    def test_the_report_names_the_remote_branch_as_the_base(self):
        self.mr_branch()
        made = self.build(branch="feat/mr", from_branch="feat/mr")
        self.assertEqual(made.base, "origin/feat/mr")
        self.assertIn("origin/feat/mr", mw.line(made))

    def test_main_takes_from_branch_alone_and_reports_the_branch_it_made(self):
        self.mr_branch()
        argv = ["--type", "implementer", "--name", "wt-new", "--root", str(self.root),
                "--clone", str(self.clone), "--from-branch", "feat/mr"]
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = mw.main(argv)
        self.assertEqual(code, 0, err.getvalue())
        self.assertIn(str(self.tree), out.getvalue())
        self.assertIn("origin/feat/mr", out.getvalue())


if __name__ == "__main__":
    unittest.main()
