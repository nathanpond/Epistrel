"""operations: a staged turn's stage (#212)

FR-EXT-16,19 (docs/02 §4.8): a turn with an effect on a ``synchronous`` write-capable provider
commits as a workflow of atomic stages (intent, claim, outcome, completion, response). Its
operation is ``committed`` from the intent commit on, so ``operations.status`` is unchanged; the
stage is tracked apart:

- a Postgres enum ``turn_stage`` (``intent_committed``, ``awaiting_outcome``,
  ``outcome_recorded``, ``completed``, ``abandoned``);
- ``operations.turn_stage``, nullable: ``NULL`` for every ordinary turn and every non-turn
  operation (no backfill);
- ``operations``' update guard (``epistrel_operations_guard``) lets ``turn_stage`` change.

Downgrade restores 0085's guard, drops the column and the type.

Revision ID: 0101
Revises: 0100
Create Date: 2026-10-31

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0101"
down_revision: str | None = "0100"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

STAGES = ("intent_committed", "awaiting_outcome", "outcome_recorded", "completed", "abandoned")

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
    "slot_k",
)
OPERATIONS_AFTER = (*OPERATIONS_BEFORE, "turn_stage")


def _operations_guard(columns: Sequence[str]) -> str:
    """The operations trigger function with ``columns`` as its allow-list (0085's body)."""
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


def upgrade() -> None:
    postgresql.ENUM(*STAGES, name="turn_stage").create(op.get_bind())
    op.add_column(
        "operations",
        sa.Column(
            "turn_stage",
            postgresql.ENUM(name="turn_stage", create_type=False),
            nullable=True,
        ),
    )
    op.execute(_operations_guard(OPERATIONS_AFTER))


def downgrade() -> None:
    op.execute(_operations_guard(OPERATIONS_BEFORE))
    op.drop_column("operations", "turn_stage")
    postgresql.ENUM(name="turn_stage").drop(op.get_bind())
