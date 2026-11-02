"""S-MOVIE: narrated skips, estimation and caps (docs/04 §7; FR-CLOCK-7,8,9,12,16, FR-STATE-13),
its M6 parts.

Every S-MOVIE (M6) function lives in this file (traceability counts a test ID per milestone in
one file): #196's stop clause (``__stop``); #199's estimation and cap clauses (``test_s_movie``).

- ``test_s_movie`` (#199): at 19:10 the player puts on a two-hour movie; at 19:40 the narrator
  narrates "They watch the rest of the movie in silence" with no advance → flagged (an
  undeclared skip), one ``time_estimation`` call (its prompt carries the sentence and the times
  the scene established) proposes 90 minutes with its reason → model-initiated, under the cap,
  narrating the established activity → accepted: the clock reads 21:10, recorded with
  ``source = estimate`` and its ``estimate_meta`` (reason, confidence, model, prompt hash,
  ``initiated_by``). Replay (the projections refolded from the log, the clock read again)
  reproduces 21:10 with no ``time_estimation`` call; regenerating the movie turn estimates
  again and records the new estimate on the new variant. Complement: the same sentence with a
  declared advance makes no estimation call. **Player-initiated**: "I fall asleep on the couch"
  at 23:30 → the narrator declares the night to 07:00, accepted as the player's (no cap
  applies, even a cap of 1 h). **Model-initiated**: a render that narrates falling asleep on
  the couch with nothing in the scene establishing sleep (its advance declared, under 24 h) →
  removed and repaired (``unprompted_skip``), and "an hour later, the doorbell rings" (no
  activity) → removed; neither asks the estimator. **Cap**: the scenario's cap of 1 h, written
  on the clock's definition as genesis will (#202 asserts the seeding) → the 90-minute estimate
  is removed and repaired, while the author still advances the clock through the State API; the
  story's ``PATCH /clock`` override (2 h) wins over it; ``0`` rejects the model-initiated skip
  without asking the estimator while the player's sleep still advances the clock.

- ``test_s_movie__stop``: at 23:30 the player types "I fall asleep on the couch"; the narrator
  declares an advance to 07:00 and narrates the night and the morning (the sunrise, the coffee
  the player made at seven, a State effect and a fact placed then). A canon delivery at the
  door at 06:00 (the world's, at the apartment) stops the advance at 06:00: the render is
  regenerated to end at the interruption (the knock), and none of what the original placed
  after 06:00 is committed, on any table; the player's input is recorded as stated; the delivery
  is ``surfaced`` (its one stop) and does not stop the next advance.
"""

from __future__ import annotations

import json
from typing import Any

import httpx2 as httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.config import Settings
from epistrel.core.projections import rebuild
from epistrel.core.propositions.vocabulary import CLOCK_ANCHOR
from epistrel.orchestrator.models.stub import StubAdapter
from tests import clock_support as ck
from tests import effect_support as es
from tests import multi_support as ms
from tests import perception_support as ps
from tests import state_support as ss
from tests.auth_support import auth_settings
from tests.event_support import rows
from tests.respond_support import body, respond, respond_client
from tests.scenario.test_s_diner import leak, skip, stopped_by
from tests.story_support import WRITE
from tests.structured_turn_support import beat, claim, structured_story, turn_report

HALF_PAST_ELEVEN = 23 * 60 + 30
SIX = 6 * 60 + 30
"""06:00, as an offset from 23:30."""
SEVEN = SIX + 60
WOKEN = "You're woken at six by a knock at the door: the delivery."


@pytest.fixture
def stub_settings(pg_url: str) -> Settings:
    return auth_settings(pg_url, model_adapter="stub")


