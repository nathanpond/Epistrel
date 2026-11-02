"""The scheduled-events API (FR-CLOCK-9,19; #196): list, create, reschedule and cancel a
story's scheduled events.

- ``list_events(db, story_id, viewpoint)`` (``GET /stories/{id}/scheduled-events``): for
  ``omniscient``, every canon event as the story stands; for a character, only the events the
  character holds: the canon events it participates in (the viewpoint's scope, ADR-167: an event
  names its participants, never a holder), and, under per-character partitions, its belief-only
  schedules, its open first-order ``scheduled`` beliefs held at ``suspected`` or above that are
  no canon event's, labelled ``belief``.
- ``create_event`` (``POST``): an inter-turn slot operation (#193's ``open_inter_turn``) whose
  commit writes the canon ``scheduled`` fact (the event an ``event`` entity of the given name,
  its subject the first participant, else the event itself; the planned time an offset, or a
  time of day ``"HH:MM"`` the anchor places at its next occurrence), which
  ``core.canon.facts.commit_fact`` makes a ``pending`` event of ``scheduled_events``. #197: on a
  clock anchored only to a range, a time of day is held at its frame minute
  (``stops.place_event``): planned where the range's lower end reaches it, and
  ``possibly_due`` at once when it already lies inside the range. The owner
  is ``author`` or ``world`` (a character's own plans are created by its turns, #198).
- ``reschedule_event`` (``PATCH``): the event moves to ``pending`` at its new time, and may stop
  the clock once more. Only the owner, a shared owner, the author or the world may change an
  event (FR-CLOCK-19, ``may_change``); the consumer acts as the author.
- ``shared_owners`` (#198, FR-CLOCK-19): ``POST`` and ``PATCH`` declare the characters an event
  is shared with (cast character ids; anything else is 422 ``viewpoint_unresolved``), the only
  way an event becomes shared in M6. A shared owner may change the event by its own statement
  as the owner may (``core.world_clock.schedule_statements``). A ``PATCH`` with
  ``shared_owners`` and no ``planned`` keeps the event's status and time.
- ``cancel_event`` (``DELETE``): the event is ``cancelled``.

Each write is gated on the clock (409 ``module_off`` while the story runs it off) and answers
the event as it stands after it. Log lines carry identifiers only (invariant 3).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    StrictStr,
    field_validator,
    model_validator,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from epistrel.core.enums import (
    BeliefConfidenceLevel,
    Provenance,
    RecordProvenance,
    ScheduledStatus,
)
from epistrel.core.errors import (
    ClockUnanchored,
    InvalidRequest,
    StoryNotFound,
    ViewpointUnresolved,
)
from epistrel.core.membership import Position
from epistrel.core.settings_schema import CHARACTER_VIEWPOINT_PREFIX, OMNISCIENT
from epistrel.core.tables import characters, entities, stories
from epistrel.core.tables.propositions import propositions
from epistrel.core.tables.scheduled_events import OWNER_AUTHOR, OWNER_WORLD
from epistrel.core.viewpoint import resolve_viewpoint
from epistrel.core.world_clock import HEAD, anchor_at, clock_at, require_clock, stops, time_basis
from epistrel.core.world_clock.anchor import Anchor
from epistrel.core.world_clock.service import TIME_OF_DAY, _time_of_day
from epistrel.core.world_clock.slots import open_inter_turn, slot_position

EVENT_NAME_MAX = 200
CANON, BELIEF = "canon", "belief"


class ScheduledEventCreate(BaseModel):
    """``POST /stories/{id}/scheduled-events``: the event (``proposition``: its name, "Jenni's
    meeting"), when it is planned (``planned``: an offset in minutes from the story's start, or
    a time of day ``"HH:MM"``), who it involves (``participants``: character ids), where
    (``place``: a place entity id) and its ``owner`` (``author`` or ``world``)."""

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {"proposition": "the delivery", "planned": "06:00", "owner": "world"},
            ]
        },
    )

    proposition: StrictStr = Field(min_length=1, max_length=EVENT_NAME_MAX)
    planned: StrictInt | StrictStr
    participants: list[StrictStr] = Field(default_factory=list, max_length=20)
    place: StrictStr | None = None
    owner: Literal["author", "world"] = "world"
    shared_owners: list[StrictStr] = Field(default_factory=list, max_length=20)
    """#198: the characters the event is shared with (cast character ids)."""

    @field_validator("planned")
    @classmethod
    def _planned(cls, value: int | str) -> int | str:
        if isinstance(value, str) and not TIME_OF_DAY.match(value):
            raise ValueError('planned is an offset in minutes or a time of day "HH:MM"')
        if isinstance(value, int) and value < 0:
            raise ValueError("planned is an offset of 0 or more")
        return value


