"""A story's backfill (FR-PROF-8,9; docs/02 §9; #228).

- ``GET /stories/{id}/backfill``: the story's latest backfill.
- ``POST /stories/{id}/backfill``: a two-stage gap fill of the capabilities the story has on.
- ``DELETE /stories/{id}/backfill``: cancel the running one.

Every handler makes one ``Engine`` call (``core.backfill`` holds the rules).
"""

from __future__ import annotations

from fastapi import APIRouter, Response
from fastapi.responses import JSONResponse
from pydantic import Field

from epistrel.api.deps import ContextDep, EngineDep, StoryId
from epistrel.api.idempotency import answered
from epistrel.api.responses import MutationResponse
from epistrel.api.routes.story_profile import BackfillStartedBody
from epistrel.core.backfill import Backfill, BackfillCancelled, BackfillRequest, GapFill, Preview

router = APIRouter(prefix="/stories", tags=["stories"])

_RUN = (
    "While a backfill runs the story is paused: every truth-changing call is 409 "
    "`story_paused` with `cause: backfill` and its progress, and reads are served. Its rows are "
    "visible only once it completes, all at once."
)


class GapFillBody(MutationResponse):
    """A gap fill's answer: ``applied`` is false; without ``ignore_warnings`` the ``backfill``
    preview (needed, estimate, range, capabilities), with it also ``started``, the backfill
    queued (REST 202)."""

    applied: bool
    backfill: Preview | None
    started: BackfillStartedBody | None = Field(default=None, exclude_if=lambda v: v is None)

    @classmethod
    def of(cls, found: GapFill) -> GapFillBody:
        return cls(
            applied=found.applied,
            backfill=found.preview,
            started=BackfillStartedBody.of(found.backfill) if found.backfill is not None else None,
            timeline_revision=found.timeline_revision,
        )


class BackfillCancelledBody(MutationResponse):
    """A cancelled backfill's final state."""

    backfill: Backfill

    @classmethod
    def of(cls, found: BackfillCancelled) -> BackfillCancelledBody:
        return cls(backfill=found.backfill, timeline_revision=found.timeline_revision)


@router.get(
    "/{story_id}/backfill",
    response_model=Backfill,
    operation_id="get_backfill",
    summary="Read a story's latest backfill",
    description=(
        "The story's latest backfill of any kind (#228): its status (`queued`, `extracting`, "
        "`reconciling`, `completed`, `cancelled`, `failed`), the capabilities it re-derives over "
        "its turn range, its progress (`phase`, `chunks_done` of `chunks_total`, "
        "`reconciled_through_turn`, `last_turn`), its contradictions by status and its report. "
        "404 `backfill_not_found` when the story never had one."
    ),
)
async def get_backfill(story_id: StoryId, ctx: ContextDep, engine: EngineDep) -> Backfill:
    return await engine.get_backfill(ctx, story_id)


@router.post(
    "/{story_id}/backfill",
    response_model=GapFillBody,
    responses={202: {"model": GapFillBody, "description": "The backfill is queued."}},
    operation_id="start_backfill",
    summary="Backfill the gaps in a story's structured data",
    description=(
        "A gap fill (FR-PROF-9): re-derive, from the stored transcript and never regenerating "
        "anything, the data of the capabilities the story's configuration has on (or "
        "`capabilities`, a subset of them) over the turns they do not cover. Without "
        "`ignore_warnings` the answer is the preview (`backfill: {needed, estimate, range, "
        "capabilities}`) and nothing changes; with it the backfill is queued: 202 with `started`. "
        "409 `nothing_to_backfill` when every capability covers the story's turns, 409 "
        "`backfill_in_progress` while one runs; a capability the story does not have on is 422 "
        "`validation_error`. " + _RUN
    ),
)
async def start_backfill(
    story_id: StoryId,
    body: BackfillRequest,
    response: Response,
    ctx: ContextDep,
    engine: EngineDep,
) -> GapFillBody | JSONResponse:
    found = await engine.start_backfill(ctx, story_id, body)
    shown = answered(GapFillBody.of(found), ctx, response, replayed=found.replayed)
    if found.backfill is None:
        return shown
    return JSONResponse(
        shown.model_dump(mode="json"), status_code=202, headers=dict(response.headers)
    )


@router.delete(
    "/{story_id}/backfill",
    response_model=BackfillCancelledBody,
    operation_id="cancel_backfill",
    summary="Cancel a story's backfill",
    description=(
        "Cancel the running backfill (#228): its rows are discarded (never deleted), its "
        "extraction checkpoints dropped, the configuration it held is not applied, and the story "
        "resumes. The answer is the backfill's final state. 404 `backfill_not_found` without "
        "one; 409 `backfill_not_cancellable` once it has ended."
    ),
)
async def cancel_backfill(
    story_id: StoryId, response: Response, ctx: ContextDep, engine: EngineDep
) -> BackfillCancelledBody:
    found = await engine.cancel_backfill(ctx, story_id)
    return answered(BackfillCancelledBody.of(found), ctx, response, replayed=found.replayed)


__all__ = ["router"]
