"""client_events: ingest's dedupe record, and each operation's origin (#58)

Revision ID: 0012
Revises: 0011
Create Date: 2026-10-11

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ORIGIN_CHECK = "origin IN ('generated', 'ingested')"


def upgrade() -> None:
    # Written once at ``begin()``: ``ingested`` for a consumer's ``ingest``, else ``generated``.
    op.add_column(
        "operations",
        sa.Column("origin", sa.Text(), server_default=sa.text("'generated'"), nullable=False),
    )
    op.create_check_constraint("ck_operations_origin", "operations", ORIGIN_CHECK)
    # (story_id, client_event_id) is unique: the primary key. Rows live as long as the story.
    op.create_table(
        "client_events",
        sa.Column("story_id", sa.CHAR(26), nullable=False),
        sa.Column("client_event_id", sa.Text(), nullable=False),
        sa.Column("payload_hash", sa.CHAR(64), nullable=False),
        sa.Column("operation_id", sa.CHAR(26), nullable=False),
        sa.Column("result", postgresql.JSONB(), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("transaction_timestamp()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("story_id", "client_event_id", name="pk_client_events"),
    )


def downgrade() -> None:
    op.drop_table("client_events")
    op.drop_constraint("ck_operations_origin", "operations", type_="check")
    op.drop_column("operations", "origin")
