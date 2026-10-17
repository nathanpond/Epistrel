"""memories: episodic, semantic and reflection memory records (#135)

FR-MEM-1,2,4,5,7,8, FR-STORE-9 (docs/02 §4.2, §4.4): ``core.tables.memories`` describes the
table.

- The enum types ``memory_kind``, ``memory_trust_tier`` and ``retrieval_tier`` are created
  (``record_provenance`` is 0049's).
- ``memories`` is created with its checks, the derived-record columns and indexes of
  ``core.membership`` (``operation_id`` references ``operations``), the
  ``(story_id, membership, retrieval_tier)`` and ``(story_id, holder_character_id)`` indexes
  and a GIN index over ``source_event_ids``. ``embedding`` is an untyped pgvector ``vector``
  (0001 created the extension): #138 pins the dimension, retypes the column and creates the
  HNSW cosine index when the first vector is written.
- ``memories`` is guarded (``epistrel_memories_guard``): an UPDATE may change only the
  importance, access, retrieval-tier, embedding and membership columns (and ``text`` under
  ``epistrel.erasure = on``); a DELETE passes only under an erasure or for the story
  ``epistrel.allow_story_delete`` names; TRUNCATE is refused.

Downgrade drops all of it.

Revision ID: 0050
Revises: 0049
Create Date: 2026-10-17

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0050"
down_revision: str | None = "0049"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ULID = sa.CHAR(26)

ENUMS: dict[str, tuple[str, ...]] = {
    "memory_kind": ("episodic", "semantic", "reflection"),
    "memory_trust_tier": ("author", "external", "engine", "in_world"),
    "retrieval_tier": ("hot", "retired"),
}

MEMORIES_GUARD = """
CREATE OR REPLACE FUNCTION epistrel_memories_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    mutable text[] := ARRAY['importance', 'importance_at', 'last_accessed_at', 'retrieval_tier',
                            'embedding', 'embedding_model_version', 'membership',
                            'membership_tx'];
BEGIN
    IF TG_OP = 'UPDATE' THEN
        IF (to_jsonb(NEW) - mutable) = (to_jsonb(OLD) - mutable) THEN
            RETURN NEW;
        END IF;
        IF current_setting('epistrel.erasure', true) = 'on'
           AND (to_jsonb(NEW) - mutable - 'text') = (to_jsonb(OLD) - mutable - 'text') THEN
            RETURN NEW;
        END IF;
        RAISE EXCEPTION 'memories: update of memory % refused', OLD.id
            USING ERRCODE = 'EPI01';
    END IF;
    IF TG_OP = 'DELETE'
       AND (current_setting('epistrel.erasure', true) = 'on'
            OR current_setting('epistrel.allow_story_delete', true) = OLD.story_id::text) THEN
        RETURN OLD;
    END IF;
    IF TG_OP = 'TRUNCATE' THEN
        RAISE EXCEPTION 'memories: TRUNCATE refused' USING ERRCODE = 'EPI01';
    END IF;
    RAISE EXCEPTION 'memories: % of memory % refused', TG_OP, OLD.id USING ERRCODE = 'EPI01';
END
$$;
"""

TRIGGERS = (
    "CREATE TRIGGER memories_guard BEFORE UPDATE OR DELETE ON memories "
    "FOR EACH ROW EXECUTE FUNCTION epistrel_memories_guard()",
    "CREATE TRIGGER memories_no_truncate BEFORE TRUNCATE ON memories "
    "FOR EACH STATEMENT EXECUTE FUNCTION epistrel_memories_guard()",
)


def _enum(name: str) -> postgresql.ENUM:
    return postgresql.ENUM(name=name, create_type=False)


def upgrade() -> None:
    bind = op.get_bind()
    for name, labels in ENUMS.items():
        postgresql.ENUM(*labels, name=name).create(bind)

    op.create_table(
        "memories",
        sa.Column("id", ULID, nullable=False),
        sa.Column("story_id", ULID, nullable=False),
        sa.Column("kind", _enum("memory_kind"), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("holder_character_id", ULID, nullable=True),
        sa.Column("character_id", ULID, nullable=True),
        sa.Column(
            "visibility",
            postgresql.JSONB(),
            server_default=sa.text("""'{"scope": "objective"}'::jsonb"""),
            nullable=False,
        ),
        sa.Column("provenance", _enum("record_provenance"), nullable=False),
        sa.Column("trust_tier", _enum("memory_trust_tier"), nullable=False),
        sa.Column("importance", sa.REAL(), nullable=True),
        sa.Column("importance_at", sa.Integer(), nullable=True),
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
        sa.Column(
            "proposition_ids",
            postgresql.ARRAY(ULID),
            server_default=sa.text("'{}'"),
            nullable=False,
        ),
        sa.Column(
            "retrieval_tier",
            _enum("retrieval_tier"),
            server_default=sa.text("'hot'"),
            nullable=False,
        ),
        sa.Column("embedding_model_version", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("transaction_timestamp()"),
            nullable=False,
        ),
        sa.Column("last_accessed_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.CheckConstraint("char_length(text) <= 2000", name="ck_memories_text_length"),
        sa.CheckConstraint(
            "importance IS NULL OR (importance >= 0 AND importance <= 1)",
            name="ck_memories_importance",
        ),
        sa.CheckConstraint(
            "visibility->>'scope' IN ('objective', 'public', 'engine', 'characters')",
            name="ck_memories_visibility_scope",
        ),
        sa.CheckConstraint(
            "(visibility->>'scope' = 'characters') = "
            "(jsonb_typeof(visibility->'characters') = 'array' "
            "AND jsonb_array_length(visibility->'characters') > 0)",
            name="ck_memories_visibility_characters",
        ),
        sa.ForeignKeyConstraint(
            ["operation_id"],
            ["operations.operation_id"],
            name="fk_memories_operation_id_operations",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_memories"),
    )
    # pgvector's type, untyped until #138 pins the embedding dimension.
    op.execute("ALTER TABLE memories ADD COLUMN embedding vector")
    op.create_index("ix_memories_story_operation", "memories", ["story_id", "operation_id"])
    op.create_index("ix_memories_story_membership", "memories", ["story_id", "membership"])
    op.create_index(
        "ix_memories_story_membership_tier",
        "memories",
        ["story_id", "membership", "retrieval_tier"],
    )
    op.create_index("ix_memories_story_holder", "memories", ["story_id", "holder_character_id"])
    op.create_index(
        "ix_memories_source_event_ids",
        "memories",
        ["source_event_ids"],
        postgresql_using="gin",
    )

    op.execute(MEMORIES_GUARD)
    for statement in TRIGGERS:
        op.execute(statement)


def downgrade() -> None:
    op.drop_table("memories")
    op.execute("DROP FUNCTION epistrel_memories_guard()")
    bind = op.get_bind()
    for name in reversed(ENUMS):
        postgresql.ENUM(name=name).drop(bind)
