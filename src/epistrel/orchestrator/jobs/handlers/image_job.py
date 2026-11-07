"""``image_job`` and ``image_staging_sweep``: image generation as background jobs (FR-IMG-3,4;
#256).

``image_job`` runs one image job (``orchestrator.images.jobs.run_image_job``) on the runtime the
process's ``Engine.start_workers`` binds (``images.jobs.bind``): outside any turn, its own
transitions committed as it goes, so it records no operation. A retryable provider error or an
unavailable checker raises ``ImageJobRetry`` and #70 backs off; a process with no runtime bound
fails the attempt the same way. A worker process runs at most ``image_worker_concurrency`` of
them at once (``WorkerConfig.kind_caps``).

``image_staging_sweep`` (every 10 minutes, ``register_recurring``, #71) deletes the private
staging directories a crashed job left: older than ``staging_ttl`` and of a job that is terminal
or missing. Like the other sweeps it records its outcome on its ``recurring_tasks`` row, runs
once per due time and stores counts only.
"""

from __future__ import annotations

from datetime import timedelta

from pydantic import BaseModel, ConfigDict

from epistrel.core.config_changes import read_section
from epistrel.core.ids import Ulid
from epistrel.core.settings_schema import IMAGE_STAGING_SWEEP_TASK, ImageJobSettings
from epistrel.orchestrator.images import jobs as image_jobs
from epistrel.orchestrator.jobs.recurring import NoPayload, RecurringTask, recording
from epistrel.orchestrator.jobs.types import JobContext, JobHandler, JobOutput

IMAGE_JOB = image_jobs.IMAGE_JOB_KIND
IMAGE_STAGING_SWEEP = IMAGE_STAGING_SWEEP_TASK
SWEEP_INTERVAL = timedelta(minutes=10)


class ImageJobPayload(BaseModel):
    """The ``jobs`` payload: which image job to run."""

    model_config = ConfigDict(extra="forbid")

    image_job_id: Ulid


class NoImageRuntime(RuntimeError):
    """No ``Engine`` bound an image runtime in this process: the attempt fails and is retried."""


async def _run(ctx: JobContext) -> JobOutput:
    runtime = image_jobs.bound()
    if runtime is None:
        raise NoImageRuntime("no image runtime is bound in this process")
    assert isinstance(ctx.payload, ImageJobPayload)  # noqa: S101 - the handler's payload model
    result = await image_jobs.run_image_job(runtime, ctx.payload.image_job_id, ctx.job.failures)
    return JobOutput(result=result)


async def _sweep(ctx: JobContext) -> JobOutput:
    runtime = image_jobs.bound()
    if runtime is None:
        return JobOutput(result={"store": False})
    settings = await read_section(ctx.db, ImageJobSettings)
    return JobOutput(
        result=await image_jobs.sweep_staging(ctx.db, runtime.media, ttl_s=settings.staging_ttl)
    )


IMAGE_JOB_HANDLER = JobHandler(
    kind=IMAGE_JOB,
    payload=ImageJobPayload,
    run=_run,
    records_operation=False,
)
IMAGE_STAGING_SWEEP_HANDLER = JobHandler(
    kind=IMAGE_STAGING_SWEEP,
    payload=NoPayload,
    run=recording(_sweep),
    max_attempts=1,
    records_operation=False,
)
IMAGE_STAGING_SWEEP_TASK_DEFINITION = RecurringTask(IMAGE_STAGING_SWEEP, SWEEP_INTERVAL)

__all__ = [
    "IMAGE_JOB",
    "IMAGE_JOB_HANDLER",
    "IMAGE_STAGING_SWEEP",
    "IMAGE_STAGING_SWEEP_HANDLER",
    "IMAGE_STAGING_SWEEP_TASK_DEFINITION",
    "ImageJobPayload",
    "NoImageRuntime",
]
