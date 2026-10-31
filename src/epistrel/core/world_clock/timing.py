"""Start and end times within a turn (FR-CLOCK-6,17; docs/02 §7.1; #194).

FR-CLOCK-17's **beat** is a render (``TimedBeat``: one character's or the narrator's lines in
one turn, beats ``first_beat..last_beat``; the player's input is a render of its own), and its
**steps** are the render's declared steps (#168's ``render.StepDecl`` indices, ``time`` steps
included; a render that declares none is one step, step 0). ``time_turn(beats, steps, mode,
turn_start)`` derives, with no I/O and no model call:

- **Starts.** In ``sequential`` mode a render starts at the previous render's end (the first at
  ``turn_start``); in ``simultaneous`` mode every render starts at ``turn_start``. A render's
  step 0 starts at its start and its declared steps follow in index order, each where the
  previous one left off.
- **Advances** (``TimedAdvance``) attach to a step boundary: a declared skip (``minutes``:
  "twenty minutes later she arrives") or a target (``to_offset``: a declared ``time_advance.to``
  or an explicit narrated time, "at 15:10 she starts driving" when the clock reads 15:05). Each
  moves the start of its step and of every later step of the render. A target earlier than
  where the render stands is **contradicted** (``TurnTiming.contradicted``) and moves nothing,
  unless it is a ``flashback``'s: a flashback's times are never checked against the present and
  move nothing.
- **Ends.** A render with an accepted advance ends where its last step starts (its start plus
  its in-render advances: the advances **replace** its pacing increment, FR-CLOCK-7); one with
  none ends at its start plus its pacing increment (``core.world_clock.pacing``), applied at its
  end. The turn ends at the last render's end (``sequential``) or the latest end
  (``simultaneous``): the clock value the turn commits.
- **The recorded advances** (``TurnTiming.advances``, one per accepted advance and per non-zero
  pacing increment, ``source = declared_skip | pacing``) and where each sits: in ``sequential``
  mode a step's advance just before the step (``(first_beat, step - 1)``; a step-0 advance after
  the previous beat), a pacing increment at the end of its render (``(last_beat,
  POSITION_MAX)``), so the clock's fold at a step's own position is its start; in
  ``simultaneous`` mode only the advances of the render that ends latest are recorded, all at the
  turn's end, so no render's records see another's time and the clock ends at the latest end.
  ``derive(recorded, layout, mode, turn_start)`` rebuilds the timing from recorded advances
  (CLOCK-MONOTONIC's timing clause).

Every record a step writes takes its step's start (``TurnTiming.times``: the
``core.resolution.time.TurnTimes`` the commit's time basis carries, ``world_clock.turn_scope``).

**Times of day** (#195's anchor): ``minute_of_day(anchor, offset)`` is the minute of the day the
anchor fixes at a clock offset (``None`` unless it fixes the exact minute: the engine never
picks one); ``narrated_minutes(text)`` the explicit times a line narrates ("at 15:10", "at 3:10
pm"; one reading only: an ambiguous "at 9:15" states nothing here); ``narrated_offset`` places
one on the clock relative to an offset whose minute is known, on the same day, so a time earlier
than the clock reads is behind it (contradicted unless a flashback's); ``next_offset`` is a
declared ``to``'s target, the next such time at or after the offset (#193's consumer rule).
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from epistrel.core.enums import StateChangeSource
from epistrel.core.membership import POSITION_MAX
from epistrel.core.propositions.model import Granularity, Instant, Interval
from epistrel.core.resolution.time import TurnTimes
from epistrel.core.world_clock.anchor import Anchor, Clockwork, statement_of

SEQUENTIAL, SIMULTANEOUS = "sequential", "simultaneous"
_NARRATED = re.compile(
    r"\bat\s+(\d{1,2}:\d{2}(?:\s*[ap]\.?\s?m\.?)?|noon|midnight)(?![\w:])", re.IGNORECASE
)
"""An explicit narrated time: "at 15:10", "at 3:10 pm", "at noon"."""


def minute_of_day(anchor: Anchor | None, offset: int) -> int | None:
    """The minute of the day the anchor fixes at ``offset`` (``None``: not exactly fixed)."""
    if anchor is None or anchor.is_unanchored:
        return None
    found = anchor.absolute_at(offset).segments
    if len(found) != 1 or found[0].hi - found[0].lo != 1:
        return None
    return int(found[0].lo)


def narrated_minutes(text: str, clockwork: Clockwork | None = None) -> list[int]:
    """The minutes of the day ``text`` narrates as explicit times, each with one reading."""
    found: list[int] = []
    for match in _NARRATED.finditer(text):
        stated = (
            statement_of(match.group(1))
            if clockwork is None
            else statement_of(match.group(1), clockwork)
        )
        if stated is None or stated.approximate or stated.time is None or len(stated.time) != 1:
            continue
        lo, hi = stated.time[0]
        if hi - lo == 1:
            found.append(int(lo))
    return found


def narrated_offset(minute: int, *, at_offset: int, at_minute: int) -> int:
    """The clock offset of ``minute`` on the day of ``at_offset`` (whose minute is
    ``at_minute``): behind ``at_offset`` when the time is earlier."""
    return at_offset + (minute - at_minute)


def next_offset(minute: int, *, at_offset: int, at_minute: int, day_minutes: int = 1440) -> int:
    """The clock offset of the next ``minute`` at or after ``at_offset``."""
    return at_offset + (minute - at_minute) % day_minutes


@dataclass(frozen=True, slots=True)
class TimedAdvance:
    """One advance a render attaches to its step ``step``: ``minutes`` (a declared duration) or
    ``to_offset`` (a target clock offset: a declared ``time_advance.to`` or a narrated explicit
    time), with its ``reason``; ``narrated`` when the text stated it, ``flashback`` when it is a
    flashback's (unchecked, and it moves nothing)."""

    step: int = 0
    minutes: int | None = None
    to_offset: int | None = None
    reason: str = ""
    narrated: bool = False
    flashback: bool = False


