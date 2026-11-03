"""#656 and #657: a turn's requested effects as history and a recomposed response list them.

- History lists the changes a turn requested (an outbox row's dispatched effect, a read-only
  provider's consumer-applied request) beside the ones it committed, each ``owner: external``
  with its current status: ``pending`` until answered, then the answer (``confirmed``). Hidden
  from no one but a ``hidden`` State's, as the response.
- The response's ``state_effects`` reads each request's current status too, and an outbox
  row keeps the value the turn read (``pinned``, internal: never listed nor sent), so a
  history entry's (and a resumed turn's recomposed) ``old_value`` is that value, not
  ``null``.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.config import Settings
from epistrel.core import outbox as core_outbox
from epistrel.orchestrator.structured import staged
from tests import effect_support as es
from tests import multi_support as ms
from tests import provider_support as ps
from tests import state_support as ss
from tests.auth_support import auth_settings
from tests.respond_support import body, respond, respond_client
from tests.scenario.test_s_ext_writes import _dispatch, _spend, _spend_turn, _write_capable
from tests.story_support import READ
from tests.structured_turn_support import beat, structured_story

pytestmark = pytest.mark.integration


@pytest.fixture
def stub_settings(pg_url: str) -> Settings:
    return auth_settings(pg_url, model_adapter="stub")


async def _effects(client: Any, story: str, turn: int) -> list[dict[str, Any]]:
    found = await client.get(
        f"/stories/{story}/history", params={"viewpoint": "omniscient"}, headers=READ
    )
    assert found.status_code == 200, found.text
    (summary,) = [t for t in found.json()["turns"] if t["turn"] == turn]
    effects: list[dict[str, Any]] = summary["state_effects"]
    return effects


async def test_an_outbox_request_is_in_history_with_its_status(
    stub_settings: Settings,
    engine: AsyncEngine,
    states_delivered: None,
    drained_embedding_jobs: None,
) -> None:
    async with respond_client(stub_settings, engine) as (client, epistrel):
        story, cal, _fake = await _write_capable(engine, epistrel, client)
        played = await _spend_turn(client, epistrel, story, cal)
        (effect,) = played["state_effects"]
        assert (effect["status"], effect["old_value"]) == ("pending", 300)
        (row,) = await ps.outbox_rows(engine, story)
        assert row.effect["pinned"] == 300  # #657: kept, internal
        assert "pinned" not in core_outbox.public_effect(row.effect)

        (listed,) = await _effects(client, story, played["turn"])
        assert listed == effect  # the response's entry, as history lists it

        assert await _dispatch(engine, epistrel, row.id) == "confirmed"
        (listed,) = await _effects(client, story, played["turn"])
        assert (listed["status"], listed["outbox_id"]) == ("confirmed", row.id)

        # #657: the response recomposed from the committed turn keeps the read value.
        recomposed = await staged.recompose(engine, row.operation_id)
        (again,) = recomposed.state_effects
        assert (again["old_value"], again["status"]) == (300, "confirmed")


async def test_a_consumer_request_is_in_history(
    stub_settings: Settings,
    engine: AsyncEngine,
    states_delivered: None,
    drained_embedding_jobs: None,
) -> None:
    name = ps.unique_name("ledger")
    await ps.register(engine, ps.in_process_spec(name))
    async with respond_client(stub_settings, engine) as (client, epistrel):
        stub = epistrel.models.stub
        fake = ps.ScriptedProvider(name).attach(epistrel)
        fake.script("gold", 300, 260)
        story, ids = await structured_story(client, "Cal", "Mira")
        cal = ids["Cal"]
        await ss.set_level(engine, story, "on")
        await ps.external_gold(engine, story, name, authorization=es.ANY_CHARACTER)
        stub.reset()
        ms.renders_for(stub, "Mira", _spend(cal))
        es.narrator(stub, es.render(beat("narration", "The forge glows.")))
        answer = await respond(client, story, body(cal, None, do="spends 50 gold on a sword"))
        assert answer.status_code == 200, answer.text
        played = answer.json()
        (effect,) = played["state_effects"]

        (listed,) = await _effects(client, story, played["turn"])
        assert {k: v for k, v in listed.items() if k != "old_value"} == {
            k: v for k, v in effect.items() if k != "old_value"
        }
        assert (listed["owner"], listed["status"], listed["delta"]) == ("external", "pending", -50)
