"""Review identity, pinned head and per-finding fix identity.

Revision ID: 0006
Revises: 0005
"""
import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"


def upgrade() -> None:
    for name in ("persona", "reviewed_head", "finding_key"):
        op.add_column("segment", sa.Column(name, sa.String(), nullable=False, server_default=""))
    op.add_column("segment", sa.Column("review_round", sa.Integer(), nullable=False, server_default="0"))
