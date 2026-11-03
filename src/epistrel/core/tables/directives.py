"""``directives``: an author's directions to a story (FR-DIRECT-1,2,6; docs/02's
``apply_directive``; #226, migration 0119).

A directive is ``ephemeral`` (guidance for the next turn: ``disposition = steer_queued`` until a
committed turn consumes it, ``consumed_by_operation_id``) or ``durable`` (a change to the world:
``applied`` at an inter-turn slot by ``operation_id``, or ``routed_to_rewrite`` into a rewrite
session, ``rewrite_session_id``, when it reaches into the past). ``classified_by`` says whether
the request or the model chose the kind; ``changes`` is what the parse made of the text. Story
scoped: deleted with its story, never copied by a fork. ``orchestrator.directives`` is the only
writer.
"""

from __future__ import annotations

from sqlalchemy import TIMESTAMP, CheckConstraint, Column, Index, Table, Text, text
from sqlalchemy.dialects.postgresql import JSONB

from epistrel.core.tables.base import metadata, ulid_type

DISPOSITIONS = (
    "steer_queued",
    "applied",
    "routed_to_rewrite",
    "routed_write",
    "directed_regeneration",
    "expired",
)

directives = Table(
    "directives",
    metadata,
    Column("id", ulid_type(), primary_key=True),
    Column("story_id", ulid_type(), nullable=False),
    Column("principal", Text, nullable=False),
    Column("text", Text, nullable=False),
    Column("kind", Text, nullable=False),
    Column("classified_by", Text, nullable=False),
    Column("disposition", Text, nullable=False),
    Column("temporal_scope", Text, nullable=False, server_default=text("'now'")),
    Column("changes", JSONB, nullable=False, server_default=text("'[]'")),
    Column("operation_id", ulid_type(), nullable=True),
    Column("rewrite_session_id", ulid_type(), nullable=True),
    Column("consumed_by_operation_id", ulid_type(), nullable=True),
    Column(
        "created_at",
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=text("transaction_timestamp()"),
    ),
    CheckConstraint("kind IN ('ephemeral', 'durable')", name="kind"),
    CheckConstraint("classified_by IN ('model', 'request')", name="classified_by"),
    CheckConstraint(
        "disposition IN (" + ", ".join(repr(d) for d in DISPOSITIONS) + ")", name="disposition"
    ),
    CheckConstraint("temporal_scope IN ('now', 'past')", name="temporal_scope"),
    Index("ix_directives_story", "story_id", "created_at"),
    Index(
        "ix_directives_queued",
        "story_id",
        postgresql_where=text("disposition = 'steer_queued' AND consumed_by_operation_id IS NULL"),
    ),
)

__all__ = ["DISPOSITIONS", "directives"]
