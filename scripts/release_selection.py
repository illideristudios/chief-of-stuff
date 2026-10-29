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
    reader = getattr(supervisor, "_explicit_intent", None)
    if reader is not None:
        return reader(root)
    try:
        intent = json.loads((supervisor.state_dir(root) / "engine-explicit-intent.json").read_text())
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        raise supervisor.ServiceError(f"cannot read explicit intent: {exc}") from exc
    if not isinstance(intent, dict) or intent.get("operation") not in ("install", "uninstall"):
        raise supervisor.ServiceError("invalid explicit intent")
    if intent["operation"] == "install":
        job, release = intent.get("job"), intent.get("release")
        if (not isinstance(job, dict) or job.get("Label") != supervisor.label("engine", root)
                or job.get("WorkingDirectory") != str(root.resolve()) or not isinstance(release, str)
                or not Path(release).is_absolute() or not isinstance(intent.get("pending"), bool)):
            raise supervisor.ServiceError("invalid explicit intent")
    return intent


def selected_release(root: Path, supervisor) -> Path:
    """Use the supervisor's selector when available; support older cached supervisor modules too."""
    select = getattr(supervisor, "selected_release", None)
    if select is not None:
        return select(root)
    current = supervisor.current_release()
    intent = explicit_intent(root, supervisor)
    if intent is not None and intent["operation"] == "install":
        chosen = Path(intent["release"])
        if not intent["pending"] and chosen.parent.name == "versions" and current.parent == chosen.parent:
            return current
        return chosen
    current_version = _plugin_version(current)
    if current != supervisor.RELEASE or current_version is None:
        return current
    try:
        candidate = Path((supervisor.state_dir(root) / "engine-plugin-release").read_text()).resolve(strict=True)
        candidate_version = _plugin_version(candidate)
        if candidate.parent == current.parent and candidate_version is not None and candidate_version > current_version:
            return candidate
    except (OSError, RuntimeError, ValueError):
        pass
    return current
