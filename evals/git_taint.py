"""Generic, single-vector repositories for testing reads of worker-written git data.

All programs planted in a repository are Python. Dates enter git through its
environment, never through workspace data or a fixture file.
"""

from __future__ import annotations

import contextlib
import json
import os
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from unittest import mock

ENV = {
    "PATH": "/usr/bin:/bin:/usr/local/bin", "HOME": "/var/empty",
    "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0",
    "GIT_AUTHOR_NAME": "Fixture", "GIT_AUTHOR_EMAIL": "fixture@example.test",
    "GIT_COMMITTER_NAME": "Fixture", "GIT_COMMITTER_EMAIL": "fixture@example.test",
    "GIT_AUTHOR_DATE": "946684800 +0000", "GIT_COMMITTER_DATE": "946684800 +0000",
}

VECTORS = (
    "fsmonitor", "filter", "hooks", "hooks_in_common", "include", "wtconfig",
    "grafts", "grafts_yes", "shallow", "replace", "commondir", "gitfile",
    "gitlink", "nested_untracked", "reftable_marker",
)


def git(args: list[str], cwd: Path, *, env=None, check=True):
    return subprocess.run(["git", *args], cwd=cwd, env=ENV if env is None else env,
                          capture_output=True, text=True, timeout=15, check=check)


@dataclass(frozen=True)
class Fx:
    root: Path
    tree: Path
    clone: Path
    common: Path
    gitdir: Path
    sha: dict[str, str]

    @property
    def markers(self) -> Path:
        return self.root / "markers"

    def fired(self) -> list[str]:
        return sorted(p.name for p in self.markers.iterdir())

    def clear(self) -> None:
        for p in self.markers.iterdir():
            p.unlink()

    def command(self, tag: str, exitcode=0) -> str:
        code = (f"from pathlib import Path; import sys; "
                f"Path({str(self.markers / tag)!r}).write_text('fired'); "
                f"sys.exit({exitcode})")
        return shlex.join([sys.executable, "-c", code])


def config(fx: Fx, key: str, value: str, *, file: Path | None = None) -> None:
    # Configure by explicit file outside any repository: a previously planted
    # core.worktree must not redirect the fixture builder's next command.
    git(["config", "--file", str(file or fx.common / "config"), key, value], fx.root)


def _hook(fx: Fx, path: Path, tag: str) -> None:
    path.parent.mkdir(exist_ok=True)
    path.write_text(f"#!{sys.executable}\nfrom pathlib import Path\n"
                    f"Path({str(fx.markers / tag)!r}).write_text('fired')\n")
    path.chmod(0o755)


def _fsmonitor(fx):
    config(fx, "core.fsmonitor", fx.command("fsmonitor", 1))


def _filter(fx):
    config(fx, "filter.x.clean", fx.command("filter-clean"))
    config(fx, "filter.x.smudge", fx.command("filter-smudge"))
    config(fx, "diff.x.textconv", fx.command("textconv"))


def _hooks(fx):
    directory = fx.root / "worker-hooks"
    directory.mkdir()
    for name in ("post-index-change", "reference-transaction", "post-checkout", "pre-commit"):
        _hook(fx, directory / name, "hook-" + name)
    config(fx, "core.hooksPath", str(directory))


def _hooks_in_common(fx):
    _hook(fx, fx.common / "hooks" / "post-index-change", "hook-in-common")


def _include(fx):
    included = fx.root / "worker.cfg"
    config(fx, "core.fsmonitor", fx.command("include-fsmonitor", 1), file=included)
    config(fx, "include.path", str(included))


def _wtconfig(fx):
    config(fx, "core.repositoryformatversion", "1")
    config(fx, "extensions.worktreeConfig", "true")
    config(fx, "core.fsmonitor", fx.command("wtconfig-fsmonitor", 1),
           file=fx.gitdir / "config.worktree")


def _grafts(fx):
    (fx.common / "info" / "grafts").write_text(f"{fx.sha['C']} {fx.sha['A']}\n")


def _grafts_yes(fx):
    (fx.common / "info" / "grafts").write_text(f"{fx.sha['B']} {fx.sha['A']} {fx.sha['D']}\n")


def _shallow(fx):
    (fx.common / "shallow").write_text(f"{fx.sha['C']}\n")


def _replace(fx):
    git(["replace", "-f", fx.sha["D"], fx.sha["B"]], fx.clone)


