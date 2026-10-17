"""Account-wide limit history, separate from every workspace's issue store (§10)."""
from __future__ import annotations

import fcntl
import os
from datetime import datetime, timezone
from pathlib import Path

from alembic import command
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import Float, Integer, String, UniqueConstraint, create_engine, event, select
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

from limits import Reading
from store import migration_config


class Base(DeclarativeBase):
    pass


class LimitReading(Base):
    __tablename__ = "limit_reading"
    __table_args__ = (UniqueConstraint("runtime", "account", "limit", "read_at"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    runtime: Mapped[str] = mapped_column(String, index=True)
    account: Mapped[str] = mapped_column(String)
    limit: Mapped[str] = mapped_column(String)
    used: Mapped[float | None] = mapped_column(Float)
    cap: Mapped[float | None] = mapped_column(Float)
    unit: Mapped[str] = mapped_column(String)
    resets_at: Mapped[str | None] = mapped_column(String)
    read_at: Mapped[str] = mapped_column(String)
    unknown: Mapped[str] = mapped_column(String)


def history_path() -> Path:
    return Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state") / "chief-of-stuff/limits.sqlite"


def _stamp(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("limit timestamps must have a timezone")
    return value.astimezone(timezone.utc).isoformat()


class LimitsHistory:
    def __init__(self, path: Path | str | None = None):
        self.path = Path(path) if path is not None else history_path()
        self.engine = create_engine(f"sqlite:///{self.path}", connect_args={"timeout": 30, "isolation_level": None})

        @event.listens_for(self.engine, "begin")
        def _begin(conn) -> None:  # noqa: ANN001 - SQLAlchemy connection
            # Serialize the check and insert across workspace engines sharing this account history, as store.Store does.
            conn.exec_driver_sql("BEGIN IMMEDIATE")

    def _migrate(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        cfg = migration_config(self.path, Base.metadata,
                               Path(__file__).with_name("migrations") / "limits_versions")
        # Different workspaces can take their first reading at the same time.
        with self.path.with_suffix(".migrate.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            with self.engine.connect() as conn:
                current = MigrationContext.configure(conn).get_current_revision()
            if current != ScriptDirectory.from_config(cfg).get_current_head():
                command.upgrade(cfg, "head")

    def record(self, readings: list[Reading]) -> None:
        """Insert readings; an unknown whose reason matches the latest stored row for its series is skipped."""
        if not readings:
            return
        self._migrate()
        with Session(self.engine) as session, session.begin():
            for r in readings:
                if r.unknown:
                    latest = session.scalar(select(LimitReading).where(
                        LimitReading.runtime == r.runtime, LimitReading.account == r.account,
                        LimitReading.limit == r.limit).order_by(
                            LimitReading.read_at.desc(), LimitReading.id.desc()).limit(1))
                    if latest is not None and latest.unknown == r.unknown:
                        continue
                values = {key: getattr(r, key) for key in ("runtime", "account", "limit", "used", "cap", "unit", "unknown")}
                values.update(read_at=_stamp(r.read_at), resets_at=_stamp(r.resets_at) if r.resets_at else None)
                session.execute(insert(LimitReading).values(**values).on_conflict_do_nothing())

    def readings(self, runtime: str, account: str, since: datetime) -> list[Reading]:
        """Read since an inclusive aware timestamp (naive raises), migrating under the shared lock first."""
        since_stamp = _stamp(since)
        self._migrate()
        with Session(self.engine) as session:
            rows = session.scalars(select(LimitReading).where(
                LimitReading.runtime == runtime, LimitReading.account == account,
                LimitReading.read_at >= since_stamp).order_by(LimitReading.read_at, LimitReading.id)).all()
            return [Reading(r.runtime, r.account, r.limit, r.used, r.cap, r.unit,
                            datetime.fromisoformat(r.resets_at) if r.resets_at else None,
                            datetime.fromisoformat(r.read_at), r.unknown) for r in rows]
