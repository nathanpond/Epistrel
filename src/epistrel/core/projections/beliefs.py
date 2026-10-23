"""The ``belief_state`` projection (FR-EPI-6, FR-STORE-3; #159) and ``nested_belief_state``
(#167): caches over ``beliefs``, not over the event log.

Like #137's structured projections it answers its rows from ``fold_state``, ``apply`` is a no-op
for every event, and its incremental path is the belief write's own hook and the membership
follower (``core.epistemic.partitions``): ``derive`` is the one function both use, so a rebuilt
``belief_state`` equals the incrementally kept one row for row (``derive_nested`` likewise for
the nested one). A fork and erasure's ``rebuilt`` stage refold them.
"""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy.ext.asyncio import AsyncConnection

from epistrel.core.projections.base import LogEvent, Projection, Rows
from epistrel.core.tables.beliefs import belief_state, nested_belief_state


class BeliefStateProjection(Projection):
    """``belief_state``: folded from the story's beliefs."""

    table = belief_state

    def apply(self, rows: Rows, event: LogEvent) -> None:
        del rows, event

    async def load(self, conn: AsyncConnection, story_id: str, events: Sequence[LogEvent]) -> Rows:
        del conn, story_id, events
        return Rows(self.table)

    async def fold_state(self, conn: AsyncConnection, story_id: str) -> Rows:
        from epistrel.core.epistemic import partitions

        return Rows(self.table, await partitions.fold_state(conn, story_id))


class NestedBeliefStateProjection(Projection):
    """``nested_belief_state`` (#167): folded from the story's nested beliefs."""

    table = nested_belief_state

    def apply(self, rows: Rows, event: LogEvent) -> None:
        del rows, event

    async def load(self, conn: AsyncConnection, story_id: str, events: Sequence[LogEvent]) -> Rows:
        del conn, story_id, events
        return Rows(self.table)

    async def fold_state(self, conn: AsyncConnection, story_id: str) -> Rows:
        from epistrel.core.epistemic import partitions

        return Rows(self.table, await partitions.fold_nested_state(conn, story_id))


__all__ = ["BeliefStateProjection", "NestedBeliefStateProjection"]
