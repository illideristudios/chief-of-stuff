"""Retain a bounded event buffer and source checkpoint for each worker attempt.

Revision ID: 0013
Revises: 0012
"""
import sqlalchemy as sa
from alembic import op

revision = "0013"
down_revision = "0012"


def upgrade() -> None:
    op.create_table(
        "transcript",
        sa.Column("segment_id", sa.Integer(), sa.ForeignKey("segment.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("session_id", sa.String(), nullable=False, unique=True),
        sa.Column("source", sa.String(), nullable=True, unique=True),
        sa.Column("state", sa.JSON(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("transcript")
