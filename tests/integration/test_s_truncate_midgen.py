"""docs/04 S-TRUNCATE-MIDGEN, M2 part: a control action while a turn is generating."""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.core.errors import TimelineChanged
from epistrel.core.views import corrected_history
from tests.concurrency_support import (
    chain,
    event_count,
    operation_row,
    revision,
    start_turn,
)
from tests.event_support import new_story, play_turn
from tests.projection_support import swap, truncate


@pytest.mark.part("M2", "FR-CONC-4")
async def test_s_truncate_midgen(engine: AsyncEngine) -> None:
    """Truncate (and, separately, swap) while a stub-model turn is mid-generation: the turn is
    abandoned with 409 `timeline_changed`, none of its events exist, and the control action
    stands. Complement: the same turn released with no control action in between commits."""
    story = await new_story(engine)
    for n in range(1, 4):
        await play_turn(engine, story, f"turn {n}")

    held = await start_turn(engine, story, "a reply nobody wants any more")
    cut = await truncate(engine, story, 1)
    assert cut is not None
    await held.release()
    with pytest.raises(TimelineChanged) as caught:
        await held.task
    assert caught.value.http_status == 409
    assert caught.value.details == {
        "current_revision": await revision(engine, story),
        "control_operation_id": cut.operation_id,
    }
    assert held.model.calls == 1
    assert (await operation_row(engine, held.op.operation_id))[0] == "abandoned"
    assert await event_count(engine, held.op.operation_id) == 0
    assert await chain(engine, story) == [(1, 1)]
    texts = [e.payload.model_dump() for e in await corrected_history(engine, story)]
    assert {"text": "turn 2"} not in texts
    assert {"text": "a reply nobody wants any more"} not in texts

    await play_turn(engine, story, "turn 2, again")
    held = await start_turn(engine, story, "also unwanted")
    switched = await swap(engine, story)
    assert switched is not None
    await held.release()
    with pytest.raises(TimelineChanged):
        await held.task
    assert await event_count(engine, held.op.operation_id) == 0
    assert [t for t, _ in await chain(engine, story)] == [1, 2, 3]

    wanted = await start_turn(engine, story, "turn 4")
    await wanted.release()
    result = await wanted.task
    assert [a["outcome"] for a in result.attempts] == ["commit"]
    assert await event_count(engine, wanted.op.operation_id) == 2
