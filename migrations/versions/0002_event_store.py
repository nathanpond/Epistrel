"""event store: stories, operations, events, and the append-only triggers

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-11

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Frozen copies of epistrel.core.enums at this revision (a later value is a later migration);
# tests/integration/test_event_store_schema.py asserts the database labels match the enums.
PG_ENUM_TYPES: dict[str, tuple[str, ...]] = {
    "event_category": ("narrative_state", "input", "derived", "amendment", "audit", "control"),
    "operation_kind": (
        "turn",
        "inter_turn",
        "genesis",
        "consolidation",
        "rewrite_session",
        "backfill",
        "import",
        "control",
        "maintenance",
    ),
    "membership": ("active", "discarded", "dormant", "superseded"),
    "provenance": ("player", "engine", "consumer", "system"),
    "authority": ("authoritative", "non_authoritative"),
    "operation_status": ("in_flight", "committed", "abandoned", "failed"),
}

ULID = sa.CHAR(26)
NOW = sa.text("transaction_timestamp()")


def _enum(name: str) -> postgresql.ENUM:
    return postgresql.ENUM(name=name, create_type=False)


EVENTS_GUARD = """
CREATE FUNCTION epistrel_events_append_only() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        IF OLD.story_id IS NOT NULL
           AND current_setting('epistrel.allow_story_delete', true) = OLD.story_id::text THEN
            RETURN OLD;
        END IF;
        IF OLD.category = 'audit'
           AND current_setting('epistrel.allow_audit_expiry', true) = 'on' THEN
            RETURN OLD;
        END IF;
    END IF;
    IF TG_OP = 'TRUNCATE' THEN
        RAISE EXCEPTION 'events are append-only: TRUNCATE refused'
            USING ERRCODE = 'EPI01';
    END IF;
    RAISE EXCEPTION 'events are append-only: % of event % refused', TG_OP, OLD.id
        USING ERRCODE = 'EPI01';
END
$$;

CREATE TRIGGER events_append_only
    BEFORE UPDATE OR DELETE ON events
    FOR EACH ROW EXECUTE FUNCTION epistrel_events_append_only();

CREATE TRIGGER events_no_truncate
    BEFORE TRUNCATE ON events
    FOR EACH STATEMENT EXECUTE FUNCTION epistrel_events_append_only();
"""

OPERATIONS_GUARD = """
CREATE FUNCTION epistrel_operations_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    mutable text[] := ARRAY[
        'status', 'membership', 'membership_tx', 'narrative_turn', 'turn_variant',
        'base_revision', 'read_set', 'config_revisions', 'committed_at', 'attempts',
        'turn_report', 'viewpoint'
    ];
BEGIN
    IF (to_jsonb(NEW) - mutable) IS DISTINCT FROM (to_jsonb(OLD) - mutable) THEN
        RAISE EXCEPTION 'operation %: only % may change', OLD.operation_id, mutable
            USING ERRCODE = 'EPI02';
    END IF;
    RETURN NEW;
END
$$;

CREATE TRIGGER operations_guard
    BEFORE UPDATE ON operations
    FOR EACH ROW EXECUTE FUNCTION epistrel_operations_guard();
