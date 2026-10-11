"""``Operation`` behaviour beyond I-01: positioning, lifecycle, connections, concurrency."""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.core.enums import OperationKind, OperationStatus
from epistrel.core.errors import InvalidRequest, PrincipalRequired
from epistrel.core.events import (
    InvalidEventPayload,
    Operation,
    StoryNotFound,
    UnregisteredEventType,
)
from epistrel.core.ids import new_ulid
from epistrel.core.stories import create_story
from epistrel.core.views import audit_view, corrected_history
from tests.event_support import (
    execute_in_tx,
    is_story_setup,
    new_story,
    play_turn,
    record,
    rows,
    scalar,
    sqlstate,
    story_row,
)


async def _status(engine: AsyncEngine, op: Operation) -> str:
    status: str = await scalar(
        engine, "SELECT status FROM operations WHERE operation_id = :o", o=op.operation_id
    )
    return status


async def test_unregistered_event_type_raises_before_any_row(engine: AsyncEngine) -> None:
    story = await new_story(engine)
    before = await scalar(engine, "SELECT count(*) FROM events")
    async with Operation(engine, story_id=story, kind=OperationKind.TURN, principal="p") as op:
        with pytest.raises(UnregisteredEventType) as caught:
            op.append("no_such_type", {"text": "x"})
        assert caught.value.code == "unsupported_event_kind"
        with pytest.raises(InvalidEventPayload) as invalid:
            op.append("player_input", {"words": "the payload is not a PlayerInput"})
        assert invalid.value.details["fields"] == ["text", "words"]
        assert "the payload" not in str(invalid.value.details)
        assert op.staged == ()
    assert await scalar(engine, "SELECT count(*) FROM events") == before
    assert await _status(engine, op) == "abandoned"


async def test_story_starts_with_one_setup_operation(engine: AsyncEngine) -> None:
    story = await new_story(engine)
    assert await story_row(engine, story) == (1, 0, 1, "active")
    view = await corrected_history(engine, story, through_turn=1)
    assert [e.type for e in view] == ["story_created"]
    assert all(is_story_setup(e) and e.narrative_turn is None for e in view)


async def test_regenerate_adds_a_variant_without_advancing(engine: AsyncEngine) -> None:
    story = await new_story(engine)
    await play_turn(engine, story, "one")
    await play_turn(engine, story, "two")
    again = await play_turn(engine, story, "two, again", turn=2)
    assert (again.narrative_turn, again.turn_variant) == (2, 2)
    timeline_revision, _, next_turn, _ = await story_row(engine, story)
    assert (timeline_revision, next_turn) == (4, 3)


async def test_non_turn_operations_never_allocate_a_turn(engine: AsyncEngine) -> None:
    story = await new_story(engine)
    await play_turn(engine, story, "one")
    before = await story_row(engine, story)
    positioned = await record(
        engine,
        story,
        OperationKind.CONSOLIDATION,
        "lite_summary_segment",
        {"text": "s", "covers_from": 1, "covers_to": 1},
        turn=1,
    )
    unpositioned = await record(
        engine, story, OperationKind.CONTROL, "config_changed", {"setting": "x"}
    )
    assert (positioned.narrative_turn, positioned.turn_variant) == (1, 1)
    assert (unpositioned.narrative_turn, unpositioned.turn_variant) == (None, None)
    assert await story_row(engine, story) == before  # neither revision nor turn index moved

    with pytest.raises(InvalidRequest, match="has not happened yet"):
        await record(
            engine, story, OperationKind.MAINTENANCE, "config_changed", {"setting": "x"}, turn=2
        )
    with pytest.raises(InvalidRequest, match="has not happened yet"):
        await play_turn(engine, story, "skip ahead", turn=5)


async def test_positioned_work_needs_an_active_variant(engine: AsyncEngine) -> None:
    story = await new_story(engine)
    turn = await play_turn(engine, story, "one")
    await execute_in_tx(
        engine,
        (
            "UPDATE operations SET membership = 'discarded' WHERE operation_id = :o",
            {"o": turn.operation_id},
        ),
    )
    with pytest.raises(InvalidRequest, match="no active variant"):
        await record(
            engine, story, OperationKind.MAINTENANCE, "config_changed", {"setting": "x"}, turn=1
        )
    # Discarded operations leave the corrected history but stay in the audit view.
    assert [e.type for e in await corrected_history(engine, story)] == ["story_created"]
    assert len(await audit_view(engine, story, through_turn=1)) == 3


