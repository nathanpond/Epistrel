"""U-23: beat and step timing (docs/04 §3; FR-CLOCK-6,9,17), its M6 parts.

Every U-23 (M6) function lives in this file (traceability counts a test ID per milestone in one
file): #194's non-appointment clauses (``test_u_23``, ``test_u_23__stamps``); #196 adds the
appointment clauses (``__appointment``, pure: ``core.world_clock.stops`` and
``orchestrator.structured.stopping``).

- ``test_u_23`` (pure: ``core.world_clock.timing``, ``orchestrator.structured.clocking`` and
  the render schema). Clock 15:05 (#195's anchor fixes the minute); one render "At 15:10 she
  starts driving; twenty minutes later she arrives", declared as two ``time`` steps → step 1 at
  15:10, ``driving`` over [15:10, 15:30), step 2 (arrival) at 15:30, the render's end 15:30, no
  pacing increment added; a render with no advances starts at the previous render's end and
  ends after its pacing increment; in a sequential turn render 2 starts at render 1's end; a
  narrated "at 15:00" at 15:05 is contradicted and moves nothing; in a simultaneous turn every
  render starts at the turn's start, one render's 10-minute skip moves only its own steps, and
  the turn ends at the latest end; a record at a step (a claim, an exposure, an acquisition)
  takes the step's start (``TimeBasis.in_turn``). The schema: ``time`` steps own beats, cite
  nothing and name no proposition; a ``time_advance`` gives ``minutes`` (a ``to`` only when the
  anchor fixes the time of day: else ``clock_unanchored``), names the ``world_clock`` and a
  declared step, and needs the clock; ``flashback`` is an optional beat field. Complement: a
  ``flashback`` "at 15:00" is accepted (not contradicted), moves nothing and adds no pacing.
- ``test_u_23__stamps`` (Postgres, the structured pipeline with the stub): the same render
  through ``respond`` at 15:05, starting to drive at 15:40 (past the 15-minute time-statement
  tolerance): the clock's recorded advances are the narrated and declared ones at their steps
  (35 and 20 minutes, ``declared_skip``), the turn ends at 16:00 (``respond.clock.after``,
  the fold), each beat's event takes its step's start (``events.valid_time_minutes``) and a
  later render of the same turn starts at 16:00. The narrated "at 15:40" is verified at its
  step's time (supported, not repaired), and a later render's ``flashback`` "at 14:30" is
  never checked against the present (no claim, no repair) and moves nothing.
"""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

import pytest
from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.config import Settings
from epistrel.core import world_clock
from epistrel.core.enums import ClockMode, ScheduledStatus, StateChangeSource
from epistrel.core.membership import Position
from epistrel.core.propositions.model import Granularity, Instant, Interval
from epistrel.core.resolution.time import TimeBasis
from epistrel.core.turns.beats import DeclaredAdvance, TurnBeat
from epistrel.core.world_clock import stops
from epistrel.core.world_clock.anchor import Anchor, statement_of
from epistrel.core.world_clock.pacing import validate_pacing_table
from epistrel.core.world_clock.timing import (
    SEQUENTIAL,
    SIMULTANEOUS,
    TimedAdvance,
    TimedBeat,
    minute_of_day,
    restamp,
    time_turn,
)
from epistrel.orchestrator.structured import clocking, stopping
from epistrel.orchestrator.structured.render import render_schema
from tests import clock_support as ck
from tests import multi_support as ms
from tests import perception_support as ps
from tests.auth_support import auth_settings
from tests.event_support import rows
from tests.respond_support import body, respond, respond_client
from tests.story_support import WRITE
from tests.structured_turn_support import beat, structured_story, turn_report

AT_1505 = 15 * 60 + 5
DRIVE = "At 15:10 she starts driving."
ARRIVE = "Twenty minutes later she arrives."
LATE_DRIVE = "At 15:40 she starts driving."
"""Through the pipeline: a narrated time past the time-statement tolerance (15 minutes), so
only a claim checked at its step's time (15:40) is supported."""


def anchored(offset: int = 0) -> Anchor:
    """An anchor that fixes the time of day: 15:05 at ``offset``."""
    statement = statement_of("15:05")
    assert statement is not None
    found = Anchor.unanchored().refine(statement, offset, Position(0))
    assert isinstance(found, Anchor)
    return found


def clock(start: int = 0) -> clocking.ClockRead:
    anchor = anchored(start)
    table = validate_pacing_table(((0, 5, 0), (5, 51, 1), (51, None, 2)))
    minute = minute_of_day(anchor, start)
    return clocking.ClockRead("on", ClockMode.PACED, table, start, anchor, anchor.clockwork, minute)