"""


def upgrade() -> None:
    for name, labels in PG_ENUM_TYPES.items():
        postgresql.ENUM(*labels, name=name).create(op.get_bind())

    op.create_table(
        "stories",
        sa.Column("id", ULID, nullable=False),
        sa.Column("status", sa.Text(), server_default=sa.text("'ready'"), nullable=False),
        sa.Column(
            "timeline_revision", sa.BigInteger(), server_default=sa.text("0"), nullable=False
        ),
        sa.Column("config_revision", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("next_turn_index", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=NOW, nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_stories"),
    )

    op.create_table(
        "operations",
        sa.Column("operation_id", ULID, nullable=False),
        sa.Column("story_id", ULID, nullable=True),
        sa.Column("kind", _enum("operation_kind"), nullable=False),
        sa.Column("narrative_turn", sa.Integer(), nullable=True),
        sa.Column("turn_variant", sa.SmallInteger(), nullable=True),
        sa.Column("slot_k", sa.SmallInteger(), nullable=True),
        sa.Column(
            "status",
            _enum("operation_status"),
            server_default=sa.text("'in_flight'"),
            nullable=False,
        ),
        sa.Column("principal", sa.Text(), nullable=False),
        sa.Column("base_revision", sa.BigInteger(), nullable=True),
        sa.Column("read_set", postgresql.JSONB(), nullable=True),
        sa.Column("config_revisions", postgresql.JSONB(), nullable=True),
        sa.Column("source_window", postgresql.JSONB(), nullable=True),
        sa.Column(
            "membership", _enum("membership"), server_default=sa.text("'active'"), nullable=False
        ),
        sa.Column("membership_tx", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("turn_report", postgresql.JSONB(), nullable=True),
        sa.Column("viewpoint", sa.Text(), nullable=True),
        sa.Column("started_at", sa.TIMESTAMP(timezone=True), server_default=NOW, nullable=False),
        sa.Column("committed_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.CheckConstraint(
            "kind <> 'turn' OR status <> 'committed' OR narrative_turn IS NOT NULL",
            name="ck_operations_turn_has_position",
        ),
        sa.ForeignKeyConstraint(
            ["story_id"], ["stories.id"], name="fk_operations_story_id_stories"
        ),
        sa.PrimaryKeyConstraint("operation_id", name="pk_operations"),
    )
    op.create_index(
        "ix_operations_story_position",
        "operations",
        ["story_id", "narrative_turn", "turn_variant"],
    )

    op.create_table(
        "events",
        sa.Column("id", ULID, nullable=False),
        sa.Column("story_id", ULID, nullable=True),
        sa.Column("seq", sa.BigInteger(), nullable=False),
        sa.Column("operation_id", ULID, nullable=False),
        sa.Column("category", _enum("event_category"), nullable=False),
        sa.Column("narrative_turn", sa.Integer(), nullable=True),
        sa.Column("turn_variant", sa.SmallInteger(), nullable=True),
        sa.Column("beat", sa.SmallInteger(), nullable=True),
        sa.Column("step", sa.SmallInteger(), nullable=True),
        sa.Column("slot_k", sa.SmallInteger(), nullable=True),
        sa.Column("character_id", ULID, nullable=True),
        sa.Column("principal", sa.Text(), nullable=False),
        sa.Column("type", sa.Text(), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("valid_time", sa.TIMESTAMP(timezone=True), server_default=NOW, nullable=False),
        sa.Column("tx_time", sa.TIMESTAMP(timezone=True), server_default=NOW, nullable=False),
        sa.Column("provenance", _enum("provenance"), nullable=False),
        sa.Column("authority", _enum("authority"), nullable=False),
        sa.ForeignKeyConstraint(
            ["operation_id"],
            ["operations.operation_id"],
            name="fk_events_operation_id_operations",
        ),
        sa.ForeignKeyConstraint(["story_id"], ["stories.id"], name="fk_events_story_id_stories"),
        sa.PrimaryKeyConstraint("id", name="pk_events"),
        sa.UniqueConstraint("story_id", "seq", name="uq_events_story_id_seq"),
    )
    op.create_index(
        "ix_events_story_position", "events", ["story_id", "narrative_turn", "turn_variant"]
    )
    op.create_index("ix_events_story_category", "events", ["story_id", "category"])
    op.create_index("ix_events_operation_id", "events", ["operation_id"])

    op.execute(EVENTS_GUARD)
    op.execute(OPERATIONS_GUARD)


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS operations_guard ON operations")
    op.execute("DROP FUNCTION IF EXISTS epistrel_operations_guard()")
    op.execute("DROP TRIGGER IF EXISTS events_no_truncate ON events")
    op.execute("DROP TRIGGER IF EXISTS events_append_only ON events")
    op.execute("DROP FUNCTION IF EXISTS epistrel_events_append_only()")
    op.drop_table("events")
    op.drop_table("operations")
    op.drop_table("stories")
    for name in reversed(PG_ENUM_TYPES):
        postgresql.ENUM(name=name).drop(op.get_bind())
