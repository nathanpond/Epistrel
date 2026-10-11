"""docs/04 TIME, M2 slices: (b) truncation only flips membership and untruncation restores the
prior view exactly; (e) narrative position comes from turn operations only; (f) one active
variant per position, forming a single chain."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.core.views import audit_view, corrected_history
from tests.concurrency_support import assert_single_chain, chain, regenerate
from tests.event_support import interleave, narrative, new_story, play_turn, rows, story_row
from tests.projection_support import snapshot, swap, truncate, untruncate

TURNS = 3


async def _rows(engine: AsyncEngine, story: str, table: str) -> list[dict[str, object]]:
    found = await rows(engine, f"SELECT * FROM {table} WHERE story_id = :s", s=story)  # noqa: S608
    return sorted((dict(r._mapping) for r in found), key=lambda r: str(r.get("operation_id")))


async def _views(engine: AsyncEngine, story: str, head: int) -> list[list[str]]:
    """The corrected history, whole and through every turn, as event ids."""
    views = [await corrected_history(engine, story)]
    views += [await corrected_history(engine, story, n) for n in range(head + 1)]
    return [[e.id for e in view] for view in views]


@pytest.mark.part("M2", "FR-STORE-10,11")
async def test_time__b(engine: AsyncEngine) -> None:
    """For every checkpoint N of a story with background work between its turns: truncation
    only flips membership (every event row persists byte for byte, valid-time included; an
    operation changes only ``membership``/``membership_tx``), and untruncation restores every
    view through every turn and every projection row exactly. Complement: while truncated,
    the views after N differ from the originals."""
    story = await new_story(engine)
    for n in range(1, TURNS + 2):
        await interleave(engine, story, after_turn=n - 1)
        await play_turn(engine, story, f"turn {n}")
    head = TURNS + 1
    for checkpoint in range(head):
        events_before = await _rows(engine, story, "events")
        ops_before = {r["operation_id"]: r for r in await _rows(engine, story, "operations")}
        views_before = await _views(engine, story, head)
        projections_before = await snapshot(engine, story)

        await truncate(engine, story, checkpoint)
        events_after = await _rows(engine, story, "events")
        known = {e["id"] for e in events_before}
        assert [e for e in events_after if e["id"] in known] == events_before
        assert [e["type"] for e in events_after if e["id"] not in known] == ["truncate"]
        for row in await _rows(engine, story, "operations"):
            old = ops_before.get(str(row["operation_id"]))
            if old is not None:
                flipped = {**row, "membership": old["membership"]}
                assert {**flipped, "membership_tx": old["membership_tx"]} == old
        truncated = await _views(engine, story, head)
        assert truncated[1 : checkpoint + 2] == views_before[1 : checkpoint + 2]
        assert truncated[0] != views_before[0]

        assert await untruncate(engine, story) is not None
        assert await _views(engine, story, head) == views_before
        assert await snapshot(engine, story) == projections_before


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
