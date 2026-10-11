"""``POST /admin/stories/{id}/rebuild``: refold a story's projections from the log (FR-ADMIN-3).

A story with more than ``rebuild_async_threshold`` events is rebuilt by a worker: the call
answers 202 ``{job_id, timeline_revision}`` (#70).
"""

from __future__ import annotations

from fastapi import APIRouter, Response
from fastapi.responses import JSONResponse

from epistrel.api.deps import ContextDep, EngineDep
from epistrel.api.idempotency import answered
from epistrel.api.responses import MutationResponse
from epistrel.orchestrator.jobs.handlers.rebuild import RebuildQueued

router = APIRouter(prefix="/stories")


class RebuildResponse(MutationResponse):
    """Rows written per projection table, and the ``maintenance`` operation that wrote them."""

    counts: dict[str, int]
    operation_id: str


class RebuildQueuedResponse(MutationResponse):
    """The ``rebuild_projections`` job a large story's rebuild was handed to (202), with the
    story's revision when it was queued."""

    job_id: str


@router.post(
    "/{story_id}/rebuild",
    response_model=RebuildResponse,
    responses={202: {"model": RebuildQueuedResponse, "description": "Rebuilt by a worker."}},
)
async def rebuild_projections(
    story_id: str, response: Response, ctx: ContextDep, engine: EngineDep
) -> RebuildResponse | JSONResponse:
    result = await engine.rebuild_projections(ctx, story_id)
    if isinstance(result, RebuildQueued):
        queued = answered(
            RebuildQueuedResponse(job_id=result.job_id, timeline_revision=result.timeline_revision),
            ctx,
            response,
            replayed=result.replayed,
        )
        return JSONResponse(
            queued.model_dump(mode="json"), status_code=202, headers=dict(response.headers)
        )
    answer = RebuildResponse(
        counts=result.counts,
        operation_id=result.operation_id,
        timeline_revision=result.timeline_revision,
    )
    return answered(answer, ctx, response, replayed=result.replayed)
