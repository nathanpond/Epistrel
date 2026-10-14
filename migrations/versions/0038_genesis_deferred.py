"""genesis deferred: what a story's genesis could not use yet (#122)

- ``genesis_deferred`` (FR-SCN-9,11; docs/02 §4.10): one row per scenario item a story's
  pipeline cannot use (a seed's structured form, a relationship, a location or entity, a story
  rule, a State tracker, the starting clock, a scheduled event), naming its field, class,
  location, the capability it needs and its original JSON, written in the genesis transaction.
  A story table with no foreign key (story deletion enumerates it, a fork copies it).

Downgrade drops it.

Revision ID: 0038
Revises: 0037
Create Date: 2026-10-14

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0038"
down_revision: str | None = "0037"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ULID = sa.CHAR(26)


def upgrade() -> None:
    op.create_table(
        "genesis_deferred",
        sa.Column("id", ULID, nullable=False),
        sa.Column("story_id", ULID, nullable=False),
        sa.Column("operation_id", ULID, nullable=False),
        sa.Column("field_ref", sa.Text(), nullable=False),
        sa.Column("field_class", sa.Text(), nullable=False),
        sa.Column("location", postgresql.JSONB(), nullable=False),
        sa.Column("capability_needed", sa.Text(), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("transaction_timestamp()"),
            nullable=False,
        ),
        sa.Column("materialized_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_genesis_deferred"),
    )
    op.create_index("ix_genesis_deferred_story_id", "genesis_deferred", ["story_id"])


def downgrade() -> None:
    op.drop_index("ix_genesis_deferred_story_id", table_name="genesis_deferred")
    op.drop_table("genesis_deferred")
