"""Interface Adapters for the coordinator's `PrecedentSource` seam (§17, #1181).

Backs `decision_history.PrecedentSource` with either the daily markdown tracker's
`## Decisions` rows or the engine store's current decision graph leaves, so the
decision graph replaces tracker rows without changing the coordinator.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from decision_graph import PrecedentEntry
import decision_history
import store
from workspace import ConfigError, read_config


class TrackerPrecedentSource:
    """Reads precedent from the daily markdown tracker's `## Decisions` table."""

    def __init__(self, root: Path, *, date: str | None = None) -> None:
        self.root = Path(root)
        self.date = date

    def current_precedent(self) -> tuple[PrecedentEntry, ...]:
        try:
            cfg = read_config(self.root)
            day = self.date or datetime.now(cfg.zone).date().isoformat()
            text = (self.root / cfg.tracker_path(day)).read_text(encoding="utf-8")
        except (OSError, ConfigError):
            return ()
        return decision_history.tracker_precedent_entries(text)


class GraphPrecedentSource:
    """Reads precedent from current unreplaced leaves in the decision graph."""

    def __init__(self, backing_store: store.Store | Path) -> None:
        self._store = backing_store

    def current_precedent(self) -> tuple[PrecedentEntry, ...]:
        if isinstance(self._store, store.Store):
            return decision_history.graph_precedent_entries(self._store.decision_leaves())
        st = store.Store(self._store)
        try:
            return decision_history.graph_precedent_entries(st.decision_leaves())
        finally:
            st.engine.dispose()


def default_store_path(root: Path) -> Path:
    return Path(root) / "store.db"


def workspace_precedent_source(
    root: Path,
    *,
    store_path: Path | None = None,
    date: str | None = None,
) -> decision_history.PrecedentSource:
    """Return the decision graph source when current leaves exist in `store.db`, else tracker rows."""
    db_path = Path(store_path) if store_path is not None else default_store_path(root)
    if store.is_current(db_path):
        st = store.Store(db_path)
        try:
            has_leaves = bool(st.decision_leaves())
        finally:
            st.engine.dispose()
        if has_leaves:
            return GraphPrecedentSource(db_path)
    return TrackerPrecedentSource(root, date=date)