class ScheduledEventReschedule(BaseModel):
    """``PATCH /stories/{id}/scheduled-events/{event_id}``: the new ``planned`` time (an offset
    in minutes from the story's start, or a time of day ``"HH:MM"``) and/or the characters the
    event is shared with (``shared_owners``, #198: the whole new set; cast character ids). At
    least one is given."""

    model_config = ConfigDict(extra="forbid", json_schema_extra={"examples": [{"planned": 1140}]})

    planned: StrictInt | StrictStr | None = None
    shared_owners: list[StrictStr] | None = Field(default=None, max_length=20)

    @field_validator("planned")
    @classmethod
    def _planned(cls, value: int | str | None) -> int | str | None:
        return None if value is None else ScheduledEventCreate._planned(value)

    @model_validator(mode="after")
    def _something(self) -> ScheduledEventReschedule:
        if self.planned is None and self.shared_owners is None:
            raise ValueError("give planned, shared_owners or both")
        return self


@dataclass(frozen=True, slots=True)
class ScheduledEventView:
    """One event as a viewpoint reads it: ``label`` ``canon`` (an event of the story) or
    ``belief`` (a character's belief-only schedule: ``status`` ``null``, never a stop)."""

    event_id: str
    name: str | None
    label: str
    status: str | None
    planned: int | None
    owner: str | None
    participants: tuple[str, ...]
    place_id: str | None
    grace_minutes: int | None
    stopped_clock_at: int | None
    confidence: str | None = None
    shared_owners: tuple[str, ...] = ()

    def document(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "name": self.name,
            "label": self.label,
            "status": self.status,
            "planned": self.planned,
            "owner": self.owner,
            "shared_owners": list(self.shared_owners),
            "participants": list(self.participants),
            "place_id": self.place_id,
            "grace_minutes": self.grace_minutes,
            "stopped_clock_at": self.stopped_clock_at,
            "confidence": self.confidence,
        }


@dataclass(frozen=True, slots=True)
class ScheduledEventList:
    """``GET``: the events the viewpoint holds, the viewpoint and the story's revision."""

    viewpoint: str
    events: tuple[ScheduledEventView, ...]
    timeline_revision: int


@dataclass(frozen=True, slots=True)
class ScheduledEventWritten:
    """A write's answer: its slot operation, the event after it and the story's revision."""

    operation_id: str
    event: ScheduledEventView
    timeline_revision: int
    replayed: bool = False
    """``True`` when an ``Idempotency-Key`` retry returned this stored result (#61)."""


class ScheduledEventNotFound(StoryNotFound):
    """No canon scheduled event of the story has that id."""


@dataclass(slots=True)
class _Story:
    revision: int
    default_viewpoint: str | None
    cast: tuple[str, ...]
    names: dict[str, str] = field(default_factory=dict)


async def _story(conn: AsyncConnection, story_id: str) -> _Story:
    from epistrel.core.settings_schema import StorySettings

    row = (
        await conn.execute(
            select(stories.c.timeline_revision, stories.c.settings).where(stories.c.id == story_id)
        )
    ).one_or_none()
    if row is None:
        raise StoryNotFound(f"story {story_id} does not exist", {"story_id": story_id})
    cast = tuple(
        str(c)
        for c in await conn.scalars(
            select(characters.c.id)
            .where(characters.c.story_id == story_id)
            .order_by(characters.c.id)
        )
    )
    return _Story(
        int(row.timeline_revision),
        StorySettings.from_row(row.settings).default_output_viewpoint,
        cast,
    )


async def event_names(conn: AsyncConnection, event_ids: Sequence[str]) -> dict[str, str]:
    """Each event's name: its proposition's event entity's canonical name."""
    if not event_ids:
        return {}
    p = propositions.c
    rows = await conn.execute(
        select(p.id, entities.c.canonical_name)
        .join(entities, entities.c.id == p.object_id)
        .where(p.id.in_(list(event_ids)))
    )
    return {str(r.id): str(r.canonical_name) for r in rows}


