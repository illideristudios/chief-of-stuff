"""The issue-keyed store: work items, segments, decisions, ownership, log, resume.

Revision ID: 0001
Revises:
"""
import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None


def upgrade() -> None:
    op.create_table(
        "work_item",
        sa.Column("issue_key", sa.String(), primary_key=True),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("stage", sa.String(), nullable=False),
        sa.Column("forge_state", sa.String(), nullable=False),
        sa.Column("review_state", sa.String(), nullable=False),
        sa.Column("merge_state", sa.String(), nullable=False),
        sa.Column("human_review", sa.Boolean(), nullable=False),
        sa.Column("updated_at", sa.String(), nullable=False),
    )
    op.create_table(
        "segment",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("issue_key", sa.String(), sa.ForeignKey("work_item.issue_key", ondelete="CASCADE"), nullable=False),
        sa.Column("stage", sa.String(), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("started_at", sa.String(), nullable=False),
        sa.Column("ended_at", sa.String()),
        sa.Column("outcome", sa.String()),
        sa.UniqueConstraint("issue_key", "stage", "attempt"),
    )
    op.create_table(
        "decision",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("issue_key", sa.String(), sa.ForeignKey("work_item.issue_key", ondelete="CASCADE"), nullable=False),
        sa.Column("topic", sa.String(), nullable=False),
        sa.Column("question", sa.String(), nullable=False),
        sa.Column("answer", sa.String()),
        sa.Column("decided_by", sa.String(), nullable=False),
        sa.Column("record", sa.JSON()),
        sa.Column("created_at", sa.String(), nullable=False),
    )
    op.create_table(
        "ownership",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("issue_key", sa.String(), sa.ForeignKey("work_item.issue_key", ondelete="CASCADE"), nullable=False),
        sa.Column("path", sa.String(), nullable=False),
        sa.Column("mode", sa.String(), nullable=False),
        sa.UniqueConstraint("issue_key", "path"),
    )
    op.create_table(
        "log_entry",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("issue_key", sa.String(), sa.ForeignKey("work_item.issue_key", ondelete="CASCADE"), nullable=False),
        sa.Column("at", sa.String(), nullable=False),
        sa.Column("message", sa.String(), nullable=False),
    )
    op.create_table(
        "resume",
        sa.Column("issue_key", sa.String(), sa.ForeignKey("work_item.issue_key", ondelete="CASCADE"), primary_key=True),
        sa.Column("data", sa.JSON(), nullable=False),
    )