@dataclass(frozen=True, slots=True)
class TimedBeat:
    """One render as timing reads it: its beats ``first_beat..last_beat``, its pacing increment
    and the advances it attaches to its steps."""

    first_beat: int
    last_beat: int
    pacing: int = 0
    advances: tuple[TimedAdvance, ...] = ()


@dataclass(frozen=True, slots=True)
class BeatTime:
    """A render's start and end (clock offsets)."""

    start: int
    end: int


@dataclass(frozen=True, slots=True)
class Advance:
    """One advance the turn records: its minutes, source, the render (``unit``) and step it
    belongs to (``step`` ``None``: the render's pacing increment), its reason and the in-turn
    position ``(beat, step)`` it is written at."""

    minutes: int
    source: StateChangeSource
    unit: int
    step: int | None
    reason: str
    beat_position: int
    step_position: int

    @property
    def position(self) -> tuple[int, int]:
        return (self.beat_position, self.step_position)


@dataclass(frozen=True, slots=True)
class TurnTiming:
    """What ``time_turn`` derived (see the module docstring)."""

    mode: str
    turn_start: int
    beat_times: tuple[BeatTime, ...]
    step_starts: tuple[tuple[tuple[int, int], int], ...]
    """``((render index, step), start)`` for each render's step 0 and declared steps."""
    turn_end: int
    advances: tuple[Advance, ...]
    contradicted: tuple[tuple[int, TimedAdvance], ...] = ()
    """``(render index, advance)``: targets before where the render stood."""
    layout: tuple[tuple[int, int], ...] = field(default=())
    """Each render's ``(first_beat, last_beat)``."""
    owners: tuple[tuple[int, int], ...] = ()
    """Each beat's own step, where a step owns it (``(beat, step)``)."""

    @property
    def minutes(self) -> int:
        """How far the turn moves the clock."""
        return self.turn_end - self.turn_start

    def step_start(self, unit: int, step: int) -> int:
        """Render ``unit``'s step ``step``'s start (a step timing does not know: the latest
        known step's before it)."""
        known = [(s, start) for (u, s), start in self.step_starts if u == unit and s <= step]
        return max(known)[1] if known else self.beat_times[unit].start

    def at(self, beat: int, step: int) -> int:
        """The clock offset at which a record at ``(beat, step)`` of the turn starts."""
        return self.turn_start + self.times(0, 0).offset(beat, step)

    def next_start(self, beat: int, step: int) -> int | None:
        """The start of the step after ``step`` in the render holding ``beat`` (``None``: it
        is the render's last)."""
        for n, (first, last) in enumerate(self.layout):
            if first <= beat <= last:
                later = sorted((s, at) for (u, s), at in self.step_starts if u == n and s > step)
                return later[0][1] if later else None
        return None

    def times(self, turn: int, variant: int) -> TurnTimes:
        """The in-turn times a commit's time basis carries (relative to the turn's start)."""
        units = []
        for n, (first, last) in enumerate(self.layout):
            starts = tuple(
                (step, start - self.turn_start) for (u, step), start in self.step_starts if u == n
            )
            units.append((first, last, starts))
        return TurnTimes(turn, variant, tuple(units), self.turn_end - self.turn_start, self.owners)


