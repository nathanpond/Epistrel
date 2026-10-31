"""Scheduled events and the clock's stops (FR-CLOCK-9,16,17; docs/02 §7.1; #196).

**Scheduled events are canon.** A time-anchored plan the author, the world, narration or a State
sets ("my meeting is at five", "the last train leaves at 23:40") is a canon ``scheduled`` fact
(#131's core predicate: the subject owns it, the event is its object, the planned time its
valid-time instant, ``participants`` and ``place`` its qualifiers), and **every canon one is an
event of ``scheduled_events``**, written with the fact in the same transaction
(``on_canon_fact``, which ``core.canon.facts.commit_fact`` calls) or created between turns
(``create``; the API's ``POST``). A ``scheduled`` belief in a character's partition (#159) is
never a row and never stops the clock: it shapes that character's context only (FR-CLOCK-9's
belief-only rule).

**The lifecycle** moves ``pending → due → surfaced → completed | missed | cancelled``, and a
reschedule returns the event to ``pending`` at its new time (#197 adds ``possibly_due``). Each
step is a **derived event** (``scheduled_event``) at the step of the turn or slot that caused
it, and a version row of ``scheduled_events`` written with it (``record``): the event's state at
a position is its latest active version there (``events_at``), so a truncation, a swap and a
regenerate move the lifecycle with the turns that caused it, and a fork copies it through the
fork point.

**The stop rules** (``check_advance``; FR-CLOCK-9,16,17):

- ``due_between(story, from, to, present, scene)`` is the range read: the ``pending`` canon
  events planned in ``(from, to]`` that **involve** the scene: a participant among the present
  characters, or the event's place the scene's location (``ScheduledEvent.involves``; an event
  with neither never stops the clock).
- ``check_advance(advance, present, scene, events=, stops=)`` answers ``Proceed`` (nothing due;
  or, with stops off, the events crossed, surfaced as overdue), ``Stop(event, at_offset)``
  (the first such event: the advance stops at its planned time) or ``Pause`` (#197's
  ``possibly_due``). Stops apply only when **both** the effective profile's
  ``clock.scheduled_stops`` (#106's flag) and the story's ``world_clock.stop_at_due_events``
  are on (``stops_enabled``).
- An event stops the clock **at most once**: a stop records ``due`` with ``stopped_clock_at``,
  and only a ``pending`` event stops an advance, so it never stops again unless it is
  rescheduled.
- **A turn's cut** (``cut_turn``): a sequential turn's renders are checked in order, each from
  where the previous one ended. A stop inside a render's **pacing** increment clamps the render's
  end at the event's time (no regeneration; later renders start there with the event surfaced
  to them); a stop inside a **skip** (a declared or narrated advance) caps the advance at the
  event's time and asks for the render's regeneration under the cutoff (FR-CLOCK-16:
  ``Cut.regenerate``; the Orchestrator's repair reason ``time_stop``). A simultaneous turn is
  checked once, after reconciliation, over all its renders together (and over the events its own
  renders create, ``created``): its cutoff is the earliest stop, every render is capped there, a
  pacing-only overrun is clamped and a skip that crossed it is regenerated, so every render ends
  at or before the turn's end, which is the cutoff.

**Grace and completion** (``turn_steps``): an event that was ``due`` or ``surfaced`` before a
turn becomes ``missed`` when the turn ends past its planned time plus its ``grace_minutes``
(evaluated at the next advance past it: no timer, so a replay decides the same), and
``completed`` when every participant has left the scene; one stopped by a pacing clamp at the
end of a turn is surfaced to the next turn. The involved characters present when an event is
missed observe it (``observe_missed``: an ``observation`` transition through
``core.epistemic.transitions.transition``, citing the ``scheduled_event`` event as its
``record`` antecedent, at the step after it; ADR-168); anyone else learns of it only when a beat
narrates it.

Log lines carry identifiers and counts only (invariant 3); this module logs nothing.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any

from sqlalchemy import func, insert, select
from sqlalchemy.ext.asyncio import AsyncConnection

from epistrel.core.enums import (
    Authority,
    EventCategory,
    Provenance,
    RecordProvenance,
    ScheduledStatus,
)
from epistrel.core.erasure import guard
from epistrel.core.erasure.index import index_records
from epistrel.core.erasure.records import event_record, scheduled_event_record
from epistrel.core.events.registry import validate
from epistrel.core.events.types import ScheduledEventChanged
from epistrel.core.ids import new_ulid
from epistrel.core.membership import POSITION_MAX, Position, active_only, insert_derived
from epistrel.core.tables import events
from epistrel.core.tables.scheduled_events import OWNER_AUTHOR, OWNER_WORLD, scheduled_events
from epistrel.core.world_clock.timing import (
    SIMULTANEOUS,
    TimedBeat,
    TurnTiming,
    time_turn,
)

if TYPE_CHECKING:
    from epistrel.core.canon.facts import Fact, FactOperation

EVENT_TYPE = "scheduled_event"
"""The derived event each lifecycle step records."""

S = ScheduledStatus
OPEN: frozenset[ScheduledStatus] = frozenset({S.PENDING, S.POSSIBLY_DUE, S.DUE, S.SURFACED})
"""The statuses an event is still open in (not completed, missed or cancelled)."""
SETTLED: frozenset[ScheduledStatus] = frozenset({S.COMPLETED, S.MISSED, S.CANCELLED})
_RANK: Mapping[ScheduledStatus, int] = {
    S.PENDING: 0,
    S.POSSIBLY_DUE: 1,
    S.DUE: 2,
    S.SURFACED: 3,
    S.COMPLETED: 4,
    S.MISSED: 4,
    S.CANCELLED: 4,
}
"""Two versions of one event at the same position: the later lifecycle step wins."""

CREATED, RESCHEDULED, STOP, OVERDUE, SURFACED_CAUSE = (
    "created",
    "rescheduled",
    "stop",
    "overdue",
    "surfaced",
)
GRACE, LEFT, CANCELLED = "grace", "left", "cancelled"
SHARED = "shared"
"""#198: the event's shared owners changed (its status and time kept)."""
CAUSES = frozenset(
    {CREATED, RESCHEDULED, STOP, OVERDUE, SURFACED_CAUSE, GRACE, LEFT, CANCELLED, SHARED}
)
"""Why a lifecycle step happened (``scheduled_event.cause``)."""


