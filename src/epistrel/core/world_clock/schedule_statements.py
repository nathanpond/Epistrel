"""Creating a plan, changing it or reporting it (FR-CLOCK-19,10; docs/02 §7.1; #198).

A sincere schedule statement ("I'll go to the bank at three", "my meeting's at five", "I pushed
it to seven") is a ``scheduled(subject, event, time)`` claim. Before #166's three-way verify,
the claim's event is resolved against the story's canon scheduled events (``resolve``: the
event whose canon ``scheduled`` proposition names the same event entity, #139's resolution of
the mention; a cancelled event is no match), and ``classify`` decides what the statement is:

- **Create** (``Create``): no event matches, and the speaker is the plan's owner-to-be: the
  claim's subject or one of its ``participants``. The plan is the speaker's own errand, so it is
  committed as a canon ``scheduled`` event **owned** by the speaker (the canon fact at the
  beat's step, which ``core.world_clock.stops.on_canon_fact`` makes an event), and the speaker
  holds it: a ``discovery`` belief through ``core.epistemic.transitions.transition`` citing the
  committed canon fact as its ``record`` antecedent at the beat's step + 1 (FR-EPI-14: a belief
  citing the asserting beat itself would be ``self_attestation``). The world (a director's or
  narration's beat) creates a world-owned event the same way, with no belief.
- **Change** (``Change``): an event matches, the statement is an explicit change (the change
  lexicon ``CHANGE_LANGUAGE``: "moved", "pushed", "rescheduled", "instead", "changed to", "now
  at", or a declared ``reschedule`` effect, #187) and the speaker **may change** it: its owner, a
  shared owner, the author or the world (FR-CLOCK-19). The event is rescheduled (``rescheduled``
  back to ``pending`` at the new time, its one stop cleared), and the speaker holds the new time
  (an ``observation`` citing the reschedule's ``scheduled_event`` event at step + 1). Nothing is
  canonized: the change is a lifecycle step of the existing event.
- **Report** (``Report``): anything else about an existing event: a bare statement of its time,
  from anyone (the owner included), and a change attempted by someone who may not change it
  (``refused_change``). An ordinary claim: its **objective** relation is the event's planned
  time (``same_time``: ``supported`` when the claim states it, ``contradicted`` otherwise, citing
  the event's canon proposition), its **belief** relation the speaker's partition, its intent the
  beat's (#166). It never creates or reschedules canon: a sincere differing report is a sincere
  error when the speaker holds that time through a legitimate antecedent (she was told five)
  and a hallucination, repaired, when not.
- A statement about a plan that is no existing event and not the speaker's (a character saying
  "the gate closes at 15:10") is a ``Report`` with no event (``event is None``): never a
  ``Create``. Only a director's or the world's beat makes a world event (#144: a character's
  world claim is not its own plan); the claim is never canonized.

**The planned time.** A schedule names a time of day ("at three", "15:10", "five o'clock"):
``place_time`` places it at its next occurrence at or after the beat's moment, through the
clock's anchor (#195), which must fix the minute of the day there (else the time is unresolved
and the claim ``uncertain``); a time with two readings ("five o'clock": 05:00 or 17:00) takes the
sooner one.

Pure apart from ``resolve`` (one read of the story's events). Log lines carry identifiers only
(invariant 3); this module logs nothing.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from epistrel.core.enums import ScheduledStatus
from epistrel.core.propositions.model import Instant, Interval, Polarity, Proposition
from epistrel.core.tables.propositions import propositions
from epistrel.core.tables.scheduled_events import OWNER_AUTHOR, OWNER_WORLD
from epistrel.core.world_clock import stops

if TYPE_CHECKING:
    from epistrel.core.resolution.time import Position, TimeBasis

CHANGE_LANGUAGE = re.compile(
    r"\b(?:moved|pushed|rescheduled|instead|changed\s+(?:it\s+)?to|now\s+at)\b", re.IGNORECASE
)
"""The change lexicon (FR-CLOCK-19's "explicit change language"): anything else is a report."""

NOT_OWNERS = frozenset({OWNER_AUTHOR, OWNER_WORLD})


def explicit_change(text: str | None, *, declared: bool = False) -> bool:
    """Whether a statement is an explicit change: its text uses the change lexicon, or its beat
    declared a ``reschedule`` effect (``declared``)."""
    return declared or bool(text and CHANGE_LANGUAGE.search(text))


@dataclass(frozen=True, slots=True)
class Create:
    """A new plan: a canon event owned by ``owner`` (the speaker, or ``world``), involving
    ``participants``."""

    owner: str
    participants: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Change:
    """A change of ``event`` by one who may change it: rescheduled to story-minute
    ``planned_at``."""

    event: stops.ScheduledEvent
    planned_at: int


@dataclass(frozen=True, slots=True)
class Report:
    """An ordinary claim about ``event`` (``None``: no event, and not the speaker's own plan);
    ``refused_change``: a change attempted by one who may not change it."""

    event: stops.ScheduledEvent | None
    refused_change: bool = False


Statement = Create | Change | Report


def participants_of(claim: Proposition) -> tuple[str, ...]:
    """The plan's people as the claim states them: its subject (whose plan it is) and its
    ``participants`` qualifier."""
    raw = claim.qualifiers.get("participants") or ()
    named = [str(p) for p in raw] if isinstance(raw, list | tuple) else []
    return tuple(dict.fromkeys([claim.subject_id, *named]))


def planned_minute(claim: Proposition) -> int | None:
    """The story-minute the claim plans the event for (its valid-time instant; an interval's
    start), ``None`` when it states none."""
    valid = claim.valid_time
    if isinstance(valid, Instant):
        return int(valid.at)
    if isinstance(valid, Interval) and valid.start is not None:
        return int(valid.start.inner)
    return None


def may_change(event: stops.ScheduledEvent, speaker: str) -> bool:
    """FR-CLOCK-19: the owner, a shared owner, the author or the world."""
    return speaker in (event.owner, *event.shared_owners, *NOT_OWNERS)


def classify(
    claim: Proposition,
    speaker: str,
    resolved_event: stops.ScheduledEvent | None,
    explicit_change: bool,
) -> Statement:
    """What ``speaker``'s ``scheduled`` statement is (see the module docstring). ``speaker`` is
    a character id, or ``world``/``author`` for a director's, narration's or the author's beat;
    ``resolved_event`` the canon event the claim's event resolves to (``resolve``)."""
    if resolved_event is None:
        people = participants_of(claim)
        if speaker in NOT_OWNERS:
            return Create(speaker, people[1:])
        if speaker in people:
            return Create(speaker, people)
        return Report(None)
    at = planned_minute(claim)
    if explicit_change and at is not None and claim.polarity is Polarity.AFFIRMED:
        if may_change(resolved_event, speaker):
            return Change(resolved_event, at)
        return Report(resolved_event, refused_change=True)
    return Report(resolved_event)


def same_time(event: stops.ScheduledEvent, claim: Proposition, minute_zero: int) -> bool:
    """Whether the claim states the event's planned time (a report's objective relation)."""
    at = planned_minute(claim)
    return at is not None and at - minute_zero == event.planned


def place_time(text: str, basis: TimeBasis, position: Position) -> int | None:
    """``text`` (a time of day: "3 pm", "15:10", "five o'clock") as the story-minute of its next
    occurrence at or after ``position`` (see the module docstring); ``None`` when it is no time
    of day, or the clock's anchor does not fix the minute of the day there."""
    from epistrel.core.world_clock.anchor import statement_of

    anchor = basis.anchor
    if anchor is None or anchor.is_unanchored:
        return None
    statement = statement_of(text, anchor.clockwork)
    if statement is None or statement.time is None or statement.approximate:
        return None
    if any(v is not None for v in (statement.weekday, statement.year, statement.month)):
        return None
    if statement.day is not None:
        return None
    now = basis.at(position)
    current = now - basis.minute_zero
    found = anchor.absolute_at(current)
    if len(found.segments) != 1:
        return None
    (segment,) = found.segments
    if segment.hi - segment.lo != 1:
        return None
    day = anchor.clockwork.day_minutes
    readings = [lo for lo, hi in statement.time if hi - lo == 1]
    if not readings or len(readings) != len(statement.time):
        return None
    return now + min((lo - segment.lo) % day for lo in readings)


async def resolve(
    conn: AsyncConnection,
    story_id: str,
    claim: Proposition,
    *,
    before_turn: int | None = None,
    found: Iterable[stops.ScheduledEvent] | None = None,
) -> stops.ScheduledEvent | None:
    """The canon event the claim's event resolves to: the latest open or missed (not
    cancelled) event whose canon ``scheduled`` proposition names the claim's event entity
    (``None``: no match). ``before_turn``: only events written before that turn (a turn's own
    writes are not yet canon while it is checked); ``found``: the events already read."""
    if claim.object_id is None:
        return None
    events = list(found) if found is not None else await stops.events_at(conn, story_id)
    if before_turn is not None:
        events = [e for e in events if e.position.turn < before_turn]
    events = [e for e in events if e.status is not ScheduledStatus.CANCELLED]
    if not events:
        return None
    objects = await object_ids(conn, [e.event_id for e in events])
    matching = [e for e in events if objects.get(e.event_id) == claim.object_id]
    return max(matching, key=lambda e: (e.position.key(), e.event_id), default=None)


async def object_ids(conn: AsyncConnection, event_ids: Sequence[str]) -> dict[str, str]:
    """Each event's event entity: its canon ``scheduled`` proposition's object."""
    if not event_ids:
        return {}
    p = propositions.c
    rows = await conn.execute(
        select(p.id, p.object_id).where(p.id.in_(list(event_ids)), p.object_id.is_not(None))
    )
    return {str(r.id): str(r.object_id) for r in rows}


class Book:
    """One turn's view of the story's canon scheduled events, for ``classify`` (#198): the
    events written before ``turn`` (read once, on the first schedule statement)."""

    def __init__(self, conn: AsyncConnection, story_id: str, turn: int) -> None:
        self.conn = conn
        self.story_id = story_id
        self.turn = turn
        self._events: list[stops.ScheduledEvent] | None = None
        self._zero: int | None = None

    async def events(self) -> list[stops.ScheduledEvent]:
        if self._events is None:
            self._events = [
                e
                for e in await stops.events_at(self.conn, self.story_id)
                if e.position.turn < self.turn
            ]
        return self._events

    async def minute_zero(self) -> int:
        if self._zero is None:
            from epistrel.core import world_clock  # here: the clock package imports stops

            self._zero = int((await world_clock.time_basis(self.conn, self.story_id)).minute_zero)
        return self._zero

    async def statement(self, claim: Proposition, speaker: str, explicit: bool) -> Statement:
        """``classify`` over the claim's resolved event."""
        event = await resolve(self.conn, self.story_id, claim, found=await self.events())
        return classify(claim, speaker, event, explicit)


__all__ = [
    "CHANGE_LANGUAGE",
    "OWNER_AUTHOR",
    "OWNER_WORLD",
    "Book",
    "Change",
    "Create",
    "Report",
    "Statement",
    "classify",
    "explicit_change",
    "may_change",
    "object_ids",
    "participants_of",
    "place_time",
    "planned_minute",
    "resolve",
    "same_time",
]
