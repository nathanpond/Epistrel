"""S-INPUT-INGEST (docs/04): authoritative player input and deduplicated ingest. The respond
half (#57, FR-RESP-9) and the ingest half (#58, FR-RESP-10)."""

from __future__ import annotations

import asyncio
from typing import Any

import httpx2 as httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.config import Settings
from tests.auth_support import auth_settings
from tests.event_support import rows
from tests.respond_support import (
    FIRST,
    PLAYER,
    WITHIN_S,
    body,
    new_story,
    played,
    respond,
    respond_client,
)
from tests.story_support import READ, WRITE

ONLY_IN_SCENE = "the lantern was green"


@pytest.fixture
def stub_settings(pg_url: str) -> Settings:
    return auth_settings(pg_url, model_adapter="stub")


@pytest.mark.part("M2", "FR-RESP-9")
async def test_s_input_ingest__respond(stub_settings: Settings, engine: AsyncEngine) -> None:
    """The player input is the turn's first beat: the acting character's ``say``/``do`` as a
    ``player_input`` event at beat 1, before the reply at beat 2, recorded under the request's
    idempotency key; content placed only in the scene's recent messages never enters history.

    Complement asserted: the acting character can be a non-player cast member, and the input
    is still beat 1, attributed to that character."""
    async with respond_client(stub_settings, engine) as (client, _):
        story, ids = await new_story(client)
        scene = {"recent_messages": [{"text": ONLY_IN_SCENE}]}
        result = await played(
            client,
            story,
            body(ids[PLAYER], "Open the gate.", do="pushes", scene_context=scene),
            headers={"Idempotency-Key": "turn-key-1"},
        )
        said, done, reply = result["beats"]
        for first in (said, done):
            assert (first["beat"], first["kind"], first["actor_character_id"]) == (
                1,
                "player_input",
                ids[PLAYER],
            )
        assert said["message_id"] == done["message_id"]
        assert [(said["channel"], said["text"]), (done["channel"], done["text"])] == [
            ("say", "Open the gate."),
            ("do", "pushes"),
        ]
        assert (reply["beat"], reply["kind"]) == (2, "message_rendered")

        events = await rows(
            engine,
            "SELECT e.type, e.beat, e.seq, e.character_id, e.provenance, o.idempotency_key "
            "FROM events e JOIN operations o USING (operation_id) "
            "WHERE e.story_id = :s AND o.kind = 'turn' ORDER BY e.seq",
            s=story,
        )
        assert [(e.type, e.beat) for e in events] == [
            ("player_input", 1),
            ("message_rendered", 2),
        ]
        assert events[0].seq < events[1].seq
        assert (events[0].character_id, events[0].provenance) == (ids[PLAYER], "player")
        assert {e.idempotency_key for e in events} == {"turn-key-1"}

        history = (await client.get(f"/stories/{story}/history", headers=READ)).json()
        texts = "\n".join(item["text"] for item in history["items"])
        assert "Open the gate." in texts
        assert ONLY_IN_SCENE not in texts

        npc = await played(client, story, body(ids[FIRST], "I'll hold it open."))
        assert npc["beats"][0]["beat"] == 1
        assert npc["beats"][0]["actor_character_id"] == ids[FIRST]


async def ingest(
    client: httpx.AsyncClient, story_id: str, *events: dict[str, Any]
) -> httpx.Response:
    return await client.post(
        f"/stories/{story_id}/ingest", json={"events": list(events)}, headers=WRITE
    )


async def _recorded(engine: AsyncEngine, story_id: str) -> list[Any]:
    """The story's ingested operations, with the dedupe row of each."""
    return await rows(
        engine,
        "SELECT o.operation_id, o.narrative_turn, o.status, c.client_event_id "
        "FROM operations o LEFT JOIN client_events c USING (operation_id) "
        "WHERE o.story_id = :s AND o.origin = 'ingested' ORDER BY o.started_at",
        s=story_id,
    )


