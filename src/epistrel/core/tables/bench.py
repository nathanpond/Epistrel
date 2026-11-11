"""``character_bench``: a story character's bench state, one version per change (FR-MULTI-13;
#831, migration 0150).

One row is one **version** of a character's bench state, written by the operation that changed
it at that operation's position (the derived-record columns plus ``slot_k``, ``beat`` and
``step``; the turn is never ``NULL``):

- a bench (``benched`` true) in ``mode`` (``sequestered``: the character perceives nothing;
  ``present``: #832's sibling mode), with the participation role it had (``previous_role``,
  ``previous_fixed``), which a reactivation restores;
- a reactivation (``benched`` false), ``mode`` being the mode it left.

A character's bench state at a position is its latest active version there (``core.bench``), so
a truncation, an untruncation, a swap and a fork move the bench with the operation that wrote it,
and a bundle carries it with its events. #832 adds a mode switch as another ``benched`` version
in the new mode, valid from its own position. The table is a membership-flipped content table,
not a projection: nothing rebuilds it from facts.
"""

from __future__ import annotations

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    Index,
    SmallInteger,
    Table,
    Text,
    text,
)

from epistrel.core.membership import (
    DerivedTable,
    derived_columns,
    derived_indexes,
    register_derived,
)
from epistrel.core.tables.base import metadata, ulid_type

BENCH_MODES: tuple[str, ...] = ("sequestered", "present")
"""Every mode the table admits (its ``mode`` CHECK). ``core.bench`` says which a request may
choose."""

character_bench = Table(
    "character_bench",
    metadata,
    Column("id", ulid_type(), primary_key=True),
    Column("story_id", ulid_type(), nullable=False),
    Column("character_id", ulid_type(), nullable=False),
    Column("mode", Text, nullable=False),
    Column("benched", Boolean, nullable=False),
    Column("previous_role", Text, nullable=True),
    Column("previous_fixed", Boolean, nullable=True),
    *derived_columns(),
    Column("slot_k", SmallInteger, nullable=False, server_default=text("0")),
    Column("beat", SmallInteger, nullable=False, server_default=text("0")),
    Column("step", SmallInteger, nullable=False, server_default=text("0")),
    CheckConstraint("narrative_turn IS NOT NULL AND turn_variant IS NOT NULL", name="positioned"),
    CheckConstraint("mode IN ('sequestered', 'present')", name="mode"),
    *derived_indexes("character_bench"),
)
Index(
    "ix_character_bench_story_character",
    character_bench.c.story_id,
    character_bench.c.character_id,
)

CHARACTER_BENCH = register_derived(DerivedTable(character_bench))
"""``character_bench`` in ``core.membership.DERIVED_TABLES``: truncation, untruncation, swap,
regenerate and fork move each version with the operation that wrote it."""

__all__ = ["BENCH_MODES", "CHARACTER_BENCH", "character_bench"]
