"""``image_candidate_expiry``: unattached catalog image candidates expire (FR-IMG-7, FR-MEDIA-2,
NFR-REL-5; #260).

A recurring task every hour (``register_recurring``, #71; the recovery pass creates its row). It
deletes the candidates past ``expires_at`` in batches of 500 (``core.images.candidates
.expire_batch``: oldest first, ``FOR UPDATE SKIP LOCKED``, so an attach holding a row wins and
the row waits for the next run), each batch in its own transaction, until none remain: the
candidate row and its ``candidate`` asset row, then, once the batch has committed, their bytes
unless another asset row has the same checksum (``core.media.sweep.collect_blob``). A process
with no blob store bound (``media_sweep.bind``) leaves the bytes to the media sweep's orphan step.
Work that came due while no worker ran is done at the next start, once. Like the other sweeps
it records its outcome on its ``recurring_tasks`` row, runs once per due time and stores counts
only.
"""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.core.images import candidates
from epistrel.core.media.store import BlobStore
from epistrel.core.media.sweep import collect_blob
from epistrel.core.settings_schema import IMAGE_CANDIDATE_EXPIRY_TASK
from epistrel.orchestrator.jobs.handlers import media_sweep
from epistrel.orchestrator.jobs.recurring import NoPayload, RecurringTask, recording
from epistrel.orchestrator.jobs.types import JobContext, JobHandler, JobOutput

IMAGE_CANDIDATE_EXPIRY = IMAGE_CANDIDATE_EXPIRY_TASK
INTERVAL = timedelta(hours=1)
MAX_BATCHES = 1_000
"""A bound on one run's batches (500,000 candidates); the next run goes on from there."""


async def expire(db: AsyncEngine, store: BlobStore | None) -> dict[str, int]:
    """Delete every expired candidate (module docstring): counts only."""
    expired = blobs = 0
    for _ in range(MAX_BATCHES):
        async with db.begin() as conn:
            count, checksums = await candidates.expire_batch(conn)
        expired += count
        if store is not None:
            for checksum in checksums:
                blobs += await collect_blob(db, store, checksum)
        if count < candidates.EXPIRY_BATCH:
            break
    return {"expired": expired, "blobs_deleted": blobs}


async def _run(ctx: JobContext) -> JobOutput:
    return JobOutput(result=await expire(ctx.db, media_sweep.bound()))


IMAGE_CANDIDATE_EXPIRY_HANDLER = JobHandler(
    kind=IMAGE_CANDIDATE_EXPIRY,
    payload=NoPayload,
    run=recording(_run),
    max_attempts=1,
    records_operation=False,
)
IMAGE_CANDIDATE_EXPIRY_TASK_DEFINITION = RecurringTask(IMAGE_CANDIDATE_EXPIRY, INTERVAL)

__all__ = [
    "IMAGE_CANDIDATE_EXPIRY",
    "IMAGE_CANDIDATE_EXPIRY_HANDLER",
    "IMAGE_CANDIDATE_EXPIRY_TASK_DEFINITION",
    "INTERVAL",
    "expire",
]
