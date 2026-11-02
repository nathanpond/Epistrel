"""The reconciliation status of a story's external effects (FR-EXT-10,14,18,22; #210, #218).

- ``GET /stories/{id}/reconciliation``: every effect dispatched to a write-capable provider that
  Epistrel cannot report as applied (``core.outbox.listings``).
- ``POST /stories/{id}/reconciliation/{outbox_id}/settle-now``: the author settles a listed
  late success, discarded-origin confirmation or settlement conflict now, re-checked at that
  moment (#218).
- ``POST /stories/{id}/reconciliation/{outbox_id}/compensate``: the author issues a
  compensating external effect (a refund) for it through the outbox (#218).
- ``POST /stories/{id}/reconciliation/{listing}/resolve``: the author resolves a listed
  consumer-applied effect, which nothing dispatches (#661).

Every handler makes one ``Engine`` call (``orchestrator.jobs.settlement`` holds the rules).
"""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Path, Query, Response
from fastapi import status as http_status
from pydantic import BaseModel

from epistrel.api.deps import ContextDep, EngineDep, StoryId
from epistrel.api.idempotency import answered
from epistrel.api.responses import MutationResponse
from epistrel.core.ids import ULID_PATTERN
from epistrel.core.outbox import (
    OUTBOX_LIMIT_DEFAULT,
    OUTBOX_LIMIT_MAX,
    CompensateRequest,
    OutboxRow,
    Reconciliation,
    ResolveRequest,
)
from epistrel.orchestrator.jobs.settlement import Compensated, Resolved, SettledNow

router = APIRouter(prefix="/stories", tags=["stories"])

OutboxId = Annotated[
    str,
    Path(
        pattern=ULID_PATTERN,
        description="The listed effect's outbox id (the provider idempotency key; a ULID).",
        examples=["01J9Z8Q4N5V6W7X8Y9Z0A1B2F7"],
    ),
]

