"""The ``external_values`` projection (FR-EXT-2, FR-STORE-4,17; #205): a fold of the log's
``external_read`` events.

Each ``external_read`` event (the Orchestrator's ``PinnedReads`` stages one per value it pins in
a turn) becomes one row, keyed by the event: its State and scope key, its position (the event's
turn, variant, slot, beat and step), version 1 (a read; #215's confirmations add later versions
of a value at their own positions), the value and its source (``live`` → ``read``). Nothing else
changes a row: a row follows its turn's membership through its operation (``operation_id``),
which every read joins (``core.states.external``), so a truncate, a swap or a regenerate needs
no write here. ``rebuild`` refolds it from the log, a fork from the copied events, and neither
calls a provider (FR-STORE-17).
"""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy.ext.asyncio import AsyncConnection

from epistrel.core.enums import ExternalValueSource
from epistrel.core.events.types import ExternalRead
from epistrel.core.membership import Position
from epistrel.core.projections.base import LogEvent, Projection, Row, Rows
from epistrel.core.tables.states import STORY_SCOPE_KEY, external_values

SOURCES: dict[str, ExternalValueSource] = {
    "live": ExternalValueSource.READ,
    "last_known": ExternalValueSource.LAST_KNOWN,
    "unknown": ExternalValueSource.UNKNOWN,
}
"""An ``external_read`` event's ``source`` as the row's."""

READ_VERSION = 1
"""A read is a value's first version in its turn (FR-EXT-20's post-effect versions follow)."""


def value_row(event: LogEvent, read: ExternalRead) -> Row:
    """The ``external_values`` row of one ``external_read`` event."""
    position = Position(
        int(event.narrative_turn or 0),
        int(event.turn_variant or 0),
        int(event.slot_k or 0),
        int(event.beat or 0),
        int(event.step or 0),
    )
    return {
        "event_id": event.id,
        "story_id": event.story_id,
        "operation_id": event.operation_id,
        "state_id": read.state_id,
        "scope_key": STORY_SCOPE_KEY if read.scope_key is None else read.scope_key,
        **position.columns("position"),
        "version": READ_VERSION,
        "value": read.value,
        "source": SOURCES[read.source].value,
    }


class ExternalValuesProjection(Projection):
    """``external_values``: one row per ``external_read`` event."""

    table = external_values

    def apply(self, rows: Rows, event: LogEvent) -> None:
        if isinstance(event.payload, ExternalRead):
            rows.put(value_row(event, event.payload))

    async def load(self, conn: AsyncConnection, story_id: str, events: Sequence[LogEvent]) -> Rows:
        del conn, story_id, events  # a new event only adds its own row
        return Rows(self.table)


__all__ = ["READ_VERSION", "SOURCES", "ExternalValuesProjection", "value_row"]
