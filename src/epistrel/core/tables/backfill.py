"""Backfill (FR-PROF-8,9; ADR-086; #228, migration 0114).

- ``story_backfills``: one row per backfill of a story: its job and its ``backfill``
  operation, ``status`` (``queued``, ``extracting``, ``reconciling``, ``paused_for_conflicts``
  (#230), ``completed``, ``cancelled``, ``failed``), ``kind`` (``upgrade``, ``gap_fill``,
  ``resummarize`` (#232)), the configuration it came from (``source``) and the one it applies on
  completion (``target``), the capabilities it re-derives over its turn range, its chunk counts
  and reconciliation cursor, who asked, its ``report`` and ``error``. At most one row of a story
  is non-terminal (a partial unique index).
- ``backfill_chunks``: the extraction checkpoint, one row per chunk of whole turns: its range,
  ``status`` (``pending``, ``extracted``) and what extraction found (``extraction``).
- ``backfill_conflicts``: a contradiction reconciliation met between statements about one
  subject (``subject``, ``statements``, ``valid_time``) and how it was resolved (#228 writes
  ``last_mention``'s ``resolved`` rows; #230 the open ones and the route).
- ``capability_coverage``: for each data-producing capability, the turn intervals of the story
  that have its data (``source`` ``live`` or ``backfill``; ``level`` ``prompt_only``/``on`` for
  the clock and States). A backfill's range is the active turns outside them
  (``core.backfill.coverage.gaps``).

None of them is a derived record; a fork copies ``capability_coverage`` clipped to its fork
point and none of the backfill tables.
"""

from __future__ import annotations

from sqlalchemy import (
    TIMESTAMP,
    CheckConstraint,
    Column,
    ForeignKey,
    Index,
    Integer,
    PrimaryKeyConstraint,
    Table,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB

from epistrel.core.tables.base import metadata, ulid_type

NOW = text("transaction_timestamp()")
ACTIVE_STATUSES = ("queued", "extracting", "reconciling", "paused_for_conflicts")
"""A backfill in one of these statuses is running (the story is paused)."""

story_backfills = Table(
    "story_backfills",
    metadata,
    Column("id", ulid_type(), primary_key=True),
    Column("story_id", ulid_type(), nullable=False),
    Column("job_id", ulid_type(), nullable=True),
    Column("operation_id", ulid_type(), nullable=True),
    Column("status", Text, nullable=False, server_default=text("'queued'")),
    Column("kind", Text, nullable=False, server_default=text("'upgrade'")),
    Column("source", JSONB, nullable=False, server_default=text("'{}'")),
    Column("target", JSONB, nullable=False, server_default=text("'{}'")),
    Column("capabilities", ARRAY(Text), nullable=False),
    Column("range_from_turn", Integer, nullable=False),
    Column("range_to_turn", Integer, nullable=False),
    Column("chunks_total", Integer, nullable=False, server_default=text("0")),
    Column("chunks_done", Integer, nullable=False, server_default=text("0")),
    Column("reconcile_cursor", Integer, nullable=True),
    Column("requested_by", Text, nullable=False),
    Column("report", JSONB, nullable=False, server_default=text("'{}'")),
    Column("error", Text, nullable=True),
    Column("created_at", TIMESTAMP(timezone=True), nullable=False, server_default=NOW),
    Column("started_at", TIMESTAMP(timezone=True), nullable=True),
    Column("finished_at", TIMESTAMP(timezone=True), nullable=True),
    CheckConstraint(
        "status IN ('queued', 'extracting', 'reconciling', 'paused_for_conflicts', 'completed', "
        "'cancelled', 'failed')",
        name="status",
    ),
    CheckConstraint("kind IN ('upgrade', 'gap_fill', 'resummarize')", name="kind"),
    Index("ix_story_backfills_story", "story_id", "created_at"),
    Index(
        "uq_story_backfills_running",
        "story_id",
        unique=True,
        postgresql_where=text(
            "status IN ('queued', 'extracting', 'reconciling', 'paused_for_conflicts')"
        ),
    ),
)

backfill_chunks = Table(
    "backfill_chunks",
    metadata,
    Column("backfill_id", ulid_type(), ForeignKey("story_backfills.id"), nullable=False),
    Column("story_id", ulid_type(), nullable=False),
    Column("chunk_index", Integer, nullable=False),
    Column("from_turn", Integer, nullable=False),
    Column("to_turn", Integer, nullable=False),
    Column("status", Text, nullable=False, server_default=text("'pending'")),
    Column("extraction", JSONB, nullable=True),
    Column("extracted_at", TIMESTAMP(timezone=True), nullable=True),
    PrimaryKeyConstraint("backfill_id", "chunk_index"),
    CheckConstraint("status IN ('pending', 'extracted')", name="status"),
)

backfill_conflicts = Table(
    "backfill_conflicts",
    metadata,
    Column("id", ulid_type(), primary_key=True),
    Column("backfill_id", ulid_type(), ForeignKey("story_backfills.id"), nullable=False),
    Column("story_id", ulid_type(), nullable=False),
    Column("subject", JSONB, nullable=False),
    Column("statements", JSONB, nullable=False, server_default=text("'[]'")),
    Column("valid_time", JSONB, nullable=True),
    Column("status", Text, nullable=False, server_default=text("'resolved'")),
    Column("resolution", JSONB, nullable=True),
    Column("resolved_by", Text, nullable=True),
    Column("resolved_at", TIMESTAMP(timezone=True), nullable=True),
    CheckConstraint("status IN ('open', 'resolved', 'uncertain')", name="status"),
    Index("ix_backfill_conflicts_backfill", "story_id", "backfill_id"),
)

capability_coverage = Table(
    "capability_coverage",
    metadata,
    Column("id", ulid_type(), primary_key=True),
    Column("story_id", ulid_type(), nullable=False),
    Column("capability", Text, nullable=False),
    Column("from_turn", Integer, nullable=False),
    Column("to_turn", Integer, nullable=True),
    Column("level", Text, nullable=True),
    Column("source", Text, nullable=False),
    Column("operation_id", ulid_type(), nullable=True),
    Column("created_at", TIMESTAMP(timezone=True), nullable=False, server_default=NOW),
    CheckConstraint("source IN ('live', 'backfill')", name="source"),
    CheckConstraint(
        "level IS NULL OR level IN ('prompt_only', 'on', 'lite', 'structured')", name="level"
    ),
    CheckConstraint("to_turn IS NULL OR to_turn >= from_turn", name="range"),
    Index("ix_capability_coverage_story", "story_id", "capability", "from_turn"),
)

__all__ = [
    "ACTIVE_STATUSES",
    "backfill_chunks",
    "backfill_conflicts",
    "capability_coverage",
    "story_backfills",
]
