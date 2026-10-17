"""Read worker-written git data through private, launcher-authored metadata.

Only allowlisted caller environment settings survive; the module pins config isolation.
Refs and objects remain untrusted data; config, hooks, grafts and shallow boundaries
never enter the view. Unless callers supply `expected_common`, workers must not be
able to write metadata outside their tree.
This module does not serve writes or inspect submodules.
"""

from __future__ import annotations

import io
import os
import re
import selectors
import shutil
import stat
import subprocess
import tempfile
import threading
import time
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path


class Unviewable(Exception):
    """A short, path-free reason why the repository cannot supply a safe view."""


class NotARepository(Unviewable):
    """A plain folder: no `.git` entry and none of HEAD, objects, refs."""


class CacheError(Exception):
    """Launcher cache configuration failed; never turn this into a per-tree refusal."""


@dataclass(frozen=True)
class Layout:
    tree: Path | None
    gitdir: Path
    common: Path


@dataclass(frozen=True)
class View:
    layout: Layout
    env: dict[str, str]
    hidden_index_entries: int = 0


def read_regular(path: Path, limit: int, nofollow: bool = False) -> bytes | None:
    """At most `limit` bytes of `path`, or None when it is not a regular file (decided on the opened one, so a swap after
    the open cannot matter). For a file a worker can write: `O_NONBLOCK`, so opening a FIFO cannot block; `nofollow` refuses
    a symlink (an `OSError`, as is any open or read that fails)."""
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | (os.O_NOFOLLOW if nofollow else 0))
    try:
        return os.read(fd, limit) if stat.S_ISREG(os.fstat(fd).st_mode) else None
    finally:
        os.close(fd)


def _read(path: Path, limit: int) -> bytes:
    try:
        data = read_regular(path, limit + 1, nofollow=True)
    except OSError:
        raise Unviewable("metadata is not a readable regular file") from None
    if data is None or len(data) > limit:
        raise Unviewable("metadata is not a bounded regular file")
    return data


def _repository(path: Path) -> None:
    if not stat.S_ISREG((path / "HEAD").lstat().st_mode):
        raise Unviewable("HEAD is not a regular file")
    if not (path / "objects").is_dir() or not (path / "refs").is_dir():
        raise Unviewable("repository data directories are missing")


def locate(path: Path, expected_common: Path | None = None) -> Layout:
    """Resolve a clone, linked tree or bare git directory, optionally pinned to a caller's trusted common dir."""
    def checked(layout: Layout) -> Layout:
        if expected_common is not None and layout.common != Path(expected_common).resolve():
            raise Unviewable("repository common directory is not the expected one")
        return layout

    try:
        tree = Path(path).resolve()
        dotgit = tree / ".git"
        try:
            mode = dotgit.lstat().st_mode
        except FileNotFoundError:
            if tree.is_dir() and not any(os.path.lexists(tree / name) for name in ("HEAD", "objects", "refs")):
                raise NotARepository("not a git repository") from None
            _repository(tree)
            return checked(Layout(None, tree, tree))
        if stat.S_ISDIR(mode):
            common = dotgit.resolve()
            _repository(common)
            return checked(Layout(tree, common, common))
        if not stat.S_ISREG(mode):
            raise Unviewable(".git is neither a directory nor a regular file")
        line = _read(dotgit, 4096).decode()
        target = line.removeprefix("gitdir: ").rstrip("\r\n")
        if not line.startswith("gitdir: ") or not target or "\n" in target or "\r" in target:
            raise Unviewable(".git is not a gitfile")
        gitdir = (tree / target).resolve()
        common_name = _read(gitdir / "commondir", 4096).decode().rstrip("\r\n")
        back_name = _read(gitdir / "gitdir", 4096).decode().rstrip("\r\n")
        if not common_name or not back_name or any(c in common_name + back_name for c in "\r\n"):
            raise Unviewable("invalid worktree pointers")
        common = (gitdir / common_name).resolve()
        back = (gitdir / back_name).resolve()
        if common.is_relative_to(tree) or gitdir.is_relative_to(tree):
            raise Unviewable("linked repository metadata is inside the worktree")
        if gitdir.parent != common / "worktrees" or back != dotgit:
            raise Unviewable("not a linked worktree of its own repository")
        _repository(common)
        return checked(Layout(tree, gitdir, common))
    except (OSError, ValueError, RuntimeError):
        raise Unviewable("repository layout is unreadable") from None


