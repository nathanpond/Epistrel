"""facts: bitemporal objective canon (#133)

FR-BITEMP-1..5, FR-STORE-9 (docs/02 §4.4, §4.5): ``core.tables.facts`` describes the table.

- The enum types ``record_provenance``, ``fact_authority``, ``holder_kind`` and
  ``fact_commit_state`` are created.
- ``facts`` is created with its checks, the derived-record columns and indexes of
  ``core.membership`` (``operation_id`` references ``operations``), a GiST index over
  ``valid_span``, the ``(story_id, subject_id, predicate_id, holder)`` index, the lineage and
  open-version indexes, and a GIN index over ``supersedes``.
- ``facts`` is append-only (``epistrel_facts_append_only``): an UPDATE may set ``tx_to`` and
  ``superseded_by`` once each and change ``membership``/``membership_tx`` (and ``visibility``
  under ``epistrel.erasure = on``); a DELETE passes only under an erasure or for the story
  ``epistrel.allow_story_delete`` names; TRUNCATE is refused.

Downgrade drops all of it.

Revision ID: 0049
Revises: 0048
Create Date: 2026-10-17

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0049"
down_revision: str | None = "0048"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ULID = sa.CHAR(26)

ENUMS: dict[str, tuple[str, ...]] = {
    "record_provenance": (
        "author",
        "seed",
        "system_consolidated",
        "in_world",
        "render",
        "director",
        "player",
        "external",
        "resolution",
        "backfill",
    ),
    "fact_authority": ("author", "engine", "external"),
    "holder_kind": ("canon",),
    "fact_commit_state": ("committed", "introduced", "pending"),
}

FACTS_GUARD = """
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
        END IF;
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

TRIGGERS = (
    "CREATE TRIGGER facts_append_only BEFORE UPDATE OR DELETE ON facts "
    "FOR EACH ROW EXECUTE FUNCTION epistrel_facts_append_only()",
    "CREATE TRIGGER facts_no_truncate BEFORE TRUNCATE ON facts "
    "FOR EACH STATEMENT EXECUTE FUNCTION epistrel_facts_append_only()",
)


def _enum(name: str) -> postgresql.ENUM:
    return postgresql.ENUM(name=name, create_type=False)


def upgrade() -> None:
    bind = op.get_bind()
    for name, labels in ENUMS.items():
        postgresql.ENUM(*labels, name=name).create(bind)

    op.create_table(
        "facts",
        sa.Column("id", ULID, nullable=False),
        sa.Column("story_id", ULID, nullable=False),
        sa.Column("lineage_id", ULID, nullable=False),
        sa.Column("proposition_id", ULID, nullable=True),
        sa.Column("closed_set_id", ULID, nullable=True),
        sa.Column("subject_id", ULID, nullable=False),
        sa.Column("predicate_id", ULID, nullable=False),
        sa.Column(
            "holder", _enum("holder_kind"), server_default=sa.text("'canon'"), nullable=False
        ),
        sa.Column("valid_from", sa.BigInteger(), nullable=True),
        sa.Column("valid_to", sa.BigInteger(), nullable=True),
        sa.Column("valid_from_inner", sa.BigInteger(), nullable=True),
        sa.Column("valid_to_inner", sa.BigInteger(), nullable=True),
        sa.Column("valid_span", postgresql.INT8RANGE(), nullable=False),
        sa.Column("tx_from", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("tx_to", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("provenance", _enum("record_provenance"), nullable=False),
        sa.Column("authority", _enum("fact_authority"), nullable=False),
        sa.Column("author_locked", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column(
            "visibility",
            postgresql.JSONB(),
            server_default=sa.text("""'{"scope": "objective"}'::jsonb"""),
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
            "membership", _enum("membership"), server_default=sa.text("'active'"), nullable=False
        ),
        sa.Column("membership_tx", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("beat", sa.SmallInteger(), nullable=True),
        sa.Column("slot_k", sa.SmallInteger(), nullable=True),
        sa.Column("superseded_by", ULID, nullable=True),
        sa.Column(
            "supersedes", postgresql.ARRAY(ULID), server_default=sa.text("'{}'"), nullable=False
        ),
        sa.Column("anchored_to", ULID, nullable=True),
        sa.Column(
            "commit_state",
            _enum("fact_commit_state"),
            server_default=sa.text("'committed'"),
            nullable=False,
        ),
        sa.Column("committed_at_position", postgresql.JSONB(), nullable=True),
        sa.Column("thunk_id", ULID, nullable=True),
        sa.Column("resolution_level", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "num_nonnulls(proposition_id, closed_set_id) = 1", name="ck_facts_one_wrapped"
        ),
        sa.CheckConstraint(
            "valid_from IS NULL OR valid_to IS NULL OR valid_from < valid_to",
            name="ck_facts_valid_order",
        ),
        sa.CheckConstraint(
            "(valid_from IS NULL) = (valid_from_inner IS NULL) "
            "AND (valid_to IS NULL) = (valid_to_inner IS NULL) "
            "AND (valid_from IS NULL OR valid_from <= valid_from_inner) "
            "AND (valid_to IS NULL OR valid_to_inner <= valid_to)",
            name="ck_facts_inner_bounds",
        ),
        sa.CheckConstraint(
            "valid_span = int8range(valid_from, valid_to, '[)')", name="ck_facts_valid_span"
        ),
        sa.CheckConstraint("tx_to IS NULL OR tx_from < tx_to", name="ck_facts_tx_order"),
        sa.CheckConstraint(
            "visibility->>'scope' IN ('objective', 'public', 'engine', 'characters')",
            name="ck_facts_visibility_scope",
        ),
        sa.CheckConstraint(
            "(visibility->>'scope' = 'characters') = "
            "(jsonb_typeof(visibility->'characters') = 'array' "
            "AND jsonb_array_length(visibility->'characters') > 0)",
            name="ck_facts_visibility_characters",
        ),
        sa.ForeignKeyConstraint(
            ["operation_id"],
            ["operations.operation_id"],
            name="fk_facts_operation_id_operations",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_facts"),
    )
    op.create_index("ix_facts_story_operation", "facts", ["story_id", "operation_id"])
    op.create_index("ix_facts_story_membership", "facts", ["story_id", "membership"])
    op.create_index(
        "ix_facts_story_subject_predicate",
        "facts",
        ["story_id", "subject_id", "predicate_id", "holder"],
    )
    op.create_index("ix_facts_valid_span", "facts", ["valid_span"], postgresql_using="gist")
    op.create_index("ix_facts_story_lineage", "facts", ["story_id", "lineage_id"])
    op.create_index(
        "ix_facts_story_open", "facts", ["story_id"], postgresql_where=sa.text("tx_to IS NULL")
    )
    op.create_index("ix_facts_supersedes", "facts", ["supersedes"], postgresql_using="gin")

    op.execute(FACTS_GUARD)
    for statement in TRIGGERS:
        op.execute(statement)


def downgrade() -> None:
    op.drop_table("facts")
    op.execute("DROP FUNCTION epistrel_facts_append_only()")
    bind = op.get_bind()
    for name in reversed(ENUMS):
        postgresql.ENUM(name=name).drop(bind)
