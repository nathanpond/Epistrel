"""#681: ``GET /stories/{id}/operations`` lists a story's operations by id, filterable by
``label`` (derived as the by-id read derives it), ``kind`` and ``status``: a staged turn (a
purchase on a synchronous provider), a plain turn and the story's creation."""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.config import Settings
from tests import effect_support as es
from tests import multi_support as ms
from tests import staged_support as sts
from tests.auth_support import auth_settings
from tests.respond_support import body, played, respond_client
from tests.story_support import READ
from tests.structured_turn_support import beat

pytestmark = pytest.mark.integration


@pytest.fixture
def stub_settings(pg_url: str) -> Settings:
    return auth_settings(pg_url, model_adapter="stub")


async def _listed(client: Any, story: str, **params: Any) -> dict[str, Any]:
    found = await client.get(f"/stories/{story}/operations", params=params, headers=READ)
    assert found.status_code == 200, found.text
    page: dict[str, Any] = found.json()
    return page


async def test_operations_are_listed_by_label(
    stub_settings: Settings,
    engine: AsyncEngine,
    states_delivered: None,
    drained_embedding_jobs: None,
) -> None:
    async with respond_client(stub_settings, engine) as (client, epistrel):
        story, cal, _name, fake = await sts.sync_story(client, engine, epistrel)
        fake.script_writes("gold", "confirmed")
        sts.script_purchase(epistrel.models.stub, cal)
        bought = await sts.buy(client, story, cal)
        assert bought.status_code == 200, bought.text
        epistrel.models.stub.reset()
        ms.renders_for(epistrel.models.stub, "Mira", es.render(beat("say", "Anything else?")))
        await played(client, story, body(cal, None, do="nods"))

        staged = await _listed(client, story, label="staged_turn")
        (row,) = staged["operations"]
        assert (row["kind"], row["label"], row["staged"]) == ("turn", "staged_turn", True)

        (genesis,) = (await _listed(client, story, label="genesis"))["operations"]
        assert genesis["kind"] == "genesis"

        turns = (await _listed(client, story, kind="turn"))["operations"]
        assert len(turns) >= 2
        assert [t["operation_id"] for t in turns] == sorted(t["operation_id"] for t in turns)
        assert {t["label"] for t in turns} == {"staged_turn", None}
        assert row["operation_id"] in {t["operation_id"] for t in turns}

        first = await _listed(client, story, kind="turn", limit=1)
        assert first["next_after"] == first["operations"][0]["operation_id"]
        rest = await _listed(client, story, kind="turn", after_id=first["next_after"])
        assert len(rest["operations"]) == len(turns) - 1
        assert rest["next_after"] is None

        bad = await client.get(
            f"/stories/{story}/operations", params={"label": "nope"}, headers=READ
        )
        assert bad.status_code == 422
