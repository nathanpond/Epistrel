"""entities, the predicate registry, propositions, closed sets and proposals (#131)

FR-PROP-1..8 (docs/02 §4.9): ``core.tables.propositions`` describes each table.

- ``entities``, ``entity_aliases``, ``predicates``, ``propositions``, ``closed_sets`` and
  ``predicate_proposals`` are created, with their checks and indexes (``propositions`` has its
  ``(story_id, subject_id, predicate_id)`` index and a GiST index over ``valid_span``).
- The core vocabulary (``core.propositions.vocabulary``) is inserted with its fixed ids
  (``story_id`` null, ``layer = core``); every start re-syncs it (``registry.sync_core``).
- ``propositions`` is append-only (``epistrel_propositions_append_only``): an UPDATE passes only
  under ``epistrel.erasure = on`` and changes ``literal`` and ``qualifiers`` alone, a DELETE only
  for the story ``epistrel.allow_story_delete`` names; TRUNCATE is refused. ``predicates`` keeps
  its identity (``epistrel_predicates_identity``): ``id``, ``story_id`` and ``layer`` never
  change, nor the ``name`` outside an erasure; a core row is never deleted, a story row only
  with its story.
- ``stories`` gains ``story_epoch`` (nullable), ``story_minute_zero`` (0) and
  ``predicate_rules`` (``[]``).

Downgrade drops all of it.

Revision ID: 0048
Revises: 0047
Create Date: 2026-10-17

"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from epistrel.core.propositions.codec import predicate_row
from epistrel.core.propositions.vocabulary import CORE_PREDICATES

revision: str = "0048"
down_revision: str | None = "0047"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ULID = sa.CHAR(26)


def _created() -> sa.Column[Any]:
    return sa.Column(
        "created_at",
        sa.TIMESTAMP(timezone=True),
        server_default=sa.text("transaction_timestamp()"),
        nullable=False,
    )


PROPOSITIONS_GUARD = """
CREATE OR REPLACE FUNCTION epistrel_propositions_append_only() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    redactable text[] := ARRAY['literal', 'qualifiers'];
BEGIN
    IF TG_OP = 'UPDATE'
       AND current_setting('epistrel.erasure', true) = 'on'
       AND (to_jsonb(NEW) - redactable) = (to_jsonb(OLD) - redactable) THEN
        RETURN NEW;
    END IF;
    IF TG_OP = 'DELETE'
       AND current_setting('epistrel.allow_story_delete', true) = OLD.story_id::text THEN
        RETURN OLD;
    END IF;
    IF TG_OP = 'TRUNCATE' THEN
        RAISE EXCEPTION 'propositions are append-only: TRUNCATE refused'
            USING ERRCODE = 'EPI01';
    END IF;
    RAISE EXCEPTION 'propositions are append-only: % of proposition % refused', TG_OP, OLD.id
        USING ERRCODE = 'EPI01';
END
$$;
"""

PREDICATES_GUARD = """
CREATE OR REPLACE FUNCTION epistrel_predicates_identity() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'UPDATE' THEN
        IF NEW.id = OLD.id
           AND NEW.story_id IS NOT DISTINCT FROM OLD.story_id
           AND NEW.layer = OLD.layer
           AND (NEW.name = OLD.name OR current_setting('epistrel.erasure', true) = 'on') THEN
            RETURN NEW;
        END IF;
        RAISE EXCEPTION 'a predicate keeps its id, scope, layer and name: update of % refused',
            OLD.id USING ERRCODE = 'EPI01';
    END IF;
    IF TG_OP = 'DELETE' AND OLD.story_id IS NOT NULL
       AND current_setting('epistrel.allow_story_delete', true) = OLD.story_id::text THEN
        RETURN OLD;
    END IF;
    IF TG_OP = 'TRUNCATE' THEN
        RAISE EXCEPTION 'predicates: TRUNCATE refused' USING ERRCODE = 'EPI01';
    END IF;
    RAISE EXCEPTION 'predicates: delete of % refused', OLD.id USING ERRCODE = 'EPI01';