def _commondir(fx):
    decoy = fx.root / "decoy"
    decoy.mkdir()
    git(["init", "-q", "-b", "main"], decoy)
    config(fx, "core.fsmonitor", fx.command("decoy-fsmonitor", 1), file=decoy / ".git" / "config")
    (fx.gitdir / "commondir").write_text(str(decoy / ".git") + "\n")


def _gitfile(fx):
    forged = fx.root / "forged"
    shutil.copytree(fx.gitdir, forged)
    (forged / "commondir").write_text(str(fx.common) + "\n")
    config(fx, "core.repositoryformatversion", "1")
    config(fx, "extensions.worktreeConfig", "true")
    config(fx, "core.fsmonitor", fx.command("gitfile-fsmonitor", 1), file=forged / "config.worktree")
    (fx.tree / ".git").write_text(f"gitdir: {forged}\n")


def _nested(fx, tracked):
    nested = fx.tree / ("gl" if tracked else "nested")
    nested.mkdir()
    git(["init", "-q", "-b", "main"], nested)
    (nested / "file.txt").write_text("a\n")
    git(["add", "file.txt"], nested)
    git(["commit", "-qm", "nested"], nested)
    oid = git(["rev-parse", "HEAD"], nested).stdout.strip()
    config(fx, "core.fsmonitor", fx.command("nested-fsmonitor", 1), file=nested / ".git" / "config")
    (nested / "file.txt").write_text("z\n")
    if tracked:
        git(["update-index", "--add", "--cacheinfo", f"160000,{oid},gl"], fx.tree)


def _gitlink(fx):
    _nested(fx, True)


def _nested_untracked(fx):
    _nested(fx, False)


def _reftable_marker(fx):
    (fx.common / "reftable").mkdir()


def apply(fx: Fx, vector: str) -> None:
    if vector not in VECTORS:
        raise ValueError(f"unknown taint vector: {vector}")
    globals()["_" + vector](fx)


def one_shot_control(case, fx: Fx, vector: str, env: dict[str, str]) -> None:
    """Prove a live hazard under the former one_shot read environment.

    HEAD/branch reads cannot execute status hooks or observe grafted ancestry;
    use status/history as the section 4.2 witness for those call sites too.
    Replace refs were already disabled: prove both the attack and that guard.
    """
    def raw(args, child_env=env, cwd=fx.tree):
        return git(args, cwd, env=child_env, check=False)

    markers = {
        "fsmonitor": "fsmonitor", "filter": "filter-clean",
        "hooks": "hook-post-index-change", "hooks_in_common": "hook-in-common",
        "include": "include-fsmonitor", "wtconfig": "wtconfig-fsmonitor",
        "commondir": "decoy-fsmonitor", "gitfile": "gitfile-fsmonitor",
        "gitlink": "nested-fsmonitor",
    }
    fx.clear()
    if vector in markers:
        case.assertNotIn("GIT_OPTIONAL_LOCKS", env, "the old read must allow index-refresh hooks")
        raw(["status", "--short"])
        case.assertIn(markers[vector], fx.fired())
    elif vector == "grafts":
        result = raw(["rev-list", "--count", "origin/main..HEAD"])
        case.assertEqual((result.returncode, result.stdout), (0, "2\n"))  # truth: 3
    elif vector == "grafts_yes":
        case.assertEqual(raw(["merge-base", "--is-ancestor", fx.sha["D"], "main"]).returncode, 0)  # truth: 1
    elif vector == "shallow":
        result = raw(["rev-parse", "--is-shallow-repository"])
        case.assertEqual((result.returncode, result.stdout), (0, "true\n"))  # truth: false
    elif vector == "replace":
        unpinned = {k: v for k, v in env.items() if k != "GIT_NO_REPLACE_OBJECTS"}
        result = raw(["rev-list", "--count", "origin/main..HEAD"], unpinned)
        case.assertEqual((result.returncode, result.stdout), (0, "1\n"))
        guarded = raw(["rev-list", "--count", "origin/main..HEAD"])
        case.assertEqual((guarded.returncode, guarded.stdout), (0, "3\n"))
    elif vector == "nested_untracked":
        raw(["status", "--short"], cwd=fx.tree / "nested")
        case.assertIn("nested-fsmonitor", fx.fired())
    else:
        case.fail(f"missing old one_shot control for {vector}")
    fx.clear()
    # The old control may refresh the REAL index. Make the next status refresh
    # it again, so a hook regression cannot pass merely because it ran once.
    for name in ("README.md", "b.txt", "c.txt", "d.txt"):
        os.utime(fx.tree / name, (1_900_000_000, 1_900_000_000))