_OID = r"(?:[0-9a-f]{40}|[0-9a-f]{64})"
_NAME = r"[A-Za-z0-9._/-]+"
_BOOL_KEYS = ("core.filemode", "core.ignorecase", "core.precomposeunicode", "core.symlinks")
_INDEX_LIMIT = 512 * 1024 * 1024
_PACKED_REFS_LIMIT = 16 * 1024 * 1024
_CONFIG_LIMIT = 1024 * 1024
_OUTPUT_LIMIT = 16 * 1024 * 1024
_OBJECT_ONLY = frozenset(("branch", "cat-file", "for-each-ref", "log", "ls-files", "ls-tree",
                          "merge-base", "rev-list", "rev-parse", "show", "symbolic-ref"))
# Exact option spellings, restricted operand count, required read option, operand policy.
# A named operand group consumes a separate option value, not a revision/path.
_READ_OPTIONS = {
    "status": (r"--(?:ignored|porcelain|short)", None, None, "path"),
    "rev-parse": (r"--(?:abbrev-ref|absolute-git-dir|git-common-dir|is-shallow-repository|quiet|short|show-toplevel|verify)"
                  r"|-q|--path-format=(?:absolute|relative)|--disambiguate=[0-9a-f]{1,64}"
                  r"|--git-path=.+|(?P<operand>--git-path)", None, None, "mixed"),
    "log": (r"--format=(?!.*%G).+|--(?:no-color|no-ext-diff|no-textconv|stat)|-1", None, None, "mixed"),
    "merge-base": (r"--is-ancestor", None, None, None),
    "diff": (r"--(?:name-only|no-color|no-ext-diff|no-renames|no-textconv|numstat|stat)", None, None, "revision"),
    "show": (r"--format=(?!.*%G).+|--stat", None, None, "mixed"),
    "for-each-ref": (r"--format=.+|(?P<operand>--format)", None, None, None),
    "ls-files": (r"--stage|-z|-v|--others|--exclude-standard", None, None, "path"),
    "ls-tree": (r"-r", None, None, "mixed"),
    "cat-file": (r"--batch-check|-[ept]", None, None, None),
    "rev-list": (r"--(?:count|left-right)", None, None, "mixed"),
    "branch": (r"--show-current", 0, "--show-current", None),
    "symbolic-ref": (r"--short|-q", 1, None, None),
    "check-ref-format": (r"--branch", None, None, None),
}
_SAFE_CALLER_ENV = frozenset(("PATH", "HOME", "GIT_TERMINAL_PROMPT", "GIT_OPTIONAL_LOCKS",
                             "GIT_NO_REPLACE_OBJECTS"))


def _check_args(args: list[str]) -> tuple[list[str], list[str]]:
    if not args or args[0] not in _READ_OPTIONS:
        raise Unviewable("command is not an allowed read")
    if any(arg.startswith(("--git-dir", "--work-tree", "--output", "--submodule", "--ignore-submodules"))
           for arg in args):
        raise Unviewable("repository, output and submodule options are not allowed")
    pattern, count, required, policy = _READ_OPTIONS[args[0]]
    revisions, paths = [], []
    operands = 0
    options = set()
    separated = False
    tokens = iter(args[1:])
    for arg in tokens:
        if arg == "--" and not separated:
            separated = True
        elif separated or not arg.startswith("-"):
            operands += 1
            if policy is not None:
                paths.append(arg)
                if policy == "revision" and not separated:
                    revisions.append(arg)
        else:
            match = re.fullmatch(pattern, arg)
            if match is None:
                raise Unviewable("option is not an allowed read")
            options.add(arg)
            if match.groupdict().get("operand"):
                value = next(tokens, None)
                if not value or value.startswith("-"):
                    raise Unviewable("option value is missing")
    if (count is not None and operands != count) or (required is not None and required not in options):
        raise Unviewable("arguments are not an allowed read form")
    return revisions, paths


