"""The reconciliation status of a story's external effects (FR-EXT-10,14,18,22; #210, #218).

- ``GET /stories/{id}/reconciliation``: every effect dispatched to a write-capable provider that
  Epistrel cannot report as applied (``core.outbox.listings``).
- ``POST /stories/{id}/reconciliation/{outbox_id}/settle-now``: the author settles a listed
  late success, discarded-origin confirmation or settlement conflict now, re-checked at that
  moment (#218).
- ``POST /stories/{id}/reconciliation/{outbox_id}/compensate``: the author issues a
  compensating external effect (a refund) for it through the outbox (#218).

Every handler makes one ``Engine`` call (``orchestrator.jobs.settlement`` holds the rules).
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Path, Response
from fastapi import status as http_status

from epistrel.api.deps import ContextDep, EngineDep, StoryId
from epistrel.api.idempotency import answered
from epistrel.api.responses import MutationResponse
from epistrel.core.ids import ULID_PATTERN
from epistrel.core.outbox import CompensateRequest, Reconciliation
from epistrel.orchestrator.jobs.settlement import Compensated, SettledNow

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


__all__ = ["router"]