@pytest.mark.integration
@pytest.mark.part("M6", "FR-CLOCK-9,16")
async def test_s_movie__stop(
    stub_settings: Settings,
    engine: AsyncEngine,
    states_delivered: None,
    stops_delivered: None,
    drained_embedding_jobs: None,
) -> None:
    async with respond_client(stub_settings, engine) as (client, epistrel):
        stub = epistrel.models.stub
        story, ids = await structured_story(client, "Cal", "Jenni")
        cal, jenni = ids["Cal"], ids["Jenni"]
        apartment = await ps.place_entity(engine, story, "the apartment")
        kitchen = await ps.place_entity(engine, story, "the kitchen")
        for who in (cal, jenni):
            await ps.put(engine, story, who, apartment)
        await ss.set_level(engine, story, "on")
        await ss.define_all(engine, story, es.suspicion())
        await ck.set_clock(engine, story, "on")
        await ck.anchor_at(engine, story, HALF_PAST_ELEVEN)
        # The budget pays for the narrator's render, its regeneration and their checks.
        await ms.per_character(engine, story, model_calls=20)
        created = await client.post(
            f"/stories/{story}/scheduled-events",
            json={"proposition": "the delivery", "planned": "06:00", "place": apartment},
            headers=WRITE,
        )
        assert created.status_code == 201, created.text
        delivery = created.json()["event"]
        assert (delivery["planned"], delivery["owner"]) == (SIX, "world")

        # "I fall asleep on the couch": the narrator declares the night through to 07:00 and
        # narrates the morning; the 06:00 delivery stops it.
        stub.reset()
        es.narrator(
            stub,
            es.render(
                beat(
                    "narration",
                    "You drift off on the couch.",
                    effects=[skip(SEVEN - 1, "the night passes")],
                ),
                beat("narration", "Sunrise paints the windows gold."),
                beat(
                    "narration",
                    "You make coffee at seven in the kitchen, and something stirs.",
                    propositions=[claim("Cal", "located_at", "the kitchen")],
                    effects=[es.effect("suspicion", delta=2, reason="a stir")],
                ),
            ),
        )
        ms.renders_for(stub, "Jenni", ms.render(beat("say", "Goodnight.")))
        stub.script("regeneration", [json.dumps({"text": WOKEN})])
        answer = await respond(
            client,
            story,
            body(cal, None, do="I fall asleep on the couch", render_character_ids=[jenni]),
        )
        assert answer.status_code == 200, answer.text
        found = answer.json()
        assert found["clock"]["after"]["offset"] == SIX
        assert len(es.regenerations(stub)) == 1
        narrated = [b["text"] for b in found["beats"] if b["kind"] == "beat_narration"]
        assert narrated == [WOKEN]
        # Nothing the original placed after 06:00 was committed, on any table: its sunrise, its
        # coffee and its fact, its State effect.
        for needle in ("sunrise", "coffee"):
            assert await leak(engine, story, needle) == [], needle
        suspicion = await rows(
            engine,
            "SELECT c.id FROM state_changes c JOIN state_definitions d ON d.id = c.state_id "
            "WHERE c.story_id = :s AND d.name = 'suspicion'",
            s=story,
        )
        assert suspicion == []
        located = await rows(
            engine,
            "SELECT id FROM facts WHERE story_id = :s AND subject_id = :c AND "
            "proposition_id IN (SELECT id FROM propositions WHERE object_id = :k)",
            s=story,
            c=cal,
            k=kitchen,
        )
        assert located == []
        # The player's input is recorded as stated.
        player = await rows(
            engine,
            "SELECT payload FROM events WHERE story_id = :s AND type = 'player_input'",
            s=story,
        )
        assert any("I fall asleep on the couch" in json.dumps(p.payload) for p in player)
        # The delivery surfaced, its one stop recorded with the stopped advance.
        assert await ck.statuses(engine, story, delivery["event_id"]) == [
            "pending",
            "due",
            "surfaced",
        ]
        (event,) = await ck.events(engine, story)
        assert (event.status.value, event.stopped_clock_at) == ("surfaced", SIX)
        assert ("declared_skip", SIX - 1, delivery["event_id"]) in await stopped_by(engine, story)

        # The next advance is not stopped by it.
        stub.reset()
        es.narrator(
            stub,
            es.render(
                beat(
                    "narration",
                    "You sign for the parcel and go back to sleep.",
                    effects=[skip(60, "a nap")],
                )
            ),
        )
        ms.renders_for(stub, "Jenni", ms.render(beat("say", "Who was it?")))
        answer = await respond(
            client, story, body(cal, None, do="I open the door", render_character_ids=[jenni])
        )
        assert answer.status_code == 200, answer.text
        assert answer.json()["clock"]["after"]["offset"] == SIX + 60
        assert es.regenerations(stub) == []


