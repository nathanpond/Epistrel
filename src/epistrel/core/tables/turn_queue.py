"""``turn_queue``: each ``queue`` story's FIFO of turns waiting for the story's turn slot
(FR-CONC-5; #63).

One row per queued turn: the story, a monotonic ``position`` (an identity, so positions only
grow), the turn's ``operations`` row (status ``queued`` until the drainer starts it), its
``kind`` (``respond`` or ``ingest``: a queued ingest batch is one row), the validated
``request`` with the context the drainer runs it under, the requesting ``principal``, the
``Idempotency-Key`` and how many times a crash interrupted it (``attempts``). The drainer
deletes the row in the transaction that commits or fails the turn. ``story_id`` is plain (no
foreign key; story deletion enumerates it).
"""

from __future__ import annotations

from sqlalchemy import (
    TIMESTAMP,
    BigInteger,
    CheckConstraint,
    Column,
    Identity,
    PrimaryKeyConstraint,
    SmallInteger,
    Table,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB

from epistrel.core.tables.base import metadata, ulid_type

KIND_CHECK = "kind IN ('respond', 'ingest')"

turn_queue = Table(
    "turn_queue",
    metadata,
    Column("story_id", ulid_type(), nullable=False),
    Column("position", BigInteger, Identity(always=False), nullable=False),
    Column("operation_id", ulid_type(), nullable=False),
    Column("kind", Text, nullable=False),
    Column("request", JSONB, nullable=False),
    Column("principal", Text, nullable=False),
    Column("idempotency_key", Text, nullable=True),
    Column("attempts", SmallInteger, nullable=False, server_default=text("0")),
    Column(
        "enqueued_at",
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=text("transaction_timestamp()"),
    ),
    PrimaryKeyConstraint("story_id", "position"),
    UniqueConstraint("operation_id"),
    CheckConstraint(KIND_CHECK, name="kind"),
)
