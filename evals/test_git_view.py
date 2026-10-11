"""Slice 1 contract: only launcher-authored git metadata may drive a read.

Controls run independently of the missing production module. Each vector test
also proves its control before importing git_view; no control is skipped or
xfail'ed. Compare return codes AND stdout, including empty/error answers.
"""

from __future__ import annotations

import os
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import git_trees  # noqa: E402
from evals import git_taint as taint  # noqa: E402


# plan-443.md section 1: read calls only (fetch/worktree are write funnels).
# check-ref-format is the one inventory read outside the object-only set.
OBJECT_ONLY = frozenset((
    "rev-parse", "rev-list", "merge-base", "cat-file", "for-each-ref", "log",
    "show", "ls-tree", "branch", "symbolic-ref", "ls-files",
))
READ_ONLY_COMMANDS = OBJECT_ONLY | {"status", "diff", "check-ref-format"}
SAFE_CALLER_GIT_ENV = frozenset((
    "GIT_TERMINAL_PROMPT", "GIT_OPTIONAL_LOCKS", "GIT_NO_REPLACE_OBJECTS",
    "GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM", "GIT_CONFIG_NOSYSTEM",
))


def raw(fx, args, env=None, path=None):
    return subprocess.run(["git", "-C", str(path or fx.tree), *args],
                          env=git_trees.audit_env() if env is None else env,
                          capture_output=True, text=True, timeout=5)


def answer(result):
    return result.returncode, result.stdout


def commands(fx):
    s = fx.sha
    return (
        ["status", "--short"],
        ["rev-parse", "HEAD"],
        ["log", "--format=%h %s", "--stat", f"{s['B']}..{s['D']}"],
        ["rev-list", "--count", f"{s['B']}..{s['D']}"],
        ["rev-list", "--count", "origin/main..HEAD"],
        ["rev-list", "--count", "@{u}..HEAD"],
        ["branch", "--show-current"],
        ["merge-base", "--is-ancestor", s["A"], s["D"]],
        ["merge-base", "--is-ancestor", s["D"], s["A"]],
        ["rev-parse", "--is-shallow-repository"],
    )


def prove_control(case, fx, vector):
    """Assert the precise bad behaviour, rather than just a changed answer."""
    fx.clear()
    env = git_trees.audit_env()
    markers = {
        "fsmonitor": "fsmonitor", "filter": "filter-clean",
        "hooks": "hook-post-index-change", "hooks_in_common": "hook-in-common",
        "include": "include-fsmonitor", "wtconfig": "wtconfig-fsmonitor",
        "commondir": "decoy-fsmonitor", "gitfile": "gitfile-fsmonitor",
        "gitlink": "nested-fsmonitor",
    }
    if vector in markers:
        if vector in ("hooks", "hooks_in_common"):
            # OPTIONAL_LOCKS=0 suppresses index-refresh hooks in the old audit
            # path; one_shot's old environment permits that refresh.
            env.pop("GIT_OPTIONAL_LOCKS")
        raw(fx, ["status", "--short"], env)
        case.assertIn(markers[vector], fx.fired())
    elif vector == "grafts":
        case.assertEqual(answer(raw(fx, ["rev-list", "--count", "origin/main..HEAD"])), (0, "2\n"))
    elif vector == "grafts_yes":
        case.assertEqual(raw(fx, ["merge-base", "--is-ancestor", fx.sha["D"], "main"]).returncode, 0)
    elif vector == "shallow":
        case.assertEqual(answer(raw(fx, ["rev-parse", "--is-shallow-repository"])), (0, "true\n"))
    elif vector == "replace":
        # The existing audit env ALREADY closes replace refs. Demonstrate the
        # hostile ref with replacement enabled, then retain the audit guard.
        env.pop("GIT_NO_REPLACE_OBJECTS")
        case.assertEqual(answer(raw(fx, ["rev-list", "--count", "origin/main..HEAD"], env)), (0, "1\n"))
        case.assertEqual(answer(raw(fx, ["rev-list", "--count", "origin/main..HEAD"])), (0, "3\n"))
    elif vector == "nested_untracked":
        # This vector is safe at the parent, but its nested command must be
        # live: a test that merely planted a dead hook would prove nothing.
        raw(fx, ["status", "--short"], path=fx.tree / "nested")
        case.assertIn("nested-fsmonitor", fx.fired())
        fx.clear()
        parent = raw(fx, ["status", "--short"])
        case.assertEqual(parent.returncode, 0, parent.stderr)
        case.assertIn("?? nested/\n", parent.stdout)
        case.assertEqual(fx.fired(), [])
    elif vector == "reftable_marker":
        # Plain files-backend git ignores this marker. The view must instead
        # refuse to interpret a possible reftable store as an empty ref set.
        case.assertTrue((fx.common / "reftable").is_dir())
        case.assertEqual(answer(raw(fx, ["rev-parse", "HEAD"])), (0, fx.sha["D"] + "\n"))
    else:
        case.fail(f"control missing for {vector}")
    fx.clear()


@contextmanager
def deadline(seconds=3):
    """A FIFO regression must fail promptly, without a stranded test thread."""
    def expired(signum, frame):
        raise AssertionError("worker-controlled file blocked the read")
    previous = signal.signal(signal.SIGALRM, expired)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


