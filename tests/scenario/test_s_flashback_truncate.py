"""S-FLASHBACK-TRUNCATE: membership, not valid-time (FR-STORE-10, FR-BITEMP-5; docs/04 §7), its
canon half (#133) and its memory half (#135, ``test_s_flashback_truncate__memories``).

Scaled down by a hundred: turn 9 ("message 900") introduces "Bobby broke his arm at age seven",
valid decades before the story-time of checkpoint 8 ("800"). Truncating to 8 takes the fact out
of the active timeline although its valid-time precedes the checkpoint; its valid-time and
transaction-time are unchanged; untruncation restores it. The complement: a fact written
before the checkpoint whose valid-time lies *after* the checkpoint's story-time stays, so
valid-time is not what decides; and the record as it stood before the truncation still shows
the flashback (transaction-time keeps the history).
"""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.core.propositions import store
from epistrel.core.propositions.model import Interval, Proposition
from tests.canon_support import (
    as_recorded,
    at_time,
    auto_predicate,
    commit,
    fact_rows,
    world,
)
from tests.event_support import play_turn
from tests.memory_support import memory_rows, turn, use_structured
from tests.projection_support import truncate, untruncate

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("drained_embedding_jobs")]

YEAR = 525_600
"""Story-minutes in a year."""
CHECKPOINT_TIME = 30 * YEAR
"""The story-time of checkpoint 8: Bobby is grown up."""


@pytest.mark.part("M4", "FR-BITEMP-5; FR-STORE-10")
async def test_s_flashback_truncate(engine: AsyncEngine) -> None:
    w = await world(engine)
    story = w.story_id
    bobby = w.people["Bobby"]
    broke = await auto_predicate(engine, story, "broke")
    async with engine.begin() as conn:
        arm_entity = await store.add_entity(conn, story, "item", "his left arm")
    promised = await auto_predicate(engine, story, "will visit")

    for n in range(1, 7):
        await play_turn(engine, story, f"message {n}")
    # Turn 7: a plan for after the checkpoint's story-time (valid-time later, position earlier).
    later = await commit(
        engine,
        story,
        Proposition(
            subject_id=bobby,
            predicate_id=promised,
            object_id=w.people["Ann"],
            valid_time=Interval.of(CHECKPOINT_TIME + YEAR, CHECKPOINT_TIME + 2 * YEAR),
        ),
    )
    await play_turn(engine, story, "message 8")
    # Turn 9: the flashback, at age seven.
    arm = await commit(
        engine,
        story,
        Proposition(
            subject_id=bobby,
            predicate_id=broke,
            object_id=arm_entity,
            valid_time=Interval.of(7 * YEAR, 7 * YEAR + 60 * 24 * 42),
        ),
    )
    rows = await fact_rows(engine, story)
    assert (rows[later.id].narrative_turn, rows[arm.id].narrative_turn) == (7, 9)
    flashback_time = 7 * YEAR + 10
    assert [f.id for f in await at_time(engine, story, flashback_time)] == [arm.id]
    recorded_at = rows[arm.id].tx_from

    await truncate(engine, story, 8)
    after = await fact_rows(engine, story)
    assert str(after[arm.id].membership) == "discarded"
    assert after[arm.id].membership_tx > rows[arm.id].membership_tx
    for column in ("valid_from", "valid_to", "valid_from_inner", "valid_to_inner", "tx_from"):
        assert getattr(after[arm.id], column) == getattr(rows[arm.id], column), column
    assert after[arm.id].tx_to is None
    assert await at_time(engine, story, flashback_time) == []
    # Complement: valid-time after the checkpoint, position before it: still canon.
    assert str(after[later.id].membership) == "active"
    assert [f.id for f in await at_time(engine, story, CHECKPOINT_TIME + YEAR + 1)] == [later.id]
    # The record as it stood before the truncation still says it.
    assert arm.id in {f.id for f in await as_recorded(engine, story, recorded_at)}

    await untruncate(engine, story)
    restored = await fact_rows(engine, story)
    assert str(restored[arm.id].membership) == "active"
    assert restored[arm.id].valid_from == rows[arm.id].valid_from
    assert [f.id for f in await at_time(engine, story, flashback_time)] == [arm.id]


@pytest.mark.part("M4", "FR-STORE-10")
async def test_s_flashback_truncate__memories(engine: AsyncEngine) -> None:
    """The memory half: turn 9 narrates the flashback ("Bobby broke his arm at age seven");
    its episodic memory leaves the active timeline when the story is truncated to 8, every
    other column unchanged, and comes back with the untruncation. Complement: turn 7's memory,
    before the checkpoint, stays active."""
    w = await world(engine)
    story = w.story_id
    await use_structured(engine, story)
    made: dict[int, str] = {}
    for n in range(1, 10):
        reply = "Bobby broke his arm at age seven." if n == 9 else f"message {n}"
        _, (memory,) = await turn(engine, story, say=None, reply=reply)
        made[n] = memory.id
    before = await memory_rows(engine, story)
    assert before[made[9]].narrative_turn == 9

    await truncate(engine, story, 8)
    after = await memory_rows(engine, story)
    assert str(after[made[9]].membership) == "discarded"
    assert str(after[made[7]].membership) == "active"
    for column in ("text", "narrative_turn", "created_at", "source_event_ids", "importance"):
        assert getattr(after[made[9]], column) == getattr(before[made[9]], column), column

    await untruncate(engine, story)
    assert str((await memory_rows(engine, story))[made[9]].membership) == "active"
