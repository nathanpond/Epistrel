"""S-INPUT-INGEST (docs/04): authoritative player input. The respond half (#57, FR-RESP-9);
#58 adds the ingest half (FR-RESP-10) to this file."""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.config import Settings
from tests.auth_support import auth_settings
from tests.event_support import rows
from tests.respond_support import FIRST, PLAYER, body, new_story, played, respond_client
from tests.story_support import READ

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
        first, reply = result["beats"]
        assert (first["beat"], first["kind"], first["actor_character_id"]) == (
            1,
            "player_input",
            ids[PLAYER],
        )
        assert first["text"] == "Open the gate.\npushes"
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
