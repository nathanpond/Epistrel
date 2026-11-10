"""``consumer_purposes`` and ``consumer_purpose_versions``: model purposes a consumer registers
(FR-MODEL-14; #824, migration 0145).

- **``consumer_purposes``** — one row per registered purpose, deployment-wide
  (``/admin/purposes``): its ULID ``id``, its ``name`` (unique, case-sensitive, immutable; an
  archived name stays taken), when it was created, by whom, and ``archived_at`` (null while it
  is live; there is no unarchive).
- **``consumer_purpose_versions``** — one immutable row per version (``1`` at creation, ``n+1``
  per replacement): the ``template`` that renders the user message, the optional
  ``system_template``, the ``output_schema`` (JSONB, the supported JSON Schema subset), the
  ``description``, when it was created and by whom.

Neither is story-scoped, and neither holds story content: the templates and the schema are
operator configuration. Every write is a deployment configuration change
(``consumer_purpose:<name>``).
"""

from __future__ import annotations

from sqlalchemy import (
    TIMESTAMP,
    CheckConstraint,
    Column,
    ForeignKey,
    Integer,
    PrimaryKeyConstraint,
    Table,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB

from epistrel.core.tables.base import metadata, ulid_type

consumer_purposes = Table(
    "consumer_purposes",
    metadata,
    Column("id", ulid_type(), nullable=False),
    Column("name", Text, nullable=False),
    Column(
        "created_at",
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=text("transaction_timestamp()"),
    ),
    Column("created_by", Text, nullable=False),
    Column("archived_at", TIMESTAMP(timezone=True), nullable=True),
    PrimaryKeyConstraint("id", name="pk_consumer_purposes"),
    UniqueConstraint("name", name="uq_consumer_purposes_name"),
)

consumer_purpose_versions = Table(
    "consumer_purpose_versions",
    metadata,
    Column(
        "purpose_id",
        ulid_type(),
        ForeignKey(
            "consumer_purposes.id",
            name="fk_consumer_purpose_versions_purpose_id_consumer_purposes",
        ),
        nullable=False,
    ),
    Column("version", Integer, nullable=False),
    Column("description", Text, nullable=False),
    Column("template", Text, nullable=False),
    Column("system_template", Text, nullable=True),
    Column("output_schema", JSONB, nullable=False),
    Column(
        "created_at",
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=text("transaction_timestamp()"),
    ),
    Column("created_by", Text, nullable=False),
    PrimaryKeyConstraint("purpose_id", "version", name="pk_consumer_purpose_versions"),
    CheckConstraint("version >= 1", name="version_positive"),
)

__all__ = ["consumer_purpose_versions", "consumer_purposes"]
