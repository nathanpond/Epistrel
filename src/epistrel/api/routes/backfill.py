"""A story's backfill (FR-PROF-8,9; docs/02 §9; #228).

- ``GET /stories/{id}/backfill``: the story's latest backfill.
- ``POST /stories/{id}/backfill``: a two-stage gap fill of the capabilities the story has on.
- ``DELETE /stories/{id}/backfill``: cancel the running one.
- ``POST /stories/{id}/backfill/conflicts/{cid}``: settle one of an ``ask`` backfill's
  conflicts (#230).

Every handler makes one ``Engine`` call (``core.backfill`` holds the rules).
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Path, Query, Response
from fastapi.responses import JSONResponse
from pydantic import Field

from epistrel.api.deps import ContextDep, EngineDep, StoryId
from epistrel.api.idempotency import answered
from epistrel.api.responses import MutationResponse
from epistrel.api.routes.story_profile import BackfillStartedBody
from epistrel.core.backfill import Backfill, BackfillCancelled, BackfillRequest, GapFill, Preview
from epistrel.core.backfill.conflicts import Conflict, Resolved, ResolveRequest
from epistrel.core.ids import ULID_PATTERN

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
    backfill: Preview | dict[str, Any] | None
    started: BackfillStartedBody | None = Field(default=None, exclude_if=lambda v: v is None)
    conflicts_open: int | None = Field(default=None, exclude_if=lambda v: v is None)
    """#230: on a backfill awaiting its conflicts, how many are open."""
    conflicts: list[Conflict] | None = Field(default=None, exclude_if=lambda v: v is None)
    """#230: without ``ignore_warnings``, the open conflicts with their recommended answers."""
    conflicts_made_uncertain: int | None = Field(default=None, exclude_if=lambda v: v is None)
    """#230: with ``ignore_warnings``, how many open conflicts were left ``uncertain``."""

    @classmethod
    def of(cls, found: GapFill) -> GapFillBody:
        if found.resumed is not None:
            resumed = found.resumed
            return cls(
                applied=resumed.applied,
                backfill={"status": resumed.status},
                conflicts_open=resumed.conflicts_open,
                conflicts=None if resumed.applied else list(resumed.conflicts),
                conflicts_made_uncertain=resumed.conflicts_made_uncertain
                if resumed.applied
                else None,
                timeline_revision=found.timeline_revision,
            )
        return cls(
            applied=found.applied,
            backfill=found.preview,
            started=BackfillStartedBody.of(found.backfill) if found.backfill is not None else None,
            timeline_revision=found.timeline_revision,
        )


class ResolvedBody(MutationResponse):
    """A settled conflict, and the backfill's status and open count after it."""

    conflict: Conflict
    backfill: dict[str, Any]

    @classmethod
    def of(cls, found: Resolved) -> ResolvedBody:
        return cls(
            conflict=found.conflict,
            backfill={"status": found.status, "conflicts_open": found.conflicts_open},
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
        "#230: `conflict_list` lists its conflicts with their statements (`statement_id`, "
        "`event_id`, `turn`, `position`, `speaker`, `source`, the quoted transcript `quote`, "
        "`value`), each open one with its `recommended` answer (the `last_mention` statement): "
        "the open ones by default, every one with `conflicts=all`; at most `limit` (50, at most "
        "200). A backfill under `backfill_conflict_policy: ask` ends its pass "
        "`paused_for_conflicts` and the story stays paused (409 `story_paused`, `cause: "
        "backfill_conflicts`, `conflicts_open`) until they are resolved. 404 "
        "`backfill_not_found` when the story never had one."
    ),
)
async def get_backfill(
    story_id: StoryId,
    ctx: ContextDep,
    engine: EngineDep,
    conflicts: Annotated[Literal["open", "all"], Query()] = "open",
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> Backfill:
    return await engine.get_backfill(ctx, story_id, conflicts=conflicts, limit=limit)


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
        "`validation_error`. On a backfill `paused_for_conflicts` (#230) it resumes instead: "
        "without `ignore_warnings`, 200 `applied: false` with `conflicts_open` and the open "
        "`conflicts` (nothing changes); with it the backfill completes and every open conflict "
        "becomes `uncertain` (`conflicts_made_uncertain`): an open thunk where the target "
        "configuration runs commitment. " + _RUN
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
    if found.backfill is None or found.resumed is not None:
        return shown
    return JSONResponse(
        shown.model_dump(mode="json"), status_code=202, headers=dict(response.headers)
    )


ConflictId = Annotated[str, Path(pattern=ULID_PATTERN, description="The conflict's id.")]


@router.post(
    "/{story_id}/backfill/conflicts/{cid}",
    response_model=ResolvedBody,
    operation_id="resolve_backfill_conflict",
    summary="Resolve one of a backfill's conflicts",
    description=(
        "FR-PROF-10 (#230): settle an open conflict of a backfill `paused_for_conflicts`, with "
        "exactly one of `choose` (a statement id), `value` (a value one of the statements "
        "states) or `accept_recommendation: true` (the listing's recommended answer, the "
        "`last_mention` statement). The chosen statement is canon for the overlap (provenance "
        "`author`) and the others, when a character spoke them, are recorded as in-world "
        "claims; the conflict is `resolved` with `resolution {choice, via, by}`. Resolving the "
        "last open one completes the backfill in the same call (`backfill.status: completed`, "
        "the story resumed). 404 `backfill_not_found` / `backfill_conflict_not_found`, 409 "
        "`backfill_not_awaiting_conflicts`, 409 `backfill_conflict_closed`, 422 "
        "`invalid_resolution` (or `validation_error` for a body with none or several)."
    ),
)
async def resolve_backfill_conflict(
    story_id: StoryId,
    cid: ConflictId,
    body: ResolveRequest,
    response: Response,
    ctx: ContextDep,
    engine: EngineDep,
) -> ResolvedBody:
    found = await engine.resolve_backfill_conflict(ctx, story_id, cid, body)
    return answered(ResolvedBody.of(found), ctx, response, replayed=found.replayed)


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
