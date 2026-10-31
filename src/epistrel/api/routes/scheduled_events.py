"""A story's scheduled events (FR-CLOCK-9,19; #196).

- ``GET /stories/{id}/scheduled-events?viewpoint=``: the events the viewpoint holds.
- ``POST /stories/{id}/scheduled-events``: create a canon event between turns.
- ``PATCH /stories/{id}/scheduled-events/{event_id}``: reschedule it.
- ``DELETE /stories/{id}/scheduled-events/{event_id}``: cancel it.

Every handler makes one ``Engine`` call.
"""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Path, Response
from fastapi import status as http_status
from pydantic import BaseModel

from epistrel.api.deps import ContextDep, EngineDep, StoryId
from epistrel.api.idempotency import answered
from epistrel.api.responses import MutationResponse
from epistrel.core.ids import ULID_PATTERN
from epistrel.core.world_clock.scheduled import (
    ScheduledEventCreate,
    ScheduledEventList,
    ScheduledEventReschedule,
    ScheduledEventView,
    ScheduledEventWritten,
)

router = APIRouter(prefix="/stories", tags=["stories"])

EventId = Annotated[
    str,
    Path(pattern=ULID_PATTERN, description="The event's id (its canon `scheduled` proposition)."),
]

_GATE = (
    " 409 `module_off` while the story runs the world clock off, `story_archived`, "
    "`story_paused` or `revision_conflict`."
)


class ScheduledEventBody(BaseModel):
    """One scheduled event as the viewpoint reads it: its `event_id`, `name`, `label`
    (`canon`: an event of the story; `belief`: a character's belief-only schedule, which never
    stops the clock), its `status` (`pending`, `possibly_due`, `due`, `surfaced`, `completed`,
    `missed`, `cancelled`; null for a belief), its `planned` offset in minutes from the story's
    start, its `owner` (a character id, `author` or `world`), the characters it is shared with
    (`shared_owners`: they may change it as the owner may), `participants`, `place_id`,
    `grace_minutes`, the offset it stopped the clock at (`stopped_clock_at`: it stops it at
    most once) and, for a belief, the holder's `confidence`."""

    event_id: str
    name: str | None
    label: Literal["canon", "belief"]
    status: str | None
    planned: int | None
    owner: str | None
    shared_owners: list[str]
    participants: list[str]
    place_id: str | None
    grace_minutes: int | None
    stopped_clock_at: int | None
    confidence: str | None

    @classmethod
    def of(cls, view: ScheduledEventView) -> ScheduledEventBody:
        return cls.model_validate(view.document())


class ScheduledEventsBody(BaseModel):
    """The events the viewpoint holds (by planned time, canon first), the viewpoint the read
    was produced for and the story's revision."""

    output_viewpoint: str
    events: list[ScheduledEventBody]
    timeline_revision: int

    @classmethod
    def of(cls, found: ScheduledEventList) -> ScheduledEventsBody:
        return cls(
            output_viewpoint=found.viewpoint,
            events=[ScheduledEventBody.of(e) for e in found.events],
            timeline_revision=found.timeline_revision,
        )


class ScheduledEventWrittenBody(MutationResponse):
    """A write's answer: the inter-turn slot `operation_id` that recorded it and the `event`
    after it."""

    operation_id: str
    event: ScheduledEventBody

    @classmethod
    def of(cls, written: ScheduledEventWritten) -> ScheduledEventWrittenBody:
        return cls(
            operation_id=written.operation_id,
            event=ScheduledEventBody.of(written.event),
            timeline_revision=written.timeline_revision,
        )


@router.get(
    "/{story_id}/scheduled-events",
    response_model=ScheduledEventsBody,
    operation_id="list_scheduled_events",
    summary="List a story's scheduled events",
    description=(
        "The story's scheduled events (FR-CLOCK-9) for one output viewpoint: `?viewpoint=` "
        "(`omniscient` or `character:<id>`), else the story's `default_output_viewpoint`; 422 "
        "`viewpoint_unresolved` when none resolves. `omniscient` lists every canon event; a "
        "character lists only the events it holds: the canon events it participates in and, "
        "under per-character partitions, its own belief-only schedules (labelled `belief`; a "
        "belief never stops the clock). An event moves `pending` → `due` (the clock reaches "
        "it) → `surfaced` → `completed`, `missed` (still open past its planned time plus "
        "`grace_minutes`) or `cancelled`; a reschedule returns it to `pending`."
    ),
)
async def list_scheduled_events(
    story_id: StoryId, ctx: ContextDep, engine: EngineDep
) -> ScheduledEventsBody:
    return ScheduledEventsBody.of(await engine.list_scheduled_events(ctx, story_id))


