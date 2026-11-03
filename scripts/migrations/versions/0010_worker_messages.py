"""Persist operator messages for a particular worker attempt.

Revision ID: 0010
Revises: 0009
"""
import sqlalchemy as sa
from alembic import op

revision = "0010"
down_revision = "0009"


def upgrade() -> None:
    op.create_table(
        "worker_message",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("issue_key", sa.String(), sa.ForeignKey("work_item.issue_key", ondelete="CASCADE"), nullable=False),
        sa.Column("stage", sa.String(), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("session_id", sa.String(), nullable=False),
        sa.Column("request_id", sa.String(), nullable=False, unique=True),
        sa.Column("body", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("evidence", sa.String(), nullable=False),
        sa.Column("delivery_id", sa.String(), nullable=True, unique=True),
        sa.Column("created_at", sa.String(), nullable=False),
        sa.Column("claimed_at", sa.String(), nullable=True),
        sa.Column("resolved_at", sa.String(), nullable=True),
        sa.CheckConstraint("status IN ('queued', 'sending', 'delivered', 'failed')", name="worker_message_status"),
    )
