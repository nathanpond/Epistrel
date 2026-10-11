"""The event store's closed vocabularies (docs/02 §4.4), mirrored by Postgres enum types.

Each ``StrEnum`` value is the database label; ``PG_ENUM_TYPES`` maps the Postgres type names.
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
    """Where an operation is in its life: inserted ``in_flight`` at begin, finalised once."""

    IN_FLIGHT = "in_flight"
    COMMITTED = "committed"
    ABANDONED = "abandoned"
    FAILED = "failed"


class StoryStatus(StrEnum):
    """Where a story is in its lifecycle (#50). Stored as checked text, not a Postgres enum.

    ``archived`` is read-only: truth-changing work is refused with ``story_archived``.
    ``paused`` is reserved for M3's rating and reactivation checks.
    """

    ACTIVE = "active"
    ARCHIVED = "archived"
    PAUSED = "paused"


PG_ENUM_TYPES: dict[str, type[StrEnum]] = {
    "event_category": EventCategory,
    "operation_kind": OperationKind,
    "membership": Membership,
    "provenance": Provenance,
    "authority": Authority,
    "operation_status": OperationStatus,
}
"""Postgres enum type name → the Python enum whose values it holds (creation order)."""
