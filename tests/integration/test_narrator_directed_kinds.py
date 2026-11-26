"""#1059 (FR-DIRECT-2, FR-MULTI-9): an author direction meant for a character never fails the
narrator's render.

Wren (the player) and Ada, with Malik present. The author queues an ephemeral directive for
Ada, and Wren's input is a ``do`` naming no one, so the ``unaddressed`` trigger runs the
narrator's render first. The scripted narrator acts the direction out: it answers a
``narration`` beat and a ``say`` of Ada's line, a kind its render may not return, on every
attempt (as ``claude-haiku-5-5`` did on the real-use run that found it).

- ``test_a_narrator_acting_out_a_direction__dropped``: the first attempt is refused with the
  dedicated correction (``render.NARRATOR_KIND_CORRECTION``); the second still carries the
  ``say``, which is dropped while its narration stands. The turn is 200, Ada's own render
  speaks, the dropped line is in no beat, and the turn reports ``narrator_kind_dropped``. The
  narrator's prompt frames the direction for it (``directives.NARRATOR_STEERING``); Ada's
  carries the plain direction line.
- ``test_a_narrator_answer_of_only_character_beats__still_refused``: complement, a narrator
  answer with nothing but a ``say`` has nothing to keep, so the turn still fails as structure.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.config import Settings
from tests import effect_support as es
from tests import multi_support as ms
from tests import rewrite_support as rs
from tests.auth_support import auth_settings
from tests.respond_support import body, played, respond, respond_client
from tests.story_support import WRITE
from tests.structured_turn_support import beat, render, structured_story

pytestmark = pytest.mark.integration

DIRECTION = "Ada finally tells Wren the truth about the ledger."
DROPPED = "I kept the ledger, Wren. I always did."
NARRATION = "Rain drums on the shutters while the fire settles."
ADA = "Fine. The ledger was mine."
DO = "Wren sits down by the fire and waits."
NARRATOR_STEERING = (
    "Narrate the world around this direction; a character's speech, action or thought that it"
    " asks for is that character's own render, never a beat of yours."
)
"""``directives.NARRATOR_STEERING`` (spelled out, so the test runs against the pre-fix code)."""
CORRECTION = (
    "The narrator's render returns only narration and latent beats; a character's say, do or"
    " think is that character's own render."
)
"""``render.NARRATOR_KIND_CORRECTION`` (spelled out, likewise)."""


@pytest.fixture
def stub_settings(pg_url: str) -> Settings:
    return auth_settings(pg_url, model_adapter="stub")


async def _directed(client: Any, stub: Any, narrator: str) -> tuple[str, dict[str, Any]]:
    story, ids = await structured_story(client, "Wren", "Ada", "Malik")
    queued = await client.post(
        f"/stories/{story}/directives",
        params=rs.OMNISCIENT,
        json={"text": DIRECTION, "kind": "ephemeral"},
        headers=WRITE,
    )
    assert queued.status_code == 200, queued.text
    assert queued.json()["disposition"] == "steer_queued"
    stub.reset()
    es.narrator(stub, narrator)
    ms.renders_for(stub, "Ada", render(beat("say", ADA)))
    return story, {
        **ids,
        "payload": body(ids["Wren"], None, do=DO, render_character_ids=[ids["Ada"]]),
    }


def _prompts(stub: Any, schema: str) -> list[str]:
    return [
        "\n".join(m.content for m in c.messages)
        for c in stub.calls
        if c.purpose == "generation" and c.response_schema == schema
    ]


async def test_a_narrator_acting_out_a_direction__dropped(
    stub_settings: Settings, engine: AsyncEngine, drained_embedding_jobs: None
) -> None:
    async with respond_client(stub_settings, engine) as (client, epistrel):
        stub = epistrel.models.stub
        answer = render(beat("narration", NARRATION), beat("say", DROPPED))
        story, ids = await _directed(client, stub, answer)
        result = await played(client, story, ids["payload"])
        texts = [b["text"] for b in result["beats"]]
        assert NARRATION in texts
        assert ADA in texts
        assert not any(DROPPED in t for t in texts)
        assert "narrator_kind_dropped" in result["degradation"]
        (detail,) = [
            d for d in result["degradation_details"] if d["step"] == "narrator_kind_dropped"
        ]
        assert detail["cause"] == "narrator"
        narrator = _prompts(stub, "NarratorRender")
        assert narrator, "the narrator rendered"
        assert all(NARRATOR_STEERING in p and DIRECTION in p for p in narrator)
        # The correction was sent once, on the second attempt of the call.
        assert sum(CORRECTION in p for p in narrator) == 1
        (ada,) = ms.render_prompts(stub, "Ada")
        assert f"Author direction for this reply: {DIRECTION}" in ada
        assert NARRATOR_STEERING not in ada


async def test_a_narrator_answer_of_only_character_beats__still_refused(
    stub_settings: Settings, engine: AsyncEngine, drained_embedding_jobs: None
) -> None:
    async with respond_client(stub_settings, engine) as (client, epistrel):
        stub = epistrel.models.stub
        story, ids = await _directed(client, stub, render(beat("say", DROPPED)))
        response = await respond(client, story, ids["payload"])
        assert response.status_code == 503, response.text
        error = response.json()["error"]
        assert error["details"]["reason"] == "invalid_structure"
        errors = error["details"]["structural_errors"]
        assert errors
        assert all("beats.0.kind: not a kind this render may return" in e for e in errors)
