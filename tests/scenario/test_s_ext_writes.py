"""S-EXT-WRITES: changes to game-owned values (docs/04 §7; FR-EXT-9, FR-RESP-7).

Every S-EXT-WRITES M6 part lives in this file: #209's read-only part (``__read_only``) and
#210's write-capable part.

#209's part, played end to end through ``respond`` with the stub adapter: Cal's gold is owned by
a registered provider that is not ``write_capable`` (an in-process ``ScriptedProvider``) and
reads 300. "I spend 50 gold": Mira's render declares ``gold -50``. Epistrel never applies it:
no ``state_changes`` row is written, a ``requested_effect`` event (``input``, at the declaring
beat, ``{state, scope_key, delta, reason, effect_id, status: requested}``) is recorded, and the
response returns it with ``owner: external``, ``status: pending`` and its ``effect_id``. The
pinned value and ``external_values`` are unchanged (300), and the next turn's live read is
whatever the adapter says (here 260, as the game applied it).

Complement: the same effect on a story-owned State commits a change (300 → 250).
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
from tests.event_support import rows
from tests.respond_support import body, respond, respond_client
from tests.structured_turn_support import beat, structured_story, turn_report

pytestmark = pytest.mark.integration

SOLD = "Mira takes the coins and hands Cal the sword."
REASON = "bought a sword"
ULID_LENGTH = 26


@pytest.fixture
def stub_settings(pg_url: str) -> Settings:
    return auth_settings(pg_url, model_adapter="stub")


def _spend(cal: str) -> str:
    return es.render(
        beat("do", SOLD, effects=[es.effect("gold", scope_key=cal, delta=-50, reason=REASON)]),
        beat("say", "Mind the edge.", intent="sincere"),
    )


async def requested(engine: AsyncEngine, story: str) -> list[Any]:
    """The story's ``requested_effect`` events, in log order."""
    return await rows(
        engine,
        "SELECT id, payload, category, beat, step, narrative_turn FROM events "
        "WHERE story_id = :s AND type = 'requested_effect' ORDER BY seq",
        s=story,
    )


@pytest.mark.part("M6", "FR-EXT-9; FR-RESP-7")
async def test_s_ext_writes__read_only(
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
        fake.script("gold", 300, 260)
        story, ids = await structured_story(client, "Cal", "Mira")
        cal = ids["Cal"]
        await ss.set_level(engine, story, "on")
        await ps.external_gold(engine, story, name, authorization=es.ANY_CHARACTER)

        # "I spend 50 gold": returned, not applied.
        stub.reset()
        ms.renders_for(stub, "Mira", _spend(cal))
        es.narrator(stub, es.render(beat("narration", "The forge glows.")))
        answer = await respond(client, story, body(cal, None, do="spends 50 gold on a sword"))
        assert answer.status_code == 200, answer.text
        played = answer.json()
        (effect,) = played["state_effects"]
        effect_id = effect.pop("effect_id")
        assert len(effect_id) == ULID_LENGTH
        assert effect == {
            "state": "gold",
            "scope_key": cal,
            "old_value": 300,
            "new_value": None,
            "reason": REASON,
            "source": "turn",
            "contingency": "unconditional",
            "status": "pending",
            "owner": "external",
            "delta": -50,
        }
        assert await es.turn_changes(engine, story) == []
        (event,) = await requested(engine, story)
        assert event.category == "input"
        assert event.payload == {
            "effect_id": effect_id,
            "kind": "change",
            "state": "gold",
            "scope_key": cal,
            "delta": -50,
            "reason": REASON,
            "status": "requested",
        }
        do_beat = next(b for b in played["beats"] if b["text"] == SOLD)
        assert (event.narrative_turn, event.beat) == (played["turn"], do_beat["beat"])
        report = (await turn_report(engine, story))["state_effects"]
        assert (report["committed"], report["requested"]) == (0, 1)

        # The pin and external_values are unchanged: the one read, 300.
        assert [r["value"] for r in await ps.external_value_rows(engine, story)] == [300]

        # The next turn reads live again: whatever the adapter says (the game applied it).
        stub.reset()
        es.narrator(stub, es.render(beat("narration", "Wind rattles the door.")))
        ms.renders_for(stub, "Mira", es.render(beat("say", "Close it.")))
        again = await respond(client, story, body(cal, None, do="looks around"))
        assert again.status_code == 200, again.text
        assert "state_effects" not in again.json()
        assert [r.payload["value"] for r in await ps.external_reads(engine, story)] == [300, 260]
        assert len(await requested(engine, story)) == 1
        assert await es.turn_changes(engine, story) == []

        # Complement: the same effect on a story-owned State commits a change.
        owned, owned_ids = await es.shop(client, engine, es.gold(), gold_of_player=300)
        stub.reset()
        ms.renders_for(stub, "Mira", _spend(owned_ids["Cal"]))
        es.narrator(stub, es.render(beat("narration", "The forge glows.")))
        plain = await respond(
            client, owned, body(owned_ids["Cal"], None, do="spends 50 gold on a sword")
        )
        assert plain.status_code == 200, plain.text
        assert [
            (e["old_value"], e["new_value"], e["status"]) for e in plain.json()["state_effects"]
        ] == [(300, 250, "committed")]
        assert "effect_id" not in plain.json()["state_effects"][0]
        assert await requested(engine, owned) == []
        assert len(await es.turn_changes(engine, owned)) == 1
        await ss.set_level(engine, story, None)
        await ss.set_level(engine, owned, None)