def _check_operand(arg: str, tree: Path) -> None:
    try:
        if (tree / arg).resolve().is_relative_to(tree):
            return
    except (OSError, ValueError, RuntimeError):
        pass
    raise Unviewable("operand is outside the repository tree")


def _run(args: list[str], *, cwd: Path, env: dict[str, str], timeout: float,
         text: bool = True, input: bytes | None = None) -> subprocess.CompletedProcess:
    # Drain both pipes concurrently, with a shared byte budget. Closing the readers
    # on overflow stops a producer via SIGPIPE; the subprocess deadline still applies.
    deadline = time.monotonic() + timeout
    chunks: list[list[bytes]] = [[], []]
    errors: list[Exception] = []
    with ExitStack() as stack:
        readers, writers = [], []
        for _ in chunks:
            read_fd, write_fd = os.pipe()
            readers.append(stack.enter_context(os.fdopen(read_fd, "rb")))
            writers.append(stack.enter_context(os.fdopen(write_fd, "wb")))
        stdin = subprocess.DEVNULL
        if input is not None:
            stdin = stack.enter_context(tempfile.TemporaryFile())
            stdin.write(input)
            stdin.seek(0)

        def capture() -> None:
            remaining = _OUTPUT_LIMIT
            try:
                with selectors.DefaultSelector() as selector:
                    for number, reader in enumerate(readers):
                        selector.register(reader, selectors.EVENT_READ, number)
                    while selector.get_map():
                        for key, _ in selector.select(_remaining(deadline)):
                            chunk = os.read(key.fd, min(65536, remaining + 1))
                            if not chunk:
                                selector.unregister(key.fileobj)
                                continue
                            remaining -= len(chunk)
                            if remaining < 0:
                                raise Unviewable("captured git output is too large")
                            chunks[key.data].append(chunk)
            except Exception as exc:
                errors.append(exc)
            finally:
                for reader in readers:
                    reader.close()

        collector = threading.Thread(target=capture)
        collector.start()
        try:
            out = subprocess.run(["git", *args], cwd=cwd, env=env, stdin=stdin,
                                 stdout=writers[0], stderr=writers[1], timeout=timeout)
        finally:
            for writer in writers:
                writer.close()
            collector.join()
            if errors and isinstance(errors[0], Unviewable):
                raise errors[0]
        if errors:
            raise errors[0]
    stdout, stderr = (b"".join(parts) for parts in chunks)
    if text:
        stdout, stderr = (io.TextIOWrapper(io.BytesIO(data), errors="backslashreplace").read()
                          for data in (stdout, stderr))
    return subprocess.CompletedProcess(out.args, out.returncode, stdout, stderr)


def _head(layout: Layout) -> tuple[bytes, str | None]:
    data = _read(layout.gitdir / "HEAD", 1024)
    try:
        text = data.decode("utf-8").removesuffix("\n").removesuffix("\r")
    except UnicodeDecodeError:
        raise Unviewable("HEAD is not UTF-8") from None
    if re.fullmatch(_OID, text):
        return data, None
    if text.startswith("ref: refs/"):
        ref = text.removeprefix("ref: ")
        # Git ref syntax excludes these bytes, not non-ASCII or punctuation in general.
        if (not re.search(r"[\x00-\x20\x7f\\~^:?*\[]", ref)
                and ".." not in ref and "//" not in ref and "@{" not in ref
                and not any(part.startswith(".") or part.endswith(".lock") for part in ref.split("/"))
                and not ref.endswith(("/", ".")) and not ref.startswith("refs/heads/-")):
            branch = ref.removeprefix("refs/heads/") if ref.startswith("refs/heads/") else None
            return data, branch
    raise Unviewable("HEAD is neither a safe symbolic ref nor an object id")


