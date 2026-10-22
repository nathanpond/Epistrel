"""commitments: thunks, the promissory ledger and the fact lifecycle (#146)

FR-COMMIT-1..3,5..7,10,13 (docs/02 §4.4): ``core.tables.commitments`` describes the tables.

- The enum types ``thunk_state``, ``thunk_open_slot``, ``ledger_kind`` and
  ``ledger_discharge_mode`` are created, and ``fact_commit_state`` gains ``revised``
  (FR-COMMIT-1's last state; #154's regeneration revert writes it).
- ``thunks`` and ``promissory_ledger`` are created with their checks, the derived-record
  columns and indexes of ``core.membership`` (``operation_id`` references ``operations``), the
  ``(story_id, state, predicate_id)`` index matching scans and the ``(story_id, thunk_id)``
  index of the ledger.
- The facts guard (``epistrel_facts_append_only``, 0049) lets ``commit_state`` and
  ``committed_at_position`` change in place along the lifecycle's edges only
  (``core.commit.lifecycle.TRANSITIONS``): introduced → pending → committed, introduced →
  committed, a pending fact nobody depended on back to introduced, committed → revised and
  revised → committed (#154's swap-back). Everything else about a fact stays append-only.

Downgrade restores 0049's guard, drops the tables and the new types, and takes ``revised``
out of ``fact_commit_state`` (a revised version reads ``committed`` again).

Revision ID: 0060
Revises: 0059
Create Date: 2026-10-17

"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0060"
down_revision: str | None = "0059"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ULID = sa.CHAR(26)

ENUMS: dict[str, tuple[str, ...]] = {
    "thunk_state": ("open", "resolved", "expired"),
    "thunk_open_slot": ("subject", "object", "literal"),
    "ledger_kind": ("salient_reference",),
    "ledger_discharge_mode": ("dependence", "render", "minimal"),
}

TRANSITIONS: tuple[tuple[str, str], ...] = (
    ("introduced", "pending"),
    ("introduced", "committed"),
    ("pending", "committed"),
    ("pending", "introduced"),
    ("committed", "revised"),
    ("revised", "committed"),
)
"""The in-place ``commit_state`` moves the guard lets through (``core.commit.lifecycle``'s)."""

PREVIOUS_STATES = ("committed", "introduced", "pending")

_EDGES = ", ".join(f"'{a}>{b}'" for a, b in TRANSITIONS)

GUARD = """
CREATE OR REPLACE FUNCTION epistrel_facts_append_only() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    mutable text[] := ARRAY['tx_to', 'superseded_by', 'membership', 'membership_tx'];
BEGIN
    IF TG_OP = 'UPDATE' THEN
        IF (OLD.tx_to IS NOT NULL AND NEW.tx_to IS DISTINCT FROM OLD.tx_to)
           OR (OLD.superseded_by IS NOT NULL
               AND NEW.superseded_by IS DISTINCT FROM OLD.superseded_by) THEN
            RAISE EXCEPTION 'facts: a closed version keeps its tx_to and superseded_by (fact %)',
                OLD.id USING ERRCODE = 'EPI01';
        END IF;
        IF (to_jsonb(NEW) - mutable) = (to_jsonb(OLD) - mutable) THEN
            RETURN NEW;
        END IF;{lifecycle}
        IF current_setting('epistrel.erasure', true) = 'on'
           AND (to_jsonb(NEW) - mutable - 'visibility')
               = (to_jsonb(OLD) - mutable - 'visibility') THEN
            RETURN NEW;
        END IF;
        RAISE EXCEPTION 'facts are append-only: update of fact % refused', OLD.id
            USING ERRCODE = 'EPI01';
    END IF;
    IF TG_OP = 'DELETE'
       AND (current_setting('epistrel.erasure', true) = 'on'
            OR current_setting('epistrel.allow_story_delete', true) = OLD.story_id::text) THEN
        RETURN OLD;
    END IF;
    IF TG_OP = 'TRUNCATE' THEN
        RAISE EXCEPTION 'facts are append-only: TRUNCATE refused' USING ERRCODE = 'EPI01';
    END IF;
    RAISE EXCEPTION 'facts are append-only: % of fact % refused', TG_OP, OLD.id
        USING ERRCODE = 'EPI01';
END
$$;
"""

LIFECYCLE = f"""
        IF (to_jsonb(NEW) - mutable - 'commit_state' - 'committed_at_position')
               = (to_jsonb(OLD) - mutable - 'commit_state' - 'committed_at_position')
           AND (OLD.commit_state::text || '>' || NEW.commit_state::text) = ANY(ARRAY[{_EDGES}])
        THEN
            RETURN NEW;
        END IF;"""


def guard(*, current: bool) -> str:
    """0060's guard (``current``) or 0049's."""
    return GUARD.replace("{lifecycle}", LIFECYCLE if current else "")


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


def upgrade() -> None:
    bind = op.get_bind()
    op.execute("ALTER TYPE fact_commit_state ADD VALUE IF NOT EXISTS 'revised'")
    for name, labels in ENUMS.items():
        postgresql.ENUM(*labels, name=name).create(bind)

    op.create_table(
        "thunks",
        sa.Column("id", ULID, nullable=False),
        sa.Column("story_id", ULID, nullable=False),
        sa.Column("proposition_sketch", postgresql.JSONB(), nullable=False),
        sa.Column("predicate_id", ULID, nullable=False),
        sa.Column("subject_id", ULID, nullable=True),
        sa.Column("object_id", ULID, nullable=True),
        sa.Column("open_slot", _enum("thunk_open_slot"), nullable=False),
        sa.Column("valid_time", sa.BigInteger(), nullable=True),
        sa.Column("introduced_event_id", ULID, nullable=True),
        *_derived(),
        sa.Column("beat", sa.SmallInteger(), nullable=True),
        sa.Column("state", _enum("thunk_state"), server_default=sa.text("'open'"), nullable=False),
        sa.Column("resolved_fact_id", ULID, nullable=True),
        sa.Column("resolution_operation_id", ULID, nullable=True),
        sa.Column("salient", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.CheckConstraint(
            "(open_slot = 'subject') = (subject_id IS NULL)", name="ck_thunks_open_subject"
        ),
        sa.CheckConstraint(
            "(resolved_fact_id IS NULL) = (resolution_operation_id IS NULL)",
            name="ck_thunks_resolution_pair",
        ),
        sa.CheckConstraint(
            "state <> 'resolved' OR resolved_fact_id IS NOT NULL", name="ck_thunks_resolved_fact"
        ),
        sa.ForeignKeyConstraint(
            ["operation_id"],
            ["operations.operation_id"],
            name="fk_thunks_operation_id_operations",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_thunks"),
    )
    op.create_index("ix_thunks_story_operation", "thunks", ["story_id", "operation_id"])
    op.create_index("ix_thunks_story_membership", "thunks", ["story_id", "membership"])
    op.create_index(
        "ix_thunks_story_state_predicate", "thunks", ["story_id", "state", "predicate_id"]
    )

    op.create_table(
        "promissory_ledger",
        sa.Column("id", ULID, nullable=False),
        sa.Column("story_id", ULID, nullable=False),
        sa.Column("thunk_id", ULID, nullable=True),
        sa.Column("proposition_id", ULID, nullable=True),
        sa.Column(
            "kind",
            _enum("ledger_kind"),
            server_default=sa.text("'salient_reference'"),
            nullable=False,
        ),
        *_derived(),
        sa.Column("beat", sa.SmallInteger(), nullable=True),
        sa.Column("discharged_by_operation", ULID, nullable=True),
        sa.Column("discharged_mode", _enum("ledger_discharge_mode"), nullable=True),
        sa.CheckConstraint(
            "num_nonnulls(thunk_id, proposition_id) = 1", name="ck_promissory_ledger_one_target"
        ),
        sa.CheckConstraint(
            "(discharged_by_operation IS NULL) = (discharged_mode IS NULL)",
            name="ck_promissory_ledger_discharge_pair",
        ),
        sa.ForeignKeyConstraint(
            ["operation_id"],
            ["operations.operation_id"],
            name="fk_promissory_ledger_operation_id_operations",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_promissory_ledger"),
    )
    op.create_index(
        "ix_promissory_ledger_story_operation", "promissory_ledger", ["story_id", "operation_id"]
    )
    op.create_index(
        "ix_promissory_ledger_story_membership", "promissory_ledger", ["story_id", "membership"]
    )
    op.create_index(
        "ix_promissory_ledger_story_thunk", "promissory_ledger", ["story_id", "thunk_id"]
    )

    op.execute(guard(current=True))


def downgrade() -> None:
    bind = op.get_bind()
    op.drop_table("promissory_ledger")
    op.drop_table("thunks")
    for name in reversed(ENUMS):
        postgresql.ENUM(name=name).drop(bind)
    # ``revised`` back to ``committed`` (the 0060 guard allows that edge), then 0049's guard and
    # the type without ``revised``.
    op.execute("UPDATE facts SET commit_state = 'committed' WHERE commit_state = 'revised'")
    op.execute(guard(current=False))
    labels = ", ".join(f"'{s}'" for s in PREVIOUS_STATES)
    op.execute("ALTER TABLE facts ALTER COLUMN commit_state DROP DEFAULT")
    op.execute("ALTER TYPE fact_commit_state RENAME TO fact_commit_state_0060")
    op.execute(f"CREATE TYPE fact_commit_state AS ENUM ({labels})")
    op.execute(
        "ALTER TABLE facts ALTER COLUMN commit_state TYPE fact_commit_state "
        "USING commit_state::text::fact_commit_state"
    )
    op.execute("ALTER TABLE facts ALTER COLUMN commit_state SET DEFAULT 'committed'")
    op.execute("DROP TYPE fact_commit_state_0060")
