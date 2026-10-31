"""A story's world clock (FR-CLOCK-1,3,5,13; #193).

- ``GET /stories/{id}/clock``: the clock as the story stands.
- ``POST /stories/{id}/clock/advance``: move it forward between turns, as the consumer.
- ``PATCH /stories/{id}/clock``: change its settings (a story configuration write).

The clock cannot be deleted, and the State API never sets it (``core.states`` refuses a write to
``world_clock`` with ``reason: reserved_state``, naming the advance route). Every handler makes
one ``Engine`` call.
"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Response
from pydantic import BaseModel, ConfigDict, Field

from epistrel.api.deps import ContextDep, EngineDep, StoryId
from epistrel.api.idempotency import answered
from epistrel.api.responses import MutationResponse
from epistrel.core.states.model import Calendar
from epistrel.core.world_clock import ClockValue
from epistrel.core.world_clock.service import (
    ClockAdvanceRequest,
    ClockMoved,
    ClockSettingsWrite,
    ClockView,
    TurnClock,
)

router = APIRouter(prefix="/stories", tags=["stories"])


class ClockValueBody(BaseModel):
    """The clock at one point: its exact ``offset`` in minutes from the story's start, and its
    ``display`` (``+HHh MMm`` from the start, then the anchor's known components and ranges, if
    any: a date or minute the story has not established is never shown)."""

    offset: int
    display: str

    @classmethod
    def of(cls, value: ClockValue) -> ClockValueBody:
        return cls(offset=value.offset_minutes, display=value.display())


class TurnClockBody(BaseModel):
    """The world clock ``before`` and ``after`` a turn (FR-CLOCK-13)."""

    before: ClockValueBody
    after: ClockValueBody

    @classmethod
    def of(cls, clock: TurnClock) -> TurnClockBody:
        return cls(before=ClockValueBody.of(clock.before), after=ClockValueBody.of(clock.after))


class ClockSettingsBody(BaseModel):
    """The clock's settings (FR-CLOCK-3,5,8,9): its ``mode``; the ``pacing_table`` rows
    ``[from_words, to_words (exclusive; null: no end), minutes]``; the cap on a skip a render
    starts on its own (``max_unprompted_skip_minutes``); whether it stops at due events
    (``stop_at_due_events``) and the grace after one (``grace_minutes``); how far a stated time
    may be from it (``time_statement_tolerance_minutes``) and an elapsed time
    (``elapsed_tolerance_factor``); how precisely characters know the time
    (``time_awareness``); the time-of-day ``bands`` (``[name, from, to]``); and the
    ``calendar``."""

    mode: Literal["paced", "manual", "external"]
    pacing_table: list[list[int | None]]
    max_unprompted_skip_minutes: int
    stop_at_due_events: bool
    grace_minutes: int
    time_statement_tolerance_minutes: int
    elapsed_tolerance_factor: float
    time_awareness: Literal["exact", "approximate", "none"]
    bands: list[list[str]]
    calendar: dict[str, Any]


class ClockBody(BaseModel):
    """The clock as the story stands: its ``offset`` and ``display``, its ``anchor`` (``null``:
    unanchored), the ``level`` the story's configuration runs it at (``off``: inert, no
    advance is written; ``prompt_only``; ``on``), its ``mode`` and its ``settings``."""

    offset: int
    display: str
    anchor: dict[str, Any] | None
    level: Literal["off", "prompt_only", "on"]
    mode: Literal["paced", "manual", "external"]
    settings: ClockSettingsBody

    @classmethod
    def of(cls, view: ClockView) -> ClockBody:
        return cls(
            offset=view.offset,
            display=view.display,
            anchor=view.anchor,
            level=view.level,
            mode=view.mode,
            settings=ClockSettingsBody.model_validate(view.settings),
        )


class ClockAdvancedBody(MutationResponse):
    """A consumer's advance: the inter-turn slot ``operation_id`` that recorded it, the
    ``change_id`` of its ``world_clock`` change, the clock ``before`` and ``after``, the slot's
    ``position`` (``{turn, variant, slot_k, beat, step}``) and the story's revision after."""

    operation_id: str
    change_id: str
    before: ClockValueBody
    after: ClockValueBody
    position: dict[str, int]

    @classmethod
    def of(cls, moved: ClockMoved) -> ClockAdvancedBody:
        return cls(
            operation_id=moved.operation_id,
            change_id=moved.change_id,
            before=ClockValueBody.of(moved.before),
            after=ClockValueBody.of(moved.after),
            position=dict(moved.position),
            timeline_revision=moved.timeline_revision,
        )


class ClockPatch(BaseModel):
    """``PATCH /stories/{id}/clock``: the settings to change (each optional)."""

    model_config = ConfigDict(extra="forbid", json_schema_extra={"examples": [{"mode": "manual"}]})

    mode: Literal["paced", "manual", "external"] | None = None
    pacing_table: list[list[int | None]] | None = None
    max_unprompted_skip_minutes: int | None = Field(default=None, ge=0)
    stop_at_due_events: bool | None = None
    time_awareness: Literal["exact", "approximate", "none"] | None = None
    bands: list[list[str]] | None = None
    calendar: Literal["gregorian"] | Calendar | None = None


