"""``memories``: episodic, semantic and reflection memory records (FR-MEM-1,2,4,5,7,8,
FR-STORE-9; docs/02 §4.2, §4.4; #135, migration 0050).

One row is one memory record:

- **what it is**: ``kind`` (``memory_kind``: ``episodic`` from one observable beat; ``semantic``
  and ``reflection`` from #143's consolidation) and ``text`` (an episodic memory's rendered,
  attributed line, ``Bobby says: …``; at most ``TEXT_MAX`` characters).
- **where it came from**: ``provenance`` (#133's ``record_provenance``), ``trust_tier``
  (``memory_trust_tier``, derived mechanically from the provenance), the derived-record
  columns of ``core.membership`` (the producing operation, ``source_event_ids``, the narrative
  position ``narrative_turn``/``turn_variant`` plus ``beat``, and the timeline membership with
  ``membership_tx``), ``proposition_ids`` (the propositions a memory wraps: a beat's), and
  ``character_id`` (the beat's actor, ``NULL`` for narration).
- **who may know it**: ``holder_character_id`` (whose memory it is; ``NULL``: shared, as every
  render-derived memory is in M4) and ``visibility`` (#133's shape:
  ``{"scope": objective|public|engine|characters, "characters": [...]}``).
- **how important it is now**: ``importance`` (0..1, ``NULL`` until scored; ranking and
  retirement read ``NULL`` as ``DEFAULT_IMPORTANCE``) and ``importance_at`` (the committed turn
  the importance was assigned at; ``core.memory.records.decayed_importance`` decays from it).
- **retrieval**: ``retrieval_tier`` (``hot``/``retired``, independent of membership; #147),
  ``embedding`` (a pgvector ``vector``, untyped here: #138 pins the dimension, retypes the
  column and creates the HNSW cosine index; ``NULL`` until the embed job runs) and
  ``embedding_model_version``; ``created_at`` and ``last_accessed_at`` (``NULL`` until #141's
  first retrieval).

Guarded (``epistrel_memories_guard``): an UPDATE may change only ``MUTABLE_MEMORY_COLUMNS``; an
erasure (``epistrel.erasure = on``) may also rewrite ``text``. A DELETE passes only under an
erasure or the story's deletion (``epistrel.allow_story_delete``); TRUNCATE is refused.
``core.memory.records`` is the only writer of rows.
"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import (
    REAL,
    TIMESTAMP,
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
from sqlalchemy.dialects.postgresql.base import ischema_names
from sqlalchemy.types import UserDefinedType

from epistrel.core.enums import MemoryKind, MemoryTrustTier, RecordProvenance, RetrievalTier
from epistrel.core.membership import (
    DerivedTable,
    derived_columns,
    derived_indexes,
    register_derived,
)
from epistrel.core.tables.base import metadata, pg_enum, ulid_type

TEXT_MAX = 2_000
"""The longest memory text (an episodic line is cut to it)."""

VISIBILITY_SCOPES: tuple[str, ...] = ("objective", "public", "engine", "characters")
"""The visibility scopes a memory may carry (#133's, ``visibility->>'scope'``, CHECK)."""

MUTABLE_MEMORY_COLUMNS: tuple[str, ...] = (
    "importance",
    "importance_at",
    "last_accessed_at",
    "retrieval_tier",
    "embedding",
    "embedding_model_version",
    "membership",
    "membership_tx",
)
"""The columns an UPDATE of ``memories`` may change outside an erasure."""


class Vector(UserDefinedType[list[float]]):
    """pgvector's ``vector`` (ADR-002; the extension is migration 0001's), untyped or of
    ``dim`` dimensions. Values are lists of floats; no dependency beyond SQLAlchemy."""

    cache_ok = True

    def __init__(self, dim: int | None = None) -> None:
        self.dim = dim

    def get_col_spec(self, **kw: Any) -> str:
        return "VECTOR" if self.dim is None else f"VECTOR({self.dim})"

    def bind_processor(self, dialect: Any) -> Any:
        def process(value: Any) -> str | None:
            if value is None:
                return None
            return "[" + ",".join(repr(float(v)) for v in value) + "]"

        return process

    def result_processor(self, dialect: Any, coltype: Any) -> Any:
        def process(value: Any) -> list[float] | None:
            if value is None:
                return None
            if isinstance(value, str):
                return [float(v) for v in json.loads(value)]
            return [float(v) for v in value]

        return process


ischema_names["vector"] = Vector  # reflection (the drift check) reads the column back as one

_SCOPES = ", ".join(f"'{s}'" for s in VISIBILITY_SCOPES)

memories = Table(
    "memories",
    metadata,
    Column("id", ulid_type(), primary_key=True),
    Column("story_id", ulid_type(), nullable=False),
    Column("kind", pg_enum(MemoryKind, "memory_kind"), nullable=False),
    Column("text", Text, nullable=False),
    Column("holder_character_id", ulid_type(), nullable=True),
    Column("character_id", ulid_type(), nullable=True),
    Column(
        "visibility",
        JSONB,
        nullable=False,
        server_default=text("""'{"scope": "objective"}'::jsonb"""),
    ),
    Column("provenance", pg_enum(RecordProvenance, "record_provenance"), nullable=False),
    Column("trust_tier", pg_enum(MemoryTrustTier, "memory_trust_tier"), nullable=False),
    Column("importance", REAL, nullable=True),
    Column("importance_at", Integer, nullable=True),
    *derived_columns(),
    Column("beat", SmallInteger, nullable=True),
    Column("proposition_ids", ARRAY(ulid_type()), nullable=False, server_default=text("'{}'")),
    Column(
        "retrieval_tier",
        pg_enum(RetrievalTier, "retrieval_tier"),
        nullable=False,
        server_default=text("'hot'"),
    ),
    Column("embedding", Vector(), nullable=True),
    Column("embedding_model_version", Text, nullable=True),
    Column(
        "created_at",
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=text("transaction_timestamp()"),
    ),
    Column("last_accessed_at", TIMESTAMP(timezone=True), nullable=True),
    CheckConstraint(f"char_length(text) <= {TEXT_MAX}", name="text_length"),
    CheckConstraint(
        "importance IS NULL OR (importance >= 0 AND importance <= 1)", name="importance"
    ),
    CheckConstraint(f"visibility->>'scope' IN ({_SCOPES})", name="visibility_scope"),
    CheckConstraint(
        "(visibility->>'scope' = 'characters') = "
        "(jsonb_typeof(visibility->'characters') = 'array' "
        "AND jsonb_array_length(visibility->'characters') > 0)",
        name="visibility_characters",
    ),
    *derived_indexes("memories"),
    Index("ix_memories_story_membership_tier", "story_id", "membership", "retrieval_tier"),
)
Index("ix_memories_story_holder", memories.c.story_id, memories.c.holder_character_id)
Index("ix_memories_source_event_ids", memories.c.source_event_ids, postgresql_using="gin")

MEMORIES = register_derived(DerivedTable(memories))
"""``memories`` in ``core.membership.DERIVED_TABLES``: truncation, untruncation, swap,
regenerate and fork move its rows with their operations. A memory has a narrative position but
neither valid-time nor transaction-time columns."""

__all__ = [
    "MEMORIES",
    "MUTABLE_MEMORY_COLUMNS",
    "TEXT_MAX",
    "VISIBILITY_SCOPES",
    "Vector",
    "memories",
]
