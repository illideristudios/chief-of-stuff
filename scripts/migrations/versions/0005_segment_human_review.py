"""Retain a segment's human-review history after completion is accepted.

Revision ID: 0005
Revises: 0004
"""
import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"


def upgrade() -> None:
    op.add_column("segment", sa.Column("required_human_review", sa.Boolean(), nullable=False,
                                       server_default=sa.false()))
    op.execute("UPDATE segment SET required_human_review = 1 WHERE outcome = 'human_review'")