_LISTED = (
    " 404 `not_found` (`reason: no_listing`) unless the effect is listed "
    "`late_success_after_expiry`, `confirmed_discarded_origin` or `settlement_conflict`; 409 "
    "`module_off` while the story's configuration has States off."
)


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
    `late_success_after_expiry` carries its `reverification` (`passed` or `failed`, with
    `reasons`); an effect the author chose to compensate shows `status: compensated` and
    `compensated_by`, the compensating effect, until that one is confirmed. A confirmed or
    cancelled effect is not listed. 404 for an unknown story."""
    return await engine.reconciliation(ctx, story_id)


class OutboxBody(BaseModel):
    """#679: a page of the story's outbox rows (any status) by id, `next_after` (the id to
    continue after; `null` on the last page), read for `omniscient`."""

    rows: list[OutboxRow]
    next_after: str | None
    output_viewpoint: Literal["omniscient"] = "omniscient"


@router.get(
    "/{story_id}/outbox",
    response_model=OutboxBody,
    operation_id="list_outbox",
    summary="List the story's outbox rows",
    description=(
        "Every write-capable external effect the story committed, whatever its status "
        "(#679): `intended`, `claimed`, `confirmed`, `failed`, `unknown`, `cancelled`, or "
        "`compensated` once a compensating effect answers it (`compensated_by`). Each row has "
        "its outbox id (the provider idempotency key), provider and value, the effect, where it "
        "was declared (`position`) and its timeline `membership`, whether a story-owned change "
        "waits on it (`contingent`), the row it `compensates`, its send `attempts` and times, "
        "and the latest recorded `outcome`. A row an erasure redacted carries ids, provider, "
        "status and position only (`redacted`). A page by id (`after_id`; `limit` at most "
        "200), filtered by `status` and `provider`. An omniscient read: 422 "
        "`viewpoint_unresolved` when no viewpoint is named, 403 `omniscient_required` for a "
        "character. 404 for an unknown story."
    ),
)
async def list_outbox(
    story_id: StoryId,
    ctx: ContextDep,
    engine: EngineDep,
    viewpoint: str | None = None,
    status: str | None = None,
    provider: str | None = None,
    after_id: Annotated[str | None, Query(pattern=ULID_PATTERN)] = None,
    limit: Annotated[int, Query(ge=1, le=OUTBOX_LIMIT_MAX)] = OUTBOX_LIMIT_DEFAULT,
) -> OutboxBody:
    page = await engine.list_outbox(
        ctx,
        story_id,
        viewpoint=viewpoint,
        status=status,
        provider=provider,
        after_id=after_id,
        limit=limit,
    )
    return OutboxBody(rows=page.rows, next_after=page.next_after)


class SettledNowBody(MutationResponse):
    """A settle-now: the effect and its confirmed outcome record, the re-verification at that
    moment (`reverification`, `reasons`), and `result`: `settled` at the slot `position`
    (`N⁺k`; `slot` the full position, by `operation_id`), or `queued` while an incomplete
    staged turn holds the slot (applied at the next one)."""

    outbox_id: str
    outcome_id: str
    result: str
    reverification: str
    reasons: list[str]
    operation_id: str | None = None
    position: str | None = None
    slot: dict[str, int] | None = None

    @classmethod
    def of(cls, done: SettledNow) -> SettledNowBody:
        return cls(
            outbox_id=done.outbox_id,
            outcome_id=done.outcome_id,
            result=done.result,
            reverification=done.reverification,
            reasons=done.reasons,
            operation_id=done.operation_id,
            position=done.position,
            slot=done.slot,
            timeline_revision=done.timeline_revision,
        )


class CompensatedBody(MutationResponse):
    """A compensation: the new outbox row (`outbox_id`, `intended`, dispatched like any other)
    that compensates `compensates`, its slot (`position`, `N⁺k`; `slot` the full position) and
    its operation."""

    outbox_id: str
    compensates: str
    status: str
    position: str
    slot: dict[str, int]
    operation_id: str

    @classmethod
    def of(cls, done: Compensated) -> CompensatedBody:
        return cls(
            outbox_id=done.outbox_id,
            compensates=done.compensates,
            status=done.status,
            position=done.position,
            slot=done.slot,
            operation_id=done.operation_id,
            timeline_revision=done.timeline_revision,
        )


@router.post(
    "/{story_id}/reconciliation/{outbox_id}/settle-now",
    response_model=SettledNowBody,
    operation_id="settle_now",
    summary="Settle a listed effect now",
    description=(
        "Settle a listed external effect now (FR-EXT-22): its transfer is re-checked against "
        "current state at this moment (the item still the seller's, no other hold on it, no "
        "other owner in canon, the State bounds) and applied at the next inter-turn slot as a "
        "late settlement, and the listing is resolved. 409 `settle_refused` (`reasons`) when "
        "re-verification fails: nothing changes." + _LISTED
    ),
)
async def settle_now(
    story_id: StoryId,
    outbox_id: OutboxId,
    response: Response,
    ctx: ContextDep,
    engine: EngineDep,
) -> SettledNowBody:
    done = await engine.settle_now(ctx, story_id, outbox_id)
    return answered(SettledNowBody.of(done), ctx, response, replayed=done.replayed)


@router.post(
    "/{story_id}/reconciliation/{outbox_id}/compensate",
    response_model=CompensatedBody,
    status_code=http_status.HTTP_201_CREATED,
    operation_id="compensate",
    summary="Compensate a listed effect",
    description=(
        "Issue a compensating external effect (a refund) for a listed effect (FR-EXT-22): "
        "`effect` (`{state, scope_key?, delta | value, reason}`) on an externally owned State "
        "becomes a new `intended` outbox row naming the listed one (`compensates`), written by "
        "an inter-turn slot operation and dispatched like any other. The listed effect shows "
        "`status: compensated` until the compensating effect is confirmed. 422 "
        "`provider_read_only` when the State's provider is not write-capable; 422 "
        "`state_unpermitted` for a State no external provider owns." + _LISTED
    ),
)
async def compensate(
    story_id: StoryId,
    outbox_id: OutboxId,
    body: CompensateRequest,
    response: Response,
    ctx: ContextDep,
    engine: EngineDep,
) -> CompensatedBody:
    done = await engine.compensate(ctx, story_id, outbox_id, body)
    return answered(CompensatedBody.of(done), ctx, response, replayed=done.replayed)


ListingId = Annotated[
    str,
    Path(
        pattern=ULID_PATTERN,
        description=(
            "The listed consumer-applied effect's request id (its `requested_effect`'s "
            "`effect_id`, the listing's `request_id`; a ULID)."
        ),
        examples=["01J9Z8Q4N5V6W7X8Y9Z0A1B2F8"],
    ),
]


class ResolvedBody(MutationResponse):
    """A resolve: the listed request, how it was resolved, the reasons it was listed under,
    and the inter-turn slot (`position`, `N⁺k`; `slot` the full position) and operation that
    recorded it."""

    request_id: str
    resolution: str
    reasons: list[str]
    position: str
    slot: dict[str, int]
    operation_id: str

    @classmethod
    def of(cls, done: Resolved) -> ResolvedBody:
        return cls(
            request_id=done.request_id,
            resolution=done.resolution,
            reasons=done.reasons,
            position=done.position,
            slot=done.slot,
            operation_id=done.operation_id,
            timeline_revision=done.timeline_revision,
        )


@router.post(
    "/{story_id}/reconciliation/{listing}/resolve",
    response_model=ResolvedBody,
    operation_id="resolve_listing",
    summary="Resolve a listed consumer-applied effect",
    description=(
        "Resolve a listed consumer-applied effect (FR-EXT-22; #661): one a read-only "
        "provider's consumer applies, listed by its `request_id`. `resolution` is "
        "`applied_elsewhere` (the consumer applied it) or `abandoned` (it never will be). "
        "Nothing is dispatched and no outcome is recorded: the listing is closed and a "
        "`reconciliation_resolved` audit event is written, at the next inter-turn slot. 404 "
        "`not_found` (`reason: no_listing`) for anything but a listed consumer-applied effect "
        "(an outbox row takes settle-now or compensate); 409 `module_off` while the story's "
        "configuration has States off."
    ),
)
async def resolve_listing(
    story_id: StoryId,
    listing: ListingId,
    body: ResolveRequest,
    response: Response,
    ctx: ContextDep,
    engine: EngineDep,
) -> ResolvedBody:
    done = await engine.resolve_listing(ctx, story_id, listing, body)
    return answered(ResolvedBody.of(done), ctx, response, replayed=done.replayed)


__all__ = ["router"]
