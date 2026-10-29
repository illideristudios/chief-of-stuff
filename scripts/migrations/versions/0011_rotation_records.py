"""Persist rotation turns and each attempt's workflow.

Revision ID: 0011
Revises: 0010
"""
import sqlalchemy as sa
from alembic import op

revision = "0011"
down_revision = "0010"


def upgrade() -> None:
    op.create_table("rotation_cursor", sa.Column("task_class", sa.String(), primary_key=True),
                    sa.Column("entry", sa.String(), nullable=False))
    op.add_column("segment", sa.Column("workflow", sa.String(), nullable=False, server_default=""))
