"""``operations``: one row per unit of work, grouping its events (docs/02 §4.4).

Inserted ``in_flight`` at ``Operation.begin()`` and finalised once. A trigger lets an update
change only the columns in ``MUTABLE_OPERATION_COLUMNS`` (engine-wide decision, #44).
"""

from __future__ import annotations

from sqlalchemy import (
    TIMESTAMP,
    BigInteger,
    CheckConstraint,
    Column,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    Table,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB

from epistrel.core.enums import Membership, OperationKind, OperationStatus
from epistrel.core.tables.base import metadata, pg_enum, ulid_type

MUTABLE_OPERATION_COLUMNS: tuple[str, ...] = (
    "status",
    "membership",
    "membership_tx",
    "narrative_turn",
    "turn_variant",
    "base_revision",
    "read_set",
    "config_revisions",
    "committed_at",
    "attempts",
    "turn_report",
    "viewpoint",
)
"""The only ``operations`` columns an UPDATE may change (#44's engine-wide allow-list)."""

ACTIVE_TURN_PREDICATE = "kind = 'turn' AND membership = 'active' AND status = 'committed'"
"""The rows ``uq_operations_active_turn`` covers: a story's active committed turn variants."""

operations = Table(
    "operations",
    metadata,
    Column("operation_id", ulid_type(), primary_key=True),
    Column("story_id", ulid_type(), ForeignKey("stories.id"), nullable=True),
    Column("kind", pg_enum(OperationKind, "operation_kind"), nullable=False),
    Column("narrative_turn", Integer, nullable=True),
    Column("turn_variant", SmallInteger, nullable=True),
    Column("slot_k", SmallInteger, nullable=True),
    Column(
        "status",
        pg_enum(OperationStatus, "operation_status"),
        nullable=False,
        server_default=text("'in_flight'"),
    ),
    Column("principal", Text, nullable=False),
    Column("base_revision", BigInteger, nullable=True),
    Column("read_set", JSONB, nullable=True),
    Column("config_revisions", JSONB, nullable=True),
    Column("source_window", JSONB, nullable=True),
    Column(
        "membership",
        pg_enum(Membership, "membership"),
        nullable=False,
        server_default=text("'active'"),
    ),
    Column("membership_tx", TIMESTAMP(timezone=True), nullable=True),
    Column("attempts", JSONB, nullable=False, server_default=text("'[]'::jsonb")),
    Column("turn_report", JSONB, nullable=True),
    Column("viewpoint", Text, nullable=True),
    Column(
        "started_at",
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=text("transaction_timestamp()"),
    ),
    Column("committed_at", TIMESTAMP(timezone=True), nullable=True),
    CheckConstraint(
        "kind <> 'turn' OR status <> 'committed' OR narrative_turn IS NOT NULL",
        name="turn_has_position",
    ),
    Index("ix_operations_story_position", "story_id", "narrative_turn", "turn_variant"),
    # TIME (f) / FR-STORE-28: exactly one active committed variant per turn index (#47).
    Index(
        "uq_operations_active_turn",
        "story_id",
        "narrative_turn",
        unique=True,
        postgresql_where=text(ACTIVE_TURN_PREDICATE),
    ),
)
