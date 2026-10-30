"""The issue-keyed store: one row per forge issue, written by upsert only.

The forge is the source of truth. The forge-owned columns of a `WorkItem` (title, stage, forge/review/merge state, and the
human-review flag when kanban is configured) are rebuildable; segments, decisions, worker trees, log,
and resume are the engine's own and are never touched by a sync. Omitted snapshot fields keep their stored values.
The schema lives in `migrations/`, not in `create_all`: `migrate` is the one place
it changes, and a test holds these models to what the migrations build.
"""

from __future__ import annotations

import os
import sqlite3
import uuid
from contextlib import closing, contextmanager
from copy import deepcopy
from pathlib import Path
from typing import Any, Callable, Iterator, Sequence

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
import private_state

from sqlalchemy import JSON, Boolean, CheckConstraint, ForeignKey, Integer, String, UniqueConstraint, create_engine, delete, event, select, update
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

import clock
import decision_graph
import decision_history

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
    workflow: Mapped[str] = mapped_column(String, default="", server_default="")

    @property
    def runner(self) -> str:
        return getattr(self, "_runner", "")


class Transcript(Base):
    """A bounded ordered event buffer and durable cursor, owned by exactly one segment."""
    __tablename__ = "transcript"
    segment_id: Mapped[int] = mapped_column(ForeignKey("segment.id", ondelete="CASCADE"), primary_key=True)
    session_id: Mapped[str] = mapped_column(String, unique=True)
    source: Mapped[str | None] = mapped_column(String, unique=True, nullable=True)
    state: Mapped[dict[str, Any]] = mapped_column(JSON)


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


class RotationCursor(Base):
    """The last selected entry per configured task class, shared by this workspace's launches."""
    __tablename__ = "rotation_cursor"
    task_class: Mapped[str] = mapped_column(String, primary_key=True)
    entry: Mapped[str] = mapped_column(String)


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


class WorkerMessage(WorkerRecord, Base):
    """An operator message addressed to exactly one worker session, never implicitly retargeted."""
    __tablename__ = "worker_message"
    __table_args__ = (CheckConstraint("status IN ('queued', 'sending', 'delivered', 'failed')", name="worker_message_status"),)
    request_id: Mapped[str] = mapped_column(String, unique=True)
    body: Mapped[str] = mapped_column(String)
    status: Mapped[str] = mapped_column(String)
    evidence: Mapped[str] = mapped_column(String)
    delivery_id: Mapped[str | None] = mapped_column(String, nullable=True, unique=True)
    claimed_at: Mapped[str | None] = mapped_column(String, nullable=True)
    resolved_at: Mapped[str | None] = mapped_column(String, nullable=True)


class DecisionGraphNode(Base):
    """One decision in the workspace decision DAG; leaves are nodes no child links to as parent."""
    __tablename__ = "decision_graph_node"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    topic: Mapped[str] = mapped_column(String, default="")
    decision: Mapped[str] = mapped_column(String)
    reason: Mapped[str] = mapped_column(String)
    decided_by: Mapped[str] = mapped_column(String, default="")
    issue_key: Mapped[str] = mapped_column(String, default="")
    created_at: Mapped[str] = mapped_column(String)


