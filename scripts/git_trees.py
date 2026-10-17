"""Git, and the worktrees on disk to ask it in (#43).

`git()` is the one bounded read-only call every audit question goes through; it ignores replace objects, so a
replace ref cannot rewrite ancestry. `read_state` fills a `TreeState`; `discover` lists the trees under the
`Worktrees:` dir, so the audit starts from what is on disk rather than from the File ownership rows that happen to
name a tree. The one write is `fetch_base`, which moves `refs/remotes/origin/main` (no other ref) and leaves `FETCH_HEAD` and the
fetched objects behind, for `check_sha` and `make_worktree`.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import NamedTuple

import git_view
from tree_state import UNREADABLE_BRANCH, TreeState

# Replace refs and grafts both rewrite parents, so neither may make a yes.
GIT_ENV = {"GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0", "GIT_NO_REPLACE_OBJECTS": "1"}
# The tree under inspection is worker-writable, so no home, global or system config may be read: a home with none.
SAFE_HOME = "/var/empty"
# What picks a repository over `-C`: the fetch must not inherit them, or it writes where the reads do not look.
REPO_VARS = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_NAMESPACE", "GIT_COMMON_DIR", "GIT_OBJECT_DIRECTORY",
             "GIT_ALTERNATE_OBJECT_DIRECTORIES")
GIT_TIMEOUT = 15
BASE = "origin/main"
LOCAL_NOTE = "; local main holds it"
REF = f"refs/remotes/{BASE}"  # the full name: a branch, tag or `refs/origin/main` called `origin/main` shadows the short one
LOCAL_REF = "refs/heads/main"


def audit_env() -> dict[str, str]:
    """The environment of every git call that audits a worker-writable tree. Fresh each call.

    No home, global or system config is read (a system-level `safe.directory`, for one, no longer applies to audit
    calls). The repository's own config is still read, and is tracked separately.
    """
    return {**GIT_ENV, "PATH": "/usr/bin:/bin:/usr/local/bin", "HOME": SAFE_HOME, "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_SYSTEM": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}


def user_env() -> dict[str, str]:
    """The caller's own environment minus the repository-selecting variables, for calls that run the user's own hooks, need the user's credentials, or read a tree with the user's own config. It is the user's config, not a worker's: a read that must trust no home config uses audit_env."""
    return {**{k: v for k, v in os.environ.items() if k not in REPO_VARS}, "GIT_TERMINAL_PROMPT": "0"}


# -c pins for every git call the launcher writes with (#556): no hook path, no credential helper and no submodule
# recursion may reach the call out of a config a worker can write. `credential.helper=` — the empty value — resets the
# helper list, so no helper runs at all, and a `-c` outlasts the repository's own config for these keys.
GIT_WRITE_PINS = ["-c", "core.hooksPath=/dev/null", "-c", "credential.helper=",
                  "-c", "submodule.recurse=false", "-c", "fetch.recurseSubmodules=false"]

# The caller's variables a git write may authenticate with (#556): the agent socket, the ssh command and fallback ssh,
# and the askpass program the caller chose. Every other caller variable stays out: the write runs on the audit env.
CREDENTIAL_VARS = ("SSH_AUTH_SOCK", "GIT_SSH_COMMAND", "GIT_SSH", "GIT_ASKPASS")


def write_env() -> dict[str, str]:
    """The environment of the launcher's git writes (#556): the audit's isolation — no home, global or system config,
    no prompts, no replace refs — plus only the caller's credential variables, so a fetch the caller could
    authenticate still authenticates. The repository-selecting variables are never carried, or the write would land
    where the reads do not look, as `user_env` never carried them."""
    env = audit_env()
    env.update({name: os.environ[name] for name in CREDENTIAL_VARS if name in os.environ})
    return env


class ReadFailure(NamedTuple):
    """A helper failure, distinguishable from Git's exit while preserving the two-item return shape."""

    code: int
    detail: str


