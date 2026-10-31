"""S-ANCHOR-MEETING: a meeting on a loosely anchored clock (docs/04 §7; FR-CLOCK-3,9,17,18), its
M6 part (#197).

Jenni has a canon 16:00 meeting (created between turns through the API: the clock knows only a
range, so the meeting is held at its time of day, ``planned`` where the range's lower end
reaches it), involving her, with the clock's default 30-minute grace; stops are on (the profile's
``clock.scheduled_stops`` and the story's ``stop_at_due_events``).

- **Pause, not interruption:** anchored "early afternoon", [13:00, 15:00) at offset 0 (the
  meeting ``pending``), "we wander the market for a couple of hours" (Jenni's render declares
  120 minutes) pauses at +61, where the range's upper end passes 16:00 ([14:01, 16:01)): the
  render is regenerated under "time pauses", which names no event and calls nothing due; the
  meeting is ``possibly_due`` with the anchor's revision, ``stopped_clock_at`` stays null, the
  advance records ``paused_for_event_id`` (no ``stopped_by_event_id``), nothing is surfaced, no
  render is told the meeting is due, #148's ``time`` detection finds no time claim in the
  committed text, and the turn report marks ``establish_time``: the next turn's narrator (never
  Jenni's render) is steered to establish the current time.
- **No refinement:** the next advance passes the pause (no second pause for that anchor state),
  stops factually at +180 (the lower end reaches 16:00: ``due``, ``surfaced`` by the regenerated
  render) and, the clock past +210 with the meeting not completed, it is ``missed``.
- **A new partial refinement** ("it's around four" after the pause: [15:45, 16:01)) still leaves
  the meeting inside the range: re-planned ``possibly_due`` (due at +76), and the next advance
  pauses once more, for the new revision.
- **Refinement variants**, each from the range [14:00, 18:00) with the meeting ``possibly_due``
  from the start: Jenni's phone reads 14:20 → ``pending`` again (due at +100), and a later
  advance stops once, factually, at 16:00; 16:02 → ``due`` (its factual stop), ``surfaced`` at
  the next turn; 16:20 → surfaced as overdue, within grace, not ``missed``; 16:45 and 17:10 →
  ``missed``.
- **Complements:** with the story's ``stop_at_due_events`` off, or the profile's
  ``clock.scheduled_stops`` off, no pause occurs: the advance crosses +61 and +180, the meeting
  is surfaced as overdue (no ``stopped_clock_at``, no paused or stopped advance), and the next
  turn past +210 makes it ``missed``.
"""

from __future__ import annotations

import json
from typing import Any

import httpx2 as httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.config import Settings
from epistrel.core import world_clock
from epistrel.orchestrator.models.stub import StubAdapter
from epistrel.orchestrator.structured import prompt as prompts
from tests import clock_support as ck
from tests import effect_support as es
from tests import multi_support as ms
from tests import perception_support as ps
from tests.auth_support import auth_settings
from tests.event_support import rows
from tests.respond_support import body, respond, respond_client
from tests.story_support import WRITE
from tests.structured_turn_support import beat, structured_story, turn_report

ONE, TWO, THREE = 13 * 60, 14 * 60, 15 * 60
SIX = 18 * 60
WANDER = "We wander the market for a couple of hours."
WANDER_ON = "We wander the market; skip ahead a few hours."
"""#199 (FR-CLOCK-8): a player line that invites a skip longer than the unprompted cap."""
DRIFT = "They drift between the stalls, tasting apricots."
"""The render regenerated under the pause: no time, nothing due."""
INTERRUPTED = "Jenni glances at her phone: her meeting. It is four."
SENTINEL = "watching the sunset over the river"
"""What the unpaused render narrates past the pause: never committed."""


@pytest.fixture
def stub_settings(pg_url: str) -> Settings:
    return auth_settings(pg_url, model_adapter="stub")


