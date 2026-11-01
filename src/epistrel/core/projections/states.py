"""The ``state_values`` projection (FR-STATE-4, FR-STORE-4; #185): a cache over ``state_changes``,
not over the event log.

Like ``belief_state`` it answers its rows from ``fold_state``, and ``apply`` is a no-op for every
event. Its incremental path is ``core.states``' own: ``apply_change`` refreshes the value it
changed in its transaction, and the membership follower registered here re-derives the values a
truncate, an untruncate, a swap, a regenerate or a turn over a rewound tail touched, in the
flip's transaction. ``core.states.derive`` is the one function both paths and the fold use, so a
rebuilt ``state_values`` equals the incrementally kept one row for row. A fork and erasure's
``rebuilt`` stage refold it.
"""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy.ext.asyncio import AsyncConnection

from epistrel.core.enums import Membership
from epistrel.core.membership import register_follower
from epistrel.core.projections.base import LogEvent, Projection, Rows
from epistrel.core.tables.states import state_values


class StateValuesProjection(Projection):
    """``state_values``: folded from the story's active State changes."""

    table = state_values

    def apply(self, rows: Rows, event: LogEvent) -> None:
        del rows, event

    async def load(self, conn: AsyncConnection, story_id: str, events: Sequence[LogEvent]) -> Rows:
        del conn, story_id, events
        return Rows(self.table)

    async def fold_state(self, conn: AsyncConnection, story_id: str) -> Rows:
        from epistrel.core import states  # here: core.states writes through Operation

        return Rows(self.table, await states.fold_state(conn, story_id))


async def follow_operations(
    conn: AsyncConnection, story_id: str | None, operation_ids: Sequence[str], to: Membership
) -> None:
    """``core.membership``'s follower: the State values the flipped operations touched are
    re-derived in the flip's transaction (``core.states.follow_operations``)."""
    from epistrel.core import states  # here: core.states writes through Operation

    if Membership(to) is Membership.ACTIVE:
        await states.check_active_names(conn, story_id, operation_ids)
    await states.follow_operations(conn, story_id, operation_ids)


register_follower(follow_operations)

__all__ = ["StateValuesProjection", "follow_operations"]