def git(args: list[str], cwd: Path, view: git_view.View | git_view.Unviewable | None = None, *,
        expected_common: Path | None = None) -> tuple[int, str]:
    """One git call that reads, through the sanitised view (#443). Never a shell, always a list, always bounded.
    With `view` (the audit's one opened view per tree, #548) the read runs inside it instead of building and
    removing a view for one call; a view opened for another tree is an unreadable refusal, never a wrong-tree answer."""
    try:
        if isinstance(view, git_view.Unviewable):
            raise view
        if view is None:
            options = {"expected_common": expected_common} if expected_common is not None else {}
            out = git_view.run(args, cwd, env=audit_env(), timeout=GIT_TIMEOUT, **options)
        elif _for_tree(view, cwd) and (expected_common is None or view.layout.common == expected_common.resolve()):
            out = git_view.in_view(view, args, timeout=GIT_TIMEOUT)
        else:
            return ReadFailure(128, "unreadable: the opened view is not this tree's")
    except (git_view.Unviewable, RuntimeError, ValueError) as exc:  # RuntimeError: no home to put the view in; ValueError: a path it cannot write
        return ReadFailure(128, f"unreadable: {exc}")
    except (OSError, subprocess.SubprocessError) as exc:
        return ReadFailure(128, f"{type(exc).__name__}: {exc}")  # 1 is a real answer to `--is-ancestor`
    return out.returncode, (out.stdout or out.stderr).strip()


def _for_tree(view: git_view.View, cwd: Path) -> bool:
    """Whether `view` was opened for `cwd`'s repository: its working tree, or the git dir itself when bare."""
    try:
        return (view.layout.tree or view.layout.gitdir) == Path(cwd).resolve()
    except OSError:  # a cwd that cannot be resolved is not this view's
        return False


def _count(rev_range: str, tree: Path, view: git_view.View | git_view.Unviewable | None = None, *,
           expected_common: Path | None = None) -> int | None:
    code, out = git(["rev-list", "--count", rev_range], tree, view, expected_common=expected_common)
    return int(out) if code == 0 and out.isdecimal() else None


def branch_of(tree: Path, view: git_view.View | git_view.Unviewable | None = None, *,
              expected_common: Path | None = None) -> str:
    """The branch HEAD is on; one fixed placeholder when git cannot say, never its error text (it is printed everywhere)."""
    code, branch = git(["rev-parse", "--abbrev-ref", "HEAD"], tree, view, expected_common=expected_common)
    return branch if code == 0 else UNREADABLE_BRANCH


def read_state(tree: Path, view: git_view.View | git_view.Unviewable | None = None, *,
               expected_common: Path | None = None) -> TreeState:
    branch = branch_of(tree, view, expected_common=expected_common)
    code, dirty = git(["status", "--porcelain"], tree, view, expected_common=expected_common)
    readable = code == 0  # an unreadable status cannot rule out uncommitted work: leave the counts unknown, so the tree stays at risk
    return TreeState(
        branch=branch,
        dirty=len(dirty.splitlines()) if readable else None,
        off_origin=_count(f"{BASE}..HEAD", tree, view, expected_common=expected_common) if readable else None,
        unpushed=_count("@{u}..HEAD", tree, view, expected_common=expected_common) if readable else None,
    )


def trusted_commons(clones: list[Path]) -> dict[Path, Path]:
    """Pin registered trees from known clones' admin directories, never from a tree's worker-written `.git`."""
    found: dict[Path, Path] = {}
    try:
        for clone in dict.fromkeys(clones):
            layout = git_view.locate(clone)
            common = layout.common
            found[clone.resolve()] = common
            found[common] = common
            admins = common / "worktrees"
            try:
                entries = sorted(admins.iterdir())
            except FileNotFoundError:
                continue
            for admin in entries:
                back = git_view._read(admin / "gitdir", 4096).decode().rstrip("\r\n")
                path = Path(back)
                if not path.is_absolute() or path.name != ".git" or any(c in back for c in "\r\n"):
                    raise git_view.Unviewable("invalid registered worktree pointer")
                tree = path.parent.resolve()
                if tree in found and found[tree] != common:
                    raise git_view.Unviewable("worktree is registered to more than one repository")
                found[tree] = common
    except (git_view.Unviewable, OSError, ValueError, RuntimeError):
        raise ValueError("known repository registrations are unreadable") from None
    return found


def discover(root: Path, trees: str) -> list[Path]:
    """Every git tree directly under the Worktrees dir, resolved and inside the root. With no
    `Worktrees:` line there is no dir to walk: the root's own children include the main clone."""
    if not trees:
        return []
    base, top = (root / trees).resolve(), root.resolve()
    found = []
    for child in sorted(base.iterdir()) if base.is_dir() else []:
        path = child.resolve()
        if path.is_relative_to(top) and (path / ".git").exists():
            found.append(path)
    return found


