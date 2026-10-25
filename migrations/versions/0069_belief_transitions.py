"""belief_transitions: dated, typed belief acquisitions with mandatory provenance (#160)

FR-EPI-7,13,14, FR-BELIEF-1,2,4,10 (docs/02 §4.4): ``core.tables.beliefs`` describes the tables.

- The enum types ``belief_transition_kind`` (the nine causes), ``antecedent_kind`` (``record``,
  ``staged_beat``, ``render_step``, ``resolution``) and ``belief_hold_state`` (``held``,
  ``decayed``, ``corrected``) are created.
- ``belief_transitions`` is created: one row per belief-set change, naming the belief it
  produced or changed (``belief_id``, a foreign key deferred to the commit, so the transition
  is written first), its kind, its antecedent (``antecedent_kind``, ``antecedent_ref``), the
  source character, the story-time, the confidence it left (dual: level and scalar), the
  accessibility and hold state it left, a testimony's ``outcome``, the beliefs it closed
  (``prior_belief_ids``) and the in-place state it replaced (``prior``), the dependent step's
  position (the five ``position_*`` columns), transaction-time (``tx_to`` never set: a
  transition is an event) and the derived-record columns of ``core.membership``. Append-only
  (``epistrel_belief_transitions_append_only``): an UPDATE may change the membership pair, and
  an erasure may clear ``source_character_id``; a DELETE passes only under an erasure or the
  story's deletion.
- ``beliefs`` gains ``accessibility`` (``1.0``), ``state`` (``held``), ``corrected_by`` and
  ``lineage_id``; ``provenance`` becomes NOT NULL with a deferred foreign key to
  ``belief_transitions`` and a unique index (one belief per producing transition), and a
  constraint trigger after each insert (``epistrel_beliefs_provenance``) refuses a belief
  whose producing transition is missing, of another story or holder, or attached to another
  belief
  (BELIEF-PROVENANCE). The append-only guard admits the in-place columns and lets #160's
  membership follower reopen a corrected belief (``epistrel.belief_reopen = on``).

A database with beliefs that have no provenance (none exist outside #159's development) is
refused rather than backfilled. Downgrade drops the transitions and restores 0068's beliefs.

Revision ID: 0069
Revises: 0068
Create Date: 2026-10-23

"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0069"
down_revision: str | None = "0068"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ULID = sa.CHAR(26)

ENUMS: dict[str, tuple[str, ...]] = {
    "belief_transition_kind": (
        "observation",
        "testimony",
        "inference",
        "discovery",
        "correction",
        "forgetting",
        "revision",
        "author",
        "resolution",
    ),
    "antecedent_kind": ("record", "staged_beat", "render_step", "resolution"),
    "belief_hold_state": ("held", "decayed", "corrected"),
}

POSITION = ("turn", "variant", "slot_k", "beat", "step")

BELIEF_GUARD = """
CREATE OR REPLACE FUNCTION epistrel_beliefs_append_only() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    mutable text[] := ARRAY['tx_to', 'held_to_turn', 'held_to_variant', 'held_to_slot_k',
                            'held_to_beat', 'held_to_step', 'membership', 'membership_tx',
                            'confidence_level', 'confidence_scalar', 'accessibility', 'state',
                            'corrected_by'];
BEGIN
    IF TG_OP = 'UPDATE' THEN
        IF coalesce(current_setting('epistrel.belief_reopen', true), '') <> 'on'
           AND ((OLD.tx_to IS NOT NULL AND NEW.tx_to IS DISTINCT FROM OLD.tx_to)
                OR (OLD.held_to_turn IS NOT NULL
                    AND (NEW.held_to_turn, NEW.held_to_variant, NEW.held_to_slot_k,
                         NEW.held_to_beat, NEW.held_to_step)
                        IS DISTINCT FROM (OLD.held_to_turn, OLD.held_to_variant,
                                          OLD.held_to_slot_k, OLD.held_to_beat,
                                          OLD.held_to_step))) THEN
            RAISE EXCEPTION 'beliefs: a closed belief keeps its tx_to and held_to (belief %)',
                OLD.id USING ERRCODE = 'EPI01';
        END IF;
        IF (to_jsonb(NEW) - mutable) = (to_jsonb(OLD) - mutable) THEN
            RETURN NEW;
        END IF;
        RAISE EXCEPTION 'beliefs are append-only: update of belief % refused', OLD.id
            USING ERRCODE = 'EPI01';
    END IF;
    IF TG_OP = 'DELETE'
       AND (current_setting('epistrel.erasure', true) = 'on'
            OR current_setting('epistrel.allow_story_delete', true) = OLD.story_id::text) THEN
        RETURN OLD;
    END IF;
    IF TG_OP = 'TRUNCATE' THEN
        RAISE EXCEPTION 'beliefs are append-only: TRUNCATE refused' USING ERRCODE = 'EPI01';
    END IF;
    RAISE EXCEPTION 'beliefs are append-only: % of belief % refused', TG_OP, OLD.id
        USING ERRCODE = 'EPI01';
