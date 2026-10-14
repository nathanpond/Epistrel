"""``offboarding``: carry out a principal's offboarding, one entity per transaction (FR-AUTH-7;
#117). ``core.offboarding`` holds the rules. This handler drives them.

The job's id is its ``offboarding_jobs`` row's. Each run works through the snapshot from
``progress.index``:

1. Record the entry as ``progress.current``, unless it already is: then a run before this one
   may have acted on it, so it is ``resumed``.
2. Apply it through its type's own path (``offboarding.apply``), as ``system``, with the job
   id on every ownership row (``ownership.acting_job``).
3. Record its outcome and move the index past it, in one transaction.

Every transaction the job writes is fenced by its claim (``queue.hold``). A run that lost its
claim writes nothing more, and the run that took the claim over resumes from the row.

A story that answers 423 ``story_busy`` is deferred: the pass moves on, and the deferred
stories are tried again at its end, up to ``BUSY_RETRIES`` more times, ``BUSY_DELAY_S`` apart.
After that one is skipped as ``busy``. The handler yields every ``BATCH`` entities
(``JobOutput.requeue_in``), so no run nears the worker's runtime cap. It records no operation:
each entity's change is recorded by the path that made it. ``max_attempts`` is 20. A run that
fails is retried with #70's backoff and resumes where it stopped, and the last failed attempt
marks the offboarding ``failed`` with its partial report.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncConnection

from epistrel.core import offboarding, ownership
from epistrel.core.errors import StoryBusy
from epistrel.core.tables.offboarding import OffboardingStatus
from epistrel.orchestrator.jobs import queue
from epistrel.orchestrator.jobs.types import ClaimedJob, JobContext, JobHandler, JobOutput

OFFBOARDING_KIND = "offboarding"
MAX_ATTEMPTS = 20
BATCH = 100
"""Entities per run: the handler yields (requeues) after this many."""
BUSY_RETRIES = 3
"""How many more times a busy story is tried after the pass, before it is skipped."""
BUSY_DELAY_S = 5.0
"""How long the job waits between tries of the busy stories."""


class OffboardingPayload(BaseModel):
    """The job carries nothing: its id is its ``offboarding_jobs`` row's."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class _Fence:
    def __init__(self, job: ClaimedJob) -> None:
        self.job = job

    async def __call__(self, conn: AsyncConnection) -> None:
        await queue.hold(conn, self.job)


async def _step(
    ctx: JobContext,
    job: offboarding.Job,
    entry: Mapping[str, Any],
    progress: dict[str, Any],
    guard: offboarding.Guard,
) -> tuple[list[offboarding.Outcome] | None, dict[str, Any]]:
    """Mark, apply and answer one entry: its outcomes (``None`` when its story was busy) and
    the progress with the marker set."""
    resumed = progress.get("current") == dict(entry)
    if not resumed:
        progress = {**progress, "current": dict(entry)}
        await offboarding.save_progress(ctx.db, job.id, progress, [], guard)
    try:
        outcomes = await offboarding.apply(ctx.db, job, entry, resumed=resumed)
    except StoryBusy:
        return None, progress
    return outcomes, progress


async def _work(ctx: JobContext, guard: offboarding.Guard) -> JobOutput:
    job = await offboarding.begin_run(ctx.db, ctx.job.id, guard)
    if job is None or job.status not in (
        OffboardingStatus.QUEUED.value,
        OffboardingStatus.RUNNING.value,
    ):
        return JobOutput(result={"status": job.status if job else "gone"})
    progress: dict[str, Any] = {"index": 0, "current": None, "deferred": [], **job.progress}
    ran = 0
    while int(progress["index"]) < len(job.snapshot):
        if ran >= BATCH:
            return JobOutput(requeue_in=0.0, result={"done": progress["index"]})
        entry = job.snapshot[int(progress["index"])]
        outcomes, progress = await _step(ctx, job, entry, progress, guard)
        moved: dict[str, Any] = {**progress, "index": int(progress["index"]) + 1, "current": None}
        if outcomes is None:
            moved["deferred"] = [*progress["deferred"], {"entry": dict(entry), "retries": 0}]
            outcomes = []
        await offboarding.save_progress(ctx.db, job.id, moved, outcomes, guard)
        progress = moved
        ran += 1
    if progress["deferred"]:
        waiting: list[dict[str, Any]] = []
        for item in list(progress["deferred"]):
            entry = item["entry"]
            outcomes, progress = await _step(ctx, job, entry, progress, guard)
            rest = [other for other in progress["deferred"] if other["entry"] != entry]
            if outcomes is None:
                retries = int(item["retries"]) + 1
                if retries < BUSY_RETRIES:
                    waiting.append({"entry": entry, "retries": retries})
                    rest.append({"entry": entry, "retries": retries})
                    outcomes = []
                else:
                    skipped = {"id": entry["id"], "reason": "busy"}
                    outcomes = [("skipped", str(entry["type"]), skipped)]
            progress = {**progress, "deferred": rest, "current": None}
            await offboarding.save_progress(ctx.db, job.id, progress, outcomes, guard)
        if waiting:
            return JobOutput(requeue_in=BUSY_DELAY_S, result={"deferred": len(waiting)})
    await offboarding.finish(ctx.db, dataclasses.replace(job, progress=progress), guard)
    return JobOutput(result={"status": OffboardingStatus.DONE.value})


async def _run(ctx: JobContext) -> JobOutput:
    guard = _Fence(ctx.job)
    try:
        with ownership.acting_job(ctx.job.id):
            return await _work(ctx, guard)
    except queue.Fenced:
        # Another run holds the job now and resumes from the row; this run's finish is refused.
        return JobOutput()
    except Exception as exc:
        if ctx.job.failures + 1 >= MAX_ATTEMPTS:
            await offboarding.fail(ctx.db, ctx.job.id, exc)
        raise


OFFBOARDING_HANDLER = JobHandler(
    kind=OFFBOARDING_KIND,
    payload=OffboardingPayload,
    run=_run,
    max_attempts=MAX_ATTEMPTS,
    records_operation=False,
)

__all__ = [
    "BATCH",
    "BUSY_DELAY_S",
    "BUSY_RETRIES",
    "MAX_ATTEMPTS",
    "OFFBOARDING_HANDLER",
    "OFFBOARDING_KIND",
    "OffboardingPayload",
]
