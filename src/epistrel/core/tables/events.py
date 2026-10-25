"""``events``: the append-only source of truth (docs/02 §4.4, FR-STORE-1,2,24).

``seq`` is dense per story (``max(seq)+1`` under the story lock) and is recording order only;
narrative position is ``(narrative_turn, turn_variant, beat, step, slot_k)``, copied from the
operation. ``tx_time`` is the commit transaction's time; ``valid_time`` equals it until the
world clock arrives (M6). A trigger refuses every UPDATE, DELETE and TRUNCATE except the two
exemptions in ``0002_event_store`` and erasure's in-place rewrite (``0039_erasure``: under
``epistrel.erasure = on`` an UPDATE may change ``payload``, ``character_id`` and ``redacted_at``
only; SQLSTATE ``EPI01``).
"""

from __future__ import annotations

from sqlalchemy import (
    TIMESTAMP,
    BigInteger,
    Column,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    Table,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB

from epistrel.core.enums import Authority, EventCategory, Provenance
from epistrel.core.tables.base import metadata, pg_enum, ulid_type

APPEND_ONLY_SQLSTATE = "EPI01"
"""Raised by the ``events`` trigger on a refused UPDATE, DELETE or TRUNCATE."""

OPERATION_IMMUTABLE_SQLSTATE = "EPI02"
"""Raised by the ``operations`` trigger when an UPDATE touches a column off the allow-list."""

ALLOW_STORY_DELETE_GUC = "epistrel.allow_story_delete"
"""Set (transaction-local) to a story id to let that story's events be deleted."""

ERASURE_GUC = "epistrel.erasure"
"""Set (transaction-local) to ``on`` to let erasure rewrite events in place (#124; the catalog
versions' triggers honour the same setting)."""

ALLOW_AUDIT_EXPIRY_GUC = "epistrel.allow_audit_expiry"
"""Set (transaction-local) to ``on`` to let audit-category events be deleted (retention, #71)."""

events = Table(
    "events",
    metadata,
    Column("id", ulid_type(), primary_key=True),
    Column("story_id", ulid_type(), ForeignKey("stories.id"), nullable=True),
    Column("seq", BigInteger, nullable=False),
    Column(
        "operation_id",
        ulid_type(),
        ForeignKey("operations.operation_id"),
        nullable=False,
    ),
    Column("category", pg_enum(EventCategory, "event_category"), nullable=False),
    Column("narrative_turn", Integer, nullable=True),
    Column("turn_variant", SmallInteger, nullable=True),
    Column("beat", SmallInteger, nullable=True),
    # FR-STORE-31 (#168): 0 outside declared steps. Nullable: rows written before 0082 keep their
    # ``NULL`` (the log is append-only), which readers take as 0.
    Column("step", SmallInteger, nullable=True, server_default=text("0")),
    Column("slot_k", SmallInteger, nullable=True),
    Column("character_id", ulid_type(), nullable=True),
    Column("principal", Text, nullable=False),
    Column("type", Text, nullable=False),
    Column("payload", JSONB, nullable=False),
    Column(
        "valid_time",
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=text("transaction_timestamp()"),
    ),
    Column(
        "tx_time",
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=text("transaction_timestamp()"),
    ),
    Column("provenance", pg_enum(Provenance, "provenance"), nullable=False),
    Column("authority", pg_enum(Authority, "authority"), nullable=False),
    # When erasure (#124) rewrote the event in place: its payload's matched spans (or a beat the
    # subject authored, whole) became ``[erased]``. Position, kind and operation are kept.
    Column("redacted_at", TIMESTAMP(timezone=True), nullable=True),
    UniqueConstraint("story_id", "seq", name="uq_events_story_id_seq"),
    Index("ix_events_story_position", "story_id", "narrative_turn", "turn_variant"),
    Index("ix_events_story_category", "story_id", "category"),
    Index("ix_events_operation_id", "operation_id"),
    # #182 (migration 0081): an exposure's state is read through its resolutions'
    # ``belief_resolved`` events, matched on the payload's ``exposure_id``.
    Index(
        "ix_events_belief_resolved",
        "story_id",
        text("(payload ->> 'exposure_id')"),
        postgresql_where=text("type = 'belief_resolved'"),
    ),
)
