"""A structured turn's due-event stops (FR-CLOCK-9,16,17; #196).

The turn reads its schedule once with its clock (``read_schedule``: the story's canon scheduled
events as the turn starts, whether stops apply, who is present and where the scene is). Every
advance the turn's renders make, its pacing increments included, goes through
``core.world_clock.stops.cut_turn`` before anything commits:

- **In a sequential turn** each primary's render is checked as soon as it is verified, before
  it is staged (``stops_of`` over the beats so far), so a later primary renders only after the
  cutoff: a stop inside a **skip** marks the render's first beat past the cutoff for repair
  (``time_stop``, a ``core.states.effects.Finding`` the repair ladder regenerates under "time
  stops at …", #151) and drops the render's later beats past it; a stop inside **pacing** clamps
  the render's end and surfaces the event to the later renders (``due_lines``).
- **In a simultaneous turn** the check runs once after reconciliation (#177), over all the
  renders and the scheduled events the turn's own beats create (``created_events``).
- A beat regenerated under a cutoff reaches it exactly (``reach``: its advance becomes the
  engine's target, the cutoff), and nothing the original placed after the cutoff is committed:
  the dropped beats and the rejected text never reach the commit.

``lifecycle`` turns the final cut into the turn's lifecycle steps (``Planned``), which the
commit writes after the turn's canon (``record``): the stops (``due``, and ``surfaced`` when a
render of the turn presents the event), the events crossed with stops off (surfaced as
overdue), a previous turn's ``due`` events surfaced to this one, and the grace and completion
rules of ``core.world_clock.stops``. Log lines carry counts only (invariant 3).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from epistrel.core import world_clock
from epistrel.core.canon.canonize import CanonRequest
from epistrel.core.enums import ScheduledStatus
from epistrel.core.membership import POSITION_MAX, Position
from epistrel.core.propositions.model import Instant, Interval
from epistrel.core.propositions.vocabulary import SCHEDULED
from epistrel.core.states.effects import TIME_STOP, Finding
from epistrel.core.states.model import WORLD_CLOCK
from epistrel.core.tables import entities
from epistrel.core.tables.propositions import propositions
from epistrel.core.tables.scheduled_events import OWNER_WORLD
from epistrel.core.turns.beats import DeclaredAdvance, TurnBeat
from epistrel.core.world_clock import stops
from epistrel.core.world_clock.stops import Cut, ScheduledEvent
from epistrel.core.world_clock.timing import TurnTiming
from epistrel.orchestrator.structured.clocking import (
    PLAYER_BEAT,
    ClockRead,
    Timed,
    timed_renders,
)

S = ScheduledStatus
CREATED_PREFIX = "new:"
"""A scheduled event the turn itself creates has no row before its commit: its key until then."""
STOP_REASON = "time stops at a scheduled event"


@dataclass(frozen=True, slots=True)
class Schedule:
    """The turn's scheduled events as it starts (see the module docstring); ``names``: each
    event's name (its canon proposition's event), for the renders it is surfaced to."""

    events: tuple[ScheduledEvent, ...]
    stops: bool
    present: frozenset[str]
    scene: str | None
    names: Mapping[str, str] = field(default_factory=dict)
    minute_zero: int = 0
    grace: int = 30
    """The clock's ``grace_minutes`` (an event the turn creates takes it)."""

    @property
    def due(self) -> tuple[ScheduledEvent, ...]:
        """A previous turn's stops, surfaced to this one."""
        return tuple(e for e in self.events if e.status is S.DUE)


