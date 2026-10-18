"""Account-wide runtime limits. Independent of the workspace store's revisions."""
from alembic import op
import sqlalchemy as sa

revision = "limits_0001"
down_revision = None


def upgrade() -> None:
    op.create_table("limit_reading",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("runtime", sa.String(), nullable=False),
        sa.Column("account", sa.String(), nullable=False),
        sa.Column("limit", sa.String(), nullable=False),
        sa.Column("used", sa.Float(), nullable=True),
        sa.Column("cap", sa.Float(), nullable=True),
        sa.Column("unit", sa.String(), nullable=False),
        sa.Column("resets_at", sa.String(), nullable=True),
        sa.Column("read_at", sa.String(), nullable=False),
        sa.Column("unknown", sa.String(), nullable=False),
        sa.UniqueConstraint("runtime", "account", "limit", "read_at"))
    op.create_index("ix_limit_reading_runtime", "limit_reading", ["runtime"])


def downgrade() -> None:
    op.drop_table("limit_reading")
