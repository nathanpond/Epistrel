"""A structured turn's pacing and in-turn timing (FR-CLOCK-6,7,17, FR-TURN-11; #194).

The turn reads its clock once (``read_clock``: the clock's level, mode, pacing table and the
offset and anchor it starts at), and its render schemas take the clock's level and whether the
anchor fixes the time of day (a ``time_advance`` needs the clock; its ``to`` needs the minute).
After reconciliation, before the turn commits, ``timed_turn`` derives its timing
(``core.world_clock.timing.time_turn``):

- **Renders** (FR-CLOCK-17's beats): the player's input (beat 1, with the ``co_author``
  player's narrated beats after it, #180), then each run of consecutive beats of one render: a
  character's (its reactive narration included, #176's ``narrated_by``) or the narrator's.
- **Pacing** (``core.world_clock.pacing``): each render's spoken and acted words through the
  story's pacing table (0 in ``manual`` mode); a ``flashback`` beat counts nothing.
- **Steps**: each character render's declared steps (#168's, #194's ``time`` steps included).
- **Advances**: each beat's declared ``time_advance`` effects (``minutes``, or ``to`` placed at
  the next such time once the anchor fixes the minute) at the step they name (else the beat's
  own), and each explicit time a ``say``, ``do`` or ``narration`` narrates ("at 15:10") at the
  beat's step, placed on the clock's day when the anchor fixes the minute (a time the anchor
  cannot place moves nothing; #199 decides whether to estimate). A flashback beat's advances
  are unchecked and move nothing.

The commit's first step writes the timing's advances (``world_clock.record_turn``), and the
operation carries the timing (``Operation.turn_times``) so every record the turn writes takes
its step's start. No model call: pacing and timing are deterministic (U-20). Log lines carry
counts only (invariant 3).
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

from sqlalchemy.ext.asyncio import AsyncConnection

from epistrel.core import world_clock
from epistrel.core.enums import ClockMode
from epistrel.core.turns.beats import TurnBeat
from epistrel.core.world_clock.anchor import Anchor, Clockwork
from epistrel.core.world_clock.pacing import PACED_KINDS, PacingTable, pacing_increment
from epistrel.core.world_clock.timing import (
    SEQUENTIAL,
    TimedAdvance,
    TimedBeat,
    TurnTiming,
    minute_of_day,
    narrated_minutes,
    narrated_offset,
    next_offset,
    time_turn,
)

PLAYER_BEAT = 1
"""The player's input is beat 1 of a structured turn."""


@dataclass(frozen=True, slots=True)
class ClockRead:
    """The clock as a structured turn starts: its level, mode, pacing table, offset, anchor and
    the minute of the day the anchor fixes there (``None``: not fixed)."""

    level: str
    mode: ClockMode
    table: PacingTable
    start: int
    anchor: Anchor
    clockwork: Clockwork
    minute: int | None

    @property
    def fixed(self) -> bool:
        """Whether the anchor fixes the time of day as the turn starts."""
        return self.minute is not None


async def read_clock(conn: AsyncConnection, story_id: str) -> ClockRead | None:
    """The clock as the turn starts (``None``: the story does not run it)."""
    level = await world_clock.clock_level(conn, story_id)
    if level is None:
        return None
    found = await world_clock.settings(conn, story_id)
    start = (await world_clock.clock_at(conn, story_id, world_clock.HEAD)).offset_minutes
    anchor = await world_clock.anchor_at(conn, story_id)
    return ClockRead(
        level=level,
        mode=found.mode,
        table=tuple(found.pacing_table),
        start=start,
        anchor=anchor,
        clockwork=world_clock.clockwork(found),
        minute=minute_of_day(anchor, start),
    )


@dataclass(frozen=True, slots=True)
class _Line:
    kind: str
    text: str


def renders_of(beats: Sequence[TurnBeat], player_last: int = PLAYER_BEAT) -> list[list[TurnBeat]]:
    """The turn's beats grouped into renders (see the module docstring); the player's render
    (beats up to ``player_last``) first, even when it has no beat here."""
    player: list[TurnBeat] = []
    found: list[list[TurnBeat]] = []
    key: object = None
    for beat in sorted(beats, key=lambda b: b.beat):
        if beat.beat <= player_last:
            player.append(beat)
            continue
        mine = "narrator" if beat.narrator else (beat.narrated_by or beat.character_id)
        if not found or mine != key:
            found.append([])
            key = mine
        found[-1].append(beat)
    return [player, *found]