async def market(
    client: httpx.AsyncClient,
    engine: AsyncEngine,
    *,
    lo: int = ONE,
    hi: int = THREE,
    stops: bool = True,
) -> tuple[str, dict[str, str], str]:
    """Cal and Jenni at the market, the clock anchored only to ``[lo, hi)`` at offset 0, and
    Jenni's 16:00 meeting: the story, its characters and the meeting's id."""
    story, ids = await structured_story(client, "Cal", "Jenni")
    place = await ps.place_entity(engine, story, "the market")
    for who in ids.values():
        await ps.put(engine, story, who, place)
    await ck.set_clock(engine, story, "on", stops=stops)
    await ck.anchor_range(engine, story, lo, hi)
    await ms.per_character(engine, story)
    created = await client.post(
        f"/stories/{story}/scheduled-events",
        json={
            "proposition": "Jenni's meeting",
            "planned": "16:00",
            "participants": [ids["Jenni"]],
            "owner": "world",
        },
        headers=WRITE,
    )
    assert created.status_code == 201, created.text
    return story, ids, created.json()["event"]["event_id"]


def skip(minutes: int, reason: str = "they wander") -> dict[str, Any]:
    return {"kind": "time_advance", "state": "world_clock", "minutes": minutes, "reason": reason}


async def turn(
    client: httpx.AsyncClient,
    stub: StubAdapter,
    story: str,
    ids: dict[str, str],
    *jenni: dict[str, Any],
    say: str = "Shall we?",
    regenerated: str | None = None,
) -> dict[str, Any]:
    """One turn: Cal says ``say``, Jenni's render answers ``jenni``'s beats (and a
    regeneration answers ``regenerated``)."""
    stub.reset()
    ms.renders_for(stub, "Jenni", ms.render(*jenni))
    if regenerated is not None:
        stub.script("regeneration", [json.dumps({"text": regenerated})])
    answer = await respond(
        client, story, body(ids["Cal"], say, render_character_ids=[ids["Jenni"]])
    )
    assert answer.status_code == 200, answer.text
    found: dict[str, Any] = answer.json()
    return found


async def wander(
    client: httpx.AsyncClient, stub: StubAdapter, story: str, ids: dict[str, str]
) -> dict[str, Any]:
    """Cal: "we wander the market for a couple of hours"; Jenni's render declares the 120
    minutes and narrates past the pause (``SENTINEL``), regenerated as ``DRIFT``."""
    return await turn(
        client,
        stub,
        story,
        ids,
        beat("do", "They set off between the stalls.", effects=[skip(120)]),
        beat("do", f"They end up {SENTINEL}."),
        say=WANDER,
        regenerated=DRIFT,
    )


async def narrate(
    client: httpx.AsyncClient, stub: StubAdapter, story: str, ids: dict[str, str], text: str
) -> dict[str, Any]:
    """A turn whose narration is ``text`` (Jenni says something timeless; no time passes)."""
    stub.reset()
    es.narrator(stub, es.render(beat("narration", text)))
    ms.renders_for(stub, "Jenni", es.render(beat("say", "Mind the step.")))
    answer = await respond(client, story, body(ids["Cal"], None, do="looks around"))
    assert answer.status_code == 200, answer.text
    found: dict[str, Any] = answer.json()
    return found


async def meeting_of(engine: AsyncEngine, story: str, meeting: str) -> Any:
    (found,) = [e for e in await ck.events(engine, story) if e.event_id == meeting]
    return found


async def advances(engine: AsyncEngine, story: str) -> list[tuple[str, int, Any, Any]]:
    """The clock's recorded advances: source, minutes, and the event each stopped or paused
    for."""
    found = await rows(
        engine,
        "SELECT c.source, c.delta, c.stopped_by_event_id, c.paused_for_event_id FROM "
        "state_changes c JOIN state_definitions d ON d.id = c.state_id WHERE c.story_id = :s "
        "AND d.name = 'world_clock' AND c.membership = 'active' "
        "ORDER BY c.narrative_turn, c.slot_k, c.beat, c.step",
        s=story,
    )
    return [(r.source, int(r.delta), r.stopped_by_event_id, r.paused_for_event_id) for r in found]


async def revision(engine: AsyncEngine, story: str) -> int:
    async with engine.connect() as conn:
        return await world_clock.anchor_revision(conn, story)


def offset(found: dict[str, Any]) -> int:
    return int(found["clock"]["after"]["offset"])


def no_due_line(stub: StubAdapter) -> None:
    """No prompt of the turn tells a render the meeting is due."""
    for call in stub.calls:
        sent = "\n".join(m.content for m in call.messages)
        assert "Scheduled event now due" not in sent
        assert "is due then" not in sent


