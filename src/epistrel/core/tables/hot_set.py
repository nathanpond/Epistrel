"""``overflow_episodes``: the hot set's overflow episodes (FR-MEM-8, NFR-SCALE-2; docs/02 §4.2;
#147, migration 0061).

One row is one episode of one hot set: a character's unpinned hot memories (``scope =
character``, ``scope_id`` the character's id) or the story's unpinned hot vectors (``scope =
story``, ``scope_id`` the story's id). It opens at the commit that takes the set from within its
budget to over it: ``opened_turn`` is the story's head turn then (counted in committed turns of
the active chain) and ``deadline_turn`` is ``opened_turn + memory.hot_set.retirement_max_lag``,
fixed for the episode's life; ``excess_at_open`` is how far over the budget that commit took it
and ``excess`` how far over it is now (``0`` once closed; ``epistrel_hot_overflow{scope}`` sums
the open ones). It closes (``closed_turn``, ``closed_reason``: ``within_budget`` when retirement,
a truncation or an unpin brought the set back, ``forced`` when the deadline's forced retirement
did) when the set is back within budget. At most one episode per set is open
(``ux_overflow_episodes_open``); a later overflow opens a new one.

Maintenance state, never events: a story deletion drops the rows, a fork copies none (its
counters are recomputed and its next commit opens what it needs), and identifiers, scope kinds
and turn numbers are all a row holds. ``core.memory.retirement`` is the only writer.
"""

from __future__ import annotations

from sqlalchemy import TIMESTAMP, CheckConstraint, Column, Index, Integer, Table, Text, text

from epistrel.core.tables.base import metadata, ulid_type

EPISODE_SCOPES: tuple[str, ...] = ("character", "story")
CLOSED_REASONS: tuple[str, ...] = ("within_budget", "forced")


def _in(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{v}'" for v in values)


overflow_episodes = Table(
    "overflow_episodes",
    metadata,
    Column("id", ulid_type(), primary_key=True),
    Column("story_id", ulid_type(), nullable=False),
    Column("scope", Text, nullable=False),
    Column("scope_id", ulid_type(), nullable=False),
    Column("opened_turn", Integer, nullable=False),
    Column("deadline_turn", Integer, nullable=False),
    Column("closed_turn", Integer, nullable=True),
    Column("closed_reason", Text, nullable=True),
    Column("excess_at_open", Integer, nullable=False),
    Column("excess", Integer, nullable=False),
    Column(
        "opened_at",
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=text("transaction_timestamp()"),
    ),
    Column("closed_at", TIMESTAMP(timezone=True), nullable=True),
    CheckConstraint(f"scope IN ({_in(EPISODE_SCOPES)})", name="scope"),
    CheckConstraint(
        f"closed_reason IS NULL OR closed_reason IN ({_in(CLOSED_REASONS)})", name="closed_reason"
    ),
    CheckConstraint(
        "(closed_turn IS NULL) = (closed_reason IS NULL) "
        "AND (closed_turn IS NULL) = (closed_at IS NULL)",
        name="closed",
    ),
    CheckConstraint("deadline_turn >= opened_turn", name="deadline"),
    CheckConstraint("excess_at_open > 0 AND excess >= 0", name="excess"),
)
Index(
    "ux_overflow_episodes_open",
    overflow_episodes.c.story_id,
    overflow_episodes.c.scope,
    overflow_episodes.c.scope_id,
    unique=True,
    postgresql_where=text("closed_turn IS NULL"),
)
Index("ix_overflow_episodes_story", overflow_episodes.c.story_id, overflow_episodes.c.opened_turn)

__all__ = ["CLOSED_REASONS", "EPISODE_SCOPES", "overflow_episodes"]
