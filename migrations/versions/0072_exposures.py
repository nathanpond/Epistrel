"""exposures: what a non-salient witness heard, resolved lazily as of its frame (#164)

FR-BELIEF-5,6,7,12,13,14, FR-STORE-31 (docs/02 §4.4): ``core.tables.exposures`` describes the
table.

- The enums ``exposure_kind`` (``content``, ``envelope``), ``exposure_fidelity`` (``full``,
  ``partial``) and ``exposure_state`` (``unresolved``, ``resolved``).
- ``exposures``: one row per (witness, beat, proposition) a non-salient witness was exposed to,
  with the source, the beat's event, the exposure position (the five ``position_*`` columns;
  CHECK ``slot_k = 0 OR beat = 0``), the latest resolution (``state``,
  ``resolved_by_transition``, ``resolution_operation_id``; CHECK resolved iff an operation is
  named) and the derived-record columns of ``core.membership``. Append-only
  (``epistrel_exposures_append_only``): an UPDATE may change the resolution columns and the
  membership pair; a DELETE passes only under an erasure or the story's deletion.
- ``beliefs.resolution_operation_id``: the narrative turn that resolved a lazily resolved belief
  (its inclusion operation; ``NULL`` for every other belief).
- ``facts.step`` (NOT NULL, default 0): canon positions are refined to ``(turn, beat, step)``
  (FR-STORE-31), so an exposure frame bounds canon at its step.

Downgrade drops the table, the columns and the enums.

Revision ID: 0072
Revises: 0071
Create Date: 2026-10-23

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0072"
down_revision: str | None = "0071"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ULID = sa.CHAR(26)

ENUMS: dict[str, tuple[str, ...]] = {
    "exposure_kind": ("content", "envelope"),
    "exposure_fidelity": ("full", "partial"),
    "exposure_state": ("unresolved", "resolved"),
}

POSITION = ("turn", "variant", "slot_k", "beat", "step")

GUARD = """
CREATE OR REPLACE FUNCTION epistrel_exposures_append_only() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    mutable text[] := ARRAY['state', 'resolved_by_transition', 'resolution_operation_id',
                            'membership', 'membership_tx'];
BEGIN
    IF TG_OP = 'UPDATE' THEN
        IF (to_jsonb(NEW) - mutable) = (to_jsonb(OLD) - mutable) THEN
            RETURN NEW;
        END IF;
        RAISE EXCEPTION 'exposures are append-only: update of exposure % refused', OLD.id
            USING ERRCODE = 'EPI01';
    END IF;
    IF TG_OP = 'DELETE'
       AND (current_setting('epistrel.erasure', true) = 'on'
            OR current_setting('epistrel.allow_story_delete', true) = OLD.story_id::text) THEN
        RETURN OLD;
    END IF;
    IF TG_OP = 'TRUNCATE' THEN
        RAISE EXCEPTION 'exposures are append-only: TRUNCATE refused' USING ERRCODE = 'EPI01';
    END IF;
    RAISE EXCEPTION 'exposures are append-only: % of exposure % refused', TG_OP, OLD.id
        USING ERRCODE = 'EPI01';
END
$$;
"""

TRIGGERS = (
    "CREATE TRIGGER exposures_append_only BEFORE UPDATE OR DELETE ON exposures "
    "FOR EACH ROW EXECUTE FUNCTION epistrel_exposures_append_only()",
    "CREATE TRIGGER exposures_no_truncate BEFORE TRUNCATE ON exposures "
    "FOR EACH STATEMENT EXECUTE FUNCTION epistrel_exposures_append_only()",
)


def _enum(name: str) -> postgresql.ENUM:
    return postgresql.ENUM(name=name, create_type=False)


def upgrade() -> None:
    bind = op.get_bind()
    for name, labels in ENUMS.items():
        postgresql.ENUM(*labels, name=name).create(bind)
    op.create_table(
        "exposures",
        sa.Column("id", ULID, nullable=False),
        sa.Column("story_id", ULID, nullable=False),
        sa.Column("witness_character_id", ULID, nullable=False),
        sa.Column("proposition_id", ULID, nullable=False),
        sa.Column("source_character_id", ULID, nullable=True),
        sa.Column("source_event_id", ULID, nullable=False),
        sa.Column("kind", _enum("exposure_kind"), nullable=False),
        sa.Column("fidelity", _enum("exposure_fidelity"), nullable=False),
        sa.Column("position_turn", sa.Integer(), nullable=False),
        *(
            sa.Column(
                f"position_{name}", sa.SmallInteger(), server_default=sa.text("0"), nullable=False
            )
            for name in POSITION[1:]
        ),
        sa.Column(
            "state",
            _enum("exposure_state"),
            server_default=sa.text("'unresolved'"),
            nullable=False,
        ),
        sa.Column("resolved_by_transition", ULID, nullable=True),
        sa.Column("resolution_operation_id", ULID, nullable=True),
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
        sa.CheckConstraint(
            "position_slot_k = 0 OR position_beat = 0", name="ck_exposures_slot_shape"
        ),
        sa.CheckConstraint(
            "(state = 'resolved') = (resolution_operation_id IS NOT NULL)",
            name="ck_exposures_resolution_pair",
        ),
        sa.ForeignKeyConstraint(
            ["operation_id"],
            ["operations.operation_id"],
            name="fk_exposures_operation_id_operations",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_exposures"),
    )
    op.create_index("ix_exposures_story_operation", "exposures", ["story_id", "operation_id"])
    op.create_index("ix_exposures_story_membership", "exposures", ["story_id", "membership"])
    op.create_index(
        "ix_exposures_story_witness",
        "exposures",
        ["story_id", "witness_character_id", "state"],
    )
    op.create_index("ix_exposures_story_proposition", "exposures", ["story_id", "proposition_id"])
    op.execute(GUARD)
    for statement in TRIGGERS:
        op.execute(statement)
    op.add_column("beliefs", sa.Column("resolution_operation_id", ULID, nullable=True))
    op.add_column(
        "facts",
        sa.Column("step", sa.SmallInteger(), server_default=sa.text("0"), nullable=False),
    )


def downgrade() -> None:
    op.drop_column("facts", "step")
    op.drop_column("beliefs", "resolution_operation_id")
    op.drop_table("exposures")
    op.execute("DROP FUNCTION epistrel_exposures_append_only()")
    bind = op.get_bind()
    for name in reversed(ENUMS):
        postgresql.ENUM(name=name).drop(bind)