class DecisionGraphEdge(Base):
    """Directed provenance link: `child_id` replaces or refines `parent_id`."""
    __tablename__ = "decision_graph_edge"
    __table_args__ = (
        UniqueConstraint("child_id", "parent_id"),
        CheckConstraint("child_id != parent_id", name="decision_graph_no_self_edge"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    child_id: Mapped[int] = mapped_column(ForeignKey("decision_graph_node.id", ondelete="CASCADE"))
    parent_id: Mapped[int] = mapped_column(ForeignKey("decision_graph_node.id", ondelete="CASCADE"))
    position: Mapped[int] = mapped_column(Integer, default=0)


class DecisionGraphRejected(Base):
    """Rejected alternative attached strictly to an ancestor decision, never to a current leaf."""
    __tablename__ = "decision_graph_rejected"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ancestor_id: Mapped[int] = mapped_column(ForeignKey("decision_graph_node.id", ondelete="CASCADE"))
    approach: Mapped[str] = mapped_column(String)
    reason: Mapped[str] = mapped_column(String)
    created_at: Mapped[str] = mapped_column(String)


class DecisionHistoryRequest(Base):
    """A worker's justified request for ancestor history or a narrow rejected-alternative check."""
    __tablename__ = "decision_history_request"
    __table_args__ = (
        CheckConstraint("kind IN ('ancestors', 'was_rejected')", name="decision_history_request_kind"),
        CheckConstraint("status IN ('pending', 'approved', 'denied')", name="decision_history_request_status"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    issue_key: Mapped[str] = mapped_column(String, default="")
    stage: Mapped[str] = mapped_column(String, default="")
    attempt: Mapped[int] = mapped_column(Integer, default=0)
    session_id: Mapped[str] = mapped_column(String, default="")
    kind: Mapped[str] = mapped_column(String)
    leaf_id: Mapped[int | None] = mapped_column(ForeignKey("decision_graph_node.id", ondelete="SET NULL"), nullable=True)
    other_leaf_id: Mapped[int | None] = mapped_column(ForeignKey("decision_graph_node.id", ondelete="SET NULL"), nullable=True)
    requested_depth: Mapped[int] = mapped_column(Integer, default=1)
    approach: Mapped[str] = mapped_column(String, default="")
    justification: Mapped[str] = mapped_column(String)
    evidence: Mapped[str] = mapped_column(String, default="")
    status: Mapped[str] = mapped_column(String)
    grant_case: Mapped[str] = mapped_column(String, default="")
    verdict_reason: Mapped[str] = mapped_column(String, default="")
    approved_depth: Mapped[int | None] = mapped_column(Integer, nullable=True)
    report_to_architect: Mapped[bool] = mapped_column(Boolean, default=False)
    decided_by: Mapped[str] = mapped_column(String, default="")
    created_at: Mapped[str] = mapped_column(String)
    decided_at: Mapped[str | None] = mapped_column(String, nullable=True)


class DecisionHistoryGrant(Base):
    """Append-only audit log of every approved decision history or rejected-alternative grant."""
    __tablename__ = "decision_history_grant"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    request_id: Mapped[int] = mapped_column(ForeignKey("decision_history_request.id", ondelete="CASCADE"), unique=True)
    issue_key: Mapped[str] = mapped_column(String, default="")
    session_id: Mapped[str] = mapped_column(String, default="")
    kind: Mapped[str] = mapped_column(String)
    grant_case: Mapped[str] = mapped_column(String)
    leaf_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    approved_depth: Mapped[int | None] = mapped_column(Integer, nullable=True)
    returned_ids: Mapped[list[int]] = mapped_column(JSON)
    matched_approach: Mapped[str] = mapped_column(String, default="")
    granted_at: Mapped[str] = mapped_column(String)


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
    with closing(sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True, timeout=30)) as conn:
        try:
            return conn.execute("SELECT version_num FROM alembic_version").fetchall() == [(head,)]
        except sqlite3.OperationalError:  # no alembic_version table: never migrated
            return False



def verdict_fifo_path(root: Path, request_id: int) -> Path:
    return Path(root).resolve() / ".chief-of-stuff" / "verdicts" / f"req_{request_id}.fifo"


def signal_history_verdict(root: Path, request_id: int) -> None:
    """Wake a blocked MCP `decisions` waiter immediately once its verdict commits."""
    fifo = verdict_fifo_path(root, request_id)
    if not fifo.exists():
        return
    try:
        wfd = os.open(fifo, os.O_WRONLY | os.O_NONBLOCK)
    except OSError:
        return
    try:
        os.write(wfd, b"\x01")
    except OSError:
        pass
    finally:
        os.close(wfd)


def migrate(path: Path | str) -> None:
    """Enable WAL and bring the database at `path` to the latest revision. Call it once at engine start, never from
    concurrent writers: journal-mode changes need an exclusive lock, and first-time migrations race to create tables."""
    private_state.store_files(Path(path), create=True)
    command.upgrade(_config(path), "head")


class Store:
    def __init__(self, path: Path | str, clock: clock.Clock = clock.system) -> None:
        self.path = Path(path)
        private_state.store_files(self.path, create=True)
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
    def rotation_choice(self, issue_key: str, stage: str, attempt: int, task_class: str,
                        eligible: tuple[str, ...], choose: Callable[[str], str], workflow: str = "") -> str:
        """Reserve a turn and its workflow atomically; recovering the same launch spends no extra turn.

        Selection policy stays with the caller. A persisted selection which is no longer eligible is replaced.
        The resume evidence survives a refused start dropping its slot reservation.
        """
        with self.session() as s:
            resume = s.get(Resume, issue_key)
            data = deepcopy(resume.data) if resume is not None and isinstance(resume.data, dict) else {}
            choices = data.setdefault("rotation_choices", {})
            run = f"{stage}-{attempt}"
            prior = choices.get(run, {})
            if prior.get("entry") in eligible:
                return prior["entry"]
            cursor = s.get(RotationCursor, task_class)
            entry = choose(cursor.entry if cursor else "")
            choices[run] = {"entry": entry, "workflow": workflow}
            if resume is None:
                s.add(Resume(issue_key=issue_key, data=data))
            else:
                resume.data = data
            if cursor is None:
                s.add(RotationCursor(task_class=task_class, entry=entry))
            else:
                cursor.entry = entry
            return entry

    def record_segment(self, issue_key: str, stage: str, attempt: int, started_at: str,
                       ended_at: str | None = None, outcome: str | None = None,
                       runtime: str | None = None, model: str | None = None,
                       effort: str | None = None, agent_type: str | None = None,
                       runner: str | None = None, workflow: str | None = None) -> None:
        # Omitted fields stay as stored, so a retried start never clears completion or who ran it.
        if workflow is None:
            data = self.resume(issue_key)
            choice = data.get("rotation_choices", {}).get(f"{stage}-{attempt}", {}) if isinstance(data, dict) else {}
            workflow = choice.get("workflow")
        done = {k: v for k, v in (("ended_at", ended_at), ("outcome", outcome), ("runtime", runtime),
                                  ("model", model), ("effort", effort), ("agent_type", agent_type), ("workflow", workflow))
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

    def transcript(self, issue_key: str, stage: str, attempt: int) -> dict | None:
        """Page-server read API: exact attempt identity, ordered events and loss/cursor metadata.

        Reads only the store; the runtime and its original log need not still exist.
        """
        with self.session(write=False) as s:
            row = s.scalar(select(Transcript).join(Segment).where(
                Segment.issue_key == issue_key, Segment.stage == stage, Segment.attempt == attempt))
            if row is None:
                return None
            return {"issue_key": issue_key, "stage": stage, "attempt": attempt,
                    "session_id": row.session_id, "source": row.source, **deepcopy(row.state)}

    def update_transcript(self, issue_key: str, stage: str, attempt: int, session_id: str,
                          source: str | None, change: Callable[[dict, bool], None]) -> None:
        """Serialize binding, ingestion and checkpoint in one ORM transaction.

        The callback receives whether the immutable launch/source identity still matches.
        A source or session already owned by another attempt is refused before reading content.
        """
        with self.session() as s:
            segment = s.scalar(select(Segment).where(Segment.issue_key == issue_key,
                               Segment.stage == stage, Segment.attempt == attempt))
            if segment is None:
                raise ValueError("transcript requires an existing segment")
            row = s.get(Transcript, segment.id)
            matches = row is None or (row.session_id == session_id
                                      and (source is None or row.source is None or row.source == source))
            if matches:
                same_source = Transcript.source == source if source is not None else False
                duplicate = s.scalar(select(Transcript.segment_id).where(
                    Transcript.segment_id != segment.id, (Transcript.session_id == session_id) | same_source))
                if duplicate is not None:
                    raise ValueError("transcript source/session already bound to another attempt")
            if row is None:
                row = Transcript(segment_id=segment.id, session_id=session_id, source=source, state={})
                s.add(row)
            elif matches and row.source is None:
                row.source = source
            state = deepcopy(row.state)
            change(state, matches)
            row.state = state

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

    def queue_worker_message(self, issue_key: str, stage: str, attempt: int, session_id: str, body: str,
                             *, request_id: str) -> WorkerMessage:
        """Persist an operator message to a currently open, identified worker attempt.

        A request id gives clients safe retries. It binds the entire request, so a different body or destination
        cannot reuse it. A queued record by itself is not a delivery claim; no transport is invoked here.
        """
        if not all(isinstance(value, str) and value.strip() for value in (issue_key, stage, session_id, body, request_id)):
            raise ValueError("message identity, text and request_id must be nonempty strings")
        if not isinstance(attempt, int) or isinstance(attempt, bool) or attempt < 1:
            raise ValueError("attempt must be a positive integer")
        with self.session() as s:
            prior = s.scalar(select(WorkerMessage).where(WorkerMessage.request_id == request_id))
            if prior is not None:
                if (prior.issue_key, prior.stage, prior.attempt, prior.session_id, prior.body) != (
                        issue_key, stage, attempt, session_id, body):
                    raise ValueError("request_id already belongs to a different message")
                s.expunge(prior)
                return prior
            segment = s.scalar(select(Segment).where(Segment.issue_key == issue_key, Segment.stage == stage,
                                                     Segment.attempt == attempt))
            resume = s.get(Resume, issue_key)
            data = resume.data if resume is not None else None
            runs = data.get("runs") if isinstance(data, dict) else None
            run = runs.get(f"{stage}-{attempt}") if isinstance(runs, dict) else None
            if segment is None or segment.ended_at is not None or not isinstance(run, dict) or run.get("session_id") != session_id:
                raise ValueError("worker attempt is not open with the requested session")
            row = WorkerMessage(issue_key=issue_key, stage=stage, attempt=attempt, session_id=session_id,
                                request_id=request_id, body=body, status="queued", evidence="", created_at=self._stamp())
            s.add(row)
            s.flush()
            s.expunge(row)
            return row

    def worker_messages(self, issue_key: str, *, stage: str | None = None, attempt: int | None = None,
                        session_id: str | None = None) -> list[WorkerMessage]:
        stmt = select(WorkerMessage).where(WorkerMessage.issue_key == issue_key)
        for key, value in (("stage", stage), ("attempt", attempt), ("session_id", session_id)):
            if value is not None:
                stmt = stmt.where(getattr(WorkerMessage, key) == value)
        return self._all(stmt.order_by(WorkerMessage.id))

    def begin_worker_message_delivery(self, message_id: int, issue_key: str, stage: str, attempt: int,
                                      session_id: str) -> WorkerMessage:
        """Claim before writing to a transport. A claim surviving a crash is unconfirmed, never auto-retried."""
        with self.session() as s:
            row = s.get(WorkerMessage, message_id)
            if row is None or (row.issue_key, row.stage, row.attempt, row.session_id) != (issue_key, stage, attempt, session_id):
                raise ValueError("message does not belong to this worker session")
            if row.status != "queued":
                raise ValueError("message delivery was already claimed or resolved")
            segment = s.scalar(select(Segment).where(Segment.issue_key == issue_key, Segment.stage == stage,
                                                     Segment.attempt == attempt))
            resume = s.get(Resume, issue_key)
            data = resume.data if resume is not None else None
            runs = data.get("runs") if isinstance(data, dict) else None
            run = runs.get(f"{stage}-{attempt}") if isinstance(runs, dict) else None
            if segment is None or segment.ended_at is not None or not isinstance(run, dict) or run.get("session_id") != session_id:
                raise ValueError("worker attempt is not open with the requested session")
            row.status = "sending"
            row.delivery_id = str(uuid.uuid4())
            row.claimed_at = self._stamp()
            s.flush()
            s.expunge(row)
            return row

    def _resolve_worker_message(self, message_id: int, issue_key: str, stage: str, attempt: int, session_id: str,
                                status: str, evidence: str, delivery_id: str | None = None,
                                expected_status: str | None = None) -> None:
        if not isinstance(evidence, str) or not evidence.strip():
            raise ValueError("message resolution requires evidence")
        with self.session() as s:
            row = s.get(WorkerMessage, message_id)
            if row is None or (row.issue_key, row.stage, row.attempt, row.session_id) != (issue_key, stage, attempt, session_id):
                raise ValueError("message does not belong to this worker session")
            if status == "delivered" and (not delivery_id or delivery_id != row.delivery_id):
                raise ValueError("acknowledgment does not match the delivery claim")
            if expected_status is not None and (row.status != expected_status or
                    (expected_status == "sending" and (not delivery_id or row.delivery_id != delivery_id))):
                raise ValueError("message state or delivery claim changed")
            if row.status == status and row.evidence == evidence:
                return
            if row.status not in ("queued", "sending") or (status == "delivered" and row.status != "sending"):
                raise ValueError("message already resolved")
            row.status = status
            row.evidence = evidence
            row.resolved_at = self._stamp()

    def ack_worker_message(self, message_id: int, issue_key: str, stage: str, attempt: int, session_id: str,
                           *, delivery_id: str, evidence: str) -> None:
        """Record an adapter's affirmative acknowledgment, never a send attempt or a successful enqueue."""
        self._resolve_worker_message(message_id, issue_key, stage, attempt, session_id, "delivered", evidence, delivery_id)

    def fail_worker_message(self, message_id: int, issue_key: str, stage: str, attempt: int, session_id: str,
                            *, reason: str, expected_status: str = "queued", delivery_id: str | None = None) -> None:
        """Fail a queued message or the exact sending claim supported by transport evidence."""
        if expected_status not in ("queued", "sending"):
            raise ValueError("failure requires queued state or a sending claim")
        self._resolve_worker_message(message_id, issue_key, stage, attempt, session_id, "failed", reason,
                                     delivery_id, expected_status)

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

    # decision graph and history tool (§17, #1181)
    def _parents_map(self, s: Session) -> dict[int, tuple[int, ...]]:
        node_ids = list(s.scalars(select(DecisionGraphNode.id).order_by(DecisionGraphNode.id)).all())
        by_node: dict[int, list[int]] = {nid: [] for nid in node_ids}
        edges = s.scalars(
            select(DecisionGraphEdge).order_by(DecisionGraphEdge.child_id, DecisionGraphEdge.position, DecisionGraphEdge.id)
        ).all()
        for edge in edges:
            if edge.child_id in by_node and edge.parent_id in by_node:
                by_node[edge.child_id].append(edge.parent_id)
        return {nid: tuple(pids) for nid, pids in by_node.items()}

    def _graph_snapshot(self, s: Session) -> tuple[dict[int, decision_graph.AncestorDecision], dict[int, tuple[int, ...]]]:
        parents_by_node = self._parents_map(s)
        ancestors = decision_graph.ancestor_ids(parents_by_node)
        rejected_by_ancestor: dict[int, list[decision_graph.RejectedAlternative]] = {}
        for rej in s.scalars(select(DecisionGraphRejected).order_by(DecisionGraphRejected.id)).all():
            if rej.ancestor_id in ancestors:
                rejected_by_ancestor.setdefault(rej.ancestor_id, []).append(
                    decision_graph.RejectedAlternative(approach=rej.approach, reason=rej.reason)
                )
        nodes_by_id: dict[int, decision_graph.AncestorDecision] = {}
        for row in s.scalars(select(DecisionGraphNode).order_by(DecisionGraphNode.id)).all():
            nodes_by_id[row.id] = decision_graph.AncestorDecision(
                id=row.id,
                topic=row.topic,
                decision=row.decision,
                reason=row.reason,
                parent_ids=parents_by_node.get(row.id, ()),
                rejected_alternatives=tuple(rejected_by_ancestor.get(row.id, ())),
                decided_by=row.decided_by,
                issue_key=row.issue_key,
                created_at=row.created_at,
            )
        return nodes_by_id, parents_by_node

    def add_graph_decision(
        self,
        decision: str,
        reason: str,
        *,
        parent_ids: Sequence[int] = (),
        topic: str = "",
        decided_by: str = "",
        issue_key: str = "",
        rejected_alternatives: Sequence[decision_graph.RejectedAlternative | dict[str, str]] = (),
        created_at: str | None = None,
    ) -> decision_graph.DecisionLeaf:
        if not isinstance(decision, str) or not decision.strip():
            raise ValueError("decision text must be non-empty")
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("decision reason must be non-empty")
        normalized_alts: list[decision_graph.RejectedAlternative] = []
        for alt in rejected_alternatives:
            if isinstance(alt, decision_graph.RejectedAlternative):
                normalized_alts.append(alt)
            elif isinstance(alt, dict):
                normalized_alts.append(
                    decision_graph.RejectedAlternative(
                        approach=str(alt.get("approach", "")).strip(),
                        reason=str(alt.get("reason", "")).strip(),
                    )
                )
            else:
                raise ValueError(f"unsupported rejected alternative: {alt!r}")
        stamp = created_at or self._stamp()
        with self.session() as s:
            existing_parents = self._parents_map(s)
            validated_parents = decision_graph.validate_parent_links(existing_parents, None, parent_ids)
            if normalized_alts and not validated_parents:
                raise ValueError("rejected alternatives live only in ancestors, never in a leaf")
            node = DecisionGraphNode(
                topic=topic.strip(),
                decision=decision.strip(),
                reason=reason.strip(),
                decided_by=decided_by.strip(),
                issue_key=issue_key.strip(),
                created_at=stamp,
            )
            s.add(node)
            s.flush()
            for idx, pid in enumerate(validated_parents):
                s.add(DecisionGraphEdge(child_id=node.id, parent_id=pid, position=idx))
            if normalized_alts:
                # Attach rejected alternatives to the primary superseded ancestor, never to the new leaf.
                target_ancestor = validated_parents[0]
                for alt in normalized_alts:
                    s.add(
                        DecisionGraphRejected(
                            ancestor_id=target_ancestor,
                            approach=alt.approach,
                            reason=alt.reason,
                            created_at=stamp,
                        )
                    )
            return decision_graph.DecisionLeaf(
                id=node.id,
                topic=node.topic,
                decision=node.decision,
                reason=node.reason,
                parent_ids=validated_parents,
                decided_by=node.decided_by,
                issue_key=node.issue_key,
                created_at=node.created_at,
            )

    def add_rejected_alternative(
        self,
        ancestor_id: int,
        approach: str,
        reason: str,
        *,
        created_at: str | None = None,
    ) -> decision_graph.RejectedAlternative:
        alt = decision_graph.RejectedAlternative(approach=approach.strip(), reason=reason.strip())
        stamp = created_at or self._stamp()
        with self.session() as s:
            parents_by_node = self._parents_map(s)
            decision_graph.validate_rejected_target(parents_by_node, ancestor_id)
            s.add(
                DecisionGraphRejected(
                    ancestor_id=ancestor_id,
                    approach=alt.approach,
                    reason=alt.reason,
                    created_at=stamp,
                )
            )
        return alt

    def decision_leaves(self) -> tuple[decision_graph.DecisionLeaf, ...]:
        """Return only current unreplaced leaves (full decision text, reason, parent links; no rejected alternatives)."""
        with self.session(write=False) as s:
            parents_by_node = self._parents_map(s)
            current_ids = decision_graph.leaf_ids(parents_by_node)
            if not current_ids:
                return ()
            rows = s.scalars(
                select(DecisionGraphNode).where(DecisionGraphNode.id.in_(sorted(current_ids))).order_by(DecisionGraphNode.id)
            ).all()
            return tuple(
                decision_graph.DecisionLeaf(
                    id=row.id,
                    topic=row.topic,
                    decision=row.decision,
                    reason=row.reason,
                    parent_ids=parents_by_node.get(row.id, ()),
                    decided_by=row.decided_by,
                    issue_key=row.issue_key,
                    created_at=row.created_at,
                )
                for row in rows
            )

    @staticmethod
    def _serialize_leaf(leaf: decision_graph.DecisionLeaf) -> dict[str, Any]:
        return {
            "id": leaf.id,
            "topic": leaf.topic,
            "decision": leaf.decision,
            "reason": leaf.reason,
            "parent_ids": list(leaf.parent_ids),
            "decided_by": leaf.decided_by,
            "issue_key": leaf.issue_key,
            "created_at": leaf.created_at,
        }

    @staticmethod
    def _serialize_ancestor(node: decision_graph.AncestorDecision) -> dict[str, Any]:
        return {
            "id": node.id,
            "topic": node.topic,
            "decision": node.decision,
            "reason": node.reason,
            "parent_ids": list(node.parent_ids),
            "rejected_alternatives": [
                {"approach": alt.approach, "reason": alt.reason} for alt in node.rejected_alternatives
            ],
            "decided_by": node.decided_by,
            "issue_key": node.issue_key,
            "created_at": node.created_at,
        }

    def read_decision_graph(
        self,
        *,
        role: str,
        leaf_id: int | None = None,
        max_depth: int | None = None,
    ) -> dict[str, Any]:
        """Free-reader access strictly for the coordinator, the architect and the owner (`Fr -> reads freely`)."""
        if not decision_history.reads_freely(role):
            raise PermissionError("only the coordinator, the architect and the owner may read history freely")
        with self.session(write=False) as s:
            nodes_by_id, parents_by_node = self._graph_snapshot(s)
        leaves = decision_graph.current_leaves(list(nodes_by_id.values()), parents_by_node)
        result: dict[str, Any] = {"leaves": [self._serialize_leaf(leaf) for leaf in leaves]}
        if leaf_id is not None:
            ancestors = decision_graph.ancestor_slice(nodes_by_id, leaf_id, max_depth=max_depth)
            result["ancestors"] = [self._serialize_ancestor(a) for a in ancestors]
        else:
            anc_ids = decision_graph.ancestor_ids(parents_by_node)
            result["ancestors"] = [
                self._serialize_ancestor(nodes_by_id[nid]) for nid in sorted(anc_ids) if nid in nodes_by_id
            ]
        return result

    def submit_history_request(
        self,
        req: decision_history.HistoryRequest,
        *,
        stage: str = "",
        attempt: int = 0,
    ) -> DecisionHistoryRequest:
        refusal = decision_history.validate_worker_request(req)
        if refusal is not None:
            raise ValueError(refusal)
        with self.session() as s:
            known_ids = set(s.scalars(select(DecisionGraphNode.id)).all())
            leaf_fk = req.leaf_id if req.leaf_id in known_ids else None
            other_fk = req.other_leaf_id if req.other_leaf_id in known_ids else None
            row = DecisionHistoryRequest(
                issue_key=req.issue_key.strip(),
                stage=stage.strip(),
                attempt=attempt,
                session_id=req.worker_id.strip(),
                kind=req.kind,
                leaf_id=leaf_fk,
                other_leaf_id=other_fk,
                requested_depth=max(1, req.requested_depth),
                approach=req.approach.strip(),
                justification=req.justification.strip(),
                evidence=req.evidence.strip(),
                status="pending",
                grant_case="",
                verdict_reason="",
                approved_depth=None,
                report_to_architect=False,
                decided_by="",
                created_at=self._stamp(),
                decided_at=None,
            )
            s.add(row)
            s.flush()
            s.expunge(row)
            return row

    def history_request(self, request_id: int) -> DecisionHistoryRequest | None:
        with self.session(write=False) as s:
            row = s.get(DecisionHistoryRequest, request_id)
            if row is not None:
                s.expunge(row)
            return row

    def pending_history_requests(self) -> list[DecisionHistoryRequest]:
        """Return pending history requests (request metadata and justification text only; zero history)."""
        return self._all(
            select(DecisionHistoryRequest)
            .where(DecisionHistoryRequest.status == "pending")
            .order_by(DecisionHistoryRequest.id)
        )

    def decide_history_request(
        self,
        request_id: int,
        *,
        approved: bool,
        reason: str,
        grant_case: str = "",
        approved_depth: int | None = None,
        report_to_architect: bool = False,
        decided_by: str = "coordinator",
    ) -> DecisionHistoryRequest:
        """Record the coordinator's verdict on a pending request without reading ancestor history."""
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("verdict reason must be non-empty")
        target_status = "approved" if approved else "denied"
        with self.session() as s:
            row = s.get(DecisionHistoryRequest, request_id)
            if row is None:
                raise KeyError(f"unknown history request: {request_id}")
            if row.status != "pending":
                if row.status == target_status:
                    s.expunge(row)
                    signal_history_verdict(self.path.resolve().parent, request_id)
                    return row
                raise ValueError(f"history request {request_id} is already {row.status}")
            if approved:
                if row.kind == "ancestors":
                    current_ids = decision_graph.leaf_ids(self._parents_map(s))
                    if row.leaf_id is None or row.leaf_id not in current_ids:
                        raise ValueError(f"decision {row.leaf_id!r} is not a current leaf")
                    depth = approved_depth if approved_depth is not None else row.requested_depth
                    if type(depth) is not int or depth < 1:
                        raise ValueError("approved_depth must be a positive integer")
                    row.approved_depth = depth
                    row.grant_case = grant_case.strip() or "coordinator_approved"
                else:
                    row.approved_depth = None
                    row.grant_case = grant_case.strip() or "rejected_approach"
                row.report_to_architect = bool(report_to_architect)
            else:
                row.approved_depth = None
                row.grant_case = ""
                row.report_to_architect = False
            row.status = target_status
            row.verdict_reason = reason.strip()
            row.decided_by = decided_by.strip() or "coordinator"
            row.decided_at = self._stamp()
            if row.report_to_architect:
                s.add(
                    EngineLog(
                        at=row.decided_at,
                        kind="architect_mismatch",
                        message=(
                            f"Decision #{row.leaf_id} code mismatch reported on request #{row.id} "
                            f"({row.issue_key or 'workspace'}): {row.justification}"
                        ),
                    )
                )
            s.flush()
            s.expunge(row)
        signal_history_verdict(self.path.resolve().parent, request_id)
        return row

    def judge_history_request(self, request_id: int, *, decided_by: str = "coordinator") -> DecisionHistoryRequest:
        """Judge one pending request using only current leaves and the worker's justification text."""
        row = self.history_request(request_id)
        if row is None:
            raise KeyError(f"unknown history request: {request_id}")
        if row.status != "pending":
            return row
        leaves = self.decision_leaves()
        req = decision_history.HistoryRequest(
            kind=row.kind,
            justification=row.justification,
            leaf_id=row.leaf_id,
            other_leaf_id=row.other_leaf_id,
            requested_depth=row.requested_depth,
            approach=row.approach,
            evidence=row.evidence,
            issue_key=row.issue_key,
            worker_id=row.session_id,
        )
        verdict = decision_history.judge_justification(req, leaves)
        return self.decide_history_request(
            request_id,
            approved=verdict.approved,
            reason=verdict.reason,
            grant_case=verdict.grant_case,
            approved_depth=verdict.approved_depth,
            report_to_architect=verdict.report_to_architect,
            decided_by=decided_by,
        )

    def judge_pending_history_requests(self, *, decided_by: str = "coordinator") -> list[DecisionHistoryRequest]:
        """Judge all pending history requests against current leaves without reading history."""
        return [self.judge_history_request(row.id, decided_by=decided_by) for row in self.pending_history_requests()]

    def fulfill_history_request(self, request_id: int) -> dict[str, Any]:
        """Materialize the approved slice straight for the worker and durably log the grant (`Ap -> Lg`)."""
        with self.session() as s:
            row = s.get(DecisionHistoryRequest, request_id)
            if row is None:
                raise KeyError(f"unknown history request: {request_id}")
            if row.status == "pending":
                return {"status": "pending", "request_id": row.id, "kind": row.kind}
            if row.status == "denied":
                return {
                    "status": "denied",
                    "request_id": row.id,
                    "kind": row.kind,
                    "reason": row.verdict_reason,
                }
            nodes_by_id, parents_by_node = self._graph_snapshot(s)
            stamp = self._stamp()
            existing_grant = s.scalar(
                select(DecisionHistoryGrant).where(DecisionHistoryGrant.request_id == row.id)
            )
            if row.kind == "ancestors":
                if row.leaf_id is None or row.leaf_id not in nodes_by_id:
                    raise KeyError(f"unknown decision leaf on request {row.id}: {row.leaf_id!r}")
                slice_nodes = decision_graph.ancestor_slice(
                    nodes_by_id, row.leaf_id, max_depth=row.approved_depth
                )
                returned_ids = [n.id for n in slice_nodes]
                if existing_grant is None:
                    s.add(
                        DecisionHistoryGrant(
                            request_id=row.id,
                            issue_key=row.issue_key,
                            session_id=row.session_id,
                            kind=row.kind,
                            grant_case=row.grant_case,
                            leaf_id=row.leaf_id,
                            approved_depth=row.approved_depth,
                            returned_ids=returned_ids,
                            matched_approach="",
                            granted_at=stamp,
                        )
                    )
                    s.add(
                        EngineLog(
                            at=stamp,
                            kind="decision_history_grant",
                            message=(
                                f"Granted ancestors up to depth {row.approved_depth} on leaf #{row.leaf_id} "
                                f"(request #{row.id}, case={row.grant_case}, returned={returned_ids})"
                            ),
                        )
                    )
                return {
                    "status": "approved",
                    "request_id": row.id,
                    "kind": "ancestors",
                    "grant_case": row.grant_case,
                    "leaf_id": row.leaf_id,
                    "approved_depth": row.approved_depth,
                    "report_to_architect": row.report_to_architect,
                    "ancestors": [self._serialize_ancestor(n) for n in slice_nodes],
                }

            # row.kind == "was_rejected": return ONLY the matching rejected alternative and its reason; zero ancestors.
            hit = decision_graph.find_rejected_alternative(nodes_by_id, parents_by_node, row.approach)
            matched_alt = hit[1] if hit is not None else None
            if existing_grant is None:
                s.add(
                    DecisionHistoryGrant(
                        request_id=row.id,
                        issue_key=row.issue_key,
                        session_id=row.session_id,
                        kind=row.kind,
                        grant_case=row.grant_case,
                        leaf_id=None,
                        approved_depth=None,
                        returned_ids=[],
                        matched_approach=matched_alt.approach if matched_alt is not None else "",
                        granted_at=stamp,
                    )
                )
                s.add(
                    EngineLog(
                        at=stamp,
                        kind="decision_history_grant",
                        message=(
                            f"Granted narrow was_rejected check on {row.approach!r} "
                            f"(request #{row.id}, matched={matched_alt is not None})"
                        ),
                    )
                )
            return {
                "status": "approved",
                "request_id": row.id,
                "kind": "was_rejected",
                "grant_case": row.grant_case,
                "matched": matched_alt is not None,
                "rejected_alternative": (
                    {"approach": matched_alt.approach, "reason": matched_alt.reason}
                    if matched_alt is not None
                    else None
                ),
            }

    def history_grants(self) -> list[DecisionHistoryGrant]:
        return self._all(select(DecisionHistoryGrant).order_by(DecisionHistoryGrant.id))

    def _all(self, stmt: Any) -> list[Any]:
        with self.session(write=False) as s:
            rows = s.scalars(stmt).all()
            s.expunge_all()
            return list(rows)
