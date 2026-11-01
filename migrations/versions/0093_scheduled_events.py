"""scheduled events: ``scheduled_events``, its status enum, and the stopped and paused advances
(#196)

FR-CLOCK-9,16,18 (docs/02 §7.1): ``core.tables.scheduled_events`` describes the table.

- The enum type ``scheduled_status`` (``pending``, ``possibly_due``, ``due``, ``surfaced``,
  ``completed``, ``missed``, ``cancelled``) is created.
- ``scheduled_events`` is created: one row per version of a canon scheduled event (its key the
  canon ``scheduled`` proposition, ``proposition_id``, a plain column), positioned, with the
  derived-record columns of ``core.membership`` (the operation that wrote the version). No
  foreign key to another story table.
- ``state_changes`` gains ``stopped_by_event_id`` (a clock advance a due event stopped, #196)
  and ``paused_for_event_id`` (one a ``possibly_due`` event paused, #197), both nullable.
- The core predicate ``scheduled`` (``01JPRED0000000000000000026``) names ``scheduled_events``
  as its ``projection`` (0052's pattern), as ``registry.sync_core`` would write it at the next
  start: the link from canon to the table (nothing rebuilds the table from facts).

Downgrade drops the table, the enum type and the two columns, and clears the projection.

Revision ID: 0093
Revises: 0092
Create Date: 2026-10-31

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0093"
down_revision: str | None = "0092"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ULID = sa.CHAR(26)
STATUS = ("pending", "possibly_due", "due", "surfaced", "completed", "missed", "cancelled")
SCHEDULED = "01JPRED0000000000000000026"
TABLE = "scheduled_events"


def _enum(name: str) -> postgresql.ENUM:
    return postgresql.ENUM(name=name, create_type=False)


def upgrade() -> None:
    postgresql.ENUM(*STATUS, name="scheduled_status").create(op.get_bind())
    op.create_table(
        TABLE,
        sa.Column("id", ULID, nullable=False),
        sa.Column("story_id", ULID, nullable=False),
        sa.Column("proposition_id", ULID, nullable=False),
        sa.Column("planned_offset_minutes", sa.BigInteger(), nullable=False),
        sa.Column("planned_anchor", postgresql.JSONB(), nullable=True),
        sa.Column("status", _enum("scheduled_status"), nullable=False),
        sa.Column("owner", sa.Text(), nullable=False),
        sa.Column(
            "participants", postgresql.ARRAY(ULID), server_default=sa.text("'{}'"), nullable=False
        ),
        sa.Column("place_id", ULID, nullable=True),
        sa.Column("grace_minutes", sa.Integer(), nullable=False),
        sa.Column("refinement_pause_anchor_rev", sa.Integer(), nullable=True),
        sa.Column("stopped_clock_at", sa.BigInteger(), nullable=True),
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
            "membership", _enum("membership"), server_default=sa.text("'active'"), nullable=False
        ),
        sa.Column("membership_tx", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("slot_k", sa.SmallInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("beat", sa.SmallInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("step", sa.SmallInteger(), server_default=sa.text("0"), nullable=False),
        sa.CheckConstraint(
            "narrative_turn IS NOT NULL AND turn_variant IS NOT NULL",
            name="ck_scheduled_events_positioned",
        ),
        sa.CheckConstraint("grace_minutes >= 0", name="ck_scheduled_events_grace"),
        sa.CheckConstraint("planned_offset_minutes >= 0", name="ck_scheduled_events_planned"),
        sa.CheckConstraint("char_length(owner) BETWEEN 1 AND 26", name="ck_scheduled_events_owner"),
        sa.ForeignKeyConstraint(
            ["operation_id"],
            ["operations.operation_id"],
            name="fk_scheduled_events_operation_id_operations",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_scheduled_events"),
    )
    op.create_index(f"ix_{TABLE}_story_operation", TABLE, ["story_id", "operation_id"])
    op.create_index(f"ix_{TABLE}_story_membership", TABLE, ["story_id", "membership"])
    op.create_index(f"ix_{TABLE}_story_proposition", TABLE, ["story_id", "proposition_id"])
    op.add_column("state_changes", sa.Column("stopped_by_event_id", ULID, nullable=True))
    op.add_column("state_changes", sa.Column("paused_for_event_id", ULID, nullable=True))
    op.execute(
        sa.text("UPDATE predicates SET projection = :p WHERE id = :id").bindparams(
            p=TABLE, id=SCHEDULED
        )
    )


def downgrade() -> None:
    op.execute(
        sa.text("UPDATE predicates SET projection = NULL WHERE id = :id").bindparams(id=SCHEDULED)
    )
    op.drop_column("state_changes", "paused_for_event_id")
    op.drop_column("state_changes", "stopped_by_event_id")
    op.drop_table(TABLE)
    postgresql.ENUM(name="scheduled_status").drop(op.get_bind())
