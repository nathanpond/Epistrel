"""The world clock in ``external`` mode (FR-CLOCK-5, FR-EXT-2; #209; owner decision 2026-10-30).

``PATCH /stories/{id}/clock`` with ``mode = external`` makes the ``world_clock`` State
``owner = external`` with a ``provider_id`` and an ``external_value_name`` (#204's path; the
``provider_value = "<provider>.<value>"`` shorthand sets both), which the provider's manifest
declares as a ``number``: a count of story-minutes on the clock fold's offset scale (the same
scale ``core.world_clock.advance`` writes). The story must be bound to the provider (422
``provider_unbound``). Anchors (#195) are unchanged and read the offset as before.

**Pinned, never moved.** The clock is then read and pinned once per turn like any external value
(#205's ``PinnedReads``: it is a player-visible, story-scoped external State), recorded as an
``external_read`` event and folded into ``external_values``. Its value at a position is the
latest pinned offset there (``pins``): the pinned versions on the active timeline (``as_recorded``:
every one) and, inside a turn, the turn's own pin (``core.states.external.current``). A pin holds
from the start of its turn (``PIN_SLOT``), so the turn's start reads it. ``merged`` folds the pins
into the clock's advance list, so ``clock_at``, ``time_basis`` (every story-time) and
``elapsed_since`` read one scale: each pin is an advance of the difference to the offset before
it. Advances written before the switch stay, none is written while it lasts, and a clock that
leaves ``external`` mode keeps its pins (the clock never moves backward: its next advance adds
to the last pinned offset).

Epistrel never writes an advance for an external clock: #194's pacing increments and declared
skips (as #196's stops cut them) become ``requested_effect`` events of kind ``time_advance``
(``orchestrator.structured.effects.time_requests``), ``advance`` refuses with 422
``clock_external``, and ``POST /clock/advance`` answers the same.

Reads only; logs nothing (invariant 3). No Orchestrator import: Core reads what was pinned.
"""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from epistrel.core.enums import StateOwner
from epistrel.core.errors import ClockExternal
from epistrel.core.membership import POSITION_MAX, Position, active_operation
from epistrel.core.resolution.time import Advance as BasisAdvance
from epistrel.core.states import View
from epistrel.core.states.external import ExternalPin, current
from epistrel.core.states.model import StateDefinition, number
from epistrel.core.tables import operations
from epistrel.core.tables.states import STORY_SCOPE_KEY, external_values

MANIFEST_TYPE = "number"
"""The type a provider's manifest declares the clock's value as (owner decision 2026-10-30)."""
PIN_SLOT = POSITION_MAX + 1
"""The slot key a pin's advance takes in the fold, in the turn before the pin's: after every slot
of that turn and before the pin's turn starts, so the turn's start (whichever variant) reads the
pinned offset."""


def is_external(definition: StateDefinition) -> bool:
    """Whether the clock (``definition``) runs in ``external`` mode: a provider's value."""
    return definition.owner is StateOwner.EXTERNAL


def refuse_advance(definition: StateDefinition, story_id: str) -> None:
    """422 ``clock_external`` for an advance of an external clock (see the module docstring)."""
    if is_external(definition):
        raise ClockExternal(
            "the world clock is a provider's value in external mode: the consumer advances it",
            {"story_id": story_id, "provider_id": definition.provider_id},
        )


def _offset(raw: object) -> int | None:
    found = number(raw) if not isinstance(raw, str) else None
    if found is None:
        return None
    return int(found.to_integral_value())


async def pins(
    conn: AsyncConnection,
    story_id: str,
    definition: StateDefinition,
    *,
    view: View = View.ACTIVE,
) -> list[tuple[Position, int]]:
    """Every pinned offset of the clock while it was external, by position (see the module
    docstring): the recorded versions (on the active timeline, or all of them ``as_recorded``)
    and the current turn's pin, the latest version per position. An ``Unknown`` pin is no value
    and is left out."""
    if definition.id is None:
        return []
    v = external_values.c
    query = (
        select(external_values)
        .join(operations, operations.c.operation_id == v.operation_id)
        .where(
            v.story_id == story_id,
            v.state_id == definition.id,
            v.scope_key == STORY_SCOPE_KEY,
        )
    )
    if View(view) is View.ACTIVE:
        query = query.where(active_operation(operations))
    latest: dict[tuple[int, ...], tuple[int, Position, int]] = {}
    for row in await conn.execute(query):
        position = Position.of_columns(dict(row._mapping), "position")
        offset = _offset(row.value)
        if position is None or offset is None:
            continue
        key = (*position.key(), int(position.variant))
        if key not in latest or latest[key][0] < int(row.version):
            latest[key] = (int(row.version), position, offset)
    found = [(position, offset) for _, position, offset in latest.values()]
    source = current()
    if source is not None:
        # The turn's own pin answers for its turn (a regenerate's replaced variant's does not).
        pin = source.pinned(definition.id, None, None)
        if isinstance(pin, ExternalPin) and pin.value is not None:
            offset = _offset(pin.value)
            if offset is not None:
                found = [f for f in found if f[0].turn != pin.position.turn]
                found.append((pin.position, offset))
    return sorted(found, key=lambda f: f[0].key())


def merged(
    advances: Sequence[BasisAdvance], pinned: Sequence[tuple[Position, int]]
) -> list[BasisAdvance]:
    """The clock's advance list with the pins folded in (see the module docstring): in
    position order, each written advance as it is, and each pin, at the start of its turn
    (``PIN_SLOT``), an advance of the difference to the offset before it. Pure."""
    if not pinned:
        return list(advances)
    written = [(key, False, variant, minutes) for key, variant, minutes in advances]
    # Keyed in the turn before (one pin answers per turn, ``pins``), so no variant of the
    # pin's own turn leaves it out.
    pinning = [((p.turn - 1, PIN_SLOT, 0, 0), True, 0, offset) for p, offset in pinned]
    out: list[BasisAdvance] = []
    running = 0
    for key, is_pin, variant, amount in sorted([*written, *pinning], key=lambda e: (e[0], e[1])):
        if is_pin:
            out.append((key, variant, amount - running))
            running = amount
        else:
            out.append((key, variant, amount))
            running += amount
    return out


__all__ = [
    "MANIFEST_TYPE",
    "PIN_SLOT",
    "is_external",
    "merged",
    "pins",
    "refuse_advance",
]
