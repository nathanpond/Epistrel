"""A story's entities: the author's identity merge (FR-PROP-7, FR-DIRECT-8; #225).

``POST /stories/{id}/entities/merge`` opens a rewrite session of kind ``entity_merge``: the
entities are one referent from now on and always were (``same_as``, timeless), and the merge
cascades through their dependents like any rewrite. One ``Engine`` call.
"""

from __future__ import annotations

from fastapi import APIRouter, Response
from fastapi import status as http_status

from epistrel.api.deps import ContextDep, EngineDep, StoryId
from epistrel.api.idempotency import answered
from epistrel.api.routes.rewrites import RewriteSessionBody
from epistrel.orchestrator.rewrite.api import MergeRequest

router = APIRouter(prefix="/stories", tags=["stories"])


@router.post(
    "/{story_id}/entities/merge",
    response_model=RewriteSessionBody,
    status_code=http_status.HTTP_201_CREATED,
    operation_id="merge_entities",
    summary="Merge entities into one referent",
    description=(
        "The author reveals that entities are one referent (FR-PROP-7): the first of "
        "`entity_ids` survives and each other is `same_as` it. This opens a rewrite session of "
        "kind `entity_merge` (no instruction is parsed) with a preview of what the merge "
        "touches, under `resolution_mode` and `preview` as `POST /stories/{id}/rewrites` "
        "takes them; it is applied like any session (an `identity_proposal` for the pair is "
        "approved with it). What people said keeps its text, intent and belief relation; only "
        "its objective relation follows canon. 422 `merge_invalid` (`too_few`, `not_in_story`, "
        "`type_mismatch`, `already_same`); 409 `story_paused`, `rewrite_session_limit`. An "
        "omniscient call: 403 `omniscient_required` for a character."
    ),
)
async def merge_entities(
    story_id: StoryId,
    body: MergeRequest,
    response: Response,
    ctx: ContextDep,
    engine: EngineDep,
    viewpoint: str | None = None,
) -> RewriteSessionBody:
    found = await engine.merge_entities(ctx, story_id, body, viewpoint=viewpoint)
    return answered(RewriteSessionBody.of(found), ctx, response, replayed=found.replayed)


__all__ = ["router"]
