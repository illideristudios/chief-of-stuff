"""Workspace log rows, which have no issue to belong to.

Revision ID: 0007
Revises: 0006
"""
import sqlalchemy as sa
from alembic import op

revision = "0007"
down_revision = "0006"


def upgrade() -> None:
    op.create_table(
        "engine_log",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("at", sa.String(), nullable=False),
        sa.Column("message", sa.String(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False, server_default=""),
    )
