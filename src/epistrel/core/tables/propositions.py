"""Entities, the predicate registry, propositions, closed sets and predicate proposals
(FR-PROP-1..8; docs/02 §4.9; #131, migration 0048).

- ``entities`` / ``entity_aliases``: a story's referents (a character, a place, an item, an
  institution) with their canonical name and aliases (FR-PROP-6); ``core.propositions.store`` is
  their writer. #139 (migration 0054) adds what the mention resolver (``core.resolution``) reads:
  ``gender`` (``masculine``/``feminine``; ``NULL`` is compatible with any pronoun),
  ``descriptors`` (a JSON list of the words a description can match), ``created_by_operation_id``,
  and each alias's ``source``. A story character's entity has the character's id (and
  ``character_id``), written beside its ``characters`` row; the canonical name is an alias row
  too (``source = canonical``). Alias uniqueness per entity is the writer's (by normalized
  form), not an index: erasure's redaction may make two aliases' texts equal.
- ``predicates``: the registry. A core row has ``story_id`` null (``layer = core``, seeded from
  ``core.propositions.vocabulary``); an author or auto row belongs to its story. Names are unique
  per scope (``uq_predicates_scope_name`` over ``COALESCE(story_id, 'core')``); a functional row
  has ``max_cardinality = 1`` (CHECK). Lists of predicate ids (``exclusions``, ``implications``)
  are JSON, so a fork's id remap reaches them; ``qualifiers`` declares the qualifiers a
  proposition may carry (``NULL``: undeclared, an auto predicate's).
- ``propositions``: viewpoint-free propositions (FR-PROP-1,2), wrapped by the records that hold
  them (#133's ``facts``, M5's beliefs). Valid-time is four bounds in story-minutes
  (``valid_from_inner/outer``, ``valid_to_inner/outer``; ``*_exact`` means inner = outer; ``NULL``
  is open) or an instant (``valid_from_*`` = its minute, ``valid_to_*`` null,
  ``instant_granularity`` set); ``valid_span`` is the possible span ``[from_outer, to_outer)`` (an
  instant's granule), never empty, GiST-indexed for overlap checks. ``object_set`` is a JSON list
  of entity ids. Append-only: only an erasure (``epistrel.erasure = on``, rewriting ``literal``
  and ``qualifiers``) and a story's deletion change rows.
- ``closed_sets``: an author's or canon's declaration that a set is exhaustive for a key and
  interval (FR-PROP-8). Never inferred. #133 wraps them in bitemporal ``facts``.
- ``predicate_proposals``: the engine's proposals to strengthen an auto predicate, inert until
  approved (FR-PROP-4); one pending proposal per predicate.

All are story tables without foreign keys (``core.stories.STORY_TABLES``): story deletion
empties them and a fork copies them under new ids.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import (
    TIMESTAMP,
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    Index,
    Integer,
    Table,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, INT8RANGE, JSONB

from epistrel.core.tables.base import metadata, ulid_type

CORE_SCOPE = "core"
"""``COALESCE(story_id, 'core')``: the scope a core predicate's name is unique in."""


def _created() -> Column[Any]:
    return Column(
        "created_at",
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=text("transaction_timestamp()"),
    )


entities = Table(
    "entities",
    metadata,
    Column("id", ulid_type(), primary_key=True),
    Column("story_id", ulid_type(), nullable=False),
    Column("kind", Text, nullable=False),
    Column("canonical_name", Text, nullable=False),
    Column("character_id", ulid_type(), nullable=True),
    _created(),
    # FR-PROP-6 (#139): what the mention resolver reads.
    Column("gender", Text, nullable=True),
    Column("descriptors", JSONB, nullable=False, server_default=text("'[]'::jsonb")),
    Column("created_by_operation_id", ulid_type(), nullable=True),
    CheckConstraint("gender IS NULL OR gender IN ('masculine', 'feminine')", name="gender"),
)
Index("ix_entities_story_id", entities.c.story_id)

ALIAS_SOURCES = (
    "canonical",
    "catalog_alias",
    "name_override",
    "scenario_entity",
    "persona",
    "canonized",
    "detected",
)
"""Where an alias comes from (``entity_aliases.source``, #139)."""

entity_aliases = Table(
    "entity_aliases",
    metadata,
    Column("id", ulid_type(), primary_key=True),
    Column("story_id", ulid_type(), nullable=False),
    Column("entity_id", ulid_type(), nullable=False),
    Column("alias", Text, nullable=False),
    _created(),
    Column("source", Text, nullable=False, server_default=text("'detected'")),
    CheckConstraint(
        "source IN (" + ", ".join(f"'{s}'" for s in ALIAS_SOURCES) + ")", name="source"
    ),
)
Index("ix_entity_aliases_story_id", entity_aliases.c.story_id, entity_aliases.c.entity_id)

