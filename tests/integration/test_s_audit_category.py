"""docs/04 S-AUDIT-CATEGORY, M2 prerequisite slice (the guardrail-specific part lands in #68)."""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.core.views import audit_view, corrected_history
from tests.event_support import new_story, play_turn


@pytest.mark.part("M2", "FR-STORE-25")
async def test_s_audit_category__prereq(engine: AsyncEngine) -> None:
    """A rejected output recorded inside an active turn is absent from the corrected history at
    every checkpoint. Complement: the audit view returns it, in its turn's position."""
    story = await new_story(engine)
    turn = await play_turn(engine, story, "hello", rejected="not this")

    for through in (None, 1):
        view = await corrected_history(engine, story, through)
        assert "guardrail_output_rejected" not in {e.type for e in view}
        assert [e.type for e in view if e.operation_id == turn.operation_id] == [
            "player_input",
            "message_rendered",
        ]

    audit = [e for e in await audit_view(engine, story) if e.operation_id == turn.operation_id]
    assert [(e.type, e.category.value) for e in audit] == [
        ("player_input", "input"),
        ("guardrail_output_rejected", "audit"),
        ("message_rendered", "narrative_state"),
    ]
    assert {(e.narrative_turn, e.turn_variant) for e in audit} == {(1, 1)}
