"""character bench: ``character_bench``, a story character's bench state per version (#831)

FR-MULTI-13: ``core.tables.bench`` describes the table. One row per version of a character's
bench state (a bench in a mode, or a reactivation), positioned at the inter-turn slot of the
operation that wrote it, with the derived-record columns of ``core.membership``. ``mode`` is
``sequestered`` or ``present`` (#832 ships ``present``). No foreign key to another story table.

Downgrade drops the table.

Revision ID: 0150
Revises: 0149
Create Date: 2026-11-10

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0150"
down_revision: str | None = "0149"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ULID = sa.CHAR(26)
TABLE = "character_bench"


def upgrade() -> None:
    op.create_table(
        TABLE,
        sa.Column("id", ULID, nullable=False),
        sa.Column("story_id", ULID, nullable=False),
        sa.Column("character_id", ULID, nullable=False),
        sa.Column("mode", sa.Text(), nullable=False),
        sa.Column("benched", sa.Boolean(), nullable=False),
        sa.Column("previous_role", sa.Text(), nullable=True),
        sa.Column("previous_fixed", sa.Boolean(), nullable=True),
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
        sa.Column("slot_k", sa.SmallInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("beat", sa.SmallInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("step", sa.SmallInteger(), server_default=sa.text("0"), nullable=False),
        sa.CheckConstraint(
            "narrative_turn IS NOT NULL AND turn_variant IS NOT NULL",
            name="ck_character_bench_positioned",
        ),
        sa.CheckConstraint("mode IN ('sequestered', 'present')", name="ck_character_bench_mode"),
        sa.ForeignKeyConstraint(
            ["operation_id"],
            ["operations.operation_id"],
            name="fk_character_bench_operation_id_operations",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_character_bench"),
    )
    op.create_index(f"ix_{TABLE}_story_operation", TABLE, ["story_id", "operation_id"])
    op.create_index(f"ix_{TABLE}_story_membership", TABLE, ["story_id", "membership"])
    op.create_index(f"ix_{TABLE}_story_character", TABLE, ["story_id", "character_id"])


def downgrade() -> None:
    op.drop_table(TABLE)
