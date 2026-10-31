"""The reconciliation status of a story's external effects (FR-EXT-10,14,18; #210).

- ``GET /stories/{id}/reconciliation``: every effect dispatched to a write-capable provider that
  Epistrel cannot report as applied (``core.outbox.listings``).

The handler makes one ``Engine`` call.
"""

from __future__ import annotations

from fastapi import APIRouter

from epistrel.api.deps import ContextDep, EngineDep, StoryId
from epistrel.core.outbox import Reconciliation

router = APIRouter(prefix="/stories", tags=["stories"])


@router.get(
    "/{story_id}/reconciliation",
    response_model=Reconciliation,
    operation_id="reconciliation",
    summary="Read the story's reconciliation status",
)
async def reconciliation(story_id: StoryId, ctx: ContextDep, engine: EngineDep) -> Reconciliation:
    """The effects dispatched to write-capable providers that are not confirmed, oldest first:
    each one's outbox id (the provider idempotency key), provider and value, status, why it is
    listed (`reason`: `unconfirmed`, `failed`, `unknown`, `reconciliation_required`,
    `cancelled_on_reactivation`, `confirmed_unsettled`, `confirmed_discarded_origin`,
    `settlement_conflict`, `late_success_after_expiry`, `outcome_conflict` or
    `reservation_expired`), since when, the effect, and the latest outcome recorded for it. A
    confirmed or cancelled effect is not listed. 404 for an unknown story."""
    return await engine.reconciliation(ctx, story_id)


__all__ = ["router"]
