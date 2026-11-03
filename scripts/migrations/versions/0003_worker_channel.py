"""Environment-bound worker reports and asks.

Revision ID: 0003
Revises: 0002
"""
import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"


def upgrade() -> None:
    for name, fields in (
        ("report", [sa.Column("outcome", sa.String(), nullable=False),
                    sa.Column("summary", sa.String(), nullable=False),
                    sa.Column("findings", sa.JSON(), nullable=False)]),
        ("ask", [sa.Column("question", sa.String(), nullable=False),
                 sa.Column("options", sa.JSON(), nullable=False),
                 sa.Column("context", sa.String(), nullable=False),
                 sa.Column("answer", sa.String())]),
    ):
        op.create_table(
            name,
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("issue_key", sa.String(), sa.ForeignKey("work_item.issue_key", ondelete="CASCADE"), nullable=False),
            sa.Column("stage", sa.String(), nullable=False),
            sa.Column("attempt", sa.Integer(), nullable=False),
            sa.Column("session_id", sa.String(), nullable=False),
            sa.Column("created_at", sa.String(), nullable=False),
            *fields,
        )
