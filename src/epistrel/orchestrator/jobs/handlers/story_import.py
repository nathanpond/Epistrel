"""``story_import``: one story import (FR-STORE-7, FR-ADMIN-4; #250, ``core.bundles.import_job``).

The job runs its ``bundle_jobs`` row's import: it reads the staged archive under the frozen
limits, inserts the bundle into the importing story, runs #251's gates
(``orchestrator.import_gates``, registered on import of this module) and makes the story ready,
or refuses and cleans up. A failure that is no refusal (the database, a worker crash) is retried
by #70 from the staged file; the last attempt (``MAX_ATTEMPTS``) cleans up and ends the import
``failed`` (``import_failed``). The result holds counts only.
"""

from __future__ import annotations

from typing import cast

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncConnection

from epistrel.core.bundles import import_job
from epistrel.core.ids import ULID_PATTERN
from epistrel.orchestrator import import_gates  # noqa: F401 - registers #251's gates
from epistrel.orchestrator.jobs import queue
from epistrel.orchestrator.jobs.types import JobContext, JobHandler, JobOutput

MAX_ATTEMPTS = 3


class StoryImportPayload(BaseModel):
    """The import (``bundle_jobs`` row) a job runs."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    bundle_id: str = Field(pattern=ULID_PATTERN)


async def enqueue(conn: AsyncConnection, bundle_id: str) -> str:
    """Enqueue ``bundle_id``'s job in ``conn``'s transaction (deployment-level: the story is
    ``importing``, and a story's jobs wait for its import)."""
    return await queue.enqueue(
        conn, import_job.IMPORT_KIND, StoryImportPayload(bundle_id=bundle_id)
    )


async def _run(ctx: JobContext) -> JobOutput:
    payload = cast(StoryImportPayload, ctx.payload)
    try:
        found = await import_job.run_import(ctx.db, payload.bundle_id)
    except Exception as failed:
        if ctx.job.attempt >= MAX_ATTEMPTS:
            await import_job.refuse_and_clean(
                ctx.db,
                payload.bundle_id,
                {"code": "import_failed", "message": type(failed).__name__, "details": {}},
                status="failed",
            )
            return JobOutput(result={"failed": 1})
        raise
    return JobOutput(result=found)


STORY_IMPORT_HANDLER = JobHandler(
    kind=import_job.IMPORT_KIND,
    payload=StoryImportPayload,
    run=_run,
    max_attempts=MAX_ATTEMPTS,
    records_operation=False,
)

__all__ = ["MAX_ATTEMPTS", "STORY_IMPORT_HANDLER", "StoryImportPayload", "enqueue"]
