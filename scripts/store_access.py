"""Open the engine store for the commands that write it. Kept apart from supervisor so base-Python callers never import SQLAlchemy."""
from __future__ import annotations

from pathlib import Path

import claude_adapter
import store
import supervisor


def migrated_store(root: Path, engine_owned: bool = False) -> store.Store:
    """Open store.db at the latest revision (caller disposes). A current store is only opened: migration needs no concurrent
    writer, and a live engine writes outside any lock. A stale store is migrated under migrate.lock, and by anyone but the
    engine (`engine_owned`, which already holds engine.lock) only while no engine holds engine.lock."""
    path = root / "store.db"
    state = supervisor.state_dir(root)
    if engine_owned:
        runs = state / "runs"
        if runs.is_symlink():
            raise OSError(f"unsafe worker runs directory: {runs}")
        if runs.exists():
            for run in runs.iterdir():
                if run.is_symlink():
                    raise OSError(f"unsafe worker run: {run}")
                if run.is_dir():
                    claude_adapter.messaging(run)
    with supervisor.lock(state / "migrate.lock", wait=True):
        if not store.is_current(path):
            if engine_owned:
                store.migrate(path)
            else:
                try:
                    with supervisor.lock(state / "engine.lock"):
                        store.migrate(path)
                except supervisor.ServiceError:
                    raise supervisor.ServiceError(
                        "store.db needs migrating but the engine is running; restart the engine to migrate it") from None
    return store.Store(path)
