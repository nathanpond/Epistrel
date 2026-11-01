"""External values: the ``external_values`` projection of ``external_read`` events (#205)

FR-EXT-2, FR-STORE-17,31 (docs/02 §7.1): ``core.tables.states`` describes the table.

- The enum type ``external_value_source`` (``read``, ``last_known``, ``unknown``,
  ``confirmation``; #206 and #215 write the last three) is created.
- ``external_values`` is created: one row per recorded version of an externally owned State's
  value, keyed by its ``external_read`` event, positioned where it was read (the position
  group ``position_*``), with the State, its scope key (``''`` for a story-scoped State), the
  version, the value (``NULL``: unknown) and its source. A rebuildable projection: no foreign
  key (no plain foreign keys between story tables), no story content.

Downgrade drops the table and the enum type.

Revision ID: 0090
Revises: 0089
Create Date: 2026-10-31

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0090"
down_revision: str | None = "0089"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ULID = sa.CHAR(26)
SOURCES = ("read", "last_known", "unknown", "confirmation")
POSITION = ("turn", "variant", "slot_k", "beat", "step")


def upgrade() -> None:
    postgresql.ENUM(*SOURCES, name="external_value_source").create(op.get_bind())
    op.create_table(
        "external_values",
        sa.Column("event_id", ULID, nullable=False),
        sa.Column("story_id", ULID, nullable=False),
        sa.Column("operation_id", ULID, nullable=False),
        sa.Column("state_id", ULID, nullable=False),
        sa.Column("scope_key", sa.Text(), server_default=sa.text("''"), nullable=False),
        sa.Column("position_turn", sa.Integer(), nullable=False),
        *(
            sa.Column(
                f"position_{name}", sa.SmallInteger(), server_default=sa.text("0"), nullable=False
            )
            for name in POSITION[1:]
        ),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("value", postgresql.JSONB(none_as_null=True), nullable=True),
        sa.Column(
            "source",
            postgresql.ENUM(name="external_value_source", create_type=False),
            nullable=False,
        ),
        sa.CheckConstraint("version > 0", name="ck_external_values_version"),
        sa.PrimaryKeyConstraint("event_id", name="pk_external_values"),
    )
    op.create_index(
        "ix_external_values_story_state_scope_position",
        "external_values",
        [
            "story_id",
            "state_id",
            "scope_key",
            "position_turn",
            "position_slot_k",
            "position_beat",
            "position_step",
        ],
    )


def downgrade() -> None:
    op.drop_table("external_values")
    postgresql.ENUM(name="external_value_source").drop(op.get_bind())
