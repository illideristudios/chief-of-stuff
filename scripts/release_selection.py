"""Select the installed release or a newer plugin-cache sibling requested for a workspace."""

import json
import os
from pathlib import Path

import engine_intent


def python_environment() -> dict[str, str]:
    return {key: value for key, value in os.environ.items() if not key.upper().startswith("PYTHON")}


# -I prevents Python from importing workspace startup hooks. Add the validated
# release's script directory only after interpreter startup, for local imports.
_RELEASE_BOOTSTRAP = ("import runpy, sys; entry = sys.argv[1]; "
                      "sys.path.insert(0, __import__('os').path.dirname(entry)); "
                      "sys.argv = sys.argv[1:]; runpy.run_path(entry, run_name='__main__')")


def isolated_release_argv(python: Path, entry: Path, *args: str) -> list[str]:
    return [str(python), "-I", "-c", _RELEASE_BOOTSTRAP, str(entry), *args]


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
    return engine_intent.read(root, data_dir(), supervisor.ServiceError)


def validate_handoff(root: Path, release: Path, supervisor) -> None:
    engine_intent.validate_release(root, release, supervisor.ServiceError)
    root = root.resolve()
    for path in (release / "scripts/pages.py", release / ".claude-plugin/plugin.json", release / "chief_of_stuff.py"):
        try:
            target = path.resolve()
            if target == root or root in target.parents:
                raise supervisor.ServiceError("board release must not point into the workspace")
        except (OSError, RuntimeError) as exc:
            raise supervisor.ServiceError(f"invalid board release: {exc}") from exc


def explicit_release(root: Path, supervisor, *, completed_only: bool = False) -> Path | None:
    intent = explicit_intent(root, supervisor)
    if intent is None or intent["operation"] != "install" or (completed_only and intent["pending"]):
        return None
    chosen = Path(intent["release"])
    if not intent["pending"] and chosen.parent.name == "versions":
        current = supervisor.current_release()
        if current.parent == chosen.parent:
            chosen = current
    validate_handoff(root, chosen, supervisor)
    return chosen


def marker_release(root: Path, current: Path, supervisor) -> Path | None:
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


def selected_release(root: Path, supervisor) -> Path:
    """Select independently of potentially older, unsafe supervisor selectors."""
    explicit = explicit_release(root, supervisor)
    if explicit is not None:
        return explicit
    current = supervisor.current_release()
    validate_handoff(root, current, supervisor)
    if current != supervisor.RELEASE:
        return current
    return marker_release(root, current, supervisor) or current
