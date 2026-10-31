"""The world clock: a built-in ``clock`` State for every story, and events' story-time (#193)

FR-CLOCK-1,2,3,5,12 (docs/02 §4.4, §7.1): ``core.tables.states`` and ``core.tables.events``
describe the columns.

- ``state_type`` gains ``clock`` (in an autocommit block: a label added in a transaction cannot
  be used before it commits, and the backfill below uses it).
- The enum types ``clock_mode`` (``paced``, ``manual``, ``external``) and ``time_awareness``
  (``exact``, ``approximate``, ``none``) are created.
- ``state_definitions`` gains the clock's settings, one nullable column each (``mode``,
  ``anchor``, ``pacing_table``, ``max_unprompted_skip_minutes``, ``stop_at_due_events``,
  ``grace_minutes``, ``time_statement_tolerance_minutes``, ``elapsed_tolerance_factor``,
  ``time_awareness``, ``bands``, ``calendar``), set exactly for the ``clock`` State (CHECK
  ``ck_state_definitions_clock_settings``).
- ``state_changes`` gains ``estimate_meta`` (an estimated advance's estimate and reason, #197).
- ``events`` gains ``valid_time_minutes`` (the event's story-time; ``NULL`` for rows written
  before this revision).
- ``operations``' update guard (``epistrel_operations_guard``) lets ``slot_k`` change: an
  inter-turn slot's commit positions it (``core.world_clock.slots``), as a turn's sets its turn
  and variant.
- Every existing story gets its ``world_clock`` row (offset 0, unanchored, ``paced``) in its
  genesis operation, as ``core.stories`` writes one for a new story. A story pinned to a
  clock-off profile version keeps it inert.

Downgrade removes the clock rows, the columns and the two enum types, restores the guard, and
rebuilds ``state_type`` without ``clock`` (PostgreSQL cannot drop an enum label).

Revision ID: 0085
Revises: 0084
Create Date: 2026-10-31

"""

from __future__ import annotations

import json
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from epistrel.core.ids import new_ulid

revision: str = "0085"
down_revision: str | None = "0084"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

STATE_TYPES_BEFORE = ("number", "boolean", "enum", "text")
"""``state_type`` as 0084 created it."""

ENUMS: dict[str, tuple[str, ...]] = {
    "clock_mode": ("paced", "manual", "external"),
    "time_awareness": ("exact", "approximate", "none"),
}

CLOCK_SETTINGS_CHECK = (
    "(type = 'clock') = (mode IS NOT NULL) AND (type = 'clock' OR (anchor IS NULL "
    "AND pacing_table IS NULL AND max_unprompted_skip_minutes IS NULL "
    "AND stop_at_due_events IS NULL AND grace_minutes IS NULL "
    "AND time_statement_tolerance_minutes IS NULL AND elapsed_tolerance_factor IS NULL "
    "AND time_awareness IS NULL AND bands IS NULL AND calendar IS NULL))"
)

OPERATIONS_BEFORE = (
    "status",
    "membership",
    "membership_tx",
    "narrative_turn",
    "turn_variant",
    "base_revision",
    "read_set",
    "config_revisions",
    "committed_at",
    "attempts",
    "turn_report",
    "viewpoint",
    "timeline_revision",
    "error_code",
    "error",
    "parent_variant",
    "checked_guardrails",
    "stage_log",
)
OPERATIONS_AFTER = (*OPERATIONS_BEFORE, "slot_k")


def _operations_guard(columns: Sequence[str]) -> str:
    """The operations trigger function with ``columns`` as its allow-list (0039's body)."""
    array = ", ".join(f"'{c}'" for c in columns)
    return f"""
CREATE OR REPLACE FUNCTION epistrel_operations_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    mutable text[] := ARRAY[
        {array}
    ];
BEGIN
    IF (to_jsonb(NEW) - mutable) IS DISTINCT FROM (to_jsonb(OLD) - mutable) THEN
        RAISE EXCEPTION 'operation %: only % may change', OLD.operation_id, mutable
            USING ERRCODE = 'EPI02';
    END IF;
    RETURN NEW;
END
$$;
"""


PACING_TABLE = [[0, 5, 0], [5, 51, 1], [51, None, 2]]
BANDS = [
    ["night", "00:00", "05:00"],
    ["morning", "05:00", "12:00"],
    ["afternoon", "12:00", "17:00"],
    ["evening", "17:00", "21:00"],
    ["night", "21:00", "24:00"],
]

BACKFILL = sa.text(
    "INSERT INTO state_definitions (id, story_id, name, type, scope, constraints, "
    "initial_value, unit, visibility, owner, created_by, mode, pacing_table, "
    "max_unprompted_skip_minutes, stop_at_due_events, grace_minutes, "
    "time_statement_tolerance_minutes, elapsed_tolerance_factor, time_awareness, bands, "
    "calendar, operation_id, narrative_turn, turn_variant, membership, membership_tx) "
    "VALUES (:id, :story_id, 'world_clock', 'clock', 'story', "
    "'{\"non_negative\": true}'::jsonb, '0'::jsonb, 'minutes', 'player_visible', 'story', "
    "'genesis', 'paced', CAST(:pacing AS jsonb), 1440, true, 30, 15, 2, 'exact', "
    "CAST(:bands AS jsonb), 'gregorian', :operation_id, :turn, :variant, "
    "CAST(:membership AS membership), :membership_tx)"
)


