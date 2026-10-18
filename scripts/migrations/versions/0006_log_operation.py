"""The operation a log line reports, so recovery never reads the line's wording.

Revision ID: 0006
Revises: 0005
"""
import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"


# Only known operation prefixes are recoverable. Ambiguous history stays unknown.
_BY_WORDING = (
    ("stage write failed:%", "stage_write"),
    ("stage write refused:%", "stage_write"),
    ("decision comment failed:%", "decision_comment"),
    ("merge refused:%", "merge_refused"),
    ("merge failed:%", "merge_refused"),
    ("merged %", "merge"),
    ("dispatch failed:%", "worker_run"),
    ("review failed:%", "worker_run"),
    ("worker % attempt %: failed%", "worker_run"),
    ("escalate: retry limit%", "worker_run"),
)


def upgrade() -> None:
    op.add_column("log_entry", sa.Column("kind", sa.String(), nullable=False, server_default=""))
    log = sa.table("log_entry", sa.column("message", sa.String), sa.column("kind", sa.String))
    for pattern, kind in _BY_WORDING:
        op.execute(sa.update(log).where(log.c.kind == "", log.c.message.like(pattern)).values(kind=kind))
    op.execute(sa.update(log).where(log.c.kind == "").values(kind="unknown"))
