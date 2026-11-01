"""The State API's own rules (#186), beside the S-STATE-SCHEMA, C-12 and S-TRUST-BOUNDARY parts:
the ``as_of`` boundary, the turn slot a set takes like a turn (``reject`` refuses, ``queue``
waits), the world clock's place (only under ``clock``, never set or patched here), and the
active-name constraint's backstop in the database."""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.core import turn_gate
from epistrel.core.context import RequestContext
from epistrel.core.errors import InvalidRequest, StateNotFound, StoryBusy
from epistrel.core.ids import new_ulid
from epistrel.core.membership import POSITION_MAX, Position
from epistrel.core.states.api import StateSetRequest
from epistrel.core.timeline import boundary_label, parse_as_of
from epistrel.engine import Engine
from tests import state_support as st
from tests.auth_support import auth_settings
from tests.event_support import rows
from tests.respond_support import new_story, respond_client
from tests.story_support import READ

AUTHOR = RequestContext(principal="p-author")
SET = StateSetRequest(delta=1, reason="a coin")


def test_as_of_boundaries() -> None:
    """``N`` is turn N's end without its slots, ``N+k`` (``⁺`` and a decoded ``+``, a space,
    too) includes slots 1..k; a beat, a step or anything else is 422."""
    assert parse_as_of("14") == Position.end_of(14)
    assert parse_as_of(14) == Position.end_of(14)
    for written in ("14+2", "14⁺2", "14 2", " 14 + 2 "):
        assert parse_as_of(written) == Position.end_of(14, 2), written
    for wrong in ("", "-1", "14+", "14.2", "14+2+1", "x", f"1+{POSITION_MAX + 1}"):
        with pytest.raises(InvalidRequest):
            parse_as_of(wrong)
    assert (boundary_label(14), boundary_label(14, 2)) == ("14", "14⁺2")


async def _story(engine: AsyncEngine, epistrel: Engine, client: object) -> str:
    story, _ = await new_story(client)  # type: ignore[arg-type]
    await st.set_level(engine, story, "prompt_only")
    await st.declared(epistrel, story, name="gold", type="number", initial_value=0)
    return story


async def test_a_set_waits_for_or_is_refused_by_a_turn_in_flight(
    pg_url: str, engine: AsyncEngine, states_delivered: None
) -> None:
    """A set takes the story's turn slot as a turn does (#63): with a turn holding it, the
    story's ``reject`` refuses it 423 ``story_busy`` (nothing written), and ``queue`` makes it
    wait until the turn gives the slot back, then it commits."""
    async with respond_client(auth_settings(pg_url, model_adapter="stub"), engine) as (
        client,
        epistrel,
    ):
        story = await _story(engine, epistrel, client)
        holder = new_ulid()
        held = await turn_gate.acquire(engine, story, holder, lease_s=60)
        assert held is None
        before = len(await st.change_rows(engine, story))
        with pytest.raises(StoryBusy) as busy:
            await epistrel.set_state(AUTHOR, story, "gold", SET)
        assert busy.value.details["reason"] == "in_flight"
        assert len(await st.change_rows(engine, story)) == before

        async with engine.begin() as conn:
            await conn.execute(
                text("UPDATE stories SET turn_concurrency = 'queue' WHERE id = :s"), {"s": story}
            )
        waiting = asyncio.create_task(epistrel.set_state(AUTHOR, story, "gold", SET))
        await asyncio.sleep(0.5)
        assert not waiting.done()
        await turn_gate.release_now(engine, story, holder)
        written = await asyncio.wait_for(waiting, 30)
        assert written.new_value == 1
        (row,) = await rows(engine, "SELECT in_flight_turn FROM stories WHERE id = :s", s=story)
        assert row.in_flight_turn is None


async def test_the_world_clock_is_not_a_state_of_the_api(
    pg_url: str, engine: AsyncEngine, states_delivered: None
) -> None:
    """``world_clock`` is listed only under ``clock``; a named read of it is 404, and a
    definition change of it is 422 ``reserved_state`` (``PATCH /clock`` changes it)."""
    async with respond_client(auth_settings(pg_url, model_adapter="stub"), engine) as (
        client,
        epistrel,
    ):
        story = await _story(engine, epistrel, client)
        listed = await epistrel.get_states(AUTHOR, story, viewpoint="omniscient")
        assert [s.name for s in listed.states] == ["gold"]
        assert listed.clock.offset == 0
        response = await client.get(
            f"/stories/{story}/states/world_clock",
            params={"viewpoint": "omniscient"},
            headers=READ,
        )
        assert (response.status_code, response.json()["error"]["code"]) == (404, "not_found")
        with pytest.raises(StateNotFound):
            await epistrel.get_state(AUTHOR, story, "world_clock", viewpoint="omniscient")
        with pytest.raises(InvalidRequest) as reserved:
            await epistrel.update_state_definition(AUTHOR, story, "world_clock", {"mode": "manual"})
        assert reserved.value.details["reason"] == "reserved_state"


async def test_the_database_holds_one_active_name(
    pg_url: str, engine: AsyncEngine, states_delivered: None
) -> None:
    """The deferred ``ex_state_definitions_active_name`` constraint (migration 0088) refuses a
    second active State of one name at the commit, and lets a discarded one share it."""
    async with respond_client(auth_settings(pg_url, model_adapter="stub"), engine) as (
        client,
        epistrel,
    ):
        story = await _story(engine, epistrel, client)
        copy = (
            "INSERT INTO state_definitions (id, story_id, name, type, scope, visibility, owner, "
            "created_by, operation_id, membership, membership_tx) SELECT :i, story_id, name, type, "
            "scope, visibility, owner, created_by, operation_id, CAST(:m AS membership), "
            "membership_tx "
            "FROM state_definitions WHERE story_id = :s AND name = 'gold' "
            "AND membership = 'active'"
        )
        async with engine.begin() as conn:
            await conn.execute(text(copy), {"i": new_ulid(), "m": "discarded", "s": story})
        with pytest.raises(IntegrityError, match="ex_state_definitions_active_name"):
            async with engine.begin() as conn:
                await conn.execute(text(copy), {"i": new_ulid(), "m": "active", "s": story})
