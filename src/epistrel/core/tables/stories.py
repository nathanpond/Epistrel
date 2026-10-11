"""``stories``: the story row whose lock serialises every commit to a story (FR-CONC-2).

Minimal here (#45); #50 extends it with the rest of the story's columns. #47 adds the
control-action marker (``last_control_revision``/``last_control_operation_id``: the revision and
operation of the latest truncate, untruncate, swap or regenerate, so an in-flight turn can tell
"history was edited" from "someone else played") and the story ``settings`` (``StorySettings``).
#48 adds ``content_rating_id``.
"""

from __future__ import annotations

from sqlalchemy import TIMESTAMP, BigInteger, Column, Integer, Table, Text, text
from sqlalchemy.dialects.postgresql import JSONB

from epistrel.core.seeds.ratings import DEFAULT_RATING_ID
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
    Column("last_control_revision", BigInteger, nullable=False, server_default=text("0")),
    Column("last_control_operation_id", ulid_type(), nullable=True),
    Column("settings", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    # FR-RATE-1: every story has exactly one rating (#48). The server default is the seeded
    # default; story creation resolves the deployment's current default explicitly.
    Column(
        "content_rating_id",
        ulid_type(),
        nullable=False,
        server_default=text(f"'{DEFAULT_RATING_ID}'"),
    ),
    Column(
        "created_at",
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=text("transaction_timestamp()"),
    ),
)
