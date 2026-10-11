"""variant chains: each turn records the variant of the turn it continues (#66)

``operations`` gains ``parent_variant``: the active variant of turn N - 1 a turn variant was
built on (``NULL`` for turn 1 and for non-turn operations). It is set when the turn commits,
so it joins the trigger's allow-list. Existing committed turns are backfilled from the active
chain (a non-active turn whose parent is unknown is taken to continue variant 1).

Revision ID: 0016
Revises: 0015
Create Date: 2026-10-11

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0016"
down_revision: str | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _guard(columns: Sequence[str]) -> str:
    """The operations trigger function with ``columns`` as its allow-list (0002's body)."""
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


BEFORE = (
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
)
AFTER = (*BEFORE, "parent_variant")

BACKFILL = """
UPDATE operations o
SET parent_variant = COALESCE(
    (
        SELECT p.turn_variant FROM operations p
        WHERE p.story_id = o.story_id
          AND p.kind = 'turn' AND p.status = 'committed' AND p.membership = 'active'
          AND p.narrative_turn = o.narrative_turn - 1
    ),
    1
)
WHERE o.kind = 'turn' AND o.status = 'committed' AND o.narrative_turn > 1
"""


def upgrade() -> None:
    op.add_column("operations", sa.Column("parent_variant", sa.SmallInteger(), nullable=True))
    op.execute(_guard(AFTER))
    op.execute(BACKFILL)


def downgrade() -> None:
    op.execute(_guard(BEFORE))
    op.drop_column("operations", "parent_variant")
