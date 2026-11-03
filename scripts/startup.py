"""Workspace startup for the engine and `work` commands: settings and forge."""
from __future__ import annotations

from pathlib import Path

import forge
import settings
from workspace import settings_path


def workspace_forge(root: Path) -> tuple[str, settings.Settings, forge.Forge | None]:
    """CLAUDE.md text, settings, and the forge for this workspace (or None when there is no Backlog)."""
    claude = root / "CLAUDE.md"
    text = claude.read_text() if claude.is_file() else ""
    config = settings.load(root, settings_path(text))
    source = forge.for_workspace(root, approver=config.workflow.approver or "", kanban=config.kanban)
    return text, config, source
