"""The clock's API calls (FR-CLOCK-5,13; #193): ``GET``, ``POST …/advance`` and ``PATCH
/stories/{id}/clock``, and the clock a turn reports.

- ``read_clock(db, story_id)`` is the clock as the story stands: its offset and display, the
  level the story runs it at (``off``: inert), its mode and its settings.
- ``advance_clock(db, story_id, request, *, principal)`` moves it forward between turns, as the
  consumer (FR-CLOCK-13): an inter-turn slot operation (``slots.open_inter_turn``) whose commit
  writes one advance at the slot (source ``consumer``) and records a ``clock_advanced`` event.
  ``minutes`` or ``to`` (an offset in minutes from the story's start, or ``"HH:MM"``, 422
  ``clock_unanchored`` while the anchor fixes no hour: always, until #195). A backward one is
  422 ``clock_backward`` and writes nothing; with the clock off, 409 ``module_off``.
- ``update_clock(db, story_id, patch, *, principal)`` changes its settings: a story
  configuration write through ``core.states.update_definition`` (interpretive members, and
  ``pacing_table`` prospective; one ``config_changes`` row each, setting
  ``state:world_clock.<member>``). ``mode = external`` waits for #209's providers (422).
- ``turn_clock(conn, story_id, turn, variant)`` is the clock before and after a committed turn
  (``respond``'s ``clock``; ``None`` with the clock off).
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, model_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from epistrel.core import states
from epistrel.core.enums import ClockMode, Provenance, StateChangeSource
from epistrel.core.errors import ClockBackward, ClockUnanchored, InvalidRequest
from epistrel.core.events.types import ClockAdvanced as ClockAdvancedEvent
from epistrel.core.membership import POSITION_MAX, Position
from epistrel.core.states.model import REASON_MAX, WORLD_CLOCK
from epistrel.core.tables import stories
from epistrel.core.world_clock import (
    HEAD,
    ClockValue,
    advance,
    clock_at,
    clock_level,
    require_clock,
    settings,
)
from epistrel.core.world_clock.slots import open_inter_turn, slot_position

TIME_OF_DAY = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")
PATCHABLE: tuple[str, ...] = (
    "mode",
    "pacing_table",
    "max_unprompted_skip_minutes",
    "stop_at_due_events",
    "time_awareness",
    "bands",
    "calendar",
)
"""The settings ``PATCH /clock`` takes."""


class ClockAdvanceRequest(BaseModel):
    """``POST /stories/{id}/clock/advance``: ``minutes`` forward, or ``to`` a target (an offset
    in minutes from the story's start, or a time of day ``"HH:MM"``), with a ``reason``."""

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={"examples": [{"minutes": 90, "reason": "they wait for the bus"}]},
    )

    minutes: StrictInt | None = None
    to: StrictInt | StrictStr | None = None
    reason: str = Field(min_length=1, max_length=REASON_MAX)

    @model_validator(mode="after")
    def _one_target(self) -> ClockAdvanceRequest:
        if (self.minutes is None) == (self.to is None):
            raise ValueError("give minutes or to, not both")
        if isinstance(self.to, str) and not TIME_OF_DAY.match(self.to):
            raise ValueError('to is an offset in minutes or a time of day "HH:MM"')
        return self


@dataclass(frozen=True, slots=True)
class ClockView:
    """The clock as the story stands (``GET /stories/{id}/clock``)."""

    offset: int
    display: str
    anchor: dict[str, Any] | None
    level: str
    mode: str
    settings: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ClockMoved:
    """A consumer's advance: its slot operation, the clock before and after, the change it
    wrote and where, and the story's revision after it."""

    operation_id: str
    change_id: str
    before: ClockValue
    after: ClockValue
    position: dict[str, int]
    timeline_revision: int
    replayed: bool = False
    """``True`` when an ``Idempotency-Key`` retry returned this stored result (#61)."""


@dataclass(frozen=True, slots=True)
class ClockSettingsWrite:
    """The clock's settings after a ``PATCH``, the story's configuration revision, and whether
    anything changed."""

    settings: dict[str, Any]
    config_revision: int
    timeline_revision: int
    changed: bool
    replayed: bool = False
    """``True`` when an ``Idempotency-Key`` retry returned this stored result (#61)."""


@dataclass(frozen=True, slots=True)
class TurnClock:
    """The clock before and after a turn (``respond``'s ``clock``, FR-CLOCK-13)."""

    before: ClockValue
    after: ClockValue


def _settings_document(found: states.ClockSettings) -> dict[str, Any]:
    document = found.document()
    document.pop("anchor")
    return document


async def read_clock(db: AsyncEngine, story_id: str) -> ClockView:
    """The clock as the story stands (see the module docstring)."""
    async with db.connect() as conn:
        found = await settings(conn, story_id)
        value = await clock_at(conn, story_id, HEAD)
        level = await clock_level(conn, story_id)
    return ClockView(
        offset=value.offset_minutes,
        display=value.display(),
        anchor=value.anchor,
        level=level or "off",
        mode=found.mode.value,
        settings=_settings_document(found),
    )


