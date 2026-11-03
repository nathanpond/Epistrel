"""#607: a structured turn's perception trace, recorded as routing decided it when the turn
ran and served on ``GET /operations/{id}`` for an ``omniscient`` read: at ``co_location`` with
Bobby and Jenni in the parlor and Marcus in the kitchen, each beat lists Bobby and Jenni as
``content`` perceivers and Marcus as ``unperceived``. A character-viewpoint read gets no
``perception`` block."""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.config import Settings
from tests import perception_support as ps
from tests import testimony_support as ts
from tests.auth_support import auth_settings
from tests.respond_support import body, played, respond_client
from tests.story_support import READ
from tests.structured_turn_support import beat, render

pytestmark = pytest.mark.integration


@pytest.fixture
def stub_settings(pg_url: str) -> Settings:
    return auth_settings(pg_url, model_adapter="stub")


async def test_a_turn_serves_its_perception_trace(
    stub_settings: Settings, engine: AsyncEngine, drained_embedding_jobs: None
) -> None:
    try:
        async with respond_client(stub_settings, engine) as (client, epistrel):
            story, ids, _ = await ts.room(
                client, engine, "Wren", "Jenni", "Bobby", "Marcus", away=("Marcus",)
            )
            await ps.perceive(engine, story, "co_location")
            epistrel.models.stub.script("generation", [render(beat("say", "Evening, all."))])
            found = await played(
                client, story, body(ids["Wren"], "Hello?", render_character_ids=[ids["Jenni"]])
            )
            operation = found["beats"][0]["operation_id"]

            read = await client.get(
                f"/operations/{operation}", params={"viewpoint": "omniscient"}, headers=READ
            )
            assert read.status_code == 200, read.text
            block = read.json()["perception"]
            assert block["level"] == "co_location"
            trace = block["trace"]
            assert trace
            assert len(trace) == block["routed"]
            for entry in trace:
                heard = {p["character_id"]: p for p in entry["perceivers"]}
                assert {ids["Jenni"], ids["Bobby"]} <= set(heard), entry
                assert all(p["fidelity"] == "content" for p in heard.values())
                assert ids["Marcus"] in entry["unperceived"], entry
                assert "text" not in entry

            mine = await client.get(
                f"/operations/{operation}",
                params={"viewpoint": f"character:{ids['Wren']}"},
                headers=READ,
            )
            assert mine.status_code == 200, mine.text
            assert mine.json()["perception"] is None
    finally:
        await ps.forget_perception(engine)
