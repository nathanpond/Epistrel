"""beliefs: per-character belief partitions and the belief_state projection (#159)

FR-EPI-1,3,5,6, FR-STORE-3 (docs/02 §4.4, §4.5): ``core.tables.beliefs`` describes both tables.

- The enum types ``belief_confidence_level`` (``unknown``, ``suspected``, ``believed``,
  ``known``) and ``belief_kind`` (``first_order``, ``attributed``, ``speech_act``, ``nested``;
  #167 writes the last three) are created.
- ``beliefs`` is created with its checks (the holder is the only reader of a belief, the
  ``characters`` scope is non-empty, ``held_to`` is all ``NULL`` or a whole position after
  ``held_from``, the scalar is in ``0..1``), the derived-record columns and indexes of
  ``core.membership`` (``operation_id`` references ``operations``), the
  ``(story_id, holder_character_id, proposition_id)`` and ``(story_id, proposition_id)``
  indexes, a GiST index over ``valid_span``, a GIN index over ``visibility`` and the partial
  unique index ``ux_beliefs_open`` (one open, active first-order belief per holder and
  proposition).
- ``beliefs`` is append-only (``epistrel_beliefs_append_only``): an UPDATE may set ``tx_to``
  and the ``held_to_*`` group once each (``NULL`` to a value) and change
  ``membership``/``membership_tx``; a DELETE passes only under an erasure or for the story
  ``epistrel.allow_story_delete`` names; TRUNCATE is refused.
- ``belief_state`` is created (primary key ``(story_id, holder_character_id,
  proposition_id)``, an index by proposition): a rebuildable projection, no trigger.
- ``proposition_changes``' holder check admits ``belief``, whose ``speaker_id`` is the holder.

Downgrade restores the holder checks (deleting the belief change rows) and drops the rest.

Revision ID: 0068
Revises: 0067
Create Date: 2026-10-23

"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0068"
down_revision: str | None = "0067"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ULID = sa.CHAR(26)

ENUMS: dict[str, tuple[str, ...]] = {
    "belief_confidence_level": ("unknown", "suspected", "believed", "known"),
    "belief_kind": ("first_order", "attributed", "speech_act", "nested"),
}

POSITION = ("turn", "variant", "slot_k", "beat", "step")

GUARD = """
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

TRIGGERS = (
    "CREATE TRIGGER beliefs_append_only BEFORE UPDATE OR DELETE ON beliefs "
    "FOR EACH ROW EXECUTE FUNCTION epistrel_beliefs_append_only()",
    "CREATE TRIGGER beliefs_no_truncate BEFORE TRUNCATE ON beliefs "
    "FOR EACH STATEMENT EXECUTE FUNCTION epistrel_beliefs_append_only()",
)

SCOPES = "visibility->>'scope' IN ('objective', 'public', 'engine', 'characters')"
CHARACTERS = (
    "(visibility->>'scope' = 'characters') = "
    "(jsonb_typeof(visibility->'characters') = 'array' "
    "AND jsonb_array_length(visibility->'characters') > 0)"
)
HOLDER_ONLY = (
    "visibility = jsonb_build_object('scope', 'characters', 'characters', "
    "jsonb_build_array(holder_character_id::text))"
)
OPEN_WHERE = (
    "tx_to IS NULL AND held_to_turn IS NULL AND kind = 'first_order' AND membership = 'active'"
)


def _enum(name: str) -> postgresql.ENUM:
    return postgresql.ENUM(name=name, create_type=False)


