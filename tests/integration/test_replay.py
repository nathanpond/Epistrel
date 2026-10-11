"""docs/04 REPLAY, M2 part: folding the event log is deterministic."""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.core.projections import fold, rebuild
from tests.event_support import interleave, new_story, play_turn
from tests.projection_support import snapshot, swap, truncate


@pytest.mark.part("M2", "FR-STORE-4")
async def test_replay(engine: AsyncEngine) -> None:
    """Two folds of the same log produce identical projection rows, and two rebuilds store
    identical bytes. Complement: the fold is not constant: a further turn changes it."""
    story_id = await new_story(engine)
    for n in range(1, 5):
        await play_turn(engine, story_id, f"turn {n}")
        await interleave(engine, story_id, after_turn=n)
    await truncate(engine, story_id, 2)
    await play_turn(engine, story_id, "turn 3, again")
    await swap(engine, story_id)

    async with engine.connect() as conn:
        first = {name: list(rows) for name, rows in (await fold(conn, story_id)).items()}
        second = {name: list(rows) for name, rows in (await fold(conn, story_id)).items()}
    assert first == second
    assert len(first["transcript_messages"]) == 2 * 5

    await rebuild(engine, story_id)
    stored_once = await snapshot(engine, story_id)
    await rebuild(engine, story_id)
    assert await snapshot(engine, story_id) == stored_once

    await play_turn(engine, story_id, "turn 5")
    async with engine.connect() as conn:
        third = {name: list(rows) for name, rows in (await fold(conn, story_id)).items()}
    assert third != first
