"""nested beliefs, the full speech-act shape and the attribution label (#167)

FR-BELIEF-15, FR-EPI-2, FR-TURN-6 (docs/02 §4.4): ``core.tables.beliefs`` and
``core.tables.utterances`` describe the changes.

- ``beliefs`` gains the nested shape: ``attributed_holder_id`` (whom the holder attributes the
  inner belief to), ``inner_belief_id`` (a deferred foreign key to ``beliefs``: the inner
  ``attributed`` or ``speech_act`` row), ``depth`` (first-order 1; each attribution or ``said``
  wrapper adds one), ``attributed_confidence_level``/``_scalar`` (how sure the holder thinks the
  attributed holder is) and ``attributed_held_from``/``_to`` (when, in story-minutes, the holder
  thinks the attributed holder holds it); and the full speech-act shape: ``speaker_character_id``,
  ``utterance_id`` (a soft reference), ``staged_ref`` and ``perceived_intent``. #162's
  ``speech_act`` rows are backfilled (their speaker is their producing transition's source;
  ``depth`` 2). CHECKs keep each kind's shape.
- ``nested_belief_state``: the rebuildable projection of the open nested beliefs (primary key
  ``(story_id, belief_id)``, an index by ``(story_id, holder, attributed holder,
  proposition)``), no trigger.
- ``utterances.attribution_objective_relation`` (nullable, ``true``/``false``/``unresolved``):
  whether the attributed holders of the utterance's attribution claims actually believe what
  was attributed, written at insert (the append-only guard is unchanged).

Downgrade drops the table and the columns (nested, attributed and the backfilled speech-act
columns are lost; the rows stay).

Revision ID: 0074
Revises: 0073
Create Date: 2026-10-23

"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0074"
down_revision: str | None = "0073"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ULID = sa.CHAR(26)

POSITION = ("turn", "variant", "slot_k", "beat", "step")

INTENTS = ("sincere", "joking", "deceptive")
RELATIONS = ("true", "false", "unresolved")

KIND_SHAPE = (
    "(kind = 'first_order' AND attributed_holder_id IS NULL AND inner_belief_id IS NULL "
    "AND speaker_character_id IS NULL AND depth = 1) "
    "OR (kind = 'attributed' AND attributed_holder_id IS NOT NULL AND depth >= 2) "
    "OR (kind = 'nested' AND attributed_holder_id IS NOT NULL AND inner_belief_id IS NOT NULL "
    "AND attributed_holder_id <> holder_character_id AND depth >= 2) "
    "OR (kind = 'speech_act' AND speaker_character_id IS NOT NULL AND depth >= 2)"
)


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def upgrade() -> None:
    columns: tuple[sa.Column[Any], ...] = (
        sa.Column("attributed_holder_id", ULID, nullable=True),
        sa.Column("inner_belief_id", ULID, nullable=True),
        sa.Column("depth", sa.SmallInteger(), server_default=sa.text("1"), nullable=False),
        sa.Column(
            "attributed_confidence_level",
            postgresql.ENUM(name="belief_confidence_level", create_type=False),
            nullable=True,
        ),
        sa.Column("attributed_confidence_scalar", sa.NUMERIC(4, 3), nullable=True),
        sa.Column("attributed_held_from", sa.BigInteger(), nullable=True),
        sa.Column("attributed_held_to", sa.BigInteger(), nullable=True),
        sa.Column("speaker_character_id", ULID, nullable=True),
        sa.Column("utterance_id", ULID, nullable=True),
        sa.Column("staged_ref", postgresql.JSONB(), nullable=True),
        sa.Column("perceived_intent", sa.Text(), nullable=True),
    )
    for column in columns:
        op.add_column("beliefs", column)
    # #162's speech-act rows: their speaker is their producing transition's source.
    op.execute("ALTER TABLE beliefs DISABLE TRIGGER beliefs_append_only")
    op.execute(
        "UPDATE beliefs b SET speaker_character_id = t.source_character_id, depth = 2 "
        "FROM belief_transitions t WHERE t.id = b.provenance AND b.kind = 'speech_act'"
    )
    op.execute("ALTER TABLE beliefs ENABLE TRIGGER beliefs_append_only")
    op.create_foreign_key(
        "fk_beliefs_inner_belief_id_beliefs",
        "beliefs",
        "beliefs",
        ["inner_belief_id"],
        ["id"],
        deferrable=True,
        initially="DEFERRED",
    )
    op.create_check_constraint("ck_beliefs_kind_shape", "beliefs", KIND_SHAPE)
    op.create_check_constraint("ck_beliefs_depth", "beliefs", "depth >= 1 AND depth <= 6")
    op.create_check_constraint(
        "ck_beliefs_attributed_confidence_scalar",
        "beliefs",
        "attributed_confidence_scalar IS NULL OR "
        "(attributed_confidence_scalar >= 0 AND attributed_confidence_scalar <= 1)",
    )
    op.create_check_constraint(
        "ck_beliefs_attributed_held_order",
        "beliefs",
        "attributed_held_from IS NULL OR attributed_held_to IS NULL "
        "OR attributed_held_from < attributed_held_to",
    )
    op.create_check_constraint(
        "ck_beliefs_perceived_intent",
        "beliefs",
        f"perceived_intent IS NULL OR {_in('perceived_intent', INTENTS)}",
    )
    op.create_index(
        "ix_beliefs_story_holder_attributed",
        "beliefs",
        ["story_id", "holder_character_id", "attributed_holder_id"],
    )
    op.create_index("ix_beliefs_inner", "beliefs", ["inner_belief_id"])

    op.create_table(
        "nested_belief_state",
        sa.Column("story_id", ULID, nullable=False),
        sa.Column("belief_id", ULID, nullable=False),
        sa.Column("holder_character_id", ULID, nullable=False),
        sa.Column("attributed_holder_id", ULID, nullable=False),
        sa.Column("proposition_id", ULID, nullable=False),
        sa.Column("polarity", sa.Text(), nullable=False),
        sa.Column("depth", sa.SmallInteger(), nullable=False),
        sa.Column(
            "current_level",
            postgresql.ENUM(name="belief_confidence_level", create_type=False),
            nullable=False,
        ),
        sa.Column("current_scalar", sa.NUMERIC(4, 3), nullable=True),
        sa.Column(
            "attributed_level",
            postgresql.ENUM(name="belief_confidence_level", create_type=False),
            nullable=True,
        ),
        sa.Column("attributed_scalar", sa.NUMERIC(4, 3), nullable=True),
        sa.Column("since_turn", sa.Integer(), nullable=False),
        *(
            sa.Column(
                f"since_{name}", sa.SmallInteger(), server_default=sa.text("0"), nullable=False
            )
            for name in POSITION[1:]
        ),
        sa.CheckConstraint("current_level <> 'unknown'", name="ck_nested_belief_state_held_level"),
        sa.PrimaryKeyConstraint("story_id", "belief_id", name="pk_nested_belief_state"),
    )
    op.create_index(
        "ix_nested_belief_state_story_holder_attributed",
        "nested_belief_state",
        ["story_id", "holder_character_id", "attributed_holder_id", "proposition_id"],
    )

    op.add_column(
        "utterances", sa.Column("attribution_objective_relation", sa.Text(), nullable=True)
    )
    op.create_check_constraint(
        "ck_utterances_attribution_objective_relation",
        "utterances",
        _in("attribution_objective_relation", RELATIONS),
    )


def downgrade() -> None:
    op.drop_constraint("ck_utterances_attribution_objective_relation", "utterances", type_="check")
    op.drop_column("utterances", "attribution_objective_relation")
    op.drop_table("nested_belief_state")
    op.drop_index("ix_beliefs_inner", table_name="beliefs")
    op.drop_index("ix_beliefs_story_holder_attributed", table_name="beliefs")
    for name in (
        "ck_beliefs_perceived_intent",
        "ck_beliefs_attributed_held_order",
        "ck_beliefs_attributed_confidence_scalar",
        "ck_beliefs_depth",
        "ck_beliefs_kind_shape",
    ):
        op.drop_constraint(name, "beliefs", type_="check")
    op.drop_constraint("fk_beliefs_inner_belief_id_beliefs", "beliefs", type_="foreignkey")
    for name in (
        "perceived_intent",
        "staged_ref",
        "utterance_id",
        "speaker_character_id",
        "attributed_held_to",
        "attributed_held_from",
        "attributed_confidence_scalar",
        "attributed_confidence_level",
        "depth",
        "inner_belief_id",
        "attributed_holder_id",
    ):
        op.drop_column("beliefs", name)
