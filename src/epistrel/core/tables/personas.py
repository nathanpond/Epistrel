"""``personas``: the player personas principals save for reuse (FR-CAT-6; docs/02 §4.10; #114).

A persona is what a player plays as: a name, an optional gender, an age (the persona's true age,
FR-CAT-12) and a short description. It is owned by a principal (FR-AUTH-5), like every owned
type, and is **not** a catalog entry: it lives in the default schema, beside the stories that
snapshot it, not in ``catalog``. A name is unique per owner, ignoring case
(``uq_personas_owner_name`` on ``(owner_principal, lower(name))``), and ``(updated_at, id)`` is
the list's keyset, newest first. ``created_at`` and ``updated_at`` are written by the
application (``core.clock.now()``); ``updated_at`` moves on every edit and owner transfer.

A story never references this table: a player character keeps a snapshot of the persona it was
created with (``characters.persona_snapshot``) and its id as a plain identifier
(``characters.persona_id``, no foreign key), so editing or deleting a persona never changes a
story (FR-CAT-6, FR-STORE-14).
"""

from __future__ import annotations

from sqlalchemy import (
    TIMESTAMP,
    CheckConstraint,
    Column,
    Index,
    Integer,
    Table,
    Text,
    func,
)

from epistrel.core.tables.base import metadata, ulid_type

AGE_MIN = 0
AGE_MAX = 150
"""A persona's age is its true age, so it has #50's bounds (``characters.true_age``)."""

personas = Table(
    "personas",
    metadata,
    Column("id", ulid_type(), primary_key=True),
    Column("owner_principal", Text, nullable=False),
    Column("name", Text, nullable=False),
    Column("gender", Text, nullable=True),
    Column("age", Integer, nullable=False),
    Column("description", Text, nullable=False),
    Column("created_at", TIMESTAMP(timezone=True), nullable=False),
    Column("updated_at", TIMESTAMP(timezone=True), nullable=False),
    CheckConstraint(f"age BETWEEN {AGE_MIN} AND {AGE_MAX}", name="age"),
)

NAME_INDEX = "uq_personas_owner_name"
"""The unique index a second persona of one owner with the same name (ignoring case) breaks:
409 ``persona_name_taken``, on create, on a rename and on an owner transfer."""

Index(NAME_INDEX, personas.c.owner_principal, func.lower(personas.c.name), unique=True)
Index("ix_personas_updated_at_id", personas.c.updated_at, personas.c.id)
