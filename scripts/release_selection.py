"""Select the installed release or a newer plugin-cache sibling requested for a workspace."""

import json
from pathlib import Path


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


def explicit_intent(root: Path, supervisor) -> dict | None:
    # A cached supervisor may have an unsafe workspace marker parser. Read the
    # private record directly, regardless of which supervisor was imported.
    data_dir = getattr(supervisor, "data_dir", None)
    if data_dir is None:
        return None
    import engine_intent
    return engine_intent.read(root, data_dir(), supervisor.ServiceError)


def selected_release(root: Path, supervisor) -> Path:
    """Use the supervisor's selector when available; support older cached supervisor modules too."""
    select = getattr(supervisor, "selected_release", None)
    if select is not None and getattr(supervisor, "TRUSTED_INTENT_VERSION", 0) >= 2:
        return select(root)
    import engine_intent
    def validate(release):
        engine_intent.validate_release(root, release, supervisor.ServiceError)
    current = supervisor.current_release()
    intent = explicit_intent(root, supervisor)
    if intent is not None and intent["operation"] == "install":
        chosen = Path(intent["release"])
        if not intent["pending"] and chosen.parent.name == "versions" and current.parent == chosen.parent:
            validate(current)
            return current
        validate(chosen)
        return chosen
    validate(current)
    current_version = _plugin_version(current)
    if current != supervisor.RELEASE or current_version is None:
        return current
    try:
        candidate = Path((supervisor.state_dir(root) / "engine-plugin-release").read_text()).resolve(strict=True)
        candidate_version = _plugin_version(candidate)
        if candidate.parent == current.parent and candidate_version is not None and candidate_version > current_version:
            validate(candidate)
            return candidate
    except (OSError, RuntimeError, ValueError, supervisor.ServiceError):
        pass
    return current