END
$$;
"""

TRIGGERS = (
    "CREATE TRIGGER propositions_append_only BEFORE UPDATE OR DELETE ON propositions "
    "FOR EACH ROW EXECUTE FUNCTION epistrel_propositions_append_only()",
    "CREATE TRIGGER propositions_no_truncate BEFORE TRUNCATE ON propositions "
    "FOR EACH STATEMENT EXECUTE FUNCTION epistrel_propositions_append_only()",
    "CREATE TRIGGER predicates_identity BEFORE UPDATE OR DELETE ON predicates "
    "FOR EACH ROW EXECUTE FUNCTION epistrel_predicates_identity()",
    "CREATE TRIGGER predicates_no_truncate BEFORE TRUNCATE ON predicates "
    "FOR EACH STATEMENT EXECUTE FUNCTION epistrel_predicates_identity()",
)


def upgrade() -> None:
    op.create_table(
        "entities",
        sa.Column("id", ULID, nullable=False),
        sa.Column("story_id", ULID, nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("canonical_name", sa.Text(), nullable=False),
        sa.Column("character_id", ULID, nullable=True),
        _created(),
        sa.PrimaryKeyConstraint("id", name="pk_entities"),
    )
    op.create_index("ix_entities_story_id", "entities", ["story_id"])
    op.create_table(
        "entity_aliases",
        sa.Column("id", ULID, nullable=False),
        sa.Column("story_id", ULID, nullable=False),
        sa.Column("entity_id", ULID, nullable=False),
        sa.Column("alias", sa.Text(), nullable=False),
        _created(),
        sa.PrimaryKeyConstraint("id", name="pk_entity_aliases"),
    )
    op.create_index("ix_entity_aliases_story_id", "entity_aliases", ["story_id", "entity_id"])

    op.create_table(
        "predicates",
        sa.Column("id", ULID, nullable=False),
        sa.Column("story_id", ULID, nullable=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("layer", sa.Text(), nullable=False),
        sa.Column("subject_type", sa.Text(), server_default=sa.text("'any'"), nullable=False),
        sa.Column("object_type", sa.Text(), server_default=sa.text("'any'"), nullable=False),
        sa.Column("cardinality", sa.Text(), nullable=False),
        sa.Column("max_cardinality", sa.Integer(), nullable=True),
        sa.Column(
            "key_qualifiers",
            postgresql.ARRAY(sa.Text()),
            server_default=sa.text("'{}'::text[]"),
            nullable=False,
        ),
        sa.Column("qualifiers", postgresql.JSONB(), nullable=True),
        sa.Column("set_object", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("temporal_kind", sa.Text(), nullable=False),
        sa.Column(
            "exclusions", postgresql.JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False
        ),
        sa.Column("containment_of", ULID, nullable=True),
        sa.Column(
            "implications",
            postgresql.JSONB(),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("symmetric", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("inverse_of", ULID, nullable=True),
        sa.Column("owner", sa.Text(), server_default=sa.text("'story'"), nullable=False),
        sa.Column("projection", sa.Text(), nullable=True),
        sa.Column("docs", sa.Text(), server_default=sa.text("''"), nullable=False),
        _created(),
        sa.CheckConstraint("layer IN ('core', 'author', 'auto')", name="ck_predicates_layer"),
        sa.CheckConstraint(
            "(layer = 'core') = (story_id IS NULL)", name="ck_predicates_core_scope"
        ),
        sa.CheckConstraint(
            "cardinality IN ('functional', 'multi')", name="ck_predicates_cardinality"
        ),
        sa.CheckConstraint(
            "cardinality <> 'functional' OR max_cardinality = 1",
            name="ck_predicates_functional_maximum",
        ),
        sa.CheckConstraint(
            "max_cardinality IS NULL OR max_cardinality >= 1", name="ck_predicates_maximum"
        ),
        sa.CheckConstraint(
            "temporal_kind IN ('permanent', 'fluent', 'event')",
            name="ck_predicates_temporal_kind",
        ),
        sa.CheckConstraint("owner IN ('story', 'external')", name="ck_predicates_owner"),
        sa.PrimaryKeyConstraint("id", name="pk_predicates"),
    )
    op.create_index(
        "uq_predicates_scope_name",
        "predicates",
        [sa.text("COALESCE(story_id, 'core')"), "name"],
        unique=True,
    )

    op.create_table(
        "propositions",
        sa.Column("id", ULID, nullable=False),
        sa.Column("story_id", ULID, nullable=False),
        sa.Column("subject_id", ULID, nullable=False),
        sa.Column("predicate_id", ULID, nullable=False),
        sa.Column("object_id", ULID, nullable=True),
        sa.Column("literal", postgresql.JSONB(), nullable=True),
        sa.Column("object_set", postgresql.JSONB(), nullable=True),
        sa.Column(
            "qualifiers", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False
        ),
        sa.Column("polarity", sa.Text(), nullable=False),
        sa.Column("valid_from_inner", sa.BigInteger(), nullable=True),
        sa.Column("valid_from_outer", sa.BigInteger(), nullable=True),
        sa.Column("valid_to_inner", sa.BigInteger(), nullable=True),
        sa.Column("valid_to_outer", sa.BigInteger(), nullable=True),
        sa.Column("from_exact", sa.Boolean(), nullable=False),
        sa.Column("to_exact", sa.Boolean(), nullable=False),
        sa.Column("instant_granularity", sa.Text(), nullable=True),
        sa.Column("valid_span", postgresql.INT8RANGE(), nullable=False),
        sa.Column("event_id", ULID, nullable=True),
        _created(),
        sa.CheckConstraint("polarity IN ('affirmed', 'negated')", name="ck_propositions_polarity"),
        sa.CheckConstraint(
            "num_nonnulls(object_id, literal, object_set) <= 1", name="ck_propositions_one_object"
        ),
        sa.CheckConstraint(
            "NOT from_exact OR valid_from_inner IS NOT DISTINCT FROM valid_from_outer",
            name="ck_propositions_from_exact",
        ),
        sa.CheckConstraint(
            "NOT to_exact OR valid_to_inner IS NOT DISTINCT FROM valid_to_outer",
            name="ck_propositions_to_exact",
        ),
        sa.CheckConstraint(
            "instant_granularity IS NULL OR (instant_granularity IN "
            "('minute', 'hour', 'day', 'month', 'year') AND valid_from_inner IS NOT NULL "
            "AND valid_to_inner IS NULL AND valid_to_outer IS NULL)",
            name="ck_propositions_instant",
        ),
        sa.CheckConstraint("NOT isempty(valid_span)", name="ck_propositions_span_not_empty"),
        sa.PrimaryKeyConstraint("id", name="pk_propositions"),
    )
    op.create_index(
        "ix_propositions_story_subject_predicate",
        "propositions",
        ["story_id", "subject_id", "predicate_id"],
    )
    op.create_index(
        "ix_propositions_valid_span", "propositions", ["valid_span"], postgresql_using="gist"
    )

    op.create_table(
        "closed_sets",
        sa.Column("id", ULID, nullable=False),
        sa.Column("story_id", ULID, nullable=False),
        sa.Column("predicate_id", ULID, nullable=False),
        sa.Column("key", postgresql.JSONB(), nullable=False),
        sa.Column("members", postgresql.JSONB(), nullable=False),
        sa.Column("valid_from", sa.BigInteger(), nullable=True),
        sa.Column("valid_to", sa.BigInteger(), nullable=True),
        sa.Column("asserted_by", sa.Text(), nullable=False),
        _created(),
        sa.CheckConstraint("asserted_by IN ('author', 'canon')", name="ck_closed_sets_asserted_by"),
        sa.CheckConstraint(
            "valid_from IS NULL OR valid_to IS NULL OR valid_from < valid_to",
            name="ck_closed_sets_interval",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_closed_sets"),
    )
    op.create_index("ix_closed_sets_story_predicate", "closed_sets", ["story_id", "predicate_id"])

    op.create_table(
        "predicate_proposals",
        sa.Column("id", ULID, nullable=False),
        sa.Column("story_id", ULID, nullable=False),
        sa.Column("predicate_id", ULID, nullable=False),
        sa.Column("proposed", postgresql.JSONB(), nullable=False),
        sa.Column("status", sa.Text(), server_default=sa.text("'pending'"), nullable=False),
        sa.Column("filed_by", sa.Text(), nullable=False),
        _created(),
        sa.Column("decided_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("decided_by", sa.Text(), nullable=True),
        sa.Column("decision", postgresql.JSONB(), nullable=True),
        sa.CheckConstraint(
            "status IN ('pending', 'approved', 'rejected')", name="ck_predicate_proposals_status"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_predicate_proposals"),
    )
    op.create_index("ix_predicate_proposals_story_id", "predicate_proposals", ["story_id"])
    op.create_index(
        "uq_predicate_proposals_pending",
        "predicate_proposals",
        ["predicate_id"],
        unique=True,
        postgresql_where=sa.text("status = 'pending'"),
    )

    op.add_column("stories", sa.Column("story_epoch", sa.TIMESTAMP(timezone=True), nullable=True))
    op.add_column(
        "stories",
        sa.Column(
            "story_minute_zero", sa.BigInteger(), server_default=sa.text("0"), nullable=False
        ),
    )
    op.add_column(
        "stories",
        sa.Column(
            "predicate_rules",
            postgresql.JSONB(),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
    )

    table = sa.table(
        "predicates",
        *(
            sa.column(name, postgresql.JSONB(none_as_null=True) if name in _JSON else None)
            for name in predicate_row(CORE_PREDICATES[0])
        ),
    )
    op.bulk_insert(table, [predicate_row(p) for p in CORE_PREDICATES])

    op.execute(PROPOSITIONS_GUARD)
    op.execute(PREDICATES_GUARD)
    for statement in TRIGGERS:
        op.execute(statement)


_JSON = frozenset({"qualifiers", "exclusions", "implications"})


def downgrade() -> None:
    op.drop_column("stories", "predicate_rules")
    op.drop_column("stories", "story_minute_zero")
    op.drop_column("stories", "story_epoch")
    for name in (
        "predicate_proposals",
        "closed_sets",
        "propositions",
        "predicates",
        "entity_aliases",
        "entities",
    ):
        op.drop_table(name)
    op.execute("DROP FUNCTION epistrel_propositions_append_only()")
    op.execute("DROP FUNCTION epistrel_predicates_identity()")
