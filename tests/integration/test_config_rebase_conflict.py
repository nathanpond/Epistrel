"""FR-CONC-4/14 (#64 AC3): an interpretive configuration change committed while a turn generates
makes the turn re-check its reply; a failed re-check regenerates within the story's budget, and
with the budget spent the turn is refused 409 ``revision_conflict`` (reason ``rebase_failed``)
with nothing committed (#291)."""

from __future__ import annotations

import asyncio

import httpx2 as httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.config import Settings
from epistrel.engine import Engine
from tests.auth_support import auth_settings
from tests.event_support import execute_in_tx, rows, scalar
from tests.policy_support import global_policy, words
from tests.respond_support import PLAYER, body, new_story, respond, respond_client


@pytest.fixture
def stub_settings(pg_url: str) -> Settings:
    return auth_settings(pg_url, model_adapter="stub")


async def _set_budget(engine: AsyncEngine, story: str, budget: int) -> None:
    await execute_in_tx(
        engine,
        (
            "UPDATE stories SET settings = coalesce(settings, '{}'::jsonb) || "
            "jsonb_build_object('regenerate_retry_budget', CAST(:b AS int)) WHERE id = :s",
            {"b": budget, "s": story},
        ),
    )


async def _messages(engine: AsyncEngine, story: str) -> list[str]:
    found = await rows(
        engine,
        "SELECT payload->>'text' AS text FROM events "
        "WHERE story_id = :s AND type = 'message_rendered' ORDER BY seq",
        s=story,
    )
    return [r.text for r in found]


async def _revision(engine: AsyncEngine, story: str) -> int:
    return int(await scalar(engine, "SELECT timeline_revision FROM stories WHERE id = :s", s=story))


async def _ban_mid_turn(
    client: httpx.AsyncClient,
    lib: Engine,
    engine: AsyncEngine,
    story: str,
    actor: str,
    *,
    word: str,
    budget: int,
) -> httpx.Response:
    """Play a turn whose held first reply uses ``word``; ban ``word`` on output while it is
    held (an interpretive change in the turn's dependencies), then release it."""
    stub = lib.models.stub
    stub.reset()
    stub.script("generation", [f"A {word} hums in the dark.", "Rain falls on the roof."])
    await _set_budget(engine, story, budget)
    latch = stub.hold("generation")
    task = asyncio.ensure_future(respond(client, story, body(actor, "Look.")))
    await asyncio.wait_for(latch.entered.wait(), 5)
    await global_policy(lib, words(word), applies_to=["text_output"])
    latch.release()
    return await task


async def test_rebase_failure_with_budget_spent_is_a_conflict(
    stub_settings: Settings, engine: AsyncEngine
) -> None:
    """Budget 0: the held reply breaks a policy added mid-turn, so the turn is 409
    ``rebase_failed``: its operation abandoned, no message written, the revision unchanged.

    Complement asserted: with budget 1 the same change regenerates and commits a passing reply."""
    async with respond_client(stub_settings, engine) as (client, lib):
        story, ids = await new_story(client)
        messages, revision = await _messages(engine, story), await _revision(engine, story)

        refused = await _ban_mid_turn(
            client, lib, engine, story, ids[PLAYER], word="zorbulon", budget=0
        )
        assert refused.status_code == 409, refused.text
        error = refused.json()["error"]
        assert error["code"] == "revision_conflict"
        assert error["details"]["reason"] == "rebase_failed"
        assert await _messages(engine, story) == messages
        assert await _revision(engine, story) == revision
        (last,) = await rows(
            engine,
            "SELECT status FROM operations WHERE story_id = :s AND kind = 'turn' "
            "ORDER BY operation_id DESC LIMIT 1",
            s=story,
        )
        assert last.status == "abandoned"

        committed = await _ban_mid_turn(
            client, lib, engine, story, ids[PLAYER], word="quexalt", budget=1
        )
        assert committed.status_code == 200, committed.text
        assert await _messages(engine, story) == [*messages, "Rain falls on the roof."]
        assert await _revision(engine, story) == revision + 1
