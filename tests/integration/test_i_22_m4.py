"""I-22 (M4): content ratings end to end, the structured pipeline's part (FR-RATE-4; docs/04
§4; #150). The M2 parts are in ``tests/integration/test_i_22__ratings.py`` and the M3 parts in
``tests/integration/test_rating_change.py``.

A stub adapter captures every prompt: the story rating's guidance appears, first, in every
structured render's prompt (the narrator's and the character's), in the age repair's, and in
the composition's polish (#152, ``composition.j2``).
"""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.config import Settings
from epistrel.core.ratings import story_guidance
from tests.auth_support import auth_settings
from tests.respond_support import body, respond, respond_client
from tests.structured_turn_support import ages, beat, render, structured_story

GENERATION = "generation"


@pytest.fixture
def stub_settings(pg_url: str) -> Settings:
    return auth_settings(pg_url, model_adapter="stub")


@pytest.mark.part("M4", "FR-RATE-4")
async def test_i_22__structured_guidance(
    stub_settings: Settings, engine: AsyncEngine, drained_embedding_jobs: None
) -> None:
    """A turn that brings the narrator in, introduces a character without an age and renders
    the character: each of its three ``generation`` prompts (narration, age repair, character
    render) opens with the story rating's guidance. Complement: a turn the narrator does not
    join makes one render, and its prompt carries the guidance too; a turn of two beats is
    composed by a polish call whose prompt opens with it (#152)."""
    async with respond_client(stub_settings, engine) as (client, epistrel):
        story, ids = await structured_story(client, "Wren", "Ada")
        async with engine.connect() as conn:
            guidance = (await story_guidance(conn, story)).prompt_guidance
        assert guidance
        stub = epistrel.models.stub
        stub.script(
            GENERATION,
            [
                render(
                    beat("narration", "A courier arrives."),
                    introduces=[{"name": "the courier", "clue": "a young courier"}],
                ),
                render(beat("do", "Ada nods.")),
                ages(**{"the courier": 25}),
            ],
        )
        response = await respond(client, story, body(ids["Wren"], None, do="waits outside"))
        assert response.status_code == 200, response.text
        calls = [c for c in stub.calls if c.purpose == GENERATION]
        # The two renders' beats are also polished (#156: the input's detection makes no
        # extraction call, so the default budget pays for the polish too).
        assert sorted(c.response_schema or "" for c in calls) == [
            "AgeRepairAnswer",
            "CharacterRender",
            "ComposedMessage",
            "NarratorRender",
        ]
        for call in calls:
            system = call.messages[0].content
            assert system.startswith("## Content rating guidance"), call.response_schema
            assert guidance in system, call.response_schema

        stub.reset()
        stub.script(GENERATION, [render(beat("say", "Hello.", intent="sincere"))])
        response = await respond(client, story, body(ids["Wren"], "Hello Ada."))
        assert response.status_code == 200, response.text
        (call,) = [c for c in stub.calls if c.purpose == GENERATION]
        assert guidance in call.messages[0].content

        # The composition's polish (#152): a turn of two beats is composed by one more
        # ``generation`` call, its prompt opening with the guidance too.
        stub.reset()
        stub.script(
            GENERATION,
            [render(beat("say", "Hello.", intent="sincere"), beat("do", "Ada smiles."))],
        )
        response = await respond(client, story, body(ids["Wren"], "Hello again."))
        assert response.status_code == 200, response.text
        assert response.json()["message"]["mode"] == "polished"
        (polish,) = [c for c in stub.calls if c.response_schema == "ComposedMessage"]
        assert polish.purpose == GENERATION
        assert polish.messages[0].content.startswith("## Content rating guidance")
        assert guidance in polish.messages[0].content
