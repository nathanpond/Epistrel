"""S-FORK, M6 part: duplicate-at-a-point, full copy, external values replayed (docs/04 §7;
FR-STORE-14,15,17; #205).

Cal's gold is owned by a provider. The source plays two turns that read it (300, then 320) and
is forked at the first. The fork is a full independent copy: its reconstruction of the external
values through that turn equals the source's (replayed from the copied ``external_read``
events, never re-fetched: the fork makes no adapter call), under its own ids (no event shared
with the source). Each side then plays a turn of its own, and neither sees the other's.

Complement asserted: a new turn in the fork is new work and reads the provider live (one more
call, its value recorded in the fork alone).
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.config import Settings
from tests import effect_support as es
from tests import multi_support as ms
from tests import provider_support as ps
from tests import state_support as ss
from tests.auth_support import auth_settings
from tests.fork_support import fork
from tests.respond_support import body, respond, respond_client
from tests.structured_turn_support import beat, structured_story

pytestmark = pytest.mark.integration


@pytest.fixture
def stub_settings(pg_url: str) -> Settings:
    return auth_settings(pg_url, model_adapter="stub")


@pytest.mark.part("M6", "FR-STORE-14,15,17")
async def test_s_fork__m6(
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
        story, ids = await structured_story(client, "Cal", "Mira")
        await ss.set_level(engine, story, "on")
        await ps.external_gold(engine, story, name)

        async def turn(on: str, cal: str, said: str) -> Any:
            stub.reset()
            es.narrator(stub, es.render(beat("narration", said)))
            ms.renders_for(stub, "Mira", es.render(beat("say", "Mind the edge.")))
            answer = await respond(client, on, body(cal, None, do="checks his purse"))
            assert answer.status_code == 200, answer.text
            return answer.json()["turn"]

        fake.script("gold", 300, 320, story_id=story)
        first = await turn(story, ids["Cal"], "Rain taps. Cal has 300 gold left.")
        await turn(story, ids["Cal"], "Wind rattles the door.")
        source = await ps.reconstruction(engine, story)
        assert [r[5] for r in source] == [300, 320]
        calls = len(fake.reads)

        forked = await fork(engine, story, first)
        copy = forked.story_id
        assert len(fake.reads) == calls  # replayed from the copied events, never re-fetched
        assert await ps.reconstruction(engine, copy) == [r for r in source if r[0] <= first]
        mine = {r.id for r in await ps.external_reads(engine, copy)}
        theirs = {r.id for r in await ps.external_reads(engine, story)}
        assert len(mine) == 1
        assert mine.isdisjoint(theirs)

        # Each side diverges on its own; a new turn in the fork reads live again.
        fork_cal = {c.name: c.id for c in forked.characters}["Cal"]
        fake.script("gold", 999, story_id=copy)
        fake.script("gold", 330, story_id=story)
        await turn(copy, fork_cal, "Snow drifts in.")
        assert len(fake.reads) == calls + 1
        assert fake.reads[-1] == ("gold", copy, fork_cal)
        await turn(story, ids["Cal"], "Night falls.")
        assert [r[5] for r in await ps.reconstruction(engine, copy)] == [300, 999]
        assert [r[5] for r in await ps.reconstruction(engine, story)] == [300, 320, 330]
        await ss.set_level(engine, story, None)
        await ss.set_level(engine, copy, None)