# -- the event ------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ScheduledEvent:
    """A canon scheduled event as of a position: its key (``event_id``: the canon ``scheduled``
    proposition it was created with), its latest version there (``version_id``, ``status``,
    ``position``), its plan (``planned`` clock offset, ``planned_anchor``, ``owner``,
    ``participants``, ``place_id``, ``grace``) and what it did (``stopped_clock_at``: the
    offset it stopped the clock at, ``None``: it has not; ``refinement_pause_anchor_rev``:
    #197's pause)."""

    event_id: str
    status: ScheduledStatus
    planned: int
    owner: str = OWNER_WORLD
    participants: tuple[str, ...] = ()
    place_id: str | None = None
    grace: int = 30
    version_id: str | None = None
    position: Position = field(default_factory=lambda: Position(0))
    planned_anchor: Mapping[str, Any] | None = None
    stopped_clock_at: int | None = None
    refinement_pause_anchor_rev: int | None = None
    operation_id: str | None = None
    shared_owners: tuple[str, ...] = ()
    """#198 (FR-CLOCK-19): the characters the event is shared with, who may change it as its
    owner may."""

    def involves(self, present: Iterable[str], scene: str | None) -> bool:
        """Whether the event involves the scene: a participant is present, or its place is the
        scene's location (FR-CLOCK-9's "a present character or the scene")."""
        if set(self.participants) & set(present):
            return True
        return self.place_id is not None and scene is not None and self.place_id == scene

    @property
    def is_open(self) -> bool:
        return self.status in OPEN

    def document(self) -> dict[str, Any]:
        """The event as the API answers it (identifiers, offsets and its status)."""
        return {
            "event_id": self.event_id,
            "status": self.status.value,
            "planned": self.planned,
            "owner": self.owner,
            "shared_owners": list(self.shared_owners),
            "participants": list(self.participants),
            "place_id": self.place_id,
            "grace_minutes": self.grace,
            "stopped_clock_at": self.stopped_clock_at,
            "position": self.position.document(),
        }