END
$$;
"""

OLD_BELIEF_GUARD = """
CREATE OR REPLACE FUNCTION epistrel_beliefs_append_only() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    mutable text[] := ARRAY['tx_to', 'held_to_turn', 'held_to_variant', 'held_to_slot_k',
                            'held_to_beat', 'held_to_step', 'membership', 'membership_tx'];
BEGIN
    IF TG_OP = 'UPDATE' THEN
        IF (OLD.tx_to IS NOT NULL AND NEW.tx_to IS DISTINCT FROM OLD.tx_to)
           OR (OLD.held_to_turn IS NOT NULL
               AND (NEW.held_to_turn, NEW.held_to_variant, NEW.held_to_slot_k,
                    NEW.held_to_beat, NEW.held_to_step)
                   IS DISTINCT FROM (OLD.held_to_turn, OLD.held_to_variant, OLD.held_to_slot_k,
                                     OLD.held_to_beat, OLD.held_to_step)) THEN
            RAISE EXCEPTION 'beliefs: a closed belief keeps its tx_to and held_to (belief %)',
                OLD.id USING ERRCODE = 'EPI01';
        END IF;
        IF (to_jsonb(NEW) - mutable) = (to_jsonb(OLD) - mutable) THEN
            RETURN NEW;
        END IF;
        RAISE EXCEPTION 'beliefs are append-only: update of belief % refused', OLD.id
            USING ERRCODE = 'EPI01';
    END IF;
    IF TG_OP = 'DELETE'
       AND (current_setting('epistrel.erasure', true) = 'on'
            OR current_setting('epistrel.allow_story_delete', true) = OLD.story_id::text) THEN
        RETURN OLD;
    END IF;
    IF TG_OP = 'TRUNCATE' THEN
        RAISE EXCEPTION 'beliefs are append-only: TRUNCATE refused' USING ERRCODE = 'EPI01';
    END IF;
    RAISE EXCEPTION 'beliefs are append-only: % of belief % refused', TG_OP, OLD.id
        USING ERRCODE = 'EPI01';
END
$$;
"""

PROVENANCE_GUARD = """
CREATE OR REPLACE FUNCTION epistrel_beliefs_provenance() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    producer record;
BEGIN
    SELECT t.story_id, t.holder_character_id, t.belief_id INTO producer
        FROM belief_transitions t WHERE t.id = NEW.provenance;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'beliefs: belief % has no producing transition (%)',
            NEW.id, NEW.provenance USING ERRCODE = 'EPI02';
    END IF;
    IF producer.story_id <> NEW.story_id
       OR producer.holder_character_id <> NEW.holder_character_id
       OR producer.belief_id <> NEW.id THEN
        RAISE EXCEPTION 'beliefs: transition % does not produce belief %', NEW.provenance, NEW.id
            USING ERRCODE = 'EPI02';
    END IF;
    RETURN NEW;
