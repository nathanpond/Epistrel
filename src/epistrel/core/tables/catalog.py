"""The character catalog, in its own schema ``catalog`` (docs/02 §4.10; FR-CAT-1,3; #103).

``character_catalog`` holds one row per entry: its owner principal (FR-AUTH-5), its listing
status (FR-CAT-11; the literal ``private`` until #110 makes the set configurable), its version
status (``catalog.entry_status``), its latest published version (``current_version``, NULL
before the first publish) and its one draft: a ``jsonb`` document with the ``draft_revision``
every save and publish compares (FR-CAT-3). ``created_at`` and ``updated_at`` are written by the
application (``core.clock.now()``).

``character_versions`` holds one row per publish, ``(catalog_id, version)``, with the published
document (``content``), the content rating's version it was published under, and who published
it when. Versions are immutable: a trigger refuses every UPDATE, DELETE and TRUNCATE with
SQLSTATE ``EPI01``, except an UPDATE or DELETE in a transaction that set ``epistrel.erasure`` to
``on`` (catalog delete, and #124's erasure). The foreign key cascades a deleted entry's versions.

Catalog tables live outside story data: no story table references them, and they reference no
story (FR-CAT-1); a story names where a character came from with identifiers only
(``characters.source_catalog_id``/``source_catalog_version``, FR-CAT-9). Unlike story tables,
catalog tables may use foreign keys among themselves.
"""

from __future__ import annotations

from sqlalchemy import TIMESTAMP, Column, ForeignKey, Index, Integer, Table, Text
from sqlalchemy.dialects.postgresql import JSONB

from epistrel.core.enums import EntryStatus
from epistrel.core.tables.base import metadata, pg_enum, ulid_type
from epistrel.core.tables.events import APPEND_ONLY_SQLSTATE

CATALOG_SCHEMA = "catalog"
"""The Postgres schema every catalog table lives in."""

ERASURE_GUC = "epistrel.erasure"
"""Set (transaction-local) to ``on`` to let catalog versions be deleted or rewritten: catalog
delete sets it, and #124's erasure shares it."""

VERSIONS_IMMUTABLE_SQLSTATE = APPEND_ONLY_SQLSTATE
"""Raised by the ``character_versions`` trigger on a refused UPDATE, DELETE or TRUNCATE."""

character_catalog = Table(
    "character_catalog",
    metadata,
    Column("id", ulid_type(), primary_key=True),
    Column("owner_principal", Text, nullable=False),
    Column("listing_status", Text, nullable=False),
    Column(
        "status",
        pg_enum(EntryStatus, "entry_status", schema=CATALOG_SCHEMA),
        nullable=False,
    ),
    Column("current_version", Integer, nullable=True),
    Column("draft", JSONB, nullable=False),
    Column("draft_revision", Integer, nullable=False),
    Column("created_at", TIMESTAMP(timezone=True), nullable=False),
    Column("updated_at", TIMESTAMP(timezone=True), nullable=False),
    schema=CATALOG_SCHEMA,
)

# Listing by owner and principal lookup (#104, #117) read the owner column.
Index("ix_character_catalog_owner_principal", character_catalog.c.owner_principal)

character_versions = Table(
    "character_versions",
    metadata,
    Column(
        "catalog_id",
        ulid_type(),
        ForeignKey(character_catalog.c.id, ondelete="CASCADE"),
        primary_key=True,
    ),
    Column("version", Integer, primary_key=True),
    Column("content", JSONB, nullable=False),
    Column("content_rating_version_id", ulid_type(), nullable=True),
    Column("published_at", TIMESTAMP(timezone=True), nullable=False),
    Column("published_by", Text, nullable=False),
    schema=CATALOG_SCHEMA,
)
