"""S-MINOR-INTERIORITY (docs/04 §7; FR-EPI-11, FR-TURN-3, FR-VERIFY-9, NFR-PRIV-1).

#163's slice (``__minor_privacy``, FR-EPI-11): "minor" limits weight and sparsity, never
privacy. Jenni hands the delivery driver a tip, so he is modeled as a ``minor``. When the next
request names him to render, his private ``think`` ("She seems sad today.") is a first-class
memory of his partition (channel ``think``, visible to him alone) even under the minor's sparse
accrual, while Jenni and the player hold nothing of it and the player-facing response carries
neither the thought nor its words. #172 appends the grounded resurfacing and the
non-disclosure variants.
"""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.config import Settings
from tests import modeling_support as ms
from tests import perception_support as ps
from tests.auth_support import auth_settings
from tests.respond_support import body, respond, respond_client
from tests.structured_turn_support import beat, render

pytestmark = pytest.mark.integration

TIP = "Jenni hands the delivery driver a tip."
THOUGHT = "She seems sad today."
REPLY = "Thanks, have a good one."


@pytest.fixture
def stub_settings(pg_url: str) -> Settings:
    return auth_settings(pg_url, model_adapter="stub")


@pytest.mark.part("M5", "FR-EPI-11")
async def test_s_minor_interiority__minor_privacy(
    stub_settings: Settings,
    engine: AsyncEngine,
    drained_embedding_jobs: None,
    testimony_delivered: None,
) -> None:
    """See the module docstring. Complement: no other partition holds the thought."""
    async with respond_client(stub_settings, engine) as (client, epistrel):
        story, ids, _ = await ms.street(client, engine, salience=None)
        stub = epistrel.models.stub
        stub.reset()
        stub.script("generation", [render(beat("do", TIP))])
        played = await respond(client, story, body(ids["Wren"], "Morning!"))
        assert played.status_code == 200, played.text
        driver = (await ms.characters_of(engine, story))["the delivery driver"]
        assert driver.weight == "minor"

        stub.reset()
        stub.script(
            "generation",
            [render(beat("think", THOUGHT), beat("say", REPLY, intent="sincere"))],
        )
        played = await respond(
            client,
            story,
            body(ids["Wren"], "Here you go.", render_character_ids=[driver.id]),
        )
        assert played.status_code == 200, played.text
        mine = await ps.memories_of(engine, story, driver.id)
        thought = [m for m in mine if m.channel == "think"]
        assert [m.text for m in thought] == [f"the delivery driver thinks: {THOUGHT}"]
        assert thought[0].visibility == {"scope": "characters", "characters": [driver.id]}
        assert (2, f"the delivery driver says: {REPLY}") in await ms.memory_texts(
            engine, story, driver.id
        )
        # Nobody else holds it, and the player-facing response does not carry it.
        for other in (ids["Jenni"], ids["Wren"]):
            assert not [m for m in await ps.memories_of(engine, story, other) if "sad" in m.text]
        answer = played.json()
        assert "sad" not in answer["message"]["text"]
        assert all(b["kind"] != "think" for b in answer["beats"])
        assert not [b for b in answer["beats"] if "sad" in (b.get("text") or "")]
