"""U-25: simultaneous cutoff with narrated consequences (docs/04 §3; FR-CLOCK-9,16,17,19,
FR-MULTI-8), its M6 parts.

Every U-25 (M6) function lives in this file (traceability counts a test ID per milestone in one
file): #196's cutoff (``test_u_25``); #198 adds the ownership clause (``__ownership``).

A simultaneous turn starts at 14:50 with two renders. Jenni's says "I'm leaving at 15:10, I
can't miss my train" (her own new intention: a canon ``scheduled`` event involving a present
character, which #198's create path writes from her statement; here its canon request), and
Bobby's narrates "they wait fifty minutes; at 15:40 he finally gets his ticket and hands over 20
silver" (a skip, an acquisition and a State effect after 15:10). After reconciliation the check
runs over both renders together and counts the event the turn creates: the cutoff is 15:10;
Bobby's render is regenerated under it (its beat past the cutoff regenerated, the next dropped),
so nothing of it is committed after 15:10; Jenni's render, which ends by pacing, keeps its end;
every render's end is at or before the turn's end, which is the cutoff; and Jenni's departure is
surfaced to the next turn as due. **Ownership variant:** the 15:10 event is a gate closing a
director beat of the same turn creates (at the scene's place); the cutoff is the same. **Pacing
variant:** a render that overruns only through its pacing increment is clamped, not regenerated.

The pure clauses run the cut (``core.world_clock.stops.cut_turn`` through
``orchestrator.structured.stopping``); the integration clause plays the turn through
``respond`` with the stub: the regeneration happens once, the ticket and the silver are never
committed, and the pacing-only variant makes no regeneration call.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.config import Settings
from epistrel.core.canon.canonize import CanonRequest
from epistrel.core.enums import ClockMode, RecordProvenance, ScheduledStatus
from epistrel.core.membership import Position
from epistrel.core.propositions.model import Granularity, Instant, Proposition
from epistrel.core.propositions.vocabulary import SCHEDULED
from epistrel.core.turns.beats import DeclaredAdvance, TurnBeat
from epistrel.core.world_clock import stops
from epistrel.core.world_clock.anchor import Anchor, statement_of
from epistrel.core.world_clock.pacing import validate_pacing_table
from epistrel.core.world_clock.timing import SIMULTANEOUS, minute_of_day
from epistrel.orchestrator.structured import clocking, stopping
from tests import clock_support as ck
from tests import effect_support as es
from tests import multi_support as ms
from tests import perception_support as ps
from tests import state_support as ss
from tests.auth_support import auth_settings
from tests.respond_support import body, respond, respond_client
from tests.scenario.test_s_diner import leak, skip
from tests.story_support import WRITE
from tests.structured_turn_support import beat, claim, turn_report

TEN_TO_THREE = 14 * 60 + 50
CUTOFF = 20
"""15:10, as an offset from 14:50."""
ZERO = 1_000
"""The story-minute of offset 0 in the pure clauses."""
JENNI, BOBBY = "01J9Z8Q4N5V6W7X8Y9Z0A1B2J1", "01J9Z8Q4N5V6W7X8Y9Z0A1B2B1"
GATE, STATION = "01J9Z8Q4N5V6W7X8Y9Z0A1B2G1", "01J9Z8Q4N5V6W7X8Y9Z0A1B2S1"


def clock() -> clocking.ClockRead:
    """14:50, anchored to the minute."""
    statement = statement_of("2:50 pm")
    assert statement is not None
    anchor = Anchor.unanchored().refine(statement, 0, Position(0))
    assert isinstance(anchor, Anchor)
    table = validate_pacing_table(((0, 5, 0), (5, 51, 1), (51, None, 2)))
    return clocking.ClockRead(
        "on", ClockMode.PACED, table, 0, anchor, anchor.clockwork, minute_of_day(anchor, 0)
    )


def schedule(*events: stops.ScheduledEvent) -> stopping.Schedule:
    return stopping.Schedule(
        events, True, frozenset({JENNI, BOBBY}), STATION, minute_zero=ZERO, grace=30
    )


def jenni() -> TurnBeat:
    return TurnBeat("say", 2, "I'm leaving at ten past, I can't miss my train.", JENNI)


def bobby() -> list[TurnBeat]:
    return [
        TurnBeat(
            "do",
            3,
            "They wait fifty minutes.",
            BOBBY,
            advances=(DeclaredAdvance("they wait", minutes=50),),
        ),
        TurnBeat("do", 4, "He finally gets his ticket and hands over 20 silver.", BOBBY),
    ]


def created(subject: str, provenance: RecordProvenance, **qualifiers: Any) -> CanonRequest:
    """The canon ``scheduled`` request a beat of the turn makes: 15:10."""
    return CanonRequest(
        claim=Proposition(
            subject_id=subject,
            predicate_id=SCHEDULED,
            object_id=GATE,
            qualifiers=qualifiers,
            valid_time=Instant.of(ZERO + CUTOFF, Granularity.MINUTE),
        ),
        provenance=provenance,
        beat=2,
        claim_id="c-1",
    )


@pytest.mark.part("M6", "FR-CLOCK-9,16,17,19; FR-MULTI-8")
def test_u_25() -> None:
    at = clock()
    beats = [jenni(), *bobby()]
    cast = [JENNI, BOBBY]
    # Jenni's own intention (#198's create): a canon event of hers, created by the turn.
    (train,) = stopping.created_events([created(JENNI, RecordProvenance.RENDER)], ZERO, cast, 30)
    assert (train.planned, train.participants, train.status) == (
        CUTOFF,
        (JENNI,),
        ScheduledStatus.PENDING,
    )
    # The ownership variant: a director beat's gate closing at the scene's place.
    (gate,) = stopping.created_events(
        [created(GATE, RecordProvenance.DIRECTOR, place=STATION)], ZERO, cast, 30
    )
    assert (gate.planned, gate.participants, gate.place_id) == (CUTOFF, (), STATION)
    for event in (train, gate):
        found, timed = stopping.cut(at, schedule(), beats, mode=SIMULTANEOUS, created=[event])
        assert found.cutoff == CUTOFF
        # Bobby's render (render 2) skipped past the cutoff: regenerated; its first beat past
        # it is regenerated, the rest dropped.
        assert sorted(found.regenerate) == [2]
        marks, drops = stopping.marks_of(found, timed)
        assert [(m.beat, m.cutoff, m.event_id) for m in marks] == [(3, CUTOFF, event.event_id)]
        assert drops == {4}
        # Jenni's render ends by its pacing and keeps its end; every render ends at or before
        # the turn's end, which is the cutoff.
        assert found.timing.beat_times[1] == found.uncapped.beat_times[1]  # type: ignore[union-attr]
        assert found.timing.beat_times[1].end == 1
        assert all(t.end <= found.timing.turn_end for t in found.timing.beat_times)
        assert found.timing.turn_end == CUTOFF
        # Her departure is due (with the cutoff), and surfaced to the next turn.
        planned = stopping.lifecycle(schedule(), found, timed, sequential=False)
        assert [(p.status.value, p.event.event_id, p.stopped_at) for p in planned] == [
            ("due", event.event_id, CUTOFF)
        ]
        due = stops.ScheduledEvent(
            event.event_id, ScheduledStatus.DUE, CUTOFF, participants=event.participants
        )
        next_cut, next_timed = stopping.cut(at, schedule(due), [jenni()], mode=SIMULTANEOUS)
        assert [
            (p.status.value, p.beat)
            for p in stopping.lifecycle(schedule(due), next_cut, next_timed, sequential=False)
        ] == [("surfaced", 1)]

    # The regenerated beat reaches the cutoff exactly, and the turn ends there.
    (mark,) = stopping.marks_of(
        *stopping.cut(at, schedule(), beats, mode=SIMULTANEOUS, created=[train])
    )[0]
    repaired = stopping.reach(TurnBeat("do", 3, "At ten past, Jenni leaves.", BOBBY), mark)
    found, _ = stopping.cut(at, schedule(), [jenni(), repaired], mode=SIMULTANEOUS, created=[train])
    assert found.timing.turn_end == CUTOFF
    assert found.regenerate == {}
    (advance,) = [a for a in found.timing.advances if a.minutes]
    assert (advance.minutes, advance.stopped_by) == (CUTOFF, train.event_id)

    # Pacing variant: Bobby overruns only through his pacing increment: clamped, not
    # regenerated.
    soon = stops.ScheduledEvent(GATE, ScheduledStatus.PENDING, 1, participants=(JENNI,))
    talk = TurnBeat("say", 3, " ".join(["word"] * 60), BOBBY)
    found, _ = stopping.cut(at, schedule(soon), [jenni(), talk], mode=SIMULTANEOUS)
    assert (found.cutoff, found.regenerate, found.timing.turn_end) == (1, {}, 1)
    assert all(t.end <= 1 for t in found.timing.beat_times)


@pytest.fixture
def stub_settings(pg_url: str) -> Settings:
    return auth_settings(pg_url, model_adapter="stub")


async def station(
    client: Any, engine: AsyncEngine, planned: int
) -> tuple[str, dict[str, str], str]:
    """The station at 14:50 (Cal, Jenni and Bobby there, Bobby with 30 silver), simultaneous
    renders, with Jenni's train at ``planned``."""
    story, ids = await es.shop(client, engine, ss.gold(), names=("Cal", "Jenni", "Bobby"))
    place = await ps.place_entity(engine, story, "the station")
    for who in ids.values():
        await ps.put(engine, story, who, place)
    await ck.set_clock(engine, story, "on")
    await ck.anchor_at(engine, story, TEN_TO_THREE)
    await ms.per_character(engine, story, model_calls=30)
    await ss.change(
        engine,
        story,
        ss.set_to("gold", 30, scope_key=ids["Bobby"], reason="a purse"),
    )
    made = await client.post(
        f"/stories/{story}/scheduled-events",
        json={"proposition": "Jenni's train", "planned": planned, "participants": [ids["Jenni"]]},
        headers=WRITE,
    )
    assert made.status_code == 201, made.text
    return story, ids, made.json()["event"]["event_id"]