async def _revision(client: httpx.AsyncClient, story_id: str) -> int:
    found = (await client.get(f"/stories/{story_id}", headers=READ)).json()
    revision: int = found["timeline_revision"]
    return revision


@pytest.mark.part("M2", "FR-RESP-10")
async def test_s_input_ingest__ingest(stub_settings: Settings, engine: AsyncEngine) -> None:
    """Ingesting event E twice records it once and returns the same result; E with a
    different payload is rejected and still one record exists. An ingested player-only turn
    takes the next turn index and advances the revision by one, and under ``reject`` it is
    refused busy while a turn is in flight, with nothing written.

    Complement asserted: under ``queue`` the same ingest is not refused; it waits for the
    in-flight turn to commit and then takes the following index."""
    async with respond_client(stub_settings, engine) as (client, lib):
        story, ids = await new_story(client)
        await played(client, story, body(ids[PLAYER], "First."))
        before = await _revision(client, story)
        event = {
            "client_event_id": "E",
            "kind": "player_turn",
            "actor_character_id": ids[PLAYER],
            "say": "I wait by the door.",
        }

        first = await ingest(client, story, event)
        assert first.status_code == 201, first.text
        assert "Idempotent-Replay" not in first.headers
        (result,) = first.json()["results"]
        assert (result["turn"], result["replayed"]) == (2, False)
        assert first.json()["timeline_revision"] == before + 1 == await _revision(client, story)

        again = await ingest(client, story, event)
        assert again.status_code == 200, again.text
        assert again.headers["Idempotent-Replay"] == "true"
        assert again.json()["results"] == [{**result, "replayed": True}]
        assert again.json()["timeline_revision"] == first.json()["timeline_revision"]

        changed = await ingest(client, story, {**event, "say": "I leave."})
        assert changed.status_code == 409, changed.text
        assert changed.json()["error"]["code"] == "client_event_conflict"
        recorded = await _recorded(engine, story)
        assert [(r.narrative_turn, r.client_event_id) for r in recorded] == [(2, "E")]
        assert await _revision(client, story) == before + 1

        # Busy under ``reject`` (the default) while a turn generates: nothing is written.
        stub = lib.models.stub
        latch = stub.hold("generation")
        turn = asyncio.ensure_future(respond(client, story, body(ids[FIRST], "Who's there?")))
        await asyncio.wait_for(latch.entered.wait(), 5)
        busy = await asyncio.wait_for(
            ingest(client, story, {**event, "client_event_id": "F"}), WITHIN_S
        )
        assert busy.status_code == 423, busy.text
        # Refused at once, not after waiting: it answered while the turn still holds the slot
        # (its generation is held until the release below), so it did not wait for the turn,
        # and the reason is ``in_flight``, not a lock wait that timed out. A causal check, not
        # a wall-clock bound (#446).
        assert not turn.done()
        assert busy.json()["error"]["code"] == "story_busy"
        assert busy.json()["error"]["details"]["reason"] == "in_flight"
        latch.release()
        assert (await turn).status_code == 200
        assert len(await _recorded(engine, story)) == 1

        # The complement: under ``queue`` the ingest waits for the turn, then follows it.
        queued, qids = await new_story(client, settings={"turn_concurrency": "queue"})
        latch = stub.hold("generation")
        turn = asyncio.ensure_future(respond(client, queued, body(qids[PLAYER], "Hello?")))
        await asyncio.wait_for(latch.entered.wait(), 5)
        waiting = asyncio.ensure_future(
            ingest(client, queued, {**event, "actor_character_id": qids[FIRST]})
        )
        await asyncio.sleep(0.3)
        assert not waiting.done()
        latch.release()
        played_turn, ingested = await turn, await waiting
        assert played_turn.status_code == 200, played_turn.text
        assert ingested.status_code == 201, ingested.text
        assert ingested.json()["results"][0]["turn"] == played_turn.json()["turn"] + 1
        assert ingested.json()["timeline_revision"] == played_turn.json()["timeline_revision"] + 1