def _position(row: Any) -> Position:
    return Position(
        int(row.narrative_turn or 0),
        int(row.turn_variant or 0),
        int(row.slot_k or 0),
        int(row.beat or 0),
        int(row.step or 0),
    )


def _event_of(row: Any) -> ScheduledEvent:
    return ScheduledEvent(
        event_id=str(row.proposition_id),
        status=ScheduledStatus(row.status),
        planned=int(row.planned_offset_minutes),
        owner=str(row.owner),
        participants=tuple(str(p) for p in row.participants or ()),
        place_id=None if row.place_id is None else str(row.place_id),
        grace=int(row.grace_minutes),
        version_id=str(row.id),
        position=_position(row),
        planned_anchor=row.planned_anchor,
        stopped_clock_at=None if row.stopped_clock_at is None else int(row.stopped_clock_at),
        refinement_pause_anchor_rev=None
        if row.refinement_pause_anchor_rev is None
        else int(row.refinement_pause_anchor_rev),
        operation_id=str(row.operation_id),
        shared_owners=tuple(str(o) for o in getattr(row, "shared_owners", None) or ()),
    )


def _order(event: ScheduledEvent) -> tuple[tuple[int, int, int, int], int, str]:
    return (event.position.key(), _RANK[event.status], event.version_id or "")


async def events_at(
    conn: AsyncConnection,
    story_id: str,
    through: Position | None = None,
    *,
    event_ids: Sequence[str] | None = None,
) -> list[ScheduledEvent]:
    """Every canon scheduled event of the story as of ``through`` (``None``: as the story
    stands): each event's latest active version at or before it, by planned time."""
    c = scheduled_events.c
    where: list[Any] = [c.story_id == story_id, active_only(scheduled_events)]
    if event_ids is not None:
        where.append(c.proposition_id.in_(list(event_ids)))
    rows = await conn.execute(select(scheduled_events).where(*where))
    latest: dict[str, ScheduledEvent] = {}
    for row in rows:
        found = _event_of(row)
        if through is not None and not found.position.at_or_before(through):
            continue
        held = latest.get(found.event_id)
        if held is None or _order(found) > _order(held):
            latest[found.event_id] = found
    return sorted(latest.values(), key=lambda e: (e.planned, e.event_id))


async def event_at(
    conn: AsyncConnection, story_id: str, event_id: str, through: Position | None = None
) -> ScheduledEvent | None:
    """One event as of ``through`` (``None``: it does not exist there)."""
    found = await events_at(conn, story_id, through, event_ids=[event_id])
    return found[0] if found else None


# -- the stop rules -------------------------------------------------------------------------------


def pending_between(
    found: Iterable[ScheduledEvent],
    from_offset: int,
    to_offset: int,
    present: Iterable[str],
    scene: str | None,
    *,
    exclude: Iterable[str] = (),
) -> list[ScheduledEvent]:
    """The ``pending`` events of ``found`` planned in ``(from_offset, to_offset]`` that involve
    the scene, by planned time (see the module docstring)."""
    held = set(present)
    skipped = set(exclude)
    return sorted(
        (
            e
            for e in found
            if e.status is S.PENDING
            and e.event_id not in skipped
            and from_offset < e.planned <= to_offset
            and e.involves(held, scene)
        ),
        key=lambda e: (e.planned, e.event_id),
    )


async def due_between(
    conn: AsyncConnection,
    story_id: str,
    from_offset: int,
    to_offset: int,
    present: Iterable[str],
    scene: str | None,
    *,
    through: Position | None = None,
) -> list[ScheduledEvent]:
    """The range read (see the module docstring): ``pending`` canon events only, so a
    belief-only schedule (a ``beliefs`` row, never an event) is never among them."""
    return pending_between(
        await events_at(conn, story_id, through), from_offset, to_offset, present, scene
    )


