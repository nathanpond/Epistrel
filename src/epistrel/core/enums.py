"""The engine's closed vocabularies (docs/02 §4.4, §4.10), mirrored by Postgres enum types.

Each ``StrEnum`` value is the database label; ``PG_ENUM_TYPES`` maps the Postgres type names
(schema-qualified outside ``public``).
"""

from __future__ import annotations

from enum import StrEnum


class EventCategory(StrEnum):
    """What a view may read (FR-STORE-25)."""

    NARRATIVE_STATE = "narrative_state"
    INPUT = "input"
    DERIVED = "derived"
    AMENDMENT = "amendment"
    AUDIT = "audit"
    CONTROL = "control"


class OperationKind(StrEnum):
    """The unit of work an operation is (FR-STORE-24). Only ``turn`` allocates a turn index."""

    TURN = "turn"
    INTER_TURN = "inter_turn"
    GENESIS = "genesis"
    CONSOLIDATION = "consolidation"
    REWRITE_SESSION = "rewrite_session"
    BACKFILL = "backfill"
    IMPORT = "import"
    CONTROL = "control"
    MAINTENANCE = "maintenance"


class Membership(StrEnum):
    """Timeline membership of an operation and so of its records (docs/02 §4.4, §4.6)."""

    ACTIVE = "active"
    DISCARDED = "discarded"
    DORMANT = "dormant"
    SUPERSEDED = "superseded"


class Provenance(StrEnum):
    """Who originated a record (FR-STORE-2)."""

    PLAYER = "player"
    ENGINE = "engine"
    CONSUMER = "consumer"
    SYSTEM = "system"


class Authority(StrEnum):
    """Whether a record is canon (FR-STORE-2)."""

    AUTHORITATIVE = "authoritative"
    NON_AUTHORITATIVE = "non_authoritative"


class OperationStatus(StrEnum):
    """Where an operation is in its life: inserted ``in_flight`` at begin, finalised once.

    ``queued``: a turn waiting in its story's ``turn_queue`` (#63); it becomes ``in_flight``
    when the queue's drainer starts it."""

    QUEUED = "queued"
    IN_FLIGHT = "in_flight"
    COMMITTED = "committed"
    ABANDONED = "abandoned"
    FAILED = "failed"


class StoryStatus(StrEnum):
    """Where a story is in its lifecycle (#50). Stored as checked text, not a Postgres enum.

    ``archived`` is read-only: truth-changing work is refused with ``story_archived``.
    ``paused`` is reserved for M3's rating and reactivation checks.
    ``blocked_by_erasure``: the erasure ledger holds an erasure this database has not applied
    (a restored backup that predates it; FR-ADMIN-7, #125). Every read and write of the story is
    refused with 503 ``erasure_pending`` until the reconciler has reapplied it; the status it
    had is kept in ``stories.status_before_block`` and restored then.
    """

    ACTIVE = "active"
    ARCHIVED = "archived"
    PAUSED = "paused"
    BLOCKED_BY_ERASURE = "blocked_by_erasure"


class EntryStatus(StrEnum):
    """A catalog entry's version status (FR-CAT-2; #103, #108): ``draft`` until its first
    publish, then ``published``; ``archived`` entries still read but cannot be edited,
    published or used to create a story until they are unarchived. Postgres enum
    ``catalog.entry_status``."""

    DRAFT = "draft"
    PUBLISHED = "published"
    ARCHIVED = "archived"


class RecordProvenance(StrEnum):
    """Where a structured record came from (#133; shared by ``facts`` and #135's memories):
    the author or the scenario's seed, the engine's consolidation, an in-world source, a
    render, the director or the player, an external authority (with its timestamp in the
    record's reference), a resolution, or a backfill. Postgres enum ``record_provenance``."""

    AUTHOR = "author"
    SEED = "seed"
    SYSTEM_CONSOLIDATED = "system_consolidated"
    IN_WORLD = "in_world"
    RENDER = "render"
    DIRECTOR = "director"
    PLAYER = "player"
    EXTERNAL = "external"
    RESOLUTION = "resolution"
    BACKFILL = "backfill"


class FactAuthority(StrEnum):
    """Who stands behind a fact version (FR-BITEMP-4; #133): the author (a retcon, a seed),
    the engine, or an external authority. Postgres enum ``fact_authority``."""

    AUTHOR = "author"
    ENGINE = "engine"
    EXTERNAL = "external"


class HolderKind(StrEnum):
    """Whose record a proposition wrapper is (#133): ``canon`` for objective facts (#150's
    utterances add theirs). Postgres enum ``holder_kind``."""

    CANON = "canon"


class FactCommitState(StrEnum):
    """Whether a fact's value is settled (FR-COMMIT-1; #133 creates it, #146 owns its
    semantics): ``committed`` (the default), ``introduced`` (narrated with an open qualifier),
    ``pending`` (awaiting a dependence in the same turn) or ``revised`` (#146 adds it; #154's
    regeneration revert writes it). ``core.commit.lifecycle.TRANSITIONS`` names the moves.
    Postgres enum ``fact_commit_state``."""

    COMMITTED = "committed"
    INTRODUCED = "introduced"
    PENDING = "pending"
    REVISED = "revised"


