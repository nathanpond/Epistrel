"""The clock's API calls (FR-CLOCK-5,13; #193): ``GET``, ``POST …/advance`` and ``PATCH
/stories/{id}/clock``, and the clock a turn reports.

- ``read_clock(db, story_id)`` is the clock as the story stands: its offset and display, the
  level the story runs it at (``off``: inert), its mode and its settings.
- ``advance_clock(db, story_id, request, *, principal)`` moves it forward between turns, as the
  consumer (FR-CLOCK-13): an inter-turn slot operation (``slots.open_inter_turn``) whose commit
  writes one advance at the slot (source ``consumer``) and records a ``clock_advanced`` event.
  ``minutes`` or ``to`` (an offset in minutes from the story's start, or ``"HH:MM"``, 422
  ``clock_unanchored`` unless the anchor fixes the exact time of day, #195: then the next such
  time). A backward one is
  422 ``clock_backward`` and writes nothing; with the clock off, 409 ``module_off``.
- ``update_clock(db, story_id, patch, *, principal)`` changes its settings: a story
  configuration write through ``core.states.update_definition`` (interpretive members, and
  ``pacing_table`` prospective; one ``config_changes`` row each, setting
  ``state:world_clock.<member>``). #209: ``mode = external`` makes the clock a provider's
  value (``core.world_clock.external_mode``): ``provider_id`` and ``external_value_name``, or
  ``provider_value = "<provider>.<value>"`` for both, name a registered provider's ``number``
  value (422 ``validation_error`` otherwise) the story is bound to (422 ``provider_unbound``);
  the clock's ``owner``, ``provider_id`` and ``external_value_name`` change with ``mode``
  (interpretive members, one ``config_changes`` row each), and leaving ``external`` makes it
  the story's again. An external clock's advance is 422 ``clock_external``.
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

from epistrel.core import providers, states
from epistrel.core.enums import ClockMode, Provenance, StateChangeSource, StateOwner
from epistrel.core.errors import ClockBackward, ClockUnanchored, InvalidRequest, ProviderUnbound
from epistrel.core.events.types import ClockAdvanced as ClockAdvancedEvent
from epistrel.core.membership import POSITION_MAX, Position
from epistrel.core.states.model import REASON_MAX, WORLD_CLOCK
from epistrel.core.tables import stories
from epistrel.core.world_clock import (
    HEAD,
    ClockValue,
    advance,
    anchor_at,
    clock_at,
    clock_level,
    external_mode,
    require_clock,
    settings,
)
from epistrel.core.world_clock.anchor import Anchor
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
    "provider_value",
    "provider_id",
    "external_value_name",
)
"""The settings ``PATCH /clock`` takes (#209: the provider of an ``external`` clock)."""
PROVIDER_VALUE = re.compile(r"^([^.\s]+)\.([^.\s]+)$")
"""``provider_value``: ``"<provider>.<value>"``."""


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


def _provider_members(definition: states.StateDefinition) -> dict[str, Any]:
    """#209: an external clock's ``provider_id`` and ``external_value_name`` (``None``: the
    story's clock)."""
    return {
        "provider_id": definition.provider_id,
        "external_value_name": definition.external_value_name,
    }


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
    document["calendar"] = found.calendar.document()
    return document


async def read_clock(db: AsyncEngine, story_id: str) -> ClockView:
    """The clock as the story stands (see the module docstring)."""
    async with db.connect() as conn:
        definition = await states.definition(conn, story_id, WORLD_CLOCK)
        found = await settings(conn, story_id)
        value = await clock_at(conn, story_id, HEAD)
        level = await clock_level(conn, story_id)
    return ClockView(
        offset=value.offset_minutes,
        display=value.display(),
        anchor=value.anchor,
        level=level or "off",
        mode=found.mode.value,
        settings={**_settings_document(found), **_provider_members(definition)},
    )


async def _time_of_day(conn: AsyncConnection, story_id: str, wanted: str, current: int) -> int:
    """The offset of the next ``"HH:MM"`` at or after ``current`` (#195): the anchor must fix
    the exact minute of the day there, else 422 ``clock_unanchored`` (the engine never picks
    one)."""
    anchor = await anchor_at(conn, story_id, HEAD)
    found = anchor.absolute_at(current)
    day = anchor.clockwork.day_minutes
    target = int(wanted[:2]) * 60 + int(wanted[3:])
    if anchor.is_unanchored or len(found.segments) != 1 or target >= day:
        raise ClockUnanchored(
            "the clock's anchor does not fix the time of day, so a time of day cannot be placed",
            {"story_id": story_id, "to": wanted},
        )
    (segment,) = found.segments
    if segment.hi - segment.lo != 1:
        raise ClockUnanchored(
            "the clock's anchor does not fix the time of day, so a time of day cannot be placed",
            {"story_id": story_id, "to": wanted},
        )
    return current + (target - segment.lo) % day


async def _precheck(
    conn: AsyncConnection, story_id: str, request: ClockAdvanceRequest
) -> int | None:
    """The refusals a consumer's advance gets before any operation opens: the clock off, a
    time of day the anchor cannot place, and a backward target (the commit checks again).
    Answers the target offset of a time of day (``None`` for any other advance)."""
    await require_clock(conn, story_id)
    # #209: an external clock is the provider's (422 clock_external, nothing written).
    external_mode.refuse_advance(await states.definition(conn, story_id, WORLD_CLOCK), story_id)
    current = (await clock_at(conn, story_id, HEAD)).offset_minutes
    if isinstance(request.to, str):
        return await _time_of_day(conn, story_id, request.to, current)
    attempted = current + request.minutes if request.minutes is not None else request.to
    if attempted is not None and int(attempted) < current:
        raise ClockBackward(
            "the world clock never moves backward: truncate or rewrite to reach earlier story-time",
            {"story_id": story_id, "current": current, "attempted": attempted},
        )
    return None


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
        placed = await _precheck(conn, story_id, request)
    to_offset = request.to if isinstance(request.to, int) else placed
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
    assert op.timeline_revision is not None  # noqa: S101 - set by a committed story operation
    async with db.connect() as conn:
        anchor = await anchor_at(conn, story_id, HEAD)
    return ClockMoved(
        operation_id=op.operation_id,
        change_id=moved.change_id,
        before=_value(anchor, moved.before),
        after=_value(anchor, moved.after),
        position=moved.position.document(),
        timeline_revision=op.timeline_revision,
    )


def _value(anchor: Anchor, offset: int) -> ClockValue:
    """The clock at ``offset`` under ``anchor``."""
    if anchor.is_unanchored:
        return ClockValue(offset)
    return ClockValue(offset, anchor.document(), anchor.display(offset))


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
    wanted = dict(patch)
    provider_id, value_name = _provider_of(wanted)
    async with db.begin() as conn:
        before = await states.definition(conn, story_id, WORLD_CLOCK)
        owner = await _owner_patch(conn, story_id, before, wanted, provider_id, value_name)
        after = await states.update_definition(
            conn,
            story_id,
            WORLD_CLOCK,
            {**wanted, **owner},
            principal=principal,
            clock_owner=bool(owner),
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
        settings={**_settings_document(after.clock), **_provider_members(after)},
        config_revision=int(row.config_revision),
        timeline_revision=int(row.timeline_revision),
        changed=after.document() != before.document(),
    )


def _provider_of(patch: dict[str, Any]) -> tuple[str | None, str | None]:
    """#209: the provider and value a ``PATCH`` names (``provider_value``, or ``provider_id``
    and ``external_value_name``), taken out of ``patch``."""
    shorthand = patch.pop("provider_value", None)
    provider_id = patch.pop("provider_id", None)
    value_name = patch.pop("external_value_name", None)
    if shorthand is None:
        return provider_id, value_name
    found = PROVIDER_VALUE.match(str(shorthand))
    if found is None:
        raise InvalidRequest(
            'provider_value is "<provider>.<value>"',
            {"field": "provider_value", "reason": "format"},
        )
    if (provider_id is not None and provider_id != found.group(1)) or (
        value_name is not None and value_name != found.group(2)
    ):
        raise InvalidRequest(
            "provider_value names another provider or value than provider_id and "
            "external_value_name",
            {"field": "provider_value", "reason": "conflict"},
        )
    return found.group(1), found.group(2)


async def _owner_patch(
    conn: AsyncConnection,
    story_id: str,
    before: states.StateDefinition,
    patch: dict[str, Any],
    provider_id: str | None,
    value_name: str | None,
) -> dict[str, Any]:
    """#209: the clock's owner members a ``PATCH`` changes (see the module docstring): to the
    named provider's ``number`` value for ``mode = external`` (or a new provider for an
    external clock), back to the story's on leaving ``external``; empty otherwise."""
    mode = patch.get("mode")
    was_external = before.owner is StateOwner.EXTERNAL
    naming = provider_id is not None or value_name is not None
    external = mode == ClockMode.EXTERNAL.value or (mode is None and was_external and naming)
    if not external:
        if naming:
            raise InvalidRequest(
                "a clock names a provider only in external mode",
                {"field": "mode", "reason": "not_external"},
            )
        if mode is not None and was_external:
            return {
                "owner": StateOwner.STORY.value,
                "provider_id": None,
                "external_value_name": None,
            }
        return {}
    provider_id = provider_id or before.provider_id
    value_name = value_name or before.external_value_name
    if provider_id is None or value_name is None:
        raise InvalidRequest(
            "an external clock names its provider's value: provider_value "
            '"<provider>.<value>", or provider_id and external_value_name',
            {"field": "provider_value", "reason": "required"},
        )
    await providers.check_external_value(conn, provider_id, value_name, external_mode.MANIFEST_TYPE)
    if await providers.find_binding(conn, story_id, provider_id) is None:
        raise ProviderUnbound(
            f"the story is not bound to provider {provider_id!r}",
            {"story_id": story_id, "providers": [provider_id]},
        )
    return {
        "owner": StateOwner.EXTERNAL.value,
        "provider_id": provider_id,
        "external_value_name": value_name,
    }


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