@dataclass(frozen=True, slots=True)
class AdvanceSpan:
    """An advance to check: from the clock offset it starts at to the one it would reach."""

    start: int
    end: int


@dataclass(frozen=True, slots=True)
class Proceed:
    """The advance goes on; ``overdue``: the events it crosses with stops off (surfaced as
    overdue)."""

    overdue: tuple[ScheduledEvent, ...] = ()


@dataclass(frozen=True, slots=True)
class Stop:
    """The advance stops at ``at_offset``, the planned time of ``event``."""

    event: ScheduledEvent
    at_offset: int


@dataclass(frozen=True, slots=True)
class Pause:
    """#197: the advance pauses at ``at_offset`` for anchor refinement (``possibly_due``)."""

    event: ScheduledEvent
    at_offset: int


Check = Proceed | Stop | Pause


def check_advance(
    advance: AdvanceSpan,
    present: Iterable[str],
    scene: str | None,
    *,
    events: Iterable[ScheduledEvent],
    stops: bool,
    exclude: Iterable[str] = (),
) -> Check:
    """Whether ``advance`` may go on (see the module docstring)."""
    crossed = pending_between(events, advance.start, advance.end, present, scene, exclude=exclude)
    if not crossed:
        return Proceed()
    if not stops:
        return Proceed(overdue=tuple(crossed))
    return Stop(crossed[0], crossed[0].planned)


async def stops_enabled(conn: AsyncConnection, story_id: str) -> bool:
    """Whether stops apply: the effective profile's ``clock.scheduled_stops`` (delivered) and
    the clock's ``stop_at_due_events`` are both on."""
    from epistrel.core import world_clock  # here: the clock package imports this module
    from epistrel.core.profiles import schema
    from epistrel.core.profiles.effective import effective_configuration

    settings = (await effective_configuration(conn, story_id, validate=False)).settings
    level = schema.level_of(settings, "scheduled_stops")
    if level is None or not schema.is_delivered("scheduled_stops", level):
        return False
    return bool((await world_clock.settings(conn, story_id)).stop_at_due_events)


async def present_and_scene(
    conn: AsyncConnection, story_id: str, turn: int
) -> tuple[frozenset[str], str | None]:
    """Who is present as ``turn`` starts and the scene's location (#137, #161): the characters
    placed where the player character is; with no location known, every character (nothing
    places anyone elsewhere)."""
    from epistrel.core.epistemic.scene import load_scene_state  # here: the scene reads the clock

    scene = await load_scene_state(conn, story_id, turn)
    if scene.location is None:
        return frozenset(scene.characters), None
    return (
        frozenset(c for c, place in scene.placements.items() if place == scene.location),
        scene.location,
    )


# -- a turn's cut -------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Stopped:
    """A stop a turn takes: the ``event``, the offset it stops at, the render (``unit``) that
    reached it and whether that render crossed it in a skip (``skip``: regenerated under the
    cutoff) or its pacing (clamped)."""

    event: ScheduledEvent
    at: int
    unit: int
    skip: bool


@dataclass(frozen=True, slots=True)
class Cut:
    """What ``cut_turn`` decided: the turn's ``timing`` with every render capped at its stop,
    the ``stops``, the events crossed with stops off (``overdue``, each with the render that
    reached it) and the uncapped timing it started from (``uncapped``: where a regenerated
    render's original content lay)."""

    timing: TurnTiming
    stops: tuple[Stopped, ...] = ()
    overdue: tuple[tuple[ScheduledEvent, int], ...] = ()
    uncapped: TurnTiming | None = None
    also: tuple[int, ...] = ()
    """A simultaneous turn's other renders that skipped past its cutoff (each regenerated
    under it too; ``stops`` holds the event's one stop)."""

    @property
    def regenerate(self) -> dict[int, Stopped]:
        """The renders to regenerate under their cutoff, by render index."""
        found = {s.unit: s for s in self.stops if s.skip}
        for unit in self.also:
            found[unit] = replace(self.stops[0], unit=unit)
        return found

    @property
    def cutoff(self) -> int | None:
        """The earliest stop's offset (``None``: no stop)."""
        return min((s.at for s in self.stops), default=None)


