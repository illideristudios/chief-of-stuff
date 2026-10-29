"""Private ordered engine intent, independent of workspace-writable markers."""
import hashlib
import json
import os
import re
import stat
from pathlib import Path


def label(root: Path) -> str:
    key = hashlib.sha256(os.fsencode(root.resolve())).hexdigest()[:20]
    return f"com.chief-of-stuff.engine.{key}"


def intent_path(root: Path, data: Path, error, *, create: bool = False) -> Path:
    """Keep authoritative ordered intent outside the workspace, in a private directory."""
    root = root.resolve()
    data = data.absolute()
    if data == root or root in data.parents or root in data.resolve().parents or data.resolve() == root:
        raise error("engine data directory must be outside the workspace")
    private = data / "engine-intents"
    if create:
        private.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        info = private.lstat()
    except FileNotFoundError:
        return private / (label(root) + ".json")
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise error("engine intent directory must be private and owned by the current user")
    return private / (label(root) + ".json")


def read(root: Path, data: Path, error) -> dict | None:
    """Read only the private, ordered record; workspace markers are never authoritative."""
    path = intent_path(root, data, error)
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(fd) as handle:
            intent = json.load(handle)
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        raise error(f"cannot read explicit intent: {exc}") from exc
    if not isinstance(intent, dict) or intent.get("operation") not in ("install", "uninstall") or not isinstance(intent.get("pending"), bool):
        raise error("invalid explicit intent")
    if intent["operation"] == "install":
        release, job = intent.get("release"), intent.get("job")
        if (not isinstance(release, str) or not Path(release).is_absolute()
                or not isinstance(job, dict) or job.get("Label") != label(root)
                or job.get("WorkingDirectory") != str(root.resolve())):
            raise error("invalid explicit intent")
        validate_release(root, Path(release), error)
    return intent


def validate_release(root: Path, release: Path, error) -> None:
    """Trust release trees and their ancestors, but never links or indirect pip inputs."""
    root = root.resolve()
    try:
        resolved = release.resolve(strict=True)
        if (resolved != release or resolved == root or root in resolved.parents
                or resolved in root.parents):
            raise error("engine release must be stable and outside the workspace")
        if not resolved.is_dir():
            raise error("engine release must be a directory")
        for directory, dirs, files in os.walk(release, followlinks=False, onerror=lambda exc: (_ for _ in ()).throw(exc)):
            for name in dirs + files:
                path = Path(directory) / name
                if path.is_symlink():
                    raise error("engine release must not contain symlinks or depend on workspace aliases")
        requirements = release / "requirements.txt"
        if requirements.exists():
            for line in requirements.read_text().splitlines():
                line = line.strip()
                if line and not line.startswith("#") and not re.fullmatch(
                        r"[A-Za-z0-9][A-Za-z0-9_.-]*==[A-Za-z0-9][A-Za-z0-9_.+!-]*", line):
                    raise error("engine requirements must contain only pinned package versions")
    except (OSError, RuntimeError, UnicodeError) as exc:
        raise error(f"invalid engine release: {exc}") from exc
