"""#680: ``GET /stories/{id}/states/{name}/definition`` answers a State's stored definition as
its declaration and the last definition change left it, with ``created_by`` and the
configuration revision of that change; a ``hidden`` State's is 404 for a character viewpoint,
as the State read is, and readable ``omniscient``."""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.config import Settings
from tests import state_support as ss
from tests.auth_support import auth_settings
from tests.respond_support import respond_client
from tests.story_support import READ, WRITE
from tests.structured_turn_support import structured_story

pytestmark = pytest.mark.integration

PURSE = {
    "name": "purse",
    "type": "number",
    "scope": "character",
    "initial_value": 10,
    "constraints": {"min": 0, "max": 500},
    "aliases": ["coins"],
    "unit": "gp",
}


@pytest.fixture
def stub_settings(pg_url: str) -> Settings:
    return auth_settings(pg_url, model_adapter="stub")


async def test_the_stored_definition_is_served(
    stub_settings: Settings, engine: AsyncEngine
) -> None:
    async with respond_client(stub_settings, engine) as (client, _epistrel):
        story, ids = await structured_story(client, "Cal", "Mira")
        await ss.set_level(engine, story, "on")
        for definition in (PURSE, {"name": "trust", "type": "number", "visibility": "hidden"}):
            made = await client.post(
                f"/stories/{story}/states", json={"definition": definition}, headers=WRITE
            )
            assert made.status_code == 201, made.text

        path = f"/stories/{story}/states/purse/definition"
        read = await client.get(path, params={"viewpoint": "omniscient"}, headers=READ)
        assert read.status_code == 200, read.text
        found = read.json()
        assert found["name"] == "purse"
        assert found["created_by"] == "consumer"
        assert found["config_revision"] is None
        definition = found["definition"]
        assert (definition["constraints"]["min"], definition["constraints"]["max"]) == (0, 500)
        assert (definition["aliases"], definition["unit"], definition["owner"]) == (
            ["coins"],
            "gp",
            "story",
        )

        patched = await client.patch(
            f"/stories/{story}/states/purse/definition", json={"unit": "coins"}, headers=WRITE
        )
        assert patched.status_code == 200, patched.text
        again = (await client.get(path, params={"viewpoint": "omniscient"}, headers=READ)).json()
        assert again["definition"]["unit"] == "coins"
        assert isinstance(again["config_revision"], int)

        hidden = f"/stories/{story}/states/trust/definition"
        as_cal = await client.get(
            hidden, params={"viewpoint": f"character:{ids['Cal']}"}, headers=READ
        )
        assert as_cal.status_code == 404, as_cal.text
        omniscient = await client.get(hidden, params={"viewpoint": "omniscient"}, headers=READ)
        assert omniscient.status_code == 200, omniscient.text
        assert omniscient.json()["definition"]["visibility"] == "hidden"