# -- #199: declared, estimated and bounded skips ---------------------------------------------

TEN_PAST_SEVEN = 19 * 60 + 10
"""The movie starts at 19:10 (the anchor: offset 0)."""
TWENTY_TO_EIGHT = 30
"""19:40, as an offset from 19:10."""
TEN_PAST_NINE = 120
"""21:10, as an offset from 19:10."""
REST_OF_THE_MOVIE = "They watch the rest of the movie in silence."
ESTIMATE = {
    "minutes": 90,
    "reason": "The two-hour movie started at 19:10, so about ninety minutes remain.",
    "confidence": 0.8,
}
STAYS = "They sit together on the couch."


async def movie(
    client: httpx.AsyncClient, engine: AsyncEngine, stub: StubAdapter, *, watching: bool = True
) -> tuple[str, dict[str, str]]:
    """A manual-mode story (no pacing) anchored at 19:10: the player puts on a two-hour movie
    (``watching``; else a remark that establishes nothing), then the author moves the clock to
    19:40."""
    story, ids = await structured_story(client, "Cal", "Jenni")
    await ck.set_clock(engine, story, "on")
    await ck.anchor_at(engine, story, TEN_PAST_SEVEN)
    patched = await client.patch(f"/stories/{story}/clock", json={"mode": "manual"}, headers=WRITE)
    assert patched.status_code == 200, patched.text
    await ms.per_character(engine, story, model_calls=20)
    stub.reset()
    es.narrator(stub, es.render(beat("narration", "The opening credits roll.")))
    ms.renders_for(stub, "Jenni", ms.render(beat("say", "Finally.")))
    opened = "I put on a two-hour movie for us at 19:10." if watching else "I look around."
    answer = await respond(
        client, story, body(ids["Cal"], None, do=opened, render_character_ids=[ids["Jenni"]])
    )
    assert answer.status_code == 200, answer.text
    await ck.advance(engine, story, TWENTY_TO_EIGHT)
    return story, ids


async def night(
    client: httpx.AsyncClient,
    stub: StubAdapter,
    story: str,
    ids: dict[str, str],
    *narration: dict[str, Any],
    do: str = "I glance at Jenni.",
    estimate: dict[str, Any] | None = None,
    regenerated: str | None = None,
) -> dict[str, Any]:
    """One turn: Cal does ``do``, the narrator narrates ``narration`` and Jenni says "Mm."; the
    estimator answers ``estimate`` and the repair's regeneration ``regenerated``."""
    stub.reset()
    es.narrator(stub, es.render(*narration))
    ms.renders_for(stub, "Jenni", ms.render(beat("say", "Mm.")))
    if estimate is not None:
        stub.script("time_estimation", [json.dumps(estimate)])
    if regenerated is not None:
        stub.script("regeneration", [json.dumps({"text": regenerated})])
    answer = await respond(
        client, story, body(ids["Cal"], None, do=do, render_character_ids=[ids["Jenni"]])
    )
    assert answer.status_code == 200, answer.text
    found: dict[str, Any] = answer.json()
    return found


def estimations(stub: StubAdapter) -> list[str]:
    """The prompts of the stub's ``time_estimation`` calls."""
    return [
        "\n".join(m.content for m in c.messages)
        for c in stub.calls
        if c.purpose == "time_estimation"
    ]


async def skips(engine: AsyncEngine, story: str) -> list[Any]:
    """The clock's recorded turn advances (not the consumer's), oldest first."""
    return await rows(
        engine,
        "SELECT c.source::text AS source, c.delta, c.estimate_meta, c.narrative_turn, "
        "c.turn_variant FROM state_changes c JOIN state_definitions d ON d.id = c.state_id "
        "WHERE c.story_id = :s AND d.name = 'world_clock' AND c.source <> 'consumer' "
        "ORDER BY c.narrative_turn, c.turn_variant, c.beat, c.step, c.id",
        s=story,
    )