async def _precheck(conn: AsyncConnection, story_id: str, request: ClockAdvanceRequest) -> None:
    """The refusals a consumer's advance gets before any operation opens: the clock off, a
    time of day the anchor cannot place, and a backward target (the commit checks again)."""
    await require_clock(conn, story_id)
    if isinstance(request.to, str):
        raise ClockUnanchored(
            "the clock's anchor does not fix the hour, so a time of day cannot be placed",
            {"story_id": story_id, "to": request.to},
        )
    current = (await clock_at(conn, story_id, HEAD)).offset_minutes
    attempted = current + request.minutes if request.minutes is not None else request.to
    if attempted is not None and int(attempted) < current:
        raise ClockBackward(
            "the world clock never moves backward: truncate or rewrite to reach earlier story-time",
            {"story_id": story_id, "current": current, "attempted": attempted},
        )


async def advance_clock(
    db: AsyncEngine,
    story_id: str,
    request: ClockAdvanceRequest,
    *,
    principal: str,
    idempotency_key: str | None = None,
) -> ClockMoved:
    """The consumer's advance (see the module docstring)."""
    from epistrel.core.stories import require_writable  # here: stories declares the clock

    await require_writable(db, story_id)
    async with db.connect() as conn:
        await _precheck(conn, story_id, request)
    to_offset = request.to if isinstance(request.to, int) else None
    written: list[Any] = []

    async def step(conn: AsyncConnection) -> None:
        written.append(
            await advance(
                conn,
                op,
                story_id,
                minutes=request.minutes,
                to_offset=to_offset,
                source=StateChangeSource.CONSUMER,
                position=slot_position(op),
                reason=request.reason,
            )
        )

    op = open_inter_turn(
        db,
        story_id,
        source=Provenance.CONSUMER,
        principal=principal,
        idempotency_key=idempotency_key,
    )
    async with op:
        op.append(
            "clock_advanced",
            ClockAdvancedEvent(
                source=StateChangeSource.CONSUMER.value,
                minutes=request.minutes,
                to_offset=to_offset,
            ),
            provenance=Provenance.CONSUMER,
        )
        op.add_commit_step(step)
        await op.commit()
    moved = written[0]
    anchor = (await _anchor(db, story_id)) if moved is not None else None
    assert op.timeline_revision is not None  # noqa: S101 - set by a committed story operation
    return ClockMoved(
        operation_id=op.operation_id,
        change_id=moved.change_id,
        before=ClockValue(moved.before, anchor),
        after=ClockValue(moved.after, anchor),
        position=moved.position.document(),
        timeline_revision=op.timeline_revision,
    )


async def _anchor(db: AsyncEngine, story_id: str) -> dict[str, Any] | None:
    async with db.connect() as conn:
        return (await settings(conn, story_id)).anchor


async def update_clock(
    db: AsyncEngine, story_id: str, patch: Mapping[str, Any], *, principal: str
) -> ClockSettingsWrite:
    """Change the clock's settings (see the module docstring)."""
    unknown = sorted(set(patch) - set(PATCHABLE))
    if unknown:
        raise InvalidRequest(
            f"{unknown[0]!r} is not a clock setting PATCH /clock changes",
            {"field": unknown[0], "reason": "unknown"},
        )
    if patch.get("mode") == ClockMode.EXTERNAL.value:
        raise InvalidRequest(
            "an external clock needs a registered provider (#209)",
            {"field": "mode", "reason": "external_unavailable"},
        )
    async with db.begin() as conn:
        before = await states.definition(conn, story_id, WORLD_CLOCK)
        after = await states.update_definition(
            conn, story_id, WORLD_CLOCK, dict(patch), principal=principal
        )
        row = (
            await conn.execute(
                select(stories.c.config_revision, stories.c.timeline_revision).where(
                    stories.c.id == story_id
                )
            )
        ).one()
    assert after.clock is not None  # noqa: S101 - the clock's definition carries its settings
    return ClockSettingsWrite(
        settings=_settings_document(after.clock),
        config_revision=int(row.config_revision),
        timeline_revision=int(row.timeline_revision),
        changed=after.document() != before.document(),
    )


async def turn_clock(
    conn: AsyncConnection, story_id: str, turn: int, variant: int
) -> TurnClock | None:
    """The clock before and after turn ``turn`` (its ``variant``); ``None`` with the clock
    off."""
    if await clock_level(conn, story_id) is None:
        return None
    before = await clock_at(conn, story_id, Position(turn, variant))
    after = await clock_at(conn, story_id, Position(turn, variant, 0, POSITION_MAX, POSITION_MAX))
    return TurnClock(before, after)


async def turn_clock_of(
    db: AsyncConnection | AsyncEngine, story_id: str, turn: int, variant: int
) -> TurnClock | None:
    """``turn_clock`` on a connection, or on a new one of ``db``'s."""
    if isinstance(db, AsyncConnection):
        return await turn_clock(db, story_id, turn, variant)
    async with db.connect() as conn:
        return await turn_clock(conn, story_id, turn, variant)


__all__ = [
    "PATCHABLE",
    "ClockAdvanceRequest",
    "ClockMoved",
    "ClockSettingsWrite",
    "ClockView",
    "TurnClock",
    "advance_clock",
    "read_clock",
    "turn_clock",
    "turn_clock_of",
    "update_clock",
]
