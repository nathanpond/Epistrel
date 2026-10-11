"""``client_events``: the dedupe record of every ingested event (FR-RESP-10; #58).

One row per ``(story_id, client_event_id)`` (the primary key is the uniqueness rule): the
SHA-256 of the event's canonical payload, the ``turn`` operation that recorded it, and the
result the first ingest returned (``operation_id``, ``turn``, ``timeline_revision``), kept for
the life of the story. ``story_id`` is plain (no foreign key; story deletion enumerates it).
"""

from __future__ import annotations

from sqlalchemy import CHAR, TIMESTAMP, Column, PrimaryKeyConstraint, Table, Text, text
from sqlalchemy.dialects.postgresql import JSONB

from epistrel.core.tables.base import metadata, ulid_type

client_events = Table(
    "client_events",
    metadata,
    Column("story_id", ulid_type(), nullable=False),
    Column("client_event_id", Text, nullable=False),
    Column("payload_hash", CHAR(64), nullable=False),
    Column("operation_id", ulid_type(), nullable=False),
    Column("result", JSONB, nullable=False),
    Column(
        "created_at",
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=text("transaction_timestamp()"),
    ),
    PrimaryKeyConstraint("story_id", "client_event_id"),
)
