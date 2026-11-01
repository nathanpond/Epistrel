"""CLOCK-MONOTONIC: the world clock never decreases along the active timeline, equals the fold
of its recorded active advances, and replay reproduces it (docs/04 §6; FR-CLOCK-1,2,12; #193).

Every CLOCK-MONOTONIC M6 function lives in this file (the traceability gate counts a test ID per
milestone in one file), one M6 marker each: #193's fold part here, #194's timing clauses
(``__timing``, docstring there), and #196, #197, #199 and #203 add ``__suffix`` functions for
their clauses.

**In memory** (``test_clock_monotonic``, 300 examples in CI): any set of non-negative advances,
each at a position ``(turn, variant, slot_k, beat, step)``, on a timeline whose turns each have
one active variant. The story-time basis (``core.resolution.time.TimeBasis``, built from the
active advances as ``core.world_clock`` builds it) never decreases along the active timeline's
positions in order, its value after the last position is the sum of the active advances, and a
position of a turn's other variant (a regeneration in flight) leaves out the active variant's
advances of that turn. Complement: a backward advance is refused before it is written
(``clock_backward``).

**On Postgres** (``__postgres``): one seeded sequence on a Lite story running the clock at
``prompt_only``: turns that advance the clock in their commit (a pacing advance at beat 1),
consumer advances between turns (``POST /clock/advance``'s call, an inter-turn slot),
regenerations, truncations, untruncations, swaps and forks. After every step, in every story:
the clock at each active turn and slot, in order, never decreases; its value equals the sum of
the active counted ``world_clock`` changes; the ``state_values`` projection equals the fold; and
replay holds (the projection rebuilt from the recorded changes equals the stored one, and
reading the clock twice gives the same value: nothing is estimated). Complements: a backward
advance writes nothing; a truncated advance is absent from the active view and folded by
``as_recorded``.
"""

from __future__ import annotations

import os
import random
from collections import Counter
from dataclasses import replace
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from epistrel.core import states, world_clock
from epistrel.core.enums import ScheduledStatus, StateChangeSource
from epistrel.core.errors import ClockBackward
from epistrel.core.events import Operation
from epistrel.core.membership import POSITION_MAX, Position
from epistrel.core.resolution.time import TimeBasis
from epistrel.core.world_clock import skips as rules
from epistrel.core.world_clock import stops
from epistrel.core.world_clock.anchor import Anchor, Statement
from epistrel.core.world_clock.timing import (
    SEQUENTIAL,
    SIMULTANEOUS,
    TimedAdvance,
    TimedBeat,
    derive,
    time_turn,
)
from epistrel.orchestrator.structured import stopping
from epistrel.orchestrator.structured import time_estimation as te
from epistrel.orchestrator.structured.clocking import Timed
from epistrel.orchestrator.verify.verifier import VerifyConfig
from tests import canon_support as c
from tests import clock_support as ck
from tests import effect_support as es
from tests import multi_support as ms
from tests import state_support as ss
from tests import structured_turn_support as st_turn
from tests.auth_support import auth_settings
from tests.event_support import rows
from tests.fork_support import fork
from tests.projection_support import swap, truncate, untruncate
from tests.respond_support import body, everything_sent, respond, respond_client

NIGHTLY = os.environ.get("EPISTREL_HYPOTHESIS_PROFILE") == "nightly"
EXAMPLES = 3_000 if NIGHTLY else 300

# -- in memory ------------------------------------------------------------------------------

advances = st.lists(
    st.tuples(
        st.integers(0, 8),  # turn
        st.integers(1, 2),  # variant
        st.integers(0, 2),  # slot_k
        st.integers(0, 3),  # beat
        st.integers(0, 2),  # step
        st.integers(0, 600),  # minutes
    ),
    max_size=25,
)


