"""Unit tests for scripts/git_trees.py: the read-only git reader behind the orphan check (#43).

Real repos, as in test_audit_tasks: a bare origin, a clone, and worktrees off it. `rev-list` and `@{u}`
have no useful fake.
"""

import contextlib
import inspect
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import git_trees  # noqa: E402
import git_view  # noqa: E402
from evals import git_taint as taint  # noqa: E402
import tree_state  # noqa: E402
from tree_state import TreeState, at_risk  # noqa: E402

ENV = {"PATH": "/usr/bin:/bin:/usr/local/bin", "GIT_TERMINAL_PROMPT": "0", "GIT_CONFIG_GLOBAL": "/dev/null",
       "GIT_AUTHOR_NAME": "T", "GIT_AUTHOR_EMAIL": "t@example.test",
       "GIT_COMMITTER_NAME": "T", "GIT_COMMITTER_EMAIL": "t@example.test"}


_views = contextlib.ExitStack()
_base: Path  # where this module's views are built


def setUpModule() -> None:
    """Reads go through git_view.run (#443): give them a private base, never the user's cache."""
    global _base
    _base = Path(_views.enter_context(tempfile.TemporaryDirectory()))
    _views.enter_context(taint.private_git_views(_base))


def tearDownModule() -> None:
    _views.close()


def git(*args: str, cwd: Path) -> str:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True,
                          env={**ENV, "HOME": str(cwd)}).stdout.strip()


def commit(tree: Path, name: str) -> None:
    (tree / name).write_text("work\n")
    git("add", name, cwd=tree)
    git("commit", "-m", name, cwd=tree)


def raw_git_dir(tree: Path) -> tuple[int, str]:
    """`(exit code, output)` of plain `git -C <tree> rev-parse --git-dir`: the oracle that git itself can, or cannot, open
    this layout. It is the test's own question to git, never the code's (#443 slice 4 deleted `git_trees._raw_git`)."""
    out = subprocess.run(["git", "-C", str(tree), "rev-parse", "--git-dir"], capture_output=True, text=True,
                         errors="backslashreplace", env=taint.ENV, timeout=15)
    return out.returncode, (out.stdout or out.stderr).strip()


@contextlib.contextmanager
def git_calls():
    """Every `git` process started inside the block, as `(argv, env)`: `Popen` is what `subprocess.run` and the view both use."""
    calls, real = [], subprocess.Popen

    class Spy(real):
        def __init__(self, args, *a, **kw):
            if isinstance(args, (list, tuple)) and args and args[0] == "git":
                calls.append((list(args), kw.get("env") or {}))
            super().__init__(args, *a, **kw)

    with patch.object(subprocess, "Popen", Spy):
        yield calls


def unsanitised(call: tuple[list[str], dict[str, str]]) -> bool:
    """A git process that is neither the fetch (a write, legitimately the user's) nor one of the view's own: those run with
    `GIT_DIR` pointing at the private view, or read the explicit config file (`config --file`) before there is one."""
    argv, env = call
    return "fetch" not in argv and argv[1:3] != ["config", "--file"] and "GIT_DIR" not in env


class Repo(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        origin, self.clone, self.trees = self.root / "origin.git", self.root / "repo", self.root / "trees"
        origin.mkdir()
        git("init", "--bare", "--initial-branch=main", ".", cwd=origin)
        self.clone.mkdir()
        git("init", "--initial-branch=main", ".", cwd=self.clone)
        commit(self.clone, "README.md")
        git("remote", "add", "origin", str(origin), cwd=self.clone)
        git("push", "-u", "origin", "main", cwd=self.clone)
        self.trees.mkdir()

    def tree(self, name: str, branch: str) -> Path:
        git("worktree", "add", "-b", branch, str(self.trees / name), "main", cwd=self.clone)
        return self.trees / name

    def pruned(self, name: str = "wt-dead") -> tuple[str, Path]:
        """`(sha, tree)`: a real `git worktree add`, then `<common>/worktrees/<name>` deleted. The repository is still
        there and no longer knows this tree."""
        sha = git("rev-parse", "HEAD", cwd=self.clone)
        tree = self.tree(name, f"feat/{name}")
        shutil.rmtree(self.clone / ".git" / "worktrees" / name)
        return sha, tree


class ReadStateTest(Repo):
    def test_a_tree_redirected_to_another_repository_is_unreadable(self) -> None:
        tree = self.tree("wt-a", "feat/a")
        common = (self.clone / ".git").resolve()
        other = self.root / "other"
        git("clone", str(self.clone), str(other), cwd=self.root)
        decoy = self.root / "decoy"
        git("worktree", "add", "-b", "decoy", str(decoy), cwd=other)
        admin = git_view.locate(decoy).gitdir
        (admin / "gitdir").write_text(str(tree / ".git") + "\n")
        (tree / ".git").write_text(f"gitdir: {admin}\n")
        state = git_trees.read_state(tree, expected_common=common)
        self.assertEqual((state.branch, state.dirty, state.off_origin, state.unpushed),
                         (tree_state.UNREADABLE_BRANCH, None, None, None))
        commons = git_trees.trusted_commons([self.clone])
        self.assertEqual(commons[tree.resolve()], common)
        self.assertEqual(git_trees.sha_trees(self.root, "trees", commons), [("wt-a", tree.resolve())])
        sha = git("rev-parse", "HEAD", cwd=self.clone)
        with patch.object(git_trees, "fetch_base") as fetch:
            code, line, held = git_trees.check_sha(sha, tree, expected_common=commons[tree.resolve()])
        self.assertEqual((code, held), (2, True))
        self.assertIn("git could not read this repository", line)
        fetch.assert_not_called()

    def test_a_fresh_branch_at_main_holds_nothing(self) -> None:
        s = git_trees.read_state(self.tree("wt-a", "feat/a"))
        self.assertEqual((s.branch, s.dirty, s.off_origin, s.unpushed), ("feat/a", 0, 0, None))

    def test_uncommitted_files_are_counted(self) -> None:
        t = self.tree("wt-a", "feat/a")
        (t / "one.txt").write_text("x\n")
        (t / "two.txt").write_text("x\n")
        self.assertEqual(git_trees.read_state(t).dirty, 2)

    def test_a_local_commit_has_no_upstream(self) -> None:
        t = self.tree("wt-a", "feat/a")
        commit(t, "a.txt")
        s = git_trees.read_state(t)
        self.assertEqual((s.off_origin, s.unpushed), (1, None))

    def test_a_pushed_branch_has_nothing_unpushed(self) -> None:
        t = self.tree("wt-a", "feat/a")
        commit(t, "a.txt")
        git("push", "-u", "origin", "feat/a", cwd=t)
        s = git_trees.read_state(t)
        self.assertEqual((s.off_origin, s.unpushed), (1, 0))

    def test_a_commit_after_the_push_is_unpushed(self) -> None:
        t = self.tree("wt-a", "feat/a")
        commit(t, "a.txt")
        git("push", "-u", "origin", "feat/a", cwd=t)
        commit(t, "b.txt")
        s = git_trees.read_state(t)
        self.assertEqual((s.off_origin, s.unpushed), (2, 1))

    def test_with_no_origin_main_the_count_is_unknown(self) -> None:
        t = self.tree("wt-a", "feat/a")
        git("update-ref", "-d", "refs/remotes/origin/main", cwd=self.clone)
        self.assertIsNone(git_trees.read_state(t).off_origin)


class DiscoverTest(Repo):
    def test_every_tree_under_the_worktrees_dir_is_found(self) -> None:
        """No File ownership row is needed to be seen: the field report's tree had none."""
        self.tree("wt-b", "feat/b")
        self.tree("wt-a", "feat/a")
        (self.trees / "notes").mkdir()
        found = git_trees.discover(self.root, "trees/")
        self.assertEqual([p.name for p in found], ["wt-a", "wt-b"])

    def test_no_worktrees_line_discovers_nothing(self) -> None:
        """The root's own children include the main clone, which is nobody's worktree."""
        self.tree("wt-a", "feat/a")
        self.assertEqual(git_trees.discover(self.root, ""), [])

    def test_a_link_out_of_the_workspace_is_not_followed(self) -> None:
        with tempfile.TemporaryDirectory() as away:
            (Path(away) / ".git").mkdir()
            (self.trees / "escape").symlink_to(away)
            self.assertEqual(git_trees.discover(self.root, "trees/"), [])


class ShaVerdictTest(unittest.TestCase):
    """#49: `audit --sha` answers in one line, and the exit code is the verdict. Pure: no git here.

    The lines here carry no ` [<tree>]` suffix: the caller (`audit_tasks`, which asks every repository under the
    worktrees directory) appends the tree's name, so `sha_verdict` and `check_sha` answer about one repository only.
    """

    S, T = "54b7eb3", "9c790f3"

    def test_the_table(self) -> None:
        err = "fatal: unable to access the remote"
        table = [
            # what, fetch_error, exists, ancestor -> code, line
            ("fetched, ancestor", "", True, True,
             0, "sha 54b7eb3: on origin/main 9c790f3 (fetched)"),
            ("fetch failed, ancestor of the last-fetched ref: never a yes", err, True, True,
             2, f"sha 54b7eb3: unknown \u2014 fetch failed: {err}; origin/main 9c790f3 as last fetched holds it"),
            ("fetched, not an ancestor", "", True, False,
             1, "sha 54b7eb3: not on origin/main 9c790f3 (fetched)"),
            ("fetched, no such commit", "", False, False,
             1, "sha 54b7eb3: no such commit after fetch, so not on origin/main 9c790f3"),
            ("fetch failed, not an ancestor", err, True, False,
             2, f"sha 54b7eb3: unknown \u2014 fetch failed: {err}; origin/main 9c790f3 as last fetched does not hold it"),
            ("fetch failed, no such commit", err, False, False,
             2, f"sha 54b7eb3: unknown \u2014 fetch failed: {err}; origin/main 9c790f3 as last fetched does not hold it"),
        ]
        for what, fetch_error, exists, ancestor, code, line in table:
            with self.subTest(what):
                self.assertEqual(git_trees.sha_verdict(self.S, fetch_error, exists, ancestor, self.T), (code, line))

    def test_a_fetch_failure_still_says_local_main_holds_it(self) -> None:
        """#49 round 7, 5: the sixth argument, `local`, is True when local main holds the commit. The answer stays
        unknown (exit 2), and the fact is said instead of dropped; False reads as the table above."""
        err = "fatal: unable to access the remote"
        self.assertEqual(git_trees.sha_verdict(self.S, err, True, False, self.T, True),
                         (2, f"sha 54b7eb3: unknown \u2014 fetch failed: {err}; origin/main 9c790f3 as last fetched does not hold it; local main holds it"))
        self.assertEqual(git_trees.sha_verdict(self.S, err, True, False, self.T, False),
                         git_trees.sha_verdict(self.S, err, True, False, self.T))

    def test_the_sha_is_printed_as_it_was_given(self) -> None:
        full = "54b7eb3" + "0" * 33
        self.assertEqual(git_trees.sha_verdict(full, "", True, True, self.T),
                         (0, f"sha {full}: on origin/main 9c790f3 (fetched)"))


class FetchBaseTest(Repo):
    """#49: the audit never fetched, so `origin/main` was whatever the last fetch left."""

    def push_from_another_clone(self, tag: str | None = None) -> str:
        other = Path(tempfile.mkdtemp(dir=self.root, prefix="other-"))
        git("clone", "-q", str(self.root / "origin.git"), str(other), cwd=self.root)
        commit(other, f"theirs-{other.name}.txt")
        if tag:
            git("tag", tag, cwd=other)
        git("push", "-q", "origin", "main", *([tag] if tag else []), cwd=other)
        return git("rev-parse", "HEAD", cwd=other)

    def test_success_is_the_empty_string_and_origin_main_has_moved(self) -> None:
        new = self.push_from_another_clone()
        self.assertNotEqual(git("rev-parse", "origin/main", cwd=self.clone), new)
        self.assertEqual(git_trees.fetch_base(self.clone), "")
        self.assertEqual(git("rev-parse", "origin/main", cwd=self.clone), new)

    def test_origin_main_moves_whatever_the_fetch_refspec_is(self) -> None:
        """`git fetch origin main` only updates `refs/remotes/origin/main` when `remote.origin.fetch` maps it."""
        for what, refspec in (("no refspec", None), ("maps only another branch", "+refs/heads/other:refs/remotes/origin/other")):
            with self.subTest(what):
                if refspec is None:
                    git("config", "--unset-all", "remote.origin.fetch", cwd=self.clone)
                else:
                    git("config", "--replace-all", "remote.origin.fetch", refspec, cwd=self.clone)
                new = self.push_from_another_clone()
                self.assertEqual(git_trees.fetch_base(self.clone), "")
                self.assertEqual(git("rev-parse", "origin/main", cwd=self.clone), new)

    def test_it_fetches_from_a_worktree_too(self) -> None:
        tree = self.tree("wt-a", "feat/a")
        new = self.push_from_another_clone()
        self.assertEqual(git_trees.fetch_base(tree), "")
        self.assertEqual(git("rev-parse", "origin/main", cwd=tree), new)

    def test_a_missing_remote_is_an_error_string_not_an_exception(self) -> None:
        git("remote", "set-url", "origin", str(self.root / "gone.git"), cwd=self.clone)
        err = git_trees.fetch_base(self.clone)
        self.assertIsInstance(err, str)
        self.assertTrue(err.strip())
        self.assertNotIn("\n", err)  # one line: it is printed inside the verdict line

    def test_a_non_utf8_byte_on_stderr_is_an_unknown_line_not_a_traceback(self) -> None:
        """#49 round 11: git, ssh or a credential helper can write any byte to stderr; decoding it as text raised
        `UnicodeDecodeError`, which is neither `OSError` nor `SubprocessError`. A REAL subprocess, not a fake `run` (a fake
        returning a str would hide it): `core.sshCommand` in this clone is a Python one-liner that writes `fatal: boom \xff`
        to stderr and exits 128, and origin is an ssh-style URL, so the real `git fetch` runs it and relays the bytes."""
        helper = "import sys; sys.stderr.buffer.write(b'fatal: boom \\xff\\n'); sys.exit(128)"
        git("remote", "set-url", "origin", "ssh://example.invalid/x.git", cwd=self.clone)
        git("config", "core.sshCommand", f"{shlex.quote(sys.executable)} -c {shlex.quote(helper)}", cwd=self.clone)
        err = git_trees.fetch_base(self.clone)
        self.assertIsInstance(err, str)
        self.assertTrue(err.strip())
        self.assertEqual([c for c in err if not c.isprintable()], [], repr(err))
        self.assertIn("fatal: boom", err)

    def test_git_that_cannot_run_or_times_out_is_an_error_string(self) -> None:
        for exc in (OSError("no git"), subprocess.TimeoutExpired(["git"], 30)):
            with self.subTest(type(exc).__name__), patch.object(git_trees.subprocess, "run", side_effect=exc):
                err = git_trees.fetch_base(self.clone)
                self.assertIsInstance(err, str)
                self.assertTrue(err.strip())

    def test_a_fetch_timeout_reports_only_its_bound(self) -> None:
        for timeout in (7, 30):
            with self.subTest(timeout=timeout), patch.object(
                git_trees.subprocess, "run", side_effect=subprocess.TimeoutExpired(
                    ["git", "-C", str(self.clone), "fetch"], timeout,
                ),
            ):
                err = git_trees.fetch_base(self.clone, timeout=timeout)
            self.assertEqual(err, f"git fetch timed out after {timeout}s")

    def test_the_error_never_carries_the_exceptions_text(self) -> None:
        """#49 round 9, 5: a timeout's text repeats its command, which holds the tree's path; the error is printed."""
        cmd = ["git", "-C", str(self.clone), "fetch", "-q", "origin"]
        for exc in (subprocess.TimeoutExpired(cmd=cmd, timeout=30), OSError(2, "No such file", str(self.clone))):
            with self.subTest(type(exc).__name__), patch.object(git_trees.subprocess, "run", side_effect=exc):
                err = git_trees.fetch_base(self.clone)
                self.assertTrue(err.strip())
                for path in (self.clone, self.clone.resolve()):
                    self.assertNotIn(str(path), err)

    def test_the_call_carries_the_write_pins_and_the_callers_credentials(self) -> None:
        """#556: the fetch is a launcher write into a tree a worker can write, so it runs with the write pins on argv
        and the audit's environment — no home, global or system config, no prompts — plus only the caller's credential
        variables: a fetch the caller could authenticate still authenticates. Still not through the sandboxed `git()`."""
        seen = {}

        def fake(argv, **kw):
            seen["argv"], seen["kw"] = argv, kw
            return subprocess.CompletedProcess(argv, 0, "", "")

        with patch.dict(os.environ, {"COS_FETCH_ENV_PROBE": "kept", "SSH_AUTH_SOCK": "/agent.sock",
                                     "GIT_SSH_COMMAND": "ssh -i key"}), patch.object(git_trees.subprocess, "run", fake):
            self.assertEqual(git_trees.fetch_base(self.clone, timeout=7), "")
        self.assertEqual(seen["argv"], ["git", *git_trees.GIT_WRITE_PINS, "-C", str(self.clone), "fetch", "-q",
                                        "--no-tags", "origin", "+refs/heads/main:refs/remotes/origin/main"])
        env = seen["kw"]["env"]
        self.assertEqual(env["GIT_TERMINAL_PROMPT"], "0")
        self.assertEqual(env["HOME"], git_trees.SAFE_HOME, "no home of the caller's is read")
        self.assertEqual(env["GIT_CONFIG_GLOBAL"], os.devnull)
        self.assertEqual(env["GIT_CONFIG_SYSTEM"], os.devnull)
        self.assertEqual(env["GIT_NO_REPLACE_OBJECTS"], "1")
        self.assertEqual(env["SSH_AUTH_SOCK"], "/agent.sock")
        self.assertEqual(env["GIT_SSH_COMMAND"], "ssh -i key")
        self.assertNotIn("COS_FETCH_ENV_PROBE", env, "nothing but the credential variables rides along")
        self.assertEqual(seen["kw"]["timeout"], 7)
        self.assertFalse(seen["kw"].get("shell"))

    def test_the_fetch_writes_only_origin_main_no_tags(self) -> None:
        """#49 D: upstream gains a tag on main; the default tag auto-follow would copy it into the clone."""
        new = self.push_from_another_clone(tag="upstream-tag")
        self.assertEqual(git_trees.fetch_base(self.clone), "")
        self.assertEqual(git("rev-parse", "origin/main", cwd=self.clone), new)
        self.assertEqual(git("tag", "--list", cwd=self.clone), "")

    def test_the_fetch_does_not_carry_the_variables_that_pick_a_repository(self) -> None:
        """#49 C: a caller's `GIT_DIR` beats `-C`, so the fetch would write into another repository while the reads
        look at this one. Credentials and ssh settings are still the caller's."""
        seen = {}

        def fake(argv, **kw):
            seen["env"] = kw["env"]
            return subprocess.CompletedProcess(argv, 0, "", "")

        for var in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_NAMESPACE", "GIT_COMMON_DIR", "GIT_OBJECT_DIRECTORY",
                    "GIT_ALTERNATE_OBJECT_DIRECTORIES"):
            with self.subTest(var), patch.dict(os.environ, {var: "/elsewhere"}), patch.object(git_trees.subprocess, "run", fake):
                git_trees.fetch_base(self.clone)
                self.assertNotIn(var, list(seen["env"]), f"{var} reached the fetch")
        with patch.dict(os.environ, {"GIT_SSH_COMMAND": "ssh -i key"}), patch.object(git_trees.subprocess, "run", fake):
            git_trees.fetch_base(self.clone)
            self.assertEqual(seen["env"]["GIT_SSH_COMMAND"], "ssh -i key")

    def test_a_callers_git_dir_does_not_move_the_fetch_to_another_repository(self) -> None:
        decoy = Path(tempfile.mkdtemp(dir=self.root, prefix="decoy-"))
        git("clone", "-q", str(self.root / "origin.git"), str(decoy), cwd=self.root)
        new = self.push_from_another_clone()
        with patch.dict(os.environ, {"GIT_DIR": str(decoy / ".git")}):
            self.assertEqual(git_trees.fetch_base(self.clone), "")
        self.assertEqual(git("rev-parse", "origin/main", cwd=self.clone), new)

    def test_git_failing_with_no_stderr_is_still_an_error_string(self) -> None:
        fake = subprocess.CompletedProcess(["git"], 1, "", "")
        with patch.object(git_trees.subprocess, "run", return_value=fake):
            self.assertTrue(git_trees.fetch_base(self.clone).strip())

    def test_the_error_is_the_first_fatal_line_and_never_holds_the_trees_path(self) -> None:
        """A missing remote prints four lines; the last, `and the repository exists.`, says nothing. The first `fatal:`
        line names the remote, and a remote under the tree would put the tree's path in a line that is printed."""
        git("remote", "set-url", "origin", str(self.root / "gone.git"), cwd=self.clone)
        err = git_trees.fetch_base(self.clone)
        self.assertTrue(err.startswith("fatal: "), err)
        self.assertNotIn("the repository exists", err)
        git("remote", "set-url", "origin", str(self.clone / "gone.git"), cwd=self.clone)
        err = git_trees.fetch_base(self.clone)
        self.assertTrue(err.startswith("fatal: "), err)
        self.assertNotIn(str(self.clone), err)
        self.assertNotIn(str(self.clone.resolve()), err)


    def test_the_error_text_a_remote_controls_carries_no_raw_control_character(self) -> None:
        """#49 round 8, 3b; round 10, 3: the line is printed, and git's stderr is whatever the remote makes it.
        `subprocess.run` is faked. Pinned: no character that is not printable (a control character, DEL, a C1 control such
        as CSI `\\x9b`, a bidi override, U+2028/9), `fatal: a` survives, and a to f stay in order (so a carriage return
        does not cut the text). The escape's spelling is not pinned."""
        hostile = subprocess.CompletedProcess([], 128, "", "fatal: a\x1b[31mb\rc\x7fd\x9be\u202ef\n")
        with patch.object(git_trees.subprocess, "run", return_value=hostile):
            line = git_trees.fetch_base(self.clone)
        self.assertEqual([c for c in line if not c.isprintable()], [], repr(line))
        self.assertTrue(line.startswith("fatal: a"), repr(line))
        self.assertRegex(line[len("fatal: "):], "a.*b.*c.*d.*e.*f")


