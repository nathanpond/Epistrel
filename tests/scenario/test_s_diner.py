"""S-DINER: the clock stops the rush (docs/04 §7; FR-CLOCK-6,9,10,11,15,16,19), its M6 parts.

Every S-DINER (M6) function lives in this file (traceability counts a test ID per milestone in
one file): #196's lifecycle and stop clauses (``__lifecycle``) and the scheduled-events API
(``__api``); #198 adds report vs create (``__report_vs_create``) and #200 the belief clauses.

A diner at 11:00 (the anchor fixes the minute): Cal (the player) and Jenni sit down; Jenni's
meeting is a canon event at 17:00 (created between turns through the API; #198 creates it from
her own statement), involving her.

- ``test_s_diner__lifecycle``: twenty short-to-medium exchanges from 11:00 leave the clock at
  11:20-11:40; with the meeting at 11:30, a 60-word exchange from 11:29 is clamped at 11:30 (no
  re-render) and the meeting surfaced to the next beat; "we lose track of time and talk until
  6" with stops on stops at 17:00, the render is regenerated to end at the interruption and
  nothing narrated after 17:00 is committed, the meeting ``pending → due → surfaced``; with the
  story's ``stop_at_due_events`` off it is surfaced as overdue at 18:00, and with the profile's
  ``clock.scheduled_stops`` off the clock passes 17:00 too; a story created without the setting
  behaves as on. Lifecycle: Jenni leaves after it surfaced → ``completed``, and a later skip
  passes 17:00 freely; ignoring it → ``missed`` past 17:30, and a later skip is not stopped;
  rescheduled to 19:00 → ``pending`` again, and it stops the clock once more.
- ``test_s_diner__api``: the list for each viewpoint (a character lists only what it holds),
  create, reschedule and cancel between turns, and their keyed replays.
- ``test_s_diner__report_vs_create`` (#198): a report of the boss's 18:00 meeting is never a
  change (a sincere error with an antecedent, a hallucination without; a non-owner's "pushed
  it" refused), shared owners, and Jenni's own errand created, reported and changed from her
  renders.
"""

from __future__ import annotations

import json
from typing import Any

import httpx2 as httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.config import Settings
from epistrel.core.epistemic import transitions
from epistrel.core.propositions import store
from epistrel.core.propositions.model import Granularity, Instant, Proposition
from epistrel.core.propositions.vocabulary import SCHEDULED
from epistrel.core.stories import STORY_TABLES
from epistrel.core.world_clock import stops
from epistrel.orchestrator.models.stub import StubAdapter
from tests import clock_support as ck
from tests import effect_support as es
from tests import multi_support as ms
from tests import partition_support as pt
from tests import perception_support as ps
from tests.auth_support import auth_settings
from tests.event_support import rows
from tests.respond_support import body, respond, respond_client
from tests.story_support import WRITE
from tests.structured_turn_support import beat, claim, structured_story, turn_report

ELEVEN = 11 * 60
MEETING = 17 * 60 - ELEVEN
"""17:00, as an offset from 11:00."""
SENTINEL = "dancing on the tables"
"""What the unstopped render narrates after 17:00: never committed."""
INTERRUPTED = "Jenni glances at her phone: her meeting. It is five."


@pytest.fixture
def stub_settings(pg_url: str) -> Settings:
    return auth_settings(pg_url, model_adapter="stub")


async def diner(
    client: httpx.AsyncClient,
    engine: AsyncEngine,
    *,
    planned: int = MEETING,
    stops: bool = True,
    partitions: bool = False,
    others: tuple[str, ...] = (),
) -> tuple[str, dict[str, str], str]:
    """The diner at 11:00 with Jenni's meeting at ``planned`` (and per-character partitions
    when ``partitions``; ``others`` at the next table): the story, its characters and the
    meeting's id."""
    story, ids = await structured_story(client, "Cal", "Jenni", *others)
    place = await ps.place_entity(engine, story, "the diner")
    for who in ids.values():
        await ps.put(engine, story, who, place)
    await ck.set_clock(engine, story, "on", stops=stops)
    await ck.anchor_at(engine, story, ELEVEN)
    if partitions:
        await ms.per_character(
            engine,
            story,
            character_knowledge={"partitions": True},
            perception={"level": "co_location"},
        )
    else:
        await ms.per_character(engine, story)
    created = await client.post(
        f"/stories/{story}/scheduled-events",
        json={
            "proposition": "Jenni's meeting",
            "planned": planned,
            "participants": [ids["Jenni"]],
            "owner": "world",
        },
        headers=WRITE,
    )
    assert created.status_code == 201, created.text
    return story, ids, created.json()["event"]["event_id"]