async def test_lifecycle_rules(engine: AsyncEngine) -> None:
    story = await new_story(engine)
    with pytest.raises(PrincipalRequired):
        Operation(engine, story_id=story, kind=OperationKind.TURN, principal=" ")
    with pytest.raises(InvalidRequest):
        Operation(engine, story_id=None, kind=OperationKind.TURN, principal="p")
    with pytest.raises(InvalidRequest):
        Operation(engine, story_id=story, kind=OperationKind.TURN, principal="p", turn=0)

    op = Operation(engine, story_id=story, kind=OperationKind.CONTROL, principal="p")
    with pytest.raises(RuntimeError, match="not begun"):
        op.append("config_changed", {"setting": "x"})
    await op.begin()
    with pytest.raises(RuntimeError, match="already begun"):
        await op.begin()
    await op.abandon()
    assert op.status is OperationStatus.ABANDONED
    assert await _status(engine, op) == "abandoned"
    with pytest.raises(RuntimeError, match="abandoned"):
        await op.commit()

    done = await play_turn(engine, story, "one")
    assert done.status is OperationStatus.COMMITTED
    with pytest.raises(RuntimeError, match="committed"):
        done.append("player_input", {"text": "late"})


async def test_missing_story_fails_the_operation(engine: AsyncEngine) -> None:
    ghost = new_ulid()
    with pytest.raises(DBAPIError):  # the operations row needs its story
        await Operation(engine, story_id=ghost, kind=OperationKind.TURN, principal="p").begin()

    story = await new_story(engine)
    op = Operation(engine, story_id=story, kind=OperationKind.CONTROL, principal="p")
    await op.begin()
    op.story_id = ghost  # the story row vanishes between begin and commit
    with pytest.raises(StoryNotFound):
        await op.commit()
    assert op.status is OperationStatus.FAILED
    assert await _status(engine, op) == "failed"


async def test_caller_supplied_connection(engine: AsyncEngine) -> None:
    async with engine.connect() as conn:
        story = await create_story(conn, principal="p")
        async with Operation(conn, story_id=story, kind=OperationKind.TURN, principal="p") as op:
            op.append("player_input", {"text": "hi"})
            await op.commit()
        assert op.seqs == (2,)
        assert len(await corrected_history(conn, story)) == 2  # autobegins a transaction
        await conn.rollback()

        await conn.begin()
        with pytest.raises(RuntimeError, match="no open transaction"):
            await Operation(conn, story_id=story, kind=OperationKind.TURN, principal="p").begin()
        await conn.rollback()


async def test_story_less_operations_have_their_own_sequence(engine: AsyncEngine) -> None:
    first = await record(engine, None, OperationKind.CONTROL, "config_changed", {"setting": "a"})
    second = await record(engine, None, OperationKind.CONTROL, "config_changed", {"setting": "b"})
    assert second.seqs[0] == first.seqs[0] + 1
    found = await rows(
        engine,
        "SELECT story_id FROM operations WHERE operation_id IN (:a, :b)",
        a=first.operation_id,
        b=second.operation_id,
    )
    assert [r.story_id for r in found] == [None, None]


async def test_concurrent_commits_get_dense_unique_seq(engine: AsyncEngine) -> None:
    story = await new_story(engine)
    ops = await asyncio.gather(*(play_turn(engine, story, f"t{i}") for i in range(6)))
    assert sorted(op.narrative_turn or 0 for op in ops) == [1, 2, 3, 4, 5, 6]
    seqs = await rows(engine, "SELECT seq FROM events WHERE story_id = :s ORDER BY seq", s=story)
    assert [r.seq for r in seqs] == list(range(1, 14))
    assert (await story_row(engine, story))[:3] == (7, 0, 7)


async def test_operations_guard_allows_only_listed_columns(engine: AsyncEngine) -> None:
    story = await new_story(engine)
    op = await play_turn(engine, story, "one")
    await execute_in_tx(
        engine,
        (
            "UPDATE operations SET attempts = attempts || '[{}]'::jsonb, viewpoint = 'v' "
            "WHERE operation_id = :o",
            {"o": op.operation_id},
        ),
    )
    for column, value in (("principal", "'mallory'"), ("kind", "'control'")):
        with pytest.raises(DBAPIError) as caught:
            await execute_in_tx(
                engine,
                (
                    f"UPDATE operations SET {column} = {value} WHERE operation_id = :o",  # noqa: S608
                    {"o": op.operation_id},
                ),
            )
        assert sqlstate(caught.value) == "EPI02", column