class GitReaderTest(unittest.TestCase):
    def test_git_that_cannot_run_or_times_out_is_128_not_1(self) -> None:
        """`merge-base --is-ancestor` answers a no with exit 1, so a failed run must not look like one: 128 is git's own
        failure code. The text is what went wrong, for a caller that prints it. The failure is the view's (#443): `git()`
        is `git_view.run` and nothing else."""
        for exc in (OSError("no git"), subprocess.TimeoutExpired(["git"], 15)):
            with self.subTest(type(exc).__name__), patch.object(git_view, "run", side_effect=exc):
                code, text = git_trees.git(["rev-parse", "HEAD"], Path("."))
                self.assertEqual(code, 128)
                self.assertTrue(text.strip())


class NonUtf8OutputTest(Repo):
    def test_a_path_with_a_non_utf8_byte_is_read_not_a_traceback(self) -> None:
        """#49 round 11: `git()` decodes output as text, and a byte that is not UTF-8 reads as `\\xff`, never raises. A Linux
        checkout can hold such a name; macOS refuses to create one, so it is written as git would store it: an index entry.
        Real repository, no fake."""
        (self.root / "blob.txt").write_text("odd name\n")
        blob = git("hash-object", "-w", str(self.root / "blob.txt"), cwd=self.clone)
        subprocess.run([b"git", b"update-index", b"--add", b"--cacheinfo", b"100644," + blob.encode() + b",b\xff"],
                       cwd=self.clone, env={**ENV, "HOME": str(self.clone)}, check=True)
        raw = subprocess.run(["git", "-C", str(self.clone), "ls-files", "-z"], capture_output=True)
        self.assertIn(b"b\xff\0", raw.stdout)  # the premise: git itself prints the byte
        code, text = git_trees.git(["ls-files", "-z"], self.clone)
        self.assertEqual(code, 0)
        self.assertIn("b\\xff", text)

    def test_a_head_naming_a_ref_with_a_non_utf8_byte_is_unreadable_not_a_traceback(self) -> None:
        """#49 round 11 read this branch (`git rev-parse --abbrev-ref HEAD` printed the byte). Through the view (#443) a HEAD
        that is not UTF-8 is metadata it refuses, so the tree reads `unreadable` and `read_state` leaves it at risk."""
        sha = git("rev-parse", "HEAD", cwd=self.clone)
        git_dir = self.clone / ".git"
        (git_dir / "packed-refs").write_bytes(b"# pack-refs with: peeled fully-peeled sorted \n" + sha.encode() + b" refs/heads/b\xff\n")
        (git_dir / "HEAD").write_bytes(b"ref: refs/heads/b\xff\n")
        raw = subprocess.run(["git", "-C", str(self.clone), "rev-parse", "--abbrev-ref", "HEAD"], capture_output=True)
        self.assertEqual(raw.stdout, b"b\xff\n")  # the premise: git itself prints the byte
        code, text = git_trees.git(["rev-parse", "--abbrev-ref", "HEAD"], self.clone)
        self.assertEqual(code, 128)
        self.assertTrue(text.startswith("unreadable:"), text)
        state = git_trees.read_state(self.clone)
        self.assertEqual((state.dirty, state.off_origin, state.unpushed), (None, None, None))
        self.assertIn(tree_state.UNCOMMITTED_UNKNOWN, at_risk(state))


class ShaExitTest(unittest.TestCase):
    def test_an_unknown_blocks_a_yes_only_when_that_repository_may_hold_the_commit(self) -> None:
        """#49 round 10, 2: over `(code, held)`: 1 if any repository holds the commit and says no; else 2 if the list is
        empty or any repository is unknown AND held (it may hold the commit, or be the project the claim is about); else
        0 if any says yes; else 2 if any is unknown; else 1. An unrelated repository (no remote, a `master` default, a
        shallow clone, offline) does not hold the commit, so it cannot veto a yes forever."""
        yes, no_held, no_absent = (0, True), (1, True), (1, False)
        unknown_held, unknown_absent = (2, True), (2, False)
        table = [
            ("yes alone", [yes], 0),
            ("yes, no such commit elsewhere", [yes, no_absent], 0),
            ("yes, unknown that holds it", [yes, unknown_held], 2),
            ("yes, unknown that does not", [yes, unknown_absent], 0),
            ("yes, a holder says no", [yes, no_held], 1),
            ("a holder says no, unknown that does not hold it", [no_held, unknown_absent], 1),
            ("a holder says no, unknown that holds it", [unknown_held, no_held], 1),
            ("unknown that does not hold it, alone", [unknown_absent], 2),
            ("unknown that holds it, alone", [unknown_held], 2),
            ("no such commit, unknown that does not hold it", [no_absent, unknown_absent], 2),
            ("no such commit alone", [no_absent], 1),
            ("a holder says no alone", [no_held], 1),
            ("empty", [], 2),
        ]
        for what, answers, want in table:
            with self.subTest(what):
                self.assertEqual(git_trees.sha_exit(answers), want)


def git_makes_sha256() -> bool:
    with tempfile.TemporaryDirectory() as d:
        return subprocess.run(["git", "init", "-q", "--object-format=sha256", d], capture_output=True, env=ENV).returncode == 0


