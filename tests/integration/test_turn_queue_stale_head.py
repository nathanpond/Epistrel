"""The drainer never takes the turn slot for a head item that is no longer waiting (#329;
FR-CONC-5, #63's queue).

The drainer reads its head, then waits for the slot. A control action (an erasure, a
truncation, a swap) can fail the queued turns meanwhile (``turn_gate.fail_queued``) and clear
the slot. ``acquire(newcomer=False)`` re-checks the item under the story row lock and raises
``NotQueued`` instead of taking the slot; the drainer reads the head again.

The race is scripted, not timed: the drainer's head read is wrapped so the control action
commits between that read and the drainer's ``acquire``, which is exactly the window the
issue describes. No sleep or wall-clock bound decides the outcome (#430, #432).
"""

from __future__ import annotations

import logging
from typing import Any

import pytest
from sqlalchemy import text, update
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.config import Settings
from epistrel.core import turn_gate
from epistrel.core.enums import OperationKind
from epistrel.core.errors import TimelineChanged
from epistrel.core.events import Operation
from epistrel.core.ids import new_ulid
from epistrel.core.tables import stories
from epistrel.orchestrator.queue_drainer import QueueDrainer
from tests.auth_support import auth_settings
from tests.event_support import rows, scalar
from tests.respond_support import new_story, respond_client

pytestmark = pytest.mark.integration

CONTROL = "op-control-329"


@pytest.fixture
def stub_settings(pg_url: str) -> Settings:
    return auth_settings(pg_url, model_adapter="stub")


async def _story(settings: Settings, engine: AsyncEngine) -> str:
    async with respond_client(settings, engine) as (client, _):
        story, _ids = await new_story(client)
    return story


async def _hold(engine: AsyncEngine, story: str) -> str:
    """A turn holding the story's slot (the one a queued turn waits behind)."""
    holder = Operation(engine, story_id=story, kind=OperationKind.TURN, principal="p-held")
    await holder.begin()
    assert await turn_gate.acquire(engine, story, holder.operation_id, lease_s=60) is None
    return holder.operation_id


async def _settle(engine: AsyncEngine, holder: str) -> None:
    """End the held turn as the erasure's revalidation would, so no ``in_flight`` row is left
    for a later test's orphan sweep."""
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE operations SET status = 'abandoned' WHERE operation_id = :o"),
            {"o": holder},
        )


async def _enqueue(engine: AsyncEngine, story: str) -> str:
    operation_id = new_ulid()
    await turn_gate.enqueue(
        engine,
        story,
        operation_id=operation_id,
        kind=turn_gate.RESPOND,
        request={},
        principal="p-queued",
        idempotency_key=None,
        viewpoint=None,
        origin="generated",
        queue_max=10,
    )
    return operation_id


async def _control_action(engine: AsyncEngine, story: str) -> list[str]:
    """What an erasure does to the queue in its own transaction: fail every queued turn
    (``timeline_changed``) and clear the slot's lease."""
    async with engine.begin() as conn:
        failed = await turn_gate.fail_queued(
            conn,
            story,
            TimelineChanged(
                "an erasure changed the timeline before the queued turn started",
                {"current_revision": 1, "control_operation_id": CONTROL},
            ),
        )
        await conn.execute(
            update(stories)
            .where(stories.c.id == story)
            .values(in_flight_turn=None, in_flight_lease_until=None)
        )
    turn_gate.wakeups.wake(story)
    return failed


async def _slot(engine: AsyncEngine, story: str) -> str | None:
    found: str | None = await scalar(
        engine, "SELECT in_flight_turn FROM stories WHERE id = :s", s=story
    )
    return found


async def _operation(engine: AsyncEngine, operation_id: str) -> Any:
    (row,) = await rows(
        engine,
        "SELECT status::text AS status, error_code, error FROM operations WHERE operation_id = :o",
        o=operation_id,
    )
    return row


async def _queue(engine: AsyncEngine, story: str) -> list[str]:
    found = await rows(
        engine, "SELECT operation_id FROM turn_queue WHERE story_id = :s ORDER BY position", s=story
    )
    return [r.operation_id for r in found]