predicates = Table(
    "predicates",
    metadata,
    Column("id", ulid_type(), primary_key=True),
    Column("story_id", ulid_type(), nullable=True),
    Column("name", Text, nullable=False),
    Column("layer", Text, nullable=False),
    Column("subject_type", Text, nullable=False, server_default=text("'any'")),
    Column("object_type", Text, nullable=False, server_default=text("'any'")),
    Column("cardinality", Text, nullable=False),
    Column("max_cardinality", Integer, nullable=True),
    Column("key_qualifiers", ARRAY(Text), nullable=False, server_default=text("'{}'::text[]")),
    Column("qualifiers", JSONB(none_as_null=True), nullable=True),
    Column("set_object", Boolean, nullable=False, server_default=text("false")),
    Column("temporal_kind", Text, nullable=False),
    Column("exclusions", JSONB, nullable=False, server_default=text("'[]'::jsonb")),
    Column("containment_of", ulid_type(), nullable=True),
    Column("implications", JSONB, nullable=False, server_default=text("'[]'::jsonb")),
    Column("symmetric", Boolean, nullable=False, server_default=text("false")),
    Column("inverse_of", ulid_type(), nullable=True),
    Column("owner", Text, nullable=False, server_default=text("'story'")),
    Column("projection", Text, nullable=True),
    Column("docs", Text, nullable=False, server_default=text("''")),
    _created(),
    CheckConstraint("layer IN ('core', 'author', 'auto')", name="layer"),
    CheckConstraint("(layer = 'core') = (story_id IS NULL)", name="core_scope"),
    CheckConstraint("cardinality IN ('functional', 'multi')", name="cardinality"),
    CheckConstraint(
        "cardinality <> 'functional' OR max_cardinality = 1", name="functional_maximum"
    ),
    CheckConstraint("max_cardinality IS NULL OR max_cardinality >= 1", name="maximum"),
    CheckConstraint("temporal_kind IN ('permanent', 'fluent', 'event')", name="temporal_kind"),
    CheckConstraint("owner IN ('story', 'external')", name="owner"),
)
Index(
    "uq_predicates_scope_name",
    func.coalesce(predicates.c.story_id, text(f"'{CORE_SCOPE}'")),
    predicates.c.name,
    unique=True,
)

propositions = Table(
    "propositions",
    metadata,
    Column("id", ulid_type(), primary_key=True),
    Column("story_id", ulid_type(), nullable=False),
    Column("subject_id", ulid_type(), nullable=False),
    Column("predicate_id", ulid_type(), nullable=False),
    Column("object_id", ulid_type(), nullable=True),
    Column("literal", JSONB(none_as_null=True), nullable=True),
    Column("object_set", JSONB(none_as_null=True), nullable=True),
    Column("qualifiers", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    Column("polarity", Text, nullable=False),
    Column("valid_from_inner", BigInteger, nullable=True),
    Column("valid_from_outer", BigInteger, nullable=True),
    Column("valid_to_inner", BigInteger, nullable=True),
    Column("valid_to_outer", BigInteger, nullable=True),
    Column("from_exact", Boolean, nullable=False),
    Column("to_exact", Boolean, nullable=False),
    Column("instant_granularity", Text, nullable=True),
    Column("valid_span", INT8RANGE, nullable=False),
    Column("event_id", ulid_type(), nullable=True),
    _created(),
    CheckConstraint("polarity IN ('affirmed', 'negated')", name="polarity"),
    CheckConstraint("num_nonnulls(object_id, literal, object_set) <= 1", name="one_object"),
    CheckConstraint(
        "NOT from_exact OR valid_from_inner IS NOT DISTINCT FROM valid_from_outer",
        name="from_exact",
    ),
    CheckConstraint(
        "NOT to_exact OR valid_to_inner IS NOT DISTINCT FROM valid_to_outer", name="to_exact"
    ),
    CheckConstraint(
        "instant_granularity IS NULL OR (instant_granularity IN "
        "('minute', 'hour', 'day', 'month', 'year') AND valid_from_inner IS NOT NULL "
        "AND valid_to_inner IS NULL AND valid_to_outer IS NULL)",
        name="instant",
    ),
    CheckConstraint("NOT isempty(valid_span)", name="span_not_empty"),
)
Index(
    "ix_propositions_story_subject_predicate",
    propositions.c.story_id,
    propositions.c.subject_id,
    propositions.c.predicate_id,
)
Index("ix_propositions_valid_span", propositions.c.valid_span, postgresql_using="gist")

closed_sets = Table(
    "closed_sets",
    metadata,
    Column("id", ulid_type(), primary_key=True),
    Column("story_id", ulid_type(), nullable=False),
    Column("predicate_id", ulid_type(), nullable=False),
    Column("key", JSONB, nullable=False),
    Column("members", JSONB, nullable=False),
    Column("valid_from", BigInteger, nullable=True),
    Column("valid_to", BigInteger, nullable=True),
    Column("asserted_by", Text, nullable=False),
    _created(),
    CheckConstraint("asserted_by IN ('author', 'canon')", name="asserted_by"),
    CheckConstraint(
        "valid_from IS NULL OR valid_to IS NULL OR valid_from < valid_to", name="interval"
    ),
)
Index("ix_closed_sets_story_predicate", closed_sets.c.story_id, closed_sets.c.predicate_id)

predicate_proposals = Table(
    "predicate_proposals",
    metadata,
    Column("id", ulid_type(), primary_key=True),
    Column("story_id", ulid_type(), nullable=False),
    Column("predicate_id", ulid_type(), nullable=False),
    Column("proposed", JSONB, nullable=False),
    Column("status", Text, nullable=False, server_default=text("'pending'")),
    Column("filed_by", Text, nullable=False),
    _created(),
    Column("decided_at", TIMESTAMP(timezone=True), nullable=True),
    Column("decided_by", Text, nullable=True),
    Column("decision", JSONB(none_as_null=True), nullable=True),
    CheckConstraint("status IN ('pending', 'approved', 'rejected')", name="status"),
)
Index("ix_predicate_proposals_story_id", predicate_proposals.c.story_id)
Index(
    "uq_predicate_proposals_pending",
    predicate_proposals.c.predicate_id,
    unique=True,
    postgresql_where=text("status = 'pending'"),
)

__all__ = [
    "ALIAS_SOURCES",
    "CORE_SCOPE",
    "closed_sets",
    "entities",
    "entity_aliases",
    "predicate_proposals",
    "predicates",
    "propositions",
]