@unittest.skipUnless(git_makes_sha256(), "this git cannot `init --object-format=sha256`")
class Sha256CheckTest(unittest.TestCase):
    def test_a_full_sha256_id_pushed_to_main_is_on_origin_main(self) -> None:
        """#49 round 11: a repository made with `--object-format=sha256` has 64-digit ids; `--sha` refused them as usage."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            origin, clone = root / "origin.git", root / "repo"
            origin.mkdir()
            clone.mkdir()
            git("init", "--bare", "--object-format=sha256", "--initial-branch=main", ".", cwd=origin)
            git("init", "--object-format=sha256", "--initial-branch=main", ".", cwd=clone)
            commit(clone, "README.md")
            git("remote", "add", "origin", str(origin), cwd=clone)
            git("push", "-u", "origin", "main", cwd=clone)
            sha = git("rev-parse", "HEAD", cwd=clone)
            tip = git("rev-parse", "--short", "origin/main", cwd=clone)
            self.assertEqual(len(sha), 64)
            self.assertEqual(git_trees.check_sha(sha, clone), (0, f"sha {sha}: on origin/main {tip} (fetched)", True))


class CheckShaTest(Repo):
    def test_a_pushed_commit_is_on_origin_main(self) -> None:
        sha = git("rev-parse", "HEAD", cwd=self.clone)
        tip = git("rev-parse", "--short", "origin/main", cwd=self.clone)
        self.assertEqual(git_trees.check_sha(sha, self.clone), (0, f"sha {sha}: on origin/main {tip} (fetched)", True))

    def test_a_commit_only_on_a_branch_is_not(self) -> None:
        tree = self.tree("wt-a", "feat/a")
        commit(tree, "a.txt")
        sha = git("rev-parse", "HEAD", cwd=tree)
        tip = git("rev-parse", "--short", "origin/main", cwd=self.clone)
        self.assertEqual(git_trees.check_sha(sha, tree), (1, f"sha {sha}: not on origin/main {tip} (fetched)", True))

    def test_a_prefix_two_commits_share_is_unknown_not_a_pick(self) -> None:
        """Two commit ids start with it: no answer, whichever of them is on origin/main. `git` is patched, because two
        commits sharing a 7-hex prefix cannot be made cheaply."""
        calls = []

        def fake(args, cwd, **kw):
            calls.append(args)
            if args[0] == "rev-parse" and args[1].startswith("--disambiguate="):
                return 0, "9f3c1e7aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\n9f3c1e7bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
            if args[:2] == ["cat-file", "-t"]:
                return 0, "commit"
            return 0, "abc1234" if args[:2] == ["rev-parse", "--short"] else ""

        with patch.object(git_trees, "fetch_base", return_value=""), patch.object(git_trees, "git", fake):
            self.assertEqual(git_trees.check_sha("9f3c1e7", self.clone),
                             (2, "sha 9f3c1e7: unknown \u2014 more than one commit starts with it", True))
        self.assertFalse([c for c in calls if c[:1] == ["merge-base"]], calls)

    def tip(self) -> str:
        return git("rev-parse", "--short", "refs/remotes/origin/main", cwd=self.clone)

    def test_a_shallow_clone_never_gives_a_definite_no(self) -> None:
        """#49 F: the commit may be in the history the clone cut off."""
        commit(self.clone, "second.txt")
        git("push", "-q", "origin", "main", cwd=self.clone)
        older = git("rev-parse", "HEAD~1", cwd=self.clone)
        head = git("rev-parse", "HEAD", cwd=self.clone)
        shallow = self.root / "shallow"
        git("clone", "-q", "--depth", "1", f"file://{self.root / 'origin.git'}", str(shallow), cwd=self.root)
        self.assertEqual(git("rev-parse", "--is-shallow-repository", cwd=shallow), "true")
        self.assertEqual(git_trees.check_sha(older, shallow),
                         (2, f"sha {older}: unknown \u2014 shallow clone, so history is cut off", False))
        # D2 (#443 slice 4): a shallow clone never answers yes either. The view has no `shallow` file, so ancestry past the
        # boundary cannot be asked there and the answer is decided before any comparison. The "; local main holds it" note is
        # best effort: the view cannot read a real shallow clone's parent, so the note is absent and not required.
        code, line, _ = git_trees.check_sha(head, shallow)
        self.assertEqual(code, 2)
        self.assertTrue(line.startswith(f"sha {head}: unknown \u2014 shallow clone, so history is cut off"), line)
        for definite in ("not on origin/main", "no such commit", "on origin/main"):
            self.assertNotIn(definite, line)

    def test_a_commit_on_local_main_not_pushed_is_said_so(self) -> None:
        """#49 G: distinct from a commit on no main at all (`not on origin/main`), since the row audit reports an
        unpushed local main as a fact."""
        commit(self.clone, "local.txt")
        sha = git("rev-parse", "HEAD", cwd=self.clone)
        want = (1, f"sha {sha}: on local main only, not pushed to origin/main {self.tip()} (fetched)", True)
        self.assertEqual(git_trees.check_sha(sha, self.clone), want)
        self.assertEqual(git_trees.check_sha(sha, self.tree("wt-a", "feat/a")), want)

    def test_git_that_cannot_compare_is_unknown_not_a_no(self) -> None:
        """#49 H: `merge-base --is-ancestor` exits 1 for a no. Any other exit is git failing."""
        on = git("rev-parse", "HEAD", cwd=self.clone)
        tree = self.tree("wt-a", "feat/a")
        commit(tree, "a.txt")
        off = git("rev-parse", "HEAD", cwd=tree)
        real = git_trees.git

        def fake(args, cwd, **kw):
            return (128, "fatal: boom") if args[:2] == ["merge-base", "--is-ancestor"] else real(args, cwd)

        for what, sha in (("on origin/main", on), ("not on it", off)):
            with self.subTest(what), patch.object(git_trees, "git", fake):
                self.assertEqual(git_trees.check_sha(sha, tree),
                                 (2, f"sha {sha}: unknown \u2014 git could not compare it with origin/main", True))

    def test_an_annotated_tags_object_id_is_not_a_commit(self) -> None:
        """#49 H: `cat-file -e <id>^{commit}` peels a tag, so the tag's id passes for the commit it points at."""
        head = git("rev-parse", "HEAD", cwd=self.clone)
        git("tag", "-a", "v1", "-m", "release", cwd=self.clone)
        tag = git("rev-parse", "v1", cwd=self.clone)
        self.assertNotEqual(tag, head)
        for given in (tag, tag[:7]):
            with self.subTest(given):
                self.assertEqual(git_trees.check_sha(given, self.clone),
                                 (1, f"sha {given}: no such commit after fetch, so not on origin/main {self.tip()}", False))

    def test_a_read_that_fails_is_unknown_never_no_such_commit(self) -> None:
        """#49 round 7, 2: exit 128 from the read that lists the commits (`rev-parse --disambiguate`) or from `cat-file -t`
        on an id it listed means git could not read the repository, which says nothing about whether the commit exists.
        `held` is True (round 10, 2): git could not read the repository, so it may hold the commit. Not
        `cat-file -e <id>^{commit}`: that exits 128 for a blob or a missing object too (see the next test), so its 128
        is an answer, not a fault."""
        sha = git("rev-parse", "HEAD", cwd=self.clone)
        real = git_trees.git
        for what, broken in (("disambiguate", lambda a: a[0] == "rev-parse" and a[1].startswith("--disambiguate=")),
                             ("cat-file -t", lambda a: a[:2] == ["cat-file", "-t"])):
            def fake(args, cwd, broken=broken, **kw):
                return (128, "fatal: boom") if broken(args) else real(args, cwd, **kw)

            with self.subTest(what), patch.object(git_trees, "git", fake):
                self.assertEqual(git_trees.check_sha(sha, self.clone),
                                 (2, f"sha {sha}: unknown \u2014 git could not read this repository", True))

    def test_a_prefix_that_names_a_blob_is_no_such_commit_not_a_read_fault(self) -> None:
        """`rev-parse --disambiguate` lists objects of every type, and `cat-file -e <blob>^{commit}` exits 128. That is
        git saying it is not a commit: no such commit (exit 1), not `could not read this repository`."""
        blob = git("rev-parse", "HEAD:README.md", cwd=self.clone)
        self.assertEqual(git("cat-file", "-t", blob[:7], cwd=self.clone), "blob")
        self.assertEqual(git_trees.check_sha(blob[:7], self.clone),
                         (1, f"sha {blob[:7]}: no such commit after fetch, so not on origin/main {self.tip()}", False))

    def test_a_tag_named_main_is_not_local_main(self) -> None:
        """#49 round 7, 4: `main` alone names the tag before the branch. The commit is on a feature branch, so the answer
        is the plain `not on origin/main`, never `on local main only`. Green when written: the code reads `refs/heads/main`."""
        tree = self.tree("wt-a", "feat/a")
        commit(tree, "a.txt")
        sha = git("rev-parse", "HEAD", cwd=tree)
        git("tag", "main", sha, cwd=self.clone)
        self.assertEqual(git_trees.check_sha(sha, tree), (1, f"sha {sha}: not on origin/main {self.tip()} (fetched)", True))

    def test_the_fetch_failure_line_still_says_local_main_holds_it(self) -> None:
        """#49 round 7, 5: unknown stays unknown, and the fact the old line dropped is said. A commit on a branch only
        keeps the line as it was."""
        commit(self.clone, "local.txt")
        sha = git("rev-parse", "HEAD", cwd=self.clone)
        branch = self.tree("wt-a", "feat/a")
        commit(branch, "a.txt")
        on_branch = git("rev-parse", "HEAD", cwd=branch)
        git("remote", "set-url", "origin", str(self.root / "gone.git"), cwd=self.clone)
        err = git_trees.fetch_base(self.clone)
        self.assertTrue(err)
        head = f"unknown \u2014 fetch failed: {err}; origin/main {self.tip()} as last fetched does not hold it"
        self.assertEqual(git_trees.check_sha(sha, self.clone), (2, f"sha {sha}: {head}; local main holds it", True))
        self.assertEqual(git_trees.check_sha(on_branch, self.clone), (2, f"sha {on_branch}: {head}", True))

    def test_a_shallow_clone_still_says_local_main_holds_it(self) -> None:
        commit(self.clone, "second.txt")
        git("push", "-q", "origin", "main", cwd=self.clone)
        shallow = self.root / "shallow"
        git("clone", "-q", "--depth", "1", f"file://{self.root / 'origin.git'}", str(shallow), cwd=self.root)
        commit(shallow, "local.txt")
        sha = git("rev-parse", "HEAD", cwd=shallow)
        self.assertEqual(git_trees.check_sha(sha, shallow),
                         (2, f"sha {sha}: unknown \u2014 shallow clone, so history is cut off; local main holds it", True))

    def test_a_grafts_file_written_by_the_fetch_is_seen(self) -> None:
        """#49 round 7, 6: the grafts check ran before the fetch, so a file the fetch left was not seen. `fetch_base` is
        patched to write one and report success."""
        sha = git("rev-parse", "HEAD", cwd=self.clone)
        common = Path(git("rev-parse", "--path-format=absolute", "--git-common-dir", cwd=self.clone))

        def fetch_leaves_grafts(tree, timeout=30):
            (common / "info").mkdir(exist_ok=True)
            (common / "info" / "grafts").write_text(f"{sha} {sha}\n")
            return ""

        with patch.object(git_trees, "fetch_base", fetch_leaves_grafts):
            self.assertEqual(git_trees.check_sha(sha, self.clone),
                             (2, f"sha {sha}: unknown \u2014 this repository has grafts, so ancestry cannot be trusted", True))

    def test_a_repository_the_view_cannot_locate_is_unknown_not_no_grafts(self) -> None:
        """#49 round 8, 1, restated (#443 slice 4): grafts and shallow are read from the repository's real files, found by
        `git_view.locate`, not by a `git rev-parse --git-path` call. A layout `locate` refuses (here a `.git` directory
        with no `HEAD`) is unknown, never "no grafts", with `held` as the commit lookup already said. `_verdict` is called
        directly with the lookup's answer, since the commit lookup itself cannot read such a layout."""
        sha = git("rev-parse", "HEAD", cwd=self.clone)
        broken = self.trees / "broken"
        broken.mkdir()
        git("init", "--initial-branch=main", ".", cwd=broken)
        (broken / ".git" / "HEAD").unlink()
        with self.assertRaises(git_view.Unviewable):
            git_view.locate(broken)
        self.assertEqual(git_trees._verdict(sha, broken, "", [sha]),
                         (2, f"sha {sha}: unknown \u2014 git could not read this repository", True))
        self.assertEqual(git_trees._verdict(sha, broken, "", None),
                         (2, f"sha {sha}: unknown \u2014 git could not read this repository", True))

    def test_a_grafts_file_that_cannot_be_examined_is_unknown_not_no_grafts(self) -> None:
        """#49 round 8, 1, restated (#443 slice 4): `os.stat` (which `Path.stat`, `is_file` and `exists` all go through)
        raises PermissionError, for the grafts path only; every other stat is real. The file exists and is empty, so a reader
        that can examine it finds no grafts (exit 0). `git_view.signals` fails closed on a file it cannot stat, and says
        what it fears: grafts. It used to say `could not read this repository`."""
        sha = git("rev-parse", "HEAD", cwd=self.clone)
        grafts = self.clone / ".git" / "info" / "grafts"
        grafts.parent.mkdir(exist_ok=True)
        grafts.write_text("")
        real = os.stat

        def denied(path, *args, **kw):
            if isinstance(path, (str, os.PathLike)) and os.fspath(path).endswith(os.path.join("info", "grafts")):
                raise PermissionError(13, "Permission denied", os.fspath(path))
            return real(path, *args, **kw)

        self.assertEqual(git_trees.check_sha(sha, self.clone)[0], 0)
        with patch.object(os, "stat", denied):
            self.assertEqual(git_trees.check_sha(sha, self.clone),
                             (2, f"sha {sha}: unknown \u2014 this repository has grafts, so ancestry cannot be trusted", True))

    COULD_NOT_READ = "unknown \u2014 git could not read this repository"

    def assert_could_not_read(self, sha: str, tree: Path) -> None:
        self.assertEqual(git_trees.check_sha(sha, tree), (2, f"sha {sha}: {self.COULD_NOT_READ}", True))

    def assert_answered_by_its_repository(self, sha: str, tree: Path) -> tuple[int, str, bool]:
        """#294 round 7: a pruned worktree is answered by its repository, so `check_sha` on it is exactly `check_sha` on the
        main clone (git run in `<common>`, which still has the objects and both `main` refs). Returns that answer."""
        self.assertEqual(raw_git_dir(tree)[0], 128)  # the tree itself is dead to git
        expected = git_trees.check_sha(sha, self.clone)
        self.assertEqual(git_trees.check_sha(sha, tree), expected)
        return expected

    def test_a_pruned_worktree_of_a_repository_whose_origin_main_holds_the_commit_says_yes(self) -> None:
        """#294 round 7: `git worktree add`, then `<common>/worktrees/<name>` is deleted: the `.git` file names an entry that
        is gone, and `<common>` still holds the commit on origin/main. The repository answers for the tree: exit 0, the
        same tuple as its main clone gives."""
        sha, tree = self.pruned()
        code, line, _ = self.assert_answered_by_its_repository(sha, tree)
        self.assertEqual(code, 0)
        self.assertRegex(line, rf"^sha {sha}: on origin/main [0-9a-f]+ \(fetched\)$")

    def test_a_pruned_worktree_of_a_repository_holding_the_commit_only_on_a_branch_says_no_and_holds_it(self) -> None:
        """#294 round 7, the hole: pruning the worktree entry does not remove the commit from `<common>`. It is there,
        unmerged on a branch, so the pruned tree gives what the clone gives: exit 1, `not on origin/main`, `held` True
        (it blocks another repository's yes)."""
        sha, _, _ = self.live_with_a_commit()  # a commit on `feat/wt-live`, a branch of the repository
        _, tree = self.pruned()
        code, line, held = self.assert_answered_by_its_repository(sha, tree)
        self.assertEqual((code, held), (1, True))
        self.assertRegex(line, rf"^sha {sha}: not on origin/main [0-9a-f]+ \(fetched\)$")

    def test_a_pruned_worktree_of_a_repository_that_never_had_the_commit_says_no_commit_and_holds_nothing(self) -> None:
        """#294 round 7: a commit the repository does not have is `no such commit`, `held` False, exactly as the clone says."""
        nobody = "0123456789abcdef0123456789abcdef01234567"
        _, tree = self.pruned()
        code, line, held = self.assert_answered_by_its_repository(nobody, tree)
        self.assertEqual((code, held), (1, False))
        self.assertRegex(line, rf"^sha {nobody}: no such commit after fetch, so not on origin/main [0-9a-f]+$")

    def test_a_pruned_worktree_whose_repository_git_cannot_use_could_not_be_read_and_may_hold_it(self) -> None:
        """#294 round 7: the rule is "the repository answers", so when git cannot use `<common>` the ordinary `git could not
        read this repository`, `held` True, applies: every `git` call answers `(128, "")` (a timeout, git not on PATH)."""
        sha, tree = self.pruned()
        with patch.object(git_trees, "git", lambda *a, **kw: (128, "")):
            self.assert_could_not_read(sha, tree)

    def test_a_pruned_worktree_whose_common_dir_has_an_invalid_head_could_not_be_read_and_may_hold_it(self) -> None:
        """#294 round 7: `<common>` still holds `objects` and `refs` but its `HEAD` is not a ref or an id, so git rejects it
        as a git directory (`rev-parse` exits 128). Unknown, `held` True: it blocks a yes, it is not `not a repository`."""
        sha, tree = self.pruned()
        (self.clone / ".git" / "HEAD").write_text("this is not a ref\n")
        self.assertEqual(raw_git_dir(self.clone / ".git")[0], 128)
        self.assert_could_not_read(sha, tree)

    def test_a_dot_git_file_pointing_at_a_repository_that_is_gone_could_not_be_read_and_may_hold_it(self) -> None:
        """#294 round 2: `gitdir: /nonexistent/path` (and the next test) has no repository behind it at all, so nothing says
        the repository forgot this tree; it may be the project's, on a volume that is not mounted. It blocks a yes."""
        sha = git("rev-parse", "HEAD", cwd=self.clone)
        broken = self.trees / "broken"
        broken.mkdir()
        (broken / ".git").write_text("gitdir: /nonexistent/path\n")
        self.assertEqual(raw_git_dir(broken)[0], 128)
        self.assert_could_not_read(sha, broken)

    def test_a_linked_worktree_whose_main_clone_was_moved_could_not_be_read_and_may_hold_it(self) -> None:
        """#294 round 2: `<common>/worktrees/<name>` is named by the `.git` file and `<common>` does not exist (the clone was
        renamed, or its volume is gone). The repository is not known to have forgotten the tree: unknown, `held` True."""
        sha = git("rev-parse", "HEAD", cwd=self.clone)
        tree = self.tree("wt-a", "feat/a")
        self.clone.rename(self.root / "repo-moved")
        self.assertEqual(raw_git_dir(tree)[0], 128)
        self.assert_could_not_read(sha, tree)

    def tree_naming(self, name: str, common: Path) -> Path:
        """A directory whose `.git` file says `gitdir: <common>/worktrees/gone`: a pruned worktree of whatever `<common>` is."""
        tree = self.trees / name
        tree.mkdir()
        (tree / ".git").write_text(f"gitdir: {common}/worktrees/gone\n")
        return tree

    def test_a_pruned_worktree_naming_a_plain_dir_inside_another_repository_is_not_answered_by_that_repository(self) -> None:
        """#294 round 8, A: `<common>` is a plain directory inside `outer`'s working tree, so git run there walks UP and
        answers for `outer`. The redirect is answered only when `<common>` is itself the git directory git uses there:
        otherwise `outer`'s yes is another repository's, and `audit --sha` (a gate) must not close a task on it."""
        sha = git("rev-parse", "HEAD", cwd=self.clone)
        outer = self.clone
        self.assertEqual(git_trees.check_sha(sha, outer)[0], 0)  # outer says yes
        sub = outer / "sub"
        (sub / "worktrees").mkdir(parents=True)
        # git run in sub walks up and finds outer (the premise; the view refuses `sub`, which is no repository, so
        # `check_sha(sha, sub)` is no longer the way to show it)
        self.assertEqual(Path(taint.git(["rev-parse", "--show-toplevel"], sub).stdout.strip()).resolve(), outer.resolve())
        tree = self.tree_naming("wt-outer", sub)
        self.assertEqual(raw_git_dir(tree)[0], 128)
        self.assert_could_not_read(sha, tree)

    def test_a_tree_made_by_init_separate_git_dir_whose_git_dir_is_gone_inside_another_repository_is_not_answered_by_it(self) -> None:
        """#294 round 8, A, without a hand-written `.git` file: `git init --separate-git-dir <outer>/sub/worktrees/x <tree>`
        writes it, then the git directory is removed. Same expectation as the hand-written one."""
        sha = git("rev-parse", "HEAD", cwd=self.clone)
        sub = self.clone / "sub"
        (sub / "worktrees").mkdir(parents=True)
        tree, gitdir = self.trees / "wt-separate", sub / "worktrees" / "x"
        tree.mkdir()
        git("init", "--initial-branch=main", f"--separate-git-dir={gitdir}", ".", cwd=tree)
        commit(tree, "a.txt")
        written = (tree / ".git").read_text().strip().removeprefix("gitdir: ")  # git writes the resolved path
        self.assertEqual(Path(written), gitdir.resolve())
        shutil.rmtree(gitdir)
        self.assertEqual(raw_git_dir(tree)[0], 128)
        self.assert_could_not_read(sha, tree)

    def promptly(self, sha: str, tree: Path) -> tuple[int, str, bool]:
        """`check_sha(sha, tree)` in a daemon thread that must finish in 10s. A redirect that follows another is cut at 4
        calls (a hang would otherwise spawn git until the stack runs out), so the failure says what happened."""
        real, calls, out = git_trees.check_sha, [], []

        def counted(sha, tree):
            calls.append(tree)
            if len(calls) > 4:
                raise RecursionError("check_sha called itself more than 4 times")
            return real(sha, tree)

        def run():
            try:
                out.append(git_trees.check_sha(sha, tree))
            except RecursionError as e:
                out.append(e)

        with patch.object(git_trees, "check_sha", counted):
            thread = threading.Thread(target=run, daemon=True)
            thread.start()
            thread.join(timeout=10)
        self.assertFalse(thread.is_alive(), "check_sha did not return: a redirect is followed again and again")
        self.assertNotIsInstance(out[0], RecursionError, f"check_sha recursed through redirects: {out[0]}")
        return out[0]

    def test_a_redirect_is_followed_once_so_two_dirs_naming_each_other_are_not_a_loop(self) -> None:
        """#294 round 8, B: A's `.git` names `<B>/worktrees/gone` and B's names `<A>/worktrees/gone`; neither is a
        repository. The redirected call must not follow another: could not read, `held` True, promptly (it recursed)."""
        sha = git("rev-parse", "HEAD", cwd=self.clone)
        a, b = self.root / "A", self.root / "B"
        a.mkdir()
        b.mkdir()
        (a / ".git").write_text(f"gitdir: {b}/worktrees/gone\n")
        (b / ".git").write_text(f"gitdir: {a}/worktrees/gone\n")
        self.assertEqual(self.promptly(sha, a), (2, f"sha {sha}: {self.COULD_NOT_READ}", True))

    def test_a_yes_is_not_reachable_through_a_chain_of_redirects(self) -> None:
        """#294 round 8, B: A names B, B names C, and C is a real git directory with the commit on origin/main (the
        main clone's `.git`). B is not a repository and the redirect is not followed twice: could not read, `held` True."""
        sha = git("rev-parse", "HEAD", cwd=self.clone)
        a, b = self.root / "A", self.root / "B"
        a.mkdir()
        b.mkdir()
        (a / ".git").write_text(f"gitdir: {b}/worktrees/gone\n")
        (b / ".git").write_text(f"gitdir: {self.clone / '.git'}/worktrees/gone\n")
        self.assertEqual(git_trees.check_sha(sha, self.clone / ".git")[0], 0)  # C says yes
        self.assertEqual(self.promptly(sha, a), (2, f"sha {sha}: {self.COULD_NOT_READ}", True))

    def live_with_a_commit(self, name: str = "wt-live") -> tuple[str, Path, Path]:
        """`(sha, tree, target)`: a real linked worktree holding a commit; `target` is its `<common>/worktrees/<name>`."""
        tree = self.tree(name, f"feat/{name}")
        commit(tree, "a.txt")
        return git("rev-parse", "HEAD", cwd=tree), tree, self.clone / ".git" / "worktrees" / name

    def deny(self, path: Path) -> None:
        os.chmod(path, 0)
        self.addCleanup(os.chmod, path, 0o755)  # runs before the temp dir is removed

    @unittest.skipIf(os.geteuid() == 0, "root can look inside a directory with mode 000")
    def test_a_worktrees_dir_that_cannot_be_looked_into_is_not_a_pruned_worktree(self) -> None:
        """#294 round 3, 1a: `chmod 000 <common>/worktrees`: the target is still there, it just cannot be examined, and
        `Path.exists()` answers False for that. "Cannot look" is not "gone": a live worktree that may hold the commit."""
        sha, tree, target = self.live_with_a_commit()
        self.deny(target.parent)
        self.assertIsNone(git_trees._pruned_common(tree))
        self.assert_could_not_read(sha, tree)

    @unittest.skipIf(os.geteuid() == 0, "root can look inside a directory with mode 000")
    def test_a_common_dir_that_cannot_be_looked_into_is_not_a_pruned_worktree(self) -> None:
        """#294 round 3, 1b: same with `chmod 000 <common>`."""
        sha, tree, target = self.live_with_a_commit()
        self.deny(target.parent.parent)
        self.assertIsNone(git_trees._pruned_common(tree))
        self.assert_could_not_read(sha, tree)

    def test_a_target_that_is_a_dangling_symlink_is_present_not_a_pruned_worktree(self) -> None:
        """#294 round 3, 1c: `<common>/worktrees/<name>` replaced by a symlink to nowhere is present (as a link), so the
        repository is not known to have forgotten the tree."""
        sha, tree, target = self.live_with_a_commit()
        shutil.rmtree(target)
        target.symlink_to(self.root / "nowhere")
        self.assertIsNone(git_trees._pruned_common(tree))
        self.assert_could_not_read(sha, tree)

    @unittest.skipUnless(hasattr(os, "mkfifo"), "no FIFOs on this platform")
    def test_a_dot_git_that_is_not_a_regular_file_is_not_read_and_not_a_pruned_worktree(self) -> None:
        """#294 round 4: `open()` on a FIFO blocks until a writer shows up, so `audit --sha` hung on a tree whose `.git` is
        a FIFO (or a symlink to one). `_pruned_common` reads only a regular file; anything else is not pruned and it returns at
        once. Each call runs in a daemon thread so a hang is a failure here, not a hung suite."""
        sha = git("rev-parse", "HEAD", cwd=self.clone)

        def promptly(fn, *args):
            out = []
            thread = threading.Thread(target=lambda: out.append(fn(*args)), daemon=True)
            thread.start()
            thread.join(timeout=3)
            self.assertFalse(thread.is_alive(), f"{fn.__name__} is blocked reading a non-regular .git")
            return out[0]

        for what, linked in (("a FIFO", False), ("a symlink to a FIFO", True)):
            with self.subTest(what):
                tree = self.trees / f"fifo-{linked}"
                tree.mkdir()
                fifo = self.root / "fifo-target" if linked else tree / ".git"
                os.mkfifo(fifo)
                if linked:
                    (tree / ".git").symlink_to(fifo)

                def release(fifo=fifo):  # a blocked reader gets EOF once a writer opens and closes it
                    try:
                        os.close(os.open(fifo, os.O_WRONLY | os.O_NONBLOCK))
                    except OSError:
                        pass
                self.addCleanup(release)
                self.assertIsNone(promptly(git_trees._pruned_common, tree))
                with patch.object(git_trees, "git", lambda *a, **kw: (128, "")):
                    self.assertEqual(promptly(git_trees.check_sha, sha, tree), (2, f"sha {sha}: {self.COULD_NOT_READ}", True))

    def test_a_dot_git_first_line_without_the_gitdir_prefix_is_not_a_pruned_worktree(self) -> None:
        """#294 round 3, 2: a bare path `<common>/worktrees/<gone>` is not a gitfile (git: `invalid gitfile format`), so it
        names no repository that forgot anything. `<common>` exists, the target does not."""
        sha, tree = self.pruned()
        gone = self.clone / ".git" / "worktrees" / "wt-dead"
        (tree / ".git").write_text(f"{gone}\n")
        self.assertIsNone(git_trees._pruned_common(tree))
        self.assert_could_not_read(sha, tree)

    def test_git_rejects_gitdir_without_a_space_or_after_leading_spaces_so_neither_is_a_pruned_worktree(self) -> None:
        """#294 round 3, 2: real git (2.54) rejects `gitdir:<path>` and `  gitdir: <path>` as a `.git` file (`fatal: invalid
        gitfile format`), checked on a live worktree first. The same text with the target gone is not a pruned worktree."""
        _, tree = self.pruned()
        gone = self.clone / ".git" / "worktrees" / "wt-dead"
        live_tree = self.tree("wt-live", "feat/wt-live")
        live = self.clone / ".git" / "worktrees" / "wt-live"
        for what, line in (("no space", "gitdir:{}"), ("leading spaces", "  gitdir: {}")):
            with self.subTest(what):
                (live_tree / ".git").write_text(line.format(live) + "\n")
                code, out = raw_git_dir(live_tree)
                self.assertEqual(code, 128, out)
                self.assertIn("invalid gitfile format", out)
                (tree / ".git").write_text(line.format(gone) + "\n")
                self.assertIsNone(git_trees._pruned_common(tree))

    def test_a_separate_git_dir_repository_whose_git_dir_is_gone_could_not_be_read_and_may_hold_it(self) -> None:
        """#294 round 5: `git init --separate-git-dir <x>/worktrees/repo <tree>` writes a `.git` file of exactly the
        `gitdir: <x>/worktrees/<name>` form but made no linked worktree: `<x>` is no git directory. When that git
        directory becomes unavailable (a volume gone, a rename) while `<x>` remains, there is no repository to answer
        for the tree: it may hold the commit and blocks a yes (round 7: git rejects `<x>`, so `could not read`)."""
        tree, separate = self.root / "sep-tree", self.root / "x" / "worktrees" / "repo"
        tree.mkdir()
        separate.parent.mkdir(parents=True)  # git wants the parent of the git directory to exist
        git("init", "--initial-branch=main", "--separate-git-dir", str(separate), str(tree), cwd=self.root)
        self.assertEqual((tree / ".git").read_text(), f"gitdir: {separate.resolve()}\n")  # as git wrote it (symlinks resolved)
        commit(tree, "a.txt")
        sha = git("rev-parse", "HEAD", cwd=tree)
        self.assertFalse((self.root / "x" / "HEAD").exists())  # `<x>` is just a directory, not a git directory
        shutil.rmtree(separate)
        self.assertTrue((self.root / "x").is_dir())
        self.assertEqual(raw_git_dir(tree)[0], 128)
        self.assert_could_not_read(sha, tree)

    def test_a_common_dir_that_is_an_empty_directory_could_not_be_read_and_may_hold_it(self) -> None:
        """#294 round 5: `gitdir: <empty dir>/worktrees/gone`: the target is gone and `<common>` is a directory, but it
        holds no `HEAD`, so it is no git directory and git cannot answer for the tree. Unknown, `held` True."""
        sha = git("rev-parse", "HEAD", cwd=self.clone)
        common = self.root / "empty-common"
        common.mkdir()
        tree = self.trees / "hand-made"
        tree.mkdir()
        (tree / ".git").write_text(f"gitdir: {common}/worktrees/gone\n")
        self.assert_could_not_read(sha, tree)

    def test_a_common_dir_holding_only_a_head_file_could_not_be_read_and_may_hold_it(self) -> None:
        """#294 round 6, A: git's `is_git_directory()` wants `HEAD` plus object storage plus ref storage. `<common>` is an
        ordinary directory holding a file named `HEAD` and nothing else: no git directory, so git cannot answer for the tree."""
        sha = git("rev-parse", "HEAD", cwd=self.clone)
        common = self.root / "head-only"
        common.mkdir()
        (common / "HEAD").write_text("ref: refs/heads/main\n")
        tree = self.trees / "hand-made"
        tree.mkdir()
        (tree / ".git").write_text(f"gitdir: {common}/worktrees/gone\n")
        self.assert_could_not_read(sha, tree)

    def lookalike(self, kind: str) -> Path:
        """A plain directory INSIDE the enclosing repository `self.clone` that has the files `git_view.locate` wants but that
        real git does not treat as a repository (it walks up to `self.clone`): an empty `HEAD`, a garbage `HEAD`, or a valid
        `HEAD` with no `objects/`."""
        common = self.clone / "lookalike"
        shutil.rmtree(common, ignore_errors=True)
        (common / "refs").mkdir(parents=True)
        if kind != "no objects":
            (common / "objects").mkdir()
        (common / "HEAD").write_text({"empty HEAD": "", "garbage HEAD": "not a ref\x00\xff\n", "no objects": "ref: refs/heads/main\n"}[kind])
        return common

    LOOKALIKES = ("empty HEAD", "garbage HEAD", "no objects")

    def test_a_lookalike_common_dir_inside_another_repository_is_never_fetched_into(self) -> None:
        """#550 review, security: `fetch_base(<common>)` runs real git with the user's credentials. A worker-written gitfile
        `gitdir: <lookalike>/worktrees/x` must not make a directory that real git does not open (it walks up to the ENCLOSING
        repository) the place the fetch runs. Rule A: no redirect, `fetch_base` only for the pruned tree itself, and the answer
        is the unknown of any unredirected pruned tree."""
        sha = git("rev-parse", "HEAD", cwd=self.clone)
        for kind in self.LOOKALIKES:
            with self.subTest(kind):
                common = self.lookalike(kind)
                # git's own answer: it walks up to the enclosing repository
                self.assertEqual(Path(subprocess.run(["git", "-C", str(common), "rev-parse", "--show-toplevel"], capture_output=True,
                                                     text=True, env=taint.ENV).stdout.strip()).resolve(), self.clone.resolve())
                tree = self.trees / f"hand-made-{kind.replace(' ', '-')}"
                tree.mkdir()
                (tree / ".git").write_text(f"gitdir: {common}/worktrees/x\n")
                self.assertEqual(git_trees._pruned_common(tree), common)
                with patch.object(git_trees, "fetch_base", return_value="") as fetched:
                    answer = git_trees.check_sha(sha, tree)
                self.assertEqual([c.args[0] for c in fetched.call_args_list], [tree])
                self.assertEqual(answer, (2, f"sha {sha}: {self.COULD_NOT_READ}", True))

    def test_a_real_bare_common_dir_still_gets_the_redirected_fetch(self) -> None:
        """The control for the test above: when `<common>` is a real bare repository, rule A still redirects, so `fetch_base`
        runs for the pruned tree and then for `<common>`."""
        sha = git("rev-parse", "HEAD", cwd=self.clone)
        bare = self.root / "common.git"
        git("clone", "--bare", str(self.clone), str(bare), cwd=self.root)
        tree = self.trees / "hand-made"
        tree.mkdir()
        (tree / ".git").write_text(f"gitdir: {bare}/worktrees/x\n")
        with patch.object(git_trees, "fetch_base", return_value="") as fetched:
            git_trees.check_sha(sha, tree)
        self.assertEqual([c.args[0] for c in fetched.call_args_list], [tree, bare])

    def test_is_git_dir_is_true_for_real_git_directories_and_false_for_lookalikes(self) -> None:
        """#550 review: `_is_git_dir` is true for a real bare repository and a real `.git` common dir, false for the three
        lookalikes and for a plain subdirectory of a repository."""
        bare = self.root / "common.git"
        git("clone", "--bare", str(self.clone), str(bare), cwd=self.root)
        self.assertTrue(git_trees._is_git_dir(bare))
        self.assertTrue(git_trees._is_git_dir(self.clone / ".git"))
        (self.clone / "plain").mkdir()
        self.assertFalse(git_trees._is_git_dir(self.clone / "plain"))
        for kind in self.LOOKALIKES:
            with self.subTest(kind):
                common = self.lookalike(kind)
                self.assertFalse(git_trees._is_git_dir(common))

    def test_a_common_dir_missing_its_objects_or_refs_could_not_be_read_and_may_hold_it(self) -> None:
        """#294 round 6, A: a real pruned worktree, then the main clone's `objects` (or `refs`) directory is renamed away:
        `<common>` holds `HEAD` but is not a git directory any more, so git cannot answer for the tree."""
        for part in ("objects", "refs"):
            with self.subTest(part):
                sha, tree = self.pruned(f"wt-no-{part}")
                (self.clone / ".git" / part).rename(self.clone / ".git" / f"{part}-away")
                try:
                    self.assert_could_not_read(sha, tree)
                finally:  # the next subtest needs a working clone
                    (self.clone / ".git" / f"{part}-away").rename(self.clone / ".git" / part)

    def test_two_spaces_after_gitdir_make_a_path_git_never_looks_up_so_it_could_not_be_read(self) -> None:
        """#294 round 6, B: git removes only the terminal CR/LF of the gitfile line; every other byte after `gitdir: ` is the
        path. `gitdir:  <target>` (two spaces) is the path ` <target>`, relative to the tree, which does not exist and has no
        git directory above it. Real git (2.54) fails (exit 128) for it on a live worktree. No repository answers for the
        tree: unknown, `held` True."""
        _, tree = self.pruned()
        gone = self.clone / ".git" / "worktrees" / "wt-dead"
        live_tree = self.tree("wt-live", "feat/wt-live")
        (live_tree / ".git").write_text(f"gitdir:  {self.clone / '.git' / 'worktrees' / 'wt-live'}\n")
        self.assertEqual(raw_git_dir(live_tree)[0], 128)
        (tree / ".git").write_text(f"gitdir:  {gone}\n")
        self.assert_could_not_read(git("rev-parse", "HEAD", cwd=self.clone), tree)

    def test_a_trailing_space_in_a_gitfile_path_is_part_of_the_name_not_trimmed(self) -> None:
        """#294 round 6, B: git removes only the terminal CR/LF of the gitfile line, so `gitdir: <common>/worktrees/<name> `
        names the entry `<name> ` (with the space). That entry exists here (as a directory git cannot use), while `<name>`
        itself is the pruned one: trimming the space would look up the absent `<name>` and call the tree pruned."""
        sha, tree = self.pruned()
        named = self.clone / ".git" / "worktrees" / "wt-dead "
        named.mkdir()
        (tree / ".git").write_text(f"gitdir: {named}\n")
        self.assertEqual(raw_git_dir(tree)[0], 128)  # git cannot open it either
        self.assertIsNone(git_trees._pruned_common(tree))
        self.assert_could_not_read(sha, tree)

    def test_a_crlf_line_ending_on_a_pruned_worktrees_dot_git_still_makes_it_a_pruned_worktree(self) -> None:
        """#294 round 6, B: git removes a terminal CR as well as the LF (real git 2.54 opens a live worktree whose gitfile
        ends `\\r\\n`), so a CRLF-terminated `.git` file of a pruned worktree is still pruned: answered by its repository."""
        sha, tree = self.pruned()
        (tree / ".git").write_bytes(f"gitdir: {self.clone / '.git' / 'worktrees' / 'wt-dead'}\r\n".encode())
        self.assertIsNotNone(git_trees._pruned_common(tree))
        self.assertEqual(git_trees.check_sha(sha, tree), git_trees.check_sha(sha, self.clone))
        self.assertEqual(git_trees.check_sha(sha, tree)[0], 0)

    def test_a_line_break_inside_the_gitfile_path_is_not_a_pruned_worktree(self) -> None:
        """#294 round 10, B: git reads the whole gitfile and strips only the trailing CR/LF run, so `gitdir: <path>\njunk\n`
        names a path with a line break in it, which git rejects (real git 2.54 exits 128 on a LIVE worktree with that file,
        checked first). Reading only the first line would call the pruned tree pruned and import its repository's answer:
        not a pruned worktree, so could not read, `held` True."""
        sha, tree = self.pruned()
        gone = self.clone / ".git" / "worktrees" / "wt-dead"
        live_tree, live = self.tree("wt-live", "feat/wt-live"), self.clone / ".git" / "worktrees" / "wt-live"
        for what, tail in (("a junk line", b"\njunk\n"), ("a junk line with no final newline", b"\njunk"),
                           ("a junk line and a trailing blank", b"\njunk\n\n"), ("a lone CR then junk", b"\rjunk\n")):
            with self.subTest(what):
                (live_tree / ".git").write_bytes(f"gitdir: {live}".encode() + tail)
                self.assertEqual(raw_git_dir(live_tree)[0], 128)  # git rejects it too
                (tree / ".git").write_bytes(f"gitdir: {gone}".encode() + tail)
                self.assertIsNone(git_trees._pruned_common(tree))
                self.assert_could_not_read(sha, tree)

    def test_a_pruned_worktrees_gitfile_may_end_with_any_run_of_cr_lf_or_none(self) -> None:
        """#294 round 10, B: only the trailing CR/LF run is not part of the path, and real git 2.54 opens a LIVE worktree
        whose file ends `\r\n`, `\n\n`, `\r\r\n` or with no newline at all (checked first). A pruned worktree whose `.git` ends any of
        those ways is still pruned: answered by its repository, a yes here."""
        sha, tree = self.pruned()
        gone = self.clone / ".git" / "worktrees" / "wt-dead"
        live_tree, live = self.tree("wt-live", "feat/wt-live"), self.clone / ".git" / "worktrees" / "wt-live"
        for what, tail in (("CRLF", b"\r\n"), ("no newline", b""), ("a blank line", b"\n\n"), ("CR CR LF", b"\r\r\n")):
            with self.subTest(what):
                (live_tree / ".git").write_bytes(f"gitdir: {live}".encode() + tail)
                self.assertEqual(raw_git_dir(live_tree)[0], 0)
                (tree / ".git").write_bytes(f"gitdir: {gone}".encode() + tail)
                self.assertEqual(git_trees._pruned_common(tree).resolve(), (self.clone / ".git").resolve())
                self.assertEqual(git_trees.check_sha(sha, tree), git_trees.check_sha(sha, self.clone))
                self.assertEqual(git_trees.check_sha(sha, tree)[0], 0)

    def test_a_gitfile_longer_than_the_read_bound_with_junk_after_the_padding_is_not_a_pruned_worktree(self) -> None:
        """#294 round 11, 1: git reads the whole gitfile, so `gitdir: <path>` + CR/LF padding past 4096 characters + `junk`
        puts `junk` after the trailing run, in the path, and git rejects it (real git exits 128 on a LIVE worktree with that
        file, checked first). Content the read bound cuts off is never ignored: not a pruned worktree, so could not read."""
        sha, tree = self.pruned()
        gone = self.clone / ".git" / "worktrees" / "wt-dead"
        live_tree, live = self.tree("wt-live", "feat/wt-live"), self.clone / ".git" / "worktrees" / "wt-live"
        padding = b"\r\n" * 3000  # 6000 characters, past any 4096 bound
        (live_tree / ".git").write_bytes(f"gitdir: {live}".encode() + padding + b"junk")
        self.assertEqual(raw_git_dir(live_tree)[0], 128)  # git rejects it too
        (tree / ".git").write_bytes(f"gitdir: {gone}".encode() + padding + b"junk")
        self.assertIsNone(git_trees._pruned_common(tree))
        self.assert_could_not_read(sha, tree)

    @staticmethod
    def padded_past_the_byte_bound(head: str) -> bytes:
        """`head` + CR/LF padding + `junk`, with `junk` starting at byte 4097: the first 4097 bytes are all `head` and padding."""
        head_bytes = head.encode()
        padding = b"\r\n" * ((4097 - len(head_bytes)) // 2) + b"\n" * ((4097 - len(head_bytes)) % 2)
        return head_bytes + padding + b"junk"

    def test_a_gitfile_over_the_byte_bound_with_a_multibyte_name_and_junk_after_the_padding_is_not_a_pruned_worktree(self) -> None:
        """#294 round 12: the gitfile bound is measured in bytes, and nothing past it is ignored. A multibyte character in the
        first 4097 bytes makes them decode to 4096 characters or fewer, so a character count under the bound lets the tail
        (`junk`, after the trailing run, so in the path) go unseen. Real git exits 128 on a LIVE worktree with equivalent
        content (checked first); a pruned worktree with it is not pruned: could not read."""
        sha, tree = self.pruned("gøne")
        gone = self.clone / ".git" / "worktrees" / "gøne"
        live_tree, live = self.tree("lïve", "feat/live"), self.clone / ".git" / "worktrees" / "lïve"
        (live_tree / ".git").write_bytes(self.padded_past_the_byte_bound(f"gitdir: {live}"))
        self.assertEqual(raw_git_dir(live_tree)[0], 128)  # git rejects it too
        data = self.padded_past_the_byte_bound(f"gitdir: {gone}")
        self.assertLessEqual(len(data[:4097].decode(errors="ignore")), 4096)  # a character count sees under the bound
        self.assertGreaterEqual(data.index(b"junk"), 4097)  # and junk lies beyond the bytes read
        (tree / ".git").write_bytes(data)
        self.assertFalse(gone.exists())
        self.assertIsNone(git_trees._pruned_common(tree))
        self.assert_could_not_read(sha, tree)

    def test_a_pruned_worktree_with_a_multibyte_entry_name_and_an_ordinary_gitfile_is_still_pruned(self) -> None:
        """#294 round 12 pin: a multibyte entry name alone changes nothing; the short gitfile git wrote names a gone entry, so
        the tree is pruned and its repository answers for it."""
        sha, tree = self.pruned("gøne")
        self.assertFalse((self.clone / ".git" / "worktrees" / "gøne").exists())
        self.assertEqual(git_trees._pruned_common(tree).resolve(), (self.clone / ".git").resolve())
        self.assertEqual(git_trees.check_sha(sha, tree), git_trees.check_sha(sha, self.clone))
        self.assertEqual(git_trees.check_sha(sha, tree)[0], 0)

    def test_a_pruned_worktrees_gitfile_padded_with_hundreds_of_trailing_newlines_is_still_pruned(self) -> None:
        """#294 round 11, 1 pin: a long trailing run of line breaks well under the read bound is still only the trailing run
        (real git opens a LIVE worktree with that file, checked first), so the tree is pruned: a yes from its repository."""
        sha, tree = self.pruned()
        gone = self.clone / ".git" / "worktrees" / "wt-dead"
        live_tree, live = self.tree("wt-live", "feat/wt-live"), self.clone / ".git" / "worktrees" / "wt-live"
        (live_tree / ".git").write_bytes(f"gitdir: {live}".encode() + b"\n" * 300)
        self.assertEqual(raw_git_dir(live_tree)[0], 0)
        (tree / ".git").write_bytes(f"gitdir: {gone}".encode() + b"\n" * 300)
        self.assertEqual(git_trees._pruned_common(tree).resolve(), (self.clone / ".git").resolve())
        self.assertEqual(git_trees.check_sha(sha, tree), git_trees.check_sha(sha, self.clone))
        self.assertEqual(git_trees.check_sha(sha, tree)[0], 0)

    @unittest.skipUnless(hasattr(os, "mkfifo"), "no FIFOs on this platform")
    def test_whether_dot_git_is_a_regular_file_is_decided_on_the_opened_file_not_by_a_check_before_the_open(self) -> None:
        """#294 round 11, 2: `is_file()` before `open()` leaves a window for a FIFO swapped in between, and `open()` on a
        FIFO blocks forever. Not raced: the pre-open check is made to lie (`Path.is_file` says True), so only a decision on
        the opened file can see a FIFO (or a symlink to one) and return None at once. A daemon thread, so a hang fails here."""
        for what, linked in (("a FIFO", False), ("a symlink to a FIFO", True)):
            with self.subTest(what):
                tree = self.trees / f"swapped-{linked}"
                tree.mkdir()
                fifo = self.root / "swapped-target" if linked else tree / ".git"
                os.mkfifo(fifo)
                if linked:
                    (tree / ".git").symlink_to(fifo)

                def release(fifo=fifo):  # a blocked reader gets EOF once a writer opens and closes it
                    try:
                        os.close(os.open(fifo, os.O_WRONLY | os.O_NONBLOCK))
                    except OSError:
                        pass
                self.addCleanup(release)
                out = []
                with patch.object(git_trees.Path, "is_file", lambda self: True):
                    thread = threading.Thread(target=lambda: out.append(git_trees._pruned_common(tree)), daemon=True)
                    thread.start()
                    thread.join(timeout=3)
                    alive = thread.is_alive()
                    release()
                    thread.join(timeout=3)
                self.assertFalse(alive, "_pruned_common is blocked opening a FIFO its pre-open check called a file")
                self.assertEqual(out, [None])

    def test_the_repository_of_a_pruned_worktree_is_the_grandparent_of_its_gitfile_target_so_it_still_answers(self) -> None:
        """#294 round 9, pin: `<common>` is `<common>/worktrees/<name>`'s GRANDPARENT. A real `git worktree add`, then the
        clone's whole `<common>/worktrees` directory (not just the entry) is removed: the repository is still the clone's
        `.git`, and answers for the tree exactly as the clone does (a yes)."""
        sha = git("rev-parse", "HEAD", cwd=self.clone)
        tree = self.tree("wt-gone", "feat/gone")
        shutil.rmtree(self.clone / ".git" / "worktrees")
        self.assertEqual(git_trees._pruned_common(tree).resolve(), (self.clone / ".git").resolve())
        self.assertEqual(git_trees.check_sha(sha, tree), git_trees.check_sha(sha, self.clone))
        self.assertEqual(git_trees.check_sha(sha, tree)[0], 0)

    def test_a_gitfile_target_that_is_not_a_worktrees_entry_is_not_pruned_and_imports_no_yes(self) -> None:
        """#294 round 9, pin: only a `<common>/worktrees/<name>` entry is a pruned worktree. `gitdir: <clone>/.git/modules/gone`
        (a submodule-style git directory that is gone) names the clone's `.git` as its grandparent, which holds the commit on
        origin/main; a yes must not be imported from it: could not read, `held` True."""
        sha = git("rev-parse", "HEAD", cwd=self.clone)
        self.assertEqual(git_trees.check_sha(sha, self.clone)[0], 0)
        (self.clone / ".git" / "modules").mkdir()
        tree = self.trees / "modules-gone"
        tree.mkdir()
        (tree / ".git").write_text(f"gitdir: {self.clone / '.git' / 'modules' / 'gone'}\n")
        self.assertIsNone(git_trees._pruned_common(tree))
        self.assertEqual(git_trees.check_sha(sha, tree), (2, f"sha {sha}: unknown \u2014 git could not read this repository", True))

    def test_git_refusing_or_not_running_on_a_healthy_repository_could_not_read_it_and_it_may_hold_it(self) -> None:
        """#294 round 2: the repository is real and healthy, but every `git` call fails: `fatal: detected dubious
        ownership` (git refuses a directory another user owns), or `(128, "")` (a timeout, git not on PATH). That is not
        evidence the repository is not one, so `held` stays True."""
        sha = git("rev-parse", "HEAD", cwd=self.clone)
        tree = self.tree("wt-a", "feat/a")
        dubious = f"fatal: detected dubious ownership in repository at '{tree}'"
        for what, answer in (("dubious ownership", (128, dubious)), ("git timed out or did not run", (128, ""))):
            with self.subTest(what), patch.object(git_trees, "git", lambda *a, answer=answer, **kw: answer):
                self.assert_could_not_read(sha, tree)
                self.assert_could_not_read(sha, self.clone)

    def test_a_dot_git_directory_git_cannot_use_could_not_be_read_and_may_hold_it(self) -> None:
        """#294 round 2: a `.git` DIRECTORY with `HEAD` removed, in a temp dir inside no other repository. Observed: git
        walks up, finds none, and fails `rev-parse --git-dir` (exit 128). The directory is there, so git could not read
        it: `could not read`, `held` True."""
        sha = git("rev-parse", "HEAD", cwd=self.clone)
        broken = self.trees / "broken"
        broken.mkdir()
        git("init", "--initial-branch=main", ".", cwd=broken)
        (broken / ".git" / "HEAD").unlink()
        code, out = raw_git_dir(broken)
        self.assertEqual(code, 128, out)
        self.assert_could_not_read(sha, broken)

    def test_a_repository_git_can_open_but_not_read_may_hold_it(self) -> None:
        """#294 (was #49 round 10, 2): git finds the git directory (`rev-parse --git-dir` is real and succeeds) but the commit
        lookup (`rev-parse --disambiguate`, or `cat-file -t` on an id it listed) faults. That is a repository that may hold
        the commit: `git could not read this repository`, `held` True, so its unknown blocks a yes. Only the named call
        fails; the fake must not also fail `--git-dir`, or this would describe a repository that is not one."""
        sha = git("rev-parse", "HEAD", cwd=self.clone)
        tree = self.tree("wt-a", "feat/a")
        real = git_trees.git
        self.assertEqual(raw_git_dir(tree)[0], 0)
        for what, broken in (("disambiguate", lambda a: a[0] == "rev-parse" and a[1].startswith("--disambiguate=")),
                             ("cat-file -t", lambda a: a[:2] == ["cat-file", "-t"])):
            def fake(args, cwd, *rest, broken=broken, **kw):
                return (128, "fatal: boom") if broken(args) else real(args, cwd, *rest, **kw)

            with self.subTest(what), patch.object(git_trees, "git", fake):
                self.assertEqual(git_trees.check_sha(sha, tree),
                                 (2, f"sha {sha}: unknown \u2014 git could not read this repository", True))

    def test_origin_main_missing_though_the_fetch_succeeded_is_unknown(self) -> None:
        sha = git("rev-parse", "HEAD", cwd=self.clone)
        git("update-ref", "-d", "refs/remotes/origin/main", cwd=self.clone)
        with patch.object(git_trees, "fetch_base", return_value=""):  # `sha` is on local main, so the line says so (round 9, 1)
            self.assertEqual(git_trees.check_sha(sha, self.clone),
                             (2, f"sha {sha}: unknown \u2014 no origin/main to check against; local main holds it", True))

    def test_no_origin_main_still_says_local_main_holds_it(self) -> None:
        """#49 round 9, 1: a clone whose `origin` was removed has no `refs/remotes/origin/main`. A commit on local main
        gets the `; local main holds it` suffix on the unknown line; a commit on a branch only, and one nobody has, keep
        the line without it."""
        on_main = git("rev-parse", "HEAD", cwd=self.clone)
        branch = self.tree("wt-a", "feat/a")
        commit(branch, "a.txt")
        on_branch = git("rev-parse", "HEAD", cwd=branch)
        nobody = "0123456789abcdef0123456789abcdef01234567"
        git("remote", "remove", "origin", cwd=self.clone)
        self.assertEqual(git("for-each-ref", "refs/remotes", cwd=self.clone), "")
        line = "unknown \u2014 no origin/main to check against"
        for what, sha, suffix, held in (("on local main", on_main, "; local main holds it", True),
                                        ("on a branch only", on_branch, "", True),
                                        ("nobody has it", nobody, "", False)):
            with self.subTest(what):
                self.assertEqual(git_trees.check_sha(sha, self.clone), (2, f"sha {sha}: {line}{suffix}", held))

    def test_a_single_call_that_fails_is_unknown_never_a_plain_no(self) -> None:
        """#49 round 9, 2; round 10, 1: only the named git call returns `(128, "")`; every other call is real. The commit is
        on a branch and not on origin/main, so a plain answer would be exit 1 `not on origin/main`. This tree has a local
        main, so the ref-existence lookup (`rev-parse --verify -q refs/heads/main`) says so (exit 0) and a failing
        merge-base is a real fault; when that lookup itself returns 128 (a missing ref is exit 1, a fault is 128) it is
        a fault too."""
        tree = self.tree("wt-a", "feat/a")
        commit(tree, "a.txt")
        sha = git("rev-parse", "HEAD", cwd=tree)
        real = git_trees.git
        is_lookup = lambda args: args[:1] == ["rev-parse"] and "--verify" in args and "refs/heads/main" in args
        self.assertEqual(real(["rev-parse", "--verify", "-q", "refs/heads/main"], tree)[0], 0)
        fails = {
            "the local main ancestry check": lambda args: args[:2] == ["merge-base", "--is-ancestor"] and args[-1] == "refs/heads/main",
            "the local main ref lookup": is_lookup,
        }
        for what, named in fails.items():
            def fake(args, cwd, *rest, **kw):
                return (128, "") if named(args) else real(args, cwd, *rest, **kw)

            with self.subTest(what), patch.object(git_trees, "git", fake):
                self.assertEqual(git_trees.check_sha(sha, tree),
                                 (2, f"sha {sha}: unknown \u2014 git could not read this repository", True))

    def test_origin_main_holding_it_needs_no_local_main_fact(self) -> None:
        """A commit origin/main holds (the fetch worked) is a yes even when the local main lookup, or the ancestry check
        against local main, faults: local main only feeds the note and the `local main only` wording."""
        sha = git("rev-parse", "HEAD", cwd=self.clone)
        real = git_trees.git
        faults = {
            "the local main ref lookup": lambda a: a[:1] == ["rev-parse"] and "--verify" in a and "refs/heads/main" in a,
            "the local main ancestry check": lambda a: a[:2] == ["merge-base", "--is-ancestor"] and a[-1] == "refs/heads/main",
        }
        for what, named in faults.items():
            hits = []

            def fake(args, cwd, *rest, named=named, hits=hits, **kw):
                if named(args):
                    hits.append(args)
                    return 128, ""
                return real(args, cwd, *rest, **kw)

            with self.subTest(what), patch.object(git_trees, "git", fake):
                self.assertEqual(git_trees.check_sha(sha, self.clone),
                                 (0, f"sha {sha}: on origin/main {self.tip()} (fetched)", True))
            self.assertTrue(hits, f"{what} was never reached")

    def test_a_tag_named_main_is_not_local_main_in_a_clone_with_no_local_main(self) -> None:
        """The sibling of `test_a_tag_named_main_is_not_local_main`, whose repository has a local main branch. Here there
        is no `refs/heads/main` at all, only a tag `main` on the unmerged branch commit: plain `not on origin/main`."""
        sha = self.pushed_branch_commit()
        for what, clone in self.clones_without_local_main().items():
            git("tag", "main", sha, cwd=clone)
            self.assertEqual(git("for-each-ref", "refs/heads/main", cwd=clone), "")
            with self.subTest(what):
                tip = git("rev-parse", "--short", "refs/remotes/origin/main", cwd=clone)
                self.assertEqual(git_trees.check_sha(sha, clone), (1, f"sha {sha}: not on origin/main {tip} (fetched)", True))

    def pushed_branch_commit(self) -> str:
        tree = self.tree("wt-a", "feat/a")
        commit(tree, "a.txt")
        git("push", "-q", "origin", "feat/a", cwd=tree)
        return git("rev-parse", "HEAD", cwd=tree)

    def clones_without_local_main(self) -> dict[str, Path]:
        """Two real clones of origin with no `refs/heads/main`: `clone -b feat/a`, and a clone whose main was deleted."""
        origin = f"file://{self.root / 'origin.git'}"
        only_branch = self.root / "only-branch"
        git("clone", "-q", "-b", "feat/a", origin, str(only_branch), cwd=self.root)
        deleted = self.root / "deleted"
        git("clone", "-q", origin, str(deleted), cwd=self.root)
        git("checkout", "-q", "feat/a", cwd=deleted)
        git("branch", "-D", "main", cwd=deleted)
        clones = {"clone -b feat/a": only_branch, "after branch -D main": deleted}
        for clone in clones.values():
            self.assertEqual(git("for-each-ref", "refs/heads/main", cwd=clone), "")
        return clones

    def test_a_clone_with_no_local_main_says_no_not_unknown(self) -> None:
        """#49 round 10, 1: `git clone -b feat <origin>` has no `refs/heads/main`, and `merge-base ... refs/heads/main`
        exits 128 on the missing ref. A pushed branch commit that is not on origin/main is the plain no, with no local-main
        suffix, whether the clone never had a main or had it deleted. Real repositories."""
        sha = self.pushed_branch_commit()
        for what, clone in self.clones_without_local_main().items():
            with self.subTest(what):
                tip = git("rev-parse", "--short", "refs/remotes/origin/main", cwd=clone)
                self.assertEqual(git_trees.check_sha(sha, clone), (1, f"sha {sha}: not on origin/main {tip} (fetched)", True))

    def test_a_clone_with_no_local_main_still_gives_a_yes_for_what_origin_main_holds(self) -> None:
        sha = git("rev-parse", "HEAD", cwd=self.clone)
        self.pushed_branch_commit()
        for what, clone in self.clones_without_local_main().items():
            with self.subTest(what):
                tip = git("rev-parse", "--short", "refs/remotes/origin/main", cwd=clone)
                self.assertEqual(git_trees.check_sha(sha, clone), (0, f"sha {sha}: on origin/main {tip} (fetched)", True))

    def test_a_failed_fetch_in_a_clone_with_no_local_main_keeps_the_fetch_reason(self) -> None:
        """#49 round 10, 1: the line is the fetch-failed one, with no `; local main holds it` suffix (there is no local
        main), not `git could not read this repository`."""
        sha = self.pushed_branch_commit()
        for what, clone in self.clones_without_local_main().items():
            with self.subTest(what):
                git("remote", "set-url", "origin", str(self.root / "gone.git"), cwd=clone)
                err = git_trees.fetch_base(clone)
                self.assertTrue(err)
                tip = git("rev-parse", "--short", "refs/remotes/origin/main", cwd=clone)
                self.assertEqual(git_trees.check_sha(sha, clone),
                                 (2, f"sha {sha}: unknown \u2014 fetch failed: {err}; origin/main {tip} as last fetched does not hold it", True))


class ShaTreesTest(Repo):
    """#294 round 10, A: `sha_trees` lists one tree per repository. A pruned worktree is a tree of the repository its `.git`
    file names (`<common>`, resolved), so it is keyed by that, exactly as a healthy worktree is keyed by its common git dir:
    the first tree by name is the one listed, whichever kind it is."""

    def listed(self) -> list[str]:
        return [name for name, _ in git_trees.sha_trees(self.root, "trees/")]

    def other_repository_pruned(self, name: str) -> None:
        """A second, unrelated repository with a pruned worktree `name` under the worktrees directory."""
        other = self.root / "other"
        other.mkdir(exist_ok=True)
        if not (other / ".git").exists():
            git("init", "--initial-branch=main", ".", cwd=other)
            commit(other, "o.txt")
        git("worktree", "add", "-b", f"feat/{name}", str(self.trees / name), "main", cwd=other)
        shutil.rmtree(other / ".git" / "worktrees" / name)

    def test_pruned_worktrees_that_sort_first_are_one_entry_and_the_healthy_tree_is_not_listed(self) -> None:
        self.pruned("aaa-dead")
        self.pruned("bbb-dead")
        self.tree("zzz-live", "feat/zzz-live")
        self.assertEqual(git_trees.sha_trees(self.root, "trees/"), [("aaa-dead", (self.trees / "aaa-dead").resolve())])

    def test_a_healthy_tree_that_sorts_first_is_the_one_entry_for_its_repository_and_its_pruned_worktrees(self) -> None:
        self.tree("aaa-live", "feat/aaa-live")
        self.pruned("bbb-dead")
        self.pruned("ccc-dead")
        self.assertEqual(git_trees.sha_trees(self.root, "trees/"), [("aaa-live", (self.trees / "aaa-live").resolve())])

    def test_pruned_worktrees_of_one_repository_and_no_healthy_tree_are_one_entry(self) -> None:
        self.pruned("aaa-dead")
        self.pruned("bbb-dead")
        self.assertEqual(self.listed(), ["aaa-dead"])

    def test_pruned_worktrees_of_two_repositories_are_two_entries(self) -> None:
        self.pruned("aaa-dead")
        self.other_repository_pruned("bbb-dead")
        self.assertEqual(self.listed(), ["aaa-dead", "bbb-dead"])

    def test_a_pruned_worktree_is_keyed_by_its_resolved_repository(self) -> None:
        """The gitfile names `<common>` through a symlink; the healthy tree's common git dir is the real path. Same repository."""
        self.pruned("aaa-dead")
        link = self.root / "link-to-git"
        link.symlink_to(self.clone / ".git")
        (self.trees / "aaa-dead" / ".git").write_text(f"gitdir: {link}/worktrees/aaa-dead\n")
        self.tree("zzz-live", "feat/zzz-live")
        self.assertEqual(self.listed(), ["aaa-dead"])

    def test_a_tree_git_cannot_read_that_is_not_pruned_is_its_own_entry(self) -> None:
        """`gitdir: /nonexistent/path` names no `<common>/worktrees/<name>`: nothing says a repository forgot it, so each
        such tree stays its own repository (unchanged), beside the healthy one."""
        self.tree("aaa-live", "feat/aaa-live")
        for name in ("bbb-broken", "ccc-broken"):
            (self.trees / name).mkdir()
            (self.trees / name / ".git").write_text("gitdir: /nonexistent/path\n")
            self.assertIsNone(git_trees._pruned_common(self.trees / name))
        self.assertEqual(self.listed(), ["aaa-live", "bbb-broken", "ccc-broken"])


    def test_sha_trees_asks_git_nothing(self) -> None:
        """#443 slice 4: the repository of a tree is found by `git_view.locate` (files only), the pruned fallback by the
        `.git` file, so listing the trees starts no git process and never calls `git()`. Before, one `rev-parse` per tree."""
        self.tree("aaa-live", "feat/aaa-live")
        self.tree("bbb-live", "feat/bbb-live")
        self.pruned("ccc-dead")
        self.other_repository_pruned("ddd-dead")
        with patch.object(git_trees, "git", side_effect=AssertionError("git() was called")), git_calls() as calls:
            listed = self.listed()
        self.assertEqual(listed, ["aaa-live", "ddd-dead"])
        self.assertEqual(calls, [])

    def test_two_linked_trees_of_one_clone_are_one_repository_keyed_on_locate(self) -> None:
        a, b = self.tree("aaa-live", "feat/aaa-live"), self.tree("bbb-live", "feat/bbb-live")
        self.assertEqual(git_view.locate(a).common, git_view.locate(b).common)
        self.assertEqual(git_trees.sha_trees(self.root, "trees/"), [("aaa-live", a.resolve())])

    def test_a_redirected_commondir_does_not_make_a_tree_share_a_victims_key(self) -> None:
        """`<common>/worktrees/<name>/commondir` is worker-writable. Pointed at another repository's git directory it made
        `git rev-parse --git-common-dir` (which believes it) give that repository's key, so the victim's own tree, sorting
        after, was dropped as a duplicate and never asked. `locate` refuses the forged layout, and a tree it refuses is its
        own repository (its `.git` target is present, so it is not a pruned worktree either)."""
        victim = self.root / "victim"
        victim.mkdir()
        git("init", "--initial-branch=main", ".", cwd=victim)
        commit(victim, "v.txt")
        git("worktree", "add", "-b", "feat/zzz", str(self.trees / "zzz-victim-live"), "main", cwd=victim)
        forged = self.tree("aaa-forged", "feat/aaa-forged")
        (self.clone / ".git" / "worktrees" / "aaa-forged" / "commondir").write_text(f"{victim / '.git'}\n")
        common = taint.git(["rev-parse", "--path-format=absolute", "--git-common-dir"], forged).stdout.strip()
        self.assertEqual(Path(common).resolve(), (victim / ".git").resolve(), "the premise: git follows the forged commondir")
        with self.assertRaises(git_view.Unviewable):
            git_view.locate(forged)
        self.assertEqual(self.listed(), ["aaa-forged", "zzz-victim-live"])


class AuditEnvTest(Repo):
    """The audit calls read no home, global or system config, and take nothing from the launcher's environment (#442)."""

    def _spied_env(self) -> dict[str, str]:
        """The `env` handed to the view: `git()` no longer starts git itself (#443), `git_view.run` does."""
        done = subprocess.CompletedProcess([], 0, stdout="", stderr="")
        with patch.object(git_view, "run", return_value=done) as run:
            git_trees.git(["status", "--porcelain"], self.clone)
        run.assert_called_once()
        return run.call_args.kwargs["env"]

    def _child_envs(self, **leaks: str) -> list[dict[str, str]]:
        """The environment of every git process one real `git()` read starts, the view's own included."""
        real, envs = subprocess.run, []

        def spy(argv, *args, **kwargs):
            if argv[0] == "git":
                envs.append(kwargs.get("env"))
            return real(argv, *args, **kwargs)

        with patch.dict(os.environ, leaks), patch.object(git_view.subprocess, "run", side_effect=spy):
            self.assertEqual(git_trees.git(["status", "--porcelain"], self.clone)[0], 0)
        return envs

    def test_the_audit_call_names_no_home_global_or_system_config(self) -> None:
        env = self._spied_env()
        self.assertEqual(git_trees.SAFE_HOME, "/var/empty")
        self.assertEqual(env["HOME"], git_trees.SAFE_HOME)
        self.assertNotEqual(env["HOME"], str(self.clone))
        self.assertEqual((env.get("GIT_CONFIG_GLOBAL"), env.get("GIT_CONFIG_SYSTEM")), ("/dev/null", "/dev/null"))
        self.assertEqual(env.get("GIT_CONFIG_NOSYSTEM"), "1")
        self.assertIn(env.get("XDG_CONFIG_HOME"), (None, "/dev/null"))

    def test_the_audit_env_is_built_from_scratch(self) -> None:
        leaks = {"GIT_CONFIG_PARAMETERS": "'core.fsmonitor=x'", "GIT_CONFIG_COUNT": "1", "GIT_DIR": "/elsewhere",
                 "AUDIT_ENV_MARKER": "m"}
        with patch.dict(os.environ, leaks):
            env = self._spied_env()
        self.assertEqual(set(env), {"GIT_TERMINAL_PROMPT", "GIT_OPTIONAL_LOCKS", "GIT_NO_REPLACE_OBJECTS", "PATH", "HOME",
                                    "GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM", "GIT_CONFIG_NOSYSTEM"})
        self.assertEqual(env, git_trees.audit_env())

    def test_the_audit_call_keeps_the_rest_of_its_environment(self) -> None:
        env = self._spied_env()
        self.assertEqual(env["GIT_NO_REPLACE_OBJECTS"], "1")
        self.assertEqual((env["GIT_TERMINAL_PROMPT"], env["GIT_OPTIONAL_LOCKS"]), ("0", "0"))
        self.assertEqual(env["PATH"], "/usr/bin:/bin:/usr/local/bin")

    def test_every_git_process_of_a_read_has_the_scrubbed_environment(self) -> None:
        """The view adds only the three variables that name its own repository; nothing of the launcher's leaks in."""
        envs = self._child_envs(GIT_CONFIG_PARAMETERS="'core.fsmonitor=x'", GIT_CONFIG_COUNT="1", AUDIT_ENV_MARKER="m",
                                GIT_EXEC_PATH="/elsewhere", GIT_SSH_COMMAND="ssh -i key")
        self.assertGreaterEqual(len(envs), 2, "the view's own git calls are among those spied")
        own = {"GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"}
        for env in envs:
            self.assertIsNotNone(env)
            self.assertLessEqual(set(env), set(git_trees.audit_env()) | own)
            self.assertEqual({k: v for k, v in env.items() if k not in own},
                             {k: v for k, v in git_trees.audit_env().items() if k in env})
            self.assertEqual((env["HOME"], env["GIT_CONFIG_GLOBAL"], env["GIT_CONFIG_SYSTEM"], env["GIT_CONFIG_NOSYSTEM"]),
                             (git_trees.SAFE_HOME, "/dev/null", "/dev/null", "1"))

    def test_audit_env_is_the_one_definition(self) -> None:
        self.assertEqual(git_trees.audit_env(), {
            "GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0", "GIT_NO_REPLACE_OBJECTS": "1",
            "PATH": "/usr/bin:/bin:/usr/local/bin", "HOME": git_trees.SAFE_HOME,
            "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"})

    def test_audit_env_is_a_fresh_dict_that_ignores_the_launchers_environment(self) -> None:
        git_trees.audit_env()["HOME"] = "changed"
        self.assertEqual(git_trees.audit_env()["HOME"], git_trees.SAFE_HOME)
        with patch.dict(os.environ, {"GIT_CONFIG_PARAMETERS": "'core.fsmonitor=x'", "GIT_DIR": "/elsewhere"}):
            env = git_trees.audit_env()
        self.assertNotIn("GIT_CONFIG_PARAMETERS", env)
        self.assertNotIn("GIT_DIR", env)

    def test_a_global_config_the_worker_wrote_does_not_run(self) -> None:
        tree, marker = self.tree("wt", "feat/wt"), self.root / "marker"
        hook = f"{sys.executable} -c \\\"import pathlib; pathlib.Path('{marker}').touch()\\\""
        (tree / ".gitconfig").write_text(f'[core]\n\tfsmonitor = "{hook}"\n')
        control = {"PATH": ENV["PATH"], "HOME": str(tree)}  # what the audit used to run with
        subprocess.run(["git", "-C", str(tree), "status"], env=control, capture_output=True, check=False)
        self.assertTrue(marker.exists(), "the fixture must fire under the old environment, or the test proves nothing")
        marker.unlink()
        git_trees.read_state(tree)
        self.assertFalse(marker.exists())


class GitViewFunnelTest(unittest.TestCase):
    """#443 slice 3: `git()` is `git_view.run` and nothing else, so every read in `read_state` gets the sanitised view.

    A worker writes the tree's own `.git`, so its config, hooks, aliases and `.git` file are data, never a program to run.
    Each red case has a control in the same test: the former call shape, run on the same fixture, proves the canary bites.
    """

    # vector -> the marker its former read fires (`taint.apply` plants the vector; the rest are planted below).
    FIRES = {"fsmonitor": "fsmonitor", "filter": "filter-clean", "include": "include-fsmonitor",
             "wtconfig": "wtconfig-fsmonitor", "includeif": "includeif-fsmonitor", "hooks": "hook-post-index-change",
             "hooks_in_common": "hook-in-common"}

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)

    def fixture(self, vector: str | None = None) -> taint.Fx:
        """Common repo + linked tree `wt` on `feat` (3 not on origin/main, 1 unpushed, 3 dirty paths), with one vector planted."""
        fx = taint.build(Path(tempfile.mkdtemp(prefix="fx-", dir=self.root)))
        if vector == "includeif":  # an include behind a condition that matches the tree's branch
            included = fx.root / "conditional.cfg"
            taint.config(fx, "core.fsmonitor", fx.command("includeif-fsmonitor", 1), file=included)
            taint.config(fx, "includeIf.onbranch:feat.path", str(included))
        elif vector == "alias":
            taint.config(fx, "alias.st", "!" + fx.command("alias"))
        elif vector is not None:
            taint.apply(fx, vector)
        return fx

    def bites(self, fx: taint.Fx, marker: str, vector: str) -> None:
        """Control: the former read of this tree (`git -C <tree> status` under the audit environment) runs the canary."""
        env = git_trees.audit_env()
        if vector.startswith("hooks"):  # audit_env's GIT_OPTIONAL_LOCKS=0 already keeps index-refresh hooks quiet; a caller without it is bitten
            del env["GIT_OPTIONAL_LOCKS"]
        fx.clear()
        args = ["st"] if vector == "alias" else ["status", "--porcelain"]
        subprocess.run(["git", "-C", str(fx.tree), *args], env=env, capture_output=True, timeout=15, check=False)
        self.assertIn(marker, fx.fired(), "the fixture must fire under the former read, or the test proves nothing")
        fx.clear()
        for name in ("README.md", "b.txt", "c.txt", "d.txt"):  # the control may have refreshed the real index: stale it again
            os.utime(fx.tree / name, (1_900_000_000, 1_900_000_000))

    def test_the_call_is_git_view_run_with_the_audit_env_and_the_git_timeout(self) -> None:
        """The whole change: same name, signature and return shape; the body is one call into the view."""
        tree = self.root / "any"
        done = subprocess.CompletedProcess([], 0, stdout=" out\n", stderr="ignored")
        with patch.object(git_view, "run", return_value=done) as run, \
                patch.object(subprocess, "run", side_effect=AssertionError("git() started git itself")):
            self.assertEqual(git_trees.git(["status", "--porcelain"], tree), (0, "out"))
        self.assertEqual(run.call_args.args, (["status", "--porcelain"], tree))
        self.assertEqual(run.call_args.kwargs, {"env": git_trees.audit_env(), "timeout": git_trees.GIT_TIMEOUT})

    def test_stderr_is_the_text_when_stdout_is_empty_and_the_exit_code_is_git_s(self) -> None:
        failed = subprocess.CompletedProcess([], 1, stdout="", stderr=" fatal: no\n")
        with patch.object(git_view, "run", return_value=failed):
            self.assertEqual(git_trees.git(["merge-base", "--is-ancestor", "a", "b"], self.root), (1, "fatal: no"))

    def test_the_timeout_is_read_when_called(self) -> None:
        done = subprocess.CompletedProcess([], 0, stdout="", stderr="")
        with patch.object(git_view, "run", return_value=done) as run, patch.object(git_trees, "GIT_TIMEOUT", 3):
            git_trees.git(["rev-parse", "HEAD"], self.root)
        run.assert_called_once()
        self.assertEqual(run.call_args.kwargs["timeout"], 3)

    def test_an_unviewable_tree_is_128_with_the_reason_after_unreadable(self) -> None:
        with patch.object(git_view, "run", side_effect=git_view.Unviewable("metadata is not a bounded regular file")):
            self.assertEqual(git_trees.git(["status", "--porcelain"], self.root),
                             (128, "unreadable: metadata is not a bounded regular file"))

    def test_os_and_timeout_failures_keep_their_128_and_their_text(self) -> None:
        for exc in (OSError("no git"), PermissionError("cache is read-only"), subprocess.TimeoutExpired(["git"], 15)):
            with self.subTest(type(exc).__name__), patch.object(git_view, "run", side_effect=exc):
                result = git_trees.git(["rev-parse", "HEAD"], self.root)
                self.assertIsInstance(result, git_trees.ReadFailure)
                code, text = result
                self.assertEqual((code, text), (128, f"{type(exc).__name__}: {exc}"))

    def test_a_hung_git_inside_the_view_is_bounded_by_git_timeout(self) -> None:
        """The whole view build plus the read shares one budget, and a hang there is a 128, not a stall."""
        fx = self.fixture()
        seen = []

        def hang(argv, **kwargs):
            seen.append((argv, kwargs.get("timeout")))
            raise subprocess.TimeoutExpired(argv, kwargs["timeout"])

        with patch.object(git_trees, "GIT_TIMEOUT", 2), patch.object(git_view.subprocess, "run", side_effect=hang):
            code, text = git_trees.git(["status", "--porcelain"], fx.tree)
        self.assertEqual(code, 128)
        self.assertIn("TimeoutExpired", text)
        self.assertEqual(seen[0][0][:3], ["git", "config", "--file"], "the first git process is the view's own")
        self.assertTrue(all(0 < timeout <= 2 for _, timeout in seen), seen)

    def test_git_does_not_start_a_process_itself(self) -> None:
        source = inspect.getsource(git_trees.git)
        self.assertIn("git_view.run(", source)
        self.assertNotIn("subprocess.run", source)
        self.assertNotIn('"-C"', source)

    def test_a_clean_tree_reads_through_the_view_as_before(self) -> None:
        """Control: nothing is planted, the answers are git's own and no view is left behind."""
        fx = self.fixture()
        views = sorted(_base.iterdir())
        self.assertEqual(git_trees.git(["rev-parse", "--abbrev-ref", "HEAD"], fx.tree), (0, "feat"))
        self.assertEqual(git_trees.git(["status", "--porcelain"], fx.tree), (0, "M README.md\n?? .gitattributes\n?? new.txt"))
        self.assertEqual(git_trees.git(["rev-list", "--count", "origin/main..HEAD"], fx.tree), (0, "3"))
        self.assertEqual(git_trees.git(["merge-base", "--is-ancestor", fx.sha["D"], "main"], fx.tree)[0], 1)
        self.assertEqual(fx.fired(), [])
        self.assertEqual(sorted(_base.iterdir()), views, "the view was not cleaned up")

    def test_a_worker_planted_program_does_not_run_through_git(self) -> None:
        want = git_trees.git(["status", "--porcelain"], self.fixture().tree)
        for vector, marker in self.FIRES.items():
            with self.subTest(vector):
                fx = self.fixture(vector)
                self.bites(fx, marker, vector)
                self.assertEqual(git_trees.git(["status", "--porcelain"], fx.tree), want)
                self.assertEqual(fx.fired(), [])

    def test_a_worker_planted_alias_does_not_run_through_git(self) -> None:
        fx = self.fixture("alias")
        self.bites(fx, "alias", "alias")
        code, text = git_trees.git(["st"], fx.tree)
        self.assertNotEqual(code, 0)
        self.assertTrue(text.startswith("unreadable:"), text)
        self.assertEqual(fx.fired(), [])

    def test_read_state_runs_no_worker_program_and_reads_what_a_clean_tree_reads(self) -> None:
        want = git_trees.read_state(self.fixture().tree)
        self.assertEqual(want, TreeState("feat", 3, 3, 1))
        for vector, marker in self.FIRES.items():
            with self.subTest(vector):
                fx = self.fixture(vector)
                self.bites(fx, marker, vector)
                self.assertEqual(git_trees.read_state(fx.tree), want)
                self.assertEqual(fx.fired(), [])

    def test_a_tree_with_a_submodule_reads_unreadable_for_status_and_still_answers_history(self) -> None:
        fx = self.fixture("gitlink")
        self.bites(fx, "nested-fsmonitor", "gitlink")
        code, text = git_trees.git(["status", "--porcelain"], fx.tree)
        self.assertNotEqual(code, 0)
        self.assertEqual(text, "unreadable: submodules are not inspected")
        self.assertEqual(fx.fired(), [])
        self.assertEqual(git_trees.git(["rev-parse", "HEAD"], fx.tree), (0, fx.sha["D"]))  # control: object reads are unaffected
        self.assertEqual(git_trees.git(["rev-list", "--count", "origin/main..HEAD"], fx.tree), (0, "3"))

    def test_a_submodule_tree_is_never_read_as_safe(self) -> None:
        """`dirty` is None when `status` is unreadable (R8), and the history counts with it: a tree whose commits are all on
        origin/main and whose branch is pushed (an upstream that would say `unpushed` 0) is still at risk, for the one reason
        that is true. Pinned for that tree too: there nothing else would say it is at risk, and nulling `unpushed` and
        `off_origin` is the `if readable` guard (R3)."""
        for label, on_origin in (("ahead of origin/main", False), ("all commits on origin/main", True)):
            with self.subTest(label):
                fx = self.fixture("gitlink")
                if on_origin:
                    taint.git(["update-ref", "refs/remotes/origin/main", fx.sha["D"]], fx.clone)
                    taint.git(["update-ref", "refs/remotes/origin/feat", fx.sha["D"]], fx.clone)
                    taint.git(["update-ref", "refs/heads/main", fx.sha["D"]], fx.clone)
                    self.assertEqual(git_trees.git(["rev-list", "--count", "origin/main..HEAD"], fx.tree), (0, "0"))
                state = git_trees.read_state(fx.tree)
                self.assertEqual((state.dirty, state.off_origin, state.unpushed), (None, None, None))
                self.assertIn(tree_state.UNCOMMITTED_UNKNOWN, at_risk(state), state)
                self.assertEqual(fx.fired(), [])

    def test_a_git_dir_token_in_the_arguments_does_not_select_an_unsanitised_reader(self) -> None:
        """R5, restated (#443 slice 4): `git()` chose the unsanitised reader on argv text, and argv is reachable from the
        tracker (`branch_candidates` appends a tree name, and a name may begin with a dash). Now there is no unsanitised
        reader to choose: `_raw_reads`, `_raw_git` and the `_raw` switch are gone, and every call, whatever its argv, is a
        `git_view.run` call that starts no git process of its own."""
        fx = self.fixture()
        for name in ("_raw_git", "_raw_reads", "_raw", "_has_grafts"):
            self.assertFalse(hasattr(git_trees, name), f"git_trees.{name} is still there")
        argvs = (["rev-parse", "--git-dir"], ["merge-base", "--is-ancestor", "--git-dir", "main"],
                 ["rev-parse", "--verify", "--quiet", "--git-dir"])
        with patch.object(git_view, "run", wraps=git_view.run) as viewed, git_calls() as calls:
            for args in argvs:
                with self.subTest(args=args):
                    git_trees.git(args, fx.tree)
        self.assertEqual([c.args[0] for c in viewed.call_args_list], list(argvs))
        self.assertEqual([c for c in calls if unsanitised(c)], [])

    def test_a_home_python_cannot_find_is_a_cache_configuration_error(self) -> None:
        """An unavailable launcher home must escape the per-tree unreadable handler."""
        fx = self.fixture()
        real = git_view._base

        def homeless(layout, base):  # the default base, as `run` builds it when no caller passes one
            return real(layout, None)

        with patch.dict(os.environ), patch.object(git_view, "_base", homeless), \
                patch.object(Path, "home", side_effect=RuntimeError("Could not determine home directory.")):
            os.environ.pop("XDG_CACHE_HOME", None)
            with self.assertRaisesRegex(git_view.CacheError, "cache"):
                git_trees.git(["rev-parse", "HEAD"], fx.tree)

    def test_a_value_or_runtime_error_out_of_the_view_is_an_unreadable_read(self) -> None:
        """R6, `ValueError` (a non-UTF-8 object path written into the view on Linux; macOS refuses to create one, so this
        is the exception injected, not the path)."""
        for exc in (RuntimeError("Could not determine home directory."), ValueError("embedded null byte")):
            with self.subTest(type(exc).__name__), patch.object(git_view, "run", side_effect=exc):
                code, text = git_trees.git(["rev-parse", "HEAD"], self.root)
                self.assertEqual(code, 128)
                self.assertTrue(text.startswith("unreadable:"), text)

    def test_the_branch_of_a_tree_git_cannot_read_is_one_placeholder_not_the_error_text(self) -> None:
        """R8: the branch is printed in every finding and keys reopen grouping, so it must not carry git's reason."""
        seen = {}
        for kind in ("symlink", "huge"):
            fx = self.fixture()
            dotgit = fx.tree / ".git"
            content = dotgit.read_bytes()
            dotgit.unlink()
            if kind == "symlink":
                (fx.root / "gitfile").write_bytes(content)
                dotgit.symlink_to(fx.root / "gitfile")
            else:
                dotgit.write_bytes(content + b"\n" * 4096)
            code, error = git_trees.git(["rev-parse", "--abbrev-ref", "HEAD"], fx.tree)
            self.assertEqual(code, 128)
            seen[kind] = (git_trees.read_state(fx.tree).branch, error)
        (branch, error), (other, other_error) = seen["symlink"], seen["huge"]
        self.assertNotEqual(error, other_error, "the premise: two different reasons")
        self.assertEqual(branch, other, "one placeholder, or reopen grouping splits by reason")
        self.assertTrue(branch)
        self.assertNotIn(error, branch)
        self.assertNotIn("unreadable:", branch)

    def test_a_hostile_dot_git_is_128_unreadable_and_never_hangs(self) -> None:
        for kind in ("symlink", "fifo", "huge"):
            with self.subTest(kind):
                fx = self.fixture()
                dotgit = fx.tree / ".git"
                content = dotgit.read_bytes()
                dotgit.unlink()
                if kind == "symlink":
                    target = fx.root / "gitfile"
                    target.write_bytes(content)
                    dotgit.symlink_to(target)
                elif kind == "fifo":
                    os.mkfifo(dotgit)
                else:
                    dotgit.write_bytes(content + b"\n" * 4096)
                with patch.object(git_trees, "GIT_TIMEOUT", 2):  # a former read of a FIFO `.git` may block until this
                    for args in (["rev-parse", "HEAD"], ["status", "--porcelain"]):
                        code, text = git_trees.git(args, fx.tree)
                        self.assertEqual(code, 128)
                        self.assertTrue(text.startswith("unreadable:"), text)
                    state = git_trees.read_state(fx.tree)
                self.assertEqual((state.dirty, state.off_origin, state.unpushed), (None, None, None))
                self.assertIn(tree_state.UNCOMMITTED_UNKNOWN, at_risk(state))

    def test_a_directory_that_is_not_a_repository_is_128_unreadable(self) -> None:
        """The former read also answered 128 here (`fatal: not a git repository`): the code is kept, the text now names the view."""
        empty = self.root / "empty"
        empty.mkdir()
        code, text = git_trees.git(["rev-parse", "HEAD"], empty)
        self.assertEqual(code, 128)
        self.assertTrue(text.startswith("unreadable:"), text)
        state = git_trees.read_state(empty)
        self.assertEqual((state.dirty, state.off_origin, state.unpushed), (None, None, None))
        self.assertIn(tree_state.UNCOMMITTED_UNKNOWN, at_risk(state))

    def test_a_redirected_common_dir_is_unreadable_and_runs_nothing(self) -> None:
        for vector, marker in (("commondir", "decoy-fsmonitor"), ("gitfile", "gitfile-fsmonitor")):
            with self.subTest(vector):
                fx = self.fixture(vector)
                self.bites(fx, marker, vector)
                code, text = git_trees.git(["status", "--porcelain"], fx.tree)
                self.assertEqual(code, 128)
                self.assertTrue(text.startswith("unreadable:"), text)
                self.assertEqual(fx.fired(), [])


