"""S-MOVIE: narrated skips, estimation and caps (docs/04 §7; FR-CLOCK-7,8,9,12,16, FR-STATE-13),
its M6 parts.

Every S-MOVIE (M6) function lives in this file (traceability counts a test ID per milestone in
one file): #196's stop clause (``__stop``); #199 adds the estimation and cap clauses.

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

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.config import Settings
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
from tests.structured_turn_support import beat, claim, structured_story

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
