"""Keep tree teardown attempts separate from issue resume documents.

Revision ID: 0014
Revises: 0013
"""
import sqlalchemy as sa
from alembic import op

revision = "0014"
down_revision = "0013"


def upgrade() -> None:
    op.create_table(
        "cleanup_teardown",
        sa.Column("tree", sa.String(), primary_key=True),
        sa.Column("identity", sa.JSON(), nullable=False),
    )
