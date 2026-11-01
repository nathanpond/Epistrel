"""States: ``state_definitions``, ``state_changes`` and the ``state_values`` projection (#185)

FR-STATE-1,2,4,6 (docs/02 §7.1): ``core.tables.states`` describes the tables.

- The enum types ``state_type`` (``number``, ``boolean``, ``enum``, ``text``; #193 adds
  ``clock``), ``state_scope``, ``state_visibility``, ``state_owner``, ``state_created_by``,
  ``state_change_kind``, ``state_change_source``, ``state_contingency`` and
  ``state_change_status`` are created.
- ``state_definitions`` is created: one row per declared State, unique ``(story_id, name)``,
  with the derived-record columns of ``core.membership`` (its defining operation).
- ``state_changes`` is created: one row per change, positioned (the derived columns plus
  ``slot_k``, ``beat`` and ``step``), its story-minute and record time. ``state_id`` has no
  foreign key (no plain foreign keys between story tables). Append-only
  (``epistrel_state_changes_append_only``): an UPDATE may change ``membership`` and
  ``membership_tx`` (and, under an erasure, the ``reason`` and the values it redacts); a DELETE
  passes only under an erasure or the story's deletion; TRUNCATE is refused.
- ``state_values`` is created: the rebuildable projection of the folded values.

Downgrade drops the tables, the guard and the enum types.

Revision ID: 0084
Revises: 0083
Create Date: 2026-10-31

"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0084"
down_revision: str | None = "0083"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ULID = sa.CHAR(26)

ENUMS: dict[str, tuple[str, ...]] = {
    "state_type": ("number", "boolean", "enum", "text"),
    "state_scope": ("story", "character"),
    "state_visibility": ("player_visible", "hidden"),
    "state_owner": ("story", "external"),
    "state_created_by": ("genesis", "consumer", "author", "proposal"),
    "state_change_kind": ("set", "delta", "transfer_in", "transfer_out"),
    "state_change_source": (
        "turn",
        "author",
        "consumer",
        "rule",
        "pacing",
        "declared_skip",
        "estimate",
        "player",
        "external",
        "settlement",
        "seed",
    ),
    "state_contingency": ("unconditional", "reservation", "conditional"),
    "state_change_status": ("committed", "held", "pending", "released", "converted", "dropped"),
}

GUARD = """
CREATE OR REPLACE FUNCTION epistrel_state_changes_append_only() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    mutable text[] := ARRAY['membership', 'membership_tx'];
BEGIN
    IF TG_OP = 'UPDATE' THEN
        IF current_setting('epistrel.erasure', true) = 'on' THEN
            mutable := mutable || ARRAY['reason', 'value', 'old_value', 'new_value'];
        END IF;
        IF (to_jsonb(NEW) - mutable) = (to_jsonb(OLD) - mutable) THEN
            RETURN NEW;
        END IF;
        RAISE EXCEPTION 'state changes are append-only: update of % refused', OLD.id
            USING ERRCODE = 'EPI01';
    END IF;
    IF TG_OP = 'DELETE'
       AND (current_setting('epistrel.erasure', true) = 'on'
            OR current_setting('epistrel.allow_story_delete', true) = OLD.story_id::text) THEN
        RETURN OLD;
    END IF;
    IF TG_OP = 'TRUNCATE' THEN
        RAISE EXCEPTION 'state changes are append-only: TRUNCATE refused' USING ERRCODE = 'EPI01';
    END IF;
    RAISE EXCEPTION 'state changes are append-only: % of % refused', TG_OP, OLD.id
        USING ERRCODE = 'EPI01';
