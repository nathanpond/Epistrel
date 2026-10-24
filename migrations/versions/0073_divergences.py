"""divergences and the utterance classification (#166)

FR-TURN-6,7, FR-VERIFY-5,10 (docs/02 §4.9): ``core.tables.divergences`` and
``core.tables.utterances`` describe the changes.

- ``utterances.classification`` (nullable ``text``, CHECK ``sincere``, ``sincere_error``,
  ``lie``, ``joke``, ``hallucination_repaired``): the verifier's classification of the
  utterance, written at insert. The append-only guard is unchanged: the column is set when the
  row is written and never updated.
- ``divergences``: one row per lie claim (the utterance, its speaker, the asserted proposition,
  the contrary belief and its proposition, the say beat's five-column position, the reveal) with
  the derived-record columns of ``core.membership``; ``believed_belief_id`` is a deferred foreign
  key to ``beliefs``. Append-only (``epistrel_divergences_append_only``): an UPDATE may change
  ``revealed_by_event_id`` and the membership pair; a DELETE passes only under an erasure or the
  story's deletion.

Downgrade drops the table, its guard and the column.

Revision ID: 0073
Revises: 0072
Create Date: 2026-10-23

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0073"
down_revision: str | None = "0072"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ULID = sa.CHAR(26)

POSITION = ("turn", "variant", "slot_k", "beat", "step")

CLASSIFICATIONS = ("sincere", "sincere_error", "lie", "joke", "hallucination_repaired")

GUARD = """
CREATE OR REPLACE FUNCTION epistrel_divergences_append_only() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    mutable text[] := ARRAY['revealed_by_event_id', 'membership', 'membership_tx'];
BEGIN
    IF TG_OP = 'UPDATE' THEN
        IF (to_jsonb(NEW) - mutable) = (to_jsonb(OLD) - mutable) THEN
            RETURN NEW;
        END IF;
        RAISE EXCEPTION 'divergences are append-only: update of divergence % refused', OLD.id
            USING ERRCODE = 'EPI01';
    END IF;
    IF TG_OP = 'DELETE'
       AND (current_setting('epistrel.erasure', true) = 'on'
            OR current_setting('epistrel.allow_story_delete', true) = OLD.story_id::text) THEN
        RETURN OLD;
    END IF;
    IF TG_OP = 'TRUNCATE' THEN
        RAISE EXCEPTION 'divergences are append-only: TRUNCATE refused' USING ERRCODE = 'EPI01';
    END IF;
    RAISE EXCEPTION 'divergences are append-only: % of divergence % refused', TG_OP, OLD.id
        USING ERRCODE = 'EPI01';
END
$$;
"""

TRIGGERS = (
    "CREATE TRIGGER divergences_append_only BEFORE UPDATE OR DELETE ON divergences "
    "FOR EACH ROW EXECUTE FUNCTION epistrel_divergences_append_only()",
    "CREATE TRIGGER divergences_no_truncate BEFORE TRUNCATE ON divergences "
    "FOR EACH STATEMENT EXECUTE FUNCTION epistrel_divergences_append_only()",
)


def upgrade() -> None:
    op.add_column("utterances", sa.Column("classification", sa.Text(), nullable=True))
    op.create_check_constraint(
        "ck_utterances_classification",
        "utterances",
        f"classification IN ({', '.join(repr(c) for c in CLASSIFICATIONS)})",
    )
    op.create_table(
        "divergences",
        sa.Column("id", ULID, nullable=False),
        sa.Column("story_id", ULID, nullable=False),
        sa.Column("utterance_id", ULID, nullable=False),
        sa.Column("speaker_character_id", ULID, nullable=False),
        sa.Column("asserted_proposition_id", ULID, nullable=False),
        sa.Column("believed_proposition_id", ULID, nullable=True),
        sa.Column("believed_belief_id", ULID, nullable=True),
        sa.Column("position_turn", sa.Integer(), nullable=False),
        *(
            sa.Column(
                f"position_{name}", sa.SmallInteger(), server_default=sa.text("0"), nullable=False
            )
            for name in POSITION[1:]
        ),
        sa.Column("revealed_by_event_id", ULID, nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
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
        sa.ForeignKeyConstraint(
            ["operation_id"],
            ["operations.operation_id"],
            name="fk_divergences_operation_id_operations",
        ),
        sa.ForeignKeyConstraint(
            ["believed_belief_id"],
            ["beliefs.id"],
            name="fk_divergences_believed_belief_id_beliefs",
            deferrable=True,
            initially="DEFERRED",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_divergences"),
    )
    op.create_index("ix_divergences_story_operation", "divergences", ["story_id", "operation_id"])
    op.create_index("ix_divergences_story_membership", "divergences", ["story_id", "membership"])
    op.create_index(
        "ix_divergences_story_speaker", "divergences", ["story_id", "speaker_character_id"]
    )
    op.create_index("ix_divergences_story_utterance", "divergences", ["story_id", "utterance_id"])
    op.execute(GUARD)
    for statement in TRIGGERS:
        op.execute(statement)


def downgrade() -> None:
    op.drop_table("divergences")
    op.execute("DROP FUNCTION epistrel_divergences_append_only()")
    op.drop_constraint("ck_utterances_classification", "utterances", type_="check")
    op.drop_column("utterances", "classification")
