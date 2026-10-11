"""S-TURN-CONCURRENCY: per-story ``reject`` vs ``queue`` (docs/04 §7; #63).

Two stories: a roleplay story created without the setting (so ``reject``, the default) and a
game story with ``turn_concurrency = queue``. Overlapping turns are fired at each while the
first one's generation is held by the stub. The roleplay story refuses the second with 423
``story_busy`` and generates nothing for it; the game story queues the second, starts it only
after the first committed, generates it from the post-commit story, and commits both as
consecutive turns; one turn more than ``turn_queue_max`` is refused busy. The two stories
never block each other (asserted by timing: both turns finish within one stub latency), and a
change of ``turn_concurrency`` through ``PATCH /stories/{id}/settings`` applies to the next
request.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx2 as httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.config import Settings
from tests.auth_support import auth_settings
from tests.event_support import rows
from tests.respond_support import (
    FIRST,
    PLAYER,
    body,
    generation_calls,
    new_story,
    respond,
    respond_client,
)
from tests.story_support import READ, WRITE

pytestmark = pytest.mark.integration

STUB_LATENCY_S = 1.0
"""The stub's per-call latency for the two-stories timing assertion."""


@pytest.fixture
def stub_settings(pg_url: str) -> Settings:
    return auth_settings(pg_url, model_adapter="stub")


async def _settings(client: httpx.AsyncClient, story: str, **patch: Any) -> dict[str, Any]:
    response = await client.patch(f"/stories/{story}/settings", json=patch, headers=WRITE)
    assert response.status_code == 200, response.text
    settings: dict[str, Any] = response.json()["settings"]
    return settings


async def _turns(engine: AsyncEngine, story: str) -> list[Any]:
    return await rows(
        engine,
        "SELECT operation_id, status, narrative_turn FROM operations "
        "WHERE story_id = :s AND kind = 'turn' ORDER BY narrative_turn NULLS LAST, started_at",
        s=story,
    )


async def _held(stub: Any, client: httpx.AsyncClient, story: str, actor: str) -> Any:
    """Start a turn whose generation the stub holds; return (latch, the pending request)."""
    latch = stub.hold("generation")
    pending = asyncio.ensure_future(respond(client, story, body(actor, "Who goes there?")))
    await asyncio.wait_for(latch.entered.wait(), 5)
    return latch, pending