@pytest.mark.part("M6", "FR-CLOCK-1,2,12")
@settings(max_examples=EXAMPLES, deadline=None)
@given(advances, st.lists(st.integers(1, 2), min_size=9, max_size=9), st.integers(0, 10_000))
def test_clock_monotonic(
    recorded: list[tuple[int, int, int, int, int, int]], chosen: list[int], zero: int
) -> None:
    # Turn 0 (genesis and its slots) has no variant of its own.
    active_variant = {turn: (0 if turn == 0 else v) for turn, v in enumerate(chosen)}
    # A turn's other variant is dormant (a regenerate made it so): its advances are not active.
    active = [
        ((t, k, b, s), active_variant[t], m)
        for t, v, k, b, s, m in recorded
        if (0 if t == 0 else v) == active_variant[t]
    ]
    basis = TimeBasis(minute_zero=zero, advances=tuple(active))
    positions = sorted(
        {
            Position(t, active_variant[t], k, b, s)
            for t in range(9)
            for k in range(3)
            for b in range(4)
            for s in range(3)
        },
        key=Position.key,
    )
    values = [basis.at(p) for p in positions]
    assert values == sorted(values), "the clock decreased along the active timeline"
    assert basis.at(world_clock.HEAD) == zero + sum(m for _, _, m in active)
    # A position starts before its own advances (an advance ends its beat or slot).
    assert values[0] == zero
    assert [basis.through(p) for p in positions] == sorted(basis.through(p) for p in positions)
    # A regeneration in flight (the turn's other variant) reads no advance of the active one:
    # its whole turn sits at the clock the turns before it left.
    for turn in range(1, 9):
        other = 3 - active_variant[turn]
        earlier = zero + sum(m for key, _, m in active if key[0] < turn)
        assert basis.at(Position(turn, other)) == earlier
        assert basis.at(Position(turn, other, 0, POSITION_MAX, POSITION_MAX)) == earlier
        assert basis.at(Position.end_of(turn)) >= earlier


# -- Postgres ---------------------------------------------------------------------------------


async def _turn(
    engine: AsyncEngine,
    story: str,
    minutes: int,
    *,
    regenerate: bool = False,
) -> Operation:
    """A turn whose commit advances the clock ``minutes`` at beat 1 (as #194's pacing will)."""

    async def write(conn: AsyncConnection, op: Operation) -> None:
        assert op.narrative_turn is not None
        await world_clock.advance(
            conn,
            op,
            story,
            minutes=minutes,
            source=StateChangeSource.PACING,
            position=Position(op.narrative_turn, op.turn_variant or 0, 0, 1),
            reason="the exchange takes a while",
        )

    turn = None
    if regenerate:
        head = (await rows(engine, "SELECT next_turn_index FROM stories WHERE id = :s", s=story))[0]
        turn = int(head.next_turn_index) - 1 or None
    op, _ = await c.run(engine, story, write, turn=turn)
    return op


async def _active_clock_sum(engine: AsyncEngine, story: str) -> int:
    found = await rows(
        engine,
        "SELECT coalesce(sum(c.delta), 0) AS total FROM state_changes c "
        "JOIN state_definitions d ON d.id = c.state_id WHERE c.story_id = :s "
        "AND d.name = 'world_clock' AND c.membership = 'active' "
        "AND c.status IN ('committed', 'converted')",
        s=story,
    )
    return int(found[0].total)


async def _active_positions(engine: AsyncEngine, story: str) -> list[Position]:
    """The ends of every active turn and inter-turn slot, in timeline order."""
    found = await rows(
        engine,
        "SELECT narrative_turn, turn_variant, coalesce(slot_k, 0) AS slot_k FROM operations "
        "WHERE story_id = :s AND status = 'committed' AND membership = 'active' "
        "AND narrative_turn IS NOT NULL",
        s=story,
    )
    return sorted(
        {
            Position(
                int(r.narrative_turn),
                int(r.turn_variant or 0),
                int(r.slot_k),
                POSITION_MAX,
                POSITION_MAX,
            )
            for r in found
        },
        key=Position.key,
    )


async def _clock_values(engine: AsyncEngine, story: str) -> list[dict[str, Any]]:
    async with engine.connect() as conn:
        clock = await world_clock.clock_definition(conn, story)
    return [r for r in await ss.stored_values(engine, story) if r["state_id"] == clock.id]


async def _folded_clock(engine: AsyncEngine, story: str) -> list[dict[str, Any]]:
    async with engine.connect() as conn:
        clock = await world_clock.clock_definition(conn, story)
    return [r for r in await ss.folded_values(engine, story) if r["state_id"] == clock.id]


async def _check(engine: AsyncEngine, story: str) -> None:
    values = [await ck.offset(engine, story, p) for p in await _active_positions(engine, story)]
    assert values == sorted(values), f"the clock decreased along the active timeline: {values}"
    now = await ck.offset(engine, story)
    assert now == await _active_clock_sum(engine, story)
    assert not values or values[-1] <= now
    # Replay: the projection rebuilt from the recorded advances is the stored one, and a
    # second read of the clock gives the same value (nothing estimated).
    assert await _clock_values(engine, story) == await _folded_clock(engine, story)
    assert await ck.offset(engine, story) == now


SEED = 193
STEPS = 30