async def offset_of(client: httpx.AsyncClient, story: str) -> int:
    clock = await client.get(f"/stories/{story}/clock", headers=WRITE)
    assert clock.status_code == 200, clock.text
    found: int = clock.json()["offset"]
    return found


async def cap_scenario(engine: AsyncEngine, story: str, minutes: int) -> None:
    """The scenario's ``max_unprompted_skip_minutes``, written on the clock's definition as
    genesis seeds it (#202 asserts the seeding itself)."""
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE state_definitions SET max_unprompted_skip_minutes = :m "
                "WHERE story_id = :s AND name = 'world_clock'"
            ),
            {"m": minutes, "s": story},
        )


def unprompted(report: dict[str, Any]) -> list[dict[str, Any]]:
    """The turn report's ``unprompted_skip`` repairs (E-09's pacing signal, #201)."""
    return [r for r in report.get("repairs", []) if r["tag"] == "unprompted_skip"]


@pytest.mark.integration
@pytest.mark.part("M6", "FR-CLOCK-7,8,9,12,16; FR-STATE-13")
async def test_s_movie(
    stub_settings: Settings,
    engine: AsyncEngine,
    states_delivered: None,
    stops_delivered: None,
    drained_embedding_jobs: None,
) -> None:
    async with respond_client(stub_settings, engine) as (client, epistrel):
        stub = epistrel.models.stub

        # 19:40, "they watch the rest of the movie in silence", no advance: flagged, estimated
        # (90 minutes, with its reason), model-initiated, established and under the cap:
        # accepted, the clock at 21:10, recorded as an estimate.
        story, ids = await movie(client, engine, stub)
        found = await night(
            client, stub, story, ids, beat("narration", REST_OF_THE_MOVIE), estimate=ESTIMATE
        )
        assert found["clock"]["after"]["offset"] == TEN_PAST_NINE
        (prompt,) = estimations(stub)
        assert REST_OF_THE_MOVIE in prompt
        assert "The clock reads 19:40." in prompt
        assert "two-hour movie for us at 19:10" in prompt
        *_, estimated = await skips(engine, story)
        assert (estimated.source, estimated.delta) == ("estimate", 90)
        meta = estimated.estimate_meta
        assert (meta["reason"], meta["confidence"], meta["initiated_by"]) == (
            ESTIMATE["reason"],
            0.8,
            "model",
        )
        assert meta["model"] == "stub"
        assert len(meta["prompt_hash"]) >= 16
        report = await turn_report(engine, story)
        assert report["clock"]["skips"] == {
            "accepted": 1,
            "estimated": 1,
            "refused": 0,
            "unestimated": 0,
        }
        assert unprompted(report) == []
        movie_turn = estimated.narrative_turn

        # Replay: the projections refolded from the log and the clock read again reproduce
        # 21:10, and nothing is estimated.
        stub.reset()
        await rebuild(engine, story)
        assert await offset_of(client, story) == TEN_PAST_NINE
        assert estimations(stub) == []

        # Regenerating the movie turn renders and estimates again: the new estimate is
        # recorded on the new variant.
        stub.reset()
        es.narrator(stub, es.render(beat("narration", REST_OF_THE_MOVIE)))
        ms.renders_for(stub, "Jenni", ms.render(beat("say", "Mm.")))
        again = {**ESTIMATE, "minutes": 85, "reason": "About eighty-five minutes are left."}
        stub.script("time_estimation", [json.dumps(again)])
        regenerated = await client.post(f"/stories/{story}/regenerate", json={}, headers=WRITE)
        assert regenerated.status_code == 200, regenerated.text
        assert len(estimations(stub)) == 1
        variant = regenerated.json()["variant"]
        (redone,) = [
            r
            for r in await skips(engine, story)
            if (r.narrative_turn, r.turn_variant) == (movie_turn, variant)
        ]
        assert (redone.source, redone.delta) == ("estimate", 85)
        assert redone.estimate_meta["reason"] == again["reason"]

        # Complement: the same sentence with its advance declared asks no estimate.
        story, ids = await movie(client, engine, stub)
        found = await night(
            client,
            stub,
            story,
            ids,
            beat("narration", REST_OF_THE_MOVIE, effects=[skip(90, "the rest of the movie")]),
        )
        assert found["clock"]["after"]["offset"] == TEN_PAST_NINE
        assert estimations(stub) == []
        *_, declared = await skips(engine, story)
        assert (declared.source, declared.delta) == ("declared_skip", 90)
        assert declared.estimate_meta == {"initiated_by": "model"}

        # Player-initiated: "I fall asleep on the couch" at 23:30; the narrator narrates the
        # night and declares it to 07:00: the player's skip, uncapped (the story's cap is 1 h).
        story, ids = await movie(client, engine, stub)
        await cap_scenario(engine, story, 60)
        await ck.advance(engine, story, to="23:30")
        found = await night(
            client,
            stub,
            story,
            ids,
            beat("narration", "You drift off and sleep through the night.", effects=[skip(450)]),
            do="I fall asleep on the couch",
        )
        assert found["clock"]["after"]["offset"] == HALF_PAST_ELEVEN - TEN_PAST_SEVEN + 450
        *_, slept = await skips(engine, story)
        assert (slept.source, slept.delta) == ("declared_skip", 450)
        assert slept.estimate_meta == {"initiated_by": "player"}
        assert unprompted(await turn_report(engine, story)) == []

        # Model-initiated with nothing establishing sleep: removed although under 24 h, and
        # repaired to stay in the present; "an hour later, the doorbell rings" (no activity):
        # removed. Neither asks the estimator.
        story, ids = await movie(client, engine, stub, watching=False)
        found = await night(
            client,
            stub,
            story,
            ids,
            beat(
                "narration",
                "They fall asleep on the couch and wake at dawn.",
                effects=[skip(600, "they sleep")],
            ),
            regenerated=STAYS,
        )
        assert found["clock"]["after"]["offset"] == TWENTY_TO_EIGHT
        assert [b["text"] for b in found["beats"] if b["kind"] == "beat_narration"] == [STAYS]
        assert len(unprompted(await turn_report(engine, story))) == 1
        assert estimations(stub) == []
        assert await leak(engine, story, "dawn") == []
        found = await night(
            client,
            stub,
            story,
            ids,
            beat("narration", "An hour later, the doorbell rings."),
            regenerated="The doorbell rings.",
        )
        assert found["clock"]["after"]["offset"] == TWENTY_TO_EIGHT
        assert len(unprompted(await turn_report(engine, story))) == 1
        assert estimations(stub) == []
        assert await skips(engine, story) == []

        # The scenario's cap of 1 h: the 90-minute estimate is removed and repaired; the
        # author still advances the clock through the State API.
        story, ids = await movie(client, engine, stub)
        await cap_scenario(engine, story, 60)
        found = await night(
            client,
            stub,
            story,
            ids,
            beat("narration", REST_OF_THE_MOVIE),
            estimate=ESTIMATE,
            regenerated=STAYS,
        )
        assert found["clock"]["after"]["offset"] == TWENTY_TO_EIGHT
        assert len(estimations(stub)) == 1
        assert len(unprompted(await turn_report(engine, story))) == 1
        assert await skips(engine, story) == []
        moved = await client.post(
            f"/stories/{story}/clock/advance",
            json={"minutes": 90, "reason": "the movie ends"},
            headers=WRITE,
        )
        assert moved.status_code == 200, moved.text
        assert await offset_of(client, story) == TEN_PAST_NINE

        # The story's override (2 h) wins over the scenario's: the same estimate stands.
        patched = await client.patch(
            f"/stories/{story}/clock", json={"max_unprompted_skip_minutes": 120}, headers=WRITE
        )
        assert patched.status_code == 200, patched.text
        found = await night(
            client, stub, story, ids, beat("narration", REST_OF_THE_MOVIE), estimate=ESTIMATE
        )
        assert found["clock"]["after"]["offset"] == TEN_PAST_NINE + 90
        assert unprompted(await turn_report(engine, story)) == []

        # 0 rejects every model-initiated skip (no estimate asked) while the player's still
        # moves the clock.
        patched = await client.patch(
            f"/stories/{story}/clock", json={"max_unprompted_skip_minutes": 0}, headers=WRITE
        )
        assert patched.status_code == 200, patched.text
        at = await offset_of(client, story)
        found = await night(
            client,
            stub,
            story,
            ids,
            beat("narration", REST_OF_THE_MOVIE),
            estimate=ESTIMATE,
            regenerated=STAYS,
        )
        assert found["clock"]["after"]["offset"] == at
        assert estimations(stub) == []
        assert len(unprompted(await turn_report(engine, story))) == 1
        found = await night(
            client,
            stub,
            story,
            ids,
            beat("narration", "You drift off and sleep through the night.", effects=[skip(450)]),
            do="I fall asleep on the couch",
        )
        assert found["clock"]["after"]["offset"] == at + 450