def _skipped(timing: TurnTiming, unit: int) -> bool:
    """Whether render ``unit`` moved by an advance (a skip) rather than its pacing."""
    return unit < len(timing.skipping) and timing.skipping[unit]


def cut_turn(
    beats: Sequence[TimedBeat],
    steps: Mapping[int, Sequence[int]],
    mode: str,
    turn_start: int,
    owners: Mapping[int, int] | None = None,
    *,
    events: Sequence[ScheduledEvent],
    present: Iterable[str],
    scene: str | None,
    stops: bool,
) -> Cut:
    """The turn's stops and its capped timing (see the module docstring)."""
    held = frozenset(present)
    renders = list(beats)
    uncapped = time_turn(renders, steps, mode, turn_start, owners)
    if mode == SIMULTANEOUS:
        return _simultaneous(
            renders, steps, turn_start, owners, uncapped, events, held, scene, stops
        )
    taken: list[Stopped] = []
    overdue: list[tuple[ScheduledEvent, int]] = []
    seen: set[str] = set()
    timing = uncapped
    for _ in range(len(events) + 1):
        hit: tuple[int, ScheduledEvent] | None = None
        for n, times in enumerate(timing.beat_times):
            crossed = pending_between(events, times.start, times.end, held, scene, exclude=seen)
            if crossed:
                hit = (n, crossed[0])
                break
        if hit is None:
            break
        n, event = hit
        seen.add(event.event_id)
        if not stops:
            overdue.append((event, n))
            continue
        skip = _skipped(timing, n)
        renders[n] = replace(renders[n], cap=event.planned, stopped_by=event.event_id)
        taken.append(Stopped(event, event.planned, n, skip))
        timing = time_turn(renders, steps, mode, turn_start, owners)
    return Cut(timing, tuple(taken), tuple(overdue), uncapped)


def _simultaneous(
    renders: list[TimedBeat],
    steps: Mapping[int, Sequence[int]],
    turn_start: int,
    owners: Mapping[int, int] | None,
    uncapped: TurnTiming,
    events: Sequence[ScheduledEvent],
    present: frozenset[str],
    scene: str | None,
    stops: bool,
) -> Cut:
    reached: dict[str, tuple[ScheduledEvent, int]] = {}
    for n, times in enumerate(uncapped.beat_times):
        for event in pending_between(events, turn_start, times.end, present, scene):
            reached.setdefault(event.event_id, (event, n))
    if not reached:
        return Cut(uncapped, uncapped=uncapped)
    ordered = sorted(reached.values(), key=lambda found: (found[0].planned, found[0].event_id))
    if not stops:
        return Cut(uncapped, overdue=tuple(ordered), uncapped=uncapped)
    event = ordered[0][0]
    cutoff = event.planned
    skipped = False
    for n, times in enumerate(uncapped.beat_times):
        if times.end > cutoff:
            skipped = skipped or _skipped(uncapped, n)
            renders[n] = replace(renders[n], cap=cutoff, stopped_by=event.event_id)
    timing = time_turn(renders, steps, SIMULTANEOUS, turn_start, owners)
    if timing.advances and all(a.stopped_by is None for a in timing.advances):
        last = timing.advances[-1]
        timing = replace(
            timing, advances=(*timing.advances[:-1], replace(last, stopped_by=event.event_id))
        )
    regenerated = [
        n
        for n, times in enumerate(uncapped.beat_times)
        if times.end > cutoff and _skipped(uncapped, n)
    ]
    if regenerated:
        # The event's one stop, at the first render that skipped past it; the others too.
        stop = Stopped(event, cutoff, regenerated[0], True)
        return Cut(timing, (stop,), uncapped=uncapped, also=tuple(regenerated[1:]))
    unit = max(range(len(renders)), key=lambda n: (uncapped.beat_times[n].end, -n))
    return Cut(timing, (Stopped(event, cutoff, unit, skipped),), uncapped=uncapped)