class FixtureCase(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.base = self.root / "private-views"
        self.base.mkdir()

    def fixture(self, vector=None, packed=False, sha256=False):
        root = Path(tempfile.mkdtemp(prefix="fixture-", dir=self.root))
        return (taint.build_sha256 if sha256 else taint.build)(root, vector, packed)

    @property
    def view(self):
        # Normal sibling-module import, delayed so real controls remain
        # runnable in the intended module-missing red state.
        import git_view
        return git_view

    def run_view(self, fx, args, **kwargs):
        return self.view.run(args, fx.tree, env=git_trees.audit_env(), base=self.base, **kwargs)

    def leftovers(self):
        return sorted(p.name for p in self.base.glob("view-*"))

    def compare_truth(self, fx, path=None):
        truth = [(args, answer(raw(fx, args, path=path))) for args in commands(fx)]
        for args, expected in truth:
            with self.subTest(command=args):
                got = self.view.run(args, path or fx.tree, env=git_trees.audit_env(), base=self.base)
                self.assertEqual(answer(got), expected, got.stderr)
        self.assertEqual(fx.fired(), [])
        self.assertEqual(self.leftovers(), [])


class FixtureControlsTest(FixtureCase):
    """These 15 green tests prove the planted hazards without git_view."""


def control_test(vector):
    def test(self):
        prove_control(self, self.fixture(vector), vector)
    test.__name__ = "test_control_" + vector
    return test


for _vector in taint.VECTORS:
    setattr(FixtureControlsTest, "test_control_" + _vector, control_test(_vector))


class LocateTest(FixtureCase):
    def assert_refused_without_git(self, path):
        view = self.view
        with patch.object(subprocess, "run", side_effect=AssertionError("locate started git")) as start:
            with deadline(), self.assertRaises(view.Unviewable) as caught:
                view.locate(path)
        start.assert_not_called()
        self.assertNotIn(str(path), str(caught.exception))
        self.assertTrue(str(caught.exception))

    def test_locate_linked_clone_symlink_and_bare_by_files_only(self):
        fx = self.fixture()
        linked = self.root / "tree-alias"
        linked.symlink_to(fx.tree, target_is_directory=True)
        bare = self.root / "bare.git"
        bare.mkdir()
        taint.git(["init", "--bare", "-q", "-b", "main"], bare)
        view = self.view
        with patch.object(subprocess, "run", side_effect=AssertionError("locate started git")) as start:
            for path, tree, gitdir, common in (
                (fx.tree, fx.tree, fx.gitdir, fx.common),
                (linked, fx.tree, fx.gitdir, fx.common),
                (fx.clone, fx.clone, fx.common, fx.common),
                (fx.common, None, fx.common, fx.common),
                (bare, None, bare, bare),
            ):
                with self.subTest(path=path):
                    layout = view.locate(path)
                    self.assertEqual((layout.tree, layout.gitdir, layout.common), (tree, gitdir, common))
        start.assert_not_called()

    def test_locate_relative_worktree_paths(self):
        fx = self.fixture()
        relative = fx.root / "relative"
        taint.git(["worktree", "add", "-q", "-b", "relative", str(relative)], fx.clone)
        # Git 2.48+ can write these relative pointers with --relative-paths.
        # Build the same layout by hand so older CI Git exercises it too.
        gitdir = fx.common / "worktrees" / "relative"
        (relative / ".git").write_text(f"gitdir: {os.path.relpath(gitdir, relative)}\n")
        (gitdir / "gitdir").write_text(os.path.relpath(relative / ".git", gitdir) + "\n")
        self.assertFalse(Path((relative / ".git").read_text().removeprefix("gitdir: ").strip()).is_absolute())
        self.assertFalse(Path((gitdir / "gitdir").read_text().strip()).is_absolute())
        view = self.view
        with patch.object(subprocess, "run", side_effect=AssertionError("locate started git")):
            layout = view.locate(relative)
        self.assertEqual((layout.tree, layout.gitdir, layout.common),
                         (relative, fx.common / "worktrees" / "relative", fx.common))

    def test_locate_refuses_redirects_before_any_git(self):
        for vector in ("commondir", "gitfile"):
            with self.subTest(vector=vector):
                fx = self.fixture(vector)
                prove_control(self, fx, vector)
                self.assert_refused_without_git(fx.tree)
                view = self.view
                with patch.object(subprocess, "run", side_effect=AssertionError("run started git")) as start:
                    with self.assertRaises(view.Unviewable):
                        self.run_view(fx, ["rev-parse", "HEAD"])
                start.assert_not_called()
                self.assertEqual(fx.fired(), [])

    def test_locate_refuses_missing_symlink_fifo_and_oversize_gitfile(self):
        for kind in ("missing", "symlink", "fifo", "huge"):
            with self.subTest(kind=kind):
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
                elif kind == "huge":
                    # The pointer is otherwise valid even if a reader strips
                    # trailing newlines or silently truncates at its limit.
                    dotgit.write_bytes(content + b"\n" * 4096)
                self.assert_refused_without_git(fx.tree)

    def test_locate_accepts_gitfile_at_exactly_4096_bytes(self):
        fx = self.fixture()
        dotgit = fx.tree / ".git"
        content = dotgit.read_bytes()
        dotgit.write_bytes(content + b"\n" * (4096 - len(content)))
        self.assertEqual(dotgit.stat().st_size, 4096)
        view = self.view
        with patch.object(subprocess, "run", side_effect=AssertionError("locate started git")):
            self.assertEqual(view.locate(fx.tree).gitdir, fx.gitdir)

    def test_locate_refuses_submodule_gitfile_bad_backpointer_and_wrong_parent(self):
        for kind in ("submodule", "backpointer", "parent"):
            with self.subTest(kind=kind):
                fx = self.fixture()
                if kind == "backpointer":
                    (fx.gitdir / "gitdir").write_text(str(fx.clone / ".git") + "\n")
                else:
                    target = fx.common / "modules" / "x" if kind == "submodule" else fx.root / "outside"
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copytree(fx.gitdir, target)
                    (target / "commondir").unlink()
                    if kind == "parent":
                        (target / "commondir").write_text(str(fx.common) + "\n")
                    (fx.tree / ".git").write_text(f"gitdir: {target}\n")
                self.assert_refused_without_git(fx.tree)

    def test_locate_requires_head_objects_and_refs_in_common(self):
        for entry in ("HEAD", "objects", "refs"):
            with self.subTest(entry=entry):
                fx = self.fixture()
                target = fx.common / entry
                if target.is_dir():
                    shutil.rmtree(target)
                else:
                    target.unlink()
                self.assert_refused_without_git(fx.clone)
                self.assert_refused_without_git(fx.tree)

    def test_locate_bounds_commondir_and_backpointer_without_following_symlinks(self):
        for name in ("commondir", "gitdir"):
            for kind in ("fifo", "symlink", "huge"):
                with self.subTest(name=name, kind=kind):
                    fx = self.fixture()
                    target = fx.gitdir / name
                    original = target.read_bytes()
                    target.unlink()
                    if kind == "fifo":
                        os.mkfifo(target)
                    elif kind == "symlink":
                        other = fx.root / "redirect"
                        other.write_bytes(original)
                        target.symlink_to(other)
                    else:
                        target.write_bytes(original + b"\n" * 4096)
                    self.assert_refused_without_git(fx.tree)

    def test_locate_refuses_symlinked_clone_git_directory(self):
        fx = self.fixture()
        renamed = fx.root / "moved-gitdir"
        fx.common.rename(renamed)
        fx.common.symlink_to(renamed, target_is_directory=True)
        self.assert_refused_without_git(fx.clone)


class NotARepositoryTest(FixtureCase):
    """A plain folder is its own named case; every other unreadable layout stays a plain Unviewable."""

    def plain(self, name="plain"):
        path = self.root / name
        path.mkdir()
        return path

    def test_not_a_repository_is_an_unviewable_subclass(self):
        self.assertTrue(issubclass(self.view.NotARepository, self.view.Unviewable))

    def test_locate_and_run_name_a_plain_folder_not_a_repository(self):
        view = self.view
        empty, filled = self.plain("empty"), self.plain("filled")
        (filled / "notes.txt").write_text("generic\n")
        (filled / "sub").mkdir()
        for path in (empty, filled):
            with self.subTest(path=path.name):
                with patch.object(subprocess, "run", side_effect=AssertionError("started git")) as start:
                    with self.assertRaises(view.NotARepository) as located:
                        view.locate(path)
                    with self.assertRaises(view.NotARepository):
                        view.run(["branch", "--show-current"], path, env=git_trees.audit_env(), base=self.base)
                start.assert_not_called()
                self.assertNotIn(str(path), str(located.exception))
                self.assertTrue(str(located.exception))
        self.assertEqual(self.leftovers(), [])

    def test_a_plain_folder_is_still_caught_as_unviewable(self):
        with self.assertRaises(self.view.Unviewable):
            self.view.locate(self.plain())

    def assert_unviewable_only(self, path, *, locate=True):
        view = self.view
        calls = [lambda: view.run(["rev-parse", "HEAD"], path, env=git_trees.audit_env(), base=self.base)]
        if locate:
            calls.append(lambda: view.locate(path))
        for call in calls:
            with deadline(), self.assertRaises(view.Unviewable) as caught:
                call()
            self.assertNotIsInstance(caught.exception, view.NotARepository)
        self.assertEqual(self.leftovers(), [])

    def test_a_dotgit_that_is_not_a_directory_or_gitfile_is_not_a_plain_folder(self):
        for kind in ("symlink", "dangling-symlink", "fifo", "garbage", "empty", "huge"):
            with self.subTest(kind=kind):
                path = self.plain(kind)
                dotgit = path / ".git"
                if kind == "symlink":
                    (self.root / f"{kind}-target").write_text("gitdir: nowhere\n")
                    dotgit.symlink_to(self.root / f"{kind}-target")
                elif kind == "dangling-symlink":
                    dotgit.symlink_to(self.root / "missing")
                elif kind == "fifo":
                    os.mkfifo(dotgit)
                elif kind == "garbage":
                    dotgit.write_bytes(b"\x00garbage\xff not a gitfile\n")
                elif kind == "empty":
                    dotgit.write_bytes(b"")
                else:
                    dotgit.write_bytes(b"gitdir: x\n" + b"\n" * 8192)
                self.assert_unviewable_only(path)

    def test_a_dotgit_directory_missing_pieces_is_not_a_plain_folder(self):
        for missing in ("HEAD", "objects", "refs", "everything"):
            with self.subTest(missing=missing):
                fx = self.fixture()
                gone = ("HEAD", "objects", "refs") if missing == "everything" else (missing,)
                for entry in gone:
                    target = fx.clone / ".git" / entry
                    shutil.rmtree(target) if target.is_dir() else target.unlink()
                self.assert_unviewable_only(fx.clone)

    def test_a_dotgit_directory_without_head_in_a_plain_folder_is_not_a_plain_folder(self):
        path = self.plain()
        (path / ".git").mkdir()
        self.assert_unviewable_only(path)

    def test_partial_bare_layouts_without_dotgit_are_not_a_plain_folder(self):
        for present in ("HEAD", "objects", "refs"):
            with self.subTest(present=present):
                path = self.plain(f"partial-{present}")
                if present == "HEAD":
                    (path / "HEAD").write_text("ref: refs/heads/main\n")
                else:
                    (path / present).mkdir()
                self.assert_unviewable_only(path)

    def test_a_bare_layout_with_an_empty_or_unsafe_head_is_not_a_plain_folder(self):
        for text in ("", "not a ref\n", "ref: ../outside\n"):
            with self.subTest(head=text):
                bare = self.plain("bare-" + str(abs(hash(text))))
                taint.git(["init", "--bare", "-q", "-b", "main"], bare)
                (bare / "HEAD").write_text(text)
                self.assert_unviewable_only(bare, locate=False)  # locate accepts it; the head read refuses

    def test_pointers_into_another_tree_are_not_a_plain_folder(self):
        for vector in ("commondir", "gitfile"):
            with self.subTest(vector=vector):
                fx = self.fixture(vector)
                self.assert_unviewable_only(fx.tree)


class ContentsTest(FixtureCase):
    def config_values(self, directory):
        out = taint.git(["config", "--file", str(directory / "config"), "-z", "--list"], self.root)
        values = {}
        for record in out.stdout.split("\0"):
            if record:
                key, separator, value = record.partition("\n")
                self.assertEqual(separator, "\n")
                self.assertNotIn(key.lower(), values, "duplicate config fact")
                values[key.lower()] = value
        return values

    def expected_config(self, fx, *, bare=False, detached=False, upstream=True, sha256=False):
        expected = {
            "core.repositoryformatversion": "1" if sha256 else "0",
            "core.bare": str(bare).lower(), "core.fsmonitor": "false",
            "core.usereplacerefs": "false",
            "core.untrackedcache": "false", "core.commitgraph": "false",
            "core.multipackindex": "false", "core.hookspath": os.devnull,
            "core.attributesfile": os.devnull, "core.excludesfile": os.devnull,
            "status.submodulesummary": "false", "diff.ignoresubmodules": "all", "pack.usebitmaps": "false",
        }
        real = self.config_values(fx.common)
        for key in ("core.filemode", "core.ignorecase", "core.precomposeunicode", "core.symlinks"):
            if real.get(key) in ("true", "false"):
                expected[key] = real[key]
        if sha256:
            expected["extensions.objectformat"] = "sha256"
        if upstream and not detached and not bare:
            expected.update({"branch.feat.remote": "origin", "branch.feat.merge": "refs/heads/feat"})
        # Valid fetch facts may be retained for remotes other than the current
        # branch; their URL is always authored by the launcher.
        expected.update({"remote.origin.fetch": "+refs/heads/*:refs/remotes/origin/*",
                         "remote.origin.url": "/nonexistent"})
        return expected

    def assert_config_values(self, directory, expected):
        facts = self.config_values(directory)
        # Round 3 separately REQUIRES this new safety pin. Keep the earlier
        # factual-config contract compatible with both sides of that RED fix.
        if "gpg.program" in facts:
            self.assertEqual(facts.pop("gpg.program"), os.devnull)
        self.assertEqual(facts, expected)

    def test_contents_and_exact_allowlisted_config_facts(self):
        for shape in ("sha1", "sha256", "bare", "detached", "no-upstream", "packed", "split"):
            with self.subTest(shape=shape):
                fx = self.fixture(packed=shape == "packed", sha256=shape == "sha256")
                if shape == "detached":
                    taint.git(["checkout", "--detach", "-q"], fx.tree)
                if shape == "no-upstream":
                    taint.git(["config", "--unset", "branch.feat.remote"], fx.clone)
                    taint.git(["config", "--unset", "branch.feat.merge"], fx.clone)
                if shape == "split":
                    taint.git(["update-index", "--split-index"], fx.tree)
                path = fx.common if shape == "bare" else fx.tree
                with self.view.opened(path, git_trees.audit_env(), base=self.base) as v:
                    directory = Path(v.env["GIT_DIR"])
                    self.assertEqual(v.layout, self.view.locate(path))
                    self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700)
                    entries = {"HEAD", "refs", "objects", "config"}
                    if shape != "bare":
                        entries.add("index")
                        entries.update(p.name for p in fx.gitdir.glob("sharedindex.*"))
                    if shape == "packed":
                        entries.add("packed-refs")
                    self.assertEqual({p.name for p in directory.iterdir()}, entries)
                    self.assertEqual({p.relative_to(directory).as_posix() for p in (directory / "objects").rglob("*")},
                                     {"objects/info", "objects/info/alternates"})
                    self.assertTrue((directory / "refs").is_symlink())
                    self.assertEqual((directory / "refs").resolve(), fx.common / "refs")
                    if shape == "packed":
                        # R18 separately requires a bounded private copy. This
                        # older inventory check accepts either representation.
                        self.assertEqual((directory / "packed-refs").read_bytes(),
                                         (fx.common / "packed-refs").read_bytes())
                    self.assertEqual((directory / "HEAD").read_bytes(), (fx.common if shape == "bare" else fx.gitdir).joinpath("HEAD").read_bytes())
                    self.assertEqual((directory / "objects/info/alternates").read_text(), str(fx.common / "objects") + "\n")
                    self.assertNotIn("GIT_COMMON_DIR", v.env)
                    if shape == "bare":
                        self.assertNotIn("GIT_WORK_TREE", v.env)
                        self.assertNotIn("GIT_INDEX_FILE", v.env)
                    else:
                        self.assertEqual(v.env["GIT_WORK_TREE"], str(fx.tree))
                        self.assertEqual(v.env["GIT_INDEX_FILE"], str(directory / "index"))
                        self.assertFalse((directory / "index").is_symlink())
                        self.assertEqual((directory / "index").read_bytes(), (fx.gitdir / "index").read_bytes())
                        for shared in fx.gitdir.glob("sharedindex.*"):
                            self.assertEqual((directory / shared.name).read_bytes(), shared.read_bytes())
                    self.assert_config_values(directory, self.expected_config(
                        fx, bare=shape == "bare", detached=shape == "detached",
                        upstream=shape != "no-upstream", sha256=shape == "sha256"))
                self.assertFalse(directory.exists())
                self.assertEqual(self.leftovers(), [])

    def test_all_hostile_config_keys_and_includes_are_dropped(self):
        fx = self.fixture()
        # Prove each config vector by itself before stacking their settings.
        for vector in ("fsmonitor", "filter", "hooks", "hooks_in_common", "include", "wtconfig"):
            control = self.fixture(vector)
            prove_control(self, control, vector)
            taint.apply(fx, vector)
        for key in ("core.sshCommand", "core.gitProxy", "credential.helper", "diff.external",
                    "core.pager", "core.worktree", "remote.origin.uploadpack", "remote.origin.proxy",
                    "url.worker.insteadOf", "status.showUntrackedFiles"):
            taint.config(fx, key, "worker-command")
        expected = self.expected_config(fx)
        with self.view.opened(fx.tree, git_trees.audit_env(), base=self.base) as v:
            directory = Path(v.env["GIT_DIR"])
            self.assert_config_values(directory, expected)
            text = (directory / "config").read_text()
            for hostile in ("worker-command", "worker.cfg", "worker-hooks", "include-fsmonitor",
                            "wtconfig-fsmonitor", "filter-clean", "textconv", str(fx.markers)):
                self.assertNotIn(hostile, text)
            self.assertNotIn("include", text.lower())
        self.assertEqual(fx.fired(), [])

    def test_bad_branch_names_never_become_config_sections(self):
        for name in ('bad"name', "bad\nname", "bad..name"):
            with self.subTest(name=name):
                fx = self.fixture()
                (fx.gitdir / "HEAD").write_text("ref: refs/heads/" + name + "\n")
                view = self.view
                try:
                    with view.opened(fx.tree, git_trees.audit_env(), base=self.base) as v:
                        facts = self.config_values(Path(v.env["GIT_DIR"]))
                        self.assertFalse(any(k.startswith("branch.") for k in facts))
                except view.Unviewable:
                    pass  # rejecting invalid HEAD is also a closed answer
                self.assertEqual(self.leftovers(), [])

    def test_invalid_boolean_is_dropped(self):
        fx = self.fixture()
        taint.config(fx, "core.filemode", "maybe")
        with self.view.opened(fx.tree, git_trees.audit_env(), base=self.base) as v:
            self.assertNotIn("core.filemode", self.config_values(Path(v.env["GIT_DIR"])))

    def test_invalid_upstream_and_fetch_values_are_dropped(self):
        for key, value in (("branch.feat.remote", 'bad"remote'),
                           ("branch.feat.remote", "bad..remote"),
                           ("branch.feat.merge", "refs/heads/bad\nname"),
                           ("branch.feat.merge", "refs/heads/bad..name"),
                           ("remote.origin.fetch", '+refs/heads/feat:refs/remotes/bad"name')):
            with self.subTest(key=key, value=value):
                fx = self.fixture()
                taint.config(fx, key, value)
                with self.view.opened(fx.tree, git_trees.audit_env(), base=self.base) as v:
                    facts = self.config_values(Path(v.env["GIT_DIR"]))
                    self.assertNotIn(key.lower(), facts)
                    self.assertNotIn(value, facts.values())
                self.assertEqual(fx.fired(), [])

    def test_config_is_consulted_without_includes_and_command_uses_private_env(self):
        fx = self.fixture("include")
        prove_control(self, fx, "include")
        view = self.view
        with patch.object(subprocess, "run", wraps=subprocess.run) as started:
            result = self.run_view(fx, ["rev-parse", "HEAD"])
        self.assertEqual(answer(result), (0, fx.sha["D"] + "\n"))
        parsing = [call for call in started.call_args_list if "config" in call.args[0]]
        self.assertEqual(len(parsing), 1)
        argv = parsing[0].args[0]
        self.assertIn("--file", argv)
        self.assertIn(str(fx.common / "config"), argv)
        self.assertIn("-z", argv)
        self.assertIn("--list", argv)
        self.assertNotIn("--includes", argv)
        command = started.call_args_list[-1]
        self.assertEqual(command.args[0], ["git", "rev-parse", "HEAD"])
        self.assertEqual(Path(command.kwargs["cwd"]), fx.tree)
        env = command.kwargs["env"]
        directory = Path(env["GIT_DIR"])
        self.assertEqual(directory.parent, self.base)
        self.assertNotIn("GIT_COMMON_DIR", env)
        self.assertEqual(env["GIT_WORK_TREE"], str(fx.tree))
        self.assertEqual(env["GIT_INDEX_FILE"], str(directory / "index"))
        self.assertFalse(directory.exists())
        self.assertEqual(fx.fired(), [])

    def test_invalid_object_format_and_reftable_config_refuse(self):
        for key, value in (("extensions.objectformat", "md5"), ("extensions.refstorage", "reftable")):
            with self.subTest(key=key):
                fx = self.fixture()
                taint.config(fx, key, value)
                view = self.view
                with self.assertRaises(view.Unviewable):
                    self.run_view(fx, ["rev-parse", "HEAD"])
                self.assertEqual(self.leftovers(), [])

    def test_real_reftable_repository_is_refused(self):
        bare = self.root / "reftable.git"
        bare.mkdir()
        taint.git(["init", "--bare", "-q", "--ref-format=reftable", "-b", "main"], bare)
        self.assertTrue((bare / "reftable").is_dir())
        self.assertEqual(taint.git(["symbolic-ref", "HEAD"], bare).stdout, "refs/heads/main\n")
        view = self.view
        with self.assertRaises(view.Unviewable):
            view.run(["symbolic-ref", "HEAD"], bare, env=git_trees.audit_env(), base=self.base)
        self.assertEqual(self.leftovers(), [])

    def test_many_loose_refs_are_linked_in_under_half_a_second(self):
        fx = self.fixture()
        refs = fx.common / "refs/heads"
        for number in range(3000):
            (refs / f"branch-{number}").write_text(fx.sha["B"] + "\n")
        view = self.view
        start = time.monotonic()
        with view.opened(fx.tree, git_trees.audit_env(), base=self.base) as v:
            self.assertTrue(Path(v.env["GIT_DIR"]).joinpath("refs").is_symlink())
        self.assertLess(time.monotonic() - start, 0.5)
        self.assertEqual(len(list(refs.glob("branch-*"))), 3000)


