"""docs/04 TIME, M2 slice (e): narrative position comes from turn operations only."""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.core.views import audit_view, corrected_history
from tests.event_support import interleave, narrative, new_story, play_turn, story_row

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
