"""A reply's "you" resolves to the turn's input actor during verification (#483; FR-PROP-6,
FR-VERIFY-11; docs/02 §4.9).

The structured check reads its resolution contexts at the turn's start, before the turn writes
its beats, so the player's own input is not in the context window. With no scene and no earlier
beats, a 1:1 reply's "you" had no antecedent and its claim was ``uncertain(unresolved_entity)``.
Now the input's actor is among the people a reply addresses: the claim names the player's
character, and FR-VERIFY-11 denies it as before (``player_character``), so the beat is still
repaired; only the verdict's reason is now the right one.
"""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.config import Settings
from tests.auth_support import auth_settings
from tests.detection_support import claims_answer
from tests.respond_support import body, respond, respond_client
from tests.structured_turn_support import beat, claim, render, structured_story, turn_report

pytestmark = pytest.mark.integration

REPLY = "I saw you pocket the brass key."


@pytest.fixture
def stub_settings(pg_url: str) -> Settings:
    return auth_settings(pg_url, model_adapter="stub")


async def test_a_replys_you_is_the_inputs_actor(
    stub_settings: Settings, engine: AsyncEngine, drained_embedding_jobs: None
) -> None:
    """Maren answers Wren (the player) on the story's first turn: her ``say``'s declared
    claim about "you" is the player character's, denied ``player_character`` and repaired, not
    ``uncertain(unresolved_entity)``."""
    async with respond_client(stub_settings, engine) as (client, epistrel):
        story, ids = await structured_story(client, "Wren", "Maren")
        stub = epistrel.models.stub
        stub.reset()
        stub.script(
            "generation",
            [
                render(
                    beat(
                        "say",
                        REPLY,
                        intent="sincere",
                        propositions=[claim("you", "holds", "the brass key")],
                    )
                )
            ],
        )
        stub.script("extraction_backstop", [claims_answer()])
        response = await respond(client, story, body(ids["Wren"], "Have you seen my key?"))
        assert response.status_code == 200, response.text
        report = await turn_report(engine, story)
        (verdict,) = report["verify"]["verdicts"]
        assert verdict["reason"] != "unresolved_entity"
        assert verdict["permission"] == "denied:player_character"
        (denied,) = report["verify"]["denied"]
        assert (denied["reason"], denied["asserter"]) == (
            "player_character",
            f"character:{ids['Maren']}",
        )
        # FR-VERIFY-11 is unchanged: the claim about the player's character is repaired.
        assert report["repairs"]
        assert report["commit"]["canonized"] == 0