def sha_trees(root: Path, trees: str, commons: dict[Path, Path] | None = None) -> list[tuple[str, Path]]:
    """(name, tree) for each repository `--sha` should ask: the first tree by name of each repository `discover`
    finds (trees sharing a common git dir are one, as is a pruned
    worktree with its repository), else the root named `.` when it is a git tree, else none."""
    seen: set[Path] = set()
    found = []
    commons = commons or {}
    for tree in discover(root, trees):
        try:
            key = git_view.locate(tree, expected_common=commons.get(tree)).common.resolve()
        except git_view.Unviewable:  # unreadable: its pruned repository, else itself
            pruned = _pruned_common(tree)
            key = commons.get(tree) or (pruned.resolve() if pruned else tree)
        if key not in seen:
            seen.add(key)
            found.append((tree.name, tree))
    if not found and (root / ".git").exists():
        found.append((".", root.resolve()))
    return found


def fetch_base(tree: Path, timeout: int = 30) -> str:
    return fetch_branch(tree, "main", timeout=timeout)


def fetch_branch(tree: Path, branch: str, timeout: int = 30) -> str:
    """`""` once `origin/<branch>` is fetched, else a timeout line or the first `fatal:` line (else the last), with the
    tree's path blanked and every non-printable character escaped (`clean`), since it is printed. The refspec is
    explicit and forced, so the ref moves whatever `remote.origin.fetch` says and follows a rewritten branch, and
    `--no-tags` keeps it the only ref written. Not through `git()`: the fetch writes into a tree a worker can write, so
    it carries `GIT_WRITE_PINS` and runs on `write_env()` (#556) — the clone's own hooks and helpers pinned dark while
    the caller's credentials ride along. A fetch that loses a race for the ref (`cannot lock ref`) runs once more.
    `branch` names a branch the caller has checked (`check_branch`); it is an argv element, never a shell word."""
    ref = f"refs/remotes/origin/{branch}"
    try:
        for _ in range(2):  # a concurrent fetch holding the ref has moved it by the second try
            out = subprocess.run(
                ["git", *GIT_WRITE_PINS, "-C", str(tree), "fetch", "-q", "--no-tags", "origin", f"+refs/heads/{branch}:{ref}"],
                capture_output=True, text=True, errors="backslashreplace", timeout=timeout,  # stderr from ssh or a helper may hold any byte
                env=write_env(),
            )
            if out.returncode == 0 or "cannot lock ref" not in out.stderr:
                break
    except subprocess.TimeoutExpired:
        return f"git fetch timed out after {timeout}s"
    except (OSError, subprocess.SubprocessError) as exc:  # not `exc`'s text: it can repeat the tree's path
        return f"git fetch did not run ({type(exc).__name__})"
    if out.returncode == 0:
        return ""
    lines = [x for x in out.stderr.strip().split("\n") if x]  # not `splitlines()`: it also splits at `\r` and U+2028, text a remote controls
    line = next((x for x in lines if x.startswith("fatal:")), lines[-1] if lines else f"git fetch exited {out.returncode}")
    return clean(line.replace(str(tree.resolve()), ".").replace(str(tree), "."))  # resolved first: it contains the plain path


def clean(text: str) -> str:
    """`text` with every character `str.isprintable` rejects (a control character, DEL, a C1 control, a bidi or zero-width
    character, U+2028, U+2029) spelled as `repr` spells it, so what a remote or a tree name says cannot forge or split a
    line. Escapes only those: cleaning twice is a no-op."""
    return "".join(c if c.isprintable() else repr(c)[1:-1] for c in text)


def _local_note(local: bool) -> str:
    return LOCAL_NOTE if local else ""


def sha_verdict(sha: str, fetch_error: str, exists: bool, ancestor: bool, tip: str, local: bool = False) -> tuple[int, str]:
    """(exit code, one line): 0 only when fetched and on origin/main, 1 fetched and not, 2 unknown because the fetch
    failed: a stale ref cannot say yes, since main may have been rewritten since. `local` (local main holds the commit)
    is said on the unknown line, which would otherwise drop it (`LOCAL_NOTE`)."""
    if fetch_error:
        held = "holds" if ancestor else "does not hold"
        return 2, f"sha {sha}: unknown \u2014 fetch failed: {fetch_error}; {BASE} {tip} as last fetched {held} it{_local_note(local)}"
    if ancestor:
        return 0, f"sha {sha}: on {BASE} {tip} (fetched)"
    if not exists:
        return 1, f"sha {sha}: no such commit after fetch, so not on {BASE} {tip}"
    return 1, f"sha {sha}: not on {BASE} {tip} (fetched)"