async def read_schedule(conn: AsyncConnection, story_id: str, turn: int) -> Schedule:
    """The schedule as ``turn`` starts."""
    found = tuple(await stops.events_at(conn, story_id))
    present, scene = await stops.present_and_scene(conn, story_id, turn)
    names: dict[str, str] = {}
    if found:
        p, e = propositions.c, entities.c
        rows = await conn.execute(
            select(p.id, e.canonical_name)
            .join(entities, entities.c.id == p.object_id)
            .where(p.id.in_([x.event_id for x in found]))
        )
        names = {str(r.id): str(r.canonical_name) for r in rows}
    settings = await world_clock.settings(conn, story_id)
    return Schedule(
        events=found,
        stops=await stops.stops_enabled(conn, story_id),
        present=present,
        scene=scene,
        names=names,
        minute_zero=(await world_clock.time_basis(conn, story_id)).minute_zero,
        grace=int(settings.grace_minutes),
    )


def _minute(valid_time: Interval | Instant | None) -> int | None:
    if isinstance(valid_time, Instant):
        return int(valid_time.at)
    if isinstance(valid_time, Interval) and valid_time.start is not None:
        return int(valid_time.start.inner)
    return None


def created_events(
    requests: Sequence[CanonRequest], minute_zero: int, characters: Iterable[str], grace: int
) -> list[ScheduledEvent]:
    """The scheduled events the turn's beats create (its permitted ``scheduled`` canon
    requests), keyed ``new:<claim id>`` until the commit writes them (FR-CLOCK-17: a
    simultaneous turn's cut counts them)."""
    cast = set(characters)
    found: list[ScheduledEvent] = []
    for n, request in enumerate(requests):
        claim = request.claim
        minute = _minute(claim.valid_time)
        if claim.predicate_id != SCHEDULED or minute is None or claim.polarity.value != "affirmed":
            continue
        raw = claim.qualifiers.get("participants") or ()
        participants = (
            [str(p) for p in raw if str(p) in cast] if isinstance(raw, list | tuple) else []
        )
        if not participants and str(claim.subject_id) in cast:
            participants = [str(claim.subject_id)]
        place = claim.qualifiers.get("place")
        found.append(
            ScheduledEvent(
                event_id=f"{CREATED_PREFIX}{request.claim_id or n}",
                status=S.PENDING,
                planned=max(0, minute - minute_zero),
                owner=OWNER_WORLD,
                participants=tuple(participants),
                place_id=str(place) if isinstance(place, str) else None,
                grace=grace,
                position=Position(0, 0, 0, int(request.beat or 0)),
            )
        )
    return found


def cut(
    clock: ClockRead,
    schedule: Schedule,
    beats: Sequence[TurnBeat],
    *,
    say: str = "",
    do: str = "",
    player_last: int = PLAYER_BEAT,
    steps: Mapping[int, Sequence[int]] | None = None,
    mode: str,
    created: Sequence[ScheduledEvent] = (),
) -> tuple[Cut, Timed]:
    """The turn's cut over ``beats`` (see the module docstring)."""
    timed = timed_renders(clock, beats, say=say, do=do, player_last=player_last, steps=steps)
    found = stops.cut_turn(
        timed.beats,
        timed.steps,
        mode,
        clock.start,
        timed.owners,
        events=[*schedule.events, *created],
        present=schedule.present,
        scene=schedule.scene,
        stops=schedule.stops,
    )
    return found, timed


@dataclass(frozen=True, slots=True)
class StopMark:
    """A beat to regenerate under a cutoff: its number, its step, the cutoff and the event."""

    beat: int
    step: int
    cutoff: int
    event_id: str


def _start(timing: TurnTiming, beat: TurnBeat) -> int:
    return timing.at(beat.beat, int(beat.step or 0))


def marks_of(found: Cut, timed: Timed) -> tuple[list[StopMark], set[int]]:
    """For each render a stop regenerates: the first of its beats past the cutoff (else the one
    declaring the crossing advance) to regenerate, and the rest of its beats past the cutoff to
    drop. The player's render is never regenerated."""
    marks: list[StopMark] = []
    drops: set[int] = set()
    uncapped = found.uncapped or found.timing
    for unit, stopped in sorted(found.regenerate.items()):
        if unit == 0 or unit >= len(timed.renders):
            continue
        render = [b for b in timed.renders[unit] if b.beat > PLAYER_BEAT]
        past = [b for b in render if _start(uncapped, b) > stopped.at]
        if not past:
            past = [b for b in render if b.advances][:1] or render[-1:]
        if not past:
            continue
        first = past[0]
        marks.append(StopMark(first.beat, int(first.step or 0), stopped.at, stopped.event.event_id))
        drops.update(b.beat for b in past[1:])
    return marks, drops


