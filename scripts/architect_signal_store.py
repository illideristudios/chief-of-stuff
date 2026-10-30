"""Compliance text and the coordinator's architect-signal ledger on disk (#1180).

Interface Adapters, gateways. Reads `architecture/compliance.md` and reads/writes
`.chief-of-stuff/architect-signals.json`. Never edits the compliance rows.
"""

from __future__ import annotations

import json
import fcntl
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any

LEDGER_NAME = "architect-signals.json"


def ledger_path(root: Path) -> Path:
    return root / ".chief-of-stuff" / LEDGER_NAME


def compliance_text(root: Path) -> str:
    path = root / "architecture" / "compliance.md"
    if not path.is_file():
        raise FileNotFoundError(f"no compliance record at {path}")
    return path.read_text()


def load_ledger(root: Path) -> dict[str, Any]:
    path = ledger_path(root)
    if not path.exists():
        return {"acted": {}}
    data = json.loads(path.read_text())
    if (not isinstance(data, dict) or not isinstance(data.get("acted"), dict)
            or not isinstance(data.get("filing", {}), dict)):
        raise ValueError("invalid architect signal ledger; refusing to treat it as empty")
    return data


@contextmanager
def exclusive(root: Path):
    """All filing workers use the coordinator root, including workers in separate trees."""
    path = ledger_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    with (path.parent / "architect-signals.lock").open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield


def save_ledger(root: Path, data: dict) -> None:
    """Publish a complete ledger atomically; caller holds exclusive()."""
    path = ledger_path(root)
    fd, name = tempfile.mkstemp(prefix="architect-signals-", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(data, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def record_acted(root: Path, gap_id: str, *, issue_key: str, signal_key: str) -> None:
    """Record that this gap row has been acted on. Never touches compliance.md. First write wins."""
    with exclusive(root):
        data = load_ledger(root)
        if gap_id in data["acted"]:
            return
        data["acted"][gap_id] = {"issue_key": issue_key, "signal_key": signal_key}
        save_ledger(root, data)
