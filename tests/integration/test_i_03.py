"""docs/04 I-03, M2 part: state as of checkpoint N is the corrected history through narrative
position N, not the records with ``seq`` ≤ N."""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.core.enums import OperationKind
from epistrel.core.views import Event, corrected_history
from tests.event_support import new_story, play_turn, record
from tests.projection_support import truncate

TURNS = 4
CHECKPOINT = 2


def _segments(view: list[Event]) -> list[str]:
    return [e.payload.model_dump()["text"] for e in view if e.type == "lite_summary_segment"]


def _texts(view: list[Event]) -> list[str]:
    return [str(e.payload.model_dump()["text"]) for e in view if e.narrative_turn]


async def _segment(engine: AsyncEngine, story: str, covers_to: int, text: str) -> None:
    """A Lite summary segment: a ``consolidation`` operation positioned at its ``covers_to``."""
    await record(
        engine,
        story,
        OperationKind.CONSOLIDATION,
        "lite_summary_segment",
        {"text": text, "through_turn": covers_to},
        turn=covers_to,
    )


@pytest.mark.part("M2", "FR-STORE-6")
async def test_i_03(engine: AsyncEngine) -> None:
    """A segment recorded after the last turn but positioned at the checkpoint is in the state
    as of the checkpoint; one positioned later is not; truncated records leave it, and a turn
    played over the truncation joins it only at its own position. Complement: cutting the log
    at the checkpoint's last ``seq`` gives a different answer (it misses the late segment)."""
    story = await new_story(engine)
    for n in range(1, TURNS + 1):
        await play_turn(engine, story, f"turn {n}")
    await _segment(engine, story, CHECKPOINT, "early, recorded late")
    await _segment(engine, story, TURNS, "late")

    as_of = await corrected_history(engine, story, CHECKPOINT)
    assert _segments(as_of) == ["early, recorded late"]
    assert {e.narrative_turn for e in as_of if e.narrative_turn} == {1, 2}
    late = next(e for e in as_of if e.type == "lite_summary_segment")
    cut = max(e.seq for e in as_of if e.type == "message_rendered")
    assert late.seq > cut  # recorded after turns 3 and 4, positioned before them

    # Complement: the seq cut is not the state as of the checkpoint.
    everything = await corrected_history(engine, story)
    by_seq = [e for e in everything if e.seq <= cut]
    assert [e.id for e in by_seq] != [e.id for e in as_of]
    assert _segments(by_seq) == []
    assert _segments(everything) == ["early, recorded late", "late"]

    # Discarded records are excluded; the state as of the checkpoint is untouched.
    await truncate(engine, story, CHECKPOINT + 1)
    after = await corrected_history(engine, story)
    assert _segments(after) == ["early, recorded late"]
    assert "turn 4" not in _texts(after)
    assert [e.id for e in await corrected_history(engine, story, CHECKPOINT)] == [
        e.id for e in as_of
    ]

    # A turn played over the truncation takes position 4, the latest seq of all.
    await play_turn(engine, story, "turn 4, again")
    again = await corrected_history(engine, story, TURNS)
    assert "turn 4, again" in _texts(again)
    assert "turn 4" not in _texts(again)
    assert _segments(again) == ["early, recorded late"]
    assert [e.id for e in await corrected_history(engine, story, CHECKPOINT)] == [
        e.id for e in as_of
    ]