class ClockSettingsWriteBody(MutationResponse):
    """The clock's settings after a ``PATCH``, the story's ``config_revision`` (moved by a
    change) and its unchanged ``timeline_revision``."""

    settings: ClockSettingsBody
    config_revision: int

    @classmethod
    def of(cls, write: ClockSettingsWrite) -> ClockSettingsWriteBody:
        return cls(
            settings=ClockSettingsBody.model_validate(write.settings),
            config_revision=write.config_revision,
            timeline_revision=write.timeline_revision,
        )


@router.get(
    "/{story_id}/clock",
    response_model=ClockBody,
    operation_id="get_clock",
    summary="Read a story's world clock",
    description=(
        "The story's world clock as it stands (FR-CLOCK-1,13): every story has one, created "
        "with it, and it cannot be deleted. `offset` is its exact value in minutes from the "
        "story's start (the fold of its active advances, so a truncation, a swap and a fork "
        "move it with everything else), `display` renders it (`+HHh MMm`, then what the anchor "
        "establishes: `+02h 10m (Saturday, between 05:00 and 12:00 (morning))`), `anchor` is "
        "its partial absolute time: each of year, month, day, weekday, band, hour and minute a "
        "value, a `[lo, hi]` range or null (unknown), never a value the story has not "
        "established (null: unanchored). The settings' `calendar` is `{kind: gregorian}` or "
        "`{kind: custom, day_minutes, months, weekdays, year_days}`. `level` "
        "is the level the story's configuration runs it at: `off` makes it inert (no advance "
        "is written, and story-time follows the clock-off rule), `prompt_only` writes advances "
        "and verifies nothing, `on` runs it all. `mode` and `settings` are set with "
        "`PATCH /stories/{id}/clock`."
    ),
)
async def get_clock(story_id: StoryId, ctx: ContextDep, engine: EngineDep) -> ClockBody:
    return ClockBody.of(await engine.get_clock(ctx, story_id))


@router.post(
    "/{story_id}/clock/advance",
    response_model=ClockAdvancedBody,
    operation_id="advance_clock",
    summary="Advance a story's world clock",
    description=(
        "Move the world clock forward between turns, as the consumer (FR-CLOCK-13): `minutes` "
        "forward, or `to` a target, either an offset in minutes from the story's start or a "
        'time of day `"HH:MM"`, with a `reason`. The advance is an inter-turn slot after the '
        "last turn (a truncation to that turn or earlier discards it, and the clock with it) "
        "and is recorded with the source `consumer`. Answers the clock `before` and `after`. "
        "422 `clock_backward` for a move backward (nothing is written; earlier story-time is "
        'reached by truncation or a rewrite), `clock_unanchored` for a `"HH:MM"` the clock\'s '
        "anchor cannot place (the anchor does not fix the exact time of day; once it does, "
        "the next such time); 409 `module_off` while the story runs the clock off, "
        "`story_archived`, `story_paused` or `revision_conflict`. A turn "
        "that was rejected or failed, and an ingested event, never advance the clock."
    ),
)
async def advance_clock(
    story_id: StoryId,
    body: ClockAdvanceRequest,
    response: Response,
    ctx: ContextDep,
    engine: EngineDep,
) -> ClockAdvancedBody:
    moved = await engine.advance_clock(ctx, story_id, body)
    return answered(ClockAdvancedBody.of(moved), ctx, response, replayed=moved.replayed)


@router.patch(
    "/{story_id}/clock",
    response_model=ClockSettingsWriteBody,
    operation_id="update_clock",
    summary="Change a story's world clock settings",
    description=(
        "Change the world clock's settings (FR-CLOCK-5): `mode` (`paced`, the default: the "
        "pacing rule and validated skips advance it; `manual`: only explicit skips do; "
        "`external` needs a registered provider and is refused until it can have one), the "
        "`pacing_table`, `max_unprompted_skip_minutes` (0 disables a skip a render starts on "
        "its own), `stop_at_due_events`, `time_awareness`, the time-of-day `bands` and the "
        "`calendar`. A story configuration write: each changed setting records a "
        "`config_changes` row (`state:world_clock.<setting>`; `pacing_table` is prospective, "
        "the others interpretive, so a turn checked under the old value is re-checked) and "
        "the story's `config_revision` moves by one; setting the values it has changes "
        "nothing. 422 for an unknown setting or an invalid value; 409 `story_archived`."
    ),
)
async def update_clock(
    story_id: StoryId,
    body: ClockPatch,
    response: Response,
    ctx: ContextDep,
    engine: EngineDep,
) -> ClockSettingsWriteBody:
    done = await engine.update_clock(ctx, story_id, body.model_dump(exclude_unset=True))
    return answered(ClockSettingsWriteBody.of(done), ctx, response, replayed=done.replayed)


__all__ = [
    "ClockAdvancedBody",
    "ClockBody",
    "ClockPatch",
    "ClockSettingsWriteBody",
    "ClockValueBody",
    "TurnClockBody",
    "router",
]
