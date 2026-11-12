"""A deleted story's ``memories`` partition is dropped in the background (#865; ADR-187).

- ``memory_partition_drop``: one story-less job per deleted story (payload ``{story_id}``,
  ``coalesce_key`` ``memory_partition_drop:<story>``), enqueued in the deletion's own
  transaction through ``core.memory.story_partitions.schedule_drop`` (this module binds the
  scheduler at import), so it runs once the deletion has committed. It runs
  ``story_partitions.drop_partition``: ``DETACH … CONCURRENTLY`` and ``DROP TABLE`` on an
  autocommit connection with ``lock_timeout`` 5 s. A lock timeout, or a story whose advisory
  lock another session holds, fails the attempt, and the worker retries it with backoff up to
  ``job_max_attempts``.
- ``memory_partition_sweep``: the hourly recurring safety net (``story_partitions
  .sweep_orphans``), also enqueued once at a worker's start (``schedule_sweep``, the recovery
  hook ``Engine.start_workers`` registers; the same coalesce key as the recurring task's job).
  Like the other sweeps it records its outcome on its ``recurring_tasks`` row and runs once per
  due time (``max_attempts = 1``).

Neither writes story state (no ``operations`` row); results are counts and outcomes only.
"""

from __future__ import annotations

from datetime import timedelta

from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncConnection

from epistrel.core.memory import story_partitions
from epistrel.orchestrator.jobs import queue
from epistrel.orchestrator.jobs.recurring import NoPayload, RecurringTask, recording
from epistrel.orchestrator.jobs.types import JobContext, JobHandler, JobOutput

MEMORY_PARTITION_DROP = "memory_partition_drop"
MEMORY_PARTITION_SWEEP = "memory_partition_sweep"
SWEEP_INTERVAL = timedelta(hours=1)
SWEEP_HOOK = "memory_partition_sweep"
"""The name of the recovery hook that enqueues the sweep at a worker's start."""


class DropPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    story_id: str


class PartitionBusy(RuntimeError):
    """Another session holds the story's advisory lock: the attempt is retried."""


def coalesce_key(story_id: str) -> str:
    return f"{MEMORY_PARTITION_DROP}:{story_id}"


async def schedule(conn: AsyncConnection, story_id: str) -> None:
    """``schedule_drop``'s scheduler: enqueue the drop in ``conn``'s transaction (coalesced)."""
    await queue.enqueue(
        conn,
        MEMORY_PARTITION_DROP,
        DropPayload(story_id=story_id),
        coalesce_key=coalesce_key(story_id),
    )


async def schedule_sweep(conn: AsyncConnection) -> None:
    """Enqueue the sweep now, coalesced with the recurring task's job (a recovery hook)."""
    await queue.enqueue(
        conn,
        MEMORY_PARTITION_SWEEP,
        NoPayload(),
        coalesce_key=f"recurring:{MEMORY_PARTITION_SWEEP}",
    )


async def _drop(ctx: JobContext) -> JobOutput:
    payload = ctx.payload
    assert isinstance(payload, DropPayload)  # noqa: S101 - validated against DropPayload
    outcome = await story_partitions.drop_partition(ctx.db, payload.story_id)
    if outcome == story_partitions.BUSY:
        raise PartitionBusy(MEMORY_PARTITION_DROP)
    return JobOutput(result={"outcome": outcome})


async def _sweep(ctx: JobContext) -> JobOutput:
    return JobOutput(result=await story_partitions.sweep_orphans(ctx.db))


MEMORY_PARTITION_DROP_HANDLER = JobHandler(
    kind=MEMORY_PARTITION_DROP,
    payload=DropPayload,
    run=_drop,
    records_operation=False,
)
MEMORY_PARTITION_SWEEP_HANDLER = JobHandler(
    kind=MEMORY_PARTITION_SWEEP,
    payload=NoPayload,
    run=recording(_sweep),
    max_attempts=1,
    records_operation=False,
)
MEMORY_PARTITION_SWEEP_TASK = RecurringTask(MEMORY_PARTITION_SWEEP, SWEEP_INTERVAL)

story_partitions.set_drop_scheduler(schedule)

__all__ = [
    "MEMORY_PARTITION_DROP",
    "MEMORY_PARTITION_DROP_HANDLER",
    "MEMORY_PARTITION_SWEEP",
    "MEMORY_PARTITION_SWEEP_HANDLER",
    "MEMORY_PARTITION_SWEEP_TASK",
    "SWEEP_HOOK",
    "SWEEP_INTERVAL",
    "DropPayload",
    "PartitionBusy",
    "coalesce_key",
    "schedule",
    "schedule_sweep",
]
