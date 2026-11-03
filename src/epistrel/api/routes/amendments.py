"""A story's amendments: read them, undo and redo them (FR-REWRITE-9,10,11; #238).

- ``GET /stories/{id}/amendments``: the amendments, newest first.
- ``GET /stories/{id}/amendments/{aid}``: one amendment, its parts and edges.
- ``POST /stories/{id}/amendments/{aid}/undo``: undo it as a checked rewrite.
- ``POST /stories/{id}/amendments/{aid}/redo``: redo a reverted one the same way.

Every route is an omniscient call. Undo and redo open a rewrite session (kinds ``undo`` and
``redo``) that is answered and applied through ``/stories/{id}/rewrites/{rid}/…``. One
``Engine`` call per handler.
"""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Path, Query, Response
from fastapi import status as http_status

from epistrel.api.deps import ContextDep, EngineDep, StoryId
from epistrel.api.idempotency import answered
from epistrel.api.routes.rewrites import RewriteSessionBody
from epistrel.core.amendments.read import (
    LIST_DEFAULT,
    LIST_MAX,
    AmendmentDetail,
    AmendmentPage,
)
from epistrel.core.ids import ULID_PATTERN
from epistrel.orchestrator.rewrite.api import AmendmentSessionRequest

router = APIRouter(prefix="/stories", tags=["stories"])

AmendmentId = Annotated[str, Path(pattern=ULID_PATTERN, description="The amendment's id.")]
_OMNISCIENT = (
    " An omniscient call: 422 `viewpoint_unresolved` without `?viewpoint`, 403 "
    "`omniscient_required` for a character. 404 `amendment_not_found` for an unknown "
    "amendment or another story's."
)
_SESSION = (
    " The answer is the rewrite session (201), answered and applied through "
    "`/stories/{id}/rewrites/{rid}`, revalidated at its final commit like any rewrite. 409 "
    "`story_paused`, `rewrite_session_limit`, `revision_conflict` for a stale "
    "`X-Epistrel-Expected-Revision`, `module_off` (`module: lite_rewrites`) for a Lite story."
)


@router.get(
    "/{story_id}/amendments",
    response_model=AmendmentPage,
    operation_id="list_amendments",
    summary="List a story's amendments",
    description=(
        "The story's amendments (FR-REWRITE-9), newest `tx_time` first: kind, status, author, "
        "parts count, `reverted_by`, `undoes`, `redoes` and the rewrite session that recorded "
        "it. `status` filters (`active` or `reverted`); `cursor` is the previous page's "
        "`next_cursor`." + _OMNISCIENT
    ),
)
async def list_amendments(
    story_id: StoryId,
    ctx: ContextDep,
    engine: EngineDep,
    viewpoint: str | None = None,
    status: Annotated[Literal["active", "reverted"] | None, Query()] = None,
    cursor: Annotated[str | None, Query(pattern=ULID_PATTERN)] = None,
    limit: Annotated[int, Query(ge=1, le=LIST_MAX)] = LIST_DEFAULT,
) -> AmendmentPage:
    return await engine.list_amendments(
        ctx, story_id, viewpoint=viewpoint, status=status, cursor=cursor, limit=limit
    )


@router.get(
    "/{story_id}/amendments/{amendment_id}",
    response_model=AmendmentDetail,
    operation_id="get_amendment",
    summary="Read an amendment",
    description=(
        "One amendment with its `parts` (every part row by ordinal), `relies_on` (the "
        "dependency edges its parts were recorded with, FR-REWRITE-11) and `dependents` (the "
        "records and later amendments that depend on its parts or the facts they wrote, one "
        "level deep)." + _OMNISCIENT
    ),
)
async def get_amendment(
    story_id: StoryId,
    amendment_id: AmendmentId,
    ctx: ContextDep,
    engine: EngineDep,
    viewpoint: str | None = None,
) -> AmendmentDetail:
    return await engine.get_amendment(ctx, story_id, amendment_id, viewpoint=viewpoint)


@router.post(
    "/{story_id}/amendments/{amendment_id}/undo",
    response_model=RewriteSessionBody,
    status_code=http_status.HTTP_201_CREATED,
    operation_id="undo_amendment",
    summary="Undo an amendment as a checked rewrite",
    description=(
        "Undo is a new, checked amendment (FR-REWRITE-10): a rewrite session of kind `undo` "
        "whose changes take back the amendment's parts and whose blast radius is planned "
        "against the restored canon. A later amendment relying on it is asked about "
        "(`cascade_undo`, recommended, or `cancel`, which cancels the undo); a record that "
        "would contradict the restored canon is repaired. Applied, it records an `undo` "
        "amendment and the undone one is `reverted`, never deleted. 409 "
        "`amendment_already_reverted`; 422 `use_redo` for an `undo` amendment."
        + _SESSION
        + _OMNISCIENT
    ),
)
async def undo_amendment(
    story_id: StoryId,
    amendment_id: AmendmentId,
    body: AmendmentSessionRequest,
    response: Response,
    ctx: ContextDep,
    engine: EngineDep,
    viewpoint: str | None = None,
) -> RewriteSessionBody:
    found = await engine.undo_amendment(ctx, story_id, amendment_id, body, viewpoint=viewpoint)
    return answered(RewriteSessionBody.of(found), ctx, response, replayed=found.replayed)


@router.post(
    "/{story_id}/amendments/{amendment_id}/redo",
    response_model=RewriteSessionBody,
    status_code=http_status.HTTP_201_CREATED,
    operation_id="redo_amendment",
    summary="Redo a reverted amendment as a checked rewrite",
    description=(
        "Redo is another checked operation (FR-REWRITE-10): a rewrite session of kind `redo` "
        "re-planning the reverted amendment's original parts against the story as it is now. "
        "Applied, it records a `redo` amendment; the original stays `reverted`. 409 "
        "`amendment_not_reverted` for an active amendment, `amendment_already_redone` when "
        "an active amendment already redoes it." + _SESSION + _OMNISCIENT
    ),
)
async def redo_amendment(
    story_id: StoryId,
    amendment_id: AmendmentId,
    body: AmendmentSessionRequest,
    response: Response,
    ctx: ContextDep,
    engine: EngineDep,
    viewpoint: str | None = None,
) -> RewriteSessionBody:
    found = await engine.redo_amendment(ctx, story_id, amendment_id, body, viewpoint=viewpoint)
    return answered(RewriteSessionBody.of(found), ctx, response, replayed=found.replayed)


__all__ = ["router"]
