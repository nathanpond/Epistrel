"""``rebuild_projections``: refold a large story's projections as a background job (FR-ADMIN-3;
#46, #70).

A story with more than ``rebuild_async_threshold`` events is rebuilt here instead of in the
request (``Engine.rebuild_projections`` answers 202 ``{job_id}``). The handler declares
``stories:<id>`` (its ``timeline_revision``) as its input and does the fold in a commit step,
under the story lock, as a ``maintenance`` operation: a turn committed meanwhile makes the
result stale, so it is dropped and the job runs again against the current timeline (FR-CONC-8).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import cast

from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncConnection

from epistrel.core import projections
from epistrel.core.enums import OperationKind
from epistrel.core.events.operation import Operation
from epistrel.core.input_versions import input_key
from epistrel.core.tables import stories
from epistrel.orchestrator.jobs.types import JobContext, JobHandler, JobOutput

REBUILD_KIND = "rebuild_projections"


class RebuildPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    story_id: str


@dataclass(frozen=True, slots=True)
class RebuildQueued:
    """A rebuild handed to a worker: the ``rebuild_projections`` job that will run it."""

    job_id: str
    timeline_revision: int = 0
    """The story's revision when the rebuild was queued (a rebuild never moves it)."""
    replayed: bool = False
    """``True`` when an ``Idempotency-Key`` retry returned this stored result (#61)."""


def coalesce_key(story_id: str) -> str:
    """One queued rebuild per story: asking again while one waits returns that job."""
    return f"{REBUILD_KIND}:{story_id}"


async def _run(ctx: JobContext) -> JobOutput:
    payload = cast(RebuildPayload, ctx.payload)
    story_id = payload.story_id
    async with ctx.snapshot() as conn:
        await ctx.declare_input(conn, input_key(stories.name, story_id))
    counts: dict[str, int] = {}

    async def fold(conn: AsyncConnection) -> None:
        counts.update(await projections.replace_with_fold(conn, story_id))

    def stage(op: Operation) -> None:
        op.add_commit_step(fold)

    return JobOutput(stage=stage, result_of=lambda: {"counts": dict(counts)})


REBUILD_HANDLER = JobHandler(
    kind=REBUILD_KIND,
    payload=RebuildPayload,
    run=_run,
    operation_kind=OperationKind.MAINTENANCE,
)

__all__ = ["REBUILD_HANDLER", "REBUILD_KIND", "RebuildPayload", "RebuildQueued", "coalesce_key"]