class HostileFilesTest(FixtureCase):
    def test_fifo_head_index_and_sharedindex_return_promptly(self):
        for name in ("HEAD", "index", "sharedindex." + "a" * 40):
            with self.subTest(name=name):
                fx = self.fixture()
                target = fx.gitdir / name
                target.unlink(missing_ok=True)
                os.mkfifo(target)
                view = self.view
                with deadline():
                    try:
                        with view.opened(fx.tree, git_trees.audit_env(), base=self.base) as v:
                            self.assertNotIn(name, {p.name for p in Path(v.env["GIT_DIR"]).iterdir()})
                    except view.Unviewable:
                        pass
                self.assertEqual(self.leftovers(), [])

    def test_index_over_512_mib_is_not_copied(self):
        fx = self.fixture()
        with (fx.gitdir / "index").open("wb") as file:
            file.truncate(512 * 1024 * 1024 + 1)  # sparse file; no giant allocation
        view = self.view
        with deadline(), self.assertRaises(view.Unviewable):
            with view.opened(fx.tree, git_trees.audit_env(), base=self.base):
                self.fail("oversize index accepted")
        self.assertEqual(self.leftovers(), [])

    def test_oversize_head_and_sharedindex_are_bounded(self):
        for name, size in (("HEAD", 1025), ("sharedindex." + "a" * 40, 512 * 1024 * 1024 + 1)):
            with self.subTest(name=name):
                fx = self.fixture()
                with (fx.gitdir / name).open("wb") as file:
                    file.truncate(size)
                view = self.view
                with deadline():
                    try:
                        with view.opened(fx.tree, git_trees.audit_env(), base=self.base) as v:
                            self.assertFalse(Path(v.env["GIT_DIR"]).joinpath(name).exists())
                    except view.Unviewable:
                        pass
                self.assertEqual(self.leftovers(), [])

    def test_symlinked_worker_files_are_never_copied(self):
        for name in ("HEAD", "index", "sharedindex." + "a" * 40):
            with self.subTest(name=name):
                fx = self.fixture()
                target = fx.gitdir / name
                other = fx.root / "other-file"
                original = target.read_bytes() if target.exists() else b"worker data"
                other.write_bytes(original)
                target.unlink(missing_ok=True)
                target.symlink_to(other)
                view = self.view
                try:
                    with view.opened(fx.tree, git_trees.audit_env(), base=self.base) as v:
                        self.assertFalse(Path(v.env["GIT_DIR"]).joinpath(name).exists())
                except view.Unviewable:
                    pass
                self.assertEqual(other.read_bytes(), original)
                self.assertEqual(self.leftovers(), [])

    def test_only_oid_named_sharedindex_files_are_copied(self):
        fx = self.fixture()
        for name in ("sharedindex.command", "sharedindex." + "g" * 40,
                     "sharedindex." + "a" * 39, "sharedindex." + "a" * 65):
            (fx.gitdir / name).write_text("worker data")
        with self.view.opened(fx.tree, git_trees.audit_env(), base=self.base) as v:
            self.assertEqual(list(Path(v.env["GIT_DIR"]).glob("sharedindex.*")), [])

    def test_refs_swapped_to_symlink_after_locate_remain_data(self):
        fx = self.fixture()
        expected = answer(raw(fx, ["rev-parse", "HEAD"]))
        view = self.view
        layout = view.locate(fx.tree)
        original = fx.common / "refs"
        renamed = fx.root / "moved-refs"
        original.rename(renamed)
        original.symlink_to(renamed, target_is_directory=True)
        # Pin the race between locating and building, without private build APIs.
        with patch.object(view, "locate", return_value=layout):
            self.assertEqual(answer(self.run_view(fx, ["rev-parse", "HEAD"])), expected)
        self.assertTrue((renamed / "heads/feat").is_file())
        self.assertEqual(fx.fired(), [])

    def test_read_regular_moved_and_legacy_alias_is_identical(self):
        view = self.view
        self.assertIs(git_trees.read_regular, view.read_regular)
        self.assertEqual(view.read_regular.__module__, "git_view")
        regular = self.root / "regular"
        regular.write_bytes(b"abcdef")
        self.assertEqual(view.read_regular(regular, 3), b"abc")
        self.assertEqual(view.read_regular(self.root, 3), None)
        fifo = self.root / "fifo"
        os.mkfifo(fifo)
        with deadline():
            self.assertIsNone(view.read_regular(fifo, 3))
        link = self.root / "link"
        link.symlink_to(regular)
        self.assertEqual(view.read_regular(link, 3), b"abc")
        with self.assertRaises(OSError):
            view.read_regular(link, 3, nofollow=True)
        with self.assertRaises(FileNotFoundError):
            view.read_regular(self.root / "absent", 3)


class PrivacyAndCleanupTest(FixtureCase):
    def test_explicit_temporary_base_is_accepted_and_view_is_private(self):
        fx = self.fixture()
        env = git_trees.audit_env()
        before = dict(env)
        with self.view.opened(fx.tree, env, base=self.base) as v:
            directory = Path(v.env["GIT_DIR"])
            self.assertEqual(directory.parent, self.base)
            self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700)
            for key, value in env.items():
                self.assertEqual(v.env[key], value)
        self.assertEqual(env, before)
        self.assertEqual(self.leftovers(), [])

    def test_explicit_base_inside_tree_common_or_gitdir_is_refused(self):
        fx = self.fixture()
        view = self.view
        for parent in (fx.tree, fx.common, fx.gitdir):
            for base in (parent, parent / "views"):
                with self.subTest(base=base), self.assertRaises(view.Unviewable):
                    view.run(["rev-parse", "HEAD"], fx.tree, env=git_trees.audit_env(), base=base)

    def test_default_base_inside_worker_roots_or_system_temp_is_refused(self):
        fx = self.fixture()
        view = self.view
        for cache in (fx.tree, fx.common, fx.gitdir, Path(tempfile.gettempdir())):
            with self.subTest(cache=cache), patch.dict(os.environ, {"XDG_CACHE_HOME": str(cache)}):
                with self.assertRaises(view.Unviewable):
                    view.run(["rev-parse", "HEAD"], fx.tree, env=git_trees.audit_env())

    def test_default_base_uses_launcher_home_and_private_cache(self):
        fx = self.fixture()
        # A temporary launcher home in this writable worktree is outside the
        # system temp dir. No writes to the user's actual home or cache.
        temporary = tempfile.TemporaryDirectory(prefix="view-home-", dir=Path(__file__).resolve().parents[1])
        self.addCleanup(temporary.cleanup)
        home = Path(temporary.name).resolve()
        view = self.view
        for shape in ("home", "xdg"):
            cache = home / (".cache" if shape == "home" else "custom-cache")
            launcher_env = {"HOME": str(home)}
            if shape == "xdg":
                launcher_env["XDG_CACHE_HOME"] = str(cache)
            with self.subTest(shape=shape), patch.dict(os.environ, launcher_env, clear=True), \
                    patch.object(Path, "home", return_value=home):
                with view.opened(fx.tree, git_trees.audit_env()) as v:
                    directory = Path(v.env["GIT_DIR"])
                    self.assertEqual(directory.parent, cache / "chief-of-stuff/git-views")
                    self.assertEqual(stat.S_IMODE(directory.parent.stat().st_mode), 0o700)
                    self.assertEqual(v.env["HOME"], git_trees.SAFE_HOME)
                self.assertFalse(directory.exists())

    def test_real_index_and_refs_untouched_when_optional_locks_are_unset(self):
        for vector in ("hooks", "hooks_in_common"):
            with self.subTest(vector=vector):
                fx = self.fixture(vector, packed=True)
                prove_control(self, fx, vector)
                for path in fx.tree.glob("*.txt"):
                    os.utime(path, (1_900_000_000, 1_900_000_000))
                index = fx.gitdir / "index"
                before = index.read_bytes(), index.stat().st_mtime_ns
                # Packed refs alone leave no loose files to lose on cleanup.
                (fx.common / "refs/heads/keep").write_text(fx.sha["B"] + "\n")
                refs = {p.relative_to(fx.common): p.read_bytes() for p in (fx.common / "refs").rglob("*") if p.is_file()}
                packed = (fx.common / "packed-refs").read_bytes()
                env = git_trees.audit_env()
                env.pop("GIT_OPTIONAL_LOCKS")
                result = self.view.run(["status", "--short"], fx.tree, env=env, base=self.base)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn(" M README.md\n", result.stdout)
                self.assertEqual((index.read_bytes(), index.stat().st_mtime_ns), before)
                self.assertTrue((fx.common / "refs").is_dir())
                self.assertEqual({p.relative_to(fx.common): p.read_bytes() for p in (fx.common / "refs").rglob("*") if p.is_file()}, refs)
                self.assertEqual((fx.common / "packed-refs").read_bytes(), packed)
                self.assertEqual(fx.fired(), [])
                self.assertEqual(self.leftovers(), [])

    def test_cleanup_after_success_and_nonzero_git_exit(self):
        fx = self.fixture()
        self.assertEqual(self.run_view(fx, ["rev-parse", "HEAD"]).returncode, 0)
        self.assertEqual(self.leftovers(), [])
        failed = self.run_view(fx, ["rev-parse", "--verify", "refs/heads/absent"])
        self.assertNotEqual(failed.returncode, 0)
        self.assertTrue(failed.stderr)
        self.assertEqual(self.leftovers(), [])

    def test_cleanup_after_timeout_and_missing_git_during_command(self):
        fx = self.fixture()
        view = self.view
        real_run = subprocess.run
        for error in (subprocess.TimeoutExpired(["git", "rev-parse"], 1), FileNotFoundError("git missing")):
            seen = []
            def failing_command(argv, **kwargs):
                if "config" in argv:
                    return real_run(argv, **kwargs)
                seen.extend(self.leftovers())
                raise error
            with self.subTest(error=type(error).__name__), patch.object(subprocess, "run", side_effect=failing_command):
                with self.assertRaises(type(error)):
                    view.run(["rev-parse", "HEAD"], fx.tree, env=git_trees.audit_env(), timeout=1, base=self.base)
            self.assertTrue(seen, "exception must occur after the private view exists")
            self.assertEqual(self.leftovers(), [])

    def test_cleanup_after_missing_git_during_build(self):
        fx = self.fixture()
        view = self.view
        allocated = []
        real_mkdtemp = tempfile.mkdtemp
        def record_allocation(*args, **kwargs):
            result = real_mkdtemp(*args, **kwargs)
            allocated.append(Path(result))
            return result
        with patch.object(tempfile, "mkdtemp", side_effect=record_allocation), \
                patch.object(subprocess, "run", side_effect=FileNotFoundError("git missing")):
            with self.assertRaises(FileNotFoundError):
                self.run_view(fx, ["rev-parse", "HEAD"])
        self.assertEqual(self.leftovers(), [])
        self.assertTrue(all(not path.exists() for path in allocated))

    def test_cleanup_after_unviewable_and_baseexception_mid_build(self):
        fx = self.fixture()
        view = self.view
        real_run = subprocess.run
        for error in (view.Unviewable("invalid config facts"), KeyboardInterrupt()):
            allocated = []
            real_mkdtemp = tempfile.mkdtemp
            def record_allocation(*args, **kwargs):
                result = real_mkdtemp(*args, **kwargs)
                allocated.append(Path(result))
                return result
            def failing_build(argv, **kwargs):
                if "config" in argv:
                    self.assertTrue(allocated, "exercise failure after mkdtemp, not before construction")
                    raise error
                return real_run(argv, **kwargs)
            with self.subTest(error=type(error).__name__), patch.object(tempfile, "mkdtemp", side_effect=record_allocation), \
                    patch.object(subprocess, "run", side_effect=failing_build):
                with self.assertRaises(type(error)):
                    self.run_view(fx, ["rev-parse", "HEAD"])
            self.assertEqual(self.leftovers(), [])
            self.assertTrue(all(not path.exists() for path in allocated))

    def test_cleanup_after_exception_in_opened_body(self):
        fx = self.fixture()
        with self.assertRaisesRegex(RuntimeError, "body failed"):
            with self.view.opened(fx.tree, git_trees.audit_env(), base=self.base) as v:
                directory = Path(v.env["GIT_DIR"])
                self.assertTrue(directory.exists())
                raise RuntimeError("body failed")
        self.assertFalse(directory.exists())
        self.assertEqual(self.leftovers(), [])

    def test_opened_supports_several_reads_in_one_view_and_cleans_up(self):
        fx = self.fixture("fsmonitor")
        prove_control(self, fx, "fsmonitor")
        with self.view.opened(fx.tree, git_trees.audit_env(), base=self.base) as v:
            directory = Path(v.env["GIT_DIR"])
            for args, expected in ((["rev-parse", "HEAD"], fx.sha["D"] + "\n"),
                                   (["rev-list", "--count", "origin/main..HEAD"], "3\n")):
                result = subprocess.run(["git", *args], cwd=v.layout.tree, env=v.env,
                                        capture_output=True, text=True, timeout=5)
                self.assertEqual(answer(result), (0, expected))
                self.assertTrue(directory.exists())
        self.assertFalse(directory.exists())
        self.assertEqual(fx.fired(), [])
        self.assertEqual(self.leftovers(), [])

    def test_stale_views_swept_fresh_views_and_symlink_targets_preserved(self):
        fx = self.fixture()
        stale = self.base / "view-stale"
        fresh = self.base / "view-fresh"
        other = self.base / "unrelated"
        for path in (stale, fresh, other):
            path.mkdir()
            (path / "keep").write_text("data")
        old = time.time() - 2 * 24 * 60 * 60
        os.utime(stale, (old, old))
        os.utime(other, (old, old))
        link = self.base / "view-old-link"
        link.symlink_to(other, target_is_directory=True)
        os.utime(link, (old, old), follow_symlinks=False)
        self.assertEqual(self.run_view(fx, ["rev-parse", "HEAD"]).returncode, 0)
        self.assertFalse(stale.exists())
        self.assertEqual((fresh / "keep").read_text(), "data")
        self.assertEqual((other / "keep").read_text(), "data")


class GitlinksTest(FixtureCase):
    def test_gitlink_refuses_status_and_diff_without_running_nested_command(self):
        fx = self.fixture("gitlink")
        prove_control(self, fx, "gitlink")
        self.assertFalse((fx.tree / ".gitmodules").exists())
        self.assertFalse((fx.common / "modules").exists())
        view = self.view
        for args in (["status", "--short"], ["diff"], ["diff", "--stat"]):
            with self.subTest(command=args), self.assertRaisesRegex(view.Unviewable, "submodules are not inspected"):
                self.run_view(fx, args)
            self.assertEqual(fx.fired(), [])
            self.assertEqual(self.leftovers(), [])
        self.assertEqual(answer(self.run_view(fx, ["rev-parse", "HEAD"])), (0, fx.sha["D"] + "\n"))

    def test_untracked_nested_repo_is_reported_without_entering_it(self):
        fx = self.fixture("nested_untracked")
        prove_control(self, fx, "nested_untracked")
        truth = answer(raw(fx, ["status", "--short"]))
        self.assertEqual(answer(self.run_view(fx, ["status", "--short"])), truth)
        self.assertEqual(fx.fired(), [])


