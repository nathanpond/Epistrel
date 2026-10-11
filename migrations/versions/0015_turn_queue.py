"""turn_queue: per-story turn concurrency, the in-flight turn lease and the queue (#63)

``stories`` gains ``turn_concurrency`` (moved out of ``settings``, where #50 stored it),
``turn_queue_max``, and the in-flight marker ``in_flight_turn`` / ``in_flight_lease_until``.
``operation_status`` gains ``queued``; ``operations`` gains ``error_code`` / ``error`` (on the
trigger's allow-list) so a request waiting on a queued turn can answer with its failure.
``turn_queue`` is the per-story FIFO.

The downgrade leaves the ``queued`` enum value in place (Postgres cannot drop one; 0002's
downgrade drops the whole type) after turning any queued operation into ``failed``.

Revision ID: 0015
Revises: 0014
Create Date: 2026-10-11

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ULID = sa.CHAR(26)
NOW = sa.text("transaction_timestamp()")


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
)
AFTER = (*BEFORE, "error_code", "error")


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute(
            "ALTER TYPE operation_status ADD VALUE IF NOT EXISTS 'queued' BEFORE 'in_flight'"
        )
    op.add_column(
        "stories",
        sa.Column(
            "turn_concurrency", sa.Text(), server_default=sa.text("'reject'"), nullable=False
        ),
    )
    op.add_column("stories", sa.Column("turn_queue_max", sa.SmallInteger(), nullable=True))
    op.add_column("stories", sa.Column("in_flight_turn", ULID, nullable=True))
    op.add_column(
        "stories",
        sa.Column("in_flight_lease_until", sa.TIMESTAMP(timezone=True), nullable=True),
    )
    op.create_check_constraint(
        "ck_stories_turn_concurrency", "stories", "turn_concurrency IN ('reject', 'queue')"
    )
    op.create_check_constraint(
        "ck_stories_turn_queue_max",
        "stories",
        "turn_queue_max IS NULL OR turn_queue_max BETWEEN 1 AND 50",
    )
    # #50 stored the setting in the settings document; it now lives in its column.
    op.execute(
        "UPDATE stories SET turn_concurrency = settings->>'turn_concurrency' "
        "WHERE settings->>'turn_concurrency' IN ('reject', 'queue')"
    )
    op.execute(
        "UPDATE stories SET settings = settings - 'turn_concurrency' - 'turn_queue_max' "
        "WHERE settings ?| array['turn_concurrency', 'turn_queue_max']"
    )
    op.add_column("operations", sa.Column("error_code", sa.Text(), nullable=True))
    op.add_column("operations", sa.Column("error", postgresql.JSONB(), nullable=True))
    op.execute(_guard(AFTER))
    op.create_table(
        "turn_queue",
        sa.Column("story_id", ULID, nullable=False),
        sa.Column("position", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("operation_id", ULID, nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("request", postgresql.JSONB(), nullable=False),
        sa.Column("principal", sa.Text(), nullable=False),
        sa.Column("idempotency_key", sa.Text(), nullable=True),
        sa.Column("attempts", sa.SmallInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("enqueued_at", sa.TIMESTAMP(timezone=True), server_default=NOW, nullable=False),
        sa.PrimaryKeyConstraint("story_id", "position", name="pk_turn_queue"),
        sa.UniqueConstraint("operation_id", name="uq_turn_queue_operation_id"),
        sa.CheckConstraint("kind IN ('respond', 'ingest')", name="ck_turn_queue_kind"),
    )


def downgrade() -> None:
    op.drop_table("turn_queue")
    op.execute(
        "UPDATE stories SET settings = settings || "
        "jsonb_build_object('turn_concurrency', turn_concurrency)"
    )
    op.execute("UPDATE operations SET status = 'failed' WHERE status = 'queued'")
    op.execute(_guard(BEFORE))
    op.drop_column("operations", "error")
    op.drop_column("operations", "error_code")
    op.drop_constraint("ck_stories_turn_queue_max", "stories", type_="check")
    op.drop_constraint("ck_stories_turn_concurrency", "stories", type_="check")
    op.drop_column("stories", "in_flight_lease_until")
    op.drop_column("stories", "in_flight_turn")
    op.drop_column("stories", "turn_queue_max")
    op.drop_column("stories", "turn_concurrency")
