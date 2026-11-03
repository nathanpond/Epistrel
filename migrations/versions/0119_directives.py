"""directives: an author's directions to a story (#226)

FR-DIRECT-1,2,6: ``core.tables.directives`` describes the table.

Revision ID: 0119
Revises: 0118
Create Date: 2026-11-02

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0119"
down_revision: str | None = "0118"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ULID = sa.CHAR(26)
DISPOSITIONS = (
    "steer_queued",
    "applied",
    "routed_to_rewrite",
    "routed_write",
    "directed_regeneration",
    "expired",
)


def upgrade() -> None:
    op.create_table(
        "directives",
        sa.Column("id", ULID, primary_key=True),
        sa.Column("story_id", ULID, nullable=False),
        sa.Column("principal", sa.Text, nullable=False),
        sa.Column("text", sa.Text, nullable=False),
        sa.Column("kind", sa.Text, nullable=False),
        sa.Column("classified_by", sa.Text, nullable=False),
        sa.Column("disposition", sa.Text, nullable=False),
        sa.Column("temporal_scope", sa.Text, nullable=False, server_default=sa.text("'now'")),
        sa.Column("changes", postgresql.JSONB, nullable=False, server_default=sa.text("'[]'")),
        sa.Column("operation_id", ULID, nullable=True),
        sa.Column("rewrite_session_id", ULID, nullable=True),
        sa.Column("consumed_by_operation_id", ULID, nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("transaction_timestamp()"),
        ),
        sa.CheckConstraint("kind IN ('ephemeral', 'durable')", name="ck_directives_kind"),
        sa.CheckConstraint(
            "classified_by IN ('model', 'request')", name="ck_directives_classified_by"
        ),
        sa.CheckConstraint(
            "disposition IN (" + ", ".join(repr(d) for d in DISPOSITIONS) + ")",
            name="ck_directives_disposition",
        ),
        sa.CheckConstraint(
            "temporal_scope IN ('now', 'past')", name="ck_directives_temporal_scope"
        ),
    )
    op.create_index("ix_directives_story", "directives", ["story_id", "created_at"])
    op.create_index(
        "ix_directives_queued",
        "directives",
        ["story_id"],
        postgresql_where=sa.text(
            "disposition = 'steer_queued' AND consumed_by_operation_id IS NULL"
        ),
    )


def downgrade() -> None:
    op.drop_table("directives")
