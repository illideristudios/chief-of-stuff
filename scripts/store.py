"""The issue-keyed store: one row per forge issue, written by upsert only.

The forge is the source of truth. The forge-owned columns of a `WorkItem` (title, stage, forge/review/merge state) are a cache
`sync_from_forge` can rebuild; everything else here (segments, decisions, ownership, log, resume, the human-review flag) is the
engine's own and is never touched by a sync. The schema lives in `migrations/`, not in `create_all`: `migrate` is the one place
it changes, and a test holds these models to what the migrations build.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from alembic import command
from alembic.config import Config
from sqlalchemy import JSON, Boolean, ForeignKey, Integer, String, UniqueConstraint, create_engine, event, select, update
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

FORGE_COLUMNS = ("title", "stage", "forge_state", "review_state", "merge_state")


class Base(DeclarativeBase):
    pass


class WorkItem(Base):
    __tablename__ = "work_item"
    issue_key: Mapped[str] = mapped_column(String, primary_key=True)  # `github:owner/repo#12`; the one identity
    title: Mapped[str] = mapped_column(String, default="")
    stage: Mapped[str] = mapped_column(String, default="")
    forge_state: Mapped[str] = mapped_column(String, default="open")
    review_state: Mapped[str] = mapped_column(String, default="")
    merge_state: Mapped[str] = mapped_column(String, default="")
    human_review: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_at: Mapped[str] = mapped_column(String, default="")


class Segment(Base):
    """A stretch of work on the issue's one gantt line. Rework is another attempt, never another item."""
    __tablename__ = "segment"
    __table_args__ = (UniqueConstraint("issue_key", "stage", "attempt"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    issue_key: Mapped[str] = mapped_column(ForeignKey("work_item.issue_key", ondelete="CASCADE"))
    stage: Mapped[str] = mapped_column(String)
    attempt: Mapped[int] = mapped_column(Integer)
    started_at: Mapped[str] = mapped_column(String)
    ended_at: Mapped[str | None] = mapped_column(String, nullable=True)
    outcome: Mapped[str | None] = mapped_column(String, nullable=True)


class Decision(Base):
    __tablename__ = "decision"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    issue_key: Mapped[str] = mapped_column(ForeignKey("work_item.issue_key", ondelete="CASCADE"))
    topic: Mapped[str] = mapped_column(String)
    question: Mapped[str] = mapped_column(String)
    answer: Mapped[str | None] = mapped_column(String, nullable=True)
    decided_by: Mapped[str] = mapped_column(String, default="")  # "trio", "user" or ""
    record: Mapped[Any] = mapped_column(JSON, nullable=True)  # the trio's votes and reasoning
    created_at: Mapped[str] = mapped_column(String, default="")


class Ownership(Base):
    __tablename__ = "ownership"
    __table_args__ = (UniqueConstraint("issue_key", "path"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    issue_key: Mapped[str] = mapped_column(ForeignKey("work_item.issue_key", ondelete="CASCADE"))
    path: Mapped[str] = mapped_column(String)
    mode: Mapped[str] = mapped_column(String, default="write")  # "write" or "read"


class LogEntry(Base):
    __tablename__ = "log_entry"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    issue_key: Mapped[str] = mapped_column(ForeignKey("work_item.issue_key", ondelete="CASCADE"))
    at: Mapped[str] = mapped_column(String)
    message: Mapped[str] = mapped_column(String)


class Resume(Base):
    __tablename__ = "resume"
    issue_key: Mapped[str] = mapped_column(ForeignKey("work_item.issue_key", ondelete="CASCADE"), primary_key=True)
    data: Mapped[Any] = mapped_column(JSON)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()  # shortcut: wall clock, the engine's injected clock replaces it in M3


def migrate(path: Path | str) -> None:
    """Bring the database at `path` to the latest revision. Call it once at engine start, never from concurrent writers:
    two first-time migrations race to create the same tables."""
    cfg = Config()
    cfg.set_main_option("script_location", str(Path(__file__).with_name("migrations")))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{path}")
    cfg.attributes["metadata"] = Base.metadata
    command.upgrade(cfg, "head")


class Store:
    def __init__(self, path: Path | str) -> None:
        self.engine = create_engine(f"sqlite:///{path}", connect_args={"timeout": 30})

        @event.listens_for(self.engine, "connect")
        def _pragmas(conn, _record) -> None:  # noqa: ANN001 - DBAPI connection
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA journal_mode=WAL")

    @contextmanager
    def session(self) -> Iterator[Session]:
        """One transaction: commits on a clean exit, rolls back on an error."""
        with Session(self.engine) as s, s.begin():
            yield s

    def _upsert(self, model: type[Base], keys: tuple[str, ...], values: dict[str, Any]) -> None:
        stmt = insert(model).values(**values)
        rest = {k: v for k, v in values.items() if k not in keys}
        stmt = stmt.on_conflict_do_update(index_elements=keys, set_=rest) if rest else stmt.on_conflict_do_nothing()
        with self.session() as s:
            s.execute(stmt)

    # work items
    def upsert_item(self, issue_key: str, **fields: Any) -> None:
        self._upsert(WorkItem, ("issue_key",), {"issue_key": issue_key, **fields, "updated_at": _now()})

    def set_human_review(self, issue_key: str, flag: bool) -> None:
        """Flag an existing item. The flag is engine data, so it never creates a row the forge does not know."""
        with self.session() as s:
            if s.execute(update(WorkItem).where(WorkItem.issue_key == issue_key)
                         .values(human_review=flag, updated_at=_now())).rowcount == 0:
                raise KeyError(issue_key)

    def items(self) -> list[WorkItem]:
        with self.session() as s:
            rows = s.scalars(select(WorkItem).order_by(WorkItem.issue_key)).all()
            s.expunge_all()
            return list(rows)

    def sync_from_forge(self, snapshots: list[dict[str, Any]]) -> None:
        """Upsert the forge-owned columns of each snapshot. Engine-owned data is never written, so a sync cannot lose it."""
        for snap in snapshots:
            self.upsert_item(snap["issue_key"], **{k: snap[k] for k in FORGE_COLUMNS if k in snap})

    # segments
    def record_segment(self, issue_key: str, stage: str, attempt: int, started_at: str,
                       ended_at: str | None = None, outcome: str | None = None) -> None:
        # Omitted completion fields stay as stored, so a retried start never reopens a finished segment.
        done = {k: v for k, v in (("ended_at", ended_at), ("outcome", outcome)) if v is not None}
        self._upsert(Segment, ("issue_key", "stage", "attempt"),
                     {"issue_key": issue_key, "stage": stage, "attempt": attempt, "started_at": started_at, **done})

    def segments(self, issue_key: str) -> list[Segment]:
        return self._all(select(Segment).where(Segment.issue_key == issue_key).order_by(Segment.id))

    # ownership
    def set_ownership(self, issue_key: str, path: str, mode: str = "write") -> None:
        self._upsert(Ownership, ("issue_key", "path"), {"issue_key": issue_key, "path": path, "mode": mode})

    def ownership(self, issue_key: str) -> list[Ownership]:
        return self._all(select(Ownership).where(Ownership.issue_key == issue_key).order_by(Ownership.path))

    # decisions and log (append-only)
    def add_decision(self, issue_key: str, topic: str, question: str, answer: str | None = None,
                     decided_by: str = "", record: Any = None) -> None:
        with self.session() as s:
            s.add(Decision(issue_key=issue_key, topic=topic, question=question, answer=answer,
                           decided_by=decided_by, record=record, created_at=_now()))

    def decisions(self, issue_key: str) -> list[Decision]:
        return self._all(select(Decision).where(Decision.issue_key == issue_key).order_by(Decision.id))

    def add_log(self, issue_key: str, message: str, at: str) -> None:
        with self.session() as s:
            s.add(LogEntry(issue_key=issue_key, at=at, message=message))

    def log(self, issue_key: str) -> list[LogEntry]:
        return self._all(select(LogEntry).where(LogEntry.issue_key == issue_key).order_by(LogEntry.id))

    # resume
    def set_resume(self, issue_key: str, data: Any) -> None:
        self._upsert(Resume, ("issue_key",), {"issue_key": issue_key, "data": data})

    def resume(self, issue_key: str) -> Any:
        with self.session() as s:
            row = s.get(Resume, issue_key)
            return row.data if row else None

    def _all(self, stmt: Any) -> list[Any]:
        with self.session() as s:
            rows = s.scalars(stmt).all()
            s.expunge_all()
            return list(rows)