@pytest.mark.integration
@pytest.mark.part("M6", "FR-CLOCK-3,9,17,18")
async def test_s_anchor_meeting(
    stub_settings: Settings,
    engine: AsyncEngine,
    stops_delivered: None,
    drained_embedding_jobs: None,
) -> None:
    """See the module docstring."""
    async with respond_client(stub_settings, engine) as (client, epistrel):
        stub = epistrel.models.stub

        # -- pause, not interruption -------------------------------------------------------------
        story, ids, meeting = await market(client, engine)
        event = await meeting_of(engine, story, meeting)
        assert (event.status.value, event.planned, event.at) == ("pending", 180, 16 * 60)
        rev = await revision(engine, story)
        found = await wander(client, stub, story, ids)
        assert offset(found) == 61  # [14:01, 16:01): the upper end passes 16:00
        regenerated = es.regenerations(stub)
        assert len(regenerated) == 1
        assert "Time pauses at" in regenerated[0]
        assert "Jenni's meeting" not in regenerated[0]
        no_due_line(stub)
        texts = [b["text"] for b in found["beats"] if b["kind"] in ("beat_do", "beat_say")]
        assert SENTINEL not in " ".join(texts)
        event = await meeting_of(engine, story, meeting)
        assert (event.status.value, event.refinement_pause_anchor_rev) == ("possibly_due", rev)
        assert event.stopped_clock_at is None  # not the meeting's factual stop
        # Created, then held at 16:00 (still pending), then paused.
        assert await ck.statuses(engine, story, meeting) == ["pending", "pending", "possibly_due"]
        recorded = await advances(engine, story)
        assert recorded[-1] == ("declared_skip", 60, None, meeting)
        assert all(stopped is None for _, _, stopped, _ in recorded)
        report = await turn_report(engine, story)
        assert report["clock"]["stops"]["establish_time"] is True
        assert [s["pause"] for s in report["clock"]["stops"]["stops"]] == [True]
        # #148's time detection finds no time claim on the committed text (the beats kept and
        # the composed message; the dropped beat's "sunset" was detected before the cut).
        kept = {b["beat"] for b in found["beats"] if b["kind"] in ("beat_do", "beat_say")}
        timed = [
            c
            for c in report["detection"]["claims"]
            if c["category"] == "time" and (c["span"][0] is None or c["span"][0] in kept)
        ]
        assert timed == []
        assert any(c["category"] == "time" for c in report["detection"]["claims"])

        # The next turn's narrator is steered to establish the time; Jenni's render never is.
        await narrate(client, stub, story, ids, "The market hums around them.")
        narrated = es.prompts(stub, "NarratorRender")
        assert narrated
        assert all(prompts.TIME_STEERING in p for p in narrated)
        assert all(prompts.TIME_STEERING not in p for p in ms.render_prompts(stub, "Jenni"))

        # -- no refinement: passes the pause, stops at +180, missed past +210 --------------------
        found = await turn(
            client,
            stub,
            story,
            ids,
            beat("do", "They keep browsing.", effects=[skip(150, "they browse")]),
            regenerated=INTERRUPTED,
        )
        assert offset(found) == 180  # the lower end reaches 16:00: its factual stop
        assert len(es.regenerations(stub)) == 1
        event = await meeting_of(engine, story, meeting)
        assert (event.status.value, event.stopped_clock_at) == ("surfaced", 180)
        assert (await advances(engine, story))[-1][2:] == (meeting, None)
        assert [
            s["pause"] for s in (await turn_report(engine, story))["clock"]["stops"]["stops"]
        ] == [False]
        found = await turn(
            client,
            stub,
            story,
            ids,
            beat("say", "Let's eat.", effects=[skip(40, "they eat")]),
            say="Let's eat, and talk later.",  # #199: the player invites the skip
        )
        assert offset(found) > 210
        assert (await meeting_of(engine, story, meeting)).status.value == "missed"
        assert await ck.statuses(engine, story, meeting) == [
            "pending",
            "pending",
            "possibly_due",
            "due",
            "surfaced",
            "missed",
        ]
        # One pause for that anchor state: only the first advance paused.
        assert [p for *_, p in await advances(engine, story) if p is not None] == [meeting]

        # -- a new partial refinement may pause once more ------------------------------------------
        story, ids, meeting = await market(client, engine)
        await wander(client, stub, story, ids)
        rev = await revision(engine, story)
        await narrate(client, stub, story, ids, "It's about 4 pm, the stallholders say.")
        assert await revision(engine, story) == rev + 1
        event = await meeting_of(engine, story, meeting)
        assert (event.status.value, event.planned) == ("possibly_due", 76)  # [15:45, 16:01)
        before = await ck.offset(engine, story)
        assert before < 76
        found = await turn(
            client,
            stub,
            story,
            ids,
            beat("do", "They walk on.", effects=[skip(10, "they walk")]),
            say="Wait ten minutes.",  # three words: no pacing of its own
            regenerated=DRIFT,
        )
        assert offset(found) == before  # paused at once, for the new anchor state
        event = await meeting_of(engine, story, meeting)
        assert (event.status.value, event.refinement_pause_anchor_rev) == (
            "possibly_due",
            rev + 1,
        )
        assert event.stopped_clock_at is None

        # -- refinement variants from [14:00, 18:00) ---------------------------------------------
        # Anchored [12:30, 16:30) at 0 and moved on 90 minutes between turns (a consumer's
        # advance): [14:00, 18:00) at +90, the meeting possibly due since it was created.
        variants = {
            "14:20": ["pending", "possibly_due", "pending"],
            "16:02": ["pending", "possibly_due", "due"],
            "16:20": ["pending", "possibly_due", "due", "surfaced"],
            "16:45": ["pending", "possibly_due", "due", "surfaced", "missed"],
            "17:10": ["pending", "possibly_due", "due", "surfaced", "missed"],
        }
        for reading, expected in variants.items():
            story, ids, meeting = await market(client, engine, lo=TWO - 90, hi=SIX - 90)
            event = await meeting_of(engine, story, meeting)
            assert (event.status.value, event.planned) == ("possibly_due", 210), reading
            await ck.advance(engine, story, 90)
            await narrate(client, stub, story, ids, f"Jenni checks her phone: it's {reading}.")
            assert await ck.statuses(engine, story, meeting) == expected, reading
            event = await meeting_of(engine, story, meeting)
            if reading == "14:20":
                # Back to pending, due at +190; a later advance stops once, factually, at 16:00.
                assert event.planned == 190
                found = await turn(
                    client,
                    stub,
                    story,
                    ids,
                    beat("do", "They browse.", effects=[skip(120, "they browse")]),
                    regenerated=INTERRUPTED,
                )
                assert offset(found) == 190
                event = await meeting_of(engine, story, meeting)
                assert (event.status.value, event.stopped_clock_at) == ("surfaced", 190)
                assert [s for *_, s in await advances(engine, story) if s is not None] == []
            elif reading == "16:02":
                # Due now: its one factual stop, surfaced to the next turn.
                assert (event.planned, event.stopped_clock_at) == (88, 90)
                await turn(client, stub, story, ids, beat("say", "Oh!"))
                (prompt,) = ms.render_prompts(stub, "Jenni")
                assert "Scheduled event now due: Jenni's meeting" in prompt
                assert (await meeting_of(engine, story, meeting)).status.value == "surfaced"
            else:
                assert event.stopped_clock_at is None, reading  # overdue: no stop

        # -- complements: stops off (the story's switch, the profile's flag) --------------------
        for switch in ("story", "profile"):
            story, ids, meeting = await market(client, engine, stops=switch != "profile")
            if switch == "story":
                patched = await client.patch(
                    f"/stories/{story}/clock", json={"stop_at_due_events": False}, headers=WRITE
                )
                assert patched.status_code == 200, patched.text
            found = await turn(
                client,
                stub,
                story,
                ids,
                beat("do", "They wander for hours.", effects=[skip(200)]),
                # #199: over the unprompted cap, so the player invites the skip.
                say=WANDER_ON,
            )
            assert offset(found) == 201, switch  # Cal's line paces a minute
            assert es.regenerations(stub) == [], switch
            assert await ck.statuses(engine, story, meeting) == [
                "pending",
                "pending",
                "due",
                "surfaced",
            ], switch
            event = await meeting_of(engine, story, meeting)
            assert (event.stopped_clock_at, event.refinement_pause_anchor_rev) == (None, None)
            assert [a for a in await advances(engine, story) if a[2] or a[3]] == [], switch
            await turn(
                client,
                stub,
                story,
                ids,
                beat("say", "Look at the time.", effects=[skip(15)]),
                say="We linger till later.",  # #199: the player invites the skip
            )
            assert (await meeting_of(engine, story, meeting)).status.value == "missed", switch