def do(n: int, text: str, step: int = 0, **fields: Any) -> TurnBeat:
    return TurnBeat(kind="do", beat=n, text=text, character_id="jenni", step=step, **fields)


def drive(first: int = 2) -> list[TurnBeat]:
    return [
        do(first, DRIVE, 1),
        do(first + 1, ARRIVE, 2, advances=(DeclaredAdvance("the drive", minutes=20),)),
    ]


@pytest.mark.part("M6", "FR-CLOCK-6,17")
def test_u_23() -> None:
    at = clock()
    assert at.minute == AT_1505

    # One render: step 1 at 15:10, step 2 at 15:30, its end 15:30, no pacing added.
    timing = clocking.timed_turn(at, drive(), steps={2: [1, 2]})
    assert timing.step_start(1, 1) == 5
    assert timing.step_start(1, 2) == 25
    assert (timing.beat_times[1].start, timing.beat_times[1].end) == (0, 25)
    assert timing.turn_end == 25
    assert [(a.source, a.minutes, a.step) for a in timing.advances] == [
        (StateChangeSource.DECLARED_SKIP, 5, 1),
        (StateChangeSource.DECLARED_SKIP, 20, 2),
    ]
    assert not timing.contradicted
    # ``driving`` (a fluent narrated by step 1) over [15:10, 15:30); the arrival at 15:30.
    zero = 1000
    held = restamp(
        Interval.of(zero, None), now=zero, start=zero + 5, next_start=zero + 25, durative=True
    )
    assert held == Interval.of(zero + 5, zero + 25)
    arrived = restamp(Instant.of(zero, Granularity.MINUTE), now=zero, start=zero + 25)
    assert arrived == Instant.of(zero + 25, Granularity.MINUTE)
    # A stated time is kept.
    stated = Interval.of(zero - 60, zero - 30)
    assert restamp(stated, now=zero, start=zero + 5, durative=True) == stated

    # A record at a step takes the step's start (claims, exposures, acquisitions alike).
    basis = TimeBasis(
        minute_zero=zero,
        advances=(((3, 0, 0, 0), 1, 7),),
        in_turn=timing.times(4, 1),
    )
    assert basis.at(Position(4, 1, 0, 2, 1)) == zero + 7 + 5
    assert basis.at(Position(4, 1, 0, 3, 2)) == zero + 7 + 25
    assert basis.at(Position(4, 1, 0, 1, 0)) == zero + 7  # the player's input: the turn start
    assert basis.at(Position(4, 1, 0, 9, 0)) == zero + 7 + 25  # after the renders: the end
    assert basis.at(Position(4, 2, 0, 3, 2)) == zero + 7  # another variant: no in-turn time

    # Sequential: render 2 starts at render 1's end and, with no advances, ends after pacing.
    later = [*drive(), TurnBeat("say", 4, " ".join(["word"] * 12), "bobby")]
    timing = clocking.timed_turn(at, later, steps={2: [1, 2]})
    assert [(t.start, t.end) for t in timing.beat_times] == [(0, 0), (0, 25), (25, 26)]
    assert timing.turn_end == 26

    # "At 15:00" when the clock reads 15:05 is contradicted and moves nothing (pacing applies).
    early = [do(2, "At 15:00 she had already left, she says, and that was the end of it.")]
    timing = clocking.timed_turn(at, early)
    assert len(timing.contradicted) == 1
    assert timing.contradicted[0][1].narrated
    assert [(a.source, a.minutes) for a in timing.advances] == [(StateChangeSource.PACING, 1)]
    # Complement: a flashback "at 15:00" is accepted, moves nothing and adds no pacing.
    back = [do(2, early[0].text, flashback=True)]
    timing = clocking.timed_turn(at, back)
    assert not timing.contradicted
    assert timing.advances == ()
    assert timing.turn_end == 0

    # Simultaneous: every render starts at the turn's start; a 10-minute skip moves only its
    # own render's steps; the turn ends at the latest end.
    renders = [
        TimedBeat(1, 1, 0),
        TimedBeat(2, 3, 1, (TimedAdvance(2, minutes=10, reason="waits"),)),
        TimedBeat(4, 5, 2),
    ]
    timing = time_turn(renders, {2: [1, 2], 4: [1, 2]}, SIMULTANEOUS, 100)
    assert [t.start for t in timing.beat_times] == [100, 100, 100]
    assert [t.end for t in timing.beat_times] == [100, 110, 102]
    assert timing.step_start(1, 2) == 110
    assert timing.step_start(2, 2) == 100
    assert timing.turn_end == 110
    # Only the latest render's advances are recorded, at the turn's end: the fold is its end.
    assert [(a.minutes, a.position[0]) for a in timing.advances] == [(10, 5)]
    assert sum(a.minutes for a in timing.advances) == timing.minutes
    # The same renders in sequence add up.
    assert time_turn(renders, {}, SEQUENTIAL, 100).turn_end == 112

    # The schema: time steps own beats; a time advance gives minutes at a declared step.
    schema = render_schema(
        actor_id="jenni", player_id=None, cast_ids=["jenni"], max_beats=6, clock_level="on"
    )
    advance = {"kind": "time_advance", "state": "world_clock", "minutes": 20, "reason": "drive"}
    ok = {
        "beats": [
            {"kind": "do", "text": DRIVE, "step": 1},
            {"kind": "do", "text": ARRIVE, "step": 2, "effects": [{**advance, "step": 2}]},
            {"kind": "say", "text": "It was 15:00 then.", "flashback": True},
        ],
        "steps": [{"index": 1, "kind": "time"}, {"index": 2, "kind": "time"}],
    }
    parsed = schema.model_validate(ok)
    assert parsed.beats[2].flashback
    assert not parsed.beats[0].flashback
    refusals = {
        "a time step that cites": {**ok, "steps": [{"index": 1, "kind": "time", "cites": ["x"]}]},
        "an undeclared step": {
            "beats": [{"kind": "do", "text": DRIVE, "effects": [{**advance, "step": 3}]}],
            "steps": [{"index": 1, "kind": "time"}],
        },
        "a to the anchor cannot place": {
            "beats": [
                {
                    "kind": "do",
                    "text": DRIVE,
                    "effects": [{**advance, "minutes": None, "to": "15:30"}],
                }
            ]
        },
        "both minutes and to": {
            "beats": [{"kind": "do", "text": DRIVE, "effects": [{**advance, "to": "15:30"}]}]
        },
        "another State": {
            "beats": [{"kind": "do", "text": DRIVE, "effects": [{**advance, "state": "gold"}]}]
        },
        "a think's": {
            "beats": [
                {"kind": "say", "text": "Hm."},
                {"kind": "think", "text": "Later.", "effects": [advance]},
            ]
        },
    }
    for name, answer in refusals.items():
        with pytest.raises(ValidationError):
            schema.model_validate(answer)
        assert name
    fixed = render_schema(
        actor_id="jenni",
        player_id=None,
        cast_ids=["jenni"],
        max_beats=6,
        clock_level="on",
        clock_fixed=True,
    )
    fixed.model_validate(refusals["a to the anchor cannot place"])
    off = render_schema(actor_id="jenni", player_id=None, cast_ids=["jenni"], max_beats=6)
    with pytest.raises(ValidationError, match="module clock is off"):
        off.model_validate({"beats": [{"kind": "do", "text": DRIVE, "effects": [advance]}]})
    # A declared ``to`` is placed at the next such time.
    to = [do(2, "They wait.", advances=(DeclaredAdvance("waits", to="15:45"),))]
    assert clocking.timed_turn(at, to).turn_end == 40