@pytest.mark.integration
@pytest.mark.part("M6", "FR-CLOCK-7,8")
async def test_s_movie__prompt_only(
    stub_settings: Settings,
    engine: AsyncEngine,
    states_delivered: None,
    stops_delivered: None,
    drained_embedding_jobs: None,
) -> None:
    """#663: at ``clock.level = prompt_only`` a structured story's clock is prompt text alone.
    "They watch the rest of the movie in silence" (an undeclared skip) asks no estimator and
    is never refused, and "It's 23:30", which the 19:40 anchor contradicts, is neither checked
    nor repaired nor anchored. Complement asserted: ``test_s_movie`` estimates the same skip
    at ``on``."""
    async with respond_client(stub_settings, engine) as (client, epistrel):
        stub = epistrel.models.stub
        story, ids = await movie(client, engine, stub)
        # A placed scene: a time statement is then the M4 fact about it, as with the clock off.
        apartment = await ps.place_entity(engine, story, "the apartment")
        for who in ids.values():
            await ps.put(engine, story, who, apartment)
        await ck.set_clock(engine, story, "prompt_only")
        anchors = await rows(
            engine,
            "SELECT count(*) AS n FROM facts WHERE story_id = :s AND predicate_id = :p",
            s=story,
            p=CLOCK_ANCHOR,
        )
        found = await night(
            client,
            stub,
            story,
            ids,
            beat("narration", REST_OF_THE_MOVIE),
            beat("narration", "It's 23:30."),
        )
        assert estimations(stub) == []
        report = await turn_report(engine, story)
        assert es.regenerations(stub) == []
        assert report.get("repairs", []) == []
        assert found["clock"]["after"]["offset"] == found["clock"]["before"]["offset"]
        after = await rows(
            engine,
            "SELECT count(*) AS n FROM facts WHERE story_id = :s AND predicate_id = :p",
            s=story,
            p=CLOCK_ANCHOR,
        )
        assert after[0].n == anchors[0].n


