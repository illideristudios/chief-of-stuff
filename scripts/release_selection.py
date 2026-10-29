"""Select the installed release or a newer plugin-cache sibling requested for a workspace."""

import json
from pathlib import Path

import engine_intent


def _plugin_version(release: Path) -> tuple[int, ...] | None:
    parent = release.parent
    if len(parent.parents) < 3 or parent.parent.parent.name != "cache" or parent.parent.parent.parent.name != "plugins":
        return None
    try:
        version = json.loads((release / ".claude-plugin/plugin.json").read_text())["version"]
        if not version or any(not part.isdigit() for part in version.split(".")):
            return None
        if not (release / "scripts/engine.py").is_file() or not (release / "requirements.txt").is_file():
            return None
        return tuple(int(part) for part in version.split("."))
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None


def validate_handoff(root: Path, release: Path, supervisor) -> None:
    """Reject workspace code, including symlinked inputs, at each handoff boundary."""
    engine_intent.validate_release(root, release, supervisor.ServiceError)
    root = root.resolve()
    for path in (release / "scripts/pages.py", release / ".claude-plugin/plugin.json",
                 release / "chief_of_stuff.py"):
        try:
            target = path.resolve()
            if target == root or root in target.parents:
                raise supervisor.ServiceError("board release must not point into the workspace")
        except (OSError, RuntimeError) as exc:
            raise supervisor.ServiceError(f"invalid board release: {exc}") from exc


def explicit_release(root: Path, supervisor, *, completed_only: bool = False) -> Path | None:
    intent = engine_intent.read(root, supervisor.data_dir(), supervisor.ServiceError)
    if intent is None or intent["operation"] != "install" or (completed_only and intent["pending"]):
        return None
    chosen = Path(intent["release"])
    # Completed installed releases follow the stable current pointer, as in the engine.
    if not intent["pending"] and chosen.parent.name == "versions":
        current = supervisor.current_release()
        if current.parent == chosen.parent:
            chosen = current
    validate_handoff(root, chosen, supervisor)
    return chosen


def selected_release(root: Path, supervisor) -> Path:
    """Select from private intent or independently verified pointer and cache marker."""
    explicit = explicit_release(root, supervisor)
    if explicit is not None:
        return explicit
    current = supervisor.current_release()
    if current != supervisor.RELEASE:
        try:
            validate_handoff(root, current, supervisor)
            return current
        except supervisor.ServiceError:
            return supervisor.RELEASE
    return marker_release(root, current, supervisor) or current


def marker_release(root: Path, current: Path, supervisor) -> Path | None:
    """Return a validated newer marker selection, never the caller fallback."""
    current_version = _plugin_version(current)
    if current_version is None:
        return None
    try:
        candidate = Path((supervisor.state_dir(root) / "engine-plugin-release").read_text()).resolve(strict=True)
        validate_handoff(root, candidate, supervisor)
        candidate_version = _plugin_version(candidate)
        if candidate.parent == current.parent and candidate_version is not None and candidate_version > current_version:
            return candidate
    except (OSError, RuntimeError, ValueError, supervisor.ServiceError):
        pass
    return None