# -- through the pipeline ------------------------------------------------------------------------


@pytest.fixture
def stub_settings(pg_url: str) -> Settings:
    return auth_settings(pg_url, model_adapter="stub")


@pytest.mark.integration
@pytest.mark.part("M6", "FR-CLOCK-17")
async def test_u_23__stamps(
    stub_settings: Settings,
    engine: AsyncEngine,
    clock_delivered: None,
    drained_embedding_jobs: None,
) -> None:
    async with respond_client(stub_settings, engine) as (client, epistrel):
        stub = epistrel.models.stub
        story, ids = await structured_story(client, "Cal", "Jenni", "Bobby")
        garage = await ps.place_entity(engine, story, "the garage")
        for who in ids.values():
            await ps.put(engine, story, who, garage)
        await ck.set_level(engine, story, "on")
        # 15:05, anchored to the minute; manual, so only the declared and narrated times move
        # the clock (no pacing).
        patched = await client.patch(
            f"/stories/{story}/clock", json={"mode": "manual"}, headers=WRITE
        )
        assert patched.status_code == 200, patched.text
        async with engine.begin() as conn:  # the author's starting anchor (no route sets it)
            await conn.execute(
                text(
                    "UPDATE state_definitions SET anchor = CAST(:a AS jsonb) "
                    "WHERE story_id = :s AND name = 'world_clock'"
                ),
                {"a": json.dumps(anchored(0).document()), "s": story},
            )
        await ms.per_character(engine, story)
        advance = {"kind": "time_advance", "state": "world_clock", "minutes": 20}
        ms.renders_for(
            stub,
            "Jenni",
            ms.render(
                beat("do", LATE_DRIVE, step=1),
                beat("do", ARRIVE, step=2, effects=[{**advance, "reason": "the drive"}]),
                steps=[{"index": 1, "kind": "time"}, {"index": 2, "kind": "time"}],
            ),
        )
        ms.renders_for(
            stub, "Bobby", ms.render(beat("do", "At 14:30 he was still asleep.", flashback=True))
        )
        answer = await respond(
            client,
            story,
            body(ids["Cal"], None, do="waits", render_character_ids=[ids["Jenni"], ids["Bobby"]]),
        )
        assert answer.status_code == 200, answer.text
        found = answer.json()
        # "At 15:40" is checked at its step's time (15:40): supported, never repaired; the
        # flashback's "at 14:30" is not checked against the present at all.
        report = await turn_report(engine, story)
        assert [v["kind"] for v in report["verify"]["verdicts"]] == ["supported"]
        assert not report.get("repairs")
        texts = [b["text"] for b in found["beats"] if b["kind"] in ("beat_do", "beat_say")]
        assert texts == [LATE_DRIVE, ARRIVE, "At 14:30 he was still asleep."]
        # The turn ends at 16:00: the narrated time and the declared skip, at their steps.
        assert found["clock"]["before"]["offset"] == 0
        assert found["clock"]["after"]["offset"] == 55
        assert await ck.offset(engine, story) == 55
        changes = await rows(
            engine,
            "SELECT c.source, c.delta, c.beat, c.step FROM state_changes c JOIN "
            "state_definitions d ON d.id = c.state_id WHERE c.story_id = :s AND "
            "d.name = 'world_clock' AND c.narrative_turn = 1 ORDER BY c.beat, c.step",
            s=story,
        )
        assert [(r.source, int(r.delta)) for r in changes] == [
            ("declared_skip", 35),
            ("declared_skip", 20),
        ]
        # Each beat's event takes its step's start; Bobby's render starts at Jenni's end.
        (zero,) = await rows(engine, "SELECT story_minute_zero FROM stories WHERE id = :s", s=story)
        stamped = await rows(
            engine,
            "SELECT character_id, step, valid_time_minutes FROM events WHERE story_id = :s "
            "AND narrative_turn = 1 AND type IN ('beat_do', 'beat_say') ORDER BY beat",
            s=story,
        )
        minute_zero = int(zero.story_minute_zero)
        assert [
            (r.character_id, int(r.step), int(r.valid_time_minutes) - minute_zero) for r in stamped
        ] == [(ids["Jenni"], 1, 35), (ids["Jenni"], 2, 55), (ids["Bobby"], 0, 55)]
        # The turn's report times every render.
        assert report["clock"]["end"] == 55
        assert [b["start"] for b in report["clock"]["beats"]][-1] == 55


