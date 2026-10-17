"""The operation a log line reports, so recovery never reads the line's wording.

Revision ID: 0004
Revises: 0003
"""
import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"


# Old lines name their operation only in their wording; the first match wins, so refusals precede the generic failure.
_BY_WORDING = (
    ("stage write%", "stage_write"),
    ("%comment failed%", "decision_comment"),
    ("merge refused:%", "merge_refused"),
    ("merged %", "merge"),
)
_WORKER_RUN = ("%failed%", "%refused%", "escalate: retry limit%")


def upgrade() -> None:
    op.add_column("log_entry", sa.Column("kind", sa.String(), nullable=False, server_default=""))
    log = sa.table("log_entry", sa.column("message", sa.String), sa.column("kind", sa.String))
    for pattern, kind in _BY_WORDING:
        op.execute(sa.update(log).where(log.c.kind == "", log.c.message.like(pattern)).values(kind=kind))
    op.execute(sa.update(log).where(log.c.kind == "", sa.or_(*(log.c.message.like(p) for p in _WORKER_RUN)))
               .values(kind="worker_run"))
