"""``stories``: the story row whose lock serialises every commit to a story (FR-CONC-2).

Minimal here (#45); #50 extends it with settings and the rest of the story's columns.
"""

from __future__ import annotations

from sqlalchemy import TIMESTAMP, BigInteger, Column, Integer, Table, Text, text

from epistrel.core.tables.base import metadata, ulid_type

INITIAL_TURN_INDEX = 1
"""``stories.next_turn_index`` of a new story: the first turn is turn 1."""

stories = Table(
    "stories",
    metadata,
    Column("id", ulid_type(), primary_key=True),
    Column("status", Text, nullable=False, server_default=text("'ready'")),
    Column("timeline_revision", BigInteger, nullable=False, server_default=text("0")),
    Column("config_revision", BigInteger, nullable=False, server_default=text("0")),
    Column(
        "next_turn_index",
        Integer,
        nullable=False,
        server_default=text(str(INITIAL_TURN_INDEX)),
    ),
    Column(
        "created_at",
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=text("transaction_timestamp()"),
    ),
)
