"""Withdraw every open `scope` decision: §8 has none, and nothing else would ever close one (#1039, C29).

Decision rows are append-only and an identity's latest row wins, so each open one gets a closing row appended, as
decision_actions.answer records an answer; history is never changed.

Revision ID: 0008
Revises: 0007
"""
from datetime import datetime, timezone

import sqlalchemy as sa
from alembic import op

revision = "0008"
down_revision = "0007"

WITHDRAWN = "withdrawn: §8 has no scope decisions (#1039)"
# decision_actions._LIFECYCLE: fields a closing row sets afresh rather than carries from the row it follows.
_LIFECYCLE = ("status", "answered_at", "blocked_reason", "rerouted_to", "intent", "intent_id", "superseded_by", "note",
              "comment")

decision = sa.table("decision", sa.column("id", sa.Integer), sa.column("issue_key", sa.String),
                    sa.column("topic", sa.String), sa.column("question", sa.String), sa.column("answer", sa.String),
                    sa.column("decided_by", sa.String), sa.column("record", sa.JSON), sa.column("created_at", sa.String))


def _identity(row) -> tuple:
    record = row.record if isinstance(row.record, dict) else {}
    found = record.get("decision") if isinstance(record.get("decision"), dict) else {}
    return row.issue_key, row.question, found.get("instance", "")


def _open(row) -> bool:
    record = row.record if isinstance(row.record, dict) else {}
    status = record.get("status") or ("answered" if (row.answer or "").strip() else "open")
    return status in ("open", "blocked") and not record.get("intent")


def upgrade() -> None:
    conn = op.get_bind()
    rows = conn.execute(sa.select(decision).where(decision.c.topic == "scope").order_by(decision.c.id)).all()
    latest = {_identity(row): row for row in rows}
    now = datetime.now(timezone.utc).isoformat()
    for row in latest.values():
        if not _open(row):
            continue
        record = {k: v for k, v in (row.record if isinstance(row.record, dict) else {}).items() if k not in _LIFECYCLE}
        conn.execute(sa.insert(decision).values(
            issue_key=row.issue_key, topic=row.topic, question=row.question, answer=WITHDRAWN, decided_by="",
            record={**record, "status": "answered", "answered_at": now}, created_at=now))
