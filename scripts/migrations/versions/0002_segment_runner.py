"""Who ran a stage segment: runtime, model, effort, agent_type.

Revision ID: 0002
Revises: 0001
"""
import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"


def upgrade() -> None:
    op.add_column("segment", sa.Column("runtime", sa.String(), nullable=False, server_default=""))
    op.add_column("segment", sa.Column("model", sa.String(), nullable=False, server_default=""))
    op.add_column("segment", sa.Column("effort", sa.String(), nullable=False, server_default=""))
    op.add_column("segment", sa.Column("agent_type", sa.String(), nullable=False, server_default=""))
