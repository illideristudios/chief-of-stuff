"""Keep tree teardown attempts separate from issue resume documents.

Revision ID: 0013
Revises: 0012
"""
import sqlalchemy as sa
from alembic import op

revision = "0013"
down_revision = "0012"


def upgrade() -> None:
    op.create_table(
        "cleanup_teardown",
        sa.Column("tree", sa.String(), primary_key=True),
        sa.Column("identity", sa.JSON(), nullable=False),
    )
