"""Projection tables: caches over the event log, rebuildable by folding it (FR-STORE-4).

Both carry a plain ``story_id`` (no foreign key) so story deletion enumerates them, and no
timestamps of their own: every value is a function of the log and the ``operations`` rows.

- ``transcript_messages``: one row per beat event, keyed by the event id (``message_id``),
  carrying its operation's ``membership`` so truncation and swaps flip rows in place, and a
  reply's ``render_character_ids`` (#57; null on input and narration beats).
- ``story_heads``: one row per story, created at genesis: the next turn index and the active
  variant of every turn (``{"<turn>": variant}``).
- ``story_texts``: one row per story, created at genesis (#60): the pinned memories
  (``[{id, text}]`` in order) and the custom instructions (``""`` when never set).
- ``lite_summary_segments`` (#72): one row per rolling-summary segment event, keyed by the event
  id: the turns it covers (``covers_from``..``covers_to``), its ``level`` (0 summarises
  transcript rows, ``n + 1`` merges level-``n`` or lower segments), the variant of the turn it is
  positioned at, its text, the sources it was made from (``source_versions``: ``[{operation_id,
  membership}]``) and its ``membership``: its operation's, except ``superseded`` while an
  active merged segment holds it as a source. Unique on ``(story_id, covers_from, covers_to,
  level, turn_variant)``.

The structured projections (FR-STORE-3; #137) are caches over canon (``facts``), not over the
event log, folded by ``core.projections.structured``:

- ``relationships``: one row per active canon fact version of a ``relationships`` or ``scene``
  predicate with an entity object (one per member of a set object), keyed by
  ``(story_id, source_fact_id, object_id)``: subject, predicate, object, the key qualifiers
  (ids and typed literals, never prose), the fact's outer valid bounds, its narrative position
  (``since_turn``/``since_slot``, ``NULL`` for a position-less fact), the active version that
  closed it (``closed_by_fact_id`` and its position; ``NULL`` while current: a superseded row is
  closed, never deleted, while its fact is active) and its visibility.
- ``placement``: the same for ``placement`` predicates, keyed by ``(story_id, source_fact_id)``:
  the placed ``entity_id`` and exactly one of ``location_id`` (``located_at``) and
  ``container_id`` (``holds``: the holder contains the item).
- ``scene_state``: one row per active turn whose scene is not empty, keyed by
  ``(story_id, turn, variant)``: the player character's location at the turn's end, the
  entities present there (omniscient) and the turn's operation.
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
    SmallInteger,
    Table,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB

from epistrel.core.enums import Membership
from epistrel.core.tables.base import metadata, pg_enum, ulid_type

transcript_messages = Table(
    "transcript_messages",
    metadata,
    Column("message_id", ulid_type(), primary_key=True),
    Column("story_id", ulid_type(), nullable=False),
    Column("seq", BigInteger, nullable=False),
    Column("operation_id", ulid_type(), nullable=False),
    Column("narrative_turn", Integer, nullable=True),
    Column("turn_variant", SmallInteger, nullable=True),
    Column("beat", SmallInteger, nullable=True),
    Column("kind", Text, nullable=False),
    Column("channel", Text, nullable=False),
    Column("role", Text, nullable=False),
    Column("actor_character_id", ulid_type(), nullable=True),
    Column("render_character_ids", JSONB, nullable=True),
    Column("text", Text, nullable=False),
    Column("membership", pg_enum(Membership, "membership"), nullable=False),
    # The beat's event was redacted by erasure (#124; ``events.redacted_at``).
    Column("redacted", Boolean, nullable=False, server_default=text("false")),
    Index("ix_transcript_messages_story_position", "story_id", "narrative_turn", "turn_variant"),
    Index("ix_transcript_messages_story_membership", "story_id", "membership"),
)

story_heads = Table(
    "story_heads",
    metadata,
    Column("story_id", ulid_type(), primary_key=True),
    Column("next_turn_index", Integer, nullable=False),
    Column("active_variants", JSONB, nullable=False),
)

story_texts = Table(
    "story_texts",
    metadata,
    Column("story_id", ulid_type(), primary_key=True),
    Column("pinned", JSONB, nullable=False),
    Column("custom_instructions", Text, nullable=False),
)

lite_summary_segments = Table(
    "lite_summary_segments",
    metadata,
    Column("segment_id", ulid_type(), primary_key=True),
    Column("story_id", ulid_type(), nullable=False),
    Column("seq", BigInteger, nullable=False),
    Column("operation_id", ulid_type(), nullable=False),
    Column("covers_from", Integer, nullable=False),
    Column("covers_to", Integer, nullable=False),
    Column("level", SmallInteger, nullable=False),
    Column("turn_variant", SmallInteger, nullable=True),
    Column("text", Text, nullable=False),
    Column("membership", pg_enum(Membership, "membership"), nullable=False),
    Column("source_versions", JSONB, nullable=False),
    Column("model", Text, nullable=True),
    Column("prompt_hash", Text, nullable=True),
    # When erasure (#124) redacted or regenerated the segment's event (``events.redacted_at``).
    Column("redacted_at", TIMESTAMP(timezone=True), nullable=True),
    Index("ix_lite_summary_segments_story_covers", "story_id", "covers_to"),
    Index(
        "uq_lite_summary_segments_position",
        "story_id",
        "covers_from",
        "covers_to",
        "level",
        "turn_variant",
        unique=True,
    ),
)


def _structured_columns() -> list[Column[Any]]:
    """The columns ``relationships`` and ``placement`` share after their own: the fact's outer
    valid bounds, its position, its active closer and its visibility."""
    return [
        Column("valid_from", BigInteger, nullable=True),
        Column("valid_to", BigInteger, nullable=True),
        Column("since_turn", Integer, nullable=True),
        Column("since_slot", SmallInteger, nullable=True),
        Column("closed_by_fact_id", ulid_type(), nullable=True),
        Column("closed_turn", Integer, nullable=True),
        Column("closed_slot", SmallInteger, nullable=True),
        Column("visibility", JSONB, nullable=False),
    ]


relationships = Table(
    "relationships",
    metadata,
    Column("story_id", ulid_type(), primary_key=True),
    Column("source_fact_id", ulid_type(), primary_key=True),
    Column("object_id", ulid_type(), primary_key=True),
    Column("subject_id", ulid_type(), nullable=False),
    Column("predicate_id", ulid_type(), nullable=False),
    Column("qualifiers", JSONB, nullable=False),
    *_structured_columns(),
    Index("ix_relationships_story_subject", "story_id", "subject_id"),
    Index("ix_relationships_story_object", "story_id", "object_id"),
)

placement = Table(
    "placement",
    metadata,
    Column("story_id", ulid_type(), primary_key=True),
    Column("source_fact_id", ulid_type(), primary_key=True),
    Column("entity_id", ulid_type(), nullable=False),
    Column("location_id", ulid_type(), nullable=True),
    Column("container_id", ulid_type(), nullable=True),
    *_structured_columns(),
    CheckConstraint("num_nonnulls(location_id, container_id) = 1", name="one_target"),
    Index("ix_placement_story_entity", "story_id", "entity_id"),
    Index("ix_placement_story_location", "story_id", "location_id"),
    Index("ix_placement_story_container", "story_id", "container_id"),
)

scene_state = Table(
    "scene_state",
    metadata,
    Column("story_id", ulid_type(), primary_key=True),
    Column("turn", Integer, primary_key=True),
    Column("variant", SmallInteger, primary_key=True),
    Column("location_id", ulid_type(), nullable=True),
    Column("present_entity_ids", ARRAY(ulid_type()), nullable=False),
    Column("source_operation_id", ulid_type(), nullable=False),
)
