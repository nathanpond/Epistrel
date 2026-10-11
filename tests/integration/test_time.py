"""docs/04 TIME, M2 slices: (e) narrative position comes from turn operations only; (f) one
active variant per position, forming a single chain."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.core.views import audit_view, corrected_history
from tests.concurrency_support import assert_single_chain, chain, regenerate
from tests.event_support import interleave, narrative, new_story, play_turn, story_row
from tests.projection_support import swap, truncate, untruncate

TURNS = 3


@pytest.mark.part("M2", "FR-STORE-8,24,25")
async def test_time__e(engine: AsyncEngine) -> None:
    """Background, audit and control events between turns never move a record's narrative
    position or change the narrative contents of any view. Complement: those events are
    recorded (seq interleaves them) and the audit view shows them."""
    quiet = await new_story(engine)
    busy = await new_story(engine)
    for n in range(1, TURNS + 1):
        await interleave(engine, busy, after_turn=n - 1)
        quiet_turn = await play_turn(engine, quiet, f"turn {n}")
        busy_turn = await play_turn(engine, busy, f"turn {n}")
        assert (quiet_turn.narrative_turn, quiet_turn.turn_variant) == (n, 1)
        assert (busy_turn.narrative_turn, busy_turn.turn_variant) == (n, 1)
    await interleave(engine, busy, after_turn=TURNS)

    # seq interleaves the extra work: the same turn content sits at different seq values.
    quiet_all = await audit_view(engine, quiet)
    busy_all = await audit_view(engine, busy)
    assert len(busy_all) > len(quiet_all)
    quiet_seq = {(e.type, e.payload): e.seq for e in quiet_all if e.narrative_turn}
    busy_seq = {(e.type, e.payload): e.seq for e in busy_all if e.category.value == "input"}
    assert any(busy_seq[k] != quiet_seq[k] for k in busy_seq)

    # Positions and narrative contents agree for every view, whole history and through each N.
    assert narrative(quiet_all) == narrative(busy_all)
    for through in (None, *range(1, TURNS + 1)):
        quiet_view = await corrected_history(engine, quiet, through)
        busy_view = await corrected_history(engine, busy, through)
        assert narrative(quiet_view) == narrative(busy_view), through
        assert {e.category.value for e in busy_view}.isdisjoint({"audit"})
        assert all(e.type != "config_changed" for e in busy_view)

    # Only turns advanced the turn index; the extra work is in the audit view.
    assert (await story_row(engine, quiet))[2] == (await story_row(engine, busy))[2] == TURNS + 1
    extra = {e.type for e in busy_all} - {e.type for e in quiet_all}
    assert extra == {"config_changed", "guardrail_output_rejected", "lite_summary_segment"}


@pytest.mark.part("M2", "FR-STORE-28")
async def test_time__f(engine: AsyncEngine) -> None:
    """After every turn, regeneration, truncation, untruncation and swap, exactly one variant
    per position is active and the active variants form a single chain (turns 1..k, next
    index k+1). Complement: the inactive variants are still there, dormant or discarded."""
    story = await new_story(engine)
    steps: list[Callable[[], Awaitable[object]]] = [
        lambda: play_turn(engine, story, "one"),
        lambda: play_turn(engine, story, "two"),
        lambda: regenerate(engine, story),
        lambda: play_turn(engine, story, "three"),
        lambda: truncate(engine, story, 1),
        lambda: untruncate(engine, story),
        lambda: truncate(engine, story, 1),
        lambda: play_turn(engine, story, "two, played differently"),
        lambda: regenerate(engine, story),
        lambda: swap(engine, story),
        lambda: regenerate(engine, story),
    ]
    for step in steps:
        await step()
        await assert_single_chain(engine, story)

    assert [t for t, _ in await chain(engine, story)] == [1, 2, 3]
    async with engine.connect() as conn:
        found = await conn.execute(
            text(
                "SELECT narrative_turn, membership, count(*) FROM operations "
                "WHERE story_id = :s AND kind = 'turn' AND status = 'committed' "
                "GROUP BY 1, 2 ORDER BY 1, 2"
            ),
            {"s": story},
        )
        by_position = {(t, m): int(n) for t, m, n in found}
    assert all(by_position[(t, "active")] == 1 for t in (1, 2, 3))
    assert sum(n for (t, m), n in by_position.items() if m != "active") == 4
