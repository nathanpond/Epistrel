"""NUL characters against PostgreSQL (#275, #278; FR-API-4): a turn whose generation holds NUL
commits with the character removed instead of failing its commit with a database error, and the
library's reads (and a gated mutation's id) refuse one with 422 instead of passing it to SQL.
The DB-free halves are in ``tests/test_nul_text.py``."""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.config import Settings
from epistrel.core.errors import InvalidRequest
from epistrel.core.story_texts import PinnedMemoriesRequest
from tests.auth_support import auth_settings
from tests.event_support import scalar
from tests.respond_support import (
    AUTHOR,
    PLAYER,
    body,
    new_story,
    played,
    respond_client,
    story_text,
)

REPLY = "Ada\x00 lifts the lantern.\x00"


@pytest.fixture
def stub_settings(pg_url: str) -> Settings:
    return auth_settings(pg_url, model_adapter="stub")


async def test_a_generation_holding_nul_commits_without_it(
    stub_settings: Settings, engine: AsyncEngine
) -> None:
    """The stub's generation carries NUL: the turn answers 200 and its stored rows hold the text
    with the NUL removed. Complement asserted: the next turn, without NUL, commits too."""
    async with respond_client(stub_settings, engine) as (client, lib):
        story, ids = await new_story(client)
        lib.models.stub.reset()
        lib.models.stub.script("generation", [REPLY, "The rain keeps falling."])
        await played(client, story, body(ids[PLAYER]))
        await played(client, story, body(ids[PLAYER], "and then?"))
        stored = await story_text(engine, story)
        assert "Ada lifts the lantern." in stored
        assert "The rain keeps falling." in stored


def _refused(error: InvalidRequest) -> tuple[str, list[str]]:
    return error.details["field"], [e["type"] for e in error.details["errors"]]


async def test_library_reads_refuse_nul_with_422(
    stub_settings: Settings, engine: AsyncEngine
) -> None:
    """#278's examples (``get_story``, ``history`` with ``actor_character_id``, ``get_operation``)
    and a gated mutation's ``story_id`` are 422 ``string_nul`` at the argument, not a database
    error. Complement asserted: the same reads without the NUL answer."""
    async with respond_client(stub_settings, engine) as (client, lib):
        story, ids = await new_story(client)
        await played(client, story, body(ids[PLAYER]))
        turn = await scalar(
            engine, "SELECT operation_id FROM operations WHERE story_id = :s LIMIT 1", s=story
        )
        bad = "a\x00b"
        calls: list[tuple[str, Any]] = [
            ("story_id", lib.get_story(AUTHOR, bad)),
            ("query.actor_character_id", lib.history(AUTHOR, story, actor_character_id=bad)),
            ("operation_id", lib.get_operation(AUTHOR, bad)),
            (
                "story_id",
                lib.set_pinned_memories(
                    AUTHOR, bad, PinnedMemoriesRequest.model_validate({"items": [{"text": "m"}]})
                ),
            ),
        ]
        for field, call in calls:
            with pytest.raises(InvalidRequest) as refused:
                await call
            assert _refused(refused.value) == (field, ["string_nul"])

        assert (await lib.get_story(AUTHOR, story)).id == story
        await lib.history(AUTHOR, story, actor_character_id=ids[PLAYER])
        await lib.get_operation(AUTHOR, turn)
