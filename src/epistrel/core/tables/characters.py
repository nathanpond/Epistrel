"""``characters``: the cast a story was created with (FR-ADMIN-1, FR-CAT-12).

A primary table, not a projection: the ``story_created`` event names each character (id, name,
``is_player``) but never its age, so the true age lives only here and no corrected view can
leak it. ``story_id`` is plain and NOT NULL (no foreign key; story deletion enumerates it), and
``ordinal`` fixes the cast order. Names are unique per story, ignoring case.

``definition`` is free text the Lite pipeline renders verbatim; structured catalog characters
arrive in M3. ``safe_fallback_text`` is the character's line when every regenerated reply fails
its guardrails (#68).
"""

from __future__ import annotations

from sqlalchemy import (
    TIMESTAMP,
    Boolean,
    CheckConstraint,
    Column,
    Index,
    Integer,
    SmallInteger,
    Table,
    Text,
    func,
    text,
)

from epistrel.core.tables.base import metadata, ulid_type

TRUE_AGE_MIN = 0
TRUE_AGE_MAX = 150

characters = Table(
    "characters",
    metadata,
    Column("id", ulid_type(), primary_key=True),
    Column("story_id", ulid_type(), nullable=False),
    Column("ordinal", SmallInteger, nullable=False),
    Column("name", Text, nullable=False),
    Column("definition", Text, nullable=False),
    Column("true_age", Integer, nullable=False),
    Column("age_recorded_at", TIMESTAMP(timezone=True), nullable=True),
    Column("is_player", Boolean, nullable=False, server_default=text("false")),
    Column("safe_fallback_text", Text, nullable=True),
    Column(
        "created_at",
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=text("transaction_timestamp()"),
    ),
    CheckConstraint(f"true_age BETWEEN {TRUE_AGE_MIN} AND {TRUE_AGE_MAX}", name="true_age"),
)

Index("uq_characters_story_name", characters.c.story_id, func.lower(characters.c.name), unique=True)
Index("uq_characters_story_ordinal", characters.c.story_id, characters.c.ordinal, unique=True)
