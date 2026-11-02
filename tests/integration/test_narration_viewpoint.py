"""#623 (owner decision): a narration delivered to a character viewpoint is bounded by what that
viewpoint may read. The narrator stating a hidden State's value ("Suspicion stands at 7.") in
a turn delivered to Cal's viewpoint is repaired, not ``supported`` at omniscient, and the value
never reaches the player. Complement: narration of a fact Cal's viewpoint reads ("Mira is at
the forge.") stands, ``supported``; and the same hidden value narrated in a turn delivered
``omniscient`` stands too."""

from __future__ import annotations

import json
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.config import Settings
from epistrel.core.enums import StateChangeSource
from tests import effect_support as es
from tests import multi_support as ms
from tests import perception_support as ps
from tests import state_support as ss
from tests.auth_support import auth_settings
from tests.respond_support import body, respond, respond_client
from tests.structured_turn_support import beat, claim, turn_report

pytestmark = pytest.mark.integration

STATED = "Suspicion stands at 7."
QUIET = "The forge is quiet."


@pytest.fixture
def stub_settings(pg_url: str) -> Settings:
    return auth_settings(pg_url, model_adapter="stub")


async def _narrate(
    client: Any,
    engine: AsyncEngine,
    stub: Any,
    story: str,
    cal: str,
    text: str,
    viewpoint: str | None = None,
    **fields: Any,
) -> tuple[dict[str, Any], dict[str, Any]]:
    stub.reset()
    es.narrator(stub, es.render(beat("narration", text, **fields)))
    ms.renders_for(stub, "Mira", es.render(beat("say", "Mind the anvil.")))
    stub.script("regeneration", [json.dumps({"text": QUIET})] * 3)
    request = body(cal, None, do="looks around")
    if viewpoint is not None:
        request["output_viewpoint"] = viewpoint
    answer = await respond(client, story, request)
    assert answer.status_code == 200, answer.text
    return answer.json(), await turn_report(engine, story)


def _narration(found: dict[str, Any]) -> str:
    return " ".join(b["text"] for b in found["beats"] if b["kind"] == "beat_narration")


async def test_narration_is_bounded_by_the_delivery_viewpoint(
    stub_settings: Settings, engine: AsyncEngine, drained_embedding_jobs: None
) -> None:
    """See the module docstring."""
    async with respond_client(stub_settings, engine) as (client, epistrel):
        stub = epistrel.models.stub
        story, ids = await es.shop(client, engine, es.suspicion())
        await ss.change(
            engine,
            story,
            ss.set_to("suspicion", 7, reason="a rumour", source=StateChangeSource.CONSUMER),
        )
        forge = await ps.place_entity(engine, story, "the forge")
        for who in (ids["Cal"], ids["Mira"]):
            await ps.put(engine, story, who, forge)

        found, report = await _narrate(client, engine, stub, story, ids["Cal"], STATED)
        assert "7" not in _narration(found), found["beats"]
        assert "7" not in found["message"]["text"]
        assert report.get("repairs"), report["verify"]
        kinds = [v.get("kind") for v in report["verify"]["verdicts"]]
        assert "supported" not in kinds, report["verify"]

        # Complement: narration the viewpoint reads stands.
        found, report = await _narrate(
            client,
            engine,
            stub,
            story,
            ids["Cal"],
            "Mira is at the forge.",
            propositions=[claim("Mira", "located_at", "the forge")],
        )
        assert _narration(found) == "Mira is at the forge."
        assert not report.get("repairs"), report.get("repairs")

        # Complement: delivered omniscient, the hidden value is the director's to narrate.
        found, report = await _narrate(
            client, engine, stub, story, ids["Cal"], STATED, viewpoint="omniscient"
        )
        assert _narration(found) == STATED
        assert not report.get("repairs"), report.get("repairs")