@router.post(
    "/{story_id}/scheduled-events",
    response_model=ScheduledEventWrittenBody,
    status_code=http_status.HTTP_201_CREATED,
    operation_id="create_scheduled_event",
    summary="Create a scheduled event",
    description=(
        "Create a canon scheduled event between turns (FR-CLOCK-9): `proposition` names the "
        "event, `planned` is an offset in minutes from the story's start or a time of day "
        '`"HH:MM"` (its next occurrence; 422 `clock_unanchored` unless the anchor fixes the '
        "time of day), `participants` are character ids, `place` a place entity id, and "
        "`owner` is `author` or `world` (a character's own plans come from its turns); "
        "`shared_owners` are the characters it is shared with (cast character ids, else 422 "
        "`viewpoint_unresolved`), who may change it as the owner may (FR-CLOCK-19). The "
        "write is an inter-turn slot after the last turn (a truncation to that turn or "
        "earlier discards it). The event is `pending`: with stops on (the profile's "
        "`clock.scheduled_stops` and the clock's `stop_at_due_events`), an advance that would "
        "carry the clock past it while a participant is present, or the scene is its place, "
        "stops at its planned time, once." + _GATE
    ),
)
async def create_scheduled_event(
    story_id: StoryId,
    body: ScheduledEventCreate,
    response: Response,
    ctx: ContextDep,
    engine: EngineDep,
) -> ScheduledEventWrittenBody:
    written = await engine.create_scheduled_event(ctx, story_id, body)
    return answered(ScheduledEventWrittenBody.of(written), ctx, response, replayed=written.replayed)


@router.patch(
    "/{story_id}/scheduled-events/{event_id}",
    response_model=ScheduledEventWrittenBody,
    operation_id="reschedule_scheduled_event",
    summary="Reschedule a scheduled event",
    description=(
        'Move a scheduled event to a new `planned` time (an offset, or `"HH:MM"`): it is '
        "`pending` again and may stop the clock once more (FR-CLOCK-9). Only the event's "
        "owner, a shared owner, the author or the world may change it (FR-CLOCK-19); the "
        "consumer acts as the author. `shared_owners` (cast character ids, else 422 "
        "`viewpoint_unresolved`) replaces the characters the event is shared with; without "
        "`planned` it keeps the event's status and time. 404 for an unknown event; 422 "
        "(`reason: event_settled`) for a cancelled or completed one." + _GATE
    ),
)
async def reschedule_scheduled_event(
    story_id: StoryId,
    event_id: EventId,
    body: ScheduledEventReschedule,
    response: Response,
    ctx: ContextDep,
    engine: EngineDep,
) -> ScheduledEventWrittenBody:
    written = await engine.reschedule_scheduled_event(ctx, story_id, event_id, body)
    return answered(ScheduledEventWrittenBody.of(written), ctx, response, replayed=written.replayed)


@router.delete(
    "/{story_id}/scheduled-events/{event_id}",
    response_model=ScheduledEventWrittenBody,
    operation_id="cancel_scheduled_event",
    summary="Cancel a scheduled event",
    description=(
        "Cancel a scheduled event (FR-CLOCK-9): it is `cancelled` and never stops the clock. "
        "404 for an unknown event; 422 (`reason: event_settled`) for a cancelled or completed "
        "one." + _GATE
    ),
)
async def cancel_scheduled_event(
    story_id: StoryId,
    event_id: EventId,
    response: Response,
    ctx: ContextDep,
    engine: EngineDep,
) -> ScheduledEventWrittenBody:
    written = await engine.cancel_scheduled_event(ctx, story_id, event_id)
    return answered(ScheduledEventWrittenBody.of(written), ctx, response, replayed=written.replayed)


__all__ = [
    "ScheduledEventBody",
    "ScheduledEventWrittenBody",
    "ScheduledEventsBody",
    "router",
]