# -- the lifecycle (writes) ------------------------------------------------------------------------


async def _append_event(
    conn: AsyncConnection,
    op: FactOperation,
    story_id: str,
    payload: ScheduledEventChanged,
    *,
    slot_k: int,
    beat: int,
    step: int,
) -> str:
    """Append the step's ``scheduled_event`` event to ``op`` in its commit (a commit step: the
    story is locked), after the operation's own events."""
    body = validate(EVENT_TYPE, payload).model_dump(mode="json")
    last = await conn.scalar(
        select(func.coalesce(func.max(events.c.seq), 0)).where(events.c.story_id == story_id)
    )
    event_id = new_ulid()
    provenance = getattr(op, "provenance", None)
    await conn.execute(
        insert(events).values(
            id=event_id,
            story_id=story_id,
            seq=int(last or 0) + 1,
            operation_id=op.operation_id,
            category=EventCategory.DERIVED.value,
            narrative_turn=op.narrative_turn,
            turn_variant=op.turn_variant,
            beat=beat,
            step=step,
            slot_k=slot_k,
            principal=op.principal,
            type=EVENT_TYPE,
            payload=body,
            provenance=(
                provenance if isinstance(provenance, Provenance) else Provenance.ENGINE
            ).value,
            authority=Authority.AUTHORITATIVE.value,
        )
    )
    await index_records(
        conn,
        [
            event_record(
                _Row({"id": event_id, "story_id": story_id, "character_id": None, "payload": body})
            )
        ],
    )
    return event_id


class _Row:
    """A row mapping read as attributes (what the erasure record builders take)."""

    def __init__(self, values: Mapping[str, Any]) -> None:
        self.__dict__.update(values)


@dataclass(frozen=True, slots=True)
class Recorded:
    """A lifecycle step written: the event's new version and its ``scheduled_event`` event."""

    event: ScheduledEvent
    record_id: str


async def record(
    conn: AsyncConnection,
    op: FactOperation,
    story_id: str,
    event: ScheduledEvent,
    status: ScheduledStatus,
    *,
    position: Position,
    cause: str,
    stopped_clock_at: int | None = None,
    planned: int | None = None,
    refinement_pause_anchor_rev: int | None = None,
    shared_owners: Sequence[str] | None = None,
) -> Recorded:
    """Write one lifecycle step of ``event`` in ``op``'s commit (see the module docstring): its
    ``scheduled_event`` event at ``position`` and the version row it leaves (the plan carried
    over; ``planned`` a reschedule's new time; ``stopped_clock_at`` a stop's offset, kept by
    every later version until a reschedule clears it; ``shared_owners`` a new sharing, #198)."""
    if cause not in CAUSES:
        raise ValueError(f"{cause!r} is not a scheduled event cause")
    status = ScheduledStatus(status)
    rescheduled = cause == RESCHEDULED
    stopped = None if rescheduled else stopped_clock_at or event.stopped_clock_at
    planned_at = event.planned if planned is None else int(planned)
    if shared_owners is not None:
        event = replace(event, shared_owners=tuple(dict.fromkeys(shared_owners)))
    await guard.check_content(
        conn,
        story_id,
        [
            guard.Content(
                (),
                frozenset(
                    {*event.participants, *event.shared_owners, *filter(None, [event.place_id])}
                ),
            )
        ],
        base_revision=getattr(op, "base_revision", None),
        path=__name__,
    )
    record_id = await _append_event(
        conn,
        op,
        story_id,
        ScheduledEventChanged(
            event_id=event.event_id,
            status=status.value,
            planned=planned_at,
            cause=cause,
            stopped_at=stopped_clock_at,
        ),
        slot_k=position.slot_k,
        beat=position.beat,
        step=position.step,
    )
    version_id = new_ulid()
    row = {
        "id": version_id,
        "story_id": story_id,
        "proposition_id": event.event_id,
        "planned_offset_minutes": planned_at,
        "planned_anchor": None if event.planned_anchor is None else dict(event.planned_anchor),
        "status": status.value,
        "owner": event.owner,
        "shared_owners": list(event.shared_owners),
        "participants": list(event.participants),
        "place_id": event.place_id,
        "grace_minutes": event.grace,
        "refinement_pause_anchor_rev": refinement_pause_anchor_rev,
        "stopped_clock_at": stopped,
        "operation_id": op.operation_id,
        "source_event_ids": [record_id],
        "narrative_turn": position.turn,
        "turn_variant": position.variant,
        "slot_k": position.slot_k,
        "beat": position.beat,
        "step": position.step,
    }
    await insert_derived(conn, scheduled_events, [row])
    await index_records(conn, [scheduled_event_record(_Row(row))])
    written = replace(
        event,
        status=status,
        planned=planned_at,
        version_id=version_id,
        position=position,
        stopped_clock_at=stopped,
        refinement_pause_anchor_rev=refinement_pause_anchor_rev,
        operation_id=op.operation_id,
    )
    return Recorded(written, record_id)


