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
from tests.idempotency_support import REPLAY
from tests.respond_support import (
    FIRST,
    PLAYER,
    SECOND,
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

KEY = "Idempotency-Key"
EXPECTED = "X-Epistrel-Expected-Revision"
SEPARATION = "Each character knows only what their notes and the scene show."
WRITTEN = (
    "SELECT (SELECT count(*) FROM operations WHERE story_id = :s)"
    " + (SELECT count(*) FROM events WHERE story_id = :s)"
    " + (SELECT count(*) FROM transcript_messages WHERE story_id = :s)"
    " + (SELECT count(*) FROM idempotency_keys WHERE scope_id = :s)"
)

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


def _unfiltered(result: dict[str, Any]) -> bool:
    """The result states perception ``none``: knowledge is separated by instruction only, and
    no audience filtering applies (FR-LITE-5, M2)."""
    return bool(result["audience_filter"] == "none")


@pytest.mark.part("M2", "FR-RESP-5,6")
async def test_i_07__fields(stub_settings: Settings, engine: AsyncEngine) -> None:
    """Several render characters compose into one message: one generation, one reply beat
    listing every render id, knowledge kept apart by one instruction line, nothing withheld;
    a single render is the same combined shape. An idempotency key and an expected revision
    are accepted and the new revision returned; a replayed key returns the identical body,
    also when the render set is spelled differently but resolves the same; a stale expected
    revision is 409 with the current revision and writes **no** rows.

    Complement asserted: a key reused with a render set that resolves differently is a conflict,
    not a replay, so the replay check is not answering every retry."""
    async with respond_client(stub_settings, engine) as (client, lib):
        stub = lib.models.stub
        story, ids = await new_story(client)
        renders = [ids[SECOND], ids[FIRST]]

        # FR-RESP-5 / FR-LITE-5: one combined generation, one message, one reply beat.
        stub.reset()
        several = await played(client, story, body(ids[PLAYER], render_character_ids=renders))
        (call,) = generation_calls(stub)
        assert SEPARATION in system_of(call)
        replies = [b for b in several["beats"] if b["kind"] == "message_rendered"]
        assert len(replies) == 1
        assert replies[0]["render_character_ids"] == renders
        assert replies[0]["actor_character_id"] is None
        assert replies[0]["message_id"] == several["message"]["message_id"]
        assert _unfiltered(several)
        single = await played(client, story, body(ids[PLAYER], render_character_ids=[ids[FIRST]]))
        (reply,) = [b for b in single["beats"] if b["kind"] == "message_rendered"]
        assert (reply["render_character_ids"], reply["actor_character_id"]) == ([ids[FIRST]], None)

        # FR-RESP-6: key + expected revision accepted, the new revision returned.
        current = single["timeline_revision"]
        headers = {KEY: "i07-fields", EXPECTED: str(current)}
        omitted = body(ids[PLAYER], "Anyone here?")
        original = await respond(client, story, omitted, headers=headers)
        assert original.status_code == 200, original.text
        assert original.json()["timeline_revision"] == current + 1
        assert original.headers[KEY] == "i07-fields"

        # A replayed key returns the identical body; the hash uses the resolved render set.
        spelled = body(ids[PLAYER], "Anyone here?", render_character_ids=[ids[FIRST], ids[SECOND]])
        spelled["render_character_ids"].append(ids[FIRST])
        for retry in (omitted, spelled):
            replay = await respond(client, story, retry, headers=headers)
            assert (replay.status_code, replay.headers.get(REPLAY)) == (200, "true")
            assert replay.content == original.content
        reordered = body(ids[PLAYER], "Anyone here?", render_character_ids=renders)
        conflict = await respond(client, story, reordered, headers=headers)
        assert conflict.status_code == 409
        assert conflict.json()["error"]["code"] == "idempotency_conflict"

        # A stale expected revision: 409 with the current revision, and no rows.
        before = await count(engine, WRITTEN, s=story)
        stub.reset()
        stale = await respond(
            client, story, body(ids[PLAYER]), headers={KEY: "i07-stale", EXPECTED: str(current)}
        )
        assert stale.status_code == 409
        error = stale.json()["error"]
        assert error["code"] == "revision_conflict"
        assert error["details"]["current_revision"] == current + 1
        assert await count(engine, WRITTEN, s=story) == before
        assert generation_calls(stub) == []
