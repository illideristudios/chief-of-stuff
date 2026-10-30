"""Drop the `ownership` table: the engine has no authorized files (#1039, C29).

Its only rows that still mean anything are a worker's tree registrations, which cleanup reads before it removes a
tree; they move to `worktree`. Older workers registered absolute tree paths as write rows, so those move too.
Declared file paths are repository-relative and are dropped with the table.

Revision ID: 0009
Revises: 0008
"""
import sqlalchemy as sa
from alembic import op

revision = "0009"
down_revision = "0008"

ownership = sa.table("ownership", sa.column("issue_key", sa.String), sa.column("path", sa.String),
                     sa.column("mode", sa.String))


def upgrade() -> None:
    worktree = op.create_table(
        "worktree",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("issue_key", sa.String(), sa.ForeignKey("work_item.issue_key", ondelete="CASCADE"), nullable=False),
        sa.Column("path", sa.String(), nullable=False),
        sa.UniqueConstraint("issue_key", "path"),
    )
    conn = op.get_bind()
    rows = conn.execute(sa.select(ownership.c.issue_key, ownership.c.path)
                        .where(sa.or_(ownership.c.mode == "tree", ownership.c.path.like("/%")))).all()
    if rows:
        conn.execute(sa.insert(worktree), [{"issue_key": key, "path": path} for key, path in rows])
    op.drop_table("ownership")
