"""S-PROMOTE, M5's clause (docs/04 §7; FR-CAT-7; #163): a driver introduced by narration is
story-scoped, like one the author adds, and his true age is assigned when he is first modeled.

Turn 1: the player only looks out of the window, so the narrator renders: "A delivery driver
pulls up outside" is mere appearance, so no ``characters`` row exists, no age is asked for and
no age fact is written. Turn 2: Jenni greets the delivery driver, a direct interaction. He is
modeled in that turn's commit as a story character (no catalog entry, no catalog source), and
the turn's age repair (``AgeRepairAnswer``, naming him) assigns his one true age
(``true_age_source = repaired``), recorded with a canon ``age`` fact from turn 2. The omniscient
read shows his age, weight and ``modeled_at``.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.config import Settings
from epistrel.core.turns.beats import AGE
from epistrel.orchestrator.structured.render import DEFAULT_REPAIRED_AGE
from tests import modeling_support as ms
from tests.auth_support import auth_settings
from tests.event_support import rows, scalar
from tests.respond_support import body, respond, respond_client
from tests.story_support import WRITE
from tests.structured_turn_support import beat, render, schema_calls, text_of

pytestmark = pytest.mark.integration

ARRIVES = "A delivery driver pulls up outside with a parcel."
GREETS = "Jenni greets the delivery driver at the door."


@pytest.fixture
def stub_settings(pg_url: str) -> Settings:
    return auth_settings(pg_url, model_adapter="stub")


async def _age_facts(engine: AsyncEngine, story_id: str) -> list[Any]:
    return await rows(
        engine,
        "SELECT p.subject_id, f.narrative_turn FROM facts f "
        "JOIN propositions p ON p.id = f.proposition_id "
        "WHERE f.story_id = :s AND p.predicate_id = :age",
        s=story_id,
        age=AGE,
    )


@pytest.mark.part("M5", "FR-CAT-7")
async def test_s_promote__m5(
    stub_settings: Settings,
    engine: AsyncEngine,
    drained_embedding_jobs: None,
) -> None:
    """See the module docstring."""
    async with respond_client(stub_settings, engine) as (client, epistrel):
        story, ids, _ = await ms.street(client, engine, salience=None)
        entries = await scalar(engine, "SELECT count(*) FROM catalog.character_catalog")
        stub = epistrel.models.stub
        stub.reset()
        stub.script(
            "generation",
            [
                render(beat("narration", ARRIVES)),
                render(beat("say", "Who could that be?", intent="sincere")),
            ],
        )
        played = await respond(
            client, story, body(ids["Wren"], None, do="looks out of the window.")
        )
        assert played.status_code == 200, played.text
        narrated = await scalar(
            engine,
            "SELECT count(*) FROM events WHERE story_id = :s AND type = 'beat_narration' "
            "AND payload->>'text' = :t",
            s=story,
            t=ARRIVES,
        )
        assert narrated == 1
        # Appearance: no character, no age asked for, no age fact.
        assert set(await ms.characters_of(engine, story)) == {"Wren", "Jenni"}
        assert schema_calls(stub, "AgeRepairAnswer") == []
        before = {str(r.subject_id) for r in await _age_facts(engine, story)}

        stub.reset()
        stub.script("generation", [render(beat("do", GREETS))])
        played = await respond(client, story, body(ids["Wren"], "Hello there!"))
        assert played.status_code == 200, played.text
        cast = await ms.characters_of(engine, story)
        driver = cast["the delivery driver"]
        # Story-scoped: an ordinary story character, never a catalog entry.
        assert driver.source_catalog_id is None
        assert not driver.is_player
        assert await scalar(engine, "SELECT count(*) FROM catalog.character_catalog") == entries
        # His true age was assigned now, when he was first modeled.
        (repair,) = schema_calls(stub, "AgeRepairAnswer")
        assert "the delivery driver" in text_of(repair)
        assert (driver.true_age, driver.true_age_source) == (DEFAULT_REPAIRED_AGE, "repaired")
        assert (driver.modeled_turn, driver.weight) == (2, "minor")
        assert [
            int(r.narrative_turn)
            for r in await _age_facts(engine, story)
            if str(r.subject_id) == driver.id
        ] == [2]
        assert driver.id not in before
        read = await client.get(
            f"/stories/{story}", params={"viewpoint": "omniscient"}, headers=WRITE
        )
        assert read.status_code == 200, read.text
        listed = {c["name"]: c for c in read.json()["characters"]}["the delivery driver"]
        assert (listed["true_age"], listed["weight"]) == (DEFAULT_REPAIRED_AGE, "minor")
        assert (listed["modeled_at"]["turn"], listed["modeled_at"]["beat"]) == (2, 2)
