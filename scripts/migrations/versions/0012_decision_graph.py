"""Decision graph nodes, provenance edges, ancestor-only rejected alternatives, history requests and grants.

Revision ID: 0012
Revises: 0011
"""
import sqlalchemy as sa
from alembic import op

revision = "0012"
down_revision = "0011"


def upgrade() -> None:
    op.create_table(
        "decision_graph_node",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("topic", sa.String(), nullable=False),
        sa.Column("decision", sa.String(), nullable=False),
        sa.Column("reason", sa.String(), nullable=False),
        sa.Column("decided_by", sa.String(), nullable=False),
        sa.Column("issue_key", sa.String(), nullable=False),
        sa.Column("created_at", sa.String(), nullable=False),
    )
    op.create_table(
        "decision_graph_edge",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("child_id", sa.Integer(), sa.ForeignKey("decision_graph_node.id", ondelete="CASCADE"), nullable=False),
        sa.Column("parent_id", sa.Integer(), sa.ForeignKey("decision_graph_node.id", ondelete="CASCADE"), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.UniqueConstraint("child_id", "parent_id"),
        sa.CheckConstraint("child_id != parent_id", name="decision_graph_no_self_edge"),
    )
    op.create_table(
        "decision_graph_rejected",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("ancestor_id", sa.Integer(), sa.ForeignKey("decision_graph_node.id", ondelete="CASCADE"), nullable=False),
        sa.Column("approach", sa.String(), nullable=False),
        sa.Column("reason", sa.String(), nullable=False),
        sa.Column("created_at", sa.String(), nullable=False),
    )
    op.create_table(
        "decision_history_request",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("issue_key", sa.String(), nullable=False),
        sa.Column("stage", sa.String(), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("session_id", sa.String(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("leaf_id", sa.Integer(), sa.ForeignKey("decision_graph_node.id", ondelete="SET NULL"), nullable=True),
        sa.Column("other_leaf_id", sa.Integer(), sa.ForeignKey("decision_graph_node.id", ondelete="SET NULL"), nullable=True),
        sa.Column("requested_depth", sa.Integer(), nullable=False),
        sa.Column("approach", sa.String(), nullable=False),
        sa.Column("justification", sa.String(), nullable=False),
        sa.Column("evidence", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("grant_case", sa.String(), nullable=False),
        sa.Column("verdict_reason", sa.String(), nullable=False),
        sa.Column("approved_depth", sa.Integer(), nullable=True),
        sa.Column("report_to_architect", sa.Boolean(), nullable=False),
        sa.Column("decided_by", sa.String(), nullable=False),
        sa.Column("created_at", sa.String(), nullable=False),
        sa.Column("decided_at", sa.String(), nullable=True),
        sa.CheckConstraint("kind IN ('ancestors', 'was_rejected')", name="decision_history_request_kind"),
        sa.CheckConstraint("status IN ('pending', 'approved', 'denied')", name="decision_history_request_status"),
    )
    op.create_table(
        "decision_history_grant",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("request_id", sa.Integer(), sa.ForeignKey("decision_history_request.id", ondelete="CASCADE"),
                  nullable=False, unique=True),
        sa.Column("issue_key", sa.String(), nullable=False),
        sa.Column("session_id", sa.String(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("grant_case", sa.String(), nullable=False),
        sa.Column("leaf_id", sa.Integer(), nullable=True),
        sa.Column("approved_depth", sa.Integer(), nullable=True),
        sa.Column("returned_ids", sa.JSON(), nullable=False),
        sa.Column("matched_approach", sa.String(), nullable=False),
        sa.Column("granted_at", sa.String(), nullable=False),
    )
