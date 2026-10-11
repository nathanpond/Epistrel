"""The ``lite_summary`` job's edges (#72): the 10-minute recurring run fans out to the stories
with work, a run with no Model Access Layer bound fails (class name only), a second run that
would write segments another run already wrote is dropped and finds nothing left to do, and a
story-less output cannot carry positioned operations."""

from __future__ import annotations

import asyncio
import json
import time

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.core import summary_port
from epistrel.orchestrator.jobs import queue
from epistrel.orchestrator.jobs.handlers import lite_summary
from epistrel.orchestrator.jobs.recurring import registered_tasks
from epistrel.orchestrator.jobs.types import ClaimedJob, JobOutput, PositionedOutput
from epistrel.orchestrator.models.access import ModelAccessLayer
from tests.auth_support import auth_settings
from tests.event_support import execute_in_tx, new_story, play_turn, rows
from tests.summary_support import claim_summary, run_due_jobs, segment_rows, summary_jobs, worker

SETTINGS = {"lite_window_messages": 2, "lite_summary_chunk": 2}


@pytest.fixture
def models(pg_url: str, engine: AsyncEngine) -> ModelAccessLayer:
    layer = ModelAccessLayer.from_settings(auth_settings(pg_url, model_adapter="stub"))
    layer.bind(engine)
    return layer


async def _story(engine: AsyncEngine, turns: int, *, scheduled: bool = True) -> str:
    story = await new_story(engine)
    await execute_in_tx(
        engine,
        (
            "UPDATE stories SET settings = settings || CAST(:p AS jsonb) WHERE id = :s",
            {"s": story, "p": json.dumps(SETTINGS)},
        ),
    )
    if not scheduled:
        summary_port.set_scheduler(None)
    try:
        for n in range(1, turns + 1):
            await play_turn(engine, story, f"line {n}")
    finally:
        summary_port.set_scheduler(lite_summary.schedule)
    return story


async def test_recurring_run_enqueues_each_story_with_work(
    engine: AsyncEngine, models: ModelAccessLayer
) -> None:
    assert registered_tasks()["lite_summary"].interval == lite_summary.INTERVAL
    busy = await _story(engine, 3, scheduled=False)
    idle = await _story(engine, 1, scheduled=False)
    assert await summary_jobs(engine, busy) == []
    async with engine.begin() as conn:
        await queue.enqueue(conn, "lite_summary", {})
    found = await rows(
        engine,
        "SELECT id FROM jobs WHERE kind = 'lite_summary' AND story_id IS NULL "
        "AND status = 'queued'",
    )
    assert found
    for row in found:
        claimed = await _claim(engine, row.id)
        assert await worker(engine).execute(claimed) is queue.CommitOutcome.COMMITTED
    assert [j.status for j in await summary_jobs(engine, busy)] == ["queued"]
    assert await summary_jobs(engine, idle) == []
    status = await rows(
        engine, "SELECT last_status FROM recurring_tasks WHERE name = 'lite_summary'"
    )
    assert [r.last_status for r in status] in ([], ["ok"])
    await run_due_jobs(engine, models, busy)
    assert [r.covers_to for r in await segment_rows(engine, busy)] == [1, 2]


async def test_a_run_without_models_fails_with_the_class_name(engine: AsyncEngine) -> None:
    story = await _story(engine, 3)
    lite_summary.bind(None)
    job = await claim_summary(engine, story)
    assert job is not None
    assert await worker(engine).execute(job) == "failed"  # one attempt: the next trigger retries
    (row,) = await summary_jobs(engine, story)
    assert row.result == {"error": "NoModels"}


async def test_a_second_run_over_written_segments_is_dropped(
    engine: AsyncEngine, models: ModelAccessLayer
) -> None:
    story = await _story(engine, 3)
    first = await claim_summary(engine, story)
    assert first is not None
    await lite_summary.schedule(engine, story)  # a second job while the first one runs
    second = await claim_summary(engine, story)
    assert second is not None
    lite_summary.bind(models)
    stub = models.stub
    latch = stub.hold("lite_summary")
    runs = [asyncio.create_task(worker(engine).execute(job)) for job in (first, second)]
    deadline = time.monotonic() + 10
    while sum(c.purpose == "lite_summary" for c in stub.calls) < 2:
        assert time.monotonic() < deadline
        await asyncio.sleep(0.02)
    latch.release()
    outcomes = sorted(str(o) for o in await asyncio.gather(*runs))
    assert outcomes == ["committed", "dropped"]  # both made the same segments; one is kept
    assert [r.covers_to for r in await segment_rows(engine, story)] == [1, 2]
    assert await run_due_jobs(engine, models, story) == [queue.CommitOutcome.COMMITTED]
    # Either run may win the commit race; the winner wrote both segments, the loser's rerun none.
    assert sorted(j.result["segments"] for j in await summary_jobs(engine, story)) == [0, 2]
    assert [r.covers_to for r in await segment_rows(engine, story)] == [1, 2]


async def test_story_less_output_cannot_position_operations(engine: AsyncEngine) -> None:
    job = ClaimedJob("J" * 26, "lite_summary", None, {}, 1, 0, None, None, None)  # type: ignore[arg-type]
    output = JobOutput(positioned=(PositionedOutput(1, lambda op: None, "O" * 26),))
    with pytest.raises(ValueError, match="no story"):
        await queue.commit_job(engine, job, lite_summary.LITE_SUMMARY_HANDLER, output, {})


async def _claim(engine: AsyncEngine, job_id: str) -> ClaimedJob:
    await execute_in_tx(
        engine,
        (
            "UPDATE jobs SET status = 'running', attempt = attempt + 1, "
            "lease_until = now() + interval '1 minute' WHERE id = :j",
            {"j": job_id},
        ),
    )
    (row,) = await rows(engine, "SELECT * FROM jobs WHERE id = :j", j=job_id)
    return ClaimedJob(
        row.id,
        row.kind,
        row.story_id,
        dict(row.payload),
        int(row.attempt),
        0,
        row.due_at,
        row.lease_until,
        None,
    )
