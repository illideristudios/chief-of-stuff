"""The issue-keyed store: one row per forge issue, written by upsert only.

The forge is the source of truth. The forge-owned columns of a `WorkItem` (title, stage, forge/review/merge state, and the
human-review flag when kanban is configured) and declared file ownership are rebuildable; segments, decisions, tree ownership, log,
and resume are the engine's own and are never touched by a sync. Omitted snapshot fields keep their stored values.
The schema lives in `migrations/`, not in `create_all`: `migrate` is the one place
it changes, and a test holds these models to what the migrations build.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing, contextmanager
from copy import deepcopy
from pathlib import Path
from typing import Any, Iterator

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import JSON, Boolean, ForeignKey, Integer, String, UniqueConstraint, create_engine, delete, event, select, update
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

import clock

FORGE_COLUMNS = ("title", "stage", "forge_state", "review_state", "merge_state", "human_review")


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
    required_human_review: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    runtime: Mapped[str] = mapped_column(String, default="", server_default="")
    model: Mapped[str] = mapped_column(String, default="", server_default="")
    effort: Mapped[str] = mapped_column(String, default="", server_default="")
    agent_type: Mapped[str] = mapped_column(String, default="", server_default="")


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
    mode: Mapped[str] = mapped_column(String, default="write")  # "write", "read", or engine-owned "tree"


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


class WorkerRecord:
    """Launch identity on every worker record; records never update another run's row."""
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    issue_key: Mapped[str] = mapped_column(ForeignKey("work_item.issue_key", ondelete="CASCADE"))
    stage: Mapped[str] = mapped_column(String)
    attempt: Mapped[int] = mapped_column(Integer)
    session_id: Mapped[str] = mapped_column(String)
    created_at: Mapped[str] = mapped_column(String)


class Report(WorkerRecord, Base):
    __tablename__ = "report"
    outcome: Mapped[str] = mapped_column(String)
    summary: Mapped[str] = mapped_column(String)
    findings: Mapped[list[dict[str, Any]]] = mapped_column(JSON)
    suite: Mapped[Any] = mapped_column(JSON, nullable=True)


class Ask(WorkerRecord, Base):
    __tablename__ = "ask"
    question: Mapped[str] = mapped_column(String)
    options: Mapped[list[str]] = mapped_column(JSON)
    context: Mapped[str] = mapped_column(String)
    answer: Mapped[str | None] = mapped_column(String, nullable=True)


def _run_result(data: dict, stage: str, attempt: int, outcome: str, ended_at: str) -> None:
    """The shared collected-result layout, used by completion and suite acceptance."""
    runs = data.get("runs")
    record = runs.get(f"{stage}-{attempt}") if isinstance(runs, dict) else None
    if isinstance(record, dict) and stage == "review":
        record["result"] = {"reviewed_head": record.get("reviewed_head"),
                            "outcome": outcome, "ended_at": ended_at}


def migration_config(path: Path | str, metadata, versions: Path | None = None) -> Config:
    """Shared Alembic environment; separate databases supply their own metadata and revisions."""
    cfg = Config()
    cfg.set_main_option("script_location", str(Path(__file__).with_name("migrations")))
    cfg.set_main_option("path_separator", "os")
    # ConfigParser interpolates percent signs; escape literal URL characters.
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{path}".replace("%", "%%"))
    if versions is not None:
        cfg.set_main_option("version_locations", str(versions))
    cfg.attributes["metadata"] = metadata
    return cfg


def _config(path: Path | str) -> Config:
    return migration_config(path, Base.metadata)


def is_current(path: Path | str) -> bool:
    """True when the database at `path` is already at the latest revision. Only reads, so it is safe beside a live writer."""
    if not Path(path).exists():
        return False
    head = ScriptDirectory.from_config(_config(path)).get_current_head()
    with closing(sqlite3.connect(path, timeout=30)) as conn:
        try:
            return conn.execute("SELECT version_num FROM alembic_version").fetchall() == [(head,)]
        except sqlite3.OperationalError:  # no alembic_version table: never migrated
            return False


def migrate(path: Path | str) -> None:
    """Enable WAL and bring the database at `path` to the latest revision. Call it once at engine start, never from
    concurrent writers: journal-mode changes need an exclusive lock, and first-time migrations race to create tables."""
    command.upgrade(_config(path), "head")


