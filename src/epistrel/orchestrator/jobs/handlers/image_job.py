"""``image_job`` and ``image_staging_sweep``: image generation as background jobs (FR-IMG-3,4;
#256).

``image_job`` runs one image job (``orchestrator.images.jobs.run_image_job``) on the runtime the
process's ``Engine.start_workers`` binds (``images.jobs.bind``): outside any turn, its own
transitions committed as it goes, so it records no operation. A retryable provider error or an
unavailable checker raises ``ImageJobRetry`` and #70 backs off; a process with no runtime bound
fails the attempt the same way. A story job waiting on its paused story (#264) goes back to
``queued``, due again in its recheck interval (``JobOutput.requeue_in``), which is no failure.
A worker process runs at most ``image_worker_concurrency`` of them at once
(``WorkerConfig.kind_caps``).

#267: the image job's lease-expiry hook (``attempts.on_lease_expired``) fences a stalled
attempt and parks the row until its horizon. ``image_attempt_horizon`` is the timer kind that
releases a fenced attempt's record at its horizon (target ``<job_id>:<attempt_token>``), and
``image_status_poll`` the timer kind that runs a confirming adapter's next ``status`` poll
(target ``job_id``): it runs the image job like ``image_job`` does, with the same hook.

``image_staging_sweep`` (every 10 minutes, ``register_recurring``, #71) deletes the private
staging directories a crashed job left: older than ``staging_ttl`` and of a job that is terminal
or missing. Like the other sweeps it records its outcome on its ``recurring_tasks`` row, runs
once per due time and stores counts only.
"""

from __future__ import annotations

from datetime import timedelta
from typing import cast

from pydantic import BaseModel, ConfigDict, Field

from epistrel.core.config_changes import read_section
from epistrel.core.ids import ULID_PATTERN, Ulid
from epistrel.core.settings_schema import IMAGE_STAGING_SWEEP_TASK, ImageJobSettings
from epistrel.orchestrator.images import attempts
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


UUID_PATTERN = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"


class HorizonPayload(BaseModel):
    """An ``image_attempt_horizon`` timer's job: ``<job_id>:<attempt_token>``."""

    model_config = ConfigDict(extra="forbid")

    target: str = Field(pattern=f"{ULID_PATTERN.removesuffix('$')}:{UUID_PATTERN}$")


class StatusPollPayload(BaseModel):
    """An ``image_status_poll`` timer's job: the image job's id."""

    model_config = ConfigDict(extra="forbid")

    target: Ulid


class NoImageRuntime(RuntimeError):
    """No ``Engine`` bound an image runtime in this process: the attempt fails and is retried."""


async def _run(ctx: JobContext) -> JobOutput:
    runtime = image_jobs.bound()
    if runtime is None:
        raise NoImageRuntime("no image runtime is bound in this process")
    assert isinstance(ctx.payload, ImageJobPayload)  # noqa: S101 - the handler's payload model
    result = await image_jobs.run_image_job(runtime, ctx.payload.image_job_id, ctx.job.failures)
    # #264: a story job waiting on its paused story is due again in ``requeue_in`` seconds.
    requeue_in = result.pop(image_jobs.REQUEUE_IN, None)
    return JobOutput(result=result, requeue_in=None if requeue_in is None else float(requeue_in))


async def _poll(ctx: JobContext) -> JobOutput:
    runtime = image_jobs.bound()
    if runtime is None:
        raise NoImageRuntime("no image runtime is bound in this process")
    payload = cast(StatusPollPayload, ctx.payload)
    result = await image_jobs.run_image_job(runtime, payload.target, ctx.job.failures)
    requeue_in = result.pop(image_jobs.REQUEUE_IN, None)
    return JobOutput(result=result, requeue_in=None if requeue_in is None else float(requeue_in))


async def _horizon(ctx: JobContext) -> JobOutput:
    payload = cast(HorizonPayload, ctx.payload)
    return JobOutput(result={"outcome": await attempts.horizon_reached(ctx.db, payload.target)})


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
    on_lease_expired=attempts.on_lease_expired,
)
IMAGE_STATUS_POLL_HANDLER = JobHandler(
    kind=attempts.STATUS_POLL,
    payload=StatusPollPayload,
    run=_poll,
    records_operation=False,
    on_lease_expired=attempts.on_lease_expired,
)
IMAGE_ATTEMPT_HORIZON_HANDLER = JobHandler(
    kind=attempts.HORIZON_TIMER,
    payload=HorizonPayload,
    run=_horizon,
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
    "IMAGE_ATTEMPT_HORIZON_HANDLER",
    "IMAGE_JOB",
    "IMAGE_JOB_HANDLER",
    "IMAGE_STAGING_SWEEP",
    "IMAGE_STAGING_SWEEP_HANDLER",
    "IMAGE_STAGING_SWEEP_TASK_DEFINITION",
    "IMAGE_STATUS_POLL_HANDLER",
    "HorizonPayload",
    "ImageJobPayload",
    "NoImageRuntime",
    "StatusPollPayload",
]