async def create(
    conn: AsyncConnection,
    op: FactOperation,
    story_id: str,
    *,
    proposition_id: str,
    planned: int,
    owner: str,
    participants: Sequence[str] = (),
    place_id: str | None = None,
    grace: int | None = None,
    position: Position,
    planned_anchor: Mapping[str, Any] | None = None,
    shared_owners: Sequence[str] = (),
) -> Recorded:
    """A new canon event, ``pending`` at ``position`` (its ``scheduled`` proposition already
    written; ``grace`` defaults to the clock's ``grace_minutes``; ``shared_owners``, #198)."""
    from epistrel.core import world_clock  # here: the clock package imports this module

    if grace is None:
        grace = int((await world_clock.settings(conn, story_id)).grace_minutes)
    event = ScheduledEvent(
        event_id=proposition_id,
        status=S.PENDING,
        planned=int(planned),
        owner=owner,
        participants=tuple(dict.fromkeys(participants)),
        place_id=place_id,
        grace=int(grace),
        planned_anchor=planned_anchor,
        shared_owners=tuple(dict.fromkeys(shared_owners)),
    )
    return await record(conn, op, story_id, event, S.PENDING, position=position, cause=CREATED)


_SHARING: ContextVar[tuple[str, ...]] = ContextVar("scheduled_sharing", default=())


@contextmanager
def sharing(shared_owners: Sequence[str]) -> Iterator[None]:
    """#198: the canon ``scheduled`` facts committed inside are created shared with
    ``shared_owners`` (the API's ``POST``; ``on_canon_fact`` reads it)."""
    token = _SHARING.set(tuple(dict.fromkeys(shared_owners)))
    try:
        yield
    finally:
        _SHARING.reset(token)


def owner_of(provenance: RecordProvenance, subject_is_character: bool, subject_id: str) -> str:
    """A canon event's owner from the fact's provenance (FR-CLOCK-19): the author's and the
    scenario's are the author's, a character's own (a render or the player asserting about
    themselves) theirs, anything else the world's."""
    if provenance in (RecordProvenance.AUTHOR, RecordProvenance.SEED):
        return OWNER_AUTHOR
    if provenance in (RecordProvenance.RENDER, RecordProvenance.PLAYER) and subject_is_character:
        return subject_id
    return OWNER_WORLD


