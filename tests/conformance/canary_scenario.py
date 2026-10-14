"""C-08's planted canaries (#74): one story driven over REST with a distinct canary phrase in every
content path the planner named — character definitions, a character's name (#123's subject
index scans every text for it) and the story name, pinned memories, custom instructions, player
input (``say`` and ``do``), ingested narration, the stub's replies (first draft and
regeneration) and a fork's name — plus requests that are refused while carrying a canary (a
validation error and an unknown story).

Not collected by the normal run (no ``test_`` prefix): ``test_c_08`` passes this file explicitly
to the subprocess it scans, beside ``tests/scenario`` and C-03."""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.config import Settings
from tests.auth_support import auth_settings
from tests.respond_support import PLAYER, body, new_story, played, respond, respond_client
from tests.story_support import READ, WRITE, character

CANARIES = {
    "story_name": "canary-plum-lantern-story-name",
    "definition": "canary-tin-heron-definition",
    "pinned": "canary-velvet-anchor-pinned",
    "instructions": "canary-quartz-meadow-instructions",
    "say": "canary-amber-falcon-say",
    "do": "canary-cobalt-ladder-do",
    "ingest": "canary-saffron-wharf-ingest",
    "reply": "canary-ivory-thimble-reply",
    "regenerated": "canary-garnet-pylon-regenerated",
    "fork_name": "canary-hazel-orbit-fork",
    "refused": "canary-onyx-kettle-refused",
    "unknown_story": "canary-jade-compass-unknown",
    "character_name": "canary-ochre-wren-name",
}
"""Every phrase is under 64 characters, so a truncating filter alone would not hide it."""


@pytest.fixture
def settings(pg_url: str) -> Settings:
    return auth_settings(pg_url, model_adapter="stub")


async def test_canary_story(settings: Settings, engine: AsyncEngine) -> None:
    async with respond_client(settings, engine) as (client, lib):
        stub = lib.models.stub
        cast = [
            character(PLAYER, 30, is_player=True),
            character(
                "Ada",
                31,
                definition=(
                    f"Ada, {CANARIES['definition']}, sister of {CANARIES['character_name']}."
                ),
            ),
            # A planted name the subject-reference index finds in the definition and the reply
            # (#123): the index logs counts, never the name.
            character(CANARIES["character_name"], 33),
        ]
        story_id, ids = await new_story(client, name=CANARIES["story_name"], characters=cast)
        pinned = await client.put(
            f"/stories/{story_id}/pinned-memories",
            json={"items": [{"text": CANARIES["pinned"]}]},
            headers=WRITE,
        )
        assert pinned.status_code in (200, 204), pinned.text
        instructions = await client.put(
            f"/stories/{story_id}/custom-instructions",
            json={"text": CANARIES["instructions"]},
            headers=WRITE,
        )
        assert instructions.status_code in (200, 204), instructions.text

        stub.script(
            "generation",
            [f"Ada answers: {CANARIES['reply']}, says {CANARIES['character_name']}."],
        )
        turn = await played(client, story_id, body(ids[PLAYER], CANARIES["say"], do=CANARIES["do"]))
        assert CANARIES["reply"] in turn["message"]["text"]

        stub.script("generation", [f"Ada again: {CANARIES['regenerated']}."])
        regenerated = await client.post(f"/stories/{story_id}/regenerate", json={}, headers=WRITE)
        assert regenerated.status_code == 200, regenerated.text

        ingested = await client.post(
            f"/stories/{story_id}/ingest",
            json={
                "events": [
                    {
                        "client_event_id": "evt-canary-1",
                        "kind": "narration",
                        "text": CANARIES["ingest"],
                    }
                ]
            },
            headers=WRITE,
        )
        assert ingested.status_code in (200, 201), ingested.text

        history = await client.get(f"/stories/{story_id}/history", headers=READ)
        assert CANARIES["regenerated"] in history.text

        forked = await client.post(
            f"/stories/{story_id}/fork", json={"name": CANARIES["fork_name"]}, headers=WRITE
        )
        assert forked.status_code == 201, forked.text

        refused = await respond(
            client,
            story_id,
            {**body(ids[PLAYER], CANARIES["refused"]), "unexpected_field": CANARIES["refused"]},
        )
        assert refused.status_code == 422
        unknown = await respond(
            client, "01J9Z8Q4N5V6W7X8Y9Z0A1B2C3", body(ids[PLAYER], CANARIES["unknown_story"])
        )
        assert unknown.status_code == 404