@pytest.mark.integration
@pytest.mark.part("M6", "FR-CLOCK-1,2,12")
async def test_clock_monotonic__postgres(engine: AsyncEngine, clock_delivered: None) -> None:
    w = await c.world(engine)
    source = w.story_id
    await ck.set_level(engine, source, "prompt_only")
    chosen = random.Random(SEED)  # noqa: S311 - a seeded sequence, not a secret
    stories = [source]
    done: Counter[str] = Counter()
    await _turn(engine, source, 5)
    for _ in range(STEPS):
        story = stories[-1]
        roll = chosen.random()
        if roll < 0.4:
            regenerate = roll < 0.1
            await _turn(engine, story, chosen.randrange(0, 90), regenerate=regenerate)
            done["regenerate" if regenerate else "turn"] += 1
        elif roll < 0.55:
            await ck.advance(engine, story, chosen.randrange(0, 600))
            done["advance"] += 1
        elif roll < 0.7:
            head = (
                await rows(engine, "SELECT next_turn_index FROM stories WHERE id = :s", s=story)
            )[0]
            cut = chosen.randrange(0, max(1, int(head.next_turn_index) - 1))
            done["truncate"] += await truncate(engine, story, cut) is not None
        elif roll < 0.8:
            done["untruncate"] += await untruncate(engine, story) is not None
        elif roll < 0.9:
            done["swap"] += await swap(engine, story) is not None
        elif len(stories) < 3:
            forked = await fork(engine, story)
            await ck.set_level(engine, forked.story_id, "prompt_only")
            stories.append(forked.story_id)
            done["fork"] += 1
        for each in stories:
            await _check(engine, each)
    assert {"turn", "regenerate", "advance", "truncate", "untruncate", "swap", "fork"} <= {
        k for k, v in done.items() if v
    }, done

    # Complement: a backward advance is refused and writes nothing.
    story = stories[-1]
    before = await ck.clock_changes(engine, story)
    now = await ck.offset(engine, story)
    with pytest.raises(ClockBackward):
        await ck.advance(engine, story, to=now - 1)
    with pytest.raises(ClockBackward):
        await ck.advance(engine, story, -5)

    async def backward(conn: AsyncConnection, op: Operation) -> None:
        assert op.narrative_turn is not None
        await world_clock.advance(
            conn,
            op,
            story,
            to_offset=now - 1,
            source=StateChangeSource.CONSUMER,
            position=Position(op.narrative_turn, op.turn_variant or 0, 0, 1),
            reason="back",
        )

    with pytest.raises(ClockBackward):
        await c.run(engine, story, backward)
    assert await ck.clock_changes(engine, story) == before
    assert await ck.offset(engine, story) == now

    # Complement: a truncated advance leaves the active view and stays in as_recorded.
    await _turn(engine, story, 7)
    await _turn(engine, story, 11)
    head = (await rows(engine, "SELECT next_turn_index FROM stories WHERE id = :s", s=story))[0]
    active_before = await ck.offset(engine, story)
    assert await truncate(engine, story, int(head.next_turn_index) - 2) is not None
    await _check(engine, story)
    assert await ck.offset(engine, story) == active_before - 11
    assert await ck.offset(engine, story, view=states.View.AS_RECORDED) >= active_before


# -- in-turn timing (#194) ----------------------------------------------------------------------

render = st.tuples(
    st.integers(1, 3),  # beats
    st.integers(0, 3),  # declared steps
    st.integers(0, 3),  # pacing increment
    st.lists(
        st.tuples(
            st.integers(0, 3),  # the step it attaches to
            st.booleans(),  # minutes (else a target offset)
            st.integers(-30, 90),  # minutes, or the target's distance from the turn start
            st.booleans(),  # a flashback's
        ),
        max_size=3,
    ),
)