def _steps_of(declared: Sequence[int]) -> list[int]:
    return [0, *sorted({int(s) for s in declared if int(s) > 0})]


def time_turn(
    beats: Sequence[TimedBeat],
    steps: Mapping[int, Sequence[int]],
    mode: str,
    turn_start: int,
    owners: Mapping[int, int] | None = None,
) -> TurnTiming:
    """The turn's beat and step times (see the module docstring). ``steps``: each render's
    declared step indices, by its ``first_beat`` (none: one step, step 0); ``owners``: each
    beat's own step, where a step owns it."""
    simultaneous = mode == SIMULTANEOUS
    beat_times: list[BeatTime] = []
    step_starts: list[tuple[tuple[int, int], int]] = []
    contradicted: list[tuple[int, TimedAdvance]] = []
    accepted: list[list[tuple[int, int, str]]] = []
    cursor = turn_start
    for n, beat in enumerate(beats):
        start = turn_start if simultaneous else cursor
        at = start
        mine: list[tuple[int, int, str]] = []
        for step in _steps_of([*steps.get(beat.first_beat, ()), *(a.step for a in beat.advances)]):
            for advance in beat.advances:
                if advance.step != step or advance.flashback:
                    continue
                if advance.minutes is not None:
                    moved = int(advance.minutes)
                elif advance.to_offset is not None:
                    moved = int(advance.to_offset) - at
                else:
                    continue
                if moved < 0:
                    contradicted.append((n, advance))
                    continue
                at += moved
                mine.append((step, moved, advance.reason))
            step_starts.append(((n, step), at))
        end = at if mine else start + max(0, int(beat.pacing))
        beat_times.append(BeatTime(start, end))
        accepted.append(mine)
        cursor = end
    if not beats:
        turn_end = turn_start
    elif simultaneous:
        turn_end = max(t.end for t in beat_times)
    else:
        turn_end = beat_times[-1].end
    return TurnTiming(
        mode=SIMULTANEOUS if simultaneous else SEQUENTIAL,
        turn_start=turn_start,
        beat_times=tuple(beat_times),
        step_starts=tuple(step_starts),
        turn_end=turn_end,
        advances=_recorded(beats, beat_times, accepted, simultaneous=simultaneous),
        contradicted=tuple(contradicted),
        layout=tuple((b.first_beat, b.last_beat) for b in beats),
        owners=tuple(sorted((int(b), int(s)) for b, s in (owners or {}).items() if s)),
    )


