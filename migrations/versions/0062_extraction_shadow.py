"""claim detection's shadow sample: the ``extraction_shadow`` table (#148)

FR-OBS-4, FR-VERIFY-13 (docs/02 §4.9): ``orchestrator.verify.extraction``'s
``extraction_shadow`` job writes it; ``core.tables.detection`` describes it.

- ``extraction_shadow`` is created: an id, the story, the derived-record columns (the sampled
  turn's operation), ``status`` (``sampled`` or ``failed``, CHECK), ``extraction_claims``,
  ``missed`` (spans, ids and shape hashes, never text), ``missed_count`` and ``created_at``,
  with the two derived indexes and ``(story_id, created_at)`` and ``created_at`` indexes.

The two new deployment settings (``extraction_shadow_sample_rate``, ``protected_index_max``)
are ``deployment_state.settings`` keys read with their defaults, so nothing is stored for them.

Downgrade drops the table.

Revision ID: 0062
Revises: 0061
Create Date: 2026-10-17

"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0062"
down_revision: str | None = "0061"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ULID = sa.CHAR(26)


def _derived() -> list[sa.Column[Any]]:
    return [
        sa.Column("operation_id", ULID, nullable=False),
        sa.Column(
            "source_event_ids",
            postgresql.ARRAY(ULID),
            server_default=sa.text("'{}'"),
            nullable=False,
        ),
        sa.Column("narrative_turn", sa.Integer(), nullable=True),
        sa.Column("turn_variant", sa.SmallInteger(), nullable=True),
        sa.Column(
            "membership",
            postgresql.ENUM(name="membership", create_type=False),
            server_default=sa.text("'active'"),
            nullable=False,
        ),
        sa.Column("membership_tx", sa.TIMESTAMP(timezone=True), nullable=False),
    ]


def upgrade() -> None:
    op.create_table(
        "extraction_shadow",
        sa.Column("id", ULID, nullable=False),
        sa.Column("story_id", ULID, nullable=False),
        *_derived(),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("extraction_claims", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "missed",
            postgresql.JSONB(),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("missed_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("status IN ('sampled', 'failed')", name="status"),
        sa.ForeignKeyConstraint(
            ["operation_id"],
            ["operations.operation_id"],
            name="fk_extraction_shadow_operation_id_operations",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_extraction_shadow"),
    )
    op.create_index(
        "ix_extraction_shadow_story_operation", "extraction_shadow", ["story_id", "operation_id"]
    )
    op.create_index(
        "ix_extraction_shadow_story_membership", "extraction_shadow", ["story_id", "membership"]
    )
    op.create_index(
        "ix_extraction_shadow_story_created", "extraction_shadow", ["story_id", "created_at"]
    )
    op.create_index("ix_extraction_shadow_created", "extraction_shadow", ["created_at"])


def downgrade() -> None:
    op.drop_table("extraction_shadow")