def sha_exit(answers: list[tuple[int, bool]]) -> int:
    """The exit over every repository's `(code, held)`, `held` being that it has the commit, or may have it (git cannot
    read it): 1 if one that holds it says no (a clone or a fresh `git init` whose origin was
    pointed at the commit cannot outvote it); else 2 if there is none, or one is unknown and held (a repository that
    may hold the commit may be the project the claim is about, so it blocks a yes); else 0 if any says yes; else 2 if
    any is unknown; else 1. An unknown that does not hold the commit (no remote, offline,
    shallow) blocks no yes, so an unrelated repository cannot veto it forever."""
    if (1, True) in answers:
        return 1
    if not answers or (2, True) in answers:
        return 2
    codes = [code for code, _ in answers]
    return 0 if 0 in codes else 2 if 2 in codes else 1


def _commits(sha: str, tree: Path, *, expected_common: Path | None = None) -> list[str] | None:
    """The commit ids that start with `sha`, or None when git could not read the repository. `--disambiguate` lists
    ids of every type without reading refs, so a blob or tree is dropped and a tag object's id is not a commit."""
    code, ids = git(["rev-parse", f"--disambiguate={sha}"], tree, expected_common=expected_common)
    types = [(i, *git(["cat-file", "-t", i], tree, expected_common=expected_common)) for i in ids.split()] if code == 0 else []
    if code != 0 or any(c != 0 for _, c, _ in types):
        return None
    return [i for i, _, kind in types if kind == "commit"]


read_regular = git_view.read_regular


def _pruned_common(tree: Path) -> Path | None:
    """The repository (`<common>`) of a pruned worktree, else None. `tree` is pruned when its `.git` is a regular file (decided on the
    opened one) of at most 4096 bytes whose content is `gitdir: <path>` (that exact prefix, as git requires) with
    `<path>` = `<common>/worktrees/<name>`, KNOWN gone (`lstat` says not found; a permission error, any other `OSError` or a dangling
    symlink is present or unknown). Only the trailing run of `\r`/`\n` is trimmed, as git does; every other byte after `gitdir: ` is the
    path, and a path left with one is no path. Read from the file and the filesystem, never git (a probe can time out). Whether
    `<common>` is itself the git directory git uses there (rule A) is not asked here: `check_sha` asks git, once, and follows the
    redirect once (rule B)."""
    try:
        data = read_regular(tree / ".git", 4097)  # one past the bound: content beyond it is never ignored
        if data is None or len(data) > 4096:
            return None
        line = data.decode()
        path = line.removeprefix("gitdir: ").rstrip("\r\n")
        target = tree / path  # an absolute path replaces `tree`
        if line.startswith("gitdir: ") and not {"\r", "\n"} & set(path) and target.parent.name == "worktrees":
            try:
                target.lstat()
            except FileNotFoundError:
                return target.parent.parent
    except (OSError, ValueError):
        pass
    return None


def _is_git_dir(common: Path) -> bool:
    """Whether `common` is itself a bare-style git directory (no tree) real git would use, by files only: the view's layout
    check plus its HEAD check (an empty or garbage HEAD is no repository to git)."""
    try:
        lay = git_view.locate(common, expected_common=common)
        git_view._head(lay)
    except git_view.Unviewable:
        return False
    return lay.tree is None