def _step_position(beat: TimedBeat, step: int) -> tuple[int, int]:
    """Where a sequential render's advance on ``step`` is written: just before the step."""
    if step > 0:
        return (beat.first_beat, step - 1)
    if beat.first_beat > 0:
        return (beat.first_beat - 1, POSITION_MAX)
    return (0, 0)


def _recorded(
    beats: Sequence[TimedBeat],
    beat_times: Sequence[BeatTime],
    accepted: Sequence[Sequence[tuple[int, int, str]]],
    *,
    simultaneous: bool,
) -> tuple[Advance, ...]:
    def of(n: int, at: tuple[int, int] | None) -> list[Advance]:
        beat, found = beats[n], []
        for step, moved, reason in accepted[n]:
            where = at if at is not None else _step_position(beat, step)
            found.append(Advance(moved, StateChangeSource.DECLARED_SKIP, n, step, reason, *where))
        minutes = beat_times[n].end - beat_times[n].start
        if not accepted[n] and minutes > 0:
            where = at if at is not None else (beat.last_beat, POSITION_MAX)
            found.append(Advance(minutes, StateChangeSource.PACING, n, None, "pacing", *where))
        return found

    if not beats:
        return ()
    if not simultaneous:
        return tuple(a for n in range(len(beats)) for a in of(n, None))
    latest = max(range(len(beats)), key=lambda n: (beat_times[n].end, -n))
    end = (max(b.last_beat for b in beats), POSITION_MAX)
    return tuple(of(latest, end))


def restamp(
    valid_time: Interval | Instant,
    *,
    now: int,
    start: int,
    next_start: int | None = None,
    durative: bool = False,
) -> Interval | Instant:
    """A claim's valid-time, normalized against the turn's start (``now``, a story-minute: no
    in-turn time is known before the turn is timed), restamped at its step's ``start``: the
    default "now" of a claim that states no time (a minute ``Instant`` at ``now``, an
    open-ended ``Interval`` from ``now``) moves to the step's start, and a ``durative`` one
    (a fluent narrated by a step) holds until the next step's start when the render's next
    step starts later (FR-CLOCK-17: "she is driving" over [15:10, 15:30)). A stated time is
    kept as it was."""
    if isinstance(valid_time, Instant):
        if valid_time.at != now or valid_time.granularity is not Granularity.MINUTE:
            return valid_time
        return valid_time if start == now else Instant.of(start, Granularity.MINUTE)
    begins, ends = valid_time.start, valid_time.end
    if begins is None or begins.inner != now or begins.outer != now or ends is not None:
        return valid_time
    if durative and next_start is not None and next_start > start:
        return Interval.of(start, next_start)
    return valid_time if start == now else Interval.of(start, None)


def derive(
    recorded: Sequence[Advance],
    layout: Sequence[tuple[int, int]],
    steps: Mapping[int, Sequence[int]],
    mode: str,
    turn_start: int,
) -> TurnTiming:
    """The timing the recorded advances of a turn give (CLOCK-MONOTONIC): each render rebuilt
    from its recorded advances (a declared skip at its step, a pacing increment as its pacing)
    and timed again. In ``simultaneous`` mode only the latest render's advances are recorded,
    so the others take the turn's start and end there."""
    beats = []
    for n, (first, last) in enumerate(layout):
        mine = [a for a in recorded if a.unit == n]
        beats.append(
            TimedBeat(
                first,
                last,
                pacing=sum(a.minutes for a in mine if a.step is None),
                advances=tuple(
                    TimedAdvance(step=int(a.step), minutes=a.minutes, reason=a.reason)
                    for a in mine
                    if a.step is not None
                ),
            )
        )
    return time_turn(beats, steps, mode, turn_start)


__all__ = [
    "SEQUENTIAL",
    "SIMULTANEOUS",
    "Advance",
    "BeatTime",
    "TimedAdvance",
    "TimedBeat",
    "TurnTiming",
    "derive",
    "minute_of_day",
    "narrated_minutes",
    "narrated_offset",
    "next_offset",
    "restamp",
    "time_turn",
]
