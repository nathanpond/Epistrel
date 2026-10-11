"""S-REWIND, M2 part: delete message 801 of a 1,000-message story and undo it (docs/04 §7;
#65). One turn per message: player-only turns written through ``Operation``."""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.core import timeline
from epistrel.core.enums import OperationKind, Provenance
from epistrel.core.events import Operation
from epistrel.core.views import corrected_history
from tests.concurrency_support import assert_single_chain, chain, regenerate
from tests.event_support import new_story, rows, scalar
from tests.projection_support import snapshot

MESSAGES = 1_000
CHECKPOINT = 800
PRINCIPAL = "p-player"


async def _message(engine: AsyncEngine, story: str, n: int) -> None:
    async with Operation(
        engine, story_id=story, kind=OperationKind.TURN, principal=PRINCIPAL
    ) as op:
        op.append("player_input", {"text": f"message {n}"}, provenance=Provenance.PLAYER)
        await op.commit()


async def _table(engine: AsyncEngine, story: str, table: str, key: str) -> dict[str, Any]:
    found = await rows(engine, f"SELECT * FROM {table} WHERE story_id = :s", s=story)  # noqa: S608
    return {r._mapping[key]: dict(r._mapping) for r in found}


@pytest.mark.part("M2", "FR-STORE-10..12,18")
async def test_s_rewind(engine: AsyncEngine) -> None:
    """Truncating to 800 marks every record positioned after 800 discarded — rows persist,
    valid-time unchanged — and the story continues from 800; untruncating restores 801-1000
    exactly (a row-level diff: only ``membership_tx`` moves); regenerating the latest message
    then keeps the old variant. Complement: records through 800 are never touched."""
    story = await new_story(engine)
    for n in range(1, MESSAGES + 1):
        await _message(engine, story, n)
    events_before = await _table(engine, story, "events", "id")
    ops_before = await _table(engine, story, "operations", "operation_id")
    view_before = [e.id for e in await corrected_history(engine, story)]
    projections_before = await snapshot(engine, story)

    cut = await timeline.truncate(
        engine,
        story,
        timeline.TruncateRequest(to_turn=CHECKPOINT),
        principal=PRINCIPAL,
    )
    assert cut.discarded_operations == MESSAGES - CHECKPOINT
    assert cut.next_turn_index == CHECKPOINT + 1
    stamp = await scalar(
        engine, "SELECT committed_at FROM operations WHERE operation_id = :o", o=cut.operation_id
    )
    events_after = await _table(engine, story, "events", "id")
    assert {k: v for k, v in events_after.items() if v["type"] != "truncate"} == events_before
    ops_after = await _table(engine, story, "operations", "operation_id")
    for op_id, old in ops_before.items():
        row = ops_after[op_id]
        if (old["narrative_turn"] or 0) > CHECKPOINT:
            assert (row["membership"], row["membership_tx"]) == ("discarded", stamp)
            assert {**row, "membership": "active", "membership_tx": old["membership_tx"]} == old
        else:
            assert row == old
    view = await corrected_history(engine, story)
    assert max(e.narrative_turn or 0 for e in view) == CHECKPOINT
    assert [e.id for e in view] == [
        e.id for e in await corrected_history(engine, story, CHECKPOINT)
    ]
    await assert_single_chain(engine, story)

    restored = await timeline.untruncate(engine, story, principal=PRINCIPAL)
    assert restored.restored_operations == MESSAGES - CHECKPOINT
    assert restored.next_turn_index == MESSAGES + 1
    restamp = await scalar(
        engine,
        "SELECT committed_at FROM operations WHERE operation_id = :o",
        o=restored.operation_id,
    )
    ops_restored = await _table(engine, story, "operations", "operation_id")
    for op_id, old in ops_before.items():
        row = ops_restored[op_id]
        assert {**row, "membership_tx": old["membership_tx"]} == old
        later = (old["narrative_turn"] or 0) > CHECKPOINT
        assert row["membership_tx"] == (restamp if later else old["membership_tx"])
    assert [e.id for e in await corrected_history(engine, story)] == view_before
    assert await snapshot(engine, story) == projections_before
    events_restored = await _table(engine, story, "events", "id")
    assert {k: v for k, v in events_restored.items() if k in events_before} == events_before

    await regenerate(engine, story)
    assert (await chain(engine, story))[-1] == (MESSAGES, 2)
    dormant = await scalar(
        engine,
        "SELECT membership FROM operations WHERE story_id = :s AND narrative_turn = :t "
        "AND turn_variant = 1",
        s=story,
        t=MESSAGES,
    )
    assert dormant == "dormant"
    assert [e.id for e in await corrected_history(engine, story, MESSAGES - 1)] == [
        e.id for e in await corrected_history(engine, story) if (e.narrative_turn or 0) < MESSAGES
    ]