@pytest.mark.integration
@pytest.mark.part("M6", "FR-CLOCK-17")
async def test_u_23__stamps_after_the_commit(
    stub_settings: Settings,
    engine: AsyncEngine,
    clock_delivered: None,
    drained_embedding_jobs: None,
) -> None:
    """#631: after the commit, a beat's records read their step's start. In a simultaneous
    turn Jenni's render declares two timed steps (35 and 20 minutes) and Bobby's none: every
    render starts at the turn's start, so no fold of the positioned advances can say Jenni's
    step 2 starts at +55. ``story_time_at`` at each beat's position, as a memory (no
    valid-time column) reads it, equals the time its events were stamped with (an utterance
    names its event). Complement asserted: Bobby's beat, in the same turn, still reads the
    turn's start."""
    async with respond_client(stub_settings, engine) as (client, epistrel):
        stub = epistrel.models.stub
        story, ids = await structured_story(client, "Cal", "Jenni", "Bobby")
        garage = await ps.place_entity(engine, story, "the garage")
        for who in ids.values():
            await ps.put(engine, story, who, garage)
        await ck.set_level(engine, story, "on")
        patched = await client.patch(
            f"/stories/{story}/clock", json={"mode": "manual"}, headers=WRITE
        )
        assert patched.status_code == 200, patched.text
        await ms.per_character(engine, story)
        advance = {"kind": "time_advance", "state": "world_clock", "reason": "the drive"}
        ms.renders_for(
            stub,
            "Jenni",
            ms.render(
                beat("do", "She starts the car.", step=1, effects=[{**advance, "minutes": 35}]),
                beat("say", "Nearly there now.", step=2, effects=[{**advance, "minutes": 20}]),
                steps=[{"index": 1, "kind": "time"}, {"index": 2, "kind": "time"}],
            ),
        )
        ms.renders_for(stub, "Bobby", ms.render(beat("say", "I'll wait here.")))
        answer = await respond(
            client,
            story,
            body(
                ids["Cal"],
                None,
                do="waits",
                render_character_ids=[ids["Jenni"], ids["Bobby"]],
                interaction_mode="simultaneous",
            ),
        )
        assert answer.status_code == 200, answer.text
        assert answer.json()["interaction_mode"] == "simultaneous"
        (zero,) = await rows(engine, "SELECT story_minute_zero FROM stories WHERE id = :s", s=story)
        minute_zero = int(zero.story_minute_zero)
        stamped = await rows(
            engine,
            "SELECT character_id, turn_variant, beat, step, valid_time_minutes FROM events "
            "WHERE story_id = :s AND narrative_turn = 1 AND type IN ('beat_do', 'beat_say') "
            "ORDER BY beat",
            s=story,
        )
        read: list[tuple[str, int]] = []
        async with engine.connect() as conn:
            for r in stamped:
                at = Position(1, int(r.turn_variant), 0, int(r.beat), int(r.step or 0))
                minute = await world_clock.story_time_at(conn, story, at)
                assert minute == int(r.valid_time_minutes), r
                read.append((r.character_id, minute - minute_zero))
        assert read == [(ids["Jenni"], 35), (ids["Jenni"], 55), (ids["Bobby"], 0)]
        # A memory (no valid-time column) at its own position reads its beat's stamp.
        by_beat = {int(r.beat): int(r.valid_time_minutes) for r in stamped}
        kept = await rows(
            engine,
            "SELECT turn_variant, beat, step FROM memories WHERE story_id = :s "
            "AND narrative_turn = 1 AND beat IS NOT NULL AND beat = ANY(:b) ORDER BY beat",
            s=story,
            b=list(by_beat),
        )
        assert {int(r.beat) for r in kept} == set(by_beat)
        async with engine.connect() as conn:
            for r in kept:
                at = Position(1, int(r.turn_variant), 0, int(r.beat), int(r.step))
                assert await world_clock.story_time_at(conn, story, at) == by_beat[int(r.beat)]