async def play(client: Any, stub: Any, story: str, ids: dict[str, str]) -> dict[str, Any]:
    answer = await respond(
        client,
        story,
        body(
            ids["Cal"],
            "We should hurry.",
            render_character_ids=[ids["Jenni"], ids["Bobby"]],
            interaction_mode="simultaneous",
        ),
    )
    assert answer.status_code == 200, answer.text
    found: dict[str, Any] = answer.json()
    return found


@pytest.mark.integration
@pytest.mark.part("M6", "FR-CLOCK-9,16,17,19; FR-MULTI-8")
async def test_u_25__turn(
    stub_settings: Settings,
    engine: AsyncEngine,
    states_delivered: None,
    stops_delivered: None,
    drained_embedding_jobs: None,
) -> None:
    """The cutoff through ``respond`` (see the module docstring): Bobby's render regenerated
    once, nothing of it committed past 15:10 (the ticket, the silver), the clock at 15:10;
    the pacing-only variant clamps with no regeneration call."""
    async with respond_client(stub_settings, engine) as (client, epistrel):
        stub = epistrel.models.stub
        story, ids, train = await station(client, engine, CUTOFF)
        stub.reset()
        ms.renders_for(stub, "Jenni", ms.render(beat("say", "I'm leaving at ten past.")))
        ms.renders_for(
            stub,
            "Bobby",
            ms.render(
                beat("do", "They wait fifty minutes.", effects=[skip(50, "they wait")]),
                beat(
                    "do",
                    "He finally gets his ticket and hands over 20 silver.",
                    propositions=[claim("Bobby", "holds", "the ticket")],
                    effects=[es.effect("gold", delta=-20, reason="the ticket")],
                ),
            ),
        )
        found = await play(client, stub, story, ids)
        assert found["clock"]["after"]["offset"] == CUTOFF
        # One regeneration under the cutoff (the ladder's other repairs are its own).
        stopped = [p for p in es.regenerations(stub) if "Time stops at 15:10" in p]
        assert len(stopped) == 1
        for needle in ("ticket", "silver"):
            assert await leak(engine, story, needle) == [], needle
        assert await ss.value(engine, story, "gold", ids["Bobby"]) == 30
        assert await ck.statuses(engine, story, train) == ["pending", "due"]
        clock = (await turn_report(engine, story))["clock"]
        assert clock["mode"] == "simultaneous"
        assert all(b["end"] <= clock["end"] == CUTOFF for b in clock["beats"])
        # Jenni's departure is surfaced to the next turn, as due (her line in her render).
        stub.reset()
        ms.renders_for(stub, "Jenni", ms.render(beat("say", "That's my train.")))
        ms.renders_for(stub, "Bobby", ms.render(beat("say", "Go.")))
        await play(client, stub, story, ids)
        assert await ck.statuses(engine, story, train) == ["pending", "due", "surfaced"]
        (prompt,) = ms.render_prompts(stub, "Jenni")
        assert "Scheduled event now due: Jenni's train (planned for 15:10)" in prompt
        (other,) = ms.render_prompts(stub, "Bobby")
        assert "Jenni's train" not in other

        # Pacing only: Bobby's 60 words overrun the 14:51 train by pacing alone: clamped, and
        # no regeneration call.
        story, ids, train = await station(client, engine, 1)
        stub.reset()
        ms.renders_for(stub, "Jenni", ms.render(beat("say", "Oh no.")))
        ms.renders_for(stub, "Bobby", ms.render(beat("say", " ".join(["word"] * 60))))
        found = await play(client, stub, story, ids)
        assert found["clock"]["after"]["offset"] == 1
        assert es.regenerations(stub) == []
        assert (await ck.events(engine, story))[0].stopped_clock_at == 1