def _config(layout: Layout, branch: str | None, env: dict[str, str], timeout: float, directory: Path) -> str:
    # Parse only the bounded snapshot, never reopen a worker-controlled path.
    source = directory / "config-source"
    source.write_bytes(_read(layout.common / "config", _CONFIG_LIMIT))
    try:
        out = _run(["config", "--file", str(source), "--no-includes", "-z", "--list"],
                   cwd=Path("/"), env=env, timeout=timeout)
    finally:
        source.unlink()
    if out.returncode:
        raise Unviewable("config is unreadable")
    facts: dict[tuple[str, str], list[str]] = {}
    fmt = "sha1"
    for record in out.stdout.split("\0"):
        key, _, value = record.partition("\n")
        if key.lower() in _BOOL_KEYS and value in ("true", "false"):
            facts[("core", key.lower().split(".", 1)[1])] = [value]
        elif key.lower() == "extensions.objectformat":
            if value not in ("sha1", "sha256"):
                raise Unviewable("unsupported object format")
            fmt = value
        elif key.lower() == "extensions.refstorage" and value != "files":
            raise Unviewable("unsupported ref storage")
        elif branch and key in (f"branch.{branch}.remote", f"branch.{branch}.merge"):
            if re.fullmatch(_NAME, value) and ".." not in value:
                quoted_branch = branch.replace('"', '\\"')
                facts[(f'branch "{quoted_branch}"', key.rsplit(".", 1)[1])] = [value]
        else:
            remote = re.fullmatch(r"remote\.([A-Za-z0-9._-]+)\.fetch", key)
            if (remote and ".." not in remote[1] and ".." not in value
                    and re.fullmatch(r"\+?[A-Za-z0-9._/*-]+:[A-Za-z0-9._/*-]+", value)):
                section = f'remote "{remote[1]}"'
                fetches = facts.setdefault((section, "fetch"), [])
                if value not in fetches:
                    fetches.append(value)
                facts[(section, "url")] = ["/nonexistent"]
    lines = ["[core]", f"\trepositoryformatversion = {int(fmt == 'sha256')}",
             f"\tbare = {str(layout.tree is None).lower()}", "\tfsmonitor = false", "\tuntrackedCache = false",
             "\tcommitGraph = false", "\tmultiPackIndex = false", "\tuseReplaceRefs = false", f"\thooksPath = {os.devnull}",
             f"\tattributesFile = {os.devnull}", f"\texcludesFile = {os.devnull}"]
    for (section, key), values in facts.items():
        if section == "core":
            lines.append(f"\t{key} = {values[0]}")
    if fmt == "sha256":
        lines.extend(["[extensions]", "\tobjectformat = sha256"])
    lines.extend(["[status]", "\tsubmoduleSummary = false", "[diff]", "\tignoreSubmodules = all",
                  "[pack]", "\tuseBitmaps = false", "[gpg]", f"\tprogram = {os.devnull}"])
    for (section, key), values in facts.items():
        if section != "core":
            lines.append(f"[{section}]")
            lines.extend(f"\t{key} = {value}" for value in values)
    return "\n".join(lines) + "\n"


