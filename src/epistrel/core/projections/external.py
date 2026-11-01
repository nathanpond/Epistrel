"""The ``external_values`` projection (FR-EXT-2,20, FR-STORE-4,17; #205, #215): a fold of the
log's ``external_read`` and ``external_outcome`` events.

Each ``external_read`` event (the Orchestrator's ``PinnedReads`` stages one per value it pins in
a turn) becomes one row, keyed by the event: its State and scope key, its position (the event's
turn, variant, slot, beat and step), its version (1 for a turn's read; a ``post_effect_read``
names its own, #215), the version-1 read it extends (``base_event_id``: the event itself for a
read), the value and its source (``live`` → ``read``). An ``external_outcome`` event (#215,
FR-EXT-20) that records a post-effect ``version`` becomes that version's row, at the outcome's
position, with source ``confirmation`` or ``unknown``.

**Never a delta.** Every row's value is the value its event recorded, copied as it is: nothing
here (or anywhere else: ``tests/scenario/test_s_effect_crash.py`` checks it) computes a value
from an earlier version and a change (FR-EXT-20).

Nothing else changes a row: a row follows its turn's membership through its operation
(``operation_id``), which every read joins (``core.states.external``), so a truncate, a swap or
a regenerate needs no write here. ``rebuild`` refolds it from the log, a fork from the copied
events, and neither calls a provider (FR-STORE-17).
"""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy.ext.asyncio import AsyncConnection

from epistrel.core.enums import ExternalValueSource
from epistrel.core.events.types import ExternalOutcome, ExternalRead, OutcomeVersion
from epistrel.core.membership import Position
from epistrel.core.projections.base import LogEvent, Projection, Row, Rows
from epistrel.core.tables.states import STORY_SCOPE_KEY, external_values

SOURCES: dict[str, ExternalValueSource] = {
    "live": ExternalValueSource.READ,
    "last_known": ExternalValueSource.LAST_KNOWN,
    "unknown": ExternalValueSource.UNKNOWN,
    "post_effect_read": ExternalValueSource.POST_EFFECT_READ,
}
"""An ``external_read`` event's ``source`` as the row's."""

OUTCOME_SOURCES: dict[str, ExternalValueSource] = {
    "confirmation": ExternalValueSource.CONFIRMATION,
    "unknown": ExternalValueSource.UNKNOWN,
}
"""An ``external_outcome`` version's ``source`` as the row's."""

READ_VERSION = 1
"""A read is a value's first version in its turn (FR-EXT-20's post-effect versions follow)."""


def _position(event: LogEvent) -> Position:
    return Position(
        int(event.narrative_turn or 0),
        int(event.turn_variant or 0),
        int(event.slot_k or 0),
        int(event.beat or 0),
        int(event.step or 0),
    )


def _scope(scope_key: str | None) -> str:
    return STORY_SCOPE_KEY if scope_key is None else scope_key


def value_row(event: LogEvent, read: ExternalRead) -> Row:
    """The ``external_values`` row of one ``external_read`` event."""
    return {
        "event_id": event.id,
        "story_id": event.story_id,
        "operation_id": event.operation_id,
        "state_id": read.state_id,
        "scope_key": _scope(read.scope_key),
        **_position(event).columns("position"),
        "version": read.version,
        "value": read.value,
        "source": SOURCES[read.source].value,
        "base_event_id": read.base_event_id or event.id,
    }


def outcome_row(event: LogEvent, version: OutcomeVersion) -> Row:
    """The ``external_values`` row of one ``external_outcome`` event's post-effect version."""
    return {
        "event_id": event.id,
        "story_id": event.story_id,
        "operation_id": event.operation_id,
        "state_id": version.state_id,
        "scope_key": _scope(version.scope_key),
        **_position(event).columns("position"),
        "version": version.version,
        "value": version.value,
        "source": OUTCOME_SOURCES[version.source].value,
        "base_event_id": version.base_event_id,
    }


class ExternalValuesProjection(Projection):
    """``external_values``: one row per ``external_read`` event and per ``external_outcome``
    event recording a version."""

    table = external_values

    def apply(self, rows: Rows, event: LogEvent) -> None:
        if isinstance(event.payload, ExternalRead):
            rows.put(value_row(event, event.payload))
        elif isinstance(event.payload, ExternalOutcome) and event.payload.version is not None:
            rows.put(outcome_row(event, event.payload.version))

    async def load(self, conn: AsyncConnection, story_id: str, events: Sequence[LogEvent]) -> Rows:
        del conn, story_id, events  # a new event only adds its own row
        return Rows(self.table)


__all__ = [
    "OUTCOME_SOURCES",
    "READ_VERSION",
    "SOURCES",
    "ExternalValuesProjection",
    "outcome_row",
    "value_row",
]
