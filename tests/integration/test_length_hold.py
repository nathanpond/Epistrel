"""A structured turn is held to the requested length (#464; FR-RESP-3, NFR-QUAL-7).

The render is asked for ``4 x length`` tokens (its JSON's overhead), so the engine holds what
it releases: the render prompt names the length as a target in words, beats past the length
(counted over the beats the output viewpoint receives) are dropped before verification, and a
polish may not take the message past it. Complement: the same over-long render under a long
enough length keeps every beat."""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.config import Settings
from tests.auth_support import auth_settings
from tests.respond_support import body, respond, respond_client
from tests.story_support import override
from tests.structured_turn_support import beat, render, renders_of, structured_story

pytestmark = pytest.mark.integration

SAY = "Rooms are two silver a night, and the stew is mutton."
DO = "Maren wipes the counter " + "slowly, " * 40 + "and looks up."
THINK = "Another traveller with questions."
LENGTH = 30  # tokens: 120 characters


@pytest.fixture
def stub_settings(pg_url: str) -> Settings:
    return auth_settings(pg_url, model_adapter="stub")


async def _turn(
    settings: Settings, engine: AsyncEngine, length: int, viewpoint: str
) -> dict[str, Any]:
    async with respond_client(settings, engine) as (client, epistrel):
        story, ids = await structured_story(client, "Wren", "Maren")
        await override(client, story, {"repair": {"max_attempts": 0}})
        stub = epistrel.models.stub
        stub.script(
            "generation",
            [render(beat("think", THINK), beat("say", SAY, intent="sincere"), beat("do", DO))],
        )
        output = "omniscient" if viewpoint == "omniscient" else f"character:{ids['Wren']}"
        response = await respond(
            client,
            story,
            body(
                ids["Wren"],
                "Is there a room?",
                prompt_params={"length": length},
                output_viewpoint=output,
            ),
        )
        assert response.status_code == 200, response.text
        (call, *_) = renders_of(stub)
        prompt = "\n".join(m.content for m in call.messages)
        return {"played": response.json(), "prompt": prompt}


def _channels(played: dict[str, Any]) -> list[str]:
    return [b["channel"] for b in played["beats"] if b["kind"].startswith("beat_")]


@pytest.mark.parametrize("viewpoint", ["omniscient", "character"])
async def test_render_is_held_to_the_requested_length(
    stub_settings: Settings, engine: AsyncEngine, viewpoint: str, drained_embedding_jobs: None
) -> None:
    found = await _turn(stub_settings, engine, LENGTH, viewpoint)
    played = found["played"]
    # The say fits; the do would take the reply past 120 characters: dropped, never verified,
    # never committed. The think counts only where the viewpoint receives it.
    expected = ["think", "say"] if viewpoint == "omniscient" else ["say"]
    assert _channels(played) == expected
    assert len(played["message"]["text"]) <= 1.25 * 4 * LENGTH
    assert "slowly" not in played["message"]["text"]
    assert f"about {round(LENGTH * 0.75)} words" in found["prompt"]


async def test_long_enough_length_keeps_every_beat(
    stub_settings: Settings, engine: AsyncEngine, drained_embedding_jobs: None
) -> None:
    found = await _turn(stub_settings, engine, 400, "omniscient")
    assert _channels(found["played"]) == ["think", "say", "do"]
    assert "slowly" in found["played"]["message"]["text"]