class ShaViewTest(Repo):
    """#443 slice 4: the `--sha` path reads through the sanitised view like every other reader, and takes grafts and shallow
    from the repository's real files (`git_view.locate` + `git_view.signals`), never from the view and never from git."""

    SHALLOW = "unknown \u2014 shallow clone, so history is cut off"
    GRAFTS = "unknown \u2014 this repository has grafts, so ancestry cannot be trusted"

    def head(self) -> str:
        return git("rev-parse", "HEAD", cwd=self.clone)

    def tip(self) -> str:
        return git("rev-parse", "--short", "refs/remotes/origin/main", cwd=self.clone)

    # a. no unsanitised git
    def test_the_unsanitised_readers_are_gone(self) -> None:
        for name in ("_raw_git", "_raw_reads", "_raw", "_has_grafts"):
            self.assertFalse(hasattr(git_trees, name), f"git_trees.{name} is still there")

    def test_no_sha_read_starts_an_unsanitised_git_process(self) -> None:
        """A normal clone, a linked worktree and a pruned one (the redirect): every git process the sha path starts is the
        fetch (a write, `fetch_base`, still real) or one of the view's. `_raw_git` was `git -C <tree> ...` with no `GIT_DIR`."""
        sha = self.head()
        tree = self.tree("wt-a", "feat/a")
        _, dead = self.pruned("wt-dead")
        with git_calls() as calls:
            asked = git_trees.sha_trees(self.root, "trees/")
            answers = [git_trees.check_sha(sha, t) for t in (self.clone, tree, dead)]
        self.assertEqual([code for code, _, _ in answers], [0, 0, 0])
        self.assertEqual([name for name, _ in asked], ["wt-a"])
        self.assertTrue([c for c in calls if "fetch" in c[0]], "the fetch stays real")
        self.assertTrue([c for c in calls if "GIT_DIR" in c[1]], "the reads ran in views")
        self.assertEqual([c[0] for c in calls if unsanitised(c)], [])

    # c. grafts and shallow come from files
    def test_a_non_empty_info_grafts_is_unknown_and_an_empty_one_is_not(self) -> None:
        sha, tip, tree = self.head(), self.tip(), self.tree("wt-a", "feat/a")
        grafts = self.clone / ".git" / "info" / "grafts"
        grafts.parent.mkdir(exist_ok=True)

        def as_file(text: str) -> None:
            if grafts.is_dir():
                grafts.rmdir()
            grafts.write_text(text)

        def as_directory() -> None:
            grafts.unlink(missing_ok=True)
            grafts.mkdir()

        unknown = (2, f"sha {sha}: {self.GRAFTS}", True)
        yes = (0, f"sha {sha}: on origin/main {tip} (fetched)", True)
        # fetch_base is patched: it is a write that runs git with the user's environment, and a planted graft is not its subject
        with patch.object(git_trees, "fetch_base", return_value=""):
            for what, plant, want in (("a graft", lambda: as_file(f"{sha} {sha}\n"), unknown),
                                      ("an empty file", lambda: as_file(""), yes),
                                      ("a directory, which is no file git would honour but is not nothing", as_directory, unknown)):
                plant()
                for place in (self.clone, tree):
                    with self.subTest(what, place=place.name):
                        self.assertEqual(git_trees.check_sha(sha, place), want)

    def test_grafts_are_said_before_shallow(self) -> None:
        sha, tree = self.head(), self.tree("wt-a", "feat/a")
        (self.clone / ".git" / "info").mkdir(exist_ok=True)
        (self.clone / ".git" / "info" / "grafts").write_text(f"{sha} {sha}\n")
        (self.clone / ".git" / "shallow").write_text(f"{sha}\n")
        with patch.object(git_trees, "fetch_base", return_value=""):
            for place in (self.clone, tree):
                self.assertEqual(git_trees.check_sha(sha, place), (2, f"sha {sha}: {self.GRAFTS}", True))

    def test_a_forged_shallow_file_in_a_clone_that_is_not_shallow_is_unknown(self) -> None:
        """A worker writes `<common>/shallow` into a full clone. The commit is on origin/main, which used to answer yes (the
        `--is-shallow-repository` question was only asked after a no). It fails closed: a shallow file at all, empty or not,
        is unknown. The note is built from local main, so a commit local main lacks has none."""
        on_main = self.head()
        tree = self.tree("wt-a", "feat/a")
        commit(tree, "a.txt")
        on_branch = git("rev-parse", "HEAD", cwd=tree)
        self.assertEqual(git("rev-parse", "--is-shallow-repository", cwd=self.clone), "false")
        shallow = self.clone / ".git" / "shallow"
        with patch.object(git_trees, "fetch_base", return_value=""):
            for what, content in (("a commit id", f"{on_main}\n"), ("an empty file", "")):
                shallow.write_text(content)
                for place in (self.clone, tree):
                    with self.subTest(what, place=place.name):
                        self.assertEqual(git_trees.check_sha(on_main, place),
                                         (2, f"sha {on_main}: {self.SHALLOW}; local main holds it", True))
                        self.assertEqual(git_trees.check_sha(on_branch, place), (2, f"sha {on_branch}: {self.SHALLOW}", True))

    def test_a_shallow_clone_is_decided_before_any_ancestry_read_and_a_failed_local_read_only_drops_the_note(self) -> None:
        """D2: with a `shallow` file the answer is the shallow line whatever the ancestry reads say: a fault comparing with
        origin/main, or with local main, is not `could not compare` / `could not read this repository`. The note
        (`; local main holds it`) is best effort: a failed local read drops it."""
        on_main = self.head()
        tree = self.tree("wt-a", "feat/a")
        commit(tree, "a.txt")
        on_branch = git("rev-parse", "HEAD", cwd=tree)
        (self.clone / ".git" / "shallow").write_text(f"{on_main}\n")
        real = git_trees.git
        faults = {
            "origin/main ancestry": lambda a: a[:2] == ["merge-base", "--is-ancestor"] and a[-1] == git_trees.REF,
            "local main ancestry": lambda a: a[:2] == ["merge-base", "--is-ancestor"] and a[-1] == "refs/heads/main",
            "local main ref lookup": lambda a: a[:1] == ["rev-parse"] and "--verify" in a and "refs/heads/main" in a,
        }
        for what, named in faults.items():
            def fake(args, cwd, *rest, named=named, **kw):
                return (128, "") if named(args) else real(args, cwd, *rest, **kw)

            local = what.startswith("local")  # the note needs the local read, so a fault in it drops the note
            for sha, note in ((on_main, "" if local else "; local main holds it"), (on_branch, "")):
                with self.subTest(what, sha=sha[:7]), patch.object(git_trees, "fetch_base", return_value=""), \
                        patch.object(git_trees, "git", fake):
                    self.assertEqual(git_trees.check_sha(sha, tree), (2, f"sha {sha}: {self.SHALLOW}{note}", True))

    def test_more_than_one_commit_is_said_before_shallow(self) -> None:
        (self.clone / ".git" / "shallow").write_text(f"{self.head()}\n")

        def fake(args, cwd, **kw):
            if args[0] == "rev-parse" and args[1].startswith("--disambiguate="):
                return 0, "9f3c1e7aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\n9f3c1e7bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
            return (0, "commit") if args[:2] == ["cat-file", "-t"] else (0, "abc1234") if args[:2] == ["rev-parse", "--short"] else (1, "")

        with patch.object(git_trees, "fetch_base", return_value=""), patch.object(git_trees, "git", fake):
            self.assertEqual(git_trees.check_sha("9f3c1e7", self.clone),
                             (2, "sha 9f3c1e7: unknown \u2014 more than one commit starts with it", True))

    # d. rule A
    def test_a_pruned_worktree_of_a_bare_repository_is_answered_by_it(self) -> None:
        """Rule A, with a `<common>` that is a bare git directory: `locate` gives a layout with no work tree, which is itself
        the git directory, so the redirect is followed (once) exactly as it was when `rev-parse --git-dir` printed `.`."""
        sha = self.head()
        bare = self.root / "bare.git"
        git("clone", "-q", "--bare", f"file://{self.root / 'origin.git'}", str(bare), cwd=self.root)
        tree = self.trees / "wt-bare"
        git("worktree", "add", "-b", "feat/bare", str(tree), "main", cwd=bare)
        shutil.rmtree(bare / "worktrees" / "wt-bare")
        self.assertEqual(raw_git_dir(tree)[0], 128)  # the tree is dead to git
        self.assertIsNone(git_view.locate(bare).tree)
        want = git_trees.check_sha(sha, bare)
        self.assertEqual(want[0], 0, want)
        self.assertEqual(git_trees.check_sha(sha, tree), want)

    def test_a_pruned_worktree_naming_a_work_tree_as_its_common_dir_is_not_answered_by_that_clone(self) -> None:
        """Rule A: `<common>` is a clone's WORK TREE, not its git directory (`locate` gives it a tree; `rev-parse --git-dir`
        there printed `.git`, not `.`). The clone holds the commit on origin/main, and its yes must not be imported."""
        sha = self.head()
        self.assertEqual(git_trees.check_sha(sha, self.clone)[0], 0)
        tree = self.trees / "wt-root"
        tree.mkdir()
        (tree / ".git").write_text(f"gitdir: {self.clone}/worktrees/gone\n")
        self.assertEqual(git_trees._pruned_common(tree).resolve(), self.clone.resolve())
        self.assertIsNotNone(git_view.locate(self.clone).tree)
        self.assertEqual(git_trees.check_sha(sha, tree), (2, f"sha {sha}: unknown \u2014 git could not read this repository", True))


