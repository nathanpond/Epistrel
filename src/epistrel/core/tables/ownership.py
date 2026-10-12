"""Ownership's two tables (FR-AUTH-5,6; docs/02 §4.10; #104), in the default schema.

``entity_relations`` holds one row per (entity, relation name, principal): consumer-named
relations such as ``editor`` or ``player``. Epistrel stores the name and never interprets it
(FR-AUTH-6, FR-AUTH-8). Names are 1 to 64 characters of ``A-Z a-z 0-9 _ -``, so they never collide
with a path segment or with the ``name:principal`` list filter. Each row records who added it
and when.

``ownership_events`` records every ownership change with the acting principal and the
transaction time. The kinds are an owner ``transfer``, ``relation_added``, ``relation_removed``,
a catalog entry's ``listing_status`` change (#110), and ``deleted`` (the entity was deleted and
its relations went with it). ``old`` and ``new`` hold the values before and after: owners as JSON
strings, relations as ``{principal, relation}``, and ``deleted``'s ``old`` as
``{owner_principal, relations}``. ``job_id`` names the background job that made the change
(#117's offboarding), and is NULL otherwise.

Neither table names a story by a ``story_id`` column: an entity is ``(entity_type, entity_id)``.
So a story deletion removes the story's relations through ``core.ownership.drop_entity``, not
through ``core.stories.STORY_TABLES``. The ownership events stay as the deployment's record.
"""

from __future__ import annotations

from enum import StrEnum

from sqlalchemy import TIMESTAMP, CheckConstraint, Column, Index, Table, Text, text
from sqlalchemy.dialects.postgresql import JSONB

from epistrel.core.tables.base import metadata, ulid_type


class EntityType(StrEnum):
    """Every owned entity type (FR-AUTH-5), as ``entity_type`` columns and the registry in
    ``core.ownership`` name them."""

    STORY = "story"
    CHARACTER = "character"
    SCENARIO = "scenario"
    PERSONA = "persona"
    MEDIA = "media"
    ENGINE_PROFILE = "engine_profile"


class OwnershipEventKind(StrEnum):
    """What an ``ownership_events`` row records."""

    TRANSFER = "transfer"
    RELATION_ADDED = "relation_added"
    RELATION_REMOVED = "relation_removed"
    LISTING_STATUS = "listing_status"
    DELETED = "deleted"


RELATION_NAME_PATTERN = r"^[A-Za-z0-9_-]{1,64}$"
"""A relation name as stored: 1 to 64 of ``A-Z a-z 0-9 _ -``, case-sensitive."""


def _in(column: str, values: type[StrEnum]) -> str:
    return f"{column} IN (" + ", ".join(f"'{v.value}'" for v in values) + ")"


ENTITY_TYPE_CHECK = _in("entity_type", EntityType)
KIND_CHECK = _in("kind", OwnershipEventKind)
RELATION_CHECK = f"relation ~ '{RELATION_NAME_PATTERN}'"

entity_relations = Table(
    "entity_relations",
    metadata,
    Column("entity_type", Text, primary_key=True),
    Column("entity_id", ulid_type(), primary_key=True),
    Column("principal", Text, primary_key=True),
    Column("relation", Text, primary_key=True),
    Column(
        "created_at",
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=text("transaction_timestamp()"),
    ),
    Column("created_by", Text, nullable=False),
    CheckConstraint(ENTITY_TYPE_CHECK, name="entity_type"),
    CheckConstraint(RELATION_CHECK, name="relation"),
)
"""The primary key is ``(entity_type, entity_id, principal, relation)``. Its leading columns are
the ``(entity_type, entity_id)`` index that an entity's relations and the list filters use. The
principal index serves principal lookup (#117)."""

Index("ix_entity_relations_principal", entity_relations.c.principal)

ownership_events = Table(
    "ownership_events",
    metadata,
    Column("id", ulid_type(), primary_key=True),
    Column("entity_type", Text, nullable=False),
    Column("entity_id", ulid_type(), nullable=False),
    Column("kind", Text, nullable=False),
    Column("old", JSONB, nullable=True),
    Column("new", JSONB, nullable=True),
    Column("acting_principal", Text, nullable=False),
    Column("job_id", ulid_type(), nullable=True),
    Column(
        "at",
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=text("transaction_timestamp()"),
    ),
    CheckConstraint(ENTITY_TYPE_CHECK, name="entity_type"),
    CheckConstraint(KIND_CHECK, name="kind"),
)

Index(
    "ix_ownership_events_entity",
    ownership_events.c.entity_type,
    ownership_events.c.entity_id,
    ownership_events.c.at,
)

__all__ = [
    "ENTITY_TYPE_CHECK",
    "KIND_CHECK",
    "RELATION_CHECK",
    "RELATION_NAME_PATTERN",
    "EntityType",
    "OwnershipEventKind",
    "entity_relations",
    "ownership_events",
]