def _build(tmp: Path, vector, packed, object_format) -> Fx:
    root = Path(tmp).resolve()
    root.mkdir(parents=True, exist_ok=True)
    (root / "markers").mkdir()
    clone = root / "repo"
    clone.mkdir()
    git(["init", "-q", "-b", "main", f"--object-format={object_format}"], clone)
    (clone / "README.md").write_text("a\n")
    git(["add", "."], clone)
    git(["commit", "-qm", "A"], clone)
    (clone / "b.txt").write_text("b\n")
    git(["add", "."], clone)
    git(["commit", "-qm", "B"], clone)
    tree = root / "wt"
    git(["worktree", "add", "-q", "-b", "feat", str(tree)], clone)
    for label in ("C", "D"):
        (tree / f"{label.lower()}.txt").write_text(label.lower() + "\n")
        git(["add", "."], tree)
        git(["commit", "-qm", label], tree)
    sha = {label: git(["rev-parse", ref], clone).stdout.strip()
           for label, ref in (("A", "main~1"), ("B", "main"), ("C", "feat~1"), ("D", "feat"))}
    common = clone / ".git"
    fx = Fx(root, tree, clone, common, common / "worktrees" / "wt", sha)
    git(["update-ref", "refs/remotes/origin/main", sha["A"]], clone)
    git(["update-ref", "refs/remotes/origin/feat", sha["C"]], clone)
    for key, value in (("remote.origin.url", "/nonexistent"),
                       ("remote.origin.fetch", "+refs/heads/*:refs/remotes/origin/*"),
                       ("branch.feat.remote", "origin"), ("branch.feat.merge", "refs/heads/feat")):
        config(fx, key, value)
    if packed:
        git(["pack-refs", "--all", "--prune"], clone)
    (tree / "README.md").write_text("z\n")  # same size, so status must compare content
    (tree / "new.txt").write_text("n\n")
    (tree / ".gitattributes").write_text("*.md filter=x diff=x\n*.txt diff=x\n")
    for name in ("README.md", "b.txt", "c.txt", "d.txt"):
        os.utime(tree / name, (2_000_000_000, 2_000_000_000))
    if vector is not None:
        apply(fx, vector)
    return fx


def build(tmp: Path, vector=None, packed=False) -> Fx:
    return _build(tmp, vector, packed, "sha1")


def build_sha256(tmp: Path, vector=None, packed=False) -> Fx:
    """The same fixture with 64-hex object ids, for the object-format allowlist."""
    return _build(tmp, vector, packed, "sha256")


def marker_program(fx: Fx, tag: str, *, name=None, upload_pack=False) -> Path:
    """An executable Python probe; no shell script or workspace-specific data."""
    program = fx.root / (name or f"probe-{tag}")
    program.write_text(
        f"#!{sys.executable}\nfrom pathlib import Path\nimport os, sys\n"
        f"Path({str(fx.markers / tag)!r}).write_text('fired')\n"
        + ("os.execvp('git', ['git', 'upload-pack', *sys.argv[1:]])\n" if upload_pack else "sys.exit(1)\n"))
    program.chmod(0o755)
    return program


def committed_gitlink(fx: Fx) -> None:
    """Only the nested dirt remains, so describe --dirty must inspect it."""
    _gitlink(fx)
    git(["commit", "-qm", "gitlink", "--", "gl"], fx.tree)
    (fx.tree / "README.md").write_text("a\n")
    git(["update-index", "--refresh"], fx.tree, check=False)
    fx.clear()


def historical_gitlink(fx: Fx) -> None:
    """Two gitlink commits, an executable nested diff, and no gitlink in index."""
    nested = fx.tree / "gl"
    nested.mkdir()
    git(["init", "-q", "-b", "main"], nested)
    for label, content in (("nested-one", "one\n"), ("nested-two", "two\n")):
        (nested / "file.txt").write_text(content)
        git(["add", "file.txt"], nested)
        git(["commit", "-qm", label], nested)
        oid = git(["rev-parse", "HEAD"], nested).stdout.strip()
        git(["update-index", "--add", "--cacheinfo", f"160000,{oid},gl"], fx.tree)
        git(["commit", "-qm", label, "--", "gl"], fx.tree)
    config(fx, "diff.external", fx.command("nested-external-diff"), file=nested / ".git/config")
    git(["update-index", "--force-remove", "gl"], fx.tree)
    fx.clear()


