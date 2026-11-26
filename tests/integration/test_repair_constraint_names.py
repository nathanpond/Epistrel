"""#1024 (FR-REPAIR-1: "regenerate under corrected constraints"): a regeneration's
``do_not_assert`` constraint names every entity in the contradicted claim, including a place
the turn itself introduced (minted by ``core.verify.introduce``, not yet an entity row), and
never by its id. Played end to end with the stub adapter over #144's key room: Bobby is in the
study, and Jenni says he is in the cellar.

No part marker: FR-REPAIR-1's part is I-12's (M4); this guards a defect found in M12.
"""

from __future__ import annotations

import re

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.config import Settings
from tests import multi_support as ms
from tests import perception_support as ps
from tests.auth_support import auth_settings
from tests.fixtures.key_room import key_room
from tests.respond_support import body, respond, respond_client
from tests.structured_turn_support import beat, claim

pytestmark = pytest.mark.integration

ULID = re.compile(r"\b[0-9A-HJKMNP-TV-Z]{26}\b")
CELLAR = "Do not assert that Bobby located_at the cellar."


@pytest.fixture
def stub_settings(pg_url: str) -> Settings:
    return auth_settings(pg_url, model_adapter="stub")


@pytest.mark.parametrize("placed", [False, True], ids=["introduced", "placed"])
async def test_regeneration_constraint_names_the_place(
    stub_settings: Settings, engine: AsyncEngine, drained_embedding_jobs: None, placed: bool
) -> None:
    """The cellar is a place the turn introduces (``introduced``; before #1024 the constraint
    read "Bobby located_at <its id>") or one already placed (``placed``, the complement): the
    regeneration's constraints name it, and no constraint line carries an id."""
    async with respond_client(stub_settings, engine) as (client, epistrel):
        stub = epistrel.models.stub
        room = await key_room(client, engine)
        if placed:
            await ps.place_entity(engine, room.story_id, "the cellar")
        stub.reset()
        said = beat(
            "say",
            "Bobby is in the cellar.",
            intent="sincere",
            propositions=[claim("Bobby", "located_at", "the cellar")],
        )
        ms.renders_for(stub, "Jenni", ms.render(said))
        answer = await respond(
            client,
            room.story_id,
            body(room.cal, "Where is Bobby?", render_character_ids=[room.jenni]),
        )
        assert answer.status_code == 200, answer.text
        (prompt,) = [
            "\n".join(m.content for m in c.messages)
            for c in stub.calls
            if c.purpose == "regeneration"
        ]
        constraints = [line for line in prompt.splitlines() if line.startswith("- Do not")]
        assert f"- {CELLAR}" in constraints, constraints
        assert not [line for line in constraints if ULID.search(line)], constraints