def finding_of(mark: StopMark, line: str) -> Finding:
    """The repair ladder's ``time_stop`` finding for ``mark`` (``line``: its constraint)."""
    return Finding(TIME_STOP, mark.beat, WORLD_CLOCK, line=line)


def stop_line(mark: StopMark, clock: ClockRead | None, schedule: Schedule) -> str:
    """The constraint a beat is regenerated under (FR-CLOCK-16)."""
    name = schedule.names.get(mark.event_id, "a scheduled event")
    return (
        f"Time stops at {when(mark.cutoff, clock)}: {name} is due then. End this beat at or "
        "before that moment, with the interruption; narrate nothing after it."
    )


def when(offset: int, clock: ClockRead | None) -> str:
    """An offset as a reader sees it (the anchor's time of day when it fixes one)."""
    if clock is not None and clock.minute is not None:
        minute = (clock.minute + offset - clock.start) % clock.clockwork.day_minutes
        return f"{minute // 60:02d}:{minute % 60:02d}"
    return world_clock.display(offset)


def reach(beat: TurnBeat, mark: StopMark) -> TurnBeat:
    """``beat``, regenerated under ``mark``, reaching the cutoff exactly: its advances become
    the engine's target (FR-CLOCK-16: the stopped advance ends at the event)."""
    return replace(
        beat,
        advances=(DeclaredAdvance(STOP_REASON, step=int(beat.step or 0), to_offset=mark.cutoff),),
    )


def due_lines(
    found: Iterable[ScheduledEvent],
    schedule: Schedule,
    clock: ClockRead | None,
    *,
    character_id: str | None,
) -> tuple[str, ...]:
    """The renders' lines for the events surfaced to them: every one for the narrator
    (``character_id`` ``None``), a character's own (a participant's) for a character."""
    lines = []
    for event in found:
        if character_id is not None and character_id not in event.participants:
            continue
        name = schedule.names.get(event.event_id, "A scheduled event")
        lines.append(
            f"Scheduled event now due: {name} (planned for {when(event.planned, clock)}). It "
            "is due at this moment; let it interrupt what is happening."
        )
    return tuple(lines)


@dataclass(frozen=True, slots=True)
class Planned:
    """One lifecycle step the turn's commit writes: the event (a ``new:`` key for one the turn
    creates), the status it moves to, why, where (``beat``, ``step``) and a stop's offset."""

    event: ScheduledEvent
    status: ScheduledStatus
    cause: str
    beat: int
    step: int
    stopped_at: int | None = None


def _advance_position(found: Cut, event_id: str, timed: Timed, unit: int) -> tuple[int, int]:
    for advance in found.timing.advances:
        if advance.stopped_by == event_id:
            return advance.beat_position, advance.step_position
    last = timed.beats[unit].last_beat if unit < len(timed.beats) else PLAYER_BEAT
    return last, POSITION_MAX


