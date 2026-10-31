"""U-08: external-fact snapshot (docs/04 §3; FR-EXT-4; #205).

Committing a reference to a (stubbed) external value records an ``external_read`` event and
the committed fact references it: provenance and authority ``external``, ``source_event_ids``
the read, ``valid_from`` the read's story-time, visibility ``{scope: engine}`` and the literal
the event recorded. Later live drift (the provider now answers 100) changes neither the event
nor the fact, also across a later turn and a projection rebuild.

Complement: a turn that makes no claim about the value commits no external fact.
"""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.config import Settings
from epistrel.core import projections, world_clock
from epistrel.core.membership import Position
from tests import effect_support as es
from tests import multi_support as ms
from tests import provider_support as ps
from tests import state_support as ss
from tests.auth_support import auth_settings
from tests.respond_support import body, respond, respond_client
from tests.structured_turn_support import beat, structured_story

pytestmark = pytest.mark.integration


@pytest.fixture
def stub_settings(pg_url: str) -> Settings:
    return auth_settings(pg_url, model_adapter="stub")


@pytest.mark.part("M6", "FR-EXT-4")
async def test_u_08(
    stub_settings: Settings,
    engine: AsyncEngine,
    states_delivered: None,
    drained_embedding_jobs: None,
) -> None:
    """See the module docstring."""
    name = ps.unique_name("ledger")
    await ps.register(engine, ps.in_process_spec(name))
    async with respond_client(stub_settings, engine) as (client, epistrel):
        stub = epistrel.models.stub
        fake = ps.ScriptedProvider(name).attach(epistrel)
        fake.script("gold", 300)
        story, ids = await structured_story(client, "Cal", "Mira")
        cal = ids["Cal"]
        await ss.set_level(engine, story, "on")
        gold = await ps.external_gold(engine, story, name)

        es.narrator(stub, es.render(beat("narration", "Rain taps. Cal has 300 gold left.")))
        ms.renders_for(stub, "Mira", es.render(beat("say", "Mind the edge.")))
        answer = await respond(client, story, body(cal, None, do="checks his purse"))
        assert answer.status_code == 200, answer.text

        (read,) = await ps.external_reads(engine, story)
        (fact,) = await ps.external_facts(engine, story)
        assert (fact.provenance, fact.authority) == ("external", "external")
        assert fact.visibility == {"scope": "engine"}
        assert fact.source_event_ids == [read.id]
        assert fact.subject_id == cal
        assert fact.literal == {"kind": "int", "value": read.payload["value"]}
        assert read.payload["value"] == 300
        async with engine.connect() as conn:
            minute = await world_clock.story_time_at(
                conn, story, Position(read.narrative_turn, read.turn_variant, 0, 0, 0)
            )
        assert fact.valid_from == minute
        assert gold.id == read.payload["state_id"]

        # Drift: the provider now answers 100. Neither the event nor the fact moves, through a
        # later turn (which states nothing about the gold) and a rebuild.
        fake.script("gold", 100)
        stub.reset()
        es.narrator(stub, es.render(beat("narration", "Wind rattles the door.")))
        ms.renders_for(stub, "Mira", es.render(beat("say", "Close it.")))
        later = await respond(client, story, body(cal, None, do="looks around"))
        assert later.status_code == 200, later.text
        await projections.rebuild(engine, story)
        first, second = await ps.external_reads(engine, story)
        assert (first.id, first.payload) == (read.id, read.payload)
        assert second.payload["value"] == 100
        assert await ps.external_facts(engine, story) == [fact]  # the complement: no new fact
        await ss.set_level(engine, story, None)
