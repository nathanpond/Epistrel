"""``POST /admin/stories/{id}/rebuild``: refold a story's projections from the log (FR-ADMIN-3)."""

from __future__ import annotations

from fastapi import APIRouter, Response

from epistrel.api.deps import ContextDep, EngineDep
from epistrel.api.idempotency import answered
from epistrel.api.responses import MutationResponse

router = APIRouter(prefix="/stories")


class RebuildResponse(MutationResponse):
    """Rows written per projection table, and the ``maintenance`` operation that wrote them."""

    counts: dict[str, int]
    operation_id: str


@router.post("/{story_id}/rebuild", response_model=RebuildResponse)
async def rebuild_projections(
    story_id: str, response: Response, ctx: ContextDep, engine: EngineDep
) -> RebuildResponse:
    result = await engine.rebuild_projections(ctx, story_id)
    answer = RebuildResponse(
        counts=result.counts,
        operation_id=result.operation_id,
        timeline_revision=result.timeline_revision,
    )
    return answered(answer, ctx, response, replayed=result.replayed)
