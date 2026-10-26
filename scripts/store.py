"""The issue-keyed store: one row per forge issue, written by upsert only.

The forge is the source of truth. The forge-owned columns of a `WorkItem` (title, stage, forge/review/merge state, and the
human-review flag when kanban is configured) are rebuildable; segments, decisions, worker trees, log,
and resume are the engine's own and are never touched by a sync. Omitted snapshot fields keep their stored values.
The schema lives in `migrations/`, not in `create_all`: `migrate` is the one place
it changes, and a test holds these models to what the migrations build.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing, contextmanager
from copy import deepcopy
from pathlib import Path
from typing import Any, Callable, Iterator

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

    @property
    def workflow(self) -> str:
        return ""


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

    @property
    def runner(self) -> str:
        return getattr(self, "_runner", "")


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

    @property
    def gate(self) -> str:
        return self.topic

    @property
    def prompt(self) -> str:
        return self.question

    @property
    def asked_at(self) -> str:
        return self.created_at

    @property
    def status(self) -> str:
        if isinstance(self.record, dict) and self.record.get("status"):
            return str(self.record["status"])
        return "answered" if self.answer else "open"

    @property
    def answered_at(self) -> str | None:
        if isinstance(self.record, dict) and "answered_at" in self.record:
            return self.record["answered_at"]
        return self.created_at if self.answer else None

    @property
    def blocked_reason(self) -> str:
        if isinstance(self.record, dict) and self.record.get("blocked_reason"):
            return str(self.record.get("blocked_reason", "") or "")
        return ""

    @property
    def rerouted_to(self) -> str:
        if isinstance(self.record, dict) and self.record.get("rerouted_to"):
            return str(self.record.get("rerouted_to", "") or "")
        return ""

    @property
    def intent(self) -> str:
        return str(self.record.get("intent", "")) if isinstance(self.record, dict) else ""

    @property
    def superseded_by(self) -> int:
        return int(self.record.get("superseded_by", 0)) if isinstance(self.record, dict) else 0


class Worktree(Base):
    """A tree a worker was launched in: cleanup removes it only once its item is finished (#1039)."""
    __tablename__ = "worktree"
    __table_args__ = (UniqueConstraint("issue_key", "path"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    issue_key: Mapped[str] = mapped_column(ForeignKey("work_item.issue_key", ondelete="CASCADE"))
    path: Mapped[str] = mapped_column(String)


class LogEntry(Base):
    __tablename__ = "log_entry"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    issue_key: Mapped[str] = mapped_column(ForeignKey("work_item.issue_key", ondelete="CASCADE"))
    at: Mapped[str] = mapped_column(String)
    message: Mapped[str] = mapped_column(String)
    kind: Mapped[str] = mapped_column(String, default="", server_default="")


class EngineLog(Base):
    """A workspace row. A policy change has no issue key to hang on."""
    __tablename__ = "engine_log"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    at: Mapped[str] = mapped_column(String)
    message: Mapped[str] = mapped_column(String)
    kind: Mapped[str] = mapped_column(String, default="", server_default="")


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

    @property
    def status(self) -> str:
        return self.outcome

    @property
    def reason(self) -> str:
        return self.summary

    @property
    def recorded_at(self) -> str:
        return self.created_at


class Ask(WorkerRecord, Base):
    __tablename__ = "ask"
    question: Mapped[str] = mapped_column(String)
    options: Mapped[list[str]] = mapped_column(JSON)
    context: Mapped[str] = mapped_column(String)
    answer: Mapped[str | None] = mapped_column(String, nullable=True)


def _run_result(data: dict, stage: str, attempt: int, outcome: str, ended_at: str) -> None:
    """Record the review stage's collected result. Other stages leave runs unchanged."""
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
        self._active_session = None
        self.engine = create_engine(f"sqlite:///{path}", connect_args={"timeout": 30, "isolation_level": None})

        @event.listens_for(self.engine, "connect")
        def _pragmas(conn, _record) -> None:  # noqa: ANN001 - DBAPI connection
            conn.execute("PRAGMA foreign_keys=ON")

        @event.listens_for(self.engine, "begin")
        def _begin(conn) -> None:  # noqa: ANN001 - SQLAlchemy connection
            # Take the write lock before reading: a WAL snapshot cannot upgrade after another writer commits.
            conn.exec_driver_sql("BEGIN IMMEDIATE" if conn.get_execution_options().get("store_write") else "BEGIN")

    def _stamp(self) -> str:
        return self.clock().isoformat()

    @contextmanager
    def session(self, *, write: bool = True) -> Iterator[Session]:
        """One transaction; readers use a deferred WAL snapshot, writers reserve before reading."""
        if self._active_session is not None:
            yield self._active_session
            return
        with self.engine.connect().execution_options(store_write=write) as conn:
            with Session(conn) as s, s.begin():
                yield s

    @contextmanager
    def transaction(self, *, write: bool = True):
        """Keep related store operations in one snapshot, including the decision POST's state check."""
        if self._active_session is not None:
            yield self
            return
        with self.session(write=write) as s:
            self._active_session = s
            try:
                yield self
            finally:
                self._active_session = None

    def _upsert(self, model: type[Base], keys: tuple[str, ...], values: dict[str, Any]) -> None:
        stmt = insert(model).values(**values)
        rest = {k: v for k, v in values.items() if k not in keys}
        stmt = stmt.on_conflict_do_update(index_elements=keys, set_=rest) if rest else stmt.on_conflict_do_nothing()
        with self.session() as s:
            s.execute(stmt)

    # work items
    def upsert_item(self, issue_key: str, **fields: Any) -> None:
        allowed = {k: v for k, v in fields.items() if k in FORGE_COLUMNS}
        with self.transaction():
            prior = self.item(issue_key)
            if "human_review" in allowed and (prior is None or prior.human_review != allowed["human_review"]):
                data = self.resume(issue_key) or {}
                # The hold's identity survives cache refreshes and changes to other forge fields.
                data["human_review_since"] = self._stamp() if allowed["human_review"] else None
                self._upsert(WorkItem, ("issue_key",), {"issue_key": issue_key, **allowed, "updated_at": self._stamp()})
                self.set_resume(issue_key, data)
            else:
                self._upsert(WorkItem, ("issue_key",), {"issue_key": issue_key, **allowed, "updated_at": self._stamp()})

    def item(self, issue_key: str) -> WorkItem | None:
        with self.session(write=False) as s:
            row = s.get(WorkItem, issue_key)
            if row is not None:
                s.expunge(row)
            return row

    def set_human_review(self, issue_key: str, flag: bool) -> None:
        """Flag an existing item; a kanban snapshot can subsequently refresh it from forge labels."""
        with self.session() as s:
            if s.execute(update(WorkItem).where(WorkItem.issue_key == issue_key)
                         .values(human_review=flag, updated_at=self._stamp())).rowcount == 0:
                raise KeyError(issue_key)

    def items(self) -> list[WorkItem]:
        with self.session(write=False) as s:
            rows = s.scalars(select(WorkItem).order_by(WorkItem.issue_key)).all()
            s.expunge_all()
            return list(rows)

    def all_items(self) -> list[WorkItem]:
        return self.items()

    def sync_from_forge(self, snapshots: list[dict[str, Any]]) -> None:
        """Upsert the forge-owned columns of each snapshot. Engine-owned data is never written, so a sync cannot lose it."""
        for snap in snapshots:
            self.upsert_item(snap["issue_key"], **{k: snap[k] for k in FORGE_COLUMNS if k in snap})

    # segments
    def record_segment(self, issue_key: str, stage: str, attempt: int, started_at: str,
                       ended_at: str | None = None, outcome: str | None = None,
                       runtime: str | None = None, model: str | None = None,
                       effort: str | None = None, agent_type: str | None = None,
                       runner: str | None = None) -> None:
        # Omitted fields stay as stored, so a retried start never clears completion or who ran it.
        done = {k: v for k, v in (("ended_at", ended_at), ("outcome", outcome), ("runtime", runtime),
                                  ("model", model), ("effort", effort), ("agent_type", agent_type))
                if v is not None}
        if outcome == "human_review":
            done["required_human_review"] = True
        self._upsert(Segment, ("issue_key", "stage", "attempt"),
                     {"issue_key": issue_key, "stage": stage, "attempt": attempt, "started_at": started_at, **done})
        if runner is not None:
            def note(state: dict) -> None:
                runs_raw = state.get("runs")
                runs = dict(runs_raw) if isinstance(runs_raw, dict) else {}
                entry_raw = runs.get(f"{stage}-{attempt}")
                run_entry = dict(entry_raw) if isinstance(entry_raw, dict) else {}
                run_entry["runner"] = runner
                runs[f"{stage}-{attempt}"] = run_entry
                state["runs"] = runs

            self.update_resume(issue_key, note)

    def end_segment(self, issue_key: str, stage: str, attempt: int, ended_at: str, outcome: str) -> None:
        """Atomically complete an open segment, preserving start and actor.

        A review also records its collected result. Other stages do not.
        """
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
        rows = self._all(stmt)
        resumes: dict[str, Any] = {}
        for row in rows:
            if row.issue_key not in resumes:
                resumes[row.issue_key] = self.resume(row.issue_key)
            res = resumes[row.issue_key]
            if isinstance(res, dict):
                runs_raw = res.get("runs")
                run_meta = runs_raw.get(f"{row.stage}-{row.attempt}", {}) if isinstance(runs_raw, dict) else {}
                if isinstance(run_meta, dict) and run_meta.get("runner"):
                    row._runner = str(run_meta["runner"])
        return rows

    # worker trees
    def set_tree(self, issue_key: str, path: str) -> None:
        self._upsert(Worktree, ("issue_key", "path"), {"issue_key": issue_key, "path": path})

    def trees(self, issue_key: str) -> list[Worktree]:
        return self._all(select(Worktree).where(Worktree.issue_key == issue_key).order_by(Worktree.path))

    # decisions and log (append-only)
    def add_decision(self, issue_key: str, topic: str, question: str, answer: str | None = None,
                     decided_by: str = "", record: Any = None, *,
                     asked_at: str | None = None, created_at: str | None = None,
                     unless: Callable[[list[Decision]], Decision | None] | None = None) -> Decision:
        """Append one decision row. `unless` sees the issue's rows under the same write lock and may name one
        that wins instead; then nothing is written and that row is returned."""
        stamp = asked_at or created_at or self._stamp()
        with self.session() as s:
            if unless is not None:
                winner = unless(list(s.scalars(select(Decision).where(Decision.issue_key == issue_key)
                                               .order_by(Decision.id)).all()))
                if winner is not None:
                    s.expunge(winner)
                    return winner
            row = Decision(issue_key=issue_key, topic=topic, question=question, answer=answer,
                           decided_by=decided_by, record=record, created_at=stamp)
            s.add(row)
            s.flush()
            s.expunge(row)
            return row

    def decision(self, decision_id: int) -> Decision | None:
        with self.session(write=False) as s:
            row = s.get(Decision, decision_id)
            if row is not None:
                s.expunge(row)
            return row

    def decisions(self, issue_key: str | None = None) -> list[Decision]:
        stmt = select(Decision).order_by(Decision.id)
        if issue_key is not None:
            stmt = stmt.where(Decision.issue_key == issue_key)
        return self._all(stmt)

    def add_log(self, issue_key: str, message: str, at: str, kind: str = "") -> None:
        with self.session() as s:
            s.add(LogEntry(issue_key=issue_key, at=at, message=message, kind=kind))

    def log_event(self, issue_key: str, message: str, at: str | None = None, kind: str = "") -> None:
        self.add_log(issue_key, message, at or self._stamp(), kind)

    def log(self, issue_key: str | None = None) -> list[LogEntry]:
        stmt = select(LogEntry).order_by(LogEntry.id)
        if issue_key is not None:
            stmt = stmt.where(LogEntry.issue_key == issue_key)
        return self._all(stmt)

    def add_engine_log(self, message: str, kind: str = "") -> None:
        with self.session() as s:
            s.add(EngineLog(at=self._stamp(), message=message, kind=kind))

    def engine_log(self) -> list[EngineLog]:
        return self._all(select(EngineLog).order_by(EngineLog.id))

    # resume
    def set_resume(self, issue_key: str, data: Any) -> None:
        self._upsert(Resume, ("issue_key",), {"issue_key": issue_key, "data": data})

    def resume(self, issue_key: str) -> Any:
        with self.session(write=False) as s:
            row = s.get(Resume, issue_key)
            return row.data if row else None

    def update_resume(self, issue_key: str, mutate: Callable[[dict], None]) -> None:
        """Change some keys of the resume blob from its current value, in one write transaction.

        Callers hold a copy across slow work (a forge call, a model call) and so must not write the whole blob back:
        the board POST and the worker add keys meanwhile."""
        with self.transaction():
            data = self.resume(issue_key)
            data = dict(data) if isinstance(data, dict) else {}
            mutate(data)
            self.set_resume(issue_key, data)

    def finish_suite_hold(self, issue_key: str, *, accept: bool) -> None:
        """Atomically retire the hold, optionally accepting its segment.

        Accepting a review records that collected result as done. Other stages only change the
        segment outcome. The Use Case decides when this transition is allowed. Keep the historical
        review marker and actor/timing fields when accepting; callers never edit the worker result layout.
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
                   outcome: str, summary: str, findings: list[dict[str, Any]], suite: Any = None,
                   *, created_at: str | None = None) -> int:
        with self.session() as s:
            row = Report(issue_key=issue_key, stage=stage, attempt=attempt, session_id=session_id,
                         outcome=outcome, summary=summary, findings=findings, suite=suite,
                         created_at=created_at or self._stamp())
            s.add(row)
            s.flush()
            return row.id

    def record_report(self, issue_key: str, stage: str, attempt: int, *,
                      status: str = "done", outcome: str | None = None,
                      summary: str = "", reason: str = "", changes: str = "",
                      session_id: str = "", findings: list[dict[str, Any]] | None = None,
                      recorded_at: str | None = None) -> int:
        items = list(findings or [])
        if changes and not items:
            items = [{"path": c.strip()} for c in changes.split(",") if c.strip()]
        return self.add_report(
            issue_key, stage, attempt, session_id or f"{stage}-{attempt}",
            outcome or status, summary or reason, items, created_at=recorded_at,
        )

    def reports(self, issue_key: str | None = None, *, stage: str | None = None, attempt: int | None = None,
                session_id: str | None = None) -> list[Report]:
        stmt = select(Report)
        if issue_key is not None:
            stmt = stmt.where(Report.issue_key == issue_key)
        for key, value in (("stage", stage), ("attempt", attempt), ("session_id", session_id)):
            if value is not None:
                stmt = stmt.where(getattr(Report, key) == value)
        return self._all(stmt.order_by(Report.attempt, Report.id))

    def segment_reports(self, issue_key: str, stage: str, attempt: int) -> list[Report]:
        """Reports for this segment's recorded worker session, in insertion order."""
        data = self.resume(issue_key)
        runs = data.get("runs") if isinstance(data, dict) else None
        record = runs.get(f"{stage}-{attempt}") if isinstance(runs, dict) else None
        session = record.get("session_id") if isinstance(record, dict) else None
        if not isinstance(session, str) or not session:
            return []
        return self.reports(issue_key, stage=stage, attempt=attempt, session_id=session)

    def add_ask(self, issue_key: str, stage: str, attempt: int, session_id: str,
                question: str, options: list[str], context: str) -> int:
        with self.session() as s:
            row = Ask(issue_key=issue_key, stage=stage, attempt=attempt, session_id=session_id,
                      question=question, options=options, context=context, created_at=self._stamp())
            s.add(row)
            s.flush()
            return row.id

    def asks(self, issue_key: str | None = None) -> list[Ask]:
        stmt = select(Ask).order_by(Ask.id)
        if issue_key is not None:
            stmt = stmt.where(Ask.issue_key == issue_key)
        return self._all(stmt)

    def answer_ask(self, issue_key: str, ask_id: int, answer: str) -> None:
        """An operator's or the trio's adopted answer, bound to the named issue; worker tools never expose this write.
        An answer outside the ask's options, or a different second answer, is refused (#750); the same answer again
        is a no-op, so a retried intake or relay is idempotent (architect ruling on #758)."""
        if not isinstance(answer, str) or not answer.strip():
            raise ValueError("an ask answer must not be empty")
        with self.session() as s:
            row = s.get(Ask, ask_id)
            if row is None or row.issue_key != issue_key:
                raise KeyError(f"ask {ask_id} does not belong to {issue_key}")
            if row.answer is not None:
                if row.answer == answer:
                    return
                raise KeyError(f"ask {ask_id} is already answered")  # #750: a different second answer is refused
            if row.options and answer not in row.options:
                raise ValueError(f"ask answer {answer!r} is not in options")
            row.answer = answer

    def _all(self, stmt: Any) -> list[Any]:
        with self.session(write=False) as s:
            rows = s.scalars(stmt).all()
            s.expunge_all()
            return list(rows)