@pytest.mark.part("M6", "FR-CLOCK-6,17")
@settings(max_examples=EXAMPLES, deadline=None)
@given(
    st.lists(render, min_size=1, max_size=5),
    st.sampled_from([SEQUENTIAL, SIMULTANEOUS]),
    st.integers(0, 10_000),
    st.integers(0, 2_000),
)
def test_clock_monotonic__timing(
    renders: list[tuple[int, int, int, list[tuple[int, bool, int, bool]]]],
    mode: str,
    start: int,
    zero: int,
) -> None:
    """#194's clauses (FR-CLOCK-6,17): for any turn of renders (beats, declared steps, a pacing
    increment, advances at steps: durations or targets, some behind the clock, some a
    flashback's), the recorded advances fold to the turn's end; every render's start and end
    and every step's start equal what ``derive`` rebuilds from the recorded advances
    (sequential renders chained end to start, simultaneous renders sharing the turn's start, the
    turn ending at the latest end); along a sequential turn the clock at each step's position is
    its start; and every record a step writes in the commit (``TimeBasis.in_turn``) takes its
    step's start. Complement: a target behind where the render stands moves nothing."""
    beats: list[TimedBeat] = []
    steps: dict[int, list[int]] = {}
    first = 1
    for count, declared, pacing, advances in renders:
        found = []
        for step, is_minutes, amount, flashback in advances:
            step = min(step, declared)
            if is_minutes:
                found.append(TimedAdvance(step, minutes=max(0, amount), flashback=flashback))
            else:
                found.append(TimedAdvance(step, to_offset=start + amount, flashback=flashback))
        beats.append(TimedBeat(first, first + count - 1, pacing, tuple(found)))
        steps[first] = list(range(1, declared + 1))
        first += count
    timing = time_turn(beats, steps, mode, start)

    # The recorded advances fold to the turn's end; nothing moves backward.
    assert all(a.minutes >= 0 for a in timing.advances)
    assert start + sum(a.minutes for a in timing.advances) == timing.turn_end
    for n, beat in enumerate(beats):
        moved = [a for a in beat.advances if not a.flashback]
        refused = [a for m, a in timing.contradicted if m == n]
        assert all(a.to_offset is not None and not a.flashback for a in refused)
        assert len(refused) <= len(moved)
    # Sequential renders chain end to start; simultaneous renders share the turn's start.
    times = timing.beat_times
    if mode == SEQUENTIAL:
        assert times[0].start == start
        assert all(times[n].start == times[n - 1].end for n in range(1, len(times)))
        assert timing.turn_end == times[-1].end
    else:
        assert all(t.start == start for t in times)
        assert timing.turn_end == max(t.end for t in times)
    assert all(t.start <= t.end for t in times)
    # Rebuilt from the recorded advances, the timing is the same.
    rebuilt = derive(timing.advances, timing.layout, steps, mode, start)
    assert rebuilt.turn_end == timing.turn_end
    if mode == SEQUENTIAL:
        assert rebuilt.beat_times == timing.beat_times
        assert rebuilt.step_starts == timing.step_starts
    # The clock along a sequential turn: each step's position folds to its start.
    turn, variant = 7, 1
    before = TimeBasis(minute_zero=zero, advances=(((turn - 1, 0, 0, 0), 1, start),))
    recorded = tuple(
        ((turn, 0, a.beat_position, a.step_position), variant, a.minutes) for a in timing.advances
    )
    basis = replace(before, advances=(*(before.advances or ()), *recorded))
    assert basis.through(Position.end_of(turn)) == zero + timing.turn_end
    if mode == SEQUENTIAL:
        for (n, step), at in timing.step_starts:
            position = Position(turn, variant, 0, beats[n].first_beat, step)
            assert basis.at(position) == zero + at
    # In the commit, every record a step writes takes the step's start.
    committing = replace(basis, in_turn=timing.times(turn, variant))
    for (n, step), at in timing.step_starts:
        for beat_index in range(beats[n].first_beat, beats[n].last_beat + 1):
            position = Position(turn, variant, 0, beat_index, step)
            assert committing.at(position) == zero + timing.step_start(n, step)
            assert timing.step_start(n, step) == at
    assert committing.at(Position(turn, variant, 0, first, 0)) == zero + timing.turn_end


# -- due-event stops (#196) --------------------------------------------------------------------

scheduled = st.lists(
    st.tuples(
        st.integers(0, 240),  # the planned offset
        st.sampled_from(["pending", "pending", "due", "surfaced", "completed"]),
        st.booleans(),  # involves someone present (else no one, and not the scene)
    ),
    max_size=4,
)
PRESENT = "01J9Z8Q4N5V6W7X8Y9Z0A1B2P1"


