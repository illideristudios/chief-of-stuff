"""The engine on chief-of-stuff's own repository installs each new origin/main as its release (the owner, 2026-10-27).

The loop already hands off to whatever `current` names, so moving `current` is the whole reinstall. Only a
workspace whose `origin` is the upstream the running release names may move it: `current` is shared by every
workspace, so a fork or any other clone carrying chief-of-stuff's files never replaces it."""

from __future__ import annotations

import contextlib
import io
import re
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # chief_of_stuff lives at the release root
import chief_of_stuff  # noqa: E402


MAIN = "refs/chief-of-stuff/main"  # private to self-update, outside every remote-tracking namespace


def _git(root: Path, *args: str) -> bytes:
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, check=True, timeout=120).stdout


REMOTE = (re.compile(r"(?:https|ssh)://(?:[^@/]+@)?([^/:@]+)(?::\d+)?/([\w.-]+/[\w.-]+?)(?:\.git)?/?", re.I),
          re.compile(r"(?:[^@/:]+@)?([^/:@]+):([\w.-]+/[\w.-]+?)(?:\.git)?/?"))  # scp-style user@host:owner/name


def _remote(url: str) -> tuple[str, str] | None:
    """(host, owner/name) of a network remote; None for a local path, file:// or any other transport."""
    for pattern in REMOTE:
        if (m := pattern.fullmatch(url.strip())) and ".." not in m[2]:
            return m[1].lower(), m[2].lower()
    return None


def same_repo(a: str, b: str) -> bool:
    return (remote := _remote(a)) is not None and remote == _remote(b)


def follow_main(root: Path, install_dir: Path, seen: Path, upstream: str, *,
                bin_dir: Path = chief_of_stuff.DEFAULT_BIN_DIR, lock=contextlib.nullcontext) -> dict | None:
    """Install origin/main the way `chief-of-stuff install` does, once per new main commit; the install's result,
    or None when there is nothing to do. The fetch runs outside `lock`; the install runs inside it."""
    try:
        origin = _git(root, "remote", "get-url", "origin").decode()
    except (OSError, subprocess.CalledProcessError):
        return None  # not a git checkout with an origin
    if not upstream or not same_repo(origin, upstream):
        return None
    # An explicit refspec, so a configured one can never map another ref onto what is installed.
    _git(root, "fetch", "--quiet", "--no-write-fetch-head", "origin", f"+refs/heads/main:{MAIN}")
    head = _git(root, "rev-parse", MAIN).decode().strip()
    if seen.is_file() and seen.read_text().strip() == head:
        return None
    with tempfile.TemporaryDirectory(prefix=".chief-main-") as tmp:
        with tarfile.open(fileobj=io.BytesIO(_git(root, "archive", head))) as archive:
            archive.extractall(tmp, filter="data")
        with lock():
            result = chief_of_stuff.install(source=Path(tmp), install_dir=install_dir, bin_dir=bin_dir)
    seen.parent.mkdir(parents=True, exist_ok=True)
    seen.write_text(head + "\n")
    return result