class Store:
    def __init__(self, path: Path | str, clock: clock.Clock = clock.system) -> None:
        self.clock = clock
        self.engine = create_engine(f"sqlite:///{path}", connect_args={"timeout": 30, "isolation_level": None})

        @event.listens_for(self.engine, "connect")
        def _pragmas(conn, _record) -> None:  # noqa: ANN001 - DBAPI connection
            conn.execute("PRAGMA foreign_keys=ON")

        @event.listens_for(self.engine, "begin")
        def _begin(conn) -> None:  # noqa: ANN001 - SQLAlchemy connection
            # Take the write lock before reading: a WAL snapshot cannot upgrade after another writer commits.
            conn.exec_driver_sql("BEGIN IMMEDIATE")

    def _stamp(self) -> str:
        return self.clock().isoformat()

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
        self._upsert(WorkItem, ("issue_key",), {"issue_key": issue_key, **fields, "updated_at": self._stamp()})

    def set_human_review(self, issue_key: str, flag: bool) -> None:
        """Flag an existing item; a kanban snapshot can subsequently refresh it from forge labels."""
        with self.session() as s:
            if s.execute(update(WorkItem).where(WorkItem.issue_key == issue_key)
                         .values(human_review=flag, updated_at=self._stamp())).rowcount == 0:
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
                       ended_at: str | None = None, outcome: str | None = None,
                       runtime: str | None = None, model: str | None = None,
                       effort: str | None = None, agent_type: str | None = None) -> None:
        # Omitted fields stay as stored, so a retried start never clears completion or who ran it.
        done = {k: v for k, v in (("ended_at", ended_at), ("outcome", outcome), ("runtime", runtime),
                                  ("model", model), ("effort", effort), ("agent_type", agent_type))
                if v is not None}
        if outcome == "human_review":
            done["required_human_review"] = True
        self._upsert(Segment, ("issue_key", "stage", "attempt"),
                     {"issue_key": issue_key, "stage": stage, "attempt": attempt, "started_at": started_at, **done})

    def end_segment(self, issue_key: str, stage: str, attempt: int, ended_at: str, outcome: str) -> None:
        """Atomically complete an open segment and its collected result, preserving start and actor."""
        with self.session() as s:
            changed = s.execute(update(Segment).where(Segment.issue_key == issue_key, Segment.stage == stage,
                                                     Segment.attempt == attempt, Segment.ended_at.is_(None))
                                .values(ended_at=ended_at, outcome=outcome,
                                        **({"required_human_review": True} if outcome == "human_review" else {})))
            resume = s.get(Resume, issue_key)
            if changed.rowcount and resume is not None and isinstance(resume.data, dict):
                data = deepcopy(resume.data)
                _run_result(data, stage, attempt, outcome, ended_at)
                resume.data = data

    def drop_segment(self, issue_key: str, stage: str, attempt: int) -> None:
        """Remove a segment that never started (a reservation whose launch failed)."""
        with self.session() as s:
            s.execute(delete(Segment).where(Segment.issue_key == issue_key, Segment.stage == stage,
                                            Segment.attempt == attempt))

    def segment_runs(self) -> list[tuple[str, str, int, str | None, Any]]:
        """Each segment's issue, stage, attempt, end time, and resume run entry (or None)."""
        with self.session() as s:
            rows = s.execute(select(Segment.issue_key, Segment.stage, Segment.attempt, Segment.ended_at, Resume.data)
                             .outerjoin(Resume, Resume.issue_key == Segment.issue_key)
                             .order_by(Segment.id)).all()
        out = []
        for key, stage, attempt, ended, data in rows:
            entry = None
            if isinstance(data, dict):
                runs = data.get("runs")
                if isinstance(runs, dict):
                    entry = runs.get(f"{stage}-{attempt}")
            out.append((key, stage, attempt, ended, entry))
        return out

    def segments(self, issue_key: str | None = None) -> list[Segment]:
        stmt = select(Segment).order_by(Segment.id)
        if issue_key is not None:
            stmt = stmt.where(Segment.issue_key == issue_key)
        return self._all(stmt)

    # ownership
    def set_ownership(self, issue_key: str, path: str, mode: str = "write") -> None:
        self._upsert(Ownership, ("issue_key", "path"), {"issue_key": issue_key, "path": path, "mode": mode})

    def ownership(self, issue_key: str) -> list[Ownership]:
        return self._all(select(Ownership).where(Ownership.issue_key == issue_key).order_by(Ownership.path))

    def replace_file_ownership(self, issue_key: str, scopes: list[tuple[str, str]]) -> None:
        """Replace the forge's declaration atomically, retaining engine worktree registrations.

        Older workers registered absolute tree paths as write rows; adopt those as tree rows too.
        Declared file paths are always repository-relative.
        """
        with self.session() as s:
            for row in s.scalars(select(Ownership).where(Ownership.issue_key == issue_key)).all():
                if row.mode == "tree" or Path(row.path).is_absolute():
                    row.mode = "tree"
                else:
                    s.delete(row)
            s.flush()
            s.add_all(Ownership(issue_key=issue_key, path=path, mode=mode) for path, mode in scopes)

    # decisions and log (append-only)
    def add_decision(self, issue_key: str, topic: str, question: str, answer: str | None = None,
                     decided_by: str = "", record: Any = None) -> None:
        with self.session() as s:
            s.add(Decision(issue_key=issue_key, topic=topic, question=question, answer=answer,
                           decided_by=decided_by, record=record, created_at=self._stamp()))

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

    def finish_suite_hold(self, issue_key: str, *, accept: bool) -> None:
        """Atomically retire the hold, optionally accepting its segment and collected run result.

        The Use Case decides when this transition is allowed. Keep the historical review marker
        and actor/timing fields when accepting; callers never edit the worker result layout.
        """
        with self.session() as s:
            resume = s.get(Resume, issue_key)
            data = deepcopy(resume.data)
            hold = data.pop("suite_hold")
            if accept:
                segment = s.scalar(select(Segment).where(Segment.issue_key == issue_key,
                                   Segment.stage == hold["stage"], Segment.attempt == hold["attempt"]))
                if segment is None:
                    raise ValueError("suite hold segment not found")
                _run_result(data, segment.stage, segment.attempt, "done", segment.ended_at)
                segment.outcome = "done"
                segment.required_human_review = True
            # Assign a fresh object so SQLAlchemy persists the nested JSON transition.
            resume.data = data

    # Worker reports are append-only, including late reports from older attempts.
    def add_report(self, issue_key: str, stage: str, attempt: int, session_id: str,
                   outcome: str, summary: str, findings: list[dict[str, Any]], suite: Any = None) -> int:
        with self.session() as s:
            row = Report(issue_key=issue_key, stage=stage, attempt=attempt, session_id=session_id,
                         outcome=outcome, summary=summary, findings=findings, suite=suite,
                         created_at=self._stamp())
            s.add(row)
            s.flush()
            return row.id

    def reports(self, issue_key: str, *, stage: str | None = None, attempt: int | None = None,
                session_id: str | None = None) -> list[Report]:
        stmt = select(Report).where(Report.issue_key == issue_key)
        for key, value in (("stage", stage), ("attempt", attempt), ("session_id", session_id)):
            if value is not None:
                stmt = stmt.where(getattr(Report, key) == value)
        return self._all(stmt.order_by(Report.attempt, Report.id))

    def add_ask(self, issue_key: str, stage: str, attempt: int, session_id: str,
                question: str, options: list[str], context: str) -> int:
        with self.session() as s:
            row = Ask(issue_key=issue_key, stage=stage, attempt=attempt, session_id=session_id,
                      question=question, options=options, context=context, created_at=self._stamp())
            s.add(row)
            s.flush()
            return row.id

    def asks(self, issue_key: str) -> list[Ask]:
        return self._all(select(Ask).where(Ask.issue_key == issue_key).order_by(Ask.id))

    def answer_ask(self, issue_key: str, ask_id: int, answer: str) -> None:
        """An operator's answer is bound to the named issue; worker tools never expose this write."""
        if not answer.strip():
            raise ValueError("an ask answer must not be empty")
        with self.session() as s:
            if not s.execute(update(Ask).where(Ask.issue_key == issue_key, Ask.id == ask_id)
                             .values(answer=answer)).rowcount:
                raise KeyError(f"ask {ask_id} does not belong to {issue_key}")

    def _all(self, stmt: Any) -> list[Any]:
        with self.session() as s:
            rows = s.scalars(stmt).all()
            s.expunge_all()
            return list(rows)