END
$$;
"""

TRANSITION_GUARD = """
CREATE OR REPLACE FUNCTION epistrel_belief_transitions_append_only() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    mutable text[] := ARRAY['membership', 'membership_tx'];
BEGIN
    IF TG_OP = 'UPDATE' THEN
        IF current_setting('epistrel.erasure', true) = 'on' THEN
            mutable := mutable || ARRAY['source_character_id'];
        END IF;
        IF (to_jsonb(NEW) - mutable) = (to_jsonb(OLD) - mutable) THEN
            RETURN NEW;
        END IF;
        RAISE EXCEPTION 'belief transitions are append-only: update of % refused', OLD.id
            USING ERRCODE = 'EPI01';
    END IF;
    IF TG_OP = 'DELETE'
       AND (current_setting('epistrel.erasure', true) = 'on'
            OR current_setting('epistrel.allow_story_delete', true) = OLD.story_id::text) THEN
        RETURN OLD;
    END IF;
    IF TG_OP = 'TRUNCATE' THEN
        RAISE EXCEPTION 'belief transitions are append-only: TRUNCATE refused'
            USING ERRCODE = 'EPI01';
    END IF;
    RAISE EXCEPTION 'belief transitions are append-only: % of % refused', TG_OP, OLD.id
        USING ERRCODE = 'EPI01';
END
$$;
"""

TRIGGERS = (
    "CREATE TRIGGER belief_transitions_append_only BEFORE UPDATE OR DELETE ON belief_transitions "
    "FOR EACH ROW EXECUTE FUNCTION epistrel_belief_transitions_append_only()",
    "CREATE TRIGGER belief_transitions_no_truncate BEFORE TRUNCATE ON belief_transitions "
    "FOR EACH STATEMENT EXECUTE FUNCTION epistrel_belief_transitions_append_only()",
    "CREATE CONSTRAINT TRIGGER beliefs_provenance AFTER INSERT ON beliefs "
    "FOR EACH ROW EXECUTE FUNCTION epistrel_beliefs_provenance()",
)

NO_ORPHANS = """
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM beliefs WHERE provenance IS NULL) THEN
        RAISE EXCEPTION 'beliefs without a producing transition exist; #160 does not backfill';
    END IF;