@pytest.mark.part("M6", "FR-CLOCK-9,16")
@settings(max_examples=EXAMPLES, deadline=None)
@given(
    st.lists(render, min_size=1, max_size=4),
    st.sampled_from([SEQUENTIAL, SIMULTANEOUS]),
    scheduled,
    st.booleans(),
    st.integers(1, 4),
)
def test_clock_monotonic__stops(
    renders: list[tuple[int, int, int, list[tuple[int, bool, int, bool]]]],
    mode: str,
    events: list[tuple[int, str, bool]],
    on: bool,
    turns: int,
) -> None:
    """#196's clauses (FR-CLOCK-9,16): for any turns of renders (pacing, skips at steps) and
    any canon scheduled events (pending or not, involving someone present or no one), each turn
    cut at its due events (``core.world_clock.stops.cut_turn``) moves the clock forward only
    (the capped recorded advances fold to its end); with stops on it never carries the clock
    past a pending involved event: a sequential turn's renders stop at each one they reach (each
    capped there, the next starting at it), a simultaneous turn ends at the earliest (its
    cutoff), every render ending at or before it; a stopped event, ``due`` afterwards, never
    stops a later turn (at most once, across the turns), and a regenerated render is one whose
    advance (not its pacing) reached the stop. Complements: with stops off, or for an event
    that is not pending or involves no one present, the clock is never stopped (the timing is
    the uncapped one) and the crossed pending involved events are overdue."""
    beats: list[TimedBeat] = []
    steps: dict[int, list[int]] = {}
    first = 1
    for count, declared, pacing, advances in renders:
        found = [
            TimedAdvance(min(step, declared), minutes=max(0, amount))
            for step, _, amount, _ in advances
        ]
        beats.append(TimedBeat(first, first + count - 1, pacing, tuple(found)))
        steps[first] = list(range(1, declared + 1))
        first += count
    pending = [
        stops.ScheduledEvent(
            f"01J9Z8Q4N5V6W7X8Y9Z0A1B{n:03d}",
            ScheduledStatus(status),
            planned,
            participants=(PRESENT,) if involved else ("01J9Z8Q4N5V6W7X8Y9Z0A1B2Q9",),
        )
        for n, (planned, status, involved) in enumerate(events)
    ]
    start = 0
    stopped_once: set[str] = set()
    for _ in range(turns):
        cut = stops.cut_turn(
            beats, steps, mode, start, events=pending, present={PRESENT}, scene=None, stops=on
        )
        timing, uncapped = cut.timing, cut.uncapped
        assert uncapped is not None
        # Forward only, and the recorded (capped) advances fold to the turn's end.
        assert all(a.minutes >= 0 for a in timing.advances)
        assert start + sum(a.minutes for a in timing.advances) == timing.turn_end
        assert all(t.start <= t.end for t in timing.beat_times)
        assert timing.turn_end >= start
        crossing = [
            e
            for e in pending
            if e.status is ScheduledStatus.PENDING
            and PRESENT in e.participants
            and start < e.planned <= uncapped.turn_end
        ]
        if not on or not crossing:
            assert cut.stops == ()
            assert timing.beat_times == uncapped.beat_times
            assert timing.turn_end == uncapped.turn_end
            if not on:
                assert {e.event_id for e, _ in cut.overdue} >= {
                    e.event_id
                    for e in crossing
                    if any(t.start < e.planned <= t.end for t in uncapped.beat_times)
                }
        else:
            earliest = min(e.planned for e in crossing)
            assert cut.stops
            assert min(s.at for s in cut.stops) == earliest
            assert len({s.event.event_id for s in cut.stops}) == len(cut.stops)
            assert not {s.event.event_id for s in cut.stops} & stopped_once
            if mode == SIMULTANEOUS:
                assert timing.turn_end == earliest == cut.cutoff
                assert all(t.end <= earliest for t in timing.beat_times)
            else:
                for s in cut.stops:
                    assert timing.beat_times[s.unit].end == s.at
                    if s.unit + 1 < len(timing.beat_times):
                        assert timing.beat_times[s.unit + 1].start == s.at
                    assert s.skip == uncapped.skipping[s.unit]
                assert timing.turn_end <= uncapped.turn_end
            # A regenerated render reached the stop by an advance, never by its pacing alone.
            for unit in cut.regenerate:
                assert uncapped.skipping[unit]
        # The stopped events are due afterwards: they never stop again.
        stopped_once |= {s.event.event_id for s in cut.stops}
        pending = [
            replace(e, status=ScheduledStatus.DUE) if e.event_id in stopped_once else e
            for e in pending
        ]
        start = timing.turn_end


# -- #199: skip authority --------------------------------------------------------------------

PLAYER_LINES = (
    "Tell me more.",
    "I glance at Jenni.",
    "We wait two hours.",
    "I fall asleep on the couch.",
    "Skip to the next day.",
)
SCENE_DOINGS = (
    "I put on a movie for us.",
    "She starts cooking supper.",
    "I look around the room.",
    "We drive north.",
)
SKIP_LINES = (
    "They watch the rest of the movie in silence.",
    "An hour later, the doorbell rings.",
    "They fall asleep on the couch and wake at dawn.",
    "She cooks on, and by the time the stew is ready the lamps are lit.",
    "They drive for two hours.",
)
skip = st.tuples(
    st.sampled_from(SKIP_LINES),
    st.booleans(),  # declared (else estimated)
    st.integers(1, 900),  # minutes
    st.floats(0.0, 1.0),  # the estimate's confidence
)