# -- the appointment (#196) -------------------------------------------------------------------

MEETING_ID = "01J9Z8Q4N5V6W7X8Y9Z0A1B2M1"


def meeting(planned: int = 55, **fields: Any) -> stops.ScheduledEvent:
    """Jenni's canon 16:00 meeting (offset 55 from 15:05), pending."""
    found: dict[str, Any] = {"participants": ("jenni",), **fields}
    return stops.ScheduledEvent(MEETING_ID, ScheduledStatus.PENDING, planned, **found)


def schedule(*events: stops.ScheduledEvent, on: bool = True) -> stopping.Schedule:
    return stopping.Schedule(
        events, on, frozenset({"jenni", "bobby"}), None, {MEETING_ID: "the meeting"}
    )


@pytest.mark.part("M6", "FR-CLOCK-9,17")
def test_u_23__appointment() -> None:
    """Pacing crosses an appointment (FR-CLOCK-9,17): a 60-word render (a 2-minute increment, no
    skip) from 15:59 with a canon 16:00 meeting ends at 16:00, clamped, with no regeneration;
    the next sequential render starts at 16:00 with the meeting surfaced to it as due (its
    lines); in a simultaneous turn from 15:59 the turn's end is clamped at 16:00 and the meeting
    is surfaced to the next turn. The stopped advance names the meeting. Complements: with stops
    off the clock passes 16:00 and the meeting is overdue; a meeting involving no one present
    never stops it; ``check_advance`` answers ``Stop`` only for a pending, involved event."""
    at = replace(clock(), start=54, minute=AT_1505 + 54)  # 15:59
    long = TurnBeat("say", 2, " ".join(["word"] * 60), "jenni")
    after = TurnBeat("say", 3, "Shall we?", "bobby")

    # Sequential: clamped at 16:00, no re-render; Bobby starts at 16:00 with it surfaced.
    found, timed = stopping.cut(at, schedule(meeting()), [long, after], mode=SEQUENTIAL)
    (stop,) = found.stops
    assert (stop.at, stop.unit, stop.skip) == (55, 1, False)
    assert found.regenerate == {}
    assert [(t.start, t.end) for t in found.timing.beat_times] == [(54, 54), (54, 55), (55, 55)]
    assert found.timing.turn_end == 55
    assert [(a.source, a.minutes, a.stopped_by) for a in found.timing.advances] == [
        (StateChangeSource.PACING, 1, MEETING_ID)
    ]
    planned = stopping.lifecycle(schedule(meeting()), found, timed, sequential=True)
    assert [(p.status.value, p.beat) for p in planned] == [("due", 2), ("surfaced", 3)]
    lines = stopping.due_lines([meeting()], schedule(meeting()), at, character_id="jenni")
    assert lines == (
        "Scheduled event now due: the meeting (planned for 16:00). It is due at this moment; "
        "let it interrupt what is happening.",
    )
    assert stopping.due_lines([meeting()], schedule(meeting()), at, character_id="bobby") == ()

    # Simultaneous: the turn's end is clamped at 16:00, and the meeting surfaces next turn.
    found, timed = stopping.cut(at, schedule(meeting()), [long, after], mode=SIMULTANEOUS)
    assert found.timing.turn_end == 55
    assert all(t.end <= 55 for t in found.timing.beat_times)
    assert found.regenerate == {}
    planned = stopping.lifecycle(schedule(meeting()), found, timed, sequential=False)
    assert [p.status.value for p in planned] == ["due"]
    due = replace(meeting(), status=ScheduledStatus.DUE, stopped_clock_at=55)
    later, timed = stopping.cut(at, schedule(due), [after], mode=SIMULTANEOUS)
    assert [
        (p.status.value, p.beat)
        for p in stopping.lifecycle(schedule(due), later, timed, sequential=False)
    ] == [("surfaced", 1)]

    # Complements: stops off (overdue, the clock passes); nobody present involved.
    found, _ = stopping.cut(at, schedule(meeting(), on=False), [long, after], mode=SEQUENTIAL)
    assert (found.stops, found.timing.turn_end) == ((), 56)
    assert [e.event_id for e, _ in found.overdue] == [MEETING_ID]
    away = meeting(participants=("marcus",))
    found, _ = stopping.cut(at, schedule(away), [long, after], mode=SEQUENTIAL)
    assert (found.stops, found.overdue, found.timing.turn_end) == ((), (), 56)
    span = stops.AdvanceSpan(54, 56)
    assert stops.check_advance(span, {"jenni"}, None, events=[meeting()], stops=True) == stops.Stop(
        meeting(), 55
    )
    assert (
        stops.check_advance(
            stops.AdvanceSpan(54, 54), {"jenni"}, None, events=[meeting()], stops=True
        )
        == stops.Proceed()
    )
    assert stops.check_advance(
        span, {"jenni"}, None, events=[meeting()], stops=False
    ) == stops.Proceed((meeting(),))
    surfaced = replace(meeting(), status=ScheduledStatus.SURFACED)
    assert (
        stops.check_advance(span, {"jenni"}, None, events=[surfaced], stops=True) == stops.Proceed()
    )
    at_scene = meeting(participants=(), place_id="diner")
    assert stops.check_advance(span, set(), "diner", events=[at_scene], stops=True) == stops.Stop(
        at_scene, 55
    )


@pytest.mark.part("M6", "FR-CLOCK-17")
def test_u_23__later_render_time() -> None:
    """#640: a sequential turn's later render is verified from where the earlier renders
    ended: with Jenni's render (a declared 30-minute wait) timed before it, Bobby's beat starts
    at +30 in his render's scope. Complement asserted: timed alone, as before, it would start
    at the turn's start (0)."""
    jenni = [do(2, "She waits.", advances=(DeclaredAdvance("waits", minutes=30),))]
    bobby = [TurnBeat("say", 3, "Done yet?", "bobby")]
    clocking.attempt("s-640", clock(0))
    with clocking.render_scope("s-640", 1, bobby, before=jenni):
        times = world_clock._in_turn("s-640")
        assert times is not None
        assert times.offset(3, 0) == 30
    with clocking.render_scope("s-640", 1, bobby):
        times = world_clock._in_turn("s-640")
        assert times is not None
        assert times.offset(3, 0) == 0
