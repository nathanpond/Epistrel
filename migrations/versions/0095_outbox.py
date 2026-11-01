"""the transactional outbox: ``external_effect_outbox`` and ``outbox_outcomes`` (#210)

FR-EXT-10,12,16..18 (docs/02 §4.8): ``core.tables.outbox`` describes both tables.

- ``external_effect_outbox`` is created: one row per effect a turn asks a write-capable
  provider to apply (its id the provider idempotency key), its status a checked text, with the
  derived-record columns of ``core.membership`` (the turn that wrote it) and its beat position
  (``slot_k``, ``beat``, ``step``). No foreign key to another story table but ``operations``.
- ``outbox_outcomes`` is created: what a provider answered for a row, append-only. No foreign
  key at all (``outbox_id`` is a plain column). The trigger
  ``epistrel_outbox_outcomes_append_only`` (0068's pattern) admits an UPDATE that changes only
  ``settled``, any UPDATE or DELETE while ``epistrel.erasure = on``, and a DELETE of the story
  ``epistrel.allow_story_delete`` names; TRUNCATE is refused.

No seed rows: the ``outbox_recovery`` recurring task's row is written by ``timers.reconcile``
from its registration, and the ``outbox_lease`` timer kind is a job kind.

Downgrade drops both tables and the trigger function.

Revision ID: 0095
Revises: 0094
Create Date: 2026-10-31

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0095"
down_revision: str | None = "0094"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ULID = sa.CHAR(26)
OUTBOX = "external_effect_outbox"
OUTCOMES = "outbox_outcomes"
STATUSES = (
    "intended",
    "claimed",
    "confirmed",
    "failed",
    "unknown",
    "cancelled",
    "imported_unsettled",
)
KINDS = ("confirmed", "failed", "unknown", "duplicate", "conflict")
VIA = ("dispatcher", "ingest")

GUARD = """
CREATE OR REPLACE FUNCTION epistrel_outbox_outcomes_append_only() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'TRUNCATE' THEN
        RAISE EXCEPTION 'outbox_outcomes is append-only: TRUNCATE refused'
            USING ERRCODE = 'EPI01';
    END IF;
    IF current_setting('epistrel.erasure', true) = 'on' THEN
        RETURN CASE WHEN TG_OP = 'DELETE' THEN OLD ELSE NEW END;
    END IF;
    IF TG_OP = 'UPDATE' THEN
        IF (to_jsonb(NEW) - 'settled') = (to_jsonb(OLD) - 'settled') THEN
            RETURN NEW;
        END IF;
        RAISE EXCEPTION 'outbox_outcomes is append-only: update of outcome % refused', OLD.id
            USING ERRCODE = 'EPI01';
    END IF;
    IF current_setting('epistrel.allow_story_delete', true) = OLD.story_id::text THEN
        RETURN OLD;
    END IF;
    RAISE EXCEPTION 'outbox_outcomes is append-only: % of outcome % refused', TG_OP, OLD.id
        USING ERRCODE = 'EPI01';
END
$$;
"""

TRIGGERS = (
    f"CREATE TRIGGER outbox_outcomes_append_only BEFORE UPDATE OR DELETE ON {OUTCOMES} "
    "FOR EACH ROW EXECUTE FUNCTION epistrel_outbox_outcomes_append_only()",
    f"CREATE TRIGGER outbox_outcomes_no_truncate BEFORE TRUNCATE ON {OUTCOMES} "
    "FOR EACH STATEMENT EXECUTE FUNCTION epistrel_outbox_outcomes_append_only()",
)


def _in(column: str, values: tuple[str, ...]) -> str:
    listed = ", ".join(f"'{v}'" for v in values)
    return f"{column} IN ({listed})"


def upgrade() -> None:
    op.create_table(
        OUTBOX,
        sa.Column("id", ULID, nullable=False),
        sa.Column("story_id", ULID, nullable=False),
        sa.Column("provider_name", sa.Text(), nullable=False),
        sa.Column("value_name", sa.Text(), nullable=False),
        sa.Column("scope_key", sa.Text(), nullable=True),
        sa.Column("effect", postgresql.JSONB(), nullable=False),
        sa.Column("status", sa.Text(), server_default=sa.text("'intended'"), nullable=False),
        sa.Column("claimed_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("claim_lease_until", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("sent_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("outcome_event_id", ULID, nullable=True),
        sa.Column("attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("executable", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("transaction_timestamp()"),
            nullable=False,
        ),
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
        sa.CheckConstraint(_in("status", STATUSES), name=f"ck_{OUTBOX}_status"),
        sa.CheckConstraint("jsonb_typeof(effect) = 'object'", name=f"ck_{OUTBOX}_effect"),
        sa.CheckConstraint("attempts >= 0", name=f"ck_{OUTBOX}_attempts"),
        sa.ForeignKeyConstraint(
            ["operation_id"],
            ["operations.operation_id"],
            name=f"fk_{OUTBOX}_operation_id_operations",
        ),
        sa.PrimaryKeyConstraint("id", name=f"pk_{OUTBOX}"),
    )
    op.create_index(f"ix_{OUTBOX}_story_operation", OUTBOX, ["story_id", "operation_id"])
    op.create_index(f"ix_{OUTBOX}_story_membership", OUTBOX, ["story_id", "membership"])
    op.create_index(f"ix_{OUTBOX}_status", OUTBOX, ["status", "story_id"])
    op.create_table(
        OUTCOMES,
        sa.Column("id", ULID, nullable=False),
        sa.Column("story_id", ULID, nullable=False),
        sa.Column("outbox_id", ULID, nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column(
            "payload", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False
        ),
        sa.Column("raw_hash", sa.Text(), server_default=sa.text("''"), nullable=False),
        sa.Column("received_via", sa.Text(), nullable=False),
        sa.Column("received_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("settled", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.CheckConstraint(_in("kind", KINDS), name=f"ck_{OUTCOMES}_kind"),
        sa.CheckConstraint(_in("received_via", VIA), name=f"ck_{OUTCOMES}_received_via"),
        sa.CheckConstraint("jsonb_typeof(payload) = 'object'", name=f"ck_{OUTCOMES}_payload"),
        sa.PrimaryKeyConstraint("id", name=f"pk_{OUTCOMES}"),
    )
    op.create_index(f"ix_{OUTCOMES}_outbox", OUTCOMES, ["outbox_id", "received_at"])
    op.create_index(f"ix_{OUTCOMES}_story", OUTCOMES, ["story_id"])
    op.execute(GUARD)
    for statement in TRIGGERS:
        op.execute(statement)


def downgrade() -> None:
    op.drop_table(OUTCOMES)
    op.execute("DROP FUNCTION epistrel_outbox_outcomes_append_only()")
    op.drop_table(OUTBOX)
