"""idempotency_keys: every mutating request's key, hash and result window (#61)

Also ``operations.timeline_revision`` (the story's revision once the operation committed), which
joins the operations trigger's allow-list.

Revision ID: 0014
Revises: 0013
Create Date: 2026-10-11

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0014"
down_revision: str | None = "0013"
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
)
AFTER = (*BEFORE, "timeline_revision")


def upgrade() -> None:
    op.add_column("operations", sa.Column("timeline_revision", sa.BigInteger(), nullable=True))
    op.execute(_guard(AFTER))
    # (scope, scope_id, key) is unique: the primary key. ``scope_id`` is the story id for scope
    # ``story`` (plain, no foreign key: story deletion removes the rows by it) and the sentinel
    # ``deployment`` otherwise.
    op.create_table(
        "idempotency_keys",
        sa.Column("scope", sa.Text(), nullable=False),
        sa.Column("scope_id", sa.Text(), nullable=False),
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("request_hash", sa.Text(), nullable=False),
        sa.Column("operation_id", ULID, nullable=True),
        sa.Column("status", sa.Text(), server_default=sa.text("'in_flight'"), nullable=False),
        sa.Column("result", postgresql.JSONB(), nullable=True),
        sa.Column("revision", sa.BigInteger(), nullable=True),
        sa.Column("owner_token", sa.Text(), nullable=False),
        sa.Column("lease_until", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=NOW, nullable=False),
        sa.Column("expires_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("scope", "scope_id", "key", name="pk_idempotency_keys"),
        sa.CheckConstraint("scope IN ('story', 'deployment')", name="ck_idempotency_keys_scope"),
        sa.CheckConstraint("status IN ('in_flight', 'done')", name="ck_idempotency_keys_status"),
    )
    op.create_index("ix_idempotency_keys_operation_id", "idempotency_keys", ["operation_id"])
    op.create_index("ix_idempotency_keys_expires_at", "idempotency_keys", ["expires_at"])


def downgrade() -> None:
    op.drop_index("ix_idempotency_keys_expires_at", table_name="idempotency_keys")
    op.drop_index("ix_idempotency_keys_operation_id", table_name="idempotency_keys")
    op.drop_table("idempotency_keys")
    op.execute(_guard(BEFORE))
    op.drop_column("operations", "timeline_revision")
