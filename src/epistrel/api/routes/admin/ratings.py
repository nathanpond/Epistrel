"""``/admin/content-ratings``: the deployment's content ratings (FR-RATE-1,2, FR-API-6).

Create, list (by display order; retired ones only with ``?include_retired=1``), get (latest
version plus effective guardrails and their sources), list versions, update (a full
replacement that creates a new version) and retire. Writes return the deployment's
``config_revision`` (``DeploymentMutationResponse``).
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Response, status
from pydantic import BaseModel

from epistrel.api.deps import ContextDep, EngineDep
from epistrel.api.idempotency import answered, no_content
from epistrel.api.responses import DeploymentMutationResponse
from epistrel.core.ratings import Rating, RatingFields, RatingReplacement, RatingWrite

router = APIRouter(prefix="/content-ratings")


class RatingBody(BaseModel):
    """A rating at one version."""

    id: str
    name: str
    description: str
    display_order: int
    prompt_guidance: str
    image_guidance: str
    policy_ids: list[str]
    inherits_from: list[str]
    version_id: str
    version: int
    created_at: datetime
    created_by: str
    retired_at: datetime | None

    @classmethod
    def of(cls, rating: Rating) -> RatingBody:
        return cls(
            id=rating.id,
            name=rating.name,
            description=rating.description,
            display_order=rating.display_order,
            prompt_guidance=rating.prompt_guidance,
            image_guidance=rating.image_guidance,
            policy_ids=list(rating.policy_ids),
            inherits_from=list(rating.inherits_from),
            version_id=rating.version_id,
            version=rating.version,
            created_at=rating.created_at,
            created_by=rating.created_by,
            retired_at=rating.retired_at,
        )


class EffectiveGuardrail(BaseModel):
    policy_id: str
    source_rating_id: str


class RatingDetailBody(RatingBody):
    effective_guardrails: list[EffectiveGuardrail]


class RatingList(BaseModel):
    items: list[RatingBody]


class RatingMutation(RatingBody, DeploymentMutationResponse):
    """A rating after a write, with the deployment's ``config_revision``."""

    @classmethod
    def written(cls, write: RatingWrite) -> RatingMutation:
        return cls(
            **RatingBody.of(write.rating).model_dump(), config_revision=write.config_revision
        )


@router.post("", status_code=status.HTTP_201_CREATED, response_model=RatingMutation)
async def create_rating(
    body: RatingFields, response: Response, ctx: ContextDep, engine: EngineDep
) -> RatingMutation:
    write = await engine.create_content_rating(ctx, body)
    return answered(RatingMutation.written(write), ctx, response, replayed=write.replayed)


@router.get("", response_model=RatingList)
async def list_ratings(
    ctx: ContextDep, engine: EngineDep, include_retired: bool = False
) -> RatingList:
    found = await engine.list_content_ratings(ctx, include_retired=include_retired)
    return RatingList(items=[RatingBody.of(rating) for rating in found])


@router.get("/{rating_id}", response_model=RatingDetailBody)
async def get_rating(rating_id: str, ctx: ContextDep, engine: EngineDep) -> RatingDetailBody:
    detail = await engine.get_content_rating(ctx, rating_id)
    return RatingDetailBody(
        **RatingBody.of(detail.rating).model_dump(),
        effective_guardrails=[
            EffectiveGuardrail(policy_id=g.policy_id, source_rating_id=g.source_rating_id)
            for g in detail.effective_guardrails
        ],
    )


@router.get("/{rating_id}/versions", response_model=RatingList)
async def list_versions(rating_id: str, ctx: ContextDep, engine: EngineDep) -> RatingList:
    found = await engine.content_rating_versions(ctx, rating_id)
    return RatingList(items=[RatingBody.of(rating) for rating in found])


@router.put("/{rating_id}", response_model=RatingMutation)
async def update_rating(
    rating_id: str,
    body: RatingReplacement,
    response: Response,
    ctx: ContextDep,
    engine: EngineDep,
) -> RatingMutation:
    write = await engine.update_content_rating(ctx, rating_id, body)
    return answered(RatingMutation.written(write), ctx, response, replayed=write.replayed)


@router.delete(
    "/{rating_id}",
    response_model=RatingMutation,
    responses={status.HTTP_204_NO_CONTENT: {"description": "Already retired; nothing changed."}},
)
async def retire_rating(
    rating_id: str, response: Response, ctx: ContextDep, engine: EngineDep
) -> RatingMutation | Response:
    """Retire the rating (200 with the retired rating); 204 when it was already retired;
    409 when it is the deployment default."""
    write = await engine.retire_content_rating(ctx, rating_id)
    if not write.changed:
        return no_content(ctx, replayed=write.replayed)
    return answered(RatingMutation.written(write), ctx, response, replayed=write.replayed)