END
$$;
"""

TRIGGERS = (
    "CREATE TRIGGER state_changes_append_only BEFORE UPDATE OR DELETE ON state_changes "
    "FOR EACH ROW EXECUTE FUNCTION epistrel_state_changes_append_only()",
    "CREATE TRIGGER state_changes_no_truncate BEFORE TRUNCATE ON state_changes "
    "FOR EACH STATEMENT EXECUTE FUNCTION epistrel_state_changes_append_only()",
)

POSITION = ("turn", "variant", "slot_k", "beat", "step")


def _enum(name: str) -> postgresql.ENUM:
    return postgresql.ENUM(name=name, create_type=False)


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
            "membership", _enum("membership"), server_default=sa.text("'active'"), nullable=False
        ),
        sa.Column("membership_tx", sa.TIMESTAMP(timezone=True), nullable=False),
    ]


def _derived_indexes(table: str) -> None:
    op.create_index(f"ix_{table}_story_operation", table, ["story_id", "operation_id"])
    op.create_index(f"ix_{table}_story_membership", table, ["story_id", "membership"])


def _operation_key(table: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        ["operation_id"],
        ["operations.operation_id"],
        name=f"fk_{table}_operation_id_operations",
    )


def upgrade() -> None:
    bind = op.get_bind()
    for name, labels in ENUMS.items():
        postgresql.ENUM(*labels, name=name).create(bind)

    op.create_table(
        "state_definitions",
        sa.Column("id", ULID, nullable=False),
        sa.Column("story_id", ULID, nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("type", _enum("state_type"), nullable=False),
        sa.Column("scope", _enum("state_scope"), nullable=False),
        sa.Column(
            "constraints",
            postgresql.JSONB(),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("initial_value", postgresql.JSONB(none_as_null=True), nullable=True),
        sa.Column(
            "aliases", postgresql.ARRAY(sa.Text()), server_default=sa.text("'{}'"), nullable=False
        ),
        sa.Column("unit", sa.Text(), nullable=True),
        sa.Column("entity_id", ULID, nullable=True),
        sa.Column("visibility", _enum("state_visibility"), nullable=False),
        sa.Column("owner", _enum("state_owner"), nullable=False),
        sa.Column("provider_id", sa.Text(), nullable=True),
        sa.Column("silent", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column(
            "rules", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False
        ),
        sa.Column(
            "authorization",
            postgresql.JSONB(),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "transfer", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False
        ),
        sa.Column("reservation_expiry_minutes", sa.Integer(), nullable=True),
        sa.Column("created_by", _enum("state_created_by"), nullable=False),
        *_derived(),
        sa.CheckConstraint(
            "length(name) BETWEEN 1 AND 80", name="ck_state_definitions_name_length"
        ),
        sa.CheckConstraint(
            "owner <> 'external' OR provider_id IS NOT NULL",
            name="ck_state_definitions_external_provider",
        ),
        sa.CheckConstraint(
            "reservation_expiry_minutes IS NULL OR reservation_expiry_minutes > 0",
            name="ck_state_definitions_reservation_expiry",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(constraints) = 'object' AND jsonb_typeof(rules) = 'object' "
            "AND jsonb_typeof(\"authorization\") = 'object' AND jsonb_typeof(transfer) = 'object'",
            name="ck_state_definitions_documents",
        ),
        _operation_key("state_definitions"),
        sa.PrimaryKeyConstraint("id", name="pk_state_definitions"),
        sa.UniqueConstraint("story_id", "name", name="uq_state_definitions_story_id"),
    )
    _derived_indexes("state_definitions")

    op.create_table(
        "state_changes",
        sa.Column("id", ULID, nullable=False),
        sa.Column("story_id", ULID, nullable=False),
        sa.Column("state_id", ULID, nullable=False),
        sa.Column("scope_key", ULID, nullable=True),
        sa.Column("kind", _enum("state_change_kind"), nullable=False),
        sa.Column("value", postgresql.JSONB(none_as_null=True), nullable=True),
        sa.Column("delta", sa.Numeric(), nullable=True),
        sa.Column("old_value", postgresql.JSONB(none_as_null=True), nullable=True),
        sa.Column("new_value", postgresql.JSONB(none_as_null=True), nullable=True),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("source", _enum("state_change_source"), nullable=False),
        sa.Column(
            "contingency",
            _enum("state_contingency"),
            server_default=sa.text("'unconditional'"),
            nullable=False,
        ),
        sa.Column(
            "status",
            _enum("state_change_status"),
            server_default=sa.text("'committed'"),
            nullable=False,
        ),
        sa.Column(
            "prerequisite_effect_ids",
            postgresql.ARRAY(ULID),
            server_default=sa.text("'{}'"),
            nullable=False,
        ),
        *_derived(),
        *(
            sa.Column(name, sa.SmallInteger(), server_default=sa.text("0"), nullable=False)
            for name in ("slot_k", "beat", "step")
        ),
        sa.Column("valid_time_minutes", sa.BigInteger(), nullable=True),
        sa.Column("tx_from", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.CheckConstraint(
            "narrative_turn IS NOT NULL AND turn_variant IS NOT NULL",
            name="ck_state_changes_positioned",
        ),
        sa.CheckConstraint(
            "(kind = 'set') = (value IS NOT NULL) AND (kind = 'set') = (delta IS NULL)",
            name="ck_state_changes_kind_shape",
        ),
        sa.CheckConstraint("length(reason) <= 200", name="ck_state_changes_reason_length"),
        _operation_key("state_changes"),
        sa.PrimaryKeyConstraint("id", name="pk_state_changes"),
    )
    _derived_indexes("state_changes")
    op.create_index(
        "ix_state_changes_story_state_scope",
        "state_changes",
        ["story_id", "state_id", "scope_key"],
    )

    op.create_table(
        "state_values",
        sa.Column("story_id", ULID, nullable=False),
        sa.Column("state_id", ULID, nullable=False),
        sa.Column("scope_key", sa.Text(), server_default=sa.text("''"), nullable=False),
        sa.Column("value", postgresql.JSONB(none_as_null=True), nullable=True),
        sa.Column("since_turn", sa.Integer(), nullable=False),
        *(
            sa.Column(
                f"since_{name}", sa.SmallInteger(), server_default=sa.text("0"), nullable=False
            )
            for name in POSITION[1:]
        ),
        sa.Column("change_count", sa.Integer(), nullable=False),
        sa.CheckConstraint("change_count > 0", name="ck_state_values_change_count"),
        sa.PrimaryKeyConstraint("story_id", "state_id", "scope_key", name="pk_state_values"),
    )

    op.execute(GUARD)
    for statement in TRIGGERS:
        op.execute(statement)


def downgrade() -> None:
    op.drop_table("state_values")
    op.drop_table("state_changes")
    op.execute("DROP FUNCTION epistrel_state_changes_append_only()")
    op.drop_table("state_definitions")
    bind = op.get_bind()
    for name in reversed(ENUMS):
        postgresql.ENUM(name=name).drop(bind)