@pytest.mark.part("M6", "FR-CLOCK-8,12")
@settings(max_examples=EXAMPLES, deadline=None)
@given(
    st.lists(skip, min_size=1, max_size=5),
    st.sampled_from(PLAYER_LINES),
    st.lists(st.sampled_from(SCENE_DOINGS), max_size=3),
    st.integers(0, 600),
    st.integers(0, 2_000),
)
def test_clock_monotonic__skips(
    found: list[tuple[str, bool, int, float]],
    player: str,
    doings: list[str],
    cap: int,
    start: int,
) -> None:
    """#199's clauses (FR-CLOCK-8,12): for any skips a render narrates (declared, or estimated
    at any confidence), any player input and scene, and any ``max_unprompted_skip_minutes``,
    the live turn's decisions (``orchestrator.structured.time_estimation.decide``) accept no
    model-initiated skip that exceeds the cap (or meets a cap of 0) or narrates no established
    activity; the clock moves by exactly the accepted skips (a refused or unestimated skip,
    over the cap or not, is never in the fold); and replay (``derive`` from the recorded
    advances) reproduces the clock, each estimate's source and meta, with no estimation call.
    Complement: a player-initiated skip is accepted whatever its length."""
    established = te.established_activities([*doings])
    calls = Counter[str]()

    def estimated(minutes: int, confidence: float) -> te.Estimate | None:
        calls["time_estimation"] += 1
        if confidence < rules.MIN_CONFIDENCE:
            return None
        return te.Estimate(minutes, "an estimate", confidence, "stub", "0" * 64)

    advances: list[TimedAdvance] = []
    accepted_minutes = 0
    for n, (text, declared, minutes, confidence) in enumerate(found):
        candidate = te.Candidate(
            beat=2 + n,
            index=0 if declared else None,
            span=None,
            text=text,
            minutes=minutes if declared else None,
        )
        decision = te.decide(candidate, player_input=player, cap=cap, established=established)
        if decision.outcome == "estimate":
            made = estimated(minutes, confidence)
            decision = (
                te.unestimated(candidate, decision.classified)
                if made is None
                else te.decide(
                    candidate,
                    player_input=player,
                    cap=cap,
                    established=established,
                    estimated=made,
                )
            )
        classified = decision.classified
        if decision.outcome != "accept":
            assert isinstance(classified, rules.ModelInitiated) or decision.outcome == "unestimated"
            continue
        if declared:
            moved, source = minutes, StateChangeSource.DECLARED_SKIP
            meta: dict[str, Any] | None = {"initiated_by": classified.initiated_by}
        else:
            supplied = decision.advance
            assert supplied is not None
            assert supplied.minutes is not None
            assert supplied.source is not None
            moved, source = supplied.minutes, StateChangeSource(supplied.source)
            meta = None if supplied.meta is None else dict(supplied.meta)
        if isinstance(classified, rules.ModelInitiated):
            # No accepted model-initiated skip exceeds the cap or lacks an established activity.
            assert 0 < moved <= cap
            assert classified.activities & established
        else:
            assert classified.initiated_by == "player"
        advances.append(TimedAdvance(0, minutes=moved, source=source, meta=meta))
        accepted_minutes += moved
    beat = TimedBeat(2, 1 + len(found), 0, tuple(advances))
    timing = time_turn([beat], {}, SEQUENTIAL, start)
    # The clock moves by exactly the accepted skips: nothing refused is in the fold.
    assert timing.turn_end == start + accepted_minutes
    assert [a.minutes for a in timing.advances] == [a.minutes for a in advances]
    # Replay from the recorded advances: the same clock, sources and meta; no estimate.
    before = calls["time_estimation"]
    replayed = derive(timing.advances, timing.layout, {}, SEQUENTIAL, start)
    assert replayed.turn_end == timing.turn_end
    assert [(a.source, a.meta) for a in replayed.advances] == [
        (a.source, a.meta) for a in timing.advances
    ]
    assert calls["time_estimation"] == before


# -- possibly_due pauses (#197) ----------------------------------------------------------------

ranged = st.tuples(st.integers(0, 1_200), st.integers(2, 600))  # the range's start and width
refinement = st.one_of(
    st.none(), st.tuples(st.integers(0, 1_439), st.integers(1, 600))
)  # a stated range [lo, lo + width) of the day, or no refinement before the turn