def metadata_snapshot(fx: Fx) -> dict[str, bytes]:
    """Ref/HEAD/config/reflog bytes: writes through shared refs must be visible."""
    paths = [fx.common / "HEAD", fx.gitdir / "HEAD", fx.common / "config", fx.common / "packed-refs"]
    for directory in (fx.common / "refs", fx.common / "logs", fx.gitdir / "logs"):
        paths.extend(p for p in directory.rglob("*") if p.is_file())
    return {str(p.relative_to(fx.root)): p.read_bytes() for p in paths if p.is_file()}


def pager_control(fx: Fx, env: dict[str, str]) -> subprocess.CompletedProcess:
    """Real Git needs a terminal on stdout to exercise the caller's PAGER."""
    import pty
    master, slave = pty.openpty()
    try:
        return subprocess.run(["git", "--paginate", "log", "--format=%s"],
                              cwd=fx.tree, env=env, stdin=subprocess.DEVNULL,
                              stdout=slave, stderr=subprocess.PIPE, text=True, timeout=5)
    finally:
        os.close(slave)
        os.close(master)


def forged_linked_layout(fx: Fx) -> tuple[Path, Path]:
    """A mutually consistent gitfile/commondir/backpointer in worker data."""
    common = fx.tree / "copied-repository"
    shutil.copytree(fx.common, common)
    shutil.rmtree(common / "worktrees")
    gitdir = common / "worktrees" / "evil"
    gitdir.mkdir(parents=True)
    (gitdir / "commondir").write_text("../..\n")
    (gitdir / "gitdir").write_text(str(fx.tree / ".git") + "\n")
    (gitdir / "HEAD").write_text(fx.sha["D"] + "\n")
    git(["--git-dir", str(common), "update-ref", "refs/heads/main", fx.sha["D"]], fx.root)
    (fx.tree / ".git").write_text(f"gitdir: {gitdir}\n")
    return common, gitdir


def partial_clone(fx: Fx, name: str) -> tuple[Path, str]:
    """A real file:// promisor clone with a known, initially missing blob.

    Callers verify --missing=print themselves: an ignored filter must never
    silently count as a partial-clone control.
    """
    config(fx, "uploadpack.allowFilter", "true")
    config(fx, "uploadpack.allowAnySHA1InWant", "true")
    clone = fx.root / name
    git(["clone", "-q", "--filter=blob:none", "--no-checkout", fx.clone.as_uri(), str(clone)], fx.root)
    blob = git(["rev-parse", "main:README.md"], fx.clone).stdout.strip()
    probe = marker_program(fx, "lazy-fetch", upload_pack=True)
    git(["config", "remote.origin.uploadpack", str(probe)], clone)
    return clone, blob


def outside_operand(fx: Fx, kind: str) -> tuple[str, str]:
    """One generic secret outside the worktree, reached by three spellings."""
    secret = fx.root / "outside.txt"
    sentinel = "generic outside content sentinel"
    secret.write_text(sentinel + "\n")
    if kind == "absolute":
        operand = str(secret)
    elif kind == "parent":
        operand = "../outside.txt"
    elif kind == "symlink_directory":
        (fx.tree / "outside-link").symlink_to(fx.root, target_is_directory=True)
        operand = "outside-link/outside.txt"
    else:
        raise ValueError(f"unknown outside operand: {kind}")
    (fx.tree / "empty.txt").write_text("")
    return operand, sentinel


def signed_commit_on_path(fx: Fx, env: dict[str, str]) -> dict[str, str]:
    """A worker-written signature header and a Python gpg on caller PATH.

    No keyring or real signature is needed: Git invokes the verifier before
    it can decide that these generic signature bytes are invalid.
    """
    tree = git(["rev-parse", "HEAD^{tree}"], fx.tree).stdout.strip()
    body = (f"tree {tree}\nparent {fx.sha['D']}\n"
            f"author Fixture <fixture@example.test> {ENV['GIT_AUTHOR_DATE']}\n"
            f"committer Fixture <fixture@example.test> {ENV['GIT_COMMITTER_DATE']}\n"
            "gpgsig -----BEGIN PGP SIGNATURE-----\n \n"
            " generic-signature-bytes\n -----END PGP SIGNATURE-----\n\n"
            "generic signed-looking commit\n")
    result = subprocess.run(["git", "hash-object", "-t", "commit", "-w", "--stdin"],
                            cwd=fx.tree, env=ENV, input=body, capture_output=True,
                            text=True, timeout=5, check=True)
    git(["update-ref", "refs/heads/feat", result.stdout.strip()], fx.tree)
    directory = fx.root / "caller-bin"
    directory.mkdir()
    marker_program(fx, "gpg", name="caller-bin/gpg")
    home = fx.root / "caller-home"
    home.mkdir()
    return {**env, "PATH": str(directory) + os.pathsep + env["PATH"], "HOME": str(home)}


