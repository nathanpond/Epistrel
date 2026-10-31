"""The prompt-only world clock and States of a Lite turn (FR-LITE-5, FR-CLOCK-5,6; #203).

Under ``pipeline: lite`` the clock and States run only in their prompt-only forms
(``clock.level`` / ``states.level`` = ``prompt_only``: ``on`` needs the structured pipeline,
FR-PROF-4):

- ``read_lite_context`` reads, in the turn's read snapshot, what the single Lite prompt shows:
  ``## Time`` (``time_lines``: the clock as the turn starts, as the anchor shows it or as an
  offset from the story's start, then each open canon scheduled event with its planned time)
  with the clock on, and ``## Tracked values`` (``tracked_values``: every player-visible State
  the story owns, with its value; a hidden one, an externally owned one and the built-in
  ``world_clock`` are left out) with States on. Off, a section is empty and the prompt leaves
  it out. A regenerate reads the clock as its turn started.
- ``pace`` is the turn's one pacing advance (FR-CLOCK-6; #194's ``pacing_increment``): the
  words of the player's input and of the reply, counted as one combined render through the
  story's pacing table, written in the turn's commit as one ``pacing`` advance at the reply's
  beat (``manual`` mode and an ``external`` clock write none). It is #194's deterministic rule:
  no model call.

Nothing else of the clock or States runs under Lite: no claim about time is verified, no
scheduled event stops the clock, no skip is estimated or bounded, and a reply declares no
State effect (Lite has no ``effects`` channel); States change only through the State API
(FR-STATE-9). Log lines carry counts only (invariant 3); this module logs nothing.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from epistrel.core import states, world_clock
from epistrel.core.enums import StateChangeSource, StateOwner, StateVisibility
from epistrel.core.events import Operation
from epistrel.core.membership import POSITION_MAX, Position
from epistrel.core.states.model import (
    WORLD_CLOCK,
    StateDefinition,
    UndefinedType,
    Value,
    to_json,
)
from epistrel.core.tables import characters
from epistrel.core.turns import REPLY_BEAT
from epistrel.core.world_clock import external_mode, stops
from epistrel.core.world_clock.anchor import Anchor
from epistrel.core.world_clock.pacing import pacing_increment
from epistrel.core.world_clock.scheduled import event_names

PACING_REASON = "pacing"
"""The reason a Lite turn's pacing advance records (#194's structured advances' own)."""


@dataclass(frozen=True, slots=True)
class LiteContext:
    """The prompt-only sections of a Lite turn (see the module docstring)."""

    time_lines: tuple[str, ...] = ()
    tracked_values: tuple[str, ...] = ()


def _when(anchor: Anchor, offset: int) -> str:
    return anchor.display(offset) or world_clock.display(offset)


async def _time_lines(conn: AsyncConnection, story_id: str, through: Position) -> tuple[str, ...]:
    now = (await world_clock.clock_at(conn, story_id, through)).offset_minutes
    anchor = await world_clock.anchor_at(conn, story_id, through)
    lines = [f"It is now {_when(anchor, now)}."]
    found = [e for e in await stops.events_at(conn, story_id, through) if e.status in stops.OPEN]
    names = await event_names(conn, [e.event_id for e in found])
    for event in found:
        name = names.get(event.event_id, "A scheduled event")
        lines.append(f"Scheduled: {name}, at {_when(anchor, event.planned)}.")
    return tuple(lines)


def _shown(value: Value | UndefinedType | None, definition: StateDefinition) -> str:
    if value is None or isinstance(value, UndefinedType):
        return "unset"
    shown = str(to_json(value))
    return f"{shown} {definition.unit}" if definition.unit else shown


def _listed(definition: StateDefinition) -> bool:
    return (
        definition.name != WORLD_CLOCK
        and definition.visibility is StateVisibility.PLAYER_VISIBLE
        and definition.owner is StateOwner.STORY
    )


async def _tracked_values(
    conn: AsyncConnection, story_id: str, through: Position | None
) -> tuple[str, ...]:
    found = [d for d in await states.definitions(conn, story_id) if _listed(d)]
    if not found:
        return ()
    folded = await states.folded_at(conn, story_id, found, through=through)
    rows = await conn.execute(
        select(characters.c.id, characters.c.name)
        .where(characters.c.story_id == story_id)
        .order_by(characters.c.ordinal)
    )
    names: Mapping[str, str] = {str(r.id): str(r.name) for r in rows}
    lines = []
    for definition in found:
        values = {
            scope: f.value
            for (state_id, scope), f in folded.items()
            if state_id == definition.id and f.count
        }
        initial = definition.initial_value
        if definition.scope.value == "character":
            held = [
                f"{name} {_shown(values.get(cid, initial), definition)}"
                for cid, name in names.items()
                if cid in values or initial is not None
            ]
            now = "; ".join(held) if held else _shown(initial, definition)
        else:
            now = _shown(values.get(None, initial), definition)
        lines.append(f"{definition.name}: {now}")
    return tuple(lines)


async def read_lite_context(
    conn: AsyncConnection, story_id: str, *, before_turn: int | None = None
) -> LiteContext:
    """The turn's prompt-only sections (see the module docstring), read in ``conn``'s snapshot;
    ``before_turn``: a regenerate's turn, read as it started."""
    through = world_clock.HEAD if before_turn is None else Position(before_turn)
    time_lines: tuple[str, ...] = ()
    if await world_clock.clock_level(conn, story_id) is not None:
        time_lines = await _time_lines(conn, story_id, through)
    tracked: tuple[str, ...] = ()
    if await states.state_level(conn, story_id) is not None:
        tracked = await _tracked_values(
            conn, story_id, None if before_turn is None else Position(before_turn)
        )
    return LiteContext(time_lines, tracked)


@dataclass(frozen=True, slots=True)
class _Line:
    kind: str
    text: str


async def pace(conn: AsyncConnection, op: Operation, story_id: str, *, say: str, reply: str) -> int:
    """The turn's one pacing advance, in ``op``'s commit (see the module docstring); the
    minutes it advanced the clock (0: none written)."""
    if await world_clock.clock_level(conn, story_id) is None or op.narrative_turn is None:
        return 0
    definition = await world_clock.clock_definition(conn, story_id)
    if external_mode.is_external(definition):
        return 0  # #209: the provider owns the clock
    found = definition.clock
    assert found is not None  # noqa: S101 - the clock's definition carries its settings
    minutes = pacing_increment(
        [_Line("say", say), _Line("narration", reply)],
        tuple(found.pacing_table),
        mode=found.mode,
    )
    if minutes <= 0:
        return 0
    turn, variant = int(op.narrative_turn), int(op.turn_variant or 0)
    await world_clock.advance(
        conn,
        op,
        story_id,
        minutes=minutes,
        source=StateChangeSource.PACING,
        position=Position(turn, variant, 0, REPLY_BEAT, POSITION_MAX),
        reason=PACING_REASON,
    )
    return minutes


__all__ = ["PACING_REASON", "LiteContext", "pace", "read_lite_context"]