def _view(event: stops.ScheduledEvent, names: dict[str, str]) -> ScheduledEventView:
    return ScheduledEventView(
        event_id=event.event_id,
        name=names.get(event.event_id),
        label=CANON,
        status=event.status.value,
        planned=event.planned,
        owner=event.owner,
        participants=event.participants,
        place_id=event.place_id,
        grace_minutes=event.grace,
        stopped_clock_at=event.stopped_clock_at,
        shared_owners=event.shared_owners,
    )


async def list_events(
    db: AsyncEngine, story_id: str, *, viewpoint: str | None
) -> ScheduledEventList:
    """The events ``viewpoint`` holds (see the module docstring)."""
    from epistrel.core.epistemic import partitions
    from epistrel.core.epistemic.scene import PARTITIONS_MODULE
    from epistrel.core.profiles.capabilities import module_on
    from epistrel.core.profiles.effective import effective_configuration
    from epistrel.core.propositions.vocabulary import SCHEDULED

    async with db.connect() as conn:
        story = await _story(conn, story_id)
        resolved = resolve_viewpoint(
            viewpoint,
            actor_character_id=None,
            story_default=story.default_viewpoint,
            cast_ids=story.cast,
            field="query.viewpoint",
        )
        found = await stops.events_at(conn, story_id)
        names = await event_names(conn, [e.event_id for e in found])
        if resolved == OMNISCIENT:
            views = [_view(e, names) for e in found]
            return ScheduledEventList(resolved, tuple(views), story.revision)
        holder = resolved.removeprefix(CHARACTER_VIEWPOINT_PREFIX)
        views = [_view(e, names) for e in found if holder in e.participants]
        settings = (await effective_configuration(conn, story_id, validate=False)).settings
        if module_on(settings, PARTITIONS_MODULE):
            canon_ids = {e.event_id for e in found}
            held = await partitions.partition(
                conn,
                story_id,
                holder,
                predicate=SCHEDULED,
                levels=partitions.at_least(BeliefConfidenceLevel.SUSPECTED),
            )
            beliefs = [b for b in held if b.proposition_id not in canon_ids]
            zero = (await time_basis(conn, story_id)).minute_zero
            object_names = await _object_names(conn, [b.proposition_id for b in beliefs])
            for belief in beliefs:
                minute = _minute_of(belief.proposition.valid_time)
                views.append(
                    ScheduledEventView(
                        event_id=belief.proposition_id,
                        name=object_names.get(belief.proposition_id),
                        label=BELIEF,
                        status=None,
                        planned=None if minute is None else max(0, minute - zero),
                        owner=None,
                        participants=(),
                        place_id=None,
                        grace_minutes=None,
                        stopped_clock_at=None,
                        confidence=belief.confidence_level.value,
                    )
                )
    return ScheduledEventList(resolved, tuple(views), story.revision)


async def _object_names(conn: AsyncConnection, proposition_ids: Sequence[str]) -> dict[str, str]:
    return await event_names(conn, proposition_ids)


def _minute_of(valid_time: Any) -> int | None:
    from epistrel.core.propositions.model import Instant, Interval

    if isinstance(valid_time, Instant):
        return int(valid_time.at)
    if isinstance(valid_time, Interval) and valid_time.start is not None:
        return int(valid_time.start.inner)
    return None


async def _planned(conn: AsyncConnection, story_id: str, planned: int | str) -> int:
    """An offset, or the next ``"HH:MM"`` the anchor places (422 ``clock_unanchored`` when it
    cannot)."""
    if isinstance(planned, int):
        return planned
    current = (await clock_at(conn, story_id, HEAD)).offset_minutes
    return await _time_of_day(conn, story_id, planned, current)


def _shared(shared_owners: Sequence[str] | None, cast: Sequence[str]) -> None:
    """#198: every shared owner is a character of the cast (422 ``viewpoint_unresolved``)."""
    unknown = sorted(set(shared_owners or ()) - set(cast))
    if unknown:
        raise ViewpointUnresolved(
            "a shared owner is not a character of the story",
            {"field": "body.shared_owners", "reason": "unknown_character", "unknown": unknown},
        )


@dataclass(frozen=True, slots=True)
class _Placed:
    """#197: a time of day a range-anchored clock places: its frame minute, under ``anchor``,
    at clock offset ``offset``."""

    at: int
    anchor: Anchor
    offset: int