class ShaTaintTest(unittest.TestCase):
    """#443 slice 4: hostile control files in the clone or its common dir never run on the `--sha` reads, and a forged layout
    is neither followed nor given another repository's answer.

    `fetch_base` is patched to report success in every test here: it is a WRITE that legitimately runs git with the user's
    own environment (and config), and is a follow-up outside this slice. The reads are what is asserted. The controls are
    the former reader's own: `bites` runs `git -C <tree> status` under the audit environment and the canary fires, so the
    fixture is armed. None of the commands the sha path ran (`rev-parse`, `cat-file`, `merge-base`) fired any of these
    canaries, so there is no honest control that the OLD sha reads executed one; what the old reads got wrong was the
    ANSWER on a forged layout, a forged `shallow` and a directory `info/grafts` (the reds elsewhere in this file).
    """

    setUp = GitViewFunnelTest.setUp
    fixture = GitViewFunnelTest.fixture
    bites = GitViewFunnelTest.bites
    # config keys that run a program, none of which a read should reach: (key, marker)
    EXTRA = {"sshcommand": ("core.sshCommand", "ssh"), "gitproxy": ("core.gitProxy", "proxy"),
             "alternaterefs": ("core.alternateRefsCommand", "alt-refs"), "packobjects": ("uploadpack.packObjectsHook", "pack"),
             "pager": ("core.pager", "pager"), "askpass": ("core.askPass", "askpass")}

    def planted(self, vector: str) -> taint.Fx:
        if vector in self.EXTRA:
            fx = self.fixture()
            key, tag = self.EXTRA[vector]
            taint.config(fx, key, fx.command(tag))
            return fx
        return self.fixture(vector)

    def tip(self, fx: taint.Fx) -> str:
        return taint.git(["rev-parse", "--short", "refs/remotes/origin/main"], fx.clone).stdout.strip()

    def test_hostile_control_files_never_run_and_the_answer_is_the_clean_one(self) -> None:
        for vector in (*GitViewFunnelTest.FIRES, *self.EXTRA):
            with self.subTest(vector):
                fx = self.planted(vector)
                if vector in GitViewFunnelTest.FIRES:
                    self.bites(fx, GitViewFunnelTest.FIRES[vector], vector)
                sha = fx.sha["A"]  # on origin/main, and on local main
                want = (0, f"sha {sha}: on origin/main {self.tip(fx)} (fetched)", True)
                with patch.object(git_trees, "fetch_base", return_value=""):
                    for tree in (fx.clone, fx.tree):
                        self.assertEqual(git_trees.check_sha(sha, tree), want)
                    self.assertEqual(git_trees.sha_trees(fx.root, "."), [("repo", fx.clone.resolve())])
                self.assertEqual(fx.fired(), [])

    def test_the_fixtures_grafts_and_shallow_are_unknown_from_their_files(self) -> None:
        a_line = "this repository has grafts, so ancestry cannot be trusted"
        for vector, why in (("grafts", a_line), ("grafts_yes", a_line),
                            ("shallow", "shallow clone, so history is cut off; local main holds it")):
            with self.subTest(vector):
                fx = self.fixture(vector)
                sha = fx.sha["A"]
                with patch.object(git_trees, "fetch_base", return_value=""):
                    for tree in (fx.clone, fx.tree):
                        self.assertEqual(git_trees.check_sha(sha, tree), (2, f"sha {sha}: unknown \u2014 {why}", True))

    def test_a_forged_layout_is_unknown_and_never_followed_or_given_another_repositorys_answer(self) -> None:
        """A tainted `commondir` (a decoy repository) or a forged gitfile target (a copy of the worktree entry, itself
        pointing at the real common dir). Today's raw read follows both: the decoy answers `no origin/main`, `held` False,
        which cannot veto another repository's yes; the forged copy answers like the real one, a yes. Both are unknown, and
        `held` True so they do block a yes. The tree's `.git` target is present, so it is not a pruned worktree: no redirect."""
        for vector, marker in (("commondir", "decoy-fsmonitor"), ("gitfile", "gitfile-fsmonitor")):
            with self.subTest(vector):
                fx = self.fixture(vector)
                self.bites(fx, marker, vector)
                sha = fx.sha["A"]
                self.assertIsNone(git_trees._pruned_common(fx.tree))
                with patch.object(git_trees, "fetch_base", return_value=""):
                    self.assertEqual(git_trees.check_sha(sha, fx.tree),
                                     (2, f"sha {sha}: unknown \u2014 git could not read this repository", True))
                    self.assertEqual(git_trees.check_sha(sha, fx.clone)[0], 0)  # the real repository still answers for itself
                self.assertEqual(fx.fired(), [])


if __name__ == "__main__":
    unittest.main()