@pytest.mark.part("M6", "FR-CLOCK-18")
@settings(max_examples=EXAMPLES, deadline=None)
@given(
    st.lists(render, min_size=1, max_size=3),
    st.sampled_from([SEQUENTIAL, SIMULTANEOUS]),
    ranged,
    st.lists(st.integers(0, 1_439), min_size=1, max_size=3),
    st.lists(refinement, min_size=1, max_size=5),
)
def test_clock_monotonic__pauses(
    renders: list[tuple[int, int, int, list[tuple[int, bool, int, bool]]]],
    mode: str,
    anchored: tuple[int, int],
    minutes: list[int],
    turns: list[tuple[int, int] | None],
) -> None:
    """#197's clauses (FR-CLOCK-18): for any range anchor, meetings at any times of day and
    any turns of renders, some preceded by a refinement of the anchor (resolved by
    ``stops.resolve``), each turn cut with stops on (``cut_turn`` with the anchor and its
    revision): no ``possibly_due`` pause occurs twice for one anchor state (event, revision);
    every pause lies where the event is ``possibly_due`` (never where it is due) and before its
    factual stop; the turn's lifecycle (``stopping.lifecycle``) makes a pause ``possibly_due``
    with the revision, never ``due`` or ``surfaced``, and the line a paused render is
    regenerated under names no event and calls nothing due; the clock never passes an owed pause
    point unpaused; an event stops the clock factually at most once."""

    beats: list[TimedBeat] = []
    steps: dict[int, list[int]] = {}
    first = 1
    for count, declared, pacing, advances in renders:
        found = [
            TimedAdvance(min(step, declared), minutes=max(0, amount))
            for step, _, amount, _ in advances
        ]
        beats.append(TimedBeat(first, first + count - 1, pacing, tuple(found)))
        steps[first] = list(range(1, declared + 1))
        first += count
    timed = Timed(tuple(beats), steps, {}, tuple(() for _ in beats))
    lo, width = anchored
    anchor = Anchor.unanchored().refine(Statement(time=((lo, lo + width),)), 0, Position(0))
    assert isinstance(anchor, Anchor)
    events = []
    for n, minute in enumerate(minutes):
        at = anchor.place(minute, 0)
        status = anchor.scheduled(at, 0)
        if status == "due":
            continue  # due as it is created: nothing to pause for
        events.append(
            stops.ScheduledEvent(
                f"01J9Z8Q4N5V6W7X8Y9Z0A1C{n:03d}",
                ScheduledStatus.POSSIBLY_DUE
                if status == "possibly_due"
                else ScheduledStatus.PENDING,
                anchor.due_window(at)[1],
                participants=(PRESENT,),
                planned_anchor={"at": at},
            )
        )
    start = 0
    paused: set[tuple[str, int]] = set()
    stopped_once: set[str] = set()
    for refined in turns:
        if refined is not None:
            a, w = refined
            found_anchor = anchor.refine(Statement(time=((a, min(1_440, a + w)),)), start)
            if isinstance(found_anchor, Anchor):
                anchor = found_anchor
                events = [_resolved(e, anchor, start) for e in events]
        rev = anchor.revision
        owed = [
            point
            for e in events
            if (point := stops.pause_at(e, start, 10**6, anchor, rev)) is not None
        ]
        cut = stops.cut_turn(
            beats,
            steps,
            mode,
            start,
            events=events,
            present={PRESENT},
            scene=None,
            stops=True,
            anchor=anchor,
            revision=rev,
        )
        uncapped = cut.uncapped
        assert uncapped is not None
        assert start + sum(a.minutes for a in cut.timing.advances) == cut.timing.turn_end
        # The clock never passes an owed pause point unpaused.
        reached = [p for p in owed if p < uncapped.turn_end or p == start < uncapped.turn_end]
        if reached:
            assert cut.stops
            assert min(s.at for s in cut.stops) <= min(reached)
        schedule = stopping.Schedule(
            tuple(events), True, frozenset({PRESENT}), None, anchor=anchor, revision=rev
        )
        planned = stopping.lifecycle(schedule, cut, timed, sequential=mode == SEQUENTIAL)
        for s in cut.stops:
            event = s.event
            if s.pause:
                key = (event.event_id, rev)
                assert key not in paused  # once per anchor state
                paused.add(key)
                assert event.at is not None
                assert anchor.scheduled(event.at, s.at) == "possibly_due"
                assert s.at < event.planned
                mine = [p for p in planned if p.event.event_id == event.event_id]
                assert any(
                    p.status is ScheduledStatus.POSSIBLY_DUE and p.pause_rev == rev for p in mine
                )
                factual = any(o.event.event_id == event.event_id and not o.pause for o in cut.stops)
                if not factual:
                    assert all(
                        p.status not in (ScheduledStatus.DUE, ScheduledStatus.SURFACED)
                        for p in mine
                    )
                line = stopping.stop_line(
                    stopping.StopMark(1, 0, s.at, event.event_id, pause=True), None, schedule
                )
                assert "due then" not in line
                assert "scheduled event" not in line
            else:
                assert s.at == event.planned
                assert event.event_id not in stopped_once  # one factual stop each
                stopped_once.add(event.event_id)
        # The turn's steps applied: a pause is possibly_due at this revision, a stop due.
        for p in planned:
            events = [
                replace(
                    e,
                    status=p.status,
                    refinement_pause_anchor_rev=p.pause_rev
                    if p.pause_rev is not None
                    else e.refinement_pause_anchor_rev,
                )
                if e.event_id == p.event.event_id
                else e
                for e in events
            ]
        start = cut.timing.turn_end


