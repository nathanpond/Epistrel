"""``story_export``: one story export (FR-STORE-7, FR-ADMIN-4; #249, ``core.bundles``).

The job writes the archive of its ``bundle_jobs`` row: one ``REPEATABLE READ`` snapshot, the
secret scan, then the file staged and renamed into ``EPISTREL_BUNDLE_DIR``. ``configure``
gives it the directory and the deployment's secrets (the Engine at start-up). The result holds
counts only.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import cast

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from epistrel.core import bundles
from epistrel.core.ids import ULID_PATTERN
from epistrel.orchestrator.jobs import queue
from epistrel.orchestrator.jobs.types import JobContext, JobHandler, JobOutput

SecretSource = Callable[[AsyncEngine], Awaitable[tuple[str, ...]]]
"""#704: the deployment's secrets held in the database (the model API keys), read per run."""

_setup: dict[str, object] = {
    "directory": Path("var/bundles"),
    "secrets": (),
    "source": None,
    "blobs": None,
}


def configure(
    *,
    directory: Path,
    secrets: tuple[str, ...],
    source: SecretSource | None = None,
    blobs: bundles.BlobReader | None = None,
) -> None:
    """Where archives go, which values must never be in one (``secrets``, plus what
    ``source`` reads at each run: #704's model API keys) and how media blobs are read
    (``blobs``)."""
    _setup["directory"] = directory
    _setup["secrets"] = secrets
    _setup["source"] = source
    _setup["blobs"] = blobs


class StoryExportPayload(BaseModel):
    """The export (``bundle_jobs`` row) a job writes."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    bundle_id: str = Field(pattern=ULID_PATTERN)


async def enqueue(conn: AsyncConnection, bundle_id: str) -> str:
    """Enqueue ``bundle_id``'s job in ``conn``'s transaction (deployment-level)."""
    return await queue.enqueue(conn, bundles.EXPORT_KIND, StoryExportPayload(bundle_id=bundle_id))


async def _run(ctx: JobContext) -> JobOutput:
    payload = cast(StoryExportPayload, ctx.payload)
    secrets = cast(tuple[str, ...], _setup["secrets"])
    source = cast("SecretSource | None", _setup["source"])
    if source is not None:
        secrets = (*secrets, *await source(ctx.db))
    found = await bundles.run_export(
        ctx.db,
        payload.bundle_id,
        cast(Path, _setup["directory"]),
        secrets=secrets,
        blobs=cast("bundles.BlobReader | None", _setup["blobs"]),
    )
    return JobOutput(result=found)


STORY_EXPORT_HANDLER = JobHandler(
    kind=bundles.EXPORT_KIND,
    payload=StoryExportPayload,
    run=_run,
    max_attempts=3,
    records_operation=False,
)

__all__ = ["STORY_EXPORT_HANDLER", "StoryExportPayload", "configure", "enqueue"]
