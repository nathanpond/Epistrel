"""Lite history rewrites (FR-LITE-8, FR-REWRITE-1,4,6,10; #245): reach is model-searched.

A Lite story's reply says Ada was a farmer. The author corrects the past ("Ada was always a
sailor."): ``rewrite_parse`` yields no change Lite can hold (its proposition is refused,
``lite_no_canon``), and the ``lite_rewrite_reach`` pass finds the reply and fixes it. The
preview lists the one record change; applied, the transcript reads the fix, the revision moves
by one, the original stays in the event log; undone, the transcript reads the original again.
A Lite directive about the past now opens a session instead of a refusal.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.config import Settings
from tests import rewrite_support as rs
from tests.auth_support import auth_settings
from tests.event_support import rows
from tests.respond_support import FIRST, respond, respond_client
from tests.respond_support import body as respond_body
from tests.story_support import WRITE, create, story_body

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("drained_embedding_jobs")]

FARMER = "Ada wipes her hands. I was a farmer before all this."
SAILOR = "Ada wipes her hands. I was a sailor before all this."
CORRECTION = "Ada was always a sailor."
PARSED = json.dumps(
    {
        "changes": [
            {"kind": "proposition", "subject": "Ada", "predicate": "occupation", "object": "sailor"}
        ]
    }
)
REACHED = json.dumps(
    {
        "hits": [
            {
                "turn": 1,
                "quote": "I was a farmer",
                "relation": "conflicts",
                "fix_text": SAILOR,
                "reason": "names her old trade",
            }
        ]
    }
)


@pytest.fixture
def stub_settings(pg_url: str) -> Settings:
    return auth_settings(pg_url, model_adapter="stub")


async def _transcript(engine: AsyncEngine, story: str) -> list[str]:
    found = await rows(
        engine,
        "SELECT text FROM transcript_messages WHERE story_id = :s AND membership = 'active' "
        "ORDER BY seq",
        s=story,
    )
    return [str(r.text) for r in found]


async def _lite_story(client: Any, engine: AsyncEngine, stub: Any) -> str:
    story = str((await create(client, story_body("Wren", "Ada")))["story_id"])
    cast = await rows(engine, "SELECT id, name FROM characters WHERE story_id = :s", s=story)
    actor = next(str(c.id) for c in cast if c.name == "Wren")
    stub.when("", FARMER, purpose="generation")
    played = await respond(client, story, respond_body(actor, FIRST))
    assert played.status_code == 200, played.text
    return story


@pytest.mark.part("M7", "FR-LITE-8; FR-REWRITE-1,4,6,10")
async def test_s_lite_rewrite(stub_settings: Settings, engine: AsyncEngine) -> None:
    async with respond_client(stub_settings, engine) as (client, epistrel):
        stub = epistrel.models.stub
        story = await _lite_story(client, engine, stub)
        assert FARMER in await _transcript(engine, story)
        stub.when("", PARSED, purpose="rewrite_parse")
        stub.when("", REACHED, purpose="lite_rewrite_reach")
        revision = await rs.revision(engine, story)
        opened = await client.post(
            f"/stories/{story}/rewrites",
            params=rs.OMNISCIENT,
            json={"instruction": CORRECTION},
            headers=WRITE,
        )
        assert opened.status_code == 201, opened.text
        session = opened.json()
        preview = session["preview"]
        assert [c["kind"] for c in preview["changes"]] == ["record"]
        assert preview["changes"][0]["new_text"] == SAILOR
        assert "lite_no_canon" in {r.get("reason") for r in preview["refused_changes"]}
        assert await rs.revision(engine, story) == revision  # opening writes no story truth
        assert FARMER in await _transcript(engine, story)
        if session["state"] != "awaiting_approval":
            answered = await rs.answer_all(client, story, session)
            assert answered.status_code == 200, answered.text
            session = answered.json()
        applied = await rs.apply(client, story, session["id"], session["base_revision"])
        assert applied.status_code == 200, applied.text
        assert await rs.revision(engine, story) == revision + 1
        lines = await _transcript(engine, story)
        assert SAILOR in lines
        assert FARMER not in lines
        originals = await rows(
            engine,
            "SELECT payload->>'text' AS body FROM events WHERE story_id = :s "
            "AND type = 'message_rendered'",
            s=story,
        )
        assert FARMER in {r.body for r in originals}  # the log keeps the original

        (amendment,) = await rows(engine, "SELECT id FROM amendments WHERE story_id = :s", s=story)
        undone = await client.post(
            f"/stories/{story}/amendments/{amendment.id}/undo",
            params=rs.OMNISCIENT,
            json={},
            headers=WRITE,
        )
        assert undone.status_code in (200, 201), undone.text
        undo_session = undone.json()
        if undo_session["state"] == "awaiting_answers":
            answered = await rs.answer_all(client, story, undo_session)
            undo_session = answered.json()
        reverted = await rs.apply(client, story, undo_session["id"], undo_session["base_revision"])
        assert reverted.status_code == 200, reverted.text
        assert FARMER in await _transcript(engine, story)


async def test_lite_past_directive_opens_a_rewrite(
    stub_settings: Settings, engine: AsyncEngine
) -> None:
    """#245 retires #244's ``directive_requires_rewrite``: a Lite directive about the past is
    routed into a Lite rewrite session (202 ``routed_to_rewrite``)."""
    async with respond_client(stub_settings, engine) as (client, epistrel):
        stub = epistrel.models.stub
        story = await _lite_story(client, engine, stub)
        stub.when(
            "",
            json.dumps({"classification": "durable", "temporal_scope": "past", "changes": []}),
            purpose="directive_parse",
        )
        stub.when("", PARSED, purpose="rewrite_parse")
        stub.when("", REACHED, purpose="lite_rewrite_reach")
        routed = await client.post(
            f"/stories/{story}/directives",
            params=rs.OMNISCIENT,
            json={"text": CORRECTION},
            headers=WRITE,
        )
        assert routed.status_code == 202, routed.text
        body = routed.json()
        assert body["disposition"] == "routed_to_rewrite"
        assert body["rewrite_session"]["kind"] == "directive"