async def test_drainer_skips_a_head_a_control_action_failed(
    stub_settings: Settings,
    engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The issue's interleaving: the drainer reads B as its head, the control action fails B
    and clears the slot, then the drainer tries to start B. B is not run, the slot is never
    taken for it, B keeps ``timeline_changed`` and nothing logs a failed turn; the drainer
    reads the head again and runs the turn queued after the control action (C)."""
    story = await _story(stub_settings, engine)
    holder = await _hold(engine, story)
    stale = await _enqueue(engine, story)

    real_head = turn_gate.head
    real_acquire = turn_gate.acquire
    late: list[str] = []
    slot_after_acquire: list[tuple[str, str | None]] = []

    async def scripted_head(db: AsyncEngine, story_id: str) -> turn_gate.QueueItem | None:
        item = await real_head(db, story_id)
        if item is not None and item.operation_id == stale:
            # The control action commits after the drainer read B, before it acquires.
            assert await _control_action(engine, story_id) == [stale]
            late.append(await _enqueue(engine, story_id))
        return item

    async def watched_acquire(db: AsyncEngine, story_id: str, operation_id: str, **kw: Any) -> Any:
        held = await real_acquire(db, story_id, operation_id, **kw)
        slot_after_acquire.append((operation_id, await _slot(engine, story_id)))
        return held

    ran: list[str] = []

    async def handler(item: turn_gate.QueueItem, concurrency: turn_gate.Concurrency) -> None:
        ran.append(item.operation_id)
        assert await _slot(engine, item.story_id) == item.operation_id
        if item.operation_id == stale:
            raise RuntimeError(f"operation {stale} is not queued")  # what Operation.begin says

    monkeypatch.setattr(turn_gate, "head", scripted_head)
    monkeypatch.setattr(turn_gate, "acquire", watched_acquire)
    drainer = QueueDrainer(lambda: engine, {turn_gate.RESPOND: handler})
    caplog.set_level(logging.INFO, logger="epistrel.orchestrator.queue_drainer")
    drainer.kick(story)
    await drainer.idle()

    assert ran == late  # B never ran; C, queued after the control action, did
    assert (stale, stale) not in slot_after_acquire  # the slot was never B's
    assert [r for r in caplog.records if "queued turn" in r.getMessage()] == []
    record = await _operation(engine, stale)
    assert (record.status, record.error_code) == ("failed", "timeline_changed")
    assert record.error["details"]["control_operation_id"] == CONTROL
    assert await _slot(engine, story) is None
    assert await _queue(engine, story) == []
    await _settle(engine, holder)


async def test_acquire_refuses_a_failed_head(stub_settings: Settings, engine: AsyncEngine) -> None:
    """``acquire(newcomer=False)`` after the control action: ``NotQueued``, the slot stays
    free. Complement: a head that is still queued takes the free slot."""
    story = await _story(stub_settings, engine)
    holder = await _hold(engine, story)
    stale = await _enqueue(engine, story)
    await _control_action(engine, story)

    with pytest.raises(turn_gate.NotQueued):
        await turn_gate.acquire(engine, story, stale, lease_s=60, newcomer=False)
    assert await _slot(engine, story) is None
    record = await _operation(engine, stale)
    assert (record.status, record.error_code) == ("failed", "timeline_changed")

    waiting = await _enqueue(engine, story)
    assert await turn_gate.acquire(engine, story, waiting, lease_s=60, newcomer=False) is None
    assert await _slot(engine, story) == waiting
    # Leave no queue behind: a later start-up recovery pass drains every story's queue.
    await turn_gate.close_item(engine, story, waiting)
    assert await _queue(engine, story) == []
    await _settle(engine, holder)


@pytest.mark.parametrize("status", ["failed", "in_flight"])
async def test_acquire_finishes_an_ended_head_left_in_the_queue(
    stub_settings: Settings, engine: AsyncEngine, status: str
) -> None:
    """A head whose operation is no longer ``queued`` although its queue row is still there
    (it ended, but nothing dropped the row): not started, the slot stays free, and it is
    finished as ``fail_item`` would (``turn_interrupted``, its row gone) so the queue moves on."""
    story = await _story(stub_settings, engine)
    stale = await _enqueue(engine, story)
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE operations SET status = CAST(:st AS operation_status) "
                "WHERE operation_id = :o"
            ),
            {"st": status, "o": stale},
        )

    with pytest.raises(turn_gate.NotQueued):
        await turn_gate.acquire(engine, story, stale, lease_s=60, newcomer=False)
    assert await _slot(engine, story) is None
    record = await _operation(engine, stale)
    assert (record.status, record.error_code) == ("failed", "turn_interrupted")
    assert await _queue(engine, story) == []


async def test_acquire_after_a_reclaim_that_failed_the_head(
    stub_settings: Settings, engine: AsyncEngine
) -> None:
    """The head's own run died ``MAX_RUNS`` times (its lease ran out): the reclaim fails it and
    drops it from the queue, so the drainer's ``acquire`` for it is ``NotQueued`` with the slot
    left free. Complement: before ``MAX_RUNS`` the reclaim requeues it and the slot is taken."""
    story = await _story(stub_settings, engine)
    for attempts, taken in ((turn_gate.MAX_RUNS - 2, True), (turn_gate.MAX_RUNS - 1, False)):
        head = await _enqueue(engine, story)
        async with engine.begin() as conn:
            await conn.execute(
                text("UPDATE operations SET status = 'in_flight' WHERE operation_id = :o"),
                {"o": head},
            )
            await conn.execute(
                text("UPDATE turn_queue SET attempts = :a WHERE operation_id = :o"),
                {"a": attempts, "o": head},
            )
            await conn.execute(
                text(
                    "UPDATE stories SET in_flight_turn = :o, "
                    "in_flight_lease_until = clock_timestamp() - interval '1 second' "
                    "WHERE id = :s"
                ),
                {"o": head, "s": story},
            )
        if taken:
            assert await turn_gate.acquire(engine, story, head, lease_s=60, newcomer=False) is None
            assert await _slot(engine, story) == head
            assert (await _operation(engine, head)).status == "queued"
            await turn_gate.close_item(engine, story, head)
            continue
        with pytest.raises(turn_gate.NotQueued):
            await turn_gate.acquire(engine, story, head, lease_s=60, newcomer=False)
        assert await _slot(engine, story) is None
        record = await _operation(engine, head)
        assert (record.status, record.error_code) == ("failed", "turn_interrupted")
        assert await _queue(engine, story) == []
