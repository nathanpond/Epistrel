"""Content ratings (FR-RATE-1,2): a head row per rating and an immutable row per version.

``content_ratings`` is the head: its ``current_version_id`` points at the latest version, its
``name`` mirrors that version's (for the case-insensitive uniqueness of live names), and
``retired_at`` marks a retired rating, which is kept for whatever still uses it. Every edit
inserts a ``content_rating_versions`` row; versions are never updated. References between
ratings, policies and versions are plain ids, without foreign keys (#48).
"""

from __future__ import annotations

from sqlalchemy import (
    TIMESTAMP,
    Column,
    Index,
    Integer,
    Table,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB

from epistrel.core.tables.base import metadata, ulid_type

content_ratings = Table(
    "content_ratings",
    metadata,
    Column("id", ulid_type(), primary_key=True),
    Column("name", Text, nullable=False),
    Column("current_version_id", ulid_type(), nullable=False),
    Column(
        "created_at",
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=text("transaction_timestamp()"),
    ),
    Column("retired_at", TIMESTAMP(timezone=True), nullable=True),
    Column("retired_by", Text, nullable=True),
)

# Names are unique among live ratings, ignoring case; a retired rating's name is reusable.
Index(
    "uq_content_ratings_live_name",
    func.lower(content_ratings.c.name),
    unique=True,
    postgresql_where=text("retired_at IS NULL"),
)

content_rating_versions = Table(
    "content_rating_versions",
    metadata,
    Column("version_id", ulid_type(), primary_key=True),
    Column("rating_id", ulid_type(), nullable=False),
    Column("version", Integer, nullable=False),
    Column("name", Text, nullable=False),
    Column("description", Text, nullable=False),
    Column("display_order", Integer, nullable=False),
    Column("prompt_guidance", Text, nullable=False),
    Column("image_guidance", Text, nullable=False),
    Column("policy_ids", JSONB, nullable=False, server_default=text("'[]'::jsonb")),
    Column("inherits_from", JSONB, nullable=False, server_default=text("'[]'::jsonb")),
    Column(
        "created_at",
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=text("transaction_timestamp()"),
    ),
    Column("created_by", Text, nullable=False),
    UniqueConstraint("rating_id", "version", name="uq_content_rating_versions_rating_version"),
)