@pytest.mark.part("M2", "FR-CONC-5")
async def test_s_turn_concurrency(stub_settings: Settings, engine: AsyncEngine) -> None:
    """``reject`` refuses the overlapping turn with nothing generated; ``queue`` plays it next
    against the post-commit story; overflow is busy; two stories never block each other; a
    runtime change applies to the next request; no setting means ``reject``.

    Complements asserted: the refused request succeeds once the first turn finished, and
    under ``queue`` the overlapping request is answered 200 (not refused)."""
    async with respond_client(stub_settings, engine) as (client, epistrel):
        stub = epistrel.models.stub

        # A story created without the setting behaves as ``reject``.
        roleplay, rids = await new_story(client)
        read = await client.get(f"/stories/{roleplay}/settings", headers=READ)
        assert read.json()["settings"]["turn_concurrency"] == "reject"
        latch, first = await _held(stub, client, roleplay, rids[PLAYER])
        calls = len(generation_calls(stub))
        busy = await respond(client, roleplay, body(rids[FIRST], "Me too!"))
        assert busy.status_code == 423, busy.text
        error = busy.json()["error"]
        assert error["code"] == "story_busy"
        assert error["details"]["reason"] == "in_flight"
        (running,) = await _turns(engine, roleplay)
        assert error["details"]["operation_id"] == running.operation_id
        assert int(busy.headers["Retry-After"]) >= 1
        assert len(generation_calls(stub)) == calls  # nothing generated for it
        latch.release()
        assert (await first).status_code == 200
        assert [t.status for t in await _turns(engine, roleplay)] == ["committed"]
        assert len(generation_calls(stub)) == calls
        again = await respond(client, roleplay, body(rids[FIRST], "Me too!"))
        assert again.status_code == 200, again.text

        # ``queue``: the second turn waits for the first, then plays against what it left.
        game, gids = await new_story(client, settings={"turn_concurrency": "queue"})
        latch, first = await _held(stub, client, game, gids[PLAYER])
        calls = len(generation_calls(stub))
        second = asyncio.ensure_future(respond(client, game, body(gids[FIRST], "My move.")))
        await asyncio.sleep(0.3)
        assert not second.done()
        assert [t.status for t in await _turns(engine, game)] == ["in_flight", "queued"]
        assert len(generation_calls(stub)) == calls  # not started while the first runs
        latch.release()
        answered = [await first, await second]
        assert [r.status_code for r in answered] == [200, 200], [r.text for r in answered]
        first_reply, second_reply = (r.json() for r in answered)
        assert [first_reply["turn"], second_reply["turn"]] == [1, 2]
        assert second_reply["timeline_revision"] == first_reply["timeline_revision"] + 1
        played = [(t.status, t.narrative_turn) for t in await _turns(engine, game)]
        assert played == [("committed", 1), ("committed", 2)]
        later = generation_calls(stub)[calls]
        sent = "\n".join(m.content for m in later.messages)
        assert first_reply["message"]["text"] in sent  # generated after the first committed

        # Overflow: one waiting turn more than ``turn_queue_max`` is refused busy.
        assert (await _settings(client, game, turn_queue_max=1))["turn_queue_max"] == 1
        latch, first = await _held(stub, client, game, gids[PLAYER])
        waiting = await respond(
            client,
            game,
            body(gids[FIRST], "Queued."),
            headers={"Prefer": "respond-async"},
        )
        assert waiting.status_code == 202, waiting.text
        assert waiting.json()["position"] == 1
        full = await respond(client, game, body(gids[FIRST], "One too many."))
        assert full.status_code == 423, full.text
        assert full.json()["error"]["details"]["reason"] == "queue_full"
        assert full.json()["error"]["details"]["queue_depth"] == 1
        latch.release()
        assert (await first).status_code == 200
        await asyncio.wait_for(epistrel.drainer.idle(), 10)
        statuses = [(t.status, t.narrative_turn) for t in await _turns(engine, game)]
        assert statuses == [("committed", n) for n in range(1, 5)]

        # Two stories never block each other: both turns finish within one stub latency.
        other, oids = await new_story(client)
        stub.latency_s = STUB_LATENCY_S
        try:
            started = time.monotonic()
            both = await asyncio.gather(
                respond(client, roleplay, body(rids[PLAYER], "Side A.")),
                respond(client, other, body(oids[PLAYER], "Side B.")),
            )
            elapsed = time.monotonic() - started
        finally:
            stub.latency_s = 0.0
        assert [r.status_code for r in both] == [200, 200], [r.text for r in both]
        assert elapsed < 2 * STUB_LATENCY_S * 0.9, elapsed

        # A runtime change applies to the next request: ``reject`` → ``queue`` → ``reject``.
        assert (await _settings(client, roleplay, turn_concurrency="queue"))[
            "turn_concurrency"
        ] == "queue"
        latch, first = await _held(stub, client, roleplay, rids[PLAYER])
        queued = await respond(
            client, roleplay, body(rids[FIRST], "Now I wait."), headers={"Prefer": "respond-async"}
        )
        assert queued.status_code == 202, queued.text
        assert (await _settings(client, roleplay, turn_concurrency=None))[
            "turn_concurrency"
        ] == "reject"
        refused = await respond(client, roleplay, body(rids[FIRST], "And now?"))
        assert refused.status_code == 423, refused.text
        latch.release()
        assert (await first).status_code == 200
        await asyncio.wait_for(epistrel.drainer.idle(), 10)
        ended = [t.status for t in await _turns(engine, roleplay)]
        assert ended == ["committed"] * 5, ended
