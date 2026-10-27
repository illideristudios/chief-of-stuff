"""The engine on chief-of-stuff's own repository installs each new origin/main as its release (Zach, 2026-10-27).

The loop already hands off to whatever `current` names, so moving `current` is the whole reinstall. Only a
workspace whose `origin` is the upstream the running release names may move it: `current` is shared by every
workspace, so a fork or any other clone carrying chief-of-stuff's files never replaces it."""

from __future__ import annotations

import io
import os
import re
import subprocess
import sys
import tarfile
import tempfile
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # start_coordinator lives at the release root
import start_coordinator  # noqa: E402


def _git(root: Path, *args: str) -> bytes:
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, check=True, timeout=120).stdout


def _repo(url: str) -> str:
    """host/owner/name for git@host:owner/name.git, https://host/owner/name or a local path."""
    url = re.sub(r"^[a-z+]+://", "", url.strip().lower()).removesuffix("/").removesuffix(".git")
    return re.sub(r"^[^@/]+@", "", url).replace(":", "/", 1)


def same_repo(a: str, b: str) -> bool:
    return bool(a.strip()) and _repo(a) == _repo(b)


def follow_main(root: Path, install_dir: Path, seen: Path, upstream: str) -> Path | None:
    """Install origin/main and point `current` at it, once per new main commit; None when there is nothing to do."""
    try:
        origin = _git(root, "remote", "get-url", "origin").decode()
    except (OSError, subprocess.CalledProcessError):
        return None  # not a git checkout with an origin
    if not upstream or not same_repo(origin, upstream):
        return None
    _git(root, "fetch", "--quiet", "origin", "main")
    head = _git(root, "rev-parse", "origin/main").decode().strip()
    if seen.is_file() and seen.read_text().strip() == head:
        return None
    with tempfile.TemporaryDirectory(prefix=".chief-main-") as tmp:
        with tarfile.open(fileobj=io.BytesIO(_git(root, "archive", head))) as archive:
            archive.extractall(tmp, filter="data")
        release = start_coordinator.install(Path(tmp), install_dir)
    staged = install_dir / f".current-{uuid.uuid4().hex}"
    try:
        staged.symlink_to(release.relative_to(install_dir))
        os.replace(staged, install_dir / "current")
    finally:
        staged.unlink(missing_ok=True)
    seen.parent.mkdir(parents=True, exist_ok=True)
    seen.write_text(head + "\n")
    return release