def _advances(clock: ClockRead, beat: TurnBeat) -> list[TimedAdvance]:
    step = int(beat.step or 0)
    found: list[TimedAdvance] = []
    for declared in beat.advances:
        at = int(declared.step) if declared.step is not None else step
        if declared.minutes is not None:
            found.append(
                TimedAdvance(
                    at,
                    minutes=int(declared.minutes),
                    reason=declared.reason,
                    flashback=beat.flashback,
                )
            )
        elif declared.to is not None and clock.minute is not None:
            wanted = int(declared.to[:2]) * 60 + int(declared.to[3:])
            target = next_offset(
                wanted,
                at_offset=clock.start,
                at_minute=clock.minute,
                day_minutes=clock.clockwork.day_minutes,
            )
            found.append(
                TimedAdvance(at, to_offset=target, reason=declared.reason, flashback=beat.flashback)
            )
    if clock.minute is not None and beat.kind in PACED_KINDS:
        for minute in narrated_minutes(beat.text, clock.clockwork):
            found.append(
                TimedAdvance(
                    step,
                    to_offset=narrated_offset(
                        minute, at_offset=clock.start, at_minute=clock.minute
                    ),
                    reason="narrated time",
                    narrated=True,
                    flashback=beat.flashback,
                )
            )
    return found


def timed_turn(
    clock: ClockRead,
    beats: Sequence[TurnBeat],
    *,
    say: str = "",
    do: str = "",
    player_last: int = PLAYER_BEAT,
    steps: Mapping[int, Sequence[int]] | None = None,
    mode: str = SEQUENTIAL,
) -> TurnTiming:
    """The turn's timing (see the module docstring). ``steps``: each character render's
    declared step indices, by the beat its steps sit at (its first beat)."""
    renders = renders_of(beats, player_last)
    timed: list[TimedBeat] = []
    by_first: dict[int, list[int]] = {}
    for n, render in enumerate(renders):
        lines: list[Any] = list(render)
        if n == 0:
            lines = [_Line("say", say), _Line("do", do), *render]
            first, last = PLAYER_BEAT, max([PLAYER_BEAT, *(b.beat for b in render)])
        else:
            first, last = render[0].beat, render[-1].beat
        for at, indices in (steps or {}).items():
            if first <= at <= last:
                by_first.setdefault(first, []).extend(indices)
        timed.append(
            TimedBeat(
                first,
                last,
                pacing=pacing_increment(lines, clock.table, mode=clock.mode),
                advances=tuple(a for b in render for a in _advances(clock, b)),
            )
        )
    owners = {b.beat: b.step for b in beats if b.step}
    return time_turn(timed, by_first, mode, clock.start, owners)


_ATTEMPT: ContextVar[tuple[str, ClockRead] | None] = ContextVar(
    "epistrel_attempt_clock", default=None
)


def attempt(story_id: str, clock: ClockRead | None) -> None:
    """The clock the current attempt of ``story_id``'s turn read (``render_scope`` times each
    render's verification against it)."""
    _ATTEMPT.set(None if clock is None else (story_id, clock))


@contextmanager
def render_scope(
    story_id: str,
    turn: int,
    beats: Sequence[TurnBeat],
    *,
    steps: Mapping[int, Sequence[int]] | None = None,
) -> Iterator[None]:
    """Within the block, the turn's positions take the render's provisional step times (its
    declared and narrated advances from the turn's start), so a claim a step makes is checked
    at the step's time: "at 15:10 she starts driving" at 15:05 is 15:10 there, not a
    contradiction of the present (FR-CLOCK-17). Nothing when the attempt runs no clock."""
    found = _ATTEMPT.get()
    if found is None or found[0] != story_id or not beats:
        yield
        return
    timing = timed_turn(found[1], beats, steps=steps)
    scope = SimpleNamespace(narrative_turn=turn, turn_variant=0, slot_k=None, turn_times=timing)
    with world_clock.turn_scope(story_id, scope):
        yield


def report(timing: TurnTiming | None) -> dict[str, Any] | None:
    """``turn_report.clock``: the turn's start and end, each render's start and end, and its
    advances and contradicted times (counts and minutes, never text)."""
    if timing is None:
        return None
    return {
        "mode": timing.mode,
        "start": timing.turn_start,
        "end": timing.turn_end,
        "beats": [{"start": t.start, "end": t.end} for t in timing.beat_times],
        "advances": [
            {"minutes": a.minutes, "source": a.source.value, "step": a.step}
            for a in timing.advances
        ],
        "contradicted": len(timing.contradicted),
    }


__all__ = [
    "PLAYER_BEAT",
    "ClockRead",
    "attempt",
    "read_clock",
    "render_scope",
    "renders_of",
    "report",
    "timed_turn",
]