END
$$;
"""


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
    op.execute(NO_ORPHANS)
    for name, labels in ENUMS.items():
        postgresql.ENUM(*labels, name=name).create(bind)

    op.create_table(
        "belief_transitions",
        sa.Column("id", ULID, nullable=False),
        sa.Column("story_id", ULID, nullable=False),
        sa.Column("holder_character_id", ULID, nullable=False),
        sa.Column("belief_id", ULID, nullable=False),
        sa.Column("kind", _enum("belief_transition_kind"), nullable=False),
        sa.Column("antecedent_kind", _enum("antecedent_kind"), nullable=False),
        sa.Column("antecedent_ref", postgresql.JSONB(), nullable=False),
        sa.Column("source_character_id", ULID, nullable=True),
        sa.Column("valid_at", sa.BigInteger(), nullable=True),
        sa.Column("confidence_level", _enum("belief_confidence_level"), nullable=False),
        sa.Column("confidence_scalar", sa.NUMERIC(4, 3), nullable=True),
        sa.Column("accessibility", sa.NUMERIC(4, 3), server_default=sa.text("1.0"), nullable=False),
        sa.Column(
            "state", _enum("belief_hold_state"), server_default=sa.text("'held'"), nullable=False
        ),
        sa.Column("outcome", sa.Text(), nullable=True),
        sa.Column(
            "prior_belief_ids",
            postgresql.ARRAY(ULID),
            server_default=sa.text("'{}'"),
            nullable=False,
        ),
        sa.Column("prior", postgresql.JSONB(none_as_null=True), nullable=True),
        sa.Column("position_turn", sa.Integer(), nullable=False),
        *(
            sa.Column(
                f"position_{name}", sa.SmallInteger(), server_default=sa.text("0"), nullable=False
            )
            for name in POSITION[1:]
        ),
        sa.Column("tx_from", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("tx_to", sa.TIMESTAMP(timezone=True), nullable=True),
        *_derived(),
        sa.CheckConstraint(
            "source_character_id IS NULL OR kind IN ('testimony', 'observation', 'discovery')",
            name="ck_belief_transitions_source_kind",
        ),
        sa.CheckConstraint(
            "outcome IS NULL OR (kind = 'testimony' AND outcome IN "
            "('accept', 'reject', 'suspend', 'speech_act_only'))",
            name="ck_belief_transitions_outcome",
        ),
        sa.CheckConstraint(
            "confidence_scalar IS NULL OR (confidence_scalar >= 0 AND confidence_scalar <= 1)",
            name="ck_belief_transitions_confidence_scalar",
        ),
        sa.CheckConstraint(
            "accessibility >= 0 AND accessibility <= 1",
            name="ck_belief_transitions_accessibility",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(antecedent_ref) = 'object'", name="ck_belief_transitions_antecedent_ref"
        ),
        sa.CheckConstraint("tx_to IS NULL", name="ck_belief_transitions_tx_open"),
        sa.ForeignKeyConstraint(
            ["operation_id"],
            ["operations.operation_id"],
            name="fk_belief_transitions_operation_id_operations",
        ),
        sa.ForeignKeyConstraint(
            ["belief_id"],
            ["beliefs.id"],
            name="fk_belief_transitions_belief_id_beliefs",
            deferrable=True,
            initially="DEFERRED",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_belief_transitions"),
    )
    op.create_index(
        "ix_belief_transitions_story_operation",
        "belief_transitions",
        ["story_id", "operation_id"],
    )
    op.create_index(
        "ix_belief_transitions_story_membership",
        "belief_transitions",
        ["story_id", "membership"],
    )
    op.create_index(
        "ix_belief_transitions_story_holder_belief",
        "belief_transitions",
        ["story_id", "holder_character_id", "belief_id"],
    )
    op.create_index("ix_belief_transitions_story_kind", "belief_transitions", ["story_id", "kind"])
    op.create_index(
        "ix_belief_transitions_prior",
        "belief_transitions",
        ["prior_belief_ids"],
        postgresql_using="gin",
    )

    op.add_column(
        "beliefs",
        sa.Column("accessibility", sa.NUMERIC(4, 3), server_default=sa.text("1.0"), nullable=False),
    )
    op.add_column(
        "beliefs",
        sa.Column(
            "state", _enum("belief_hold_state"), server_default=sa.text("'held'"), nullable=False
        ),
    )
    op.add_column("beliefs", sa.Column("corrected_by", ULID, nullable=True))
    op.add_column("beliefs", sa.Column("lineage_id", ULID, nullable=True))
    op.execute("UPDATE beliefs SET lineage_id = id")
    op.alter_column("beliefs", "lineage_id", nullable=False)
    op.alter_column("beliefs", "provenance", nullable=False)
    op.create_check_constraint(
        "ck_beliefs_accessibility", "beliefs", "accessibility >= 0 AND accessibility <= 1"
    )
    op.create_check_constraint(
        "ck_beliefs_corrected_pair", "beliefs", "(state = 'corrected') = (corrected_by IS NOT NULL)"
    )
    op.create_foreign_key(
        "fk_beliefs_provenance_belief_transitions",
        "beliefs",
        "belief_transitions",
        ["provenance"],
        ["id"],
        deferrable=True,
        initially="DEFERRED",
    )
    op.create_index("ux_beliefs_provenance", "beliefs", ["provenance"], unique=True)
    op.create_index("ix_beliefs_story_lineage", "beliefs", ["story_id", "lineage_id"])

    op.execute(BELIEF_GUARD)
    op.execute(PROVENANCE_GUARD)
    op.execute(TRANSITION_GUARD)
    for statement in TRIGGERS:
        op.execute(statement)


def downgrade() -> None:
    op.execute("DROP TRIGGER beliefs_provenance ON beliefs")
    op.execute("DROP FUNCTION epistrel_beliefs_provenance()")
    op.execute(OLD_BELIEF_GUARD)
    op.drop_index("ix_beliefs_story_lineage", table_name="beliefs")
    op.drop_index("ux_beliefs_provenance", table_name="beliefs")
    op.drop_constraint("fk_beliefs_provenance_belief_transitions", "beliefs", type_="foreignkey")
    op.drop_constraint("ck_beliefs_corrected_pair", "beliefs", type_="check")
    op.drop_constraint("ck_beliefs_accessibility", "beliefs", type_="check")
    op.alter_column("beliefs", "provenance", nullable=True)
    op.drop_column("beliefs", "lineage_id")
    op.drop_column("beliefs", "corrected_by")
    op.drop_column("beliefs", "state")
    op.drop_column("beliefs", "accessibility")
    op.drop_table("belief_transitions")
    op.execute("DROP FUNCTION epistrel_belief_transitions_append_only()")
    bind = op.get_bind()
    for name in reversed(ENUMS):
        postgresql.ENUM(name=name).drop(bind)