def _group(prefix: str, *, nullable: bool) -> list[sa.Column[Any]]:
    return [
        sa.Column(f"{prefix}_turn", sa.Integer(), nullable=nullable),
        *(
            sa.Column(
                f"{prefix}_{name}",
                sa.SmallInteger(),
                server_default=None if nullable else sa.text("0"),
                nullable=nullable,
            )
            for name in POSITION[1:]
        ),
    ]


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
    for name, labels in ENUMS.items():
        postgresql.ENUM(*labels, name=name).create(bind)

    held_to = ", ".join(f"held_to_{n}" for n in POSITION)
    op.create_table(
        "beliefs",
        sa.Column("id", ULID, nullable=False),
        sa.Column("story_id", ULID, nullable=False),
        sa.Column("holder_character_id", ULID, nullable=False),
        sa.Column(
            "kind", _enum("belief_kind"), server_default=sa.text("'first_order'"), nullable=False
        ),
        sa.Column("proposition_id", ULID, nullable=False),
        sa.Column("polarity", sa.Text(), nullable=False),
        sa.Column("confidence_level", _enum("belief_confidence_level"), nullable=False),
        sa.Column("confidence_scalar", sa.NUMERIC(4, 3), nullable=True),
        sa.Column("valid_from", sa.BigInteger(), nullable=True),
        sa.Column("valid_to", sa.BigInteger(), nullable=True),
        sa.Column("valid_span", postgresql.INT8RANGE(), nullable=False),
        *_group("held_from", nullable=False),
        *_group("held_to", nullable=True),
        sa.Column("tx_from", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("tx_to", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("provenance", ULID, nullable=True),
        sa.Column("visibility", postgresql.JSONB(), nullable=False),
        *_derived(),
        sa.Column("beat", sa.SmallInteger(), nullable=True),
        sa.Column("step", sa.SmallInteger(), server_default=sa.text("0"), nullable=False),
        sa.CheckConstraint("polarity IN ('affirmed', 'negated')", name="ck_beliefs_polarity"),
        sa.CheckConstraint(
            "confidence_scalar IS NULL OR (confidence_scalar >= 0 AND confidence_scalar <= 1)",
            name="ck_beliefs_confidence_scalar",
        ),
        sa.CheckConstraint(
            "valid_from IS NULL OR valid_to IS NULL OR valid_from < valid_to",
            name="ck_beliefs_valid_order",
        ),
        sa.CheckConstraint(
            "valid_span = int8range(valid_from, valid_to, '[)')", name="ck_beliefs_valid_span"
        ),
        sa.CheckConstraint("tx_to IS NULL OR tx_from < tx_to", name="ck_beliefs_tx_order"),
        sa.CheckConstraint(f"num_nulls({held_to}) IN (0, 5)", name="ck_beliefs_held_to_whole"),
        sa.CheckConstraint(
            "held_to_turn IS NULL OR (held_from_turn, held_from_slot_k, held_from_beat, "
            "held_from_step) < (held_to_turn, held_to_slot_k, held_to_beat, held_to_step)",
            name="ck_beliefs_held_order",
        ),
        sa.CheckConstraint(SCOPES, name="ck_beliefs_visibility_scope"),
        sa.CheckConstraint(CHARACTERS, name="ck_beliefs_visibility_characters"),
        sa.CheckConstraint(HOLDER_ONLY, name="ck_beliefs_holder_only"),
        sa.ForeignKeyConstraint(
            ["operation_id"],
            ["operations.operation_id"],
            name="fk_beliefs_operation_id_operations",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_beliefs"),
    )
    op.create_index("ix_beliefs_story_operation", "beliefs", ["story_id", "operation_id"])
    op.create_index("ix_beliefs_story_membership", "beliefs", ["story_id", "membership"])
    op.create_index(
        "ix_beliefs_story_holder_proposition",
        "beliefs",
        ["story_id", "holder_character_id", "proposition_id"],
    )
    op.create_index("ix_beliefs_story_proposition", "beliefs", ["story_id", "proposition_id"])
    op.create_index("ix_beliefs_valid_span", "beliefs", ["valid_span"], postgresql_using="gist")
    op.create_index("ix_beliefs_visibility", "beliefs", ["visibility"], postgresql_using="gin")
    op.create_index(
        "ux_beliefs_open",
        "beliefs",
        ["story_id", "holder_character_id", "proposition_id"],
        unique=True,
        postgresql_where=sa.text(OPEN_WHERE),
    )
    op.execute(GUARD)
    for statement in TRIGGERS:
        op.execute(statement)

    op.create_table(
        "belief_state",
        sa.Column("story_id", ULID, nullable=False),
        sa.Column("holder_character_id", ULID, nullable=False),
        sa.Column("proposition_id", ULID, nullable=False),
        sa.Column("belief_id", ULID, nullable=False),
        sa.Column("polarity", sa.Text(), nullable=False),
        sa.Column("current_level", _enum("belief_confidence_level"), nullable=False),
        sa.Column("current_scalar", sa.NUMERIC(4, 3), nullable=True),
        *_group("since", nullable=False),
        sa.CheckConstraint("current_level <> 'unknown'", name="ck_belief_state_held_level"),
        sa.PrimaryKeyConstraint(
            "story_id", "holder_character_id", "proposition_id", name="pk_belief_state"
        ),
    )
    op.create_index(
        "ix_belief_state_story_proposition", "belief_state", ["story_id", "proposition_id"]
    )

    op.drop_constraint("ck_proposition_changes_holder", "proposition_changes", type_="check")
    op.drop_constraint("ck_proposition_changes_speaker", "proposition_changes", type_="check")
    op.create_check_constraint(
        "ck_proposition_changes_holder",
        "proposition_changes",
        "holder IN ('canon', 'utterance', 'belief')",
    )
    op.create_check_constraint(
        "ck_proposition_changes_speaker",
        "proposition_changes",
        "(holder IN ('utterance', 'belief')) = (speaker_id IS NOT NULL)",
    )


def downgrade() -> None:
    op.execute("DELETE FROM proposition_changes WHERE holder = 'belief'")
    op.drop_constraint("ck_proposition_changes_speaker", "proposition_changes", type_="check")
    op.drop_constraint("ck_proposition_changes_holder", "proposition_changes", type_="check")
    op.create_check_constraint(
        "ck_proposition_changes_holder",
        "proposition_changes",
        "holder IN ('canon', 'utterance')",
    )
    op.create_check_constraint(
        "ck_proposition_changes_speaker",
        "proposition_changes",
        "(holder = 'utterance') = (speaker_id IS NOT NULL)",
    )
    op.drop_table("belief_state")
    op.drop_table("beliefs")
    op.execute("DROP FUNCTION epistrel_beliefs_append_only()")
    bind = op.get_bind()
    for name in reversed(ENUMS):
        postgresql.ENUM(name=name).drop(bind)