def check_sha(sha: str, tree: Path, *, expected_common: Path | None = None) -> tuple[int, str, bool]:
    """One repository's `(exit code, line, held)`; `held` is whether it has a commit with that id, or may have one (git
    cannot read it). A pruned worktree (`_pruned_common`: its `.git` file names a `<common>/worktrees/<name>` known gone) is
    answered by its repository, checked only when the commit lookup failed, with two rules. A: only when `<common>` is itself
    the git directory (`_is_git_dir`), else a plain directory inside another repository would
    answer for that one. B: the redirect is followed once, and the answer is `_verdict`, which never redirects.
    Every other failed read (a refusal, a timeout, an unusable `.git` directory, a main clone that moved, a `<common>` git
    cannot use) may hold the commit and blocks a yes. The commit is the one whose object id starts with `sha`, never a tag or
    branch named like it, and the question is about `refs/remotes/origin/main` only. Exit 0 only when the fetch worked and
    origin/main holds it; 1 when it does not; 2 (unknown, never a no) for every cause: the fetch
    failed (a stale ref cannot say yes), git cannot read the repository (the commit lookup, the signals from files that
    decide grafts and shallow, or, when origin/main does not hold it, the local main lookup or ancestry), there are grafts
    (checked after the fetch, which a hook may have written one in), no origin/main, two commits share the prefix, git
    cannot compare, or the clone is shallow and the commit may be in what was cut off. A repository with no
    `refs/heads/main` is simply not on local main. The unknown lines say `; local main holds it` when it has the
    commit."""
    if expected_common is not None:
        try:
            git_view.locate(tree, expected_common=expected_common)
        except git_view.Unviewable:
            if _pruned_common(tree) != expected_common:
                return _verdict(sha, tree, "", None, expected_common=expected_common)
    fetch_error = fetch_base(expected_common or tree)
    commits = _commits(sha, tree, expected_common=expected_common)
    if (commits is None and (common := _pruned_common(tree))
            and (expected_common is None or common.resolve() == expected_common) and _is_git_dir(common)):  # A (#294)
        tree, fetch_error = common, fetch_base(common)  # the repository forgot this tree: it answers for it, once (B)
        commits = _commits(sha, tree, expected_common=expected_common)
    return _verdict(sha, tree, fetch_error, commits, expected_common=expected_common)


def _verdict(sha: str, tree: Path, fetch_error: str, commits: list[str] | None, *,
             expected_common: Path | None = None) -> tuple[int, str, bool]:
    """`check_sha`'s answer for one repository whose fetch and commit lookup are done. It never redirects."""
    held = commits is None or bool(commits)  # any other repository git could not read may hold it

    def unknown(why: str) -> tuple[int, str, bool]:
        return 2, f"sha {sha}: unknown \u2014 {why}", held

    if commits is None:
        return unknown("git could not read this repository")
    try:
        found = git_view.signals(git_view.locate(tree, expected_common=expected_common))
    except git_view.Unviewable:
        return unknown("git could not read this repository")
    if "grafts" in found:
        return unknown("this repository has grafts, so ancestry cannot be trusted")
    ref = git(["rev-parse", "--verify", "-q", LOCAL_REF], tree, expected_common=expected_common)[0] if len(commits) == 1 else 1  # 1: no such ref
    local_code = git(["merge-base", "--is-ancestor", commits[0], LOCAL_REF], tree, expected_common=expected_common)[0] if ref == 0 else ref
    note = _local_note(local_code == 0)
    code, tip = git(["rev-parse", "--short", REF], tree, expected_common=expected_common)
    if code != 0:
        return unknown(f"no {BASE} to check against{note}")
    if len(commits) > 1:
        return unknown("more than one commit starts with it")
    if "shallow" in found:  # a shallow clone never answers yes: the commit may be in what was cut off
        return unknown(f"shallow clone, so history is cut off{note}")
    compared = git(["merge-base", "--is-ancestor", commits[0], REF], tree, expected_common=expected_common)[0] if held else 1
    if compared not in (0, 1):
        return unknown(f"git could not compare it with {BASE}")
    if local_code not in (0, 1) and compared != 0:  # origin/main holding it needs no local fact
        return unknown("git could not read this repository")
    code, line = sha_verdict(sha, fetch_error, held, compared == 0, tip, local_code == 0 and compared == 1)
    if code == 1 and local_code == 0:
        line = f"sha {sha}: on local main only, not pushed to {BASE} {tip} (fetched)"
    return code, line, held


def _own_git_dirs(cwd: Path, *, expected_common: Path | None = None) -> list[str]:
    """The git directories codex may write: a linked worktree's common dir and own gitdir, or a clone's `.git`; none if unviewable or bare."""
    try:
        lay = git_view.locate(cwd, expected_common=expected_common)
    except git_view.Unviewable:
        return []
    if lay.tree is None:
        return []
    return [str(lay.common)] if lay.gitdir == lay.common else [str(lay.common), str(lay.gitdir)]


def codex_add_dir_args(cwd: Path, *, expected_common: Path | None = None) -> list[str]:
    """The `--add-dir` argv that grants codex the validated git directories (#428)."""
    return [a for d in _own_git_dirs(cwd, expected_common=expected_common) for a in ("--add-dir", d)]