def lifecycle(schedule: Schedule, found: Cut, timed: Timed, *, sequential: bool) -> list[Planned]:
    """The turn's lifecycle steps (see the module docstring), in the order they happen."""
    planned: list[Planned] = []
    left: set[str] = set()
    for event in schedule.events:
        if event.status not in (S.DUE, S.SURFACED):
            continue
        if event.participants and not set(event.participants) & schedule.present:
            planned.append(Planned(event, S.COMPLETED, stops.LEFT, PLAYER_BEAT, 0))
            left.add(event.event_id)
        elif event.status is S.DUE:
            planned.append(Planned(event, S.SURFACED, stops.SURFACED_CAUSE, PLAYER_BEAT, 0))
    for stopped in found.stops:
        beat, step = _advance_position(found, stopped.event.event_id, timed, stopped.unit)
        planned.append(Planned(stopped.event, S.DUE, stops.STOP, beat, step, stopped.at))
        later = stopped.unit + 1 < len(timed.beats)
        if not sequential:
            # A simultaneous turn's renders do not see one another: the next turn surfaces it.
            continue
        if stopped.skip:
            # The render regenerated under the cutoff narrates the interruption.
            planned.append(Planned(stopped.event, S.SURFACED, stops.SURFACED_CAUSE, beat, step))
        elif later:
            first = timed.beats[stopped.unit + 1].first_beat
            planned.append(Planned(stopped.event, S.SURFACED, stops.SURFACED_CAUSE, first, 0))
    for event, unit in found.overdue:
        beat, step = _advance_position(found, event.event_id, timed, unit)
        planned.append(Planned(event, S.DUE, stops.OVERDUE, beat, step))
        planned.append(Planned(event, S.SURFACED, stops.OVERDUE, beat, step))
    end = found.timing.turn_end
    last = max([PLAYER_BEAT, *(b.last_beat for b in timed.beats)])
    for event in schedule.events:
        if (
            event.status in (S.DUE, S.SURFACED)
            and event.event_id not in left
            and end > event.planned + event.grace
        ):
            planned.append(Planned(event, S.MISSED, stops.GRACE, last, POSITION_MAX - 1))
    return planned


async def record(
    conn: AsyncConnection,
    op: Any,
    story_id: str,
    planned: Sequence[Planned],
    *,
    present: Iterable[str],
) -> int:
    """Write the turn's lifecycle steps in its commit, after its canon (an event the turn
    created is found by the commit's own row of it: its planned time and participants), and
    the observations of a missed event (``stops.observe_missed``). Answers the steps written."""
    turn, variant = int(op.narrative_turn), int(op.turn_variant or 0)
    created: dict[str, str] = {}
    written = 0
    for step in planned:
        key = step.event.event_id
        if key.startswith(CREATED_PREFIX):
            if key not in created:
                mine = [
                    e
                    for e in await stops.events_at(conn, story_id)
                    if e.operation_id == op.operation_id
                    and e.planned == step.event.planned
                    and set(e.participants) == set(step.event.participants)
                    and e.event_id not in created.values()
                ]
                if not mine:
                    continue
                created[key] = mine[0].event_id
            key = created[key]
        current = await stops.event_at(conn, story_id, key)
        if current is None:
            continue
        position = Position(turn, variant, 0, step.beat, step.step)
        done = await stops.record(
            conn,
            op,
            story_id,
            current,
            step.status,
            position=position,
            cause=step.cause,
            stopped_clock_at=step.stopped_at,
        )
        written += 1
        if step.status is S.MISSED:
            subject = await conn.scalar(
                select(propositions.c.object_id).where(propositions.c.id == key)
            )
            await stops.observe_missed(
                conn,
                op,
                story_id,
                done.event,
                record_id=done.record_id,
                at=position,
                present=present,
                subject_id=None if subject is None else str(subject),
            )
    return written


def report(found: Cut | None, planned: Sequence[Planned]) -> dict[str, Any] | None:
    """``turn_report.clock.stops``: the stops and lifecycle steps (offsets, statuses, counts;
    never a name)."""
    if found is None:
        return None
    return {
        "cutoff": found.cutoff,
        "stops": [{"at": s.at, "unit": s.unit, "regenerated": s.skip} for s in found.stops],
        "overdue": len(found.overdue),
        "lifecycle": [
            {"status": p.status.value, "cause": p.cause, "beat": p.beat} for p in planned
        ],
    }


__all__ = [
    "CREATED_PREFIX",
    "STOP_REASON",
    "Planned",
    "Schedule",
    "StopMark",
    "created_events",
    "cut",
    "due_lines",
    "finding_of",
    "lifecycle",
    "marks_of",
    "reach",
    "read_schedule",
    "record",
    "report",
    "stop_line",
    "when",
]