def _copy_index(source: Path, target: Path) -> None:
    try:
        fd = os.open(source, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    except FileNotFoundError:
        return
    except OSError:
        raise Unviewable("index is not a readable regular file") from None
    with os.fdopen(fd, "rb") as src:
        info = os.fstat(src.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > _INDEX_LIMIT:
            raise Unviewable("index is not a bounded regular file")
        remaining = _INDEX_LIMIT
        with target.open("wb") as dst:
            while chunk := src.read(min(1024 * 1024, remaining + 1)):
                remaining -= len(chunk)
                if remaining < 0:
                    raise Unviewable("index is too large")
                dst.write(chunk)
        # Keep Git's racy-index content checks tied to the original timestamp.
        os.utime(target, ns=(info.st_atime_ns, info.st_mtime_ns))


def _system_temp_roots() -> tuple[Path, ...]:
    """Refuse system temp spellings on every host, plus their resolved forms."""
    roots = tuple(Path(root) for root in ("/tmp", "/var/tmp", "/private/tmp", "/private/var/tmp",
                                         tempfile.gettempdir()))
    return (*roots, *(root.resolve() for root in roots))


def _base(layout: Layout, base: Path | None) -> Path:
    explicit = base is not None
    candidate = str(base) if explicit else os.environ.get("XDG_CACHE_HOME") or "launcher home cache"
    try:
        if base is None:
            cache = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
            candidate = str(cache)
            if not cache.is_absolute():
                raise CacheError(f"view cache base {candidate!r} is relative")
            base = cache / "chief-of-stuff/git-views"
        base = Path(base).resolve()
        candidate = str(base)
        forbidden = [layout.tree, layout.common, layout.gitdir]
        if not explicit:
            forbidden.extend(_system_temp_roots())
        if any(root is not None and base.is_relative_to(root) for root in forbidden):
            raise CacheError(f"view cache base {candidate!r} would be worker-writable")
        base.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not explicit:
            base.chmod(0o700)
    except (OSError, ValueError, RuntimeError):
        raise CacheError(f"cannot prepare view cache base {candidate!r}") from None
    cutoff = time.time() - 24 * 60 * 60
    try:
        entries = os.scandir(base)
    except OSError:
        raise CacheError(f"cannot read view cache base {candidate!r}") from None
    with entries:
        for entry in entries:
            if entry.name.startswith("view-"):
                try:
                    info = entry.stat(follow_symlinks=False)
                    if stat.S_ISDIR(info.st_mode) and info.st_mtime < cutoff:
                        shutil.rmtree(entry.path, ignore_errors=True)
                except OSError:
                    pass
    return base


@contextmanager
def _opened(path: Path, env: dict[str, str], base: Path | None, deadline: float,
            expected_common: Path | None = None) -> Iterator[View]:
    env = {key: value for key, value in env.items() if key in _SAFE_CALLER_ENV}
    env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_SYSTEM=os.devnull, GIT_CONFIG_NOSYSTEM="1")
    layout = locate(path, expected_common=expected_common)
    if (layout.common / "reftable").exists():
        raise Unviewable("reftable ref storage")
    cache = _base(layout, base)
    try:
        directory = Path(tempfile.mkdtemp(prefix="view-", dir=cache))
    except OSError:
        raise CacheError(f"cannot allocate a view in cache base {str(cache)!r}") from None
    try:
        directory.chmod(0o700)
        head, branch = _head(layout)
        (directory / "HEAD").write_bytes(head)
        (directory / "refs").symlink_to(layout.common / "refs", target_is_directory=True)
        if os.path.lexists(layout.common / "packed-refs"):
            (directory / "packed-refs").write_bytes(_read(layout.common / "packed-refs", _PACKED_REFS_LIMIT))
        (directory / "objects/info").mkdir(parents=True)
        objects = str(layout.common / "objects")
        if "\n" in objects or "\r" in objects:
            raise Unviewable("object directory cannot be represented")
        (directory / "objects/info/alternates").write_text(objects + "\n")
        if layout.tree is not None:
            _copy_index(layout.gitdir / "index", directory / "index")
            for shared in layout.gitdir.glob("sharedindex.*"):
                if re.fullmatch(r"sharedindex\." + _OID, shared.name):
                    _copy_index(shared, directory / shared.name)
        (directory / "config").write_text(_config(layout, branch, env, _remaining(deadline), directory))
        child_env = {**env, "GIT_DIR": str(directory)}
        hidden = set()
        if layout.tree is not None:
            child_env.update(GIT_WORK_TREE=str(layout.tree), GIT_INDEX_FILE=str(directory / "index"))
            # Older Git can trust copied fsmonitor-valid bits despite core.fsmonitor=false.
            stamp = (directory / "index").stat() if (directory / "index").is_file() else None  # an unborn tree has none
            files = _run(["ls-files", "-v", "-z"], cwd=directory, env=child_env,
                         text=False, timeout=_remaining(deadline))
            if files.returncode:
                raise Unviewable("index flags cannot be read")
            hidden = {entry[2:] for entry in files.stdout.split(b"\0")
                      if entry[:1].islower() or entry.startswith(b"S ")}
            index = _run(["update-index", "--no-fsmonitor"], cwd=directory, env=child_env,
                         timeout=_remaining(deadline))
            if index.returncode:
                raise Unviewable("index flags cannot be cleared")
            if hidden:
                paths = b"\0".join(sorted(hidden)) + b"\0"
                # Git's per-entry flag operations are mutually exclusive. Clear each
                # bit in its own call, with binary NUL paths, only in the private index.
                for flag in ("--no-assume-unchanged", "--no-skip-worktree"):
                    index = _run(["update-index", flag, "-z", "--stdin"], cwd=directory,
                                 env=child_env, timeout=_remaining(deadline), input=paths)
                    if index.returncode:
                        raise Unviewable("index flags cannot be cleared")
            # The rewrite moves the copy's mtime to now, so an edit made in the original's second stops looking racy.
            if stamp is not None:
                os.utime(directory / "index", ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
        view = View(layout, child_env, len(hidden))
    except BaseException:
        shutil.rmtree(directory, ignore_errors=True)
        raise
    try:
        yield view
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def _remaining(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise subprocess.TimeoutExpired("git view", 0)
    return remaining


@contextmanager
def opened(path: Path, env: dict[str, str], base: Path | None = None, *,
           expected_common: Path | None = None) -> Iterator[View]:
    """Build one view for several reads; always remove it when the body exits."""
    options = {"expected_common": expected_common} if expected_common is not None else {}
    with _opened(path, env, base, time.monotonic() + 15, **options) as view:
        yield view


def run(args: list[str], path: Path, *, env: dict[str, str], timeout: int = 15,
        base: Path | None = None, expected_common: Path | None = None) -> subprocess.CompletedProcess[str]:
    """Build, read and remove a view within one subprocess time budget."""
    revisions, paths = _check_args(args)
    deadline = time.monotonic() + timeout
    if paths:
        layout = locate(path, expected_common=expected_common)
        for operand in paths:
            _check_operand(operand, layout.tree or layout.gitdir)
    options = {"expected_common": expected_common} if expected_common is not None else {}
    with _opened(path, env, base, deadline, **options) as view:
        return _finish(view, args, revisions, deadline)


def in_view(view: View, args: list[str], *, timeout: int) -> subprocess.CompletedProcess[str]:
    """One allowed read against an open view, on its own budget (#548): every argument is checked the way `run`
    checks it — against the view's own layout — so sharing the view never widens what a read may do."""
    revisions, paths = _check_args(args)
    deadline = time.monotonic() + timeout
    for operand in paths:
        _check_operand(operand, view.layout.tree or view.layout.gitdir)
    return _finish(view, args, revisions, deadline)


def _finish(view: View, args: list[str], revisions: list[str],
            deadline: float) -> subprocess.CompletedProcess[str]:
    """The read `run` and `in_view` share: revision and submodule checks on the view, then the command."""
    command = args[0]
    cwd = view.layout.tree or Path(view.env["GIT_DIR"])
    for revision in revisions:
        resolved = _run(["rev-parse", "--revs-only", "--no-flags", "--end-of-options", revision],
                        cwd=cwd, env=view.env, timeout=_remaining(deadline))
        if (resolved.returncode or not resolved.stdout
                or any(not re.fullmatch(r"\^?" + _OID, oid) for oid in resolved.stdout.splitlines())):
            raise Unviewable("paths require an explicit separator")
    if command not in _OBJECT_ONLY:
        index = _run(["ls-files", "--stage", "-z"], cwd=cwd, env=view.env,
                     text=False, timeout=_remaining(deadline))
        if index.returncode:
            raise Unviewable("index is unreadable")
        if any(entry.startswith(b"160000 ") for entry in index.stdout.split(b"\0")):
            raise Unviewable("submodules are not inspected")
    return _run(args, cwd=cwd, env=view.env, timeout=_remaining(deadline))


def signals(layout: Layout) -> frozenset[str]:
    """Conservative ancestry warnings from real metadata, never from the view."""
    found = set()
    for name, path in (("grafts", layout.common / "info/grafts"), ("shallow", layout.common / "shallow")):
        try:
            info = path.stat()
        except FileNotFoundError:
            continue
        except OSError:
            found.add(name)
        else:
            if name == "shallow" or not stat.S_ISREG(info.st_mode) or info.st_size > 0:
                found.add(name)
    return frozenset(found)
