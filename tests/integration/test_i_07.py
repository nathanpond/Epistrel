"""I-07: the respond contract (docs/04), the M2 part for FR-RESP-1,2,4 (#57). FR-RESP-5,6 are
``test_i_07__fields`` in #62.

Every test runs on the stub adapter, so the captured requests are exactly what a model would
have been sent."""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.config import Settings
from epistrel.core.ratings import story_guidance
from tests.auth_support import auth_settings
from tests.concurrency_support import set_instructions, set_pins, summarise
from tests.respond_support import (
    FIRST,
    PLAYER,
    body,
    everything_sent,
    generation_calls,
    new_story,
    played,
    respond,
    respond_client,
    story_text,
    system_of,
)
from tests.story_support import count

PLANTED = "PINEAPPLE-42"
RESPONSE_FIELDS = {
    "idempotency_key",
    "message",
    "beats",
    "turn",
    "variant",
    "timeline_revision",
    "output_viewpoint",
    "audience_filter",
    "model_calls",
    "elapsed_ms",
    "degradation",
}


@pytest.fixture
def stub_settings(pg_url: str) -> Settings:
    return auth_settings(pg_url, model_adapter="stub")


@pytest.mark.part("M2", "FR-RESP-1,2,4")
async def test_i_07(stub_settings: Settings, engine: AsyncEngine) -> None:
    """A respond call returns one finished message with the new revision and the turn's cost;
    the consumer sends only the contract's fields and the prompt's guidance, instructions, pins,
    summary and history all come from the story's records; a phrase planted only in the scene
    context's recent messages reaches no prompt and no stored row.

    Complement asserted: the same phrase sent as the player's ``say`` does reach the prompt and
    the transcript, so the planted-phrase check can see it when it is there."""
    async with respond_client(stub_settings, engine) as (client, lib):
        stub = lib.models.stub
        story, ids = await new_story(client)
        start = await count(engine, "SELECT timeline_revision FROM stories WHERE id = :s", s=story)

        # FR-RESP-1: one finished message, the new revision, the turn's position and cost.
        first = await played(client, story, body(ids[PLAYER]))
        assert set(first) == RESPONSE_FIELDS
        assert first["message"]["text"]
        assert set(first["message"]) == {"message_id", "text"}
        assert (first["turn"], first["variant"]) == (1, 1)
        assert first["timeline_revision"] == start + 1
        assert first["output_viewpoint"] == f"character:{ids[PLAYER]}"
        assert first["degradation"] == []
        assert [c["purpose"] for c in first["model_calls"]] == ["generation"]
        assert first["elapsed_ms"] >= 0
        reply = next(b for b in first["beats"] if b["beat"] == 2)
        assert reply["message_id"] == first["message"]["message_id"]
        assert reply["text"] == first["message"]["text"]

        # FR-RESP-2: the consumer sends no memory; the Engine reads its own records.
        await set_instructions(engine, story, "Keep replies short.")
        await set_pins(engine, story, "The well is dry.")
        await summarise(engine, story, 1)
        stub.reset()
        refused = await respond(client, story, {**body(ids[PLAYER]), "history": ["x"]})
        assert refused.status_code == 422
        assert generation_calls(stub) == []
        await played(client, story, body(ids[PLAYER], "Is the well dry?"))
        (call,) = generation_calls(stub)
        system = system_of(call)
        guidance = (await story_guidance(engine, story)).prompt_guidance
        for own_record in (
            guidance,
            "Keep replies short.",
            "The well is dry.",
            "summary through 1",
        ):
            assert own_record in system
        assert first["message"]["text"] in [m.content for m in call.messages]

        # FR-RESP-4: scene context is accepted, then never placed in a prompt or stored.
        stub.reset()
        scene: dict[str, Any] = {
            "characters_present": [ids[FIRST]],
            "recent_messages": [{"character_id": ids[FIRST], "text": f"Ada said {PLANTED}."}],
        }
        await played(client, story, body(ids[PLAYER], "Anything new?", scene_context=scene))
        assert len(generation_calls(stub)) == 1
        assert PLANTED not in everything_sent(stub)
        assert PLANTED not in await story_text(engine, story)

        stub.reset()
        await played(client, story, body(ids[PLAYER], f"I heard {PLANTED}."))
        assert PLANTED in everything_sent(stub)
        assert PLANTED in await story_text(engine, story)
