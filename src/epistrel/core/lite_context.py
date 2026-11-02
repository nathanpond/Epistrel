"""What a Lite turn's prompt-only clock and States show (FR-LITE-5, FR-CLOCK-5,6, FR-CONC-4;
#203, #662).

Under ``pipeline: lite`` the clock and States run only in their prompt-only forms
(``clock.level`` / ``states.level`` = ``prompt_only``: ``on`` needs the structured pipeline,
FR-PROF-4). ``read_lite_context`` reads, in the turn's read snapshot, what the single Lite
prompt shows: ``## Time`` (``time_lines``: the clock as the turn starts, as the anchor shows it
or as an offset from the story's start, then each open canon scheduled event with its planned
time) with the clock on, and ``## Tracked values`` (``tracked_values``: every player-visible State
the story owns, with its value; a hidden one, an externally owned one and the built-in
``world_clock`` are left out) with States on. Off, a section is empty and the prompt leaves it
out. A regenerate reads the clock as its turn started.

The same reader is the Lite read set's ``time`` and ``tracked_values`` members
(``core.concurrency``, #662), so the prompt and its revalidation cannot drift: a change to what
a section shows, committed while a Lite turn generates, makes the turn regenerate. In Core
because ``core.concurrency`` may not import the Orchestrator; ``orchestrator.lite.context``
re-exports it. Nothing here logs.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from epistrel.core import states, world_clock
from epistrel.core.enums import StateOwner, StateVisibility
from epistrel.core.membership import Position
from epistrel.core.states.model import (
    WORLD_CLOCK,
    StateDefinition,
    UndefinedType,
    Value,
    to_json,
)
from epistrel.core.tables import characters
from epistrel.core.world_clock import stops
from epistrel.core.world_clock.anchor import Anchor
from epistrel.core.world_clock.scheduled import event_names


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


async def lite_pipeline(conn: AsyncConnection, story_id: str) -> bool:
    """Whether the story runs the Lite pipeline (the only one with a prompt-only section)."""
    from epistrel.core.profiles.effective import effective_configuration  # profiles import us

    settings = (await effective_configuration(conn, story_id, validate=False)).settings
    return settings.pipeline == "lite"


__all__ = ["LiteContext", "lite_pipeline", "read_lite_context"]
