"""``POST /admin/stories/{id}/beliefs``: an author-directed belief (FR-EPI-7, FR-BELIEF-4; #160).

With the ``partitions`` module on for the story,
the call writes an ``author`` transition citing its own author act (201), changes a belief the
holder already holds in place, and closes one it contradicts; its dual confidence is checked
(422 ``confidence_inconsistent``), the player's character has no partition (422
``holder_has_no_partition``), and an idempotent repeat answers the stored result. Without the
module the call is 409 ``module_off`` and writes nothing.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.config import Settings
from epistrel.core.epistemic import author, transitions
from epistrel.core.profiles.schema import ProfileSettings
from tests import canon_support as c
from tests import partition_support as ps
from tests.auth_support import bearer
from tests.erasure_support import erase, erasure_settings
from tests.memory_support import use_structured
from tests.respond_support import respond_client

pytestmark = pytest.mark.integration


def _headers(key: str | None = None) -> dict[str, str]:
    found = {**bearer(), "X-Epistrel-Principal": "p-author"}
    if key is not None:
        found["Idempotency-Key"] = key
    return found


async def test_write_belief(
    engine: AsyncEngine, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = await c.world(engine)
    story = w.story_id
    await use_structured(engine, story)
    ann, cal = w.characters["Ann"], w.characters["Cal"]
    garden = c.at(w.people["Bobby"], w.places["garden"], 0, None).model_dump(mode="json")
    kitchen = c.at(w.people["Bobby"], w.places["kitchen"], 0, None).model_dump(mode="json")
    path = f"/stories/{story}/beliefs"

    async with respond_client(settings, engine) as (client, _):
        off = await client.post(
            f"/admin{path}", json={"holder": ann, "proposition": garden}, headers=_headers()
        )
        assert (off.status_code, off.json()["error"]["code"]) == (409, "module_off")
        assert await ps.belief_rows(engine, story) == []

        on = await ps.partitioned(engine, story)

        async def partitioned(db: Any, story_id: str) -> ProfileSettings:
            del db, story_id
            return on

        monkeypatch.setattr(author, "require_partitions", partitioned)

        first = await client.post(
            f"/admin{path}",
            json={"holder": ann, "proposition": garden, "confidence_scalar": 0.6},
            headers=_headers("belief-1"),
        )
        assert first.status_code == 201, first.text
        body = first.json()
        belief, transition = body["belief"], body["transition"]
        assert (belief["holder_character_id"], belief["confidence_level"]) == (ann, "believed")
        assert (belief["confidence_scalar"], belief["provenance"]) == (0.6, transition["id"])
        assert transition["kind"] == "author"
        assert transition["antecedent"]["kind"] == "record"
        assert transition["antecedent"]["ref"]["table"] == "events"
        assert body["closed"] == []

        replay = await client.post(
            f"/admin{path}",
            json={"holder": ann, "proposition": garden, "confidence_scalar": 0.6},
            headers=_headers("belief-1"),
        )
        assert replay.status_code == 201
        assert replay.headers.get("Idempotent-Replay") == "true"
        assert replay.json()["belief"]["id"] == belief["id"]

        # The holder learns otherwise: the garden belief is closed, its history kept.
        moved = await client.post(
            f"/admin{path}",
            json={"holder": ann, "proposition": kitchen, "confidence_level": "known"},
            headers=_headers(),
        )
        assert moved.status_code == 201, moved.text
        assert moved.json()["closed"] == [belief["id"]]
        assert moved.json()["belief"]["lineage_id"] == belief["id"]

        bad = await client.post(
            f"/admin{path}",
            json={
                "holder": ann,
                "proposition": garden,
                "confidence_level": "known",
                "confidence_scalar": 0.3,
            },
            headers=_headers(),
        )
        assert bad.status_code == 422
        assert bad.json()["error"]["code"] == "confidence_inconsistent"
        player = await client.post(
            f"/admin{path}", json={"holder": cal, "proposition": garden}, headers=_headers()
        )
        assert (player.status_code, player.json()["error"]["code"]) == (
            422,
            "holder_has_no_partition",
        )
    assert len(await ps.belief_rows(engine, story)) == 2


async def test_erasure_takes_transitions_with_their_holder(
    pg_url: str, engine: AsyncEngine
) -> None:
    """[ERASE-PLUMB] (#160): erasing the source of Ann's testimony keeps her belief and its
    transition, without the source; erasing Ann deletes her beliefs and their transitions."""
    w = await c.world(engine)
    story = w.story_id
    await use_structured(engine, story)
    ann, bobby = w.characters["Ann"], w.characters["Bobby"]
    fact = await c.commit(engine, story, c.at(w.people["Cal"], w.places["kitchen"], 0, None))
    told = await ps.transit(
        engine,
        story,
        ann,
        c.at(w.people["Cal"], w.places["kitchen"], 0, None),
        cause=transitions.Kind.DISCOVERY,
        antecedent=transitions.Antecedent.record("facts", fact.id),
        source=bobby,
    )
    assert told.transition.source_character_id == bobby
    sql = "SELECT id, source_character_id FROM belief_transitions WHERE story_id = :s"
    async with respond_client(erasure_settings(pg_url), engine) as (client, _):
        await erase(client, "character", bobby, story, confirm_all=True)
        kept = await ps.rows_of(engine, sql, s=story)
        assert [(r.id, r.source_character_id) for r in kept] == [(told.transition.id, None)]
        assert len(await ps.belief_rows(engine, story)) == 1
        await erase(client, "character", ann, story, confirm_all=True)
    assert await ps.rows_of(engine, sql, s=story) == []
    assert await ps.belief_rows(engine, story) == []
