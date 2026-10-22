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
- **which occasion it belongs to** (#141): ``event_type`` (#139's event-type vocabulary, the
  beat's occasion; ``NULL`` when the beat names none) and ``participants`` (the entity ids the
  beat involves: its actor and the entities its mentions resolved to; GIN-indexed). #150's
  episodic writer fills them from the beat's resolved entities (``Beat.event_type``,
  ``Beat.participants``); #143's semantic memories and reflections carry their sources'.
  ``core.retrieval.pipeline``'s occasion step queries them.
- **lexical search** (#141): ``search``, the ``tsvector`` generated from ``text`` under the
  ``english`` configuration (``SEARCH_EXPRESSION``), GIN-indexed: the lexical arm and the
  alias arm of the hybrid retrieval read it, and an erasure that rewrites ``text`` recomputes
  it.
- **what consolidation made of it** (#143, migration 0058): on a ``semantic`` or ``reflection``
  row, ``level`` (1: compressed from episodic memories and canon; 2: from level-1 outputs),
  ``modality`` (``objective``, ``attributed_claim`` or ``observation``: the epistemic type
  FR-MEM-9 preserves; an attributed claim's speaker is its ``character_id``), ``inputs_hash``
  (the replay key: a digest of its inputs' content, the same in a fork) and the provenance
  FR-MEM-10 stores, ``model``, ``model_version`` (``NULL`` when the adapter reports none) and
  ``prompt_hash``; on any row, ``covered_by`` (the consolidation outputs that cover it,
  rewritten as outputs are made; an output that is no longer active covers nothing, so readers
  join on its membership; #147's retirement prefers covered records).
- **quarantine** (#145, migration 0059; FR-GUARD-4,5): ``quarantined_at`` (``NULL``: not
  quarantined), ``quarantine_reason`` (at most ``QUARANTINE_REASON_MAX`` characters;
  ``repeated_implication`` is the system's) and ``quarantined_by`` (the principal; ``system``
  for an auto-quarantine), set and cleared together (CHECK); ``implication_count``, how many
  rejected renders implicated the memory (the output gate's convergence rule). A quarantined
  memory is excluded from retrieval (every arm of #141's pipeline) and from consolidation's
  input windows, never from working memory (events are the truth, FR-MEM-5); it keeps its
  vector. ``core.mutation`` writes these.
- **retrieval**: ``retrieval_tier`` (``hot``/``retired``, independent of membership; #147),
  ``embedding`` (a pgvector ``vector``, untyped here: #138 pins the dimension, retypes the
  column and creates the HNSW cosine index; ``NULL`` until the embed job runs) and
  ``embedding_model_version``; ``created_at`` and ``last_accessed_at`` (``NULL`` until #141's
  first retrieval).
- **the hot set** (#147, migration 0061; FR-MEM-8): ``pinned`` (an author pin: outside the
  budgets, never retired, admission-limited; a pinned row is always ``hot``, CHECK) and
  ``pinned_by`` (the principal); on a retired row, ``retired_reason`` (``RETIRED_REASONS``:
  ``covered`` by an active hot consolidation output, below the importance ``floor``, or
  ``uncovered``: forced at an overflow episode's deadline, reverting to ``covered`` when
  consolidation later covers it), ``retired_at`` and ``retired_turn`` (the head turn then).
  Two partial indexes make the counters' exact recount an index-only read of the hot set
  (``ix_memories_hot_unpinned``, ``ix_memories_hot_vectors``), and one the pins'
  (``ix_memories_pinned``). ``core.memory.retirement`` and ``core.memory.pins`` write these.

Guarded (``epistrel_memories_guard``): an UPDATE may change only ``MUTABLE_MEMORY_COLUMNS``; an
erasure (``epistrel.erasure = on``) may also rewrite ``text`` and ``quarantine_reason``, and a
sanitize (``epistrel.sanitize`` = the memory's id, #145) that one row's ``text`` and
``inputs_hash``. A DELETE passes only under an
erasure or the story's deletion (``epistrel.allow_story_delete``); TRUNCATE is refused. The
guard fires after the row is written (migration 0067, #452), and the table keeps 30 % of each page
free (``fillfactor = 70``), so a retrieval's ``last_accessed_at`` touch is a heap-only update that
writes no index entry.
``core.memory.records`` is the only writer of rows; ``core.mutation``, ``core.memory.retirement``
and ``core.memory.pins`` update them.
"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import (
    REAL,
    TIMESTAMP,
    Boolean,
    CheckConstraint,
    Column,
    Computed,
    Index,
    Integer,
    SmallInteger,
    Table,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, TSVECTOR
from sqlalchemy.dialects.postgresql.base import ischema_names
from sqlalchemy.types import UserDefinedType

from epistrel.core.enums import MemoryKind, MemoryTrustTier, RecordProvenance, RetrievalTier
from epistrel.core.membership import (
    DerivedTable,
    active_only_predicate,
    derived_columns,
    derived_indexes,
    register_derived,
)
from epistrel.core.tables.base import metadata, pg_enum, ulid_type

TEXT_MAX = 2_000
"""The longest memory text (an episodic line is cut to it)."""

SEARCH_CONFIGURATION = "english"
"""The text-search configuration of ``memories.search`` (and of every query against it)."""
SEARCH_EXPRESSION = f"to_tsvector('{SEARCH_CONFIGURATION}'::regconfig, text)"
"""The generated ``search`` column's expression (migration 0056)."""
EVENT_TYPE_MAX = 64
QUARANTINE_REASON_MAX = 500
"""The longest ``quarantine_reason`` (CHECK)."""
"""The longest ``event_type`` (CHECK)."""

RETIRED_REASONS: tuple[str, ...] = ("covered", "floor", "uncovered")
"""Why a memory was retired (``retired_reason``, CHECK; FR-MEM-8, #147)."""
HOT_UNPINNED_WHERE = f"retrieval_tier = 'hot' AND NOT pinned AND {active_only_predicate()}"
"""The rows the hot-set budgets count (``ix_memories_hot_unpinned``)."""
HOT_VECTORS_WHERE = f"{HOT_UNPINNED_WHERE} AND embedding IS NOT NULL"
"""The rows the story's vector budget counts (``ix_memories_hot_vectors``)."""

MODALITIES: tuple[str, ...] = ("objective", "attributed_claim", "observation")
"""The epistemic types a consolidation output carries (``modality``, CHECK; FR-MEM-9)."""

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
    "covered_by",
    "quarantined_at",
    "quarantine_reason",
    "quarantined_by",
    "implication_count",
    "pinned",
    "pinned_by",
    "retired_reason",
    "retired_at",
    "retired_turn",
)
"""The columns an UPDATE of ``memories`` may change outside an erasure. (The generated
``search`` column is also let through by the guard, since a BEFORE trigger cannot read its new
value; it follows ``text``, which stays guarded.)"""


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
    Column("event_type", Text, nullable=True),
    Column("participants", ARRAY(ulid_type()), nullable=False, server_default=text("'{}'")),
    Column("search", TSVECTOR, Computed(SEARCH_EXPRESSION, persisted=True)),
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
    Column("covered_by", ARRAY(ulid_type()), nullable=False, server_default=text("'{}'")),
    Column("level", SmallInteger, nullable=True),
    Column("modality", Text, nullable=True),
    Column("inputs_hash", Text, nullable=True),
    Column("model", Text, nullable=True),
    Column("model_version", Text, nullable=True),
    Column("prompt_hash", Text, nullable=True),
    Column("quarantined_at", TIMESTAMP(timezone=True), nullable=True),
    Column("quarantine_reason", Text, nullable=True),
    Column("quarantined_by", Text, nullable=True),
    Column("implication_count", Integer, nullable=False, server_default=text("0")),
    Column("pinned", Boolean, nullable=False, server_default=text("false")),
    Column("pinned_by", Text, nullable=True),
    Column("retired_reason", Text, nullable=True),
    Column("retired_at", TIMESTAMP(timezone=True), nullable=True),
    Column("retired_turn", Integer, nullable=True),
    CheckConstraint(f"char_length(text) <= {TEXT_MAX}", name="text_length"),
    CheckConstraint("NOT pinned OR retrieval_tier = 'hot'", name="pinned_hot"),
    CheckConstraint(
        "retired_reason IS NULL OR retired_reason IN ("
        + ", ".join(f"'{r}'" for r in RETIRED_REASONS)
        + ")",
        name="retired_reason",
    ),
    CheckConstraint(
        "(quarantined_at IS NULL) = (quarantine_reason IS NULL) "
        "AND (quarantined_at IS NULL) = (quarantined_by IS NULL) "
        "AND (quarantine_reason IS NULL OR char_length(quarantine_reason) <= "
        f"{QUARANTINE_REASON_MAX})",
        name="quarantine",
    ),
    CheckConstraint("implication_count >= 0", name="implication_count"),
    CheckConstraint(
        "importance IS NULL OR (importance >= 0 AND importance <= 1)", name="importance"
    ),
    CheckConstraint(f"visibility->>'scope' IN ({_SCOPES})", name="visibility_scope"),
    CheckConstraint(
        f"event_type IS NULL OR char_length(event_type) BETWEEN 1 AND {EVENT_TYPE_MAX}",
        name="event_type_length",
    ),
    CheckConstraint(
        "modality IS NULL OR modality IN (" + ", ".join(f"'{m}'" for m in MODALITIES) + ")",
        name="modality",
    ),
    CheckConstraint("level IS NULL OR level IN (1, 2)", name="level"),
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
Index("ix_memories_participants", memories.c.participants, postgresql_using="gin")
Index("ix_memories_search", memories.c.search, postgresql_using="gin")
Index(
    "ix_memories_story_kind_turn", memories.c.story_id, memories.c.kind, memories.c.narrative_turn
)
Index(
    "ix_memories_hot_unpinned",
    memories.c.story_id,
    memories.c.holder_character_id,
    postgresql_where=text(HOT_UNPINNED_WHERE),
)
Index(
    "ix_memories_hot_vectors",
    memories.c.story_id,
    postgresql_where=text(HOT_VECTORS_WHERE),
)
Index(
    "ix_memories_pinned",
    memories.c.story_id,
    memories.c.holder_character_id,
    postgresql_where=text("pinned"),
)
Index(
    "ix_memories_story_occasion",
    memories.c.story_id,
    memories.c.event_type,
    memories.c.narrative_turn,
)

MEMORIES = register_derived(DerivedTable(memories))
"""``memories`` in ``core.membership.DERIVED_TABLES``: truncation, untruncation, swap,
regenerate and fork move its rows with their operations. A memory has a narrative position but
neither valid-time nor transaction-time columns."""

__all__ = [
    "EVENT_TYPE_MAX",
    "HOT_UNPINNED_WHERE",
    "HOT_VECTORS_WHERE",
    "MEMORIES",
    "MODALITIES",
    "MUTABLE_MEMORY_COLUMNS",
    "QUARANTINE_REASON_MAX",
    "RETIRED_REASONS",
    "SEARCH_CONFIGURATION",
    "SEARCH_EXPRESSION",
    "TEXT_MAX",
    "VISIBILITY_SCOPES",
    "Vector",
    "memories",
]