class SymbolicHeadBranchNamesTest(FixtureCase):
    ACCEPTED = (
        "feature+foo", "feat@x", "fix/ü", "a,b", "v1.0+build.5",
        "x=y", "x#y", "x%y", "x!y", "x$y",
    )

    def fixture_on_branch(self, name):
        # Pin the table to this machine's Git before creating the branch.
        valid = subprocess.run(["git", "check-ref-format", "--branch", name],
                               cwd=self.root, env=taint.ENV, capture_output=True,
                               text=True, timeout=5)
        self.assertEqual(valid.returncode, 0, valid.stderr)
        self.assertEqual(valid.stdout, name + "\n")
        fx = self.fixture()
        taint.git(["branch", "-m", name], fx.tree)
        return fx

    def test_special_character_branch_upstream_config_is_quoted(self):
        for name in ('a"b', "x]y", "a;b"):
            valid = taint.git(["check-ref-format", "--branch", name], self.root, check=False)
            if valid.returncode == 0:
                self.assertEqual(answer(valid), (0, name + "\n"), valid.stderr)
                break
        else:
            self.fail("Git rejected every special-character branch name")

        fx = self.fixture_on_branch(name)
        upstream = {f"branch.{name}.remote": "origin",
                    f"branch.{name}.merge": "refs/heads/feat"}
        for key, value in upstream.items():
            taint.config(fx, key, value)

        with self.view.opened(fx.tree, git_trees.audit_env(), base=self.base) as v:
            config = Path(v.env["GIT_DIR"]) / "config"
            parsed = taint.git(["config", "-f", str(config), "--list"], self.root, check=False)
            self.assertEqual(parsed.returncode, 0, parsed.stderr)
            for key, value in upstream.items():
                got = taint.git(["config", "-f", str(config), "--get", key],
                                self.root, check=False)
                self.assertEqual(answer(got), (0, value + "\n"), got.stderr)

        for args in (["branch", "--show-current"], ["status", "--short"],
                     ["rev-parse", "--abbrev-ref", "@{u}"]):
            with self.subTest(command=args):
                control = raw(fx, args)
                self.assertEqual(control.returncode, 0, control.stderr)
                got = self.run_view(fx, args)
                self.assertEqual(answer(got), answer(control), got.stderr)
        self.assertEqual(fx.fired(), [])
        self.assertEqual(self.leftovers(), [])

    def test_git_accepted_branch_names_read_status_branch_and_head(self):
        for name in self.ACCEPTED:
            with self.subTest(branch=name):
                fx = self.fixture_on_branch(name)
                reads = (
                    (["status", "--short"], " M README.md\n?? .gitattributes\n?? new.txt\n"),
                    (["branch", "--show-current"], name + "\n"),
                    (["rev-parse", "HEAD"], fx.sha["D"] + "\n"),
                    (["symbolic-ref", "HEAD"], "refs/heads/" + name + "\n"),
                    (["rev-parse", "--abbrev-ref", "HEAD"], name + "\n"),
                )
                for args, stdout in reads:
                    with self.subTest(command=args):
                        control = raw(fx, args)
                        self.assertEqual(answer(control), (0, stdout), control.stderr)
                        got = self.run_view(fx, args)
                        self.assertEqual(answer(got), (0, stdout), got.stderr)
                self.assertEqual(fx.fired(), [])
                self.assertEqual(self.leftovers(), [])

    def test_git_accepted_branch_names_do_not_refuse_one_shot_tree_note(self):
        import one_shot

        view_run = self.view.run

        def run_at_base(*args, **kwargs):
            kwargs.setdefault("base", self.base)
            return view_run(*args, **kwargs)

        for name in self.ACCEPTED:
            with self.subTest(branch=name):
                fx = self.fixture_on_branch(name)
                (fx.root / "CLAUDE.md").write_text("# Workspace\n\n- Worktrees: `.`\n")
                with patch.object(self.view, "run", side_effect=run_at_base), \
                     patch.dict(os.environ, taint.ENV, clear=True):
                    self.assertEqual(one_shot._tree_note(fx.root, fx.tree),
                                     f"worktree `wt` ({name})")
                self.assertEqual(fx.fired(), [])
                self.assertEqual(self.leftovers(), [])

    def test_unsafe_symbolic_head_branch_bytes_are_unviewable(self):
        fx = self.fixture()
        names = (
            b"space name", b"x\\y", b"x~y", b"x^y", b"x:y", b"x?y",
            b"x*y", b"x[y", b"@{u}", b"x@{u}", b"a..b", b"a//b",
            b".hidden/x", b"x/.hidden", b"x.lock", b"x.lock/y", b"x/y.lock",
            b"x/", b"x.", b"-x", b"x\xffy",
        ) + tuple(b"x" + bytes([c]) + b"y" for c in (*range(32), 127))
        for name in names:
            with self.subTest(branch_bytes=name):
                # Git cannot create these branches; emulate worker-written HEAD.
                (fx.gitdir / "HEAD").write_bytes(b"ref: refs/heads/" + name + b"\n")
                with self.assertRaises(self.view.Unviewable):
                    self.run_view(fx, ["branch", "--show-current"])
                self.assertEqual(fx.fired(), [])
                self.assertEqual(self.leftovers(), [])


