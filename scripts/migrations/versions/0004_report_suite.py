"""Observed suite counts on a worker report.

Revision ID: 0004
Revises: 0003
"""
import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"


def upgrade() -> None:
    op.add_column("report", sa.Column("suite", sa.JSON(), nullable=True))
