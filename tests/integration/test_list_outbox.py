"""#679: ``GET /stories/{id}/outbox`` lists every outbox row of a story whatever its status:
a confirmed row (which the reconciliation read no longer lists) and a failed one, each with its
outcome, position and effect; ``status`` and ``provider`` filter; paged by id; an omniscient
read (403 for a character viewpoint)."""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.config import Settings
from tests.auth_support import auth_settings
from tests.respond_support import respond_client
from tests.scenario.test_s_ext_writes import _dispatch, _spend_turn, _write_capable
from tests.story_support import READ

pytestmark = pytest.mark.integration


@pytest.fixture
def stub_settings(pg_url: str) -> Settings:
    return auth_settings(pg_url, model_adapter="stub")


async def _outbox(client: Any, story: str, **params: Any) -> dict[str, Any]:
    found = await client.get(
        f"/stories/{story}/outbox", params={"viewpoint": "omniscient", **params}, headers=READ
    )
    assert found.status_code == 200, found.text
    page: dict[str, Any] = found.json()
    return page


async def test_every_outbox_row_is_listed(
    stub_settings: Settings,
    engine: AsyncEngine,
    states_delivered: None,
    drained_embedding_jobs: None,
) -> None:
    async with respond_client(stub_settings, engine) as (client, epistrel):
        story, cal, fake = await _write_capable(engine, epistrel, client)
        paid = (await _spend_turn(client, epistrel, story, cal))["state_effects"][0]["outbox_id"]
        assert await _dispatch(engine, epistrel, paid) == "confirmed"
        fake.script_writes("gold", "failed")
        played = await _spend_turn(client, epistrel, story, cal)
        failed = played["state_effects"][0]["outbox_id"]
        assert await _dispatch(engine, epistrel, failed) == "failed"

        rows = (await _outbox(client, story))["rows"]
        by_id = {r["outbox_id"]: r for r in rows}
        assert [r["outbox_id"] for r in rows] == sorted(by_id)
        assert (by_id[paid]["status"], by_id[paid]["outcome"]["kind"]) == ("confirmed", "confirmed")
        assert (by_id[failed]["status"], by_id[failed]["outcome"]["kind"]) == ("failed", "failed")
        assert by_id[failed]["position"]["turn"] == played["turn"]
        assert by_id[paid]["effect"]["delta"] == -50
        assert "authorized" not in by_id[paid]["effect"]
        assert by_id[paid]["membership"] == "active"
        assert by_id[paid]["provider"] == by_id[failed]["provider"]

        only = (await _outbox(client, story, status="confirmed"))["rows"]
        assert [r["outbox_id"] for r in only] == [paid]
        assert (await _outbox(client, story, provider="nobody"))["rows"] == []
        first = await _outbox(client, story, limit=1)
        assert first["next_after"] == first["rows"][0]["outbox_id"]
        rest = await _outbox(client, story, after_id=first["next_after"])
        assert len(rest["rows"]) == len(rows) - 1

        refused = await client.get(
            f"/stories/{story}/outbox", params={"viewpoint": f"character:{cal}"}, headers=READ
        )
        assert refused.status_code == 403, refused.text