def edit_readme(case, fx, fsmonitor, racy=True):
    """Leave README.md edited to the same size, with every timestamp pinned.

    racy: the edit lands in the index's own mtime second, so only a content
    compare can see it. Otherwise it lands one second after the cached mtime
    and well before the index's, so a stat compare sees it unless fsmonitor
    lies. With `fsmonitor`, the index is also marked fsmonitor-valid by a
    planted monitor that always reports no change.
    """
    readme = fx.tree / "README.md"
    index = fx.gitdir / "index"
    (fx.tree / "new.txt").unlink()
    (fx.tree / ".gitattributes").unlink()
    stamp = (time.time_ns() // 10**9 - 10) * 10**9 + 250_000_000
    for _ in range(20):  # ctime (not settable) must stay in one second
        readme.write_text("a\n")
        os.utime(readme, ns=(stamp, stamp))
        taint.git(["update-index", "--refresh"], fx.tree)
        before = readme.stat().st_ctime_ns // 10**9
        if fsmonitor:
            code = (f"from pathlib import Path; import sys; "
                    f"Path({str(fx.markers / 'fsmonitor-lie')!r}).write_text('fired'); "
                    "sys.stdout.buffer.write(b'token\\0')")
            taint.config(fx, "core.fsmonitor", shlex.join([sys.executable, "-c", code]))
            taint.git(["update-index", "--fsmonitor"], fx.tree)
            # Only a status with the monitor on marks the entries valid.
            env = git_trees.audit_env()
            env.pop("GIT_OPTIONAL_LOCKS")
            case.assertEqual(answer(raw(fx, ["status", "--short"], env)), (0, ""))
        if racy:
            os.utime(index, ns=(stamp, stamp))
        readme.write_text("z\n")
        edited = stamp if racy else stamp + 10**9
        os.utime(readme, ns=(edited, edited))
        if not racy or readme.stat().st_ctime_ns // 10**9 == before:
            break
    else:
        case.fail("could not keep the edit in the index's ctime second")
    # Precondition: the index carries fsmonitor-valid bits exactly when asked
    # (`ls-files -f` tags those entries in lowercase).
    tags = raw(fx, ["ls-files", "-f"]).stdout.split("\n")
    case.assertEqual(any(t[:1].islower() for t in tags), fsmonitor, tags)
    fx.clear()


class TruthTest(FixtureCase):
    """One named test per vector, with loose/packed x ten command subtests."""

    def test_untainted_truth_has_the_required_history_and_dirt(self):
        fx = self.fixture()
        self.assertEqual(answer(raw(fx, ["status", "--short"])),
                         (0, " M README.md\n?? .gitattributes\n?? new.txt\n"))
        self.assertEqual(answer(raw(fx, ["rev-list", "--count", "origin/main..HEAD"])), (0, "3\n"))
        self.assertEqual(answer(raw(fx, ["rev-list", "--count", "@{u}..HEAD"])), (0, "1\n"))
        self.assertEqual(raw(fx, ["merge-base", "--is-ancestor", fx.sha["D"], "main"]).returncode, 1)
        self.compare_truth(fx)

    def test_sha256_loose_and_packed_answers_equal_truth(self):
        for packed in (False, True):
            with self.subTest(packed=packed):
                fx = self.fixture(sha256=True, packed=packed)
                self.assertEqual(len(fx.sha["D"]), 64)
                self.compare_truth(fx)

    def test_normal_clone_answers_equal_truth(self):
        fx = self.fixture()
        self.compare_truth(fx, path=fx.clone)

    def test_split_index_answers_equal_truth(self):
        fx = self.fixture()
        taint.git(["update-index", "--split-index"], fx.tree)
        self.assertTrue(list(fx.gitdir.glob("sharedindex.*")))
        self.compare_truth(fx)

    def test_sparse_cone_and_sparse_index_answers_equal_truth(self):
        fx = self.fixture()
        (fx.tree / "kept").mkdir()
        (fx.tree / "omitted").mkdir()
        for directory in ("kept", "omitted"):
            (fx.tree / directory / "file.txt").write_text("data\n")
        taint.git(["add", "kept", "omitted"], fx.tree)
        taint.git(["commit", "-qm", "directories"], fx.tree)
        taint.git(["sparse-checkout", "init", "--cone", "--sparse-index"], fx.tree)
        taint.git(["sparse-checkout", "set", "kept"], fx.tree)
        self.assertFalse((fx.tree / "omitted").exists())
        sparse = taint.git(["ls-files", "--sparse", "--stage"], fx.tree).stdout
        self.assertIn("040000 ", sparse)
        self.compare_truth(fx)

    def test_detached_and_unborn_heads_equal_truth_including_nonzero_codes(self):
        for shape in ("detached", "unborn"):
            with self.subTest(shape=shape):
                fx = self.fixture()
                if shape == "detached":
                    taint.git(["checkout", "--detach", "-q"], fx.tree)
                else:
                    taint.git(["checkout", "--orphan", "unborn", "-q"], fx.tree)
                    self.assertNotEqual(raw(fx, ["rev-parse", "HEAD"]).returncode, 0)
                self.compare_truth(fx)

    def test_nondefault_fetch_refspec_and_dot_remote_upstream_equal_truth(self):
        for shape in ("single-branch", "dot-remote"):
            with self.subTest(shape=shape):
                fx = self.fixture()
                if shape == "single-branch":
                    taint.config(fx, "remote.origin.fetch", "+refs/heads/feat:refs/remotes/origin/feat")
                    count = "1\n"
                else:
                    taint.config(fx, "branch.feat.remote", ".")
                    taint.config(fx, "branch.feat.merge", "refs/heads/main")
                    count = "2\n"
                self.assertEqual(answer(raw(fx, ["rev-list", "--count", "@{u}..HEAD"])), (0, count))
                self.compare_truth(fx)

    def test_fsmonitor_lie_in_index_cannot_hide_modified_file(self):
        fx = self.fixture()
        # Start clean, let a real fsmonitor protocol response mark all entries
        # valid, then change a file while the monitor reports no changes.
        edit_readme(self, fx, fsmonitor=True, racy=False)
        env = git_trees.audit_env()
        env.pop("GIT_OPTIONAL_LOCKS")
        self.assertEqual(answer(raw(fx, ["status", "--short"], env)), (0, ""))
        self.assertIn("fsmonitor-lie", fx.fired())
        fx.clear()
        result = self.run_view(fx, ["status", "--short"])
        self.assertEqual(answer(result), (0, " M README.md\n"))
        self.assertEqual(fx.fired(), [])

    def test_same_second_same_size_edit_with_fsmonitor_valid_index_shows_modified(self):
        # A tracked file changed in the same second as the index write, with the
        # same size and an fsmonitor-valid index, still shows as modified in the
        # view. Every timestamp is pinned, so no sleep or clock luck is involved.
        for fsmonitor in (True, False):
            with self.subTest(fsmonitor_valid=fsmonitor):
                fx = self.fixture()
                edit_readme(self, fx, fsmonitor)
                # Plain Git, fsmonitor off, never rewrites the index: the entry is racy.
                plain = raw(fx, ["-c", "core.fsmonitor=false", "status", "--short"],
                            git_trees.audit_env())
                self.assertEqual(answer(plain), (0, " M README.md\n"))
                result = self.run_view(fx, ["status", "--short"])
                self.assertEqual(answer(result), (0, " M README.md\n"))
                self.assertEqual(fx.fired(), [])


def vector_test(vector):
    def test(self):
        for packed in (False, True):
            with self.subTest(vector=vector, packed=packed):
                fx = self.fixture(packed=packed)
                truth = {tuple(args): answer(raw(fx, args)) for args in commands(fx)}
                taint.apply(fx, vector)
                prove_control(self, fx, vector)
                if vector == "nested_untracked":
                    truth[("status", "--short")] = answer(raw(fx, ["status", "--short"]))
                view = self.view
                for args in commands(fx):
                    with self.subTest(command=args):
                        if vector in ("commondir", "gitfile", "reftable_marker") or (vector == "gitlink" and args[0] == "status"):
                            with self.assertRaises(view.Unviewable):
                                self.run_view(fx, args)
                        else:
                            result = self.run_view(fx, args)
                            self.assertEqual(answer(result), truth[tuple(args)], result.stderr)
                        self.assertEqual(fx.fired(), [])
                        self.assertEqual(self.leftovers(), [])
                if vector == "grafts_yes":
                    # Pin the security consequence directly: an unmerged D
                    # must not become an ancestor of main because of grafts.
                    result = self.run_view(fx, ["merge-base", "--is-ancestor", fx.sha["D"], "main"])
                    self.assertEqual(answer(result), (1, ""))
                    self.assertEqual(fx.fired(), [])
    test.__name__ = "test_vector_" + vector
    return test


for _vector in taint.VECTORS:
    setattr(TruthTest, "test_vector_" + _vector, vector_test(_vector))


GITLINK_READS = {
    "status": ["status", "--short"],
    "diff": ["diff", "--ignore-submodules=none"],
    "diff_files": ["diff-files", "--ignore-submodules=none"],
    "diff_index": ["diff-index", "--ignore-submodules=none", "HEAD"],
    "describe": ["describe", "--always", "--dirty"],
    "add": ["add", "-n", "."],
}


def object_reads():
    return {
        "rev-parse": ["rev-parse", "HEAD"],
        "rev-list": ["rev-list", "--count", "HEAD"],
        "merge-base": ["merge-base", "--is-ancestor", "main", "HEAD"],
        "cat-file": ["cat-file", "-p", "HEAD:README.md"],
        "for-each-ref": ["for-each-ref", "--format=%(refname) %(objectname)"],
        "log": ["log", "--format=%H %s"],
        "show": ["show", "HEAD:README.md"],
        "ls-tree": ["ls-tree", "HEAD"],
        "branch": ["branch", "--show-current"],
        "symbolic-ref": ["symbolic-ref", "HEAD"],
        "ls-files": ["ls-files", "--stage"],
    }


def gitlink_review_test(name, *, guarded):
    def test(self):
        fx = self.fixture()
        taint.committed_gitlink(fx)
        args = GITLINK_READS[name]
        result = raw(fx, args)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("nested-fsmonitor", fx.fired(), "the exact command must enter the nested repo")
        fx.clear()
        if guarded:
            with self.assertRaises(self.view.Unviewable):
                self.run_view(fx, args)
            self.assertEqual(fx.fired(), [])
            self.assertEqual(self.leftovers(), [])
    return test


class ReviewControlsTest(FixtureCase):
    """Named controls stay green independently of the production fixes."""


class ReviewCommandsTest(FixtureCase):
    def assert_no_git(self, fx, args, *, env=None, path=None):
        view = self.view
        # Even the config parser and gitlink preflight must not run when the
        # command or options themselves have already violated the boundary.
        with patch.object(subprocess, "run", side_effect=AssertionError("rejected arguments started git")) as start:
            with self.assertRaises(view.Unviewable):
                view.run(args, path or fx.tree, env=env or git_trees.audit_env(), base=self.base)
        start.assert_not_called()
        self.assertEqual(fx.fired(), [])
        self.assertEqual(self.leftovers(), [])

    def test_r1_exact_read_only_allowlist_keeps_inventory_commands_working(self):
        fx = self.fixture()
        reads = {**object_reads(), "status": ["status", "--short"],
                 "diff": ["diff", "--no-ext-diff"],
                 "check-ref-format": ["check-ref-format", "--branch", "candidate"]}
        self.assertEqual(set(reads), READ_ONLY_COMMANDS)
        for command, args in reads.items():
            with self.subTest(command=command):
                control = raw(fx, args)
                self.assertEqual(control.returncode, 0, control.stderr)
                self.assertEqual(answer(self.run_view(fx, args)), answer(control))
        self.assertEqual(fx.fired(), [])

    def test_r1_commands_outside_allowlist_refuse_before_any_subprocess(self):
        fx = self.fixture()
        # A real write control proves that handing arbitrary commands to git
        # is a live mutation, even though the launcher currently asks for reads.
        ref = fx.common / "refs/heads/injected"
        self.assertFalse(ref.exists())
        self.assertEqual(raw(fx, ["update-ref", "refs/heads/injected", fx.sha["D"]]).returncode, 0)
        self.assertEqual(ref.read_text(), fx.sha["D"] + "\n")
        ref.unlink()
        disallowed = {
            "add": ["add", "-n", "."], "diff-files": ["diff-files"],
            "diff-index": ["diff-index", "HEAD"], "describe": ["describe", "--always"],
            "grep": ["grep", "a", "HEAD"], "config": ["config", "--list"],
            "update-ref": ["update-ref", "refs/heads/injected", fx.sha["D"]],
            "tag": ["tag", "injected"], "checkout": ["checkout", "main"],
            "commit": ["commit", "--allow-empty", "-m", "injected"],
            "fetch": ["fetch", "origin"], "push": ["push", "origin"],
            "worktree": ["worktree", "list"], "submodule": ["submodule", "status"],
            "reset": ["reset", "--hard"], "clean": ["clean", "-fd"],
            "clone": ["clone", str(fx.clone)], "init": ["init"],
            "stash": ["stash", "list"], "help": ["help"],
            "version": ["version"], "not-a-command": ["not-a-command"],
        }
        self.assertTrue(READ_ONLY_COMMANDS.isdisjoint(disallowed))
        for command, args in disallowed.items():
            with self.subTest(command=command):
                self.assert_no_git(fx, args)

    def test_r1_object_only_commands_keep_working_with_gitlinks(self):
        fx = self.fixture()
        taint.committed_gitlink(fx)
        prove_control(self, fx, "gitlink")
        reads = object_reads()
        self.assertEqual(set(reads), OBJECT_ONLY)
        for command, args in reads.items():
            with self.subTest(command=command):
                control = raw(fx, args)
                self.assertEqual(control.returncode, 0, control.stderr)
                self.assertEqual(answer(self.run_view(fx, args)), answer(control))
                self.assertEqual(fx.fired(), [])

    def test_r1_every_allowed_command_outside_object_only_set_checks_gitlinks(self):
        fx = self.fixture("gitlink")
        prove_control(self, fx, "gitlink")
        self.assertEqual(READ_ONLY_COMMANDS - OBJECT_ONLY, {"status", "diff", "check-ref-format"})
        args = ["check-ref-format", "--branch", "candidate"]
        self.assertEqual(answer(raw(fx, args)), (0, "candidate\n"))
        with self.assertRaises(self.view.Unviewable):
            self.run_view(fx, args)
        self.assertEqual(fx.fired(), [])

    def test_r2_leading_options_cannot_hide_status_after_the_first_argument(self):
        fx = self.fixture("gitlink")
        args = ["-c", "color.ui=false", "status", "--short"]
        self.assertEqual(raw(fx, args).returncode, 0)
        self.assertIn("nested-fsmonitor", fx.fired())
        fx.clear()
        self.assert_no_git(fx, args)

    def test_r2_all_leading_options_are_refused(self):
        fx = self.fixture()
        taint.config(fx, "core.fsmonitor", fx.command("option-fsmonitor", 1))
        executable = taint.marker_program(fx, "option-exec", name="git-x")
        cases = (
            (["-c", "core.fsmonitor=" + fx.command("option-config", 1), "status"], "option-config", {}),
            (["-c", "alias.x=!" + fx.command("option-alias"), "x"], "option-alias", {}),
            (["--git-dir", str(fx.clone / ".git"), "status"], "option-fsmonitor", {}),
            (["-C", str(fx.clone), "status"], "option-fsmonitor", {}),
            (["--exec-path=" + str(executable.parent), "x"], "option-exec", {}),
            (["--work-tree", str(fx.clone), "status"], "option-fsmonitor", {}),
            (["--namespace", "injected", "rev-parse", "HEAD"], None, {}),
            (["--config-env", "core.fsmonitor=PROBE_COMMAND", "status"], "option-env",
             {"PROBE_COMMAND": fx.command("option-env", 1)}),
            (["--no-pager", "rev-parse", "HEAD"], None, {}),
        )
        for args, marker, extra in cases:
            with self.subTest(option=args[0], command=args[-1]):
                env = {**git_trees.audit_env(), **extra}
                control = raw(fx, args, env, path=fx.clone)
                if marker:
                    self.assertIn(marker, fx.fired(), control.stderr)
                else:
                    self.assertEqual(answer(control), (0, fx.sha["B"] + "\n"))
                fx.clear()
                self.assert_no_git(fx, args, env=env, path=fx.clone)

    def test_r2_output_options_cannot_write_a_file(self):
        fx = self.fixture()
        output = fx.root / "written.diff"
        args = ["diff", "--output=" + str(output)]
        control = raw(fx, args)
        self.assertEqual(control.returncode, 0, control.stderr)
        self.assertIn("diff --git", output.read_text())
        output.unlink()
        self.assert_no_git(fx, args)
        self.assertFalse(output.exists())

    def test_r2_repository_and_output_option_prefixes_are_refused_anywhere(self):
        fx = self.fixture()
        output = fx.root / "written.diff"
        # Live output control, then exercise the lexical rule even after --,
        # where git would ordinarily interpret these tokens as path operands.
        self.assertEqual(raw(fx, ["diff", "--output", str(output)]).returncode, 0)
        self.assertIn("diff --git", output.read_text())
        output.unlink()
        for prefix in ("--git-dir", "--work-tree", "--output"):
            for token in (prefix, prefix + "=" + str(fx.clone / ".git"), prefix + "-extra"):
                for args in (["rev-parse", token, "HEAD"], ["diff", "HEAD", "--", token]):
                    with self.subTest(args=args):
                        self.assert_no_git(fx, args)
        self.assertFalse(output.exists())


for _name in GITLINK_READS:
    setattr(ReviewControlsTest, "test_control_r1_gitlink_" + _name,
            gitlink_review_test(_name, guarded=False))
    setattr(ReviewCommandsTest, "test_r1_gitlink_refuses_" + _name,
            gitlink_review_test(_name, guarded=True))


def caller_environment_control(case, name):
    """Return an attack only after proving its concrete effect with real git."""
    fx = case.fixture()
    env = git_trees.audit_env()
    args = ["status", "--short"]
    expected = answer(raw(fx, args))
    if name == "common_dir":
        taint.config(fx, "core.fsmonitor", fx.command("env-common", 1))
        env["GIT_COMMON_DIR"] = str(fx.common)
        control = raw(fx, args, env)
        case.assertIn("env-common", fx.fired(), control.stderr)
    elif name == "config_count":
        env.update(GIT_CONFIG_COUNT="1", GIT_CONFIG_KEY_0="core.fsmonitor",
                   GIT_CONFIG_VALUE_0=fx.command("env-count", 1))
        control = raw(fx, args, env)
        case.assertIn("env-count", fx.fired(), control.stderr)
    elif name == "config_parameters":
        probe = taint.marker_program(fx, "env-parameters")
        env["GIT_CONFIG_PARAMETERS"] = "'core.fsmonitor=" + str(probe) + "'"
        control = raw(fx, args, env)
        case.assertIn("env-parameters", fx.fired(), control.stderr)
    elif name == "object_directory":
        empty = fx.root / "empty-objects"
        empty.mkdir()
        env["GIT_OBJECT_DIRECTORY"] = str(empty)
        args = ["cat-file", "-p", "HEAD:README.md"]
        expected = answer(raw(fx, args))
        case.assertEqual(expected, (0, "a\n"))
        control = raw(fx, args, env)
        case.assertNotEqual(control.returncode, 0)
        case.assertEqual(control.stdout, "")
    elif name == "alternate_object_directories":
        other = case.fixture()
        payload = other.root / "alternate-only.txt"
        payload.write_text("alternate-only blob\n")
        oid = taint.git(["hash-object", "-w", str(payload)], other.clone).stdout.strip()
        env["GIT_ALTERNATE_OBJECT_DIRECTORIES"] = str(other.common / "objects")
        args = ["cat-file", "-p", oid]
        expected = answer(raw(fx, args))
        case.assertNotEqual(expected[0], 0)
        case.assertEqual(expected[1], "")
        case.assertEqual(answer(raw(fx, args, env)), (0, payload.read_text()))
    elif name == "namespace":
        taint.git(["update-ref", "refs/namespaces/injected/refs/heads/only", fx.sha["B"]], fx.clone)
        env["GIT_NAMESPACE"] = "injected"
        # Ordinary rev-parse need not honour namespaces; upload-pack does.
        # This makes the env control live without granting upload-pack to run().
        advertise = ["upload-pack", "--advertise-refs", str(fx.common)]
        plain = raw(fx, advertise)
        control = raw(fx, advertise, env)
        case.assertEqual(control.returncode, 0, control.stderr)
        case.assertIn(fx.sha["D"], plain.stdout)
        case.assertIn("refs/heads/only", control.stdout)
        case.assertNotIn(fx.sha["D"], control.stdout)
        args = ["rev-parse", "HEAD"]
        expected = (0, fx.sha["D"] + "\n")
    else:
        case.fail("unknown caller env control: " + name)
    fx.clear()
    return fx, args, env, expected


def caller_environment_test(name, *, guarded):
    def test(self):
        fx, args, env, expected = caller_environment_control(self, name)
        if not guarded:
            return
        caller = dict(env)
        with patch.object(subprocess, "run", wraps=subprocess.run) as started:
            result = self.view.run(args, fx.tree, env=env, base=self.base)
        self.assertEqual(answer(result), expected, result.stderr)
        self.assertEqual(fx.fired(), [])
        for call in started.call_args_list:
            child = call.kwargs["env"]
            self.assertTrue(set(child).isdisjoint(set(env) - SAFE_CALLER_GIT_ENV - {"PATH", "HOME"}),
                            "caller GIT_* reached a subprocess (including config parsing)")
        with self.view.opened(fx.tree, env, base=self.base) as v:
            self.assertEqual({key for key in v.env if key.startswith("GIT_")},
                             (set(env) & SAFE_CALLER_GIT_ENV) | {"GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"})
        self.assertEqual(env, caller, "scrubbing must not mutate the caller's mapping")
        self.assertEqual(self.leftovers(), [])
    return test


class ReviewEnvironmentTest(FixtureCase):
    def test_r3_all_non_allowlisted_git_variables_are_dropped_and_view_paths_are_authored(self):
        fx = self.fixture()
        trace = fx.root / "caller.trace"
        env = {**git_trees.audit_env(), "GIT_DIR": str(fx.common),
               "GIT_WORK_TREE": str(fx.clone), "GIT_INDEX_FILE": str(fx.common / "index"),
               "GIT_TRACE": str(trace), "GIT_FUTURE_UNTRUSTED_SETTING": "injected"}
        home = fx.root / "caller-home"
        home.mkdir()
        env.update(HOME=str(home), PATH=env["PATH"] + os.pathsep + str(fx.root))
        # These selectors would choose main's B rather than linked feat's D;
        # TRACE also proves an otherwise unenumerated GIT_* variable is live.
        self.assertEqual(answer(raw(fx, ["rev-parse", "HEAD"], env)), (0, fx.sha["B"] + "\n"))
        self.assertIn("rev-parse HEAD", trace.read_text())
        trace.unlink()
        caller = dict(env)
        with patch.object(subprocess, "run", wraps=subprocess.run) as started:
            with self.view.opened(fx.tree, env, base=self.base) as v:
                directory = Path(v.env["GIT_DIR"])
                self.assertEqual(directory.parent, self.base)
                self.assertEqual(v.env["GIT_WORK_TREE"], str(fx.tree))
                self.assertEqual(v.env["GIT_INDEX_FILE"], str(directory / "index"))
                self.assertEqual(v.env["PATH"], env["PATH"])
                self.assertEqual(v.env["HOME"], env["HOME"])
                self.assertEqual({k: value for k, value in v.env.items() if k in SAFE_CALLER_GIT_ENV},
                                 {k: value for k, value in env.items() if k in SAFE_CALLER_GIT_ENV})
                self.assertEqual({k for k in v.env if k.startswith("GIT_")},
                                 (set(env) & SAFE_CALLER_GIT_ENV) | {"GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"})
                result = subprocess.run(["git", "rev-parse", "HEAD"], cwd=fx.tree, env=v.env,
                                        capture_output=True, text=True, timeout=5)
                self.assertEqual(answer(result), (0, fx.sha["D"] + "\n"))
        for call in started.call_args_list:
            self.assertNotIn("GIT_TRACE", call.kwargs["env"])
            self.assertNotIn("GIT_FUTURE_UNTRUSTED_SETTING", call.kwargs["env"])
        self.assertFalse(trace.exists())
        self.assertEqual(env, caller)
        self.assertEqual(self.leftovers(), [])

    def test_r4_log_uses_true_history_without_caller_no_replace_objects(self):
        fx = self.fixture("replace")
        env = git_trees.audit_env()
        env.pop("GIT_NO_REPLACE_OBJECTS")
        args = ["log", "--format=%s", "HEAD"]
        self.assertEqual(answer(raw(fx, args, env)), (0, "B\nA\n"))
        self.assertEqual(answer(raw(fx, ["-c", "core.useReplaceRefs=false", *args], env)),
                         (0, "D\nC\nB\nA\n"))
        result = self.view.run(args, fx.tree, env=env, base=self.base)
        self.assertEqual(answer(result), (0, "D\nC\nB\nA\n"), result.stderr)
        with self.view.opened(fx.tree, env, base=self.base) as v:
            config = taint.git(["config", "--file", str(Path(v.env["GIT_DIR"]) / "config"),
                                "--get", "core.useReplaceRefs"], fx.root, check=False)
            self.assertEqual(answer(config), (0, "false\n"))
        self.assertEqual(self.leftovers(), [])


for _name in ("common_dir", "config_count", "config_parameters", "object_directory",
              "namespace", "alternate_object_directories"):
    setattr(ReviewControlsTest, "test_control_r3_" + _name,
            caller_environment_test(_name, guarded=False))
    setattr(ReviewEnvironmentTest, "test_r3_drops_" + _name,
            caller_environment_test(_name, guarded=True))


class ReviewLayoutsTest(FixtureCase):
    assert_refused_without_git = LocateTest.assert_refused_without_git

    def test_r5_forged_consistent_triple_inside_tree_cannot_forge_ancestry(self):
        fx = self.fixture()
        args = ["merge-base", "--is-ancestor", "HEAD", "main"]
        self.assertEqual(answer(raw(fx, args)), (1, ""))
        self.assertEqual(answer(self.run_view(fx, args)), (1, ""))
        common, gitdir = taint.forged_linked_layout(fx)
        self.assertTrue(common.is_relative_to(fx.tree))
        self.assertTrue(gitdir.is_relative_to(fx.tree))
        self.assertEqual((gitdir / "commondir").read_text(), "../..\n")
        self.assertEqual((gitdir / "gitdir").read_text(), str(fx.tree / ".git") + "\n")
        self.assertEqual(answer(raw(fx, args)), (0, ""), "the forged triple must fool unguarded ancestry")
        self.assert_refused_without_git(fx.tree)
        with patch.object(subprocess, "run", side_effect=AssertionError("forged ancestry started git")) as start:
            with self.assertRaises(self.view.Unviewable):
                self.run_view(fx, args)
        start.assert_not_called()
        self.assertEqual(self.leftovers(), [])

    def test_r5_gitdir_inside_tree_is_refused_even_with_common_outside(self):
        fx = self.fixture()
        # The worktrees directory can itself contain a forged worktree. Its
        # gitdir is inside that tree while common remains outside it; all three
        # pointers still satisfy the old equality checks.
        tree = fx.common / "worktrees"
        gitdir = tree / "evil"
        shutil.copytree(fx.gitdir, gitdir)
        (gitdir / "commondir").write_text("../..\n")
        (gitdir / "gitdir").write_text(str(tree / ".git") + "\n")
        (tree / ".git").write_text(f"gitdir: {gitdir}\n")
        taint.config(fx, "core.fsmonitor", fx.command("inside-gitdir", 1))
        control = raw(fx, ["status", "--short"], path=tree)
        self.assertIn("inside-gitdir", fx.fired(), control.stderr)
        fx.clear()
        self.assertFalse(fx.common.is_relative_to(tree))
        self.assertTrue(gitdir.is_relative_to(tree))
        self.assert_refused_without_git(tree)


class ReviewBaseTest(FixtureCase):
    def assert_default_cache_refused(self, cache):
        fx = self.fixture()
        view = self.view
        layout = view.locate(fx.tree)
        candidate = (Path(cache) / "chief-of-stuff/git-views").resolve()
        # Allocation spies keep /var/tmp and other global locations untouched.
        # Explicit base is the real guard-bypass control: it must accept the
        # identical resolved path and attempt allocation. Only default trust
        # changes between the two calls; no fake resolver or fake predicate.
        real_scandir = os.scandir
        with patch.object(Path, "mkdir") as mkdir, patch.object(Path, "chmod"), \
                patch.object(os, "scandir", side_effect=lambda path: real_scandir(self.base)), \
                patch.object(tempfile, "gettempdir", return_value=str(self.root / "other-system-temp")), \
                patch.dict(os.environ, {"XDG_CACHE_HOME": str(cache)}):
            self.assertEqual(view._base(layout, candidate), candidate)
            mkdir.assert_called_once()
            mkdir.reset_mock()
            with self.assertRaises(view.Unviewable):
                view._base(layout, None)
            mkdir.assert_not_called()

    def test_r7_default_cache_refuses_all_system_temp_roots_and_resolved_forms(self):
        # Include both macOS spellings explicitly, even on systems where /tmp
        # is a real directory. Do not let tempfile.gettempdir mask this rule.
        roots = (Path("/tmp"), Path("/var/tmp"), Path("/private/tmp"), Path("/private/var/tmp"))
        for cache in (*roots, *(root.resolve() for root in roots)):
            with self.subTest(cache=str(cache)):
                self.assert_default_cache_refused(cache)

    def test_r7_relative_xdg_cache_home_is_refused(self):
        self.assert_default_cache_refused(Path("relative-cache"))

    def test_r7_explicit_base_is_trusted_but_resolved_worker_roots_are_refused(self):
        fx = self.fixture()
        # A real tmp-based allocation is permitted by explicit launcher trust.
        self.assertEqual(answer(self.run_view(fx, ["rev-parse", "HEAD"])), (0, fx.sha["D"] + "\n"))
        for number, parent in enumerate((fx.tree, fx.common, fx.gitdir)):
            alias = self.root / f"alias-{number}"
            alias.symlink_to(parent, target_is_directory=True)
            for base in (parent, parent / "views", alias, alias / "views"):
                with self.subTest(base=base), patch.object(subprocess, "run", wraps=subprocess.run) as started:
                    with self.assertRaises(self.view.Unviewable):
                        self.view.run(["rev-parse", "HEAD"], fx.tree, env=git_trees.audit_env(), base=base)
                    started.assert_not_called()
        self.assertEqual(self.leftovers(), [])


def object_store(path):
    return {p.relative_to(path).as_posix(): p.read_bytes() for p in path.rglob("*") if p.is_file()}


def partial_clone_review_test(*, guarded):
    def test(self):
        fx = self.fixture()
        try:
            control, blob = taint.partial_clone(fx, "partial-control")
        except subprocess.CalledProcessError as error:
            self.skipTest("could not build a file:// blob:none partial clone: " + error.stderr.strip())
        missing = taint.git(["rev-list", "--objects", "--missing=print", "HEAD"], control).stdout
        if "?" + blob not in missing.splitlines():
            self.skipTest("git ignored blob:none; could not build a missing-blob partial clone")
        objects = control / ".git/objects"
        before = object_store(objects)
        fx.clear()
        trace = fx.root / "git-child.trace"
        fetched = taint.git(["cat-file", "-p", blob], control,
                            env={**git_trees.audit_env(), "GIT_TRACE": str(trace)})
        self.assertEqual(answer(fetched), (0, "a\n"))
        self.assertIn("lazy-fetch", fx.fired())
        self.assertRegex(trace.read_text(), r"\bfetch\b", "trace must observe the control's internal lazy fetch")
        trace.unlink()
        self.assertNotEqual(object_store(objects), before)
        self.assertNotIn("?" + blob, taint.git(["rev-list", "--objects", "--missing=print", "HEAD"], control).stdout)
        fx.clear()
        if not guarded:
            return
        clone, missing_blob = taint.partial_clone(fx, "partial-view")
        self.assertEqual(missing_blob, blob)
        missing = taint.git(["rev-list", "--objects", "--missing=print", "HEAD"], clone).stdout
        self.assertIn("?" + blob, missing.splitlines())
        before = object_store(clone / ".git/objects")
        real_run = subprocess.run
        def traced_run(argv, **kwargs):
            # Inject observation AFTER the view has scrubbed caller env. Git's
            # internal fetch is not visible to a Python subprocess call spy.
            kwargs["env"] = {**kwargs["env"], "GIT_TRACE": str(trace)}
            return real_run(argv, **kwargs)
        with patch.object(subprocess, "run", side_effect=traced_run):
            result = self.view.run(["cat-file", "-p", blob], clone, env=git_trees.audit_env(), base=self.base)
        self.assertNotEqual(result.returncode, 0, "the view must not lazily retrieve a missing object")
        self.assertEqual(result.stdout, "")
        self.assertTrue(result.stderr)
        observed = trace.read_text()
        self.assertIn("cat-file", observed)
        self.assertNotRegex(observed, r"\b(?:fetch|fetch-pack|upload-pack)\b", "even a failed lazy fetch is forbidden")
        self.assertEqual(fx.fired(), [])
        self.assertEqual(object_store(clone / ".git/objects"), before, "nothing may be fetched into the source store")
        self.assertIn("?" + blob, taint.git(["rev-list", "--objects", "--missing=print", "HEAD"], clone).stdout.splitlines())
        self.assertEqual(self.leftovers(), [])
    return test


class ReviewPartialCloneTest(FixtureCase):
    test_r9_partial_clone_missing_blob_fails_closed_without_lazy_fetch = partial_clone_review_test(guarded=True)


ReviewControlsTest.test_control_r9_partial_clone_really_lazy_fetches = partial_clone_review_test(guarded=False)


class SignalsTest(FixtureCase):
    def test_signals_read_real_grafts_and_shallow_not_view_metadata(self):
        for vector, expected in ((None, frozenset()), ("grafts", frozenset({"grafts"})),
                                 ("grafts_yes", frozenset({"grafts"})), ("shallow", frozenset({"shallow"}))):
            with self.subTest(vector=vector):
                fx = self.fixture(vector)
                if vector:
                    prove_control(self, fx, vector)
                view = self.view
                layout = view.locate(fx.tree)
                with patch.object(subprocess, "run", side_effect=AssertionError("signals started git")):
                    self.assertEqual(view.signals(layout), expected)
                with view.opened(fx.tree, git_trees.audit_env(), base=self.base) as v:
                    private = Path(v.env["GIT_DIR"])
                    self.assertFalse((private / "shallow").exists())
                    self.assertFalse((private / "info/grafts").exists())
                self.assertEqual(view.signals(layout), expected)

    def test_signals_empty_grafts_present_empty_shallow_and_missing_files(self):
        fx = self.fixture()
        view = self.view
        layout = view.locate(fx.tree)
        grafts = fx.common / "info/grafts"
        shallow = fx.common / "shallow"
        grafts.write_bytes(b"")
        self.assertEqual(view.signals(layout), frozenset())
        shallow.write_bytes(b"")
        self.assertEqual(view.signals(layout), frozenset({"shallow"}))
        grafts.write_bytes(b"worker data")
        self.assertEqual(view.signals(layout), frozenset({"grafts", "shallow"}))
        grafts.unlink()
        shallow.unlink()
        self.assertEqual(view.signals(layout), frozenset())
        shutil.rmtree(fx.common / "info")
        self.assertEqual(view.signals(layout), frozenset())


# R13 docstring expectation recorded: workers cannot write metadata outside their tree; expected-common validation is filed.

# Exact public vocabulary: inventory reads plus the existing suite's --stage/-z
# and --is-shallow-repository probes. Internal config/index preflights are not
# public commands. Preserve the existing reserved output/repository-token rule.
OPTION_VOCABULARY = {
    "status": {"--short", "--porcelain", "--ignored"},
    "rev-parse": {"--git-common-dir", "--abbrev-ref", "--is-shallow-repository",
                  "--verify", "--quiet", "-q", "--short", "--path-format",
                  "--disambiguate", "--git-path", "--show-toplevel", "--absolute-git-dir"},
    "log": {"--format", "--stat", "--no-ext-diff", "--no-textconv", "--no-color", "-1"},
    "merge-base": {"--is-ancestor"},
    "diff": {"--stat", "--name-only", "--numstat", "--no-ext-diff", "--no-textconv",
             "--no-color", "--no-renames"},
    "show": {"--stat", "--format"},
    "for-each-ref": {"--format"},
    "ls-files": {"--stage", "-z"},
    "ls-tree": {"-r"},
    "cat-file": {"-p", "-t", "-e", "--batch-check"},
    "rev-list": {"--count", "--left-right"},
    "branch": {"--show-current"},
    "symbolic-ref": {"-q", "--short"},
    "check-ref-format": {"--branch"},
}
# PATH/HOME are the only caller non-GIT names. No editor, pager, loader,
# locale, trace, or future arbitrary variable is inherited by any subprocess.
CHILD_ENV_ALLOWLIST = SAFE_CALLER_GIT_ENV | {
    "PATH", "HOME", "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE",
}
MODULE_CONFIG_PINS = {"GIT_CONFIG_GLOBAL": os.devnull,
                      "GIT_CONFIG_SYSTEM": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}


class RoundTwoControlsTest(FixtureCase):
    """Every attack control runs real Git independently of the guarded test."""


class RoundTwoCommandsTest(FixtureCase):
    assert_no_git = ReviewCommandsTest.assert_no_git


def historical_submodule_test(command, *, guarded):
    def test(self):
        fx = self.fixture()
        taint.historical_gitlink(fx)
        self.assertNotIn("160000 ", raw(fx, ["ls-files", "--stage"]).stdout)
        self.assertIn("160000 commit", raw(fx, ["ls-tree", "HEAD", "gl"]).stdout)
        args = [command, "-p", "--ignore-submodules=none", "--submodule=diff"]
        control = raw(fx, args)
        self.assertEqual(control.returncode, 0, control.stderr)
        self.assertIn("nested-external-diff", fx.fired(), control.stderr)
        fx.clear()
        if not guarded:
            return
        # --no-ext-diff on the parent cannot guard a nested Git process.
        for attack in (args, [*args, "--no-ext-diff"]):
            with self.subTest(args=attack):
                self.assert_no_git(fx, attack)
        for safe in ([command], [command, "--stat", "--format=%s"]):
            with self.subTest(read=safe):
                # A safe view intentionally omits gitlink patches/stats. Use
                # the same policy for truth, while still reading real history.
                control = raw(fx, ["-c", "diff.ignoreSubmodules=all", *safe])
                self.assertEqual(control.returncode, 0, control.stderr)
                result = self.run_view(fx, safe)
                self.assertEqual(answer(result), answer(control))
                self.assertIn("nested-two", result.stdout)
                self.assertEqual(fx.fired(), [])
        self.assertEqual(self.leftovers(), [])
    return test


for _command in ("log", "show"):
    setattr(RoundTwoControlsTest, f"test_control_r10_{_command}_historical_gitlink",
            historical_submodule_test(_command, guarded=False))
    setattr(RoundTwoCommandsTest, f"test_r10_{_command}_historical_gitlink_refuses_nested_diff",
            historical_submodule_test(_command, guarded=True))


def ref_write_fixture(case, form):
    fx = case.fixture()
    taint.git(["branch", "victim", fx.sha["B"]], fx.tree)
    args = {
        "branch_delete": ["branch", "-D", "victim"],
        "branch_create": ["branch", "candidate"],
        "branch_force": ["branch", "-f", "victim", fx.sha["A"]],
        "branch_description": ["branch", "--edit-description"],
        "symbolic_head_write": ["symbolic-ref", "HEAD", "refs/heads/victim"],
        "symbolic_shared_write": ["symbolic-ref", "refs/heads/alias", "refs/heads/victim"],
        "symbolic_shared_delete": ["symbolic-ref", "-d", "refs/heads/alias"],
    }[form]
    if form == "symbolic_shared_delete":
        taint.git(["symbolic-ref", "refs/heads/alias", "refs/heads/victim"], fx.tree)
    return fx, args, {**git_trees.audit_env(), "EDITOR": fx.command("editor")}


def ref_write_test(form, *, guarded):
    def test(self):
        fx, args, env = ref_write_fixture(self, form)
        before = taint.metadata_snapshot(fx)
        control = raw(fx, args, env)
        self.assertEqual(control.returncode, 0, control.stderr)
        if form == "branch_description":
            self.assertIn("editor", fx.fired())
        else:
            self.assertNotEqual(taint.metadata_snapshot(fx), before, "control must really change metadata")
        if not guarded:
            return
        fx, args, env = ref_write_fixture(self, form)
        before = taint.metadata_snapshot(fx)
        refused = False
        try:
            self.view.run(args, fx.tree, env=env, base=self.base)
        except self.view.Unviewable:
            refused = True
        # Check actual bytes even when the command failed to refuse. In
        # particular HEAD writes affect the private copy, shared refs do not.
        self.assertEqual(taint.metadata_snapshot(fx), before, "real refs/HEAD/config/reflogs were modified")
        self.assertEqual(fx.fired(), [], "caller editor executed")
        self.assertEqual(self.leftovers(), [])
        self.assertTrue(refused, "write form must raise Unviewable")
        self.assert_no_git(fx, args, env=env)
    return test


for _form in ("branch_delete", "branch_create", "branch_force", "branch_description",
              "symbolic_head_write", "symbolic_shared_write", "symbolic_shared_delete"):
    setattr(RoundTwoControlsTest, "test_control_r11_" + _form, ref_write_test(_form, guarded=False))
    setattr(RoundTwoCommandsTest, "test_r11_refuses_" + _form, ref_write_test(_form, guarded=True))


def inventory_calls(fx, command):
    """A concrete, nonempty operand for every public vocabulary entry."""
    calls = {
        "status": [["--short"], ["--porcelain"], ["--porcelain", "--ignored"]],
        "rev-parse": [["HEAD"], ["--git-common-dir"], ["--abbrev-ref", "HEAD"],
                      ["--is-shallow-repository"], ["--verify", "HEAD"],
                      ["--verify", "--quiet", "HEAD"], ["--verify", "-q", "HEAD"],
                      ["--short", "HEAD"], ["--path-format=absolute", "--git-common-dir"],
                      ["--disambiguate=" + fx.sha["D"][:12]], ["--git-path", "info/grafts"],
                      ["--show-toplevel"], ["--absolute-git-dir"]],
        "log": [[option, "HEAD"] for option in ("--format=%s", "--stat", "--no-ext-diff",
                                                 "--no-textconv", "--no-color", "-1")],
        "merge-base": [["--is-ancestor", "main", "HEAD"]],
        "diff": [[option, "HEAD"] for option in OPTION_VOCABULARY["diff"]],
        "show": [["--stat", "HEAD"], ["--format=%s", "HEAD"]],
        "for-each-ref": [["--format=%(refname)", "refs/heads"], ["--format", "%(refname)"]],
        "ls-files": [[], ["--stage"], ["-z"]],
        "ls-tree": [["-r", "HEAD"]],
        "cat-file": [["-p", "HEAD:README.md"], ["-t", "HEAD"], ["-e", "HEAD^{commit}"],
                     ["--batch-check"]],
        "rev-list": [["--count", "HEAD"], ["--left-right", "--count", "main...HEAD"]],
        "branch": [["--show-current"]],
        "symbolic-ref": [["HEAD"], ["-q", "HEAD"], ["--short", "HEAD"]],
        "check-ref-format": [["--branch", "candidate"]],
    }
    return [[command, *tail] for tail in calls[command]]


def vocabulary_test(command, *, accepted):
    def test(self):
        fx = self.fixture()
        self.assertEqual(set(OPTION_VOCABULARY), READ_ONLY_COMMANDS)
        if accepted:
            seen = set()
            for args in inventory_calls(fx, command):
                seen.update(arg.split("=", 1)[0] for arg in args[1:] if arg.startswith("-"))
                with self.subTest(args=args):
                    control = raw(fx, args)
                    self.assertEqual(control.returncode, 0, control.stderr)
                    result = self.run_view(fx, args)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    if command == "rev-parse" and any(token in args for token in (
                            "--git-common-dir", "--absolute-git-dir", "--git-path")):
                        # These name ephemeral metadata rather than the real
                        # common directory. Pin acceptance and private location.
                        private = Path(result.stdout.strip())
                        self.assertTrue(private.is_absolute())
                        self.assertTrue(private.is_relative_to(self.base))
                    else:
                        self.assertEqual(answer(result), answer(control), result.stderr)
                    self.assertEqual(fx.fired(), [])
                    self.assertEqual(self.leftovers(), [])
            self.assertEqual(seen, OPTION_VOCABULARY[command])
            return
        # A real unguarded parser control establishes that these tokens reach
        # Git (rev-parse even echoes unknown options with rc=0).
        with patch.object(subprocess, "run", wraps=subprocess.run) as start:
            raw(fx, [command, "--fixture-unknown-option"])
        start.assert_called_once()
        all_options = set().union(*OPTION_VOCABULARY.values())
        forbidden = (all_options - OPTION_VOCABULARY[command]) | {
            "--no-index", "-O" + str(fx.root / "outside-order"),
            "--exclude-from=" + str(fx.root / "outside-excludes"),
            "--output", "--output=" + str(fx.root / "outside-output"), "--output-extra",
            "--ext-diff", "--textconv", "--fixture-unknown-option",
            "--submodule", "--submodule=diff", "--submodule=log", "--submodule-extra",
            "--ignore-submodules", "--ignore-submodules=none", "--ignore-submodules-extra",
        }
        for option in sorted(forbidden):
            with self.subTest(option=option):
                self.assert_no_git(fx, [command, option])
        # No abbreviation/prefix acceptance or arbitrary values on boolean
        # options. Options taking values are separately exercised above.
        valued = {"--format", "--disambiguate", "--path-format", "--git-path"}
        for option in sorted(OPTION_VOCABULARY[command]):
            for bad in (option + "-extra", *(() if option in valued else (option + "=unexpected",))):
                with self.subTest(option=bad):
                    self.assert_no_git(fx, [command, bad])
    return test


for _command in sorted(OPTION_VOCABULARY):
    _name = _command.replace("-", "_")
    setattr(RoundTwoCommandsTest, "test_r12_accepts_vocabulary_" + _name,
            vocabulary_test(_command, accepted=True))
    setattr(RoundTwoCommandsTest, "test_r12_refuses_other_options_" + _name,
            vocabulary_test(_command, accepted=False))


def file_option_control(case, vector):
    fx = case.fixture()
    if vector == "no_index":
        outside = fx.root / "outside.txt"
        outside.write_text("generic outside sentinel\n")
        args = ["diff", "--no-index", str(outside), os.devnull]
        control = raw(fx, args)
        case.assertEqual(control.returncode, 1, control.stderr)
        case.assertIn("-generic outside sentinel\n", control.stdout)
        # The exact review reproduction, without retaining system-file data.
        system = raw(fx, ["diff", "--no-index", "/etc/hosts", os.devnull])
        case.assertEqual(system.returncode, 1, system.stderr)
        case.assertIn("diff --git", system.stdout)
    elif vector == "orderfile":
        (fx.tree / "b.txt").write_text("changed\n")
        outside = fx.root / "outside-order.txt"
        outside.write_text("b.txt\nREADME.md\n")
        args = ["diff", "-O" + str(outside)]
        before = raw(fx, ["diff"])
        control = raw(fx, args)
        case.assertEqual(control.returncode, 0, control.stderr)
        case.assertLess(before.stdout.index("diff --git a/README.md"), before.stdout.index("diff --git a/b.txt"))
        case.assertLess(control.stdout.index("diff --git a/b.txt"), control.stdout.index("diff --git a/README.md"),
                        "external orderfile bytes must change the answer")
    elif vector == "exclude_from":
        outside = fx.root / "outside-excludes.txt"
        outside.write_text("new.txt\n")
        args = ["ls-files", "--others", "--exclude-from=" + str(outside)]
        case.assertIn("new.txt\n", raw(fx, ["ls-files", "--others"]).stdout)
        control = raw(fx, args)
        case.assertEqual(control.returncode, 0, control.stderr)
        case.assertNotIn("new.txt\n", control.stdout, "external exclude bytes must change the answer")
    elif vector in ("external_diff", "textconv"):
        taint.config(fx, "diff.external" if vector == "external_diff" else "diff.x.textconv",
                     fx.command(vector))
        args = ["diff", "--ext-diff" if vector == "external_diff" else "--textconv"]
        control = raw(fx, args)
        case.assertEqual(control.returncode, 0, control.stderr)
        case.assertIn(vector, fx.fired(), control.stderr)
    else:
        case.fail("missing file-option control")
    fx.clear()
    return fx, args


def file_option_test(vector, *, guarded):
    def test(self):
        fx, args = file_option_control(self, vector)
        if not guarded:
            return
        self.assert_no_git(fx, args)
        if vector == "no_index":
            self.assert_no_git(fx, ["diff", "--no-index", "/etc/hosts", os.devnull])
        elif vector == "orderfile":
            self.assert_no_git(fx, ["diff", "-O", args[1][2:]])
        elif vector == "exclude_from":
            self.assert_no_git(fx, ["ls-files", "--exclude-from=" + args[2].split("=", 1)[1]])
            self.assert_no_git(fx, ["ls-files", "--exclude-from", args[2].split("=", 1)[1]])
    return test


for _vector in ("no_index", "orderfile", "exclude_from", "external_diff", "textconv"):
    setattr(RoundTwoControlsTest, "test_control_r12_" + _vector, file_option_test(_vector, guarded=False))
    setattr(RoundTwoCommandsTest, "test_r12_refuses_" + _vector, file_option_test(_vector, guarded=True))


def positional_test(self):
    fx = self.fixture()
    for name in ("--fixture-unknown-option", "-Ooutside-order"):
        (fx.tree / name).write_text("before\n")
    taint.git(["add", "--", "--fixture-unknown-option", "-Ooutside-order"], fx.tree)
    taint.git(["commit", "-qm", "generic paths", "--", "--fixture-unknown-option", "-Ooutside-order"], fx.tree)
    for name in ("--fixture-unknown-option", "-Ooutside-order"):
        (fx.tree / name).write_text("after\n")
        for args in (["diff", "--name-only", "HEAD", "--", name],
                     ["log", "--format=%s", "HEAD~1..HEAD", "--", name],
                     ["show", "--stat", "HEAD", "--", name], ["ls-files", "--", name]):
            with self.subTest(args=args):
                control = raw(fx, args)
                self.assertEqual(control.returncode, 0, control.stderr)
                self.assertTrue(control.stdout)
                self.assertEqual(answer(self.run_view(fx, args)), answer(control))
    self.assertEqual(fx.fired(), [])
    self.assertEqual(self.leftovers(), [])


RoundTwoCommandsTest.test_r12_revisions_and_option_looking_paths_after_separator_are_operands = positional_test


def caller_program_control(case, name):
    fx = case.fixture()
    env = {**git_trees.audit_env(), "TERM": "xterm", name: fx.command("caller-" + name.lower())}
    if name == "PAGER":
        control = taint.pager_control(fx, env)
    else:
        control = raw(fx, ["branch", "--edit-description"], env)
    case.assertEqual(control.returncode, 0, control.stderr)
    case.assertIn("caller-" + name.lower(), fx.fired(), "the exact caller variable must execute")
    fx.clear()
    return fx, env


def caller_program_test(name, *, guarded):
    def test(self):
        fx, env = caller_program_control(self, name)
        if not guarded:
            return
        caller = dict(env)
        real_run = subprocess.run
        with patch.object(subprocess, "run", wraps=real_run) as start:
            for args in (["branch", "--show-current"], ["symbolic-ref", "HEAD"], ["log", "--format=%s"]):
                result = self.view.run(args, fx.tree, env=env, base=self.base)
                self.assertEqual(result.returncode, 0, result.stderr)
        for call in start.call_args_list:
            self.assertNotIn(name, call.kwargs["env"], "also scrub the config parser/preflight")
            self.assertLessEqual(set(call.kwargs["env"]), CHILD_ENV_ALLOWLIST)
        with self.view.opened(fx.tree, env, base=self.base) as v:
            self.assertNotIn(name, v.env)
            self.assertLessEqual(set(v.env), CHILD_ENV_ALLOWLIST)
        self.assertEqual(env, caller)
        self.assertEqual(fx.fired(), [])
        self.assertEqual(self.leftovers(), [])
        self.assert_no_git(fx, ["branch", "--edit-description"], env=env)
    return test


for _name in ("EDITOR", "VISUAL", "PAGER", "GIT_EDITOR"):
    setattr(RoundTwoControlsTest, "test_control_r11_caller_" + _name.lower(), caller_program_test(_name, guarded=False))
    setattr(RoundTwoCommandsTest, "test_r11_drops_caller_" + _name.lower(), caller_program_test(_name, guarded=True))


def child_allowlist_test(self):
    fx, env = caller_program_control(self, "EDITOR")
    for name in ("VISUAL", "PAGER", "GIT_EDITOR", "SSH_ASKPASS", "LD_PRELOAD", "DYLD_INSERT_LIBRARIES",
                 "XDG_CONFIG_HOME", "LC_ALL", "GIT_FUTURE_OPTION", "FIXTURE_FUTURE_OPTION"):
        env[name] = "fixture-untrusted-value"
    # Drop the loader/editor variables BEFORE Git starts, including during
    # config parsing. The spy checks all subprocesses, not only the last read.
    real_run = subprocess.run
    caller = dict(env)
    with patch.object(subprocess, "run", wraps=real_run) as start:
        result = self.view.run(["rev-parse", "HEAD"], fx.tree, env=env, base=self.base)
    self.assertEqual(answer(result), (0, fx.sha["D"] + "\n"))
    for call in start.call_args_list:
        self.assertLessEqual(set(call.kwargs["env"]), CHILD_ENV_ALLOWLIST)
    with self.view.opened(fx.tree, env, base=self.base) as v:
        self.assertEqual(set(v.env), CHILD_ENV_ALLOWLIST)
        self.assertEqual(v.env["PATH"], env["PATH"])
        self.assertEqual(v.env["HOME"], env["HOME"])
    self.assertEqual(env, caller)
    self.assertEqual(fx.fired(), [])
    self.assertEqual(self.leftovers(), [])


RoundTwoCommandsTest.test_r11_child_environment_has_only_the_named_allowlist = child_allowlist_test


def home_config_control(case, mode):
    fx = case.fixture()
    home = fx.root / "home"
    home.mkdir()
    probe = taint.marker_program(fx, "home-external-diff")
    global_config = home / ".gitconfig"
    taint.config(fx, "diff.external", str(probe), file=global_config)
    env = {k: v for k, v in git_trees.audit_env().items() if k not in MODULE_CONFIG_PINS}
    env["HOME"] = str(home)
    if mode == "overridden":
        env.update(GIT_CONFIG_GLOBAL=str(global_config), GIT_CONFIG_SYSTEM=str(global_config),
                   GIT_CONFIG_NOSYSTEM="0")
    control = raw(fx, ["diff"], env)
    case.assertIn("home-external-diff", fx.fired(), control.stderr)
    fx.clear()
    return fx, env


def config_pins_test(mode, *, guarded):
    def test(self):
        fx, env = home_config_control(self, mode)
        if not guarded:
            return
        caller = dict(env)
        real_run = subprocess.run
        with patch.object(subprocess, "run", wraps=real_run) as start:
            result = self.view.run(["diff"], fx.tree, env=env, base=self.base)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("diff --git", result.stdout)
        self.assertEqual(fx.fired(), [])
        for call in start.call_args_list:
            child = call.kwargs["env"]
            self.assertEqual({name: child.get(name) for name in MODULE_CONFIG_PINS}, MODULE_CONFIG_PINS)
        with self.view.opened(fx.tree, env, base=self.base) as v:
            self.assertEqual({name: v.env.get(name) for name in MODULE_CONFIG_PINS}, MODULE_CONFIG_PINS)
        self.assertEqual(env, caller)
        self.assertEqual(self.leftovers(), [])
    return test


for _mode in ("omitted", "overridden"):
    setattr(RoundTwoControlsTest, "test_control_r14_config_pins_" + _mode, config_pins_test(_mode, guarded=False))
    setattr(RoundTwoCommandsTest, "test_r14_module_authors_config_pins_" + _mode, config_pins_test(_mode, guarded=True))


class RoundThreeControlsTest(FixtureCase):
    """Live attacks, runnable independently of the round-3 production fixes."""


class RoundThreeOperandsTest(FixtureCase):
    def assert_operand_refused(self, fx, args, sentinel):
        # Resolution may need a safe revision probe, so do not require zero
        # subprocesses. It must refuse, without printing private file bytes.
        with patch("sys.stdout") as stdout, patch("sys.stderr") as stderr:
            try:
                result = self.run_view(fx, args)
            except self.view.Unviewable as error:
                self.assertNotIn(sentinel, str(error))
            else:
                self.assertEqual(result.stdout, "", "outside operand produced output")
                self.assertNotIn(sentinel, result.stderr)
                self.fail("outside/path-without-separator operand must raise Unviewable")
        stdout.write.assert_not_called()
        stderr.write.assert_not_called()
        self.assertEqual(self.leftovers(), [])

    def test_r16_diff_accepts_revisions_and_in_tree_paths_after_separator(self):
        fx = self.fixture()
        for args in (["diff"], ["diff", "HEAD"], ["diff", fx.sha["A"], fx.sha["B"]],
                     ["diff", "main", "HEAD"], ["diff", "--", "README.md"],
                     ["diff", "HEAD", "--", "README.md"],
                     ["diff", fx.sha["A"], fx.sha["B"], "--", "b.txt"]):
            with self.subTest(args=args):
                control = raw(fx, args)
                self.assertEqual(control.returncode, 0, control.stderr)
                self.assertTrue(control.stdout, "positive read must produce a real patch")
                self.assertEqual(answer(self.run_view(fx, args)), answer(control))
        # Keep every currently allowlisted diff option usable with revisions
        # and paths. --cached is deliberately outside the round-2 vocabulary.
        for option in sorted(OPTION_VOCABULARY["diff"]):
            args = ["diff", option, "HEAD", "--", "README.md"]
            with self.subTest(args=args):
                control = raw(fx, args)
                self.assertEqual(control.returncode, 0, control.stderr)
                self.assertTrue(control.stdout)
                self.assertEqual(answer(self.run_view(fx, args)), answer(control))
        self.assertEqual(self.leftovers(), [])

    def test_r16_diff_paths_before_separator_are_refused(self):
        fx = self.fixture()
        # Raw Git accepts these implicit paths; the stricter view contract
        # makes the caller explicitly distinguish paths from revisions.
        for args in (["diff", "README.md"], ["diff", "HEAD", "README.md"],
                     ["diff", "README.md", "b.txt"]):
            with self.subTest(args=args):
                control = raw(fx, args)
                self.assertEqual(control.returncode, 0, control.stderr)
                self.assert_operand_refused(fx, args, "generic absent secret")


def outside_diff_control(case, kind):
    fx = case.fixture()
    operand, sentinel = taint.outside_operand(fx, kind)
    # Git's implicit-mode heuristic does not resolve the symlinked directory
    # when both spellings appear in-tree. A /dev/null peer triggers no-index,
    # after which Git follows that directory and reads the external bytes.
    peer = os.devnull if kind == "symlink_directory" else "empty.txt"
    args = ["diff", operand, peer]
    control = raw(fx, args)
    case.assertEqual(control.returncode, 1, control.stderr)
    case.assertIn("-" + sentinel + "\n", control.stdout,
                  "implicit --no-index must actually read the external file")
    return fx, operand, sentinel


def outside_diff_test(kind, *, guarded):
    def test(self):
        fx, operand, sentinel = outside_diff_control(self, kind)
        if not guarded:
            return
        # Absolute and parent paths leak with a genuinely in-tree peer. The
        # symlink control needs /dev/null to trigger Git's mode heuristic.
        # Either position and an explicit separator must be refused.
        peer = os.devnull if kind == "symlink_directory" else "empty.txt"
        for paths in ([operand, peer], [peer, operand]):
            for separator in ([], ["--"]):
                for options in ([], ["--no-ext-diff"]):
                    args = ["diff", *options, *separator, *paths]
                    with self.subTest(args=args):
                        control = raw(fx, args)
                        self.assertEqual(control.returncode, 1, control.stderr)
                        self.assertIn(sentinel, control.stdout)
                        self.assert_operand_refused(fx, args, sentinel)
        # Single paths must obey the same rule; a revision alongside an
        # external path is not permission to reach outside the worktree.
        for args in (["diff", "--", operand], ["diff", "HEAD", "--", operand]):
            with self.subTest(args=args):
                self.assert_operand_refused(fx, args, sentinel)
        if kind == "symlink_directory":
            for paths in ([operand, "empty.txt"], ["empty.txt", operand]):
                with self.subTest(paths=paths):
                    self.assert_operand_refused(fx, ["diff", "--", *paths], sentinel)
    return test


for _kind in ("absolute", "parent", "symlink_directory"):
    setattr(RoundThreeControlsTest, "test_control_r16_implicit_no_index_" + _kind,
            outside_diff_test(_kind, guarded=False))
    setattr(RoundThreeOperandsTest, "test_r16_diff_refuses_outside_" + _kind,
            outside_diff_test(_kind, guarded=True))


def other_path_operands_test(command):
    def test(self):
        for kind in ("absolute", "parent", "symlink_directory"):
            with self.subTest(kind=kind):
                # Git already refuses or ignores most of these paths. Reuse
                # the live diff control to prove this exact path reaches the
                # external bytes; do not invent a leak in a safe command.
                fx, operand, sentinel = outside_diff_control(self, kind)
                prefix = {
                    "ls-files": ["ls-files"], "ls-tree": ["ls-tree", "-r", "HEAD"],
                    "status": ["status", "--short"], "log": ["log", "-1", "--format=%s", "HEAD"],
                    "show": ["show", "--stat", "HEAD"],
                }[command]
                args = [*prefix, "--", operand]
                with self.subTest(args=args):
                    self.assert_operand_refused(fx, args, sentinel)
                safe = [*prefix, "--", "d.txt" if command == "show" else "README.md"]
                control = raw(fx, safe)
                self.assertEqual(control.returncode, 0, control.stderr)
                self.assertTrue(control.stdout)
                self.assertEqual(answer(self.run_view(fx, safe)), answer(control))
        self.assertEqual(self.leftovers(), [])
    return test


for _command in ("ls-files", "ls-tree", "status", "log", "show"):
    setattr(RoundThreeOperandsTest, "test_r16_path_boundary_" + _command.replace("-", "_"),
            other_path_operands_test(_command))


class RoundThreeSignatureTest(FixtureCase):
    assert_no_git = ReviewCommandsTest.assert_no_git

    def test_r17_gpg_program_is_pinned_non_executable_in_view_config(self):
        fx, env = signature_control(self, "log", "%G?")
        # A worker-selected verifier must not override the launcher-authored
        # defense in depth, even when all signature atoms are refused.
        taint.config(fx, "gpg.program", str(fx.root / "caller-bin/gpg"))
        with self.view.opened(fx.tree, env, base=self.base) as v:
            directory = Path(v.env["GIT_DIR"])
            result = taint.git(["config", "--file", str(directory / "config"),
                                "--get", "gpg.program"], self.root, check=False)
            self.assertEqual(answer(result), (0, os.devnull + "\n"),
                             "pin gpg.program to /dev/null as defense in depth")
            self.assertFalse(os.access(os.devnull, os.X_OK))
            # Bypass argument validation to test the config defense itself.
            direct = subprocess.run(["git", "log", "-1", "--format=%G?"], cwd=fx.tree,
                                    env=v.env, stdin=subprocess.DEVNULL,
                                    capture_output=True, text=True, timeout=5)
            self.assertEqual(direct.returncode, 0, direct.stderr)
            self.assertEqual(fx.fired(), [], "config alone must prevent PATH gpg execution")
        self.assertEqual(self.leftovers(), [])

    def test_r17_unknown_g_atom_is_refused_for_future_verifiers(self):
        fx, env = signature_control(self, "log", "%G?")
        for command in ("log", "show"):
            with self.subTest(command=command):
                self.assert_no_git(fx, [command, "--format=%GZ", "HEAD"], env=env)


def signature_control(case, command, atom):
    fx = case.fixture()
    env = taint.signed_commit_on_path(fx, git_trees.audit_env())
    args = [command, "--format=" + atom, "HEAD"]
    if command == "log":
        args.insert(1, "-1")
    control = raw(fx, args, env)
    case.assertEqual(control.returncode, 0, control.stderr)
    case.assertEqual(fx.fired(), ["gpg"], "the exact %G atom must execute caller PATH gpg")
    fx.clear()
    return fx, env


def signature_atom_test(command, atom, *, guarded):
    def test(self):
        fx, env = signature_control(self, command, atom)
        if not guarded:
            return
        for fmt in (atom, "prefix " + atom + " suffix %s"):
            args = [command, "--format=" + fmt, "HEAD"]
            with self.subTest(args=args):
                self.assert_no_git(fx, args, env=env)
        safe = [command, "--format=%H %s", "HEAD"]
        control = raw(fx, safe, env)
        self.assertEqual(control.returncode, 0, control.stderr)
        self.assertEqual(answer(self.view.run(safe, fx.tree, env=env, base=self.base)), answer(control))
        self.assertEqual(fx.fired(), [])
        self.assertEqual(self.leftovers(), [])
    return test


for _command in ("log", "show"):
    for _atom, _name in (("%G?", "status"), ("%GG", "raw"), ("%GS", "signer"),
                         ("%GK", "key"), ("%GF", "fingerprint"),
                         ("%GP", "primary_fingerprint"), ("%GT", "trust")):
        setattr(RoundThreeControlsTest, f"test_control_r17_{_command}_gpg_{_name}",
                signature_atom_test(_command, _atom, guarded=False))
        setattr(RoundThreeSignatureTest, f"test_r17_{_command}_refuses_g_atom_{_name}",
                signature_atom_test(_command, _atom, guarded=True))


def packed_secret_control(case):
    fx = case.fixture()
    sentinel = taint.packed_refs_secret(fx)
    for args in (["for-each-ref"], ["log", "-1", "--format=%H"]):
        control = raw(fx, args)
        case.assertNotEqual(control.returncode, 0)
        case.assertIn(sentinel, control.stderr,
                      "unguarded packed-ref parse error must disclose external bytes")
    return fx, sentinel


class RoundThreeMetadataTest(FixtureCase):
    def test_r18_packed_refs_symlink_refuses_without_disclosing_secret(self):
        fx, sentinel = packed_secret_control(self)
        for args in (["for-each-ref"], ["log", "-1", "--format=%H"]):
            with self.subTest(args=args), patch("sys.stdout") as stdout, patch("sys.stderr") as stderr:
                try:
                    result = self.run_view(fx, args)
                except self.view.Unviewable as error:
                    self.assertNotIn(sentinel, str(error))
                else:
                    self.assertNotIn(sentinel, result.stdout)
                    self.assertNotIn(sentinel, result.stderr, "packed-refs symlink disclosed external bytes")
                    self.fail("packed-refs symlink must raise Unviewable")
                stdout.write.assert_not_called()
                stderr.write.assert_not_called()
        self.assertEqual(self.leftovers(), [])

    def test_r18_legitimate_packed_refs_use_bounded_read_and_private_snapshot(self):
        # Establish the unsafe linked-file control before pinning its safe
        # replacement. Do not turn a legitimate packed repo into a refusal.
        packed_secret_control(self)
        fx = self.fixture(packed=True)
        source = fx.common / "packed-refs"
        before = source.read_bytes()
        reads = (["rev-parse", "main"], ["log", "-1", "--format=%H", "main"],
                 ["for-each-ref", "--format=%(refname) %(objectname)"])
        expected = [(args, answer(raw(fx, args))) for args in reads]
        for args, truth in expected:
            self.assertEqual(truth[0], 0)
            self.assertTrue(truth[1])
            self.assertEqual(answer(self.run_view(fx, args)), truth)
        view = self.view
        with patch.object(view, "_read", wraps=view._read) as bounded:
            with view.opened(fx.tree, git_trees.audit_env(), base=self.base) as v:
                copied = Path(v.env["GIT_DIR"]) / "packed-refs"
                calls = [call for call in bounded.call_args_list if Path(call.args[0]) == source]
                with self.subTest(contract="bounded _read"):
                    self.assertEqual(len(calls), 1, "packed-refs must pass through the common _read guard")
                    self.assertIsInstance(calls[0].args[1], int)
                    self.assertGreater(calls[0].args[1], 0)
                with self.subTest(contract="private regular copy"):
                    self.assertTrue(stat.S_ISREG(copied.lstat().st_mode), "packed-refs must be a regular copy")
                    self.assertFalse(copied.is_symlink())
                    self.assertEqual(copied.read_bytes(), before)
                    source.write_text("generic changed worker metadata\n")
                    self.assertEqual(copied.read_bytes(), before, "worker edits must not change the open view")
                    result = subprocess.run(["git", "rev-parse", "main"], cwd=fx.tree,
                                            env=v.env, stdin=subprocess.DEVNULL,
                                            capture_output=True, text=True, timeout=5)
                    self.assertEqual(answer(result), (0, fx.sha["B"] + "\n"))
        self.assertEqual(self.leftovers(), [])


def packed_secret_control_test(self):
    packed_secret_control(self)


RoundThreeControlsTest.test_control_r18_packed_refs_symlink_leaks_parse_error = packed_secret_control_test


def stdin_control(case):
    fx = case.fixture()
    payload = fx.sha["D"] + "\nGENERIC-LAUNCHER-INPUT\n"
    result = taint.launcher_stdin(fx, case.base, git_trees.audit_env(), payload, guarded=False)
    case.assertIsNone(result["error"])
    case.assertEqual(result["returncode"], 0, result["stderr"])
    case.assertIn(fx.sha["D"] + " commit ", result["stdout"])
    case.assertIn("GENERIC-LAUNCHER-INPUT missing\n", result["stdout"])
    case.assertEqual(result["rest"], "", "control must consume the launcher's fd 0")
    return fx, payload


class RoundThreeStdinTest(FixtureCase):
    def test_r19_every_subprocess_including_config_and_preflight_uses_devnull(self):
        fx, payload = stdin_control(self)
        real_run = subprocess.run

        def execute_with_closed_stdin(*args, **kwargs):
            # Record the module's original kwargs, but keep a missing-guard
            # regression from blocking the test runner on its own stdin.
            return real_run(*args, **{**kwargs, "stdin": subprocess.DEVNULL})

        with patch.object(subprocess, "run", side_effect=execute_with_closed_stdin) as started:
            for args in (["rev-parse", "HEAD"], ["status", "--short"], ["cat-file", "--batch-check"]):
                result = self.run_view(fx, args)
                self.assertEqual(result.returncode, 0, result.stderr)
        commands = [call.args[0][1] for call in started.call_args_list]
        self.assertIn("config", commands)
        self.assertIn("ls-files", commands)
        self.assertIn("cat-file", commands)
        for call in started.call_args_list:
            with self.subTest(argv=call.args[0]):
                self.assertEqual(call.kwargs.get("stdin"), subprocess.DEVNULL,
                                 "every module subprocess must explicitly close stdin")
        self.assertEqual(self.leftovers(), [])


def stdin_pipe_test(*, hold_open, guarded):
    def test(self):
        fx, payload = stdin_control(self)
        if not guarded:
            return
        result = taint.launcher_stdin(fx, self.base, git_trees.audit_env(), payload,
                                    guarded=True, hold_open=hold_open)
        self.assertIsNone(result["error"], "batch-check must return promptly, even without launcher EOF")
        self.assertEqual(result["returncode"], 0, result["stderr"])
        self.assertEqual(result["stdout"], "", "batch-check must not echo launcher input")
        self.assertNotIn("GENERIC-LAUNCHER-INPUT", result["stderr"])
        self.assertEqual(result["rest"], payload, "launcher stdin must remain unread")
        self.assertEqual(self.leftovers(), [])
    return test


RoundThreeControlsTest.test_control_r19_batch_check_consumes_and_echoes_launcher_pipe = stdin_pipe_test(
    hold_open=False, guarded=False)
RoundThreeStdinTest.test_r19_batch_check_leaves_launcher_pipe_unread = stdin_pipe_test(
    hold_open=False, guarded=True)
RoundThreeStdinTest.test_r19_batch_check_returns_with_launcher_pipe_still_open = stdin_pipe_test(
    hold_open=True, guarded=True)


if __name__ == "__main__":
    unittest.main()