async def on_canon_fact(
    conn: AsyncConnection,
    story_id: str,
    fact: Fact,
    operation: FactOperation,
    *,
    beat: int | None,
    slot_k: int | None,
) -> Recorded | None:
    """``commit_fact``'s hook: a new affirmed canon ``scheduled`` fact with a planned time is
    an event, ``pending`` at the fact's position (participants: its subject when the subject is
    a character, and its ``participants`` qualifier's characters; ``place`` from its
    qualifier). A later version of an existing fact, a negated one, a closed set's or a fact
    before any turn is not."""
    from epistrel.core import world_clock  # here: the clock package imports this module
    from epistrel.core.canon.facts import characters_among
    from epistrel.core.propositions.vocabulary import SCHEDULED

    proposition = fact.proposition
    if (
        fact.predicate_id != SCHEDULED
        or proposition.polarity.value != "affirmed"
        or fact.lineage_id != fact.id
        or fact.valid_from is None
        or operation.narrative_turn is None
    ):
        return None
    minute_zero = (await world_clock.time_basis(conn, story_id)).minute_zero
    planned = max(0, int(fact.valid_from) - int(minute_zero))
    raw = proposition.qualifiers.get("participants") or ()
    participants = [str(p) for p in raw] if isinstance(raw, list | tuple) else []
    subject = str(fact.subject_id)
    characters = await characters_among(conn, story_id, [*participants, subject])
    participants = [p for p in participants if p in characters]
    if subject in characters and subject not in participants:
        # #198: the subject owns the plan, so it takes part in it (first).
        participants = [subject, *participants]
    place = proposition.qualifiers.get("place")
    position = Position(
        int(operation.narrative_turn),
        int(operation.turn_variant or 0),
        int(slot_k or 0),
        int(beat or 0),
        0,
    )
    if beat is not None:
        from epistrel.core.membership import step_of

        position = replace(position, step=step_of(beat, operation.operation_id))
    return await create(
        conn,
        operation,
        story_id,
        proposition_id=fact.proposition_id,
        planned=planned,
        owner=owner_of(fact.provenance, subject in characters, subject),
        participants=participants,
        place_id=str(place) if isinstance(place, str) else None,
        position=position,
        shared_owners=_SHARING.get(),
    )


async def observe_missed(
    conn: AsyncConnection,
    op: Any,
    story_id: str,
    event: ScheduledEvent,
    *,
    record_id: str,
    at: Position,
    present: Iterable[str],
    subject_id: str | None,
) -> int:
    """The involved characters present when ``event`` was missed observe it: an
    ``observation`` transition of ``scheduled_status(event) = past`` citing the
    ``scheduled_event`` event ``record_id`` (at ``at``) as its ``record`` antecedent, at the step
    after it (ADR-168), under per-character partitions. Answers how many observed."""
    from epistrel.core.epistemic import transitions
    from epistrel.core.epistemic.scene import PARTITIONS_MODULE
    from epistrel.core.profiles.capabilities import module_on
    from epistrel.core.profiles.effective import effective_configuration
    from epistrel.core.propositions.model import (
        Granularity,
        Instant,
        LiteralKind,
        Proposition,
        TypedLiteral,
    )
    from epistrel.core.propositions.vocabulary import SCHEDULED_STATUS

    holders = sorted(set(event.participants) & set(present))
    if not holders or subject_id is None:
        return 0
    settings = (await effective_configuration(conn, story_id, validate=False)).settings
    if not module_on(settings, PARTITIONS_MODULE):
        return 0
    from epistrel.core import world_clock

    basis = await world_clock.time_basis(conn, story_id)
    after = replace(at, step=min(POSITION_MAX, at.step + 1))
    proposition = Proposition(
        subject_id=subject_id,
        predicate_id=SCHEDULED_STATUS,
        literal=TypedLiteral(kind=LiteralKind.STR, value="past"),
        valid_time=Instant.of(basis.at(after), Granularity.MINUTE),
    )
    for holder in holders:
        await transitions.transition(
            conn,
            op,
            holder=holder,
            kind=transitions.Kind.OBSERVATION,
            proposition=proposition,
            antecedent=transitions.Antecedent.record("events", record_id),
            position=after,
            settings=settings,
        )
    return len(holders)


__all__ = [
    "CAUSES",
    "EVENT_TYPE",
    "OPEN",
    "SETTLED",
    "SHARED",
    "AdvanceSpan",
    "Check",
    "Cut",
    "Pause",
    "Proceed",
    "Recorded",
    "ScheduledEvent",
    "Stop",
    "Stopped",
    "check_advance",
    "create",
    "cut_turn",
    "due_between",
    "event_at",
    "events_at",
    "observe_missed",
    "on_canon_fact",
    "owner_of",
    "pending_between",
    "present_and_scene",
    "record",
    "sharing",
    "stops_enabled",
]
