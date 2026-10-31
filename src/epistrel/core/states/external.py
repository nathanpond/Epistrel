"""Externally owned State values as Core reads them (FR-EXT-2, FR-VERIFY-7, FR-STORE-17; #205).

An externally owned State's value is never folded from ``state_changes``: it is what the
provider answered, pinned once per turn and recorded as an ``external_read`` event (the
Orchestrator's ``orchestrator.providers.pinned.PinnedReads``), folded into the
``external_values`` projection (``core.projections.external``). Core never calls a provider and
never imports ``orchestrator.providers``: it reads what was pinned.

**Two sources, one answer** (``pinned_values``):

- **Inside a turn** the Orchestrator installs its pins as the current ``PinSource`` (``pinning``)
  for the whole turn, its commit included. Its reads are staged, not yet committed, so Core asks
  the source: the version pinned at or before the position read (``PinSource.pinned``). A State
  the turn has not pinned has no value (FR-EXT-2: no earlier turn's read stands in for this
  turn's, and nothing reads speculatively).
- **Outside a turn** (a later verification of a recorded position, an inspection) Core reads the
  committed ``external_values``: the latest version at or before the position, of an operation
  on the active timeline (``core.membership.active_operation``, so a truncated or replaced
  turn's read no longer answers, and a regenerate's own replaced variant is left out).

Pure but for ``pinned_values``'s read; logs nothing (invariant 3).
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from epistrel.core.enums import ExternalValueSource, StateOwner
from epistrel.core.membership import (
    Position,
    active_operation,
    position_key,
    position_literal,
)
from epistrel.core.states.model import StateDefinition, Value, from_json
from epistrel.core.tables import operations
from epistrel.core.tables.states import STORY_SCOPE_KEY, external_values

Key = tuple[str, str | None]
"""An external value within a story: ``(state id, scope key)`` (``None``: story-scoped)."""


@dataclass(frozen=True, slots=True)
class ExternalPin:
    """One version of an externally owned State's value: the State, the scope key, the typed
    value (``None``: unknown), where it was read, its version and source, and its
    ``external_read`` event (``None`` while the turn that read it has not committed)."""

    state_id: str
    scope_key: str | None
    value: Value | None
    position: Position
    version: int = 1
    source: ExternalValueSource = ExternalValueSource.READ
    event_id: str | None = None


class PinSource(Protocol):
    """A turn's pinned reads as Core reads them (``orchestrator.providers.pinned.PinnedReads``)."""

    def pinned(self, state_id: str, scope_key: str | None, through: Position | None) -> Any:
        """The ``ExternalPin`` of ``(state_id, scope_key)`` pinned at or before ``through``
        (``None``: the latest), or ``None`` when the turn has pinned none."""
        ...


_PINS: ContextVar[PinSource | None] = ContextVar("epistrel_external_pins", default=None)


@contextmanager
def pinning(source: PinSource) -> Iterator[PinSource]:
    """Make ``source`` the current turn's pins for the block (see the module docstring)."""
    token = _PINS.set(source)
    try:
        yield source
    finally:
        _PINS.reset(token)


def current() -> PinSource | None:
    """The current turn's pins, or ``None`` outside a turn."""
    return _PINS.get()


def external(states: Sequence[StateDefinition]) -> list[StateDefinition]:
    """The externally owned ones among ``states``."""
    return [s for s in states if s.owner is StateOwner.EXTERNAL and s.id is not None]


def _at_or_before(position: Position, through: Position | None) -> bool:
    return through is None or position.at_or_before(through)


async def pinned_values(
    conn: AsyncConnection,
    story_id: str,
    states: Sequence[StateDefinition],
    keys: Sequence[Key],
    *,
    through: Position | None = None,
) -> dict[Key, ExternalPin]:
    """The value of each of ``keys`` (externally owned ``states``) at or before ``through``
    (``None``: the latest): the current turn's pin, or outside a turn the latest committed
    version on the active timeline (see the module docstring). A key with none is absent."""
    by_id = {s.id: s for s in external(states)}
    wanted = [k for k in keys if k[0] in by_id]
    if not wanted:
        return {}
    source = current()
    if source is not None:
        found: dict[Key, ExternalPin] = {}
        for key in wanted:
            pin = source.pinned(key[0], key[1], through)
            if isinstance(pin, ExternalPin):
                found[key] = pin
        return found
    v = external_values.c
    query = (
        select(external_values)
        .join(operations, operations.c.operation_id == v.operation_id)
        .where(
            v.story_id == story_id,
            v.state_id.in_(sorted({k[0] for k in wanted})),
            active_operation(operations),
        )
    )
    if through is not None:
        query = query.where(position_key(external_values, "position") <= position_literal(through))
    rows = await conn.execute(query)
    latest: dict[Key, tuple[tuple[int, ...], ExternalPin]] = {}
    want = set(wanted)
    for row in rows:
        scope_key = None if row.scope_key == STORY_SCOPE_KEY else str(row.scope_key)
        key = (str(row.state_id), scope_key)
        if key not in want:
            continue
        position = Position.of_columns(dict(row._mapping), "position")
        assert position is not None  # noqa: S101 - the column group is NOT NULL
        order = (*position.key(), int(row.version))
        if key in latest and latest[key][0] >= order:
            continue
        latest[key] = (
            order,
            ExternalPin(
                state_id=key[0],
                scope_key=scope_key,
                value=from_json(by_id[key[0]].type, row.value),
                position=position,
                version=int(row.version),
                source=ExternalValueSource(row.source),
                event_id=str(row.event_id),
            ),
        )
    return {key: pin for key, (_, pin) in latest.items() if _at_or_before(pin.position, through)}


__all__ = [
    "ExternalPin",
    "Key",
    "PinSource",
    "current",
    "external",
    "pinned_values",
    "pinning",
]
