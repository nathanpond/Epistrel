"""``scheduled_events``: canon scheduled events and their lifecycle (FR-CLOCK-9,16,18; docs/02
§7.1; #196, migration 0093).

One row is one **version** of a canon scheduled event: the event is the canon ``scheduled``
proposition it was created with (``proposition_id``, the event's key: every version of one
event names it; a plain column, never a foreign key), and each lifecycle step (created
``pending``, ``due``, ``surfaced``, ``completed``, ``missed``, ``cancelled``, a reschedule back
to ``pending``) is a new row written by the operation that caused it, at its position (the
derived-record columns plus ``slot_k``, ``beat`` and ``step``). The event's state at a position
is its latest active version there (``core.world_clock.stops``), so a truncation, a swap and a
regenerate move the lifecycle with the turns that caused it and a fork copies it through the
fork point. The table is a membership-flipped content table, not a projection: nothing rebuilds
it from facts.

- ``planned_offset_minutes``: the planned time, as a clock offset from the story's start;
  ``planned_anchor``: #197's ``{"at": <frame minute>}`` when it was given as a time of day the
  anchor holds only as a range (``core.world_clock.anchor.Anchor.place``; ``null``: an exact
  offset), the offset then being where the range's lower end reaches it;
- ``owner``: a character id, ``author`` or ``world`` (FR-CLOCK-19); ``shared_owners``: the
  characters it is shared with, who may change it as the owner may (#198, migration 0094; set
  only through the API in M6); ``participants``: character ids; ``place_id``: the event's place
  (``null``: none);
- ``grace_minutes``: how long after its planned time a surfaced event becomes ``missed``;
- ``refinement_pause_anchor_rev``: the anchor revision a ``possibly_due`` pause was taken under
  (#197); ``stopped_clock_at``: the clock offset at which the event stopped an advance (its one
  stop; ``null``: it has not stopped one).
"""

from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Column,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    Table,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB

from epistrel.core.enums import ScheduledStatus
from epistrel.core.membership import (
    DerivedTable,
    derived_columns,
    derived_indexes,
    register_derived,
)
from epistrel.core.tables.base import metadata, pg_enum, ulid_type

OWNER_AUTHOR = "author"
OWNER_WORLD = "world"
"""The two owners that are not a character (FR-CLOCK-19)."""

scheduled_events = Table(
    "scheduled_events",
    metadata,
    Column("id", ulid_type(), primary_key=True),
    Column("story_id", ulid_type(), nullable=False),
    Column("proposition_id", ulid_type(), nullable=False),
    Column("planned_offset_minutes", BigInteger, nullable=False),
    Column("planned_anchor", JSONB(none_as_null=True), nullable=True),
    Column("status", pg_enum(ScheduledStatus, "scheduled_status"), nullable=False),
    Column("owner", Text, nullable=False),
    Column("participants", ARRAY(ulid_type()), nullable=False, server_default=text("'{}'")),
    Column("shared_owners", ARRAY(ulid_type()), nullable=False, server_default=text("'{}'")),
    Column("place_id", ulid_type(), nullable=True),
    Column("grace_minutes", Integer, nullable=False),
    Column("refinement_pause_anchor_rev", Integer, nullable=True),
    Column("stopped_clock_at", BigInteger, nullable=True),
    *derived_columns(),
    Column("slot_k", SmallInteger, nullable=False, server_default=text("0")),
    Column("beat", SmallInteger, nullable=False, server_default=text("0")),
    Column("step", SmallInteger, nullable=False, server_default=text("0")),
    Column(
        "amendment_part_id",
        ulid_type(),
        ForeignKey(
            "amendment_parts.id",
            name="fk_scheduled_events_amendment_part",
            deferrable=True,
            initially="DEFERRED",
        ),
        nullable=True,
    ),
    CheckConstraint(
        "(narrative_turn IS NOT NULL AND turn_variant IS NOT NULL) "
        "OR amendment_part_id IS NOT NULL",
        name="positioned",
    ),
    CheckConstraint("grace_minutes >= 0", name="grace"),
    CheckConstraint("planned_offset_minutes >= 0", name="planned"),
    CheckConstraint("char_length(owner) BETWEEN 1 AND 26", name="owner"),
    *derived_indexes("scheduled_events"),
)
Index(
    "ix_scheduled_events_story_proposition",
    scheduled_events.c.story_id,
    scheduled_events.c.proposition_id,
)

SCHEDULED_EVENTS = register_derived(DerivedTable(scheduled_events))
"""``scheduled_events`` in ``core.membership.DERIVED_TABLES``: truncation, untruncation, swap,
regenerate and fork move each version with the operation that wrote it."""

__all__ = ["OWNER_AUTHOR", "OWNER_WORLD", "SCHEDULED_EVENTS", "scheduled_events"]