async def _placed(
    conn: AsyncConnection, story_id: str, planned: int | str
) -> tuple[int, _Placed | None]:
    """A new event's planned offset (``_planned``), or, for a time of day the anchor holds only
    as a range, the offset where its lower end reaches it and the frame minute (#197)."""
    try:
        return await _planned(conn, story_id, planned), None
    except ClockUnanchored:
        if not isinstance(planned, str):  # pragma: no cover - an offset is always placed
            raise
        anchor = await anchor_at(conn, story_id, HEAD)
        minute = int(planned[:2]) * 60 + int(planned[3:])
        if anchor.is_unanchored or minute >= anchor.clockwork.day_minutes:
            raise
        current = (await clock_at(conn, story_id, HEAD)).offset_minutes
        at = anchor.place(minute, current)
        return anchor.due_window(at)[1], _Placed(at, anchor, current)


async def _validated(
    conn: AsyncConnection, story_id: str, request: ScheduledEventCreate, cast: Sequence[str]
) -> None:
    _shared(request.shared_owners, cast)
    unknown = sorted(set(request.participants) - set(cast))
    if unknown:
        raise InvalidRequest(
            "a participant is not a character of the story",
            {"field": "body.participants", "reason": "unknown_character", "unknown": unknown},
        )
    if request.place is not None:
        kind = await conn.scalar(
            select(entities.c.kind).where(
                entities.c.story_id == story_id, entities.c.id == request.place
            )
        )
        if kind != "place":
            raise InvalidRequest(
                "the place is not a place of the story",
                {"field": "body.place", "reason": "unknown_place"},
            )


async def create_event(
    db: AsyncEngine,
    story_id: str,
    request: ScheduledEventCreate,
    *,
    principal: str,
    idempotency_key: str | None = None,
) -> ScheduledEventWritten:
    """A canon event created between turns (see the module docstring)."""
    from epistrel.core.canon.facts import commit_fact
    from epistrel.core.propositions import store
    from epistrel.core.propositions.model import Granularity, Instant, Proposition
    from epistrel.core.propositions.vocabulary import SCHEDULED
    from epistrel.core.stories import require_writable

    await require_writable(db, story_id)
    async with db.connect() as conn:
        await require_clock(conn, story_id)
        story = await _story(conn, story_id)
        await _validated(conn, story_id, request, story.cast)
        planned, placed = await _placed(conn, story_id, request.planned)
    written: list[stops.ScheduledEvent] = []

    async def step(conn: AsyncConnection) -> None:
        position = slot_position(op)
        event_entity = await store.add_entity(
            conn, story_id, "event", request.proposition, operation_id=op.operation_id
        )
        zero = (await time_basis(conn, story_id)).minute_zero
        qualifiers: dict[str, Any] = {}
        if request.participants:
            qualifiers["participants"] = list(dict.fromkeys(request.participants))
        if request.place is not None:
            qualifiers["place"] = request.place
        subject = request.participants[0] if request.participants else event_entity
        with stops.sharing(request.shared_owners):
            fact = await commit_fact(
                conn,
                story_id,
                Proposition(
                    subject_id=subject,
                    predicate_id=SCHEDULED,
                    object_id=event_entity,
                    qualifiers=qualifiers,
                    valid_time=Instant.of(zero + planned, Granularity.MINUTE),
                ),
                provenance=RecordProvenance.AUTHOR
                if request.owner == OWNER_AUTHOR
                else RecordProvenance.IN_WORLD,
                operation=op,
                slot_k=position.slot_k,
            )
        found = await stops.event_at(conn, story_id, fact.proposition_id)
        assert found is not None  # noqa: S101 - commit_fact made the fact an event
        if placed is not None:
            found = (
                await stops.place_event(
                    conn,
                    op,
                    story_id,
                    found,
                    at=placed.at,
                    anchor=placed.anchor,
                    offset=placed.offset,
                    position=replace(found.position, step=found.position.step + 1),
                )
            ).event
        written.append(found)

    op = open_inter_turn(
        db,
        story_id,
        source=Provenance.CONSUMER,
        principal=principal,
        idempotency_key=idempotency_key,
    )
    async with op:
        op.add_commit_step(step)
        await op.commit()
    return await _answer(db, story_id, op, written[0])


async def _answer(
    db: AsyncEngine, story_id: str, op: Any, event: stops.ScheduledEvent
) -> ScheduledEventWritten:
    assert op.timeline_revision is not None  # noqa: S101 - set by a committed story operation
    async with db.connect() as conn:
        names = await event_names(conn, [event.event_id])
    return ScheduledEventWritten(
        operation_id=op.operation_id,
        event=_view(event, names),
        timeline_revision=op.timeline_revision,
    )


