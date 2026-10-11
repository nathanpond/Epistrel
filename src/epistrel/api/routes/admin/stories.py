"""``POST /admin/stories/{id}/rebuild``: refold a story's projections from the log (FR-ADMIN-3).

A story with more than ``rebuild_async_threshold`` events is rebuilt by a worker: the call
answers 202 ``{job_id, timeline_revision}`` (#70).
"""

from __future__ import annotations

from fastapi import APIRouter, Response
from fastapi.responses import JSONResponse

from epistrel.api.deps import ContextDep, EngineDep, StoryId
from epistrel.api.idempotency import answered
from epistrel.api.responses import MutationResponse
from epistrel.core.projections import RebuildResult
from epistrel.orchestrator.jobs.handlers.rebuild import RebuildQueued

router = APIRouter(prefix="/stories")


class RebuildResponse(MutationResponse):
    """Rows written per projection table, and the ``maintenance`` operation that wrote them."""

    counts: dict[str, int]
    operation_id: str

    @classmethod
    def of(cls, result: RebuildResult) -> RebuildResponse:
        return cls(
            counts=result.counts,
            operation_id=result.operation_id,
            timeline_revision=result.timeline_revision,
        )


class RebuildQueuedResponse(MutationResponse):
    """The ``rebuild_projections`` job a large story's rebuild was handed to (202), with the
    story's revision when it was queued."""

    job_id: str

    @classmethod
    def of(cls, queued: RebuildQueued) -> RebuildQueuedResponse:
        return cls(job_id=queued.job_id, timeline_revision=queued.timeline_revision)


@router.post(
    "/{story_id}/rebuild",
    response_model=RebuildResponse,
    responses={202: {"model": RebuildQueuedResponse, "description": "Rebuilt by a worker."}},
    operation_id="rebuild_projections",
    summary="Rebuild a story's projections from its event log",
    description=(
        "Refold every projection of the story from the event log (FR-ADMIN-3) and answer the "
        "rows written per table and the `maintenance` operation that wrote them. A story with "
        "more than `rebuild_async_threshold` events is handed to a worker instead: 202 "
        "`{job_id, timeline_revision}` (a rebuild already queued for the story is that job)."
    ),
)
async def rebuild_projections(
    story_id: StoryId, response: Response, ctx: ContextDep, engine: EngineDep
) -> RebuildResponse | JSONResponse:
    result = await engine.rebuild_projections(ctx, story_id)
    if isinstance(result, RebuildQueued):
        queued = answered(RebuildQueuedResponse.of(result), ctx, response, replayed=result.replayed)
        return JSONResponse(
            queued.model_dump(mode="json"), status_code=202, headers=dict(response.headers)
        )
    return answered(RebuildResponse.of(result), ctx, response, replayed=result.replayed)
