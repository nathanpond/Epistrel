"""``genesis_deferred``: what a story's genesis could not use yet (FR-SCN-9,11; docs/02 §4.10;
#122).

A story created from a scenario keeps every field its pipeline cannot use as one row per item:
each knowledge seed's structured form, each relationship, location, entity and name override,
each story rule, predicate and closed set, each State tracker and starting value, the starting
clock and each scheduled event, and the player persona's audience-limited seeding. A row names
the item (``field_ref``, the dotted path with indices: ``cast.roles[0].seeded_memories[1]``), its
FR-SCN-11 class, its ``location`` (#111's ``EntryLocation`` shape: the scenario, its version,
the role, the field and the seed id), the one capability it needs (a #106 module name, such as
``structured`` or ``states``) and its ``payload``: the original JSON as the scenario holds it,
with what genesis resolved (a role's story character id), so a later materialisation (M4 to M7)
needs no re-parse. ``materialized_at`` stays ``NULL`` until then.

A primary table written in the genesis transaction (``operation_id`` is the genesis operation):
a story table with no foreign key, so story deletion enumerates it (``stories.STORY_TABLES``)
and a fork copies it with the genesis operation it belongs to.
"""

from __future__ import annotations

from sqlalchemy import TIMESTAMP, Column, Index, Table, Text, text
from sqlalchemy.dialects.postgresql import JSONB

from epistrel.core.tables.base import metadata, ulid_type

genesis_deferred = Table(
    "genesis_deferred",
    metadata,
    Column("id", ulid_type(), primary_key=True),
    Column("story_id", ulid_type(), nullable=False),
    Column("operation_id", ulid_type(), nullable=False),
    Column("field_ref", Text, nullable=False),
    Column("field_class", Text, nullable=False),
    Column("location", JSONB, nullable=False),
    Column("capability_needed", Text, nullable=False),
    Column("payload", JSONB, nullable=False),
    Column(
        "created_at",
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=text("transaction_timestamp()"),
    ),
    Column("materialized_at", TIMESTAMP(timezone=True), nullable=True),
)

Index("ix_genesis_deferred_story_id", genesis_deferred.c.story_id)

__all__ = ["genesis_deferred"]