def may_change(event: stops.ScheduledEvent, actor: str) -> bool:
    """FR-CLOCK-19: only the event's owner, a shared owner (#198), the author or the world may
    change it."""
    return actor in (event.owner, *event.shared_owners, OWNER_AUTHOR, OWNER_WORLD)


async def _existing(conn: AsyncConnection, story_id: str, event_id: str) -> stops.ScheduledEvent:
    found = await stops.event_at(conn, story_id, event_id)
    if found is None:
        raise ScheduledEventNotFound(
            f"no scheduled event {event_id} in story {story_id}",
            {"story_id": story_id, "event_id": event_id},
        )
    return found


async def _change(
    db: AsyncEngine,
    story_id: str,
    event_id: str,
    *,
    status: ScheduledStatus | None,
    cause: str,
    planned: int | str | None,
    principal: str,
    idempotency_key: str | None,
    shared_owners: Sequence[str] | None = None,
) -> ScheduledEventWritten:
    from epistrel.core.stories import require_writable

    await require_writable(db, story_id)
    async with db.connect() as conn:
        await require_clock(conn, story_id)
        if shared_owners is not None:
            _shared(shared_owners, (await _story(conn, story_id)).cast)
        current = await _existing(conn, story_id, event_id)
        if not may_change(current, OWNER_AUTHOR):  # pragma: no cover - the author always may
            raise InvalidRequest(
                "only the event's owner, the author or the world may change it",
                {"field": "event_id", "reason": "not_owner"},
            )
        if current.status in (ScheduledStatus.CANCELLED, ScheduledStatus.COMPLETED):
            raise InvalidRequest(
                f"the event is {current.status.value}: it cannot change any more",
                {"field": "event_id", "reason": "event_settled", "status": current.status.value},
            )
        offset, placed = (None, None) if planned is None else await _placed(conn, story_id, planned)
    written: list[stops.ScheduledEvent] = []

    async def step(conn: AsyncConnection) -> None:
        position: Position = slot_position(op)
        found = await _existing(conn, story_id, event_id)
        if status is None:  # #198: only the sharing changes
            status_now, cause_now = found.status, stops.SHARED
        else:
            status_now, cause_now = status, cause
        held: dict[str, Any] | None = None
        if placed is not None and status_now is ScheduledStatus.PENDING:
            # #645: a time of day a range-anchored clock holds, as ``POST`` places one.
            held = {"at": int(placed.at)}
            if placed.anchor.scheduled(placed.at, placed.offset) == "possibly_due":
                status_now = ScheduledStatus.POSSIBLY_DUE
        done = await stops.record(
            conn,
            op,
            story_id,
            found,
            status_now,
            position=position,
            cause=cause_now,
            planned=offset,
            shared_owners=shared_owners,
            planned_anchor=held,
        )
        written.append(done.event)

    op = open_inter_turn(
        db,
        story_id,
        source=Provenance.CONSUMER,
        principal=principal,
        idempotency_key=idempotency_key,
    )
    async with op:
        op.add_commit_step(step)
        await op.commit()
    return await _answer(db, story_id, op, written[0])


async def reschedule_event(
    db: AsyncEngine,
    story_id: str,
    event_id: str,
    request: ScheduledEventReschedule,
    *,
    principal: str,
    idempotency_key: str | None = None,
) -> ScheduledEventWritten:
    """The event back to ``pending`` at its new time, and/or its new sharing (see the module
    docstring)."""
    return await _change(
        db,
        story_id,
        event_id,
        status=None if request.planned is None else ScheduledStatus.PENDING,
        cause=stops.RESCHEDULED,
        planned=request.planned,
        principal=principal,
        idempotency_key=idempotency_key,
        shared_owners=request.shared_owners,
    )


async def cancel_event(
    db: AsyncEngine,
    story_id: str,
    event_id: str,
    *,
    principal: str,
    idempotency_key: str | None = None,
) -> ScheduledEventWritten:
    """The event ``cancelled`` (see the module docstring)."""
    return await _change(
        db,
        story_id,
        event_id,
        status=ScheduledStatus.CANCELLED,
        cause=stops.CANCELLED,
        planned=None,
        principal=principal,
        idempotency_key=idempotency_key,
    )


__all__ = [
    "BELIEF",
    "CANON",
    "ScheduledEventCreate",
    "ScheduledEventList",
    "ScheduledEventNotFound",
    "ScheduledEventReschedule",
    "ScheduledEventView",
    "ScheduledEventWritten",
    "cancel_event",
    "create_event",
    "event_names",
    "list_events",
    "may_change",
    "reschedule_event",
]
