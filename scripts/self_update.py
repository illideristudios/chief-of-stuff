"""The engine on chief-of-stuff's own repository installs each new origin/main as its release (Zach, 2026-10-27).

The loop already hands off to whatever `current` names, so moving `current` is the whole reinstall."""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tarfile
import tempfile
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # start_coordinator lives at the release root
import start_coordinator  # noqa: E402

NAME = "chief-of-stuff"


def _git(root: Path, *args: str) -> bytes:
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, check=True, timeout=120).stdout


def follow_main(root: Path, install_dir: Path, seen: Path) -> Path | None:
    """Install origin/main and point `current` at it, once per new main commit; None when there is nothing to do."""
    try:
        if json.loads((root / ".claude-plugin/plugin.json").read_text()).get("name") != NAME:
            return None
    except (OSError, ValueError):
        return None  # not chief-of-stuff's own repository
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