def packed_refs_secret(fx: Fx) -> str:
    """Git's packed-ref parse diagnostic must not echo this external line."""
    sentinel = "generic packed refs secret sentinel"
    secret = fx.root / "private-metadata.txt"
    secret.write_text(sentinel + "\n")
    packed = fx.common / "packed-refs"
    packed.unlink(missing_ok=True)
    packed.symlink_to(secret)
    return sentinel


def launcher_stdin(fx: Fx, base: Path, env: dict[str, str], payload: str,
                   *, guarded: bool, hold_open: bool = False) -> dict:
    """A real Python launcher with fd 0 piped, optionally without EOF.

    Read remaining bytes directly from fd 0 after the child finishes; Python
    text buffering must not hide whether Git consumed the launcher's input.
    The held-open writer establishes that a guard cannot merely wait for EOF.
    """
    scripts = Path(__file__).resolve().parent.parent / "scripts"
    code = (
        "import json, os, subprocess, sys\nfrom pathlib import Path\n"
        f"sys.path.insert(0, {str(scripts)!r})\n"
        f"tree = Path({str(fx.tree)!r})\nenv = {env!r}\n"
        "error = None\nstdout = stderr = ''\nreturncode = None\ntry:\n"
        + (" import git_view\n"
           f" result = git_view.run(['cat-file', '--batch-check'], tree, env=env, "
           f"base=Path({str(base)!r}), timeout=1)\n" if guarded else
           " result = subprocess.run(['git', 'cat-file', '--batch-check'], cwd=tree, "
           "env=env, capture_output=True, text=True, timeout=1)\n")
        + " returncode, stdout, stderr = result.returncode, result.stdout, result.stderr\n"
        "except Exception as exc:\n error = type(exc).__name__\n"
        "os.set_blocking(0, False)\n"
        "try:\n rest = os.read(0, 65536).decode()\n"
        "except BlockingIOError:\n rest = ''\n"
        "print(json.dumps(dict(error=error, returncode=returncode, stdout=stdout, "
        "stderr=stderr, rest=rest)))\n"
    )
    if not hold_open:
        result = subprocess.run([sys.executable, "-c", code], input=payload,
                                capture_output=True, text=True, timeout=4, check=True)
        return json.loads(result.stdout)
    process = subprocess.Popen([sys.executable, "-c", code], stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        process.stdin.write(payload)
        process.stdin.flush()
        process.wait(timeout=4)  # Keep stdin's writer open throughout the wait.
        stdout, stderr = process.stdout.read(), process.stderr.read()
        if process.returncode:
            raise AssertionError(f"launcher failed: {stderr}")
        return json.loads(stdout)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        for stream in (process.stdin, process.stdout, process.stderr):
            stream.close()


@contextlib.contextmanager
def private_git_views(base: Path):
    """Give `git_view.run` and `git_view.opened` a trusted base, so a test never writes the user's cache.

    A caller that passes its own `base` wins. On macOS, bypass the Git launcher's sandbox diagnostics.
    """
    import git_view  # the scripts directory is on the importing test's path
    import git_trees

    run = git_view.run
    opened = git_view.opened

    def run_at_base(*args, **kwargs):
        kwargs.setdefault("base", base)
        return run(*args, **kwargs)

    def opened_at_base(*args, **kwargs):
        kwargs.setdefault("base", base)
        return opened(*args, **kwargs)

    with contextlib.ExitStack() as stack:
        if sys.platform == "darwin":
            binary = (Path(git(["--exec-path"], base.parent).stdout.strip()) / "git").resolve()
            real_env = git_trees.audit_env
            stack.enter_context(mock.patch.object(git_trees, "audit_env", side_effect=lambda: {
                **real_env(), "PATH": f"{binary.parent}:/usr/bin:/bin",
            }))
        stack.enter_context(mock.patch.object(git_view, "run", side_effect=run_at_base))
        stack.enter_context(mock.patch.object(git_view, "opened", side_effect=opened_at_base))
        yield
