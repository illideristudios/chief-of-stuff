"""The operation a log line reports, so recovery never reads the line's wording.

Revision ID: 0004
Revises: 0003
"""
import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"


def upgrade() -> None:
    op.add_column("log_entry", sa.Column("kind", sa.String(), nullable=False, server_default=""))