class ThunkState(StrEnum):
    """A thunk's stored state (FR-COMMIT-5,10; #146): ``open`` (undetermined), ``resolved``
    (a dependence fixed its value) or ``expired`` (pruned past ``thunks_max_open``, or its
    subject erased: ignored from then on). A ``resolved`` row whose resolving operation left the
    timeline reads as ``open`` (``core.commit.lifecycle``). Postgres enum ``thunk_state``."""

    OPEN = "open"
    RESOLVED = "resolved"
    EXPIRED = "expired"


class OpenSlot(StrEnum):
    """Which argument of a thunk's proposition is undetermined (#146): its ``subject``
    ("someone knocked"), its ``object`` or its ``literal``. Postgres enum ``thunk_open_slot``."""

    SUBJECT = "subject"
    OBJECT = "object"
    LITERAL = "literal"


class LedgerKind(StrEnum):
    """A promissory-ledger entry's kind (FR-COMMIT-7; #146): a ``salient_reference``.
    Postgres enum ``ledger_kind``."""

    SALIENT_REFERENCE = "salient_reference"


class DischargeMode(StrEnum):
    """How a ledger entry was discharged (FR-COMMIT-7,13; #146): by a ``dependence`` on the
    value, by a follow-up ``render`` that settled it, or by the deterministic ``minimal`` commit
    at budget exhaustion. Postgres enum ``ledger_discharge_mode``."""

    DEPENDENCE = "dependence"
    RENDER = "render"
    MINIMAL = "minimal"


class MemoryKind(StrEnum):
    """A memory record's tier (FR-MEM-1..3; #135): ``episodic`` (one observable beat),
    ``semantic`` (a distilled standing fact) or ``reflection`` (a higher-level abstraction; the
    last two are written by #143's consolidation). Postgres enum ``memory_kind``."""

    EPISODIC = "episodic"
    SEMANTIC = "semantic"
    REFLECTION = "reflection"


class MemoryTrustTier(StrEnum):
    """How far a memory is trusted (FR-MEM-4; #135), derived mechanically from its provenance
    (``core.memory.records.trust_tier_of``): ``author`` (the author or a scenario seed),
    ``external`` (an external authority), ``engine`` (consolidation, a resolution, a backfill)
    or ``in_world`` (what happened or was said in the story). Postgres enum
    ``memory_trust_tier``."""

    AUTHOR = "author"
    EXTERNAL = "external"
    ENGINE = "engine"
    IN_WORLD = "in_world"


class RetrievalTier(StrEnum):
    """Whether a memory is searched (FR-MEM-8; #135 creates it, #147 retires): ``hot`` or
    ``retired``, independent of timeline membership. Postgres enum ``retrieval_tier``."""

    HOT = "hot"
    RETIRED = "retired"


class BeliefConfidenceLevel(StrEnum):
    """A belief's categorical confidence (FR-BELIEF-4's dual confidence; #159 creates it, #160
    projects the scalar onto it): ``unknown`` (suspended: the holder neither holds nor denies
    it), ``suspected``, ``believed`` or ``known``, weakest first. Postgres enum
    ``belief_confidence_level``."""

    UNKNOWN = "unknown"
    SUSPECTED = "suspected"
    BELIEVED = "believed"
    KNOWN = "known"


class BeliefKind(StrEnum):
    """What a belief record holds (#159 pre-declares every kind; #167 writes the last three):
    a ``first_order`` belief about the world, an ``attributed`` belief about what another
    character believes, a belief about a ``speech_act`` (that someone said something), or a
    ``nested`` belief deeper than one level. Postgres enum ``belief_kind``."""

    FIRST_ORDER = "first_order"
    ATTRIBUTED = "attributed"
    SPEECH_ACT = "speech_act"
    NESTED = "nested"


PG_ENUM_TYPES: dict[str, type[StrEnum]] = {
    "event_category": EventCategory,
    "operation_kind": OperationKind,
    "membership": Membership,
    "provenance": Provenance,
    "authority": Authority,
    "operation_status": OperationStatus,
    "catalog.entry_status": EntryStatus,
    "record_provenance": RecordProvenance,
    "fact_authority": FactAuthority,
    "holder_kind": HolderKind,
    "fact_commit_state": FactCommitState,
    "thunk_state": ThunkState,
    "thunk_open_slot": OpenSlot,
    "ledger_kind": LedgerKind,
    "ledger_discharge_mode": DischargeMode,
    "memory_kind": MemoryKind,
    "memory_trust_tier": MemoryTrustTier,
    "retrieval_tier": RetrievalTier,
    "belief_confidence_level": BeliefConfidenceLevel,
    "belief_kind": BeliefKind,
}
"""Postgres enum type name (``<schema>.<name>`` outside ``public``) → the Python enum whose
values it holds (creation order)."""