@pytest.mark.integration
@pytest.mark.part("M6", "FR-CLOCK-7,8")
async def test_s_movie__skip_in_a_repair(
    stub_settings: Settings,
    engine: AsyncEngine,
    states_delivered: None,
    stops_delivered: None,
    drained_embedding_jobs: None,
) -> None:
    """#638: a beat the ladder regenerated for another reason (here a contradicted claim) is
    decided again for the skips its new text brings: "An hour later, the doorbell rings", with
    nothing in the scene establishing it, is refused and its sentence removed, as it is when a
    first render narrates it (``test_s_movie``)."""
    async with respond_client(stub_settings, engine) as (client, epistrel):
        stub = epistrel.models.stub
        story, ids = await movie(client, engine, stub, watching=False)
        flat = await ps.place_entity(engine, story, "the flat")
        await ps.put(engine, story, ids["Jenni"], flat)
        found = await night(
            client,
            stub,
            story,
            ids,
            beat(
                "narration",
                "Jenni is not in the flat.",
                propositions=[claim("Jenni", "located_at", "the flat", negated=True)],
            ),
            regenerated="Jenni stretches. An hour later, the doorbell rings.",
        )
        narrated = [b["text"] for b in found["beats"] if b["kind"] == "beat_narration"]
        assert narrated == ["Jenni stretches."], narrated
        assert found["clock"]["after"]["offset"] == TWENTY_TO_EIGHT
        report = await turn_report(engine, story)
        assert report["clock"]["skips"]["refused"] == 1, report["clock"]
        assert estimations(stub) == []
        assert await skips(engine, story) == []