def _resolved(event: stops.ScheduledEvent, anchor: Anchor, offset: int) -> stops.ScheduledEvent:
    """``event`` after a refinement to ``anchor`` at ``offset`` (``stops.resolve``'s steps)."""
    for step in stops.resolve(event, anchor, offset, tolerance=15):
        event = replace(
            event,
            status=step.status,
            planned=event.planned if step.planned is None else step.planned,
        )
    return event


CLOCK_ROWS = (
    "SELECT count(*) AS n FROM state_changes c JOIN state_definitions d ON d.id = c.state_id "
    "WHERE c.story_id = :s AND d.name = 'world_clock'"
)
TIME_SECTIONS = ("## Time\n", "## Your sense of time")


@pytest.mark.integration
@pytest.mark.part("M6", "FR-CLOCK-1")
async def test_clock_monotonic__clock_off(
    pg_url: str,
    engine: AsyncEngine,
    drained_embedding_jobs: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With the clock delivered for real (#203: no test patch), a structured story whose
    ``clock.level`` is off keeps no clock: its turns, whose beats narrate times ("at 15:10")
    and speak them, write no ``state_changes`` row for its clock, ``respond`` carries no
    ``clock``, no clock predicate is checked (every verification of its turns runs with
    ``VerifyConfig.clock`` off), and no render's prompt has a time section (``## Time``,
    ``## Your sense of time``).

    Complement: with the clock ``on`` the same turn carries ``clock``, its prompts a time
    section and its verification the clock checks, and a plain turn advances the clock."""
    checked: list[bool] = []
    real = VerifyConfig.of

    def seen(profile: Any, deployment: Any = None) -> VerifyConfig:
        found = real(profile, deployment)
        checked.append(found.clock)
        return found

    monkeypatch.setattr(VerifyConfig, "of", staticmethod(seen))
    stub_settings = auth_settings(pg_url, model_adapter="stub")
    narrated = st_turn.render(
        st_turn.beat("narration", "At 15:10 the bell rings over the door."),
    )
    spoken = st_turn.render(
        st_turn.beat("say", "It's 15:10 already, and the shop closes soon.", intent="sincere")
    )
    async with respond_client(stub_settings, engine) as (client, epistrel):
        stub = epistrel.models.stub
        story, ids = await st_turn.structured_story(client, "Cal", "Mira")
        await ck.anchor_at(engine, story, 15 * 60)
        for n in range(2):
            stub.reset()
            ms.renders_for(stub, "Mira", spoken)
            es.narrator(stub, narrated)
            answer = await respond(client, story, body(ids["Cal"], None, do=f"waits, {n}"))
            assert answer.status_code == 200, answer.text
            assert "clock" not in answer.json()
            sent = everything_sent(stub)
            assert not any(section in sent for section in TIME_SECTIONS)
        (found,) = await rows(engine, CLOCK_ROWS, s=story)
        assert found.n == 0
        assert checked
        assert not any(checked)  # no verification ran with its clock checks on
        checked.clear()

        # Complement: the clock on.
        await ck.set_clock(engine, story, "on", stops=False)
        stub.reset()
        ms.renders_for(stub, "Mira", spoken)
        es.narrator(stub, narrated)
        answer = await respond(client, story, body(ids["Cal"], None, do="waits again"))
        assert answer.status_code == 200, answer.text
        assert "clock" in answer.json()
        assert any(section in everything_sent(stub) for section in TIME_SECTIONS)
        assert checked
        assert all(checked)  # the verification ran with its clock checks on
        stub.reset()
        ms.renders_for(stub, "Mira", st_turn.render(st_turn.beat("say", "Mind the step.")))
        es.narrator(
            stub,
            st_turn.render(
                st_turn.beat("narration", "The bell rings over the door as the light fades.")
            ),
        )
        answer = await respond(client, story, body(ids["Cal"], None, do="waits once more"))
        assert answer.status_code == 200, answer.text
        assert answer.json()["clock"]["after"]["offset"] > 0
        (found,) = await rows(engine, CLOCK_ROWS, s=story)
        assert found.n > 0
