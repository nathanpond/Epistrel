"""#635 (owner decision): narration may move an unmodeled person. "The stranger leaves the inn."
supersedes the stranger's open-ended placement (#518's ``fluent_change``) instead of being
contradicted by it and repaired; a narration contradicting a modeled character's placement with
no change cue is still repaired."""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.config import Settings
from epistrel.core.propositions import store
from tests import effect_support as es
from tests import multi_support as ms
from tests import perception_support as ps
from tests.auth_support import auth_settings
from tests.respond_support import body, respond, respond_client
from tests.structured_turn_support import beat, claim, structured_story, turn_report

pytestmark = pytest.mark.integration


@pytest.fixture
def stub_settings(pg_url: str) -> Settings:
    return auth_settings(pg_url, model_adapter="stub")


async def _narrate(
    client: Any, engine: AsyncEngine, stub: Any, story: str, cal: str, text: str, **fields: Any
) -> Any:
    stub.reset()
    es.narrator(stub, es.render(beat("narration", text, **fields)))
    ms.renders_for(stub, "Mira", es.render(beat("say", "Mind the step.")))
    answer = await respond(client, story, body(cal, None, do="looks around"))
    assert answer.status_code == 200, answer.text
    return answer.json(), await turn_report(engine, story)


async def test_a_narrator_moves_an_unmodeled_person(
    stub_settings: Settings, engine: AsyncEngine, drained_embedding_jobs: None
) -> None:
    """Complement asserted: "Mira is not at the inn." (a modeled character, no change cue)
    while she is placed there is contradicted and repaired."""
    async with respond_client(stub_settings, engine) as (client, epistrel):
        stub = epistrel.models.stub
        story, ids = await structured_story(client, "Cal", "Mira")
        inn = await ps.place_entity(engine, story, "the inn")
        await ps.place_entity(engine, story, "the market")
        async with engine.begin() as conn:
            stranger = await store.add_entity(conn, story, "person", "the stranger")
        for who in (ids["Cal"], ids["Mira"], stranger):
            await ps.put(engine, story, who, inn)

        found, report = await _narrate(
            client, engine, stub, story, ids["Cal"], "The stranger leaves the inn."
        )
        kinds = [v.get("kind") for v in report["verify"]["verdicts"]]
        assert "contradicted" not in kinds, report["verify"]
        assert not report.get("repairs"), report.get("repairs")
        assert "The stranger leaves the inn." in " ".join(b["text"] for b in found["beats"])

        _, report = await _narrate(
            client,
            engine,
            stub,
            story,
            ids["Cal"],
            "Mira is not at the inn.",
            propositions=[claim("Mira", "located_at", "the inn", negated=True)],
        )
        kinds = [v.get("kind") for v in report["verify"]["verdicts"]]
        assert "contradicted" in kinds, report["verify"]
