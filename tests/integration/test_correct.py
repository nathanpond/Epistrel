"""``correct`` (FR-RESP-14; #598, ADR-174): the latest turn replaced with new input in one
operation, on any story, and its interaction with a turn's external effects (FR-EXT-14).

- A Lite story: correcting turn 1 plays the replacement as variant 2, the original dormant;
  the replacement's ``player_input`` names the replaced operation (``corrected_from``); an
  ``Idempotency-Key`` retry replays the result; correcting the correction is 409
  ``already_corrected``; a turn that is not the latest (none yet, or one a later turn followed)
  is 409 ``not_latest_turn``, and a refusal writes nothing.
- A ``co_author`` story under partitions: a notice of a turn with a confirmed external effect
  says ``external_effects: true`` (the history read, as of the read); correcting the turn does
  not undo the effect, which stays confirmed and is listed in ``reconciliation_required``.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.config import Settings
from tests import structured_turn_support as st
from tests.auth_support import auth_settings
from tests.event_support import rows
from tests.integration.test_timeline_effects import _insert
from tests.respond_support import body, new_story, played, respond, respond_client
from tests.scenario.test_s_player_authority import TAKES, _knowledge_room, _narrate
from tests.story_support import READ, WRITE

pytestmark = pytest.mark.integration


@pytest.fixture
def stub_settings(pg_url: str) -> Settings:
    return auth_settings(pg_url, model_adapter="stub")


async def _correct(
    client: Any, story: str, turn: int, say: str, headers: dict[str, str] | None = None
) -> Any:
    return await client.post(
        f"/stories/{story}/correct",
        json={"turn": turn, "player_input": {"say": say}},
        headers={**WRITE, **(headers or {})},
    )


async def _inputs(engine: AsyncEngine, story: str) -> list[Any]:
    return await rows(
        engine,
        "SELECT e.operation_id, e.payload, o.membership::text AS membership FROM events e "
        "JOIN operations o ON o.operation_id = e.operation_id "
        "WHERE e.story_id = :s AND e.type = 'player_input' ORDER BY e.seq",
        s=story,
    )


async def _revision(client: Any, story: str) -> int:
    found = await client.get(f"/stories/{story}", headers=READ)
    return int(found.json()["timeline_revision"])


async def test_correct_lite(
    stub_settings: Settings, engine: AsyncEngine, drained_embedding_jobs: None
) -> None:
    """See the module docstring (the Lite story)."""
    async with respond_client(stub_settings, engine) as (client, epistrel):
        stub = epistrel.models.stub
        story, ids = await new_story(client)
        player = next(iter(ids.values()))
        refused = await _correct(client, story, 1, "Hello?")
        assert refused.status_code == 409, refused.text
        assert refused.json()["error"]["code"] == "not_latest_turn"
        stub.script("generation", ["A door creaks."])
        first = await played(client, story, body(player, "Is anyone there?"))
        stub.script("generation", ["Footsteps answer."])
        key = {"Idempotency-Key": "correct-1"}
        corrected = await _correct(client, story, 1, "Who goes there?", key)
        assert corrected.status_code == 200, corrected.text
        fixed = corrected.json()
        assert (fixed["turn"], fixed["variant"], fixed["previous_variant"]) == (1, 2, 1)
        assert fixed["message"]["text"] == "Footsteps answer."
        replayed = await _correct(client, story, 1, "Who goes there?", key)
        assert replayed.status_code == 200, replayed.text
        assert replayed.json()["message"] == fixed["message"]
        original, replacement = await _inputs(engine, story)
        assert (original.membership, original.payload["say"]) == ("dormant", "Is anyone there?")
        assert "corrected_from" not in original.payload
        assert (replacement.membership, replacement.payload["say"]) == (
            "active",
            "Who goes there?",
        )
        assert replacement.payload["corrected_from"] == original.operation_id
        assert first["turn"] == 1
        revision = await _revision(client, story)
        again = await _correct(client, story, 1, "Hello?")
        assert again.status_code == 409, again.text
        assert again.json()["error"]["code"] == "already_corrected"
        assert await _revision(client, story) == revision
        stub.script("generation", ["Silence."])
        await played(client, story, body(player, "Hello?"))
        revision = await _revision(client, story)
        late = await _correct(client, story, 1, "Hi.")
        assert late.status_code == 409, late.text
        assert late.json()["error"]["code"] == "not_latest_turn"
        assert late.json()["error"]["details"]["latest"] == 2
        assert await _revision(client, story) == revision
        assert len(await _inputs(engine, story)) == 3


async def test_correct_external_effects(
    stub_settings: Settings, engine: AsyncEngine, drained_embedding_jobs: None
) -> None:
    """See the module docstring (the confirmed effect)."""
    async with respond_client(stub_settings, engine) as (client, epistrel):
        stub = epistrel.models.stub
        story, ids, _, _ = await _knowledge_room(client, engine, canon="drawer")
        answer = await _narrate(client, stub, story, ids, TAKES)
        assert answer.status_code == 200, answer.text
        (notice,) = answer.json()["notices"]
        assert notice["external_effects"] is False
        (turn,) = await _inputs(engine, story)
        outbox_id = await _insert(engine, story, str(turn.operation_id), "confirmed")
        read = await client.get(
            f"/stories/{story}/history", params={"viewpoint": "omniscient"}, headers=READ
        )
        (summary,) = read.json()["turns"]
        assert summary["notices"] == [{**notice, "external_effects": True}]
        stub.reset()
        stub.script("generation", [st.render(st.beat("say", "Hm."))])
        corrected = await client.post(
            f"/stories/{story}/correct",
            json={"turn": 1, "player_input": {"do": "Jenni searches the desk."}},
            headers=WRITE,
        )
        assert corrected.status_code == 200, corrected.text
        assert [r["outbox_id"] for r in corrected.json()["reconciliation_required"]] == [outbox_id]
        (row,) = await rows(
            engine, "SELECT status FROM external_effect_outbox WHERE id = :i", i=outbox_id
        )
        assert row.status == "confirmed"
        # A turn with no notices at all can be corrected too (a plain respond's).
        stub.reset()
        stub.script("generation", [st.render(st.beat("say", "Hm."))])
        later = await respond(client, story, body(ids["Cal"], "Well?"))
        assert later.status_code == 200, later.text
        assert "notices" not in later.json()
