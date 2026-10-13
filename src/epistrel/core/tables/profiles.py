"""Engine profiles and their versions (FR-PROF-1; docs/02 §4.11; #106), in the default schema.

``engine_profiles`` holds one row per profile: its name, its one owner principal (FR-AUTH-5;
``system`` for a preset), whether it shipped as a ``preset`` (provenance only: a preset is
edited, copied and deleted like any profile), its latest version (``current_version``), when it
was created and last changed (``updated_at``: a new version, a rename, a transfer or a delete),
and ``deleted_at`` once it was deleted. A deleted profile still reads and serves the stories
that use it; the ``retention_sweep`` purges it once no story does.

``engine_profile_versions`` holds one row per version, ``(profile_id, version)`` from 1: the
settings document as stored (``ProfileSettings``, possibly sparse), who made it and when. A
version is never changed; the purge removes a profile's versions with it (the foreign key
cascades).

``stories.engine_profile_id`` and ``deployment_state.default_engine_profile_id`` reference
``engine_profiles``: a profile that a story uses, or that is the default, cannot be purged.
"""

from __future__ import annotations

from sqlalchemy import (
    TIMESTAMP,
    Boolean,
    CheckConstraint,
    Column,
    ForeignKey,
    Index,
    Integer,
    Table,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB

from epistrel.core.tables.base import metadata, ulid_type

engine_profiles = Table(
    "engine_profiles",
    metadata,
    Column("id", ulid_type(), primary_key=True),
    Column("name", Text, nullable=False),
    Column("owner_principal", Text, nullable=False),
    Column("preset", Boolean, nullable=False, server_default=text("false")),
    Column("current_version", Integer, nullable=False),
    Column("created_at", TIMESTAMP(timezone=True), nullable=False),
    Column("updated_at", TIMESTAMP(timezone=True), nullable=False),
    Column("deleted_at", TIMESTAMP(timezone=True), nullable=True),
    CheckConstraint("current_version >= 1", name="current_version"),
)

# Listing by owner and principal lookup (#104, #117) read the owner column.
Index("ix_engine_profiles_owner_principal", engine_profiles.c.owner_principal)

engine_profile_versions = Table(
    "engine_profile_versions",
    metadata,
    Column(
        "profile_id",
        ulid_type(),
        ForeignKey(engine_profiles.c.id, ondelete="CASCADE"),
        primary_key=True,
    ),
    Column("version", Integer, primary_key=True),
    Column("settings", JSONB, nullable=False),
    Column("created_by", Text, nullable=False),
    Column("created_at", TIMESTAMP(timezone=True), nullable=False),
    CheckConstraint("version >= 1", name="version"),
)

__all__ = ["engine_profile_versions", "engine_profiles"]
