"""Compliance text and the coordinator's architect-signal ledger on disk (#1180).

Interface Adapters, gateways. Reads `architecture/compliance.md` and reads/writes
`.chief-of-stuff/architect-signals.json`. Never edits the compliance rows.
"""

from __future__ import annotations

import json
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
    if not path.is_file():
        return {"acted": {}}
    data = json.loads(path.read_text())
    acted = data.get("acted") if isinstance(data, dict) else None
    return {"acted": dict(acted) if isinstance(acted, dict) else {}}


def record_acted(root: Path, gap_id: str, *, issue_key: str, signal_key: str) -> None:
    """Record that this gap row has been acted on. Never touches compliance.md. First write wins."""
    data = load_ledger(root)
    if gap_id in data["acted"]:
        return
    data["acted"][gap_id] = {"issue_key": issue_key, "signal_key": signal_key}
    path = ledger_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
