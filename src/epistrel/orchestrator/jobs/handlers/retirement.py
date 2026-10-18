"""``hot_set_retirement`` and ``hot_counts_reconcile``: the hot set's background work (FR-MEM-8,
NFR-SCALE-2; docs/02 §4.2; #147). ``core.memory.retirement`` holds the rules (counters,
episodes, ranking, the deadline); this module schedules and commits.

**When it runs.** ``core.memory.retirement.EPISODE_HOOKS`` call ``on_episode`` in the
transaction that changed an episode:

- ``opened``: #143's priority consolidation (``consolidation.schedule_overflow``: whatever is
  uncovered, due ahead of the queue; nothing when the story's consolidation module is off) and
  the episode's retirement job;
- ``overflowing`` (a later commit added to the excess) and ``follow_up`` (a forced run left more
  to retire): the retirement job again. One job per episode is queued at a time
  (``coalesce_key`` ``hot_set_retirement:<episode>``);
- ``forced`` (the deadline retired uncovered records, inside the turn's commit): priority
  consolidation, to cover them later.

**What a run does.** Nothing is read or called outside the commit: its ``maintenance``
operation (no revision bump, no events) runs ``retirement.run_episode`` in a commit step, under
the story lock, against the timeline as it then stands: the episode's set is recounted, and
the run retires what is retirable now (covered or below the importance floor), or, once the
deadline has come, the lowest-ranked uncovered records too, at most 500 a run. The job's
result carries ids and counts only (invariant 3).

**The reconcile** (``hot_counts_reconcile``, daily, NFR-REL-5): its run has no story and
enqueues one job per story with memories or an open episode; each recounts the story's
counters (``retirement.reconcile``), counts a correction in
``epistrel_hot_counts_corrected_total``, and opens or closes episodes on what it found.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from epistrel.core.enums import OperationKind
from epistrel.core.events.operation import Operation
from epistrel.core.ids import ULID_PATTERN
from epistrel.core.memory import retirement
from epistrel.core.memory.retirement import Episode, EpisodeSignal
from epistrel.core.tables import stories
from epistrel.observability.metrics import HOT_COUNTS_CORRECTED
from epistrel.orchestrator.jobs import queue
from epistrel.orchestrator.jobs.handlers import consolidation
from epistrel.orchestrator.jobs.recurring import FAILED as TASK_FAILED
from epistrel.orchestrator.jobs.recurring import OK as TASK_OK
from epistrel.orchestrator.jobs.recurring import NoPayload, RecurringTask, record
from epistrel.orchestrator.jobs.types import JobContext, JobHandler, JobOutput

logger = logging.getLogger(__name__)

RETIREMENT = "hot_set_retirement"
RECONCILE = "hot_counts_reconcile"
RECONCILE_INTERVAL = timedelta(days=1)
MAX_ATTEMPTS = 3


class RetirementPayload(BaseModel):
    """The episode a retirement job works on."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    episode_id: str = Field(pattern=ULID_PATTERN)


def coalesce_key(episode_id: str) -> str:
    """One queued retirement job per episode."""
    return f"{RETIREMENT}:{episode_id}"


async def schedule(conn: AsyncConnection, story_id: str, episode_id: str) -> str:
    """Enqueue the episode's retirement job in ``conn``'s transaction (coalesced)."""
    return await queue.enqueue(
        conn,
        RETIREMENT,
        RetirementPayload(episode_id=episode_id),
        story_id=story_id,
        coalesce_key=coalesce_key(episode_id),
    )


async def on_episode(conn: AsyncConnection, ep: Episode, signal: EpisodeSignal) -> None:
    """``core.memory.retirement``'s episode hook (see the module docstring)."""
    if signal in ("opened", "forced"):
        await consolidation.schedule_overflow(conn, ep.story_id)
    if signal in ("opened", "overflowing", "follow_up"):
        await schedule(conn, ep.story_id, ep.id)


retirement.register_episode_hook(on_episode)


# -- the runs -----------------------------------------------------------------------------------


async def _run_retirement(ctx: JobContext) -> JobOutput:
    story_id = ctx.job.story_id
    payload = ctx.payload
    assert isinstance(payload, RetirementPayload)  # noqa: S101 - the kind's payload model
    if story_id is None:
        return JobOutput(result={"episode_id": payload.episode_id, "skipped": "no_story"})
    outcome: dict[str, Any] = {"episode_id": payload.episode_id}

    def stage(op: Operation) -> None:
        async def step(conn: AsyncConnection) -> None:
            run = await retirement.run_episode(conn, story_id, payload.episode_id)
            outcome.update(run.result())

        op.add_commit_step(step)

    return JobOutput(stage=stage, result_of=lambda: dict(outcome))


async def _fan_out(ctx: JobContext) -> JobOutput:
    """The recurring run: enqueue the reconcile of every story with memories."""
    async with ctx.db.connect() as conn:
        candidates = await retirement.stories_with_memories(conn)
    scheduled = 0
    for story_id in candidates:
        async with ctx.db.begin() as conn:
            exists = await conn.scalar(select(stories.c.id).where(stories.c.id == story_id))
            if exists is not None:
                await queue.enqueue(
                    conn,
                    RECONCILE,
                    NoPayload(),
                    story_id=story_id,
                    coalesce_key=f"{RECONCILE}:{story_id}",
                )
                scheduled += 1
    return JobOutput(result={"stories": len(candidates), "scheduled": scheduled})


async def _run_reconcile(ctx: JobContext) -> JobOutput:
    story_id = ctx.job.story_id
    if story_id is None:
        try:
            output = await _fan_out(ctx)
        except Exception as exc:
            await record(ctx, TASK_FAILED, type(exc).__name__)
            raise
        await record(ctx, TASK_OK)
        return output
    found: dict[str, Any] = {"corrected": False}

    def stage(op: Operation) -> None:
        async def step(conn: AsyncConnection) -> None:
            corrected = await retirement.reconcile(conn, story_id)
            found["corrected"] = corrected
            if corrected:
                HOT_COUNTS_CORRECTED.inc()

        op.add_commit_step(step)

    return JobOutput(stage=stage, result_of=lambda: dict(found))


RETIREMENT_HANDLER = JobHandler(
    kind=RETIREMENT,
    payload=RetirementPayload,
    run=_run_retirement,
    operation_kind=OperationKind.MAINTENANCE,
    max_attempts=MAX_ATTEMPTS,
)
RECONCILE_HANDLER = JobHandler(
    kind=RECONCILE,
    payload=NoPayload,
    run=_run_reconcile,
    operation_kind=OperationKind.MAINTENANCE,
    max_attempts=1,
)
RECONCILE_TASK = RecurringTask(RECONCILE, RECONCILE_INTERVAL)

__all__ = [
    "RECONCILE",
    "RECONCILE_HANDLER",
    "RECONCILE_INTERVAL",
    "RECONCILE_TASK",
    "RETIREMENT",
    "RETIREMENT_HANDLER",
    "RetirementPayload",
    "coalesce_key",
    "on_episode",
    "schedule",
]