def _enum(name: str) -> postgresql.ENUM:
    return postgresql.ENUM(name=name, create_type=False)


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE state_type ADD VALUE IF NOT EXISTS 'clock'")
    bind = op.get_bind()
    for name, labels in ENUMS.items():
        postgresql.ENUM(*labels, name=name).create(bind)

    op.add_column("state_definitions", sa.Column("mode", _enum("clock_mode"), nullable=True))
    for name in ("anchor", "pacing_table"):
        op.add_column(
            "state_definitions",
            sa.Column(name, postgresql.JSONB(none_as_null=True), nullable=True),
        )
    op.add_column(
        "state_definitions", sa.Column("max_unprompted_skip_minutes", sa.Integer(), nullable=True)
    )
    op.add_column("state_definitions", sa.Column("stop_at_due_events", sa.Boolean(), nullable=True))
    for name in ("grace_minutes", "time_statement_tolerance_minutes"):
        op.add_column("state_definitions", sa.Column(name, sa.Integer(), nullable=True))
    op.add_column(
        "state_definitions", sa.Column("elapsed_tolerance_factor", sa.Numeric(), nullable=True)
    )
    op.add_column(
        "state_definitions",
        sa.Column("time_awareness", _enum("time_awareness"), nullable=True),
    )
    op.add_column(
        "state_definitions",
        sa.Column("bands", postgresql.JSONB(none_as_null=True), nullable=True),
    )
    op.add_column("state_definitions", sa.Column("calendar", sa.Text(), nullable=True))
    op.create_check_constraint(
        "ck_state_definitions_clock_settings", "state_definitions", CLOCK_SETTINGS_CHECK
    )
    op.add_column(
        "state_changes",
        sa.Column("estimate_meta", postgresql.JSONB(none_as_null=True), nullable=True),
    )
    op.add_column("events", sa.Column("valid_time_minutes", sa.BigInteger(), nullable=True))
    op.execute(_operations_guard(OPERATIONS_AFTER))

    genesis = bind.execute(
        sa.text(
            "SELECT DISTINCT ON (o.story_id) o.story_id, o.operation_id, o.narrative_turn, "
            "o.turn_variant, o.membership::text AS membership, "
            "COALESCE(o.membership_tx, o.committed_at, transaction_timestamp()) AS membership_tx "
            "FROM operations o WHERE o.kind = 'genesis' AND o.status = 'committed' "
            "AND o.story_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM state_definitions d "
            "WHERE d.story_id = o.story_id AND d.name = 'world_clock') "
            "ORDER BY o.story_id, o.operation_id"
        )
    ).all()
    for row in genesis:
        bind.execute(
            BACKFILL,
            {
                "id": new_ulid(),
                "story_id": row.story_id,
                "pacing": json.dumps(PACING_TABLE),
                "bands": json.dumps(BANDS),
                "operation_id": row.operation_id,
                "turn": row.narrative_turn,
                "variant": row.turn_variant,
                "membership": row.membership,
                "membership_tx": row.membership_tx,
            },
        )


def downgrade() -> None:
    op.execute(
        "DELETE FROM state_values v USING state_definitions d WHERE v.state_id = d.id "
        "AND d.type = 'clock'"
    )
    # The append-only guard lets a DELETE through only under an erasure's setting.
    op.execute("SELECT set_config('epistrel.erasure', 'on', true)")
    op.execute(
        "DELETE FROM state_changes c USING state_definitions d WHERE c.state_id = d.id "
        "AND d.type = 'clock'"
    )
    op.execute("SELECT set_config('epistrel.erasure', 'off', true)")
    op.execute("DELETE FROM state_definitions WHERE type = 'clock'")
    op.execute(_operations_guard(OPERATIONS_BEFORE))
    op.drop_column("events", "valid_time_minutes")
    op.drop_column("state_changes", "estimate_meta")
    op.drop_constraint("ck_state_definitions_clock_settings", "state_definitions", type_="check")
    for name in (
        "calendar",
        "bands",
        "time_awareness",
        "elapsed_tolerance_factor",
        "time_statement_tolerance_minutes",
        "grace_minutes",
        "stop_at_due_events",
        "max_unprompted_skip_minutes",
        "pacing_table",
        "anchor",
        "mode",
    ):
        op.drop_column("state_definitions", name)
    bind = op.get_bind()
    for name in reversed(ENUMS):
        postgresql.ENUM(name=name).drop(bind)
    # PostgreSQL cannot drop an enum label: the type is rebuilt without ``clock``.
    op.execute("ALTER TYPE state_type RENAME TO state_type_0085")
    postgresql.ENUM(*STATE_TYPES_BEFORE, name="state_type").create(bind)
    op.execute(
        "ALTER TABLE state_definitions ALTER COLUMN type TYPE state_type "
        "USING type::text::state_type"
    )
    op.execute("DROP TYPE state_type_0085")
