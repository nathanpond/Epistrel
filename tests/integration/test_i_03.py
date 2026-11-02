"""docs/04 I-03, M2 part: state as of checkpoint N is the corrected history through narrative
position N, not the records with ``seq`` ≤ N."""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.core.enums import OperationKind
from epistrel.core.views import Event, corrected_history
from tests.event_support import new_story, play_turn, record, segment_payload
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
        segment_payload(text, covers_to),
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


async def _at(engine: AsyncEngine, story: str, who: str, through: tuple[int, int]) -> list[str]:
    from epistrel.core.canon import facts as canon
    from tests.canon_support import LOCATED_AT

    async with engine.connect() as conn:
        found = await canon.canon(
            conn, story, subject=who, predicate=LOCATED_AT, through_position=through
        )
    return [str(f.proposition.object_id) for f in found]


@pytest.mark.part("M7", "FR-STORE-6,20")
async def test_i_03__m7(engine: AsyncEngine) -> None:
    """#221: an amendment recorded after turn 1,000, positioned nowhere, that corrects pre-800
    history (Bobby was at the garden from the start) is in the state as of 800; in the same
    amendment, a part whose valid-time begins after 800's story-time (Ann in the garden from
    turn 900's story-minute) is not, and the version it superseded is read there instead, while
    at 1,000 it is in. Eligibility is per part, never per amendment."""
    from epistrel.core import world_clock
    from epistrel.core.amendments.store import PropositionPartDraft
    from tests import amendment_support as am
    from tests import canon_support as c

    w = await c.world(engine)
    bobby, ann = w.people["Bobby"], w.people["Ann"]
    school, garden = w.places["school"], w.places["garden"]
    await c.commit(engine, w.story_id, c.at(bobby, school, None, None))
    await c.commit(engine, w.story_id, c.at(ann, school, None, None))
    await am.play(engine, w.story_id, 998, start=3)
    async with engine.connect() as conn:
        later = await world_clock.story_time_at(conn, w.story_id, (900, 0))
        checkpoint = await world_clock.story_time_at(conn, w.story_id, (800, 0))
    assert later > checkpoint
    await am.amend(
        engine,
        w.story_id,
        [
            PropositionPartDraft(c.at(bobby, garden, None, None)),
            PropositionPartDraft(c.at(ann, garden, later, None), valid_from_minutes=later),
        ],
    )
    assert await _at(engine, w.story_id, bobby, (800, 0)) == [garden]
    assert await _at(engine, w.story_id, ann, (800, 0)) == [school]
    assert garden in await _at(engine, w.story_id, ann, (1000, 0))
