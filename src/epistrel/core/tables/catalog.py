"""The catalog, in its own schema ``catalog`` (docs/02 §4.10; FR-CAT-1,3, FR-SCN-1; #103, #108).

Each kind of entry has two tables. ``character_catalog`` and ``scenario_catalog`` hold one row
per entry: its owner principal (FR-AUTH-5), its listing status (FR-CAT-11: one of the
deployment's ``listing_status_values``, checked by the application, never by a constraint, so an
operator adds a value without a migration; #110), its version status
(``catalog.entry_status``), its latest published version (``current_version``, NULL before the
first publish) and its one draft: a ``jsonb`` document with the ``draft_revision`` every save
and publish compares (FR-CAT-3). ``created_at`` and ``updated_at`` are written by the
application (``core.clock.now()``); ``updated_at`` moves on every draft save, publish, archive,
listing-status change and owner transfer, and ``(updated_at, id)`` is the listings' keyset
(#110). A scenario also has ``companion_of``: the catalog character whose companion it is
(#116), a foreign key that cascades, so deleting a character takes its companion with it.

``character_versions`` and ``scenario_versions`` hold one row per publish, ``(catalog_id,
version)``, with the published document (``content``), the content rating's version it was
published under, and who published it when. A scenario's roles, seeds and relationships are
part of that document (jsonb), not rows of their own. A companion's version also records the
character version its locked role is pinned to (``pinned_character_id``,
``pinned_character_version``; #116): a foreign key to ``character_versions`` that is set to
NULL if that character version is erased. Versions are immutable: a trigger refuses every
UPDATE, DELETE and TRUNCATE with SQLSTATE ``EPI01``, except an UPDATE or DELETE in a
transaction that set ``epistrel.erasure`` to ``on`` (catalog delete, and #124's erasure). The
foreign key cascades a deleted entry's versions.

A scenario version also keeps ``report``, the validation report its publish produced (#111;
``NULL`` for versions published before). Each version also has ``search``, a generated
``tsvector`` of its public card (#110), GIN indexed: ``q`` searches an entry's latest published
version, so drafts are never searchable. It is computed by
``catalog.epistrel_character_search`` and ``catalog.epistrel_scenario_search`` (``IMMUTABLE``
SQL functions over the content, ``CHARACTER_SEARCH``/``SCENARIO_SEARCH``): the name (weight A),
the labels and aliases, or a scenario's labels and role public labels (weight B), and the public
description (weight C), parsed with the ``simple`` configuration (no stemming, no stop words),
never a private field. A version rewritten by erasure (#124) gets its
vector recomputed with it.

Catalog tables live outside story data: no story table references them, and they reference no
story (FR-CAT-1); a story names where a character came from with identifiers only
(``characters.source_catalog_id``/``source_catalog_version``, FR-CAT-9). Unlike story tables,
catalog tables may use foreign keys among themselves.
"""

from __future__ import annotations

from sqlalchemy import (
    TIMESTAMP,
    CheckConstraint,
    Column,
    Computed,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Table,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR

from epistrel.core.enums import EntryStatus
from epistrel.core.tables.base import metadata, pg_enum, ulid_type
from epistrel.core.tables.events import APPEND_ONLY_SQLSTATE

CATALOG_SCHEMA = "catalog"
"""The Postgres schema every catalog table lives in."""

SEARCH_CONFIG = "simple"
"""The text-search configuration of the versions' ``search`` vectors and of every ``q``
(``websearch_to_tsquery``): words as written, lower-cased, without stemming or stop words, so a
label matches exactly and no language is assumed (#110)."""
CHARACTER_SEARCH = "catalog.epistrel_character_search(content)"
"""A character version's ``search`` vector (#110; the function is migration 0027's)."""
SCENARIO_SEARCH = "catalog.epistrel_scenario_search(content)"
"""A scenario version's ``search`` vector (#110; the function is migration 0027's)."""

ERASURE_GUC = "epistrel.erasure"
"""Set (transaction-local) to ``on`` to let catalog versions be deleted or rewritten: catalog
delete sets it, and #124's erasure shares it."""

VERSIONS_IMMUTABLE_SQLSTATE = APPEND_ONLY_SQLSTATE
"""Raised by the versions triggers on a refused UPDATE, DELETE or TRUNCATE."""

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

# Listing by owner and principal lookup (#104, #117) read the owner column; the listings page
# newest first on (updated_at, id) (#110).
Index("ix_character_catalog_owner_principal", character_catalog.c.owner_principal)
Index("ix_character_catalog_updated_at_id", character_catalog.c.updated_at, character_catalog.c.id)

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
    Column("search", TSVECTOR, Computed(CHARACTER_SEARCH, persisted=True)),
    schema=CATALOG_SCHEMA,
)

Index("ix_character_versions_search", character_versions.c.search, postgresql_using="gin")

scenario_catalog = Table(
    "scenario_catalog",
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
    Column(
        "companion_of",
        ulid_type(),
        ForeignKey(character_catalog.c.id, ondelete="CASCADE"),
        nullable=True,
    ),
    Column("created_at", TIMESTAMP(timezone=True), nullable=False),
    Column("updated_at", TIMESTAMP(timezone=True), nullable=False),
    schema=CATALOG_SCHEMA,
)

# Listing by owner and principal lookup (#104, #117); a character's companions (#104's transfer,
# #116, and the cascade of a character's delete); the listings' keyset (#110).
Index("ix_scenario_catalog_owner_principal", scenario_catalog.c.owner_principal)
Index("ix_scenario_catalog_companion_of", scenario_catalog.c.companion_of)
Index("ix_scenario_catalog_updated_at_id", scenario_catalog.c.updated_at, scenario_catalog.c.id)

PIN_COMPLETE_CHECK = "(pinned_character_id IS NULL) = (pinned_character_version IS NULL)"
"""A companion version's pin names a character and its version together, or nothing."""

scenario_versions = Table(
    "scenario_versions",
    metadata,
    Column(
        "catalog_id",
        ulid_type(),
        ForeignKey(scenario_catalog.c.id, ondelete="CASCADE"),
        primary_key=True,
    ),
    Column("version", Integer, primary_key=True),
    Column("content", JSONB, nullable=False),
    Column("content_rating_version_id", ulid_type(), nullable=True),
    Column("pinned_character_id", ulid_type(), nullable=True),
    Column("pinned_character_version", Integer, nullable=True),
    Column("published_at", TIMESTAMP(timezone=True), nullable=False),
    Column("published_by", Text, nullable=False),
    Column("search", TSVECTOR, Computed(SCENARIO_SEARCH, persisted=True)),
    Column("report", JSONB, nullable=True),
    ForeignKeyConstraint(
        ["pinned_character_id", "pinned_character_version"],
        [character_versions.c.catalog_id, character_versions.c.version],
        ondelete="SET NULL",
    ),
    CheckConstraint(PIN_COMPLETE_CHECK, name="pin_complete"),
    schema=CATALOG_SCHEMA,
)

Index("ix_scenario_versions_search", scenario_versions.c.search, postgresql_using="gin")

# The pin's foreign key: a character version's erasure finds the versions pinned to it.
Index(
    "ix_scenario_versions_pinned_character",
    scenario_versions.c.pinned_character_id,
    scenario_versions.c.pinned_character_version,
)