async def turn(
    client: httpx.AsyncClient,
    stub: StubAdapter,
    story: str,
    ids: dict[str, str],
    *jenni: dict[str, Any],
    say: str = "Tell me more.",
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


KEEP_TALKING = "Let's keep talking."
"""A three-word line (paced like "Tell me more.") inviting the skip that follows (#199)."""


def skip(minutes: int, reason: str = "they talk") -> dict[str, Any]:
    return {"kind": "time_advance", "state": "world_clock", "minutes": minutes, "reason": reason}


def words(n: int) -> str:
    return " ".join(["word"] * (n - 1) + ["end."])


async def event_of(engine: AsyncEngine, story: str, event_id: str) -> Any:
    (found,) = [e for e in await ck.events(engine, story) if e.event_id == event_id]
    return found


async def stopped_by(engine: AsyncEngine, story: str) -> list[tuple[str, int, str | None]]:
    """The clock's recorded advances: source, minutes and the event that stopped each."""
    found = await rows(
        engine,
        "SELECT c.source, c.delta, c.stopped_by_event_id FROM state_changes c JOIN "
        "state_definitions d ON d.id = c.state_id WHERE c.story_id = :s AND "
        "d.name = 'world_clock' AND c.membership = 'active' "
        "ORDER BY c.narrative_turn, c.slot_k, c.beat, c.step, c.id",
        s=story,
    )
    return [(r.source, int(r.delta), r.stopped_by_event_id) for r in found]


async def leak(engine: AsyncEngine, story: str, needle: str) -> list[str]:
    """The story tables whose rows carry ``needle`` (an audit event, the rejected render's
    record, is the one place a rejected text is kept: FR-REPAIR-3)."""

    found = []
    async with engine.connect() as conn:
        for table in STORY_TABLES:
            where = "t.story_id = :s"
            if table.name == "events":
                where += " AND t.category <> 'audit'"
            hit = await conn.scalar(
                text(
                    f'SELECT count(*) FROM "{table.name}" t WHERE {where} '  # noqa: S608
                    "AND strpos(lower(t::text), :n) > 0"
                ),
                {"s": story, "n": needle.lower()},
            )
            if hit:
                found.append(table.name)
    return found


async def talk_until_six(
    client: httpx.AsyncClient,
    engine: AsyncEngine,
    stub: StubAdapter,
    story: str,
    ids: dict[str, str],
) -> dict[str, Any]:
    """From 16:45, "we lose track of time and talk until 6": Jenni's render declares the skip
    and narrates past 17:00 (``SENTINEL``)."""
    await ck.advance(engine, story, 345)  # 16:45
    return await turn(
        client,
        stub,
        story,
        ids,
        beat("say", "We could talk forever.", effects=[skip(75)]),
        beat("do", f"They end up {SENTINEL}."),
        say="We lose track of time and talk until 6.",
        regenerated=INTERRUPTED,
    )


async def chat(
    client: httpx.AsyncClient,
    stub: StubAdapter,
    story: str,
    ids: dict[str, str],
    minutes: int | None = None,
) -> dict[str, Any]:
    """A turn whose render declares a ``minutes`` skip (none: a short exchange). #199
    (FR-CLOCK-8): Cal's line invites it (they keep talking), so the skip is the player's, not
    one Jenni's render starts on its own."""
    effects = [] if minutes is None else [skip(minutes, "time passes")]
    return await turn(
        client, stub, story, ids, beat("say", "Sure.", effects=effects), say=KEEP_TALKING
    )


@pytest.mark.integration
@pytest.mark.part("M6", "FR-CLOCK-6,9,16")
async def test_s_diner__lifecycle(
    stub_settings: Settings,
    engine: AsyncEngine,
    stops_delivered: None,
    drained_embedding_jobs: None,
) -> None:
    async with respond_client(stub_settings, engine) as (client, epistrel):
        stub = epistrel.models.stub

        # Pacing: twenty short-to-medium exchanges from 11:00 leave the clock at 11:20-11:40.
        story, ids, _ = await diner(client, engine)
        for n in range(20):
            found = await turn(
                client,
                stub,
                story,
                ids,
                beat("say", words(12 + n)),
                say="So what happened next with the two of them?",
            )
        assert 20 <= found["clock"]["after"]["offset"] <= 40, found["clock"]

        # No skip, still stopped: the meeting at 11:30, a 60-word exchange from 11:29: its
        # pacing increment is clamped at 11:30 (no re-render) and the meeting surfaced to the
        # next beat.
        story, ids, meeting = await diner(client, engine, planned=30)
        await ck.advance(engine, story, 29)
        found = await turn(client, stub, story, ids, beat("say", words(60)))
        assert found["clock"]["after"]["offset"] == 30
        assert es.regenerations(stub) == []
        assert (await event_of(engine, story, meeting)).stopped_clock_at == 30
        assert (await stopped_by(engine, story))[-1] == ("pacing", 1, meeting)
        await turn(client, stub, story, ids, beat("say", "I should go."))
        (prompt,) = ms.render_prompts(stub, "Jenni")
        assert "Scheduled event now due: Jenni's meeting (planned for 11:30)" in prompt
        assert await ck.statuses(engine, story, meeting) == ["pending", "due", "surfaced"]

        # "We lose track of time and talk until 6", stops on (the story's default setting):
        # the advance stops at 17:00, the render is regenerated to end at the interruption, and
        # nothing narrated after 17:00 is committed.
        story, ids, meeting = await diner(client, engine)
        clock = (await client.get(f"/stories/{story}/clock", headers=WRITE)).json()
        assert clock["settings"]["stop_at_due_events"] is True  # a story created without it
        found = await talk_until_six(client, engine, stub, story, ids)
        assert found["clock"]["after"]["offset"] == MEETING
        assert len(es.regenerations(stub)) == 1
        texts = [b["text"] for b in found["beats"] if b["kind"] in ("beat_do", "beat_say")]
        assert texts == [INTERRUPTED]
        assert await leak(engine, story, SENTINEL) == []
        assert await ck.statuses(engine, story, meeting) == ["pending", "due", "surfaced"]
        event = await event_of(engine, story, meeting)
        assert (event.status.value, event.stopped_clock_at) == ("surfaced", MEETING)
        # Cal's line paces a minute; Jenni's skip is the stopped advance, cut at 17:00.
        assert await stopped_by(engine, story) == [
            ("consumer", 345, None),
            ("pacing", 1, None),
            ("declared_skip", 14, meeting),
        ]
        report = await turn_report(engine, story)
        assert report["clock"]["stops"]["cutoff"] == MEETING
        # Jenni leaves after it surfaced: completed, and a later skip passes freely.
        elsewhere = await ps.place_entity(engine, story, "the office")
        await ps.put(engine, story, ids["Jenni"], elsewhere)
        await chat(client, stub, story, ids)
        assert (await event_of(engine, story, meeting)).status.value == "completed"
        found = await chat(client, stub, story, ids, 90)
        assert found["clock"]["after"]["offset"] == MEETING + 90
        assert es.regenerations(stub) == []

        # Complement: she ignores it: missed once the clock passes 17:30 (Jenni, present,
        # observes it: an observation transition citing the missed step's event, ADR-168), and
        # a later skip is not stopped; rescheduled to 19:00, it is pending again and stops the
        # clock once more.
        story, ids, meeting = await diner(client, engine, partitions=True)
        await talk_until_six(client, engine, stub, story, ids)
        await chat(client, stub, story, ids, 20)
        assert (await event_of(engine, story, meeting)).status.value == "surfaced"  # 17:21
        found = await chat(client, stub, story, ids, 20)
        assert (await event_of(engine, story, meeting)).status.value == "missed"
        (observed,) = await rows(
            engine,
            "SELECT t.kind, e.payload->>'status' AS status FROM "
            "belief_transitions t JOIN events e ON e.id = t.antecedent_ref->>'id' "
            "WHERE t.story_id = :s AND t.holder_character_id = :h AND t.kind = 'observation' "
            "AND e.type = 'scheduled_event'",
            s=story,
            h=ids["Jenni"],
        )
        assert (observed.kind, observed.status) == ("observation", "missed")
        assert found["clock"]["after"]["offset"] > MEETING + 30
        found = await chat(client, stub, story, ids, 30)
        assert es.regenerations(stub) == []
        rescheduled = await client.patch(
            f"/stories/{story}/scheduled-events/{meeting}",
            json={"planned": "19:00"},
            headers=WRITE,
        )
        assert rescheduled.status_code == 200, rescheduled.text
        assert rescheduled.json()["event"]["status"] == "pending"
        assert rescheduled.json()["event"]["stopped_clock_at"] is None
        found = await turn(
            client,
            stub,
            story,
            ids,
            beat("say", "Another coffee?", effects=[skip(120)]),
            say=KEEP_TALKING,
            regenerated=INTERRUPTED,
        )
        assert found["clock"]["after"]["offset"] == 8 * 60
        assert len(es.regenerations(stub)) == 1
        assert await ck.statuses(engine, story, meeting) == [
            "pending",
            "due",
            "surfaced",
            "missed",
            "pending",
            "due",
            "surfaced",
        ]

        # With the story's stop_at_due_events off, the meeting is surfaced as overdue past
        # 17:00 and the clock passes it; with the profile's clock.scheduled_stops off, too.
        for switch in ("story", "profile"):
            story, ids, meeting = await diner(client, engine, stops=switch != "profile")
            if switch == "story":
                patched = await client.patch(
                    f"/stories/{story}/clock", json={"stop_at_due_events": False}, headers=WRITE
                )
                assert patched.status_code == 200, patched.text
            found = await talk_until_six(client, engine, stub, story, ids)
            assert found["clock"]["after"]["offset"] == 345 + 1 + 75, switch
            assert es.regenerations(stub) == [], switch
            assert await ck.statuses(engine, story, meeting) == ["pending", "due", "surfaced"]
            event = await event_of(engine, story, meeting)
            assert (event.status.value, event.stopped_clock_at) == ("surfaced", None), switch
            assert [s for s in await stopped_by(engine, story) if s[2] is not None] == []


async def versions(engine: AsyncEngine, story: str) -> int:
    """How many ``scheduled_events`` rows (versions) the story has."""
    (found,) = await rows(
        engine, "SELECT count(*) AS n FROM scheduled_events WHERE story_id = :s", s=story
    )
    return int(found.n)


@pytest.mark.integration
@pytest.mark.part("M6", "FR-CLOCK-9")
async def test_s_diner__api(
    stub_settings: Settings,
    engine: AsyncEngine,
    stops_delivered: None,
    drained_embedding_jobs: None,
) -> None:
    """The scheduled-events API: ``POST`` creates a canon event between turns (the canon
    ``scheduled`` fact and its ``pending`` row, at an inter-turn slot), ``GET ?viewpoint=``
    lists what the viewpoint holds (omniscient: every canon event; a character: the canon
    events it participates in and its belief-only schedules, labelled ``belief``), ``PATCH``
    reschedules (back to ``pending``) and ``DELETE`` cancels; each write's keyed replay answers
    the stored result and writes nothing. Complements: a character viewpoint does not list an
    event it does not hold, a belief-only schedule never stops the clock (it is not an event),
    an unknown event is 404, a cancelled one cannot change, an unknown participant is 422 and
    the clock off is 409 ``module_off``."""

    async with respond_client(stub_settings, engine) as (client, _):
        story, ids, _ = await diner(client, engine, partitions=True, others=("Marcus",))
        cal, jenni, marcus = ids["Cal"], ids["Jenni"], ids["Marcus"]
        url = f"/stories/{story}/scheduled-events"

        # Create, keyed: Marcus's dinner reservation at 19:30, set by the author.
        keyed = {**WRITE, "Idempotency-Key": "dinner-1"}
        reservation = {
            "proposition": "the dinner reservation",
            "planned": "19:30",
            "participants": [marcus],
            "owner": "author",
        }
        created = await client.post(url, json=reservation, headers=keyed)
        assert created.status_code == 201, created.text
        event = created.json()["event"]
        assert (event["status"], event["planned"], event["owner"]) == (
            "pending",
            8 * 60 + 30,
            "author",
        )
        assert event["name"] == "the dinner reservation"
        before = await versions(engine, story)
        replay = await client.post(url, json=reservation, headers=keyed)
        assert replay.status_code == 201
        assert replay.headers["Idempotent-Replay"] == "true"
        assert replay.json()["event"] == event
        assert await versions(engine, story) == before
        dinner = event["event_id"]
        (fact,) = await rows(
            engine,
            "SELECT provenance, slot_k FROM facts WHERE story_id = :s AND proposition_id = :p",
            s=story,
            p=dinner,
        )
        assert fact.provenance == "author"
        assert fact.slot_k >= 1  # an inter-turn slot

        # Marcus wrongly believes Jenni's meeting is at 16:00: a belief-only schedule.
        async with engine.begin() as conn:
            wrong = await store.add_entity(conn, story, "event", "Jenni's meeting at four")
            zero = int(
                (
                    await conn.execute(
                        text("SELECT story_minute_zero FROM stories WHERE id = :s"), {"s": story}
                    )
                ).scalar_one()
            )
        await pt.believe(
            engine,
            story,
            marcus,
            Proposition(
                subject_id=jenni,
                predicate_id=SCHEDULED,
                object_id=wrong,
                valid_time=Instant.of(zero + 5 * 60, Granularity.MINUTE),
            ),
        )

        async def listed(viewpoint: str) -> list[tuple[str | None, str, str | None]]:
            found = await client.get(url, params={"viewpoint": viewpoint}, headers=WRITE)
            assert found.status_code == 200, found.text
            assert found.json()["output_viewpoint"] == viewpoint
            return [(e["name"], e["label"], e["status"]) for e in found.json()["events"]]

        assert await listed("omniscient") == [
            ("Jenni's meeting", "canon", "pending"),
            ("the dinner reservation", "canon", "pending"),
        ]
        assert await listed(f"character:{jenni}") == [("Jenni's meeting", "canon", "pending")]
        # Complement: Marcus does not hold Jenni's meeting (his own wrong idea of it is a
        # belief), and Cal holds neither.
        assert await listed(f"character:{marcus}") == [
            ("the dinner reservation", "canon", "pending"),
            ("Jenni's meeting at four", "belief", None),
        ]
        assert await listed(f"character:{cal}") == []
        # A belief-only schedule never stops the clock: it is no event.
        async with engine.connect() as conn:
            assert await stops.due_between(conn, story, 0, 330, {cal, jenni, marcus}, None) == []
        unresolved = await client.get(url, params={"viewpoint": "character:nobody"}, headers=WRITE)
        assert unresolved.status_code == 422
        assert unresolved.json()["error"]["code"] == "viewpoint_unresolved"

        # Reschedule, keyed: pending at the new time.
        keyed = {**WRITE, "Idempotency-Key": "dinner-2"}
        moved = await client.patch(f"{url}/{dinner}", json={"planned": 600}, headers=keyed)
        assert moved.status_code == 200, moved.text
        assert (moved.json()["event"]["status"], moved.json()["event"]["planned"]) == (
            "pending",
            600,
        )
        before = await versions(engine, story)
        replay = await client.patch(f"{url}/{dinner}", json={"planned": 600}, headers=keyed)
        assert replay.headers["Idempotent-Replay"] == "true"
        assert await versions(engine, story) == before
        assert (
            await client.patch(
                f"{url}/01J9Z8Q4N5V6W7X8Y9Z0A1B2E6", json={"planned": 9}, headers=WRITE
            )
        ).status_code == 404

        # Cancel: cancelled, and a cancelled event cannot change any more.
        cancelled = await client.delete(f"{url}/{dinner}", headers=WRITE)
        assert cancelled.status_code == 200, cancelled.text
        assert cancelled.json()["event"]["status"] == "cancelled"
        again = await client.delete(f"{url}/{dinner}", headers=WRITE)
        assert again.status_code == 422
        assert again.json()["error"]["details"]["reason"] == "event_settled"
        assert await ck.statuses(engine, story, dinner) == ["pending", "pending", "cancelled"]

        # Refusals: an unknown participant; the clock off.
        bad = await client.post(
            url, json={**reservation, "participants": ["01J9Z8Q4N5V6W7X8Y9Z0A1B2E7"]}, headers=WRITE
        )
        assert bad.status_code == 422
        await ck.set_level(engine, story, None)
        off = await client.post(url, json=reservation, headers=WRITE)
        assert off.status_code == 409
        assert off.json()["error"]["code"] == "module_off"


SIX = 18 * 60 - ELEVEN
"""18:00, as an offset from 11:00: the boss's meeting."""
FIVE = MEETING
REPAIRED = "I have a meeting later."


def plan(text: str, event: str, time: str, **fields: Any) -> dict[str, Any]:
    """Jenni's ``say`` stating ``event``'s time (a declared ``scheduled`` claim)."""
    qualifiers = fields.pop("qualifiers", None)
    sketch = claim("I", "scheduled", event, time=time)
    if qualifiers:
        sketch["qualifiers"] = qualifiers
    return beat("say", text, propositions=[sketch], **fields)


async def statement_of(engine: AsyncEngine, story: str) -> dict[str, Any]:
    """The last turn's schedule statement verdict (its one ``scheduled`` claim)."""
    (found,) = [
        v for v in (await turn_report(engine, story))["verify"]["verdicts"] if "schedule" in v
    ]
    return dict(found)


async def told_five(engine: AsyncEngine, story: str, jenni: str, marcus: str, event: str) -> None:
    """Earlier, Marcus (her assistant) told Jenni her meeting is at five: her testimony belief
    (#162), the legitimate antecedent of a sincere error."""
    (row,) = await rows(
        engine,
        "SELECT p.object_id, s.story_minute_zero AS zero FROM propositions p JOIN stories s "
        "ON s.id = p.story_id WHERE p.id = :p",
        p=event,
    )
    await pt.transit(
        engine,
        story,
        jenni,
        Proposition(
            subject_id=jenni,
            predicate_id=SCHEDULED,
            object_id=row.object_id,
            valid_time=Instant.of(int(row.zero) + FIVE, Granularity.MINUTE),
        ),
        cause=transitions.Kind.TESTIMONY,
        source=marcus,
        outcome="accept",
    )


@pytest.mark.integration
@pytest.mark.part("M6", "FR-CLOCK-19,10")
async def test_s_diner__report_vs_create(
    stub_settings: Settings,
    engine: AsyncEngine,
    stops_delivered: None,
    drained_embedding_jobs: None,
) -> None:
    """Report vs create (FR-CLOCK-19,10; #198), through ``respond`` with per-character
    partitions:

    - Canon has Jenni's meeting at 18:00 (set by her boss: owner ``world``), and her assistant
      Marcus told her "five": "my meeting's at five" is a **report**, objectively false
      (``contradicted``, ``instant_identity``) and belief-consistent: a sincere error that
      stands; the meeting stays at 18:00, and a skip from 16:45 stops once, at 18:00 (never
      17:00).
    - The same statement with no antecedent is a hallucination, repaired; canon unchanged.
    - "I pushed it to seven" from Jenni is refused as a change (she is not the owner): a report,
      labelled false, and the meeting stays at 18:00; so is her declared ``reschedule``. The
      author (the API) can reschedule it, and once it is shared with her (``PATCH
      shared_owners``) her declared ``reschedule`` changes it; a shared owner that is no
      character is 422 ``viewpoint_unresolved``.
    - Her own errand, "I'll go to the bank at three" (with Cal), is a **create** from her render
      in the turn: a canon event owned by her with both of them as participants, and her
      ``discovery`` belief citing the canon fact (FR-EPI-14). A bare "the bank's at four" is a
      report (it does not reschedule); "I'll go at four instead" is a **change**: rescheduled to
      16:00, back to ``pending``; then "the bank's at four" is a true report."""

    async with respond_client(stub_settings, engine) as (client, epistrel):
        stub = epistrel.models.stub

        # Report, with an antecedent: a sincere error that stands; canon stays at 18:00.
        story, ids, meeting = await diner(
            client, engine, planned=SIX, partitions=True, others=("Marcus",)
        )
        await told_five(engine, story, ids["Jenni"], ids["Marcus"], meeting)
        found = await turn(
            client, stub, story, ids, plan("My meeting's at five.", "the meeting", "five o'clock")
        )
        verdict = await statement_of(engine, story)
        assert verdict["schedule"] == {
            "kind": "report",
            "event_id": meeting,
            "refused_change": False,
        }
        assert (verdict["kind"], verdict["rule"]) == ("contradicted", "instant_identity")
        assert verdict["speech"]["classification"] == "sincere_error"
        assert verdict["speech"]["belief_relation"] == "consistent"
        assert es.regenerations(stub) == []
        texts = [b["text"] for b in found["beats"] if b["kind"] == "beat_say"]
        assert "My meeting's at five." in texts
        assert await ck.statuses(engine, story, meeting) == ["pending"]
        assert (await event_of(engine, story, meeting)).planned == SIX
        # The clock stops once, at 18:00: never at 17:00.
        await ck.advance(engine, story, to=345)
        found = await turn(
            client,
            stub,
            story,
            ids,
            beat("say", "We could talk forever.", effects=[skip(150)]),
            regenerated=INTERRUPTED,
        )
        assert found["clock"]["after"]["offset"] == SIX
        assert [(s[0], s[2]) for s in await stopped_by(engine, story) if s[2] is not None] == [
            ("declared_skip", meeting)
        ]
        assert await ck.statuses(engine, story, meeting) == ["pending", "due", "surfaced"]

        # Report, with no antecedent: a hallucination, repaired; canon unchanged.
        story, ids, meeting = await diner(client, engine, planned=SIX, partitions=True)
        found = await turn(
            client,
            stub,
            story,
            ids,
            plan("My meeting's at five.", "the meeting", "five o'clock"),
            regenerated=REPAIRED,
        )
        verdict = await statement_of(engine, story)
        assert verdict["schedule"]["kind"] == "report"
        assert verdict["kind"] == "contradicted"
        assert verdict["speech"]["classification"] == "hallucination_repaired"
        assert len(es.regenerations(stub)) == 1
        assert await leak(engine, story, "meeting's at five") == []
        assert await ck.statuses(engine, story, meeting) == ["pending"]
        assert (await event_of(engine, story, meeting)).planned == SIX

        # "I pushed it to seven": refused as a change (the world owns it), a report, false.
        await turn(
            client,
            stub,
            story,
            ids,
            plan("I pushed it to seven.", "the meeting", "7 pm"),
            regenerated=REPAIRED,
        )
        verdict = await statement_of(engine, story)
        assert verdict["schedule"] == {
            "kind": "report",
            "event_id": meeting,
            "refused_change": True,
        }
        assert verdict["kind"] == "contradicted"
        assert await ck.statuses(engine, story, meeting) == ["pending"]
        assert (await event_of(engine, story, meeting)).planned == SIX
        # Her declared reschedule is refused the same way.
        moved = {
            "kind": "reschedule",
            "state": "world_clock",
            "event": "the meeting",
            "planned": "20:00",
            "reason": "she would rather go later",
        }
        await turn(
            client,
            stub,
            story,
            ids,
            beat("say", "Eight, then.", effects=[moved]),
            regenerated=REPAIRED,
        )
        verdict = await statement_of(engine, story)
        assert (verdict["schedule"]["refused_change"], verdict["kind"]) == (True, "contradicted")
        assert (await event_of(engine, story, meeting)).planned == SIX

        # Complement: the author reschedules the boss's meeting (the API acts as the author).
        url = f"/stories/{story}/scheduled-events/{meeting}"
        patched = await client.patch(url, json={"planned": "19:00"}, headers=WRITE)
        assert patched.status_code == 200, patched.text
        assert patched.json()["event"]["planned"] == SIX + 60
        # Shared with Jenni, her declared reschedule changes it.
        shared = await client.patch(url, json={"shared_owners": [ids["Jenni"]]}, headers=WRITE)
        assert shared.status_code == 200, shared.text
        assert shared.json()["event"]["shared_owners"] == [ids["Jenni"]]
        assert shared.json()["event"]["planned"] == SIX + 60
        unknown = await client.patch(
            url, json={"shared_owners": ["01J9Z8Q4N5V6W7X8Y9Z0A1B2E7"]}, headers=WRITE
        )
        assert unknown.status_code == 422
        assert unknown.json()["error"]["code"] == "viewpoint_unresolved"
        await turn(client, stub, story, ids, beat("say", "Eight, then.", effects=[moved]))
        verdict = await statement_of(engine, story)
        assert verdict["schedule"]["kind"] == "change"
        assert es.regenerations(stub) == []
        event = await event_of(engine, story, meeting)
        assert (event.status.value, event.planned, event.shared_owners) == (
            "pending",
            9 * 60,
            (ids["Jenni"],),
        )
        assert await ck.statuses(engine, story, meeting) == ["pending"] * 4

        # Create: her own errand, from her render in the turn, with Cal.
        story, ids, meeting = await diner(client, engine, partitions=True)
        await turn(
            client,
            stub,
            story,
            ids,
            plan(
                "I'll go to the bank at three, Cal's coming.",
                "the bank errand",
                "3 pm",
                qualifiers={"participants": "Cal"},
            ),
        )
        verdict = await statement_of(engine, story)
        assert verdict["schedule"]["kind"] == "create"
        assert verdict["permission"] == "permitted:own"
        (bank,) = [e for e in await ck.events(engine, story) if e.planned == 4 * 60]
        assert (bank.owner, bank.participants, bank.status.value) == (
            ids["Jenni"],
            (ids["Jenni"], ids["Cal"]),
            "pending",
        )
        (fact,) = await rows(
            engine,
            "SELECT f.id, f.provenance::text AS provenance FROM facts f "
            "WHERE f.story_id = :s AND f.proposition_id = :p",
            s=story,
            p=bank.event_id,
        )
        assert fact.provenance == "render"
        (held,) = await rows(
            engine,
            "SELECT kind::text AS kind, antecedent_ref FROM belief_transitions "
            "WHERE story_id = :s AND holder_character_id = :h",
            s=story,
            h=ids["Jenni"],
        )
        assert (held.kind, held.antecedent_ref) == (
            "discovery",
            {"table": "facts", "id": fact.id},
        )
        # A bare "the bank's at four" is a report: false, and it does not reschedule.
        await turn(
            client,
            stub,
            story,
            ids,
            plan("The bank's at four.", "the bank errand", "4 pm"),
            regenerated=REPAIRED,
        )
        verdict = await statement_of(engine, story)
        assert (verdict["schedule"]["kind"], verdict["kind"]) == ("report", "contradicted")
        assert await ck.statuses(engine, story, bank.event_id) == ["pending"]
        # "I'll go at four instead": a change, rescheduled to 16:00.
        await turn(
            client, stub, story, ids, plan("I'll go at four instead.", "the bank errand", "4 pm")
        )
        verdict = await statement_of(engine, story)
        assert verdict["schedule"] == {
            "kind": "change",
            "event_id": bank.event_id,
            "refused_change": False,
        }
        assert es.regenerations(stub) == []
        moved_bank = await event_of(engine, story, bank.event_id)
        assert (moved_bank.status.value, moved_bank.planned) == ("pending", 5 * 60)
        assert await ck.statuses(engine, story, bank.event_id) == ["pending", "pending"]
        # A change writes no canon: still the meeting and the one errand.
        assert {e.event_id for e in await ck.events(engine, story)} == {meeting, bank.event_id}
        # Now "the bank's at four" is a true report.
        await turn(client, stub, story, ids, plan("The bank's at four.", "the bank errand", "4 pm"))
        verdict = await statement_of(engine, story)
        assert (verdict["schedule"]["kind"], verdict["kind"]) == ("report", "supported")
        assert verdict["speech"]["classification"] == "sincere"
        assert es.regenerations(stub) == []
        assert await ck.statuses(engine, story, bank.event_id) == ["pending", "pending"]
