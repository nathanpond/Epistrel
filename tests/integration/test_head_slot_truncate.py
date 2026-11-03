"""#625: truncating to the head discards only the head's inter-turn slots (FR-STORE-31: truncating
to N discards N's slots). A State written at ``N⁺1`` through the State API goes, turn N stays and
the head does not move; untruncate brings the slot back. Complement: truncating to the head
with nothing after it is 422."""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.config import Settings
from tests import effect_support as es
from tests import multi_support as ms
from tests import state_support as ss
from tests.auth_support import auth_settings
from tests.respond_support import body, played, respond_client
from tests.story_support import READ, WRITE
from tests.structured_turn_support import beat, structured_story

pytestmark = pytest.mark.integration


@pytest.fixture
def stub_settings(pg_url: str) -> Settings:
    return auth_settings(pg_url, model_adapter="stub")


async def test_truncating_to_the_head_discards_its_slots(
    stub_settings: Settings, engine: AsyncEngine, drained_embedding_jobs: None
) -> None:
    async with respond_client(stub_settings, engine) as (client, epistrel):
        stub = epistrel.models.stub
        story, ids = await structured_story(client, "Cal", "Mira")
        cal = ids["Cal"]
        await ss.set_level(engine, story, "on")
        await ss.define_all(engine, story, ss.gold())
        stub.reset()
        ms.renders_for(stub, "Mira", es.render(beat("say", "Welcome.")))
        head = (await played(client, story, body(cal, None, do="waves")))["turn"]

        refused = await client.post(
            f"/stories/{story}/truncate", json={"to_turn": head}, headers=WRITE
        )
        assert refused.status_code == 422, refused.text
        assert refused.json()["error"]["details"]["head"] == head

        put = await client.put(
            f"/stories/{story}/states/gold",
            json={"scope_key": cal, "delta": 5, "reason": "a purse"},
            headers=WRITE,
        )
        assert put.status_code == 200, put.text
        assert await es.gold_of(engine, story, cal) == 5

        cut = await client.post(f"/stories/{story}/truncate", json={"to_turn": head}, headers=WRITE)
        assert cut.status_code == 200, cut.text
        assert (cut.json()["to_turn"], cut.json()["discarded_operations"]) == (head, 1)
        assert await es.gold_of(engine, story, cal) == 0
        story_row = await client.get(f"/stories/{story}", headers=READ)
        assert story_row.json()["undo_available"] is True
        turns = await client.get(
            f"/stories/{story}/history", params={"viewpoint": "omniscient"}, headers=READ
        )
        assert [t["turn"] for t in turns.json()["turns"]][-1] == head

        back = await client.post(f"/stories/{story}/untruncate", headers=WRITE)
        assert back.status_code == 200, back.text
        assert await es.gold_of(engine, story, cal) == 5
