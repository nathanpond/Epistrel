"""``GET`` and ``DELETE /image-jobs/{job_id}``: follow and cancel an image job (FR-IMG-3,4;
#256).

The generation routes (#260's catalog candidates, #263's story images) answer 202 ``{job_id,
status: queued}`` with ``Location: /image-jobs/{id}``; a consumer follows the job here with a
long-polling read (``?wait=``; no webhook or push). Both need only the service credential:
ownership gates nothing. Thin handlers over ``Engine.get_image_job`` and
``Engine.cancel_image_job``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

from epistrel.api.deps import ContextDep, EngineDep, JobId, route_flags
from epistrel.orchestrator.images.jobs import (
    WAIT_MAX_S,
    WAIT_MIN_S,
    ImageJobView,
)

router = APIRouter(prefix="/image-jobs", tags=["images"])

STATUS_TEXT = (
    "`status` is `queued`, `generating`, `checking`, `promoted` (a story job), "
    "`candidates_ready` (a catalog job), `failed`, `stale`, `target_gone`, `cap_reached` or "
    "`cancelled`. `status_reason` is a bare code (`guardrail_violation`, "
    "`image_check_unconfigured`, `image_check_unavailable`, `provider_unavailable`, "
    "`provider_empty_result`, `adapter_auth`, `adapter_credits`, `request_invalid`, …) and "
    "null for `promoted`, `candidates_ready` and `cancelled`. A story job answers `asset_ids` "
    "and a catalog job `candidate_ids`, never both, `[]` until it succeeds. `attempts` counts "
    "its provider attempts. A job that failed on a policy also answers `policy_ids`. A story job "
    "is revalidated against its story before its image is attached (FR-IMG-4): it ends "
    "`target_gone` when the story was deleted, or `stale` when its source changed "
    "(`status_reason` `target_inactive`, `amended`, `erased`, `policy_changed` or "
    "`paused_too_long`, with `status_detail`). While its story is paused it stays `checking` "
    "with `waiting_on: pause` and `next_check_at`. Until an "
    "image has passed every check it has no media asset, URL or history entry: nothing a "
    "consumer can reach."
)


class ImageJobBody(BaseModel):
    """An image job's state."""

    job_id: str
    kind: Literal["catalog", "story"]
    status: str
    status_reason: str | None
    asset_ids: list[str] | None = Field(default=None, exclude_if=lambda v: v is None)
    candidate_ids: list[str] | None = Field(default=None, exclude_if=lambda v: v is None)
    attempts: int
    created_at: datetime
    updated_at: datetime
    policy_ids: list[str] | None = Field(
        default=None,
        exclude_if=lambda v: v is None,
        description="The policies a failed job's outputs violated or that could not be "
        "evaluated (absent otherwise).",
    )
    status_detail: dict[str, Any] | None = Field(
        default=None,
        exclude_if=lambda v: v is None,
        description="A `cap_reached` job's `{scope, period}`: the limit scope (`deployment` or "
        "`entry`) its retry could not reserve against and the local date (FR-IMG-11). A "
        "`stale` job's `{cause, policy_ids, amendment_id}`, as its reason has them: `cause` "
        "`truncated`, `swapped`, `regenerated` or `advanced` for `target_inactive`, the "
        "amendment for `amended`, the policies for `policy_changed` (FR-IMG-4). Absent "
        "otherwise; never a name.",
    )
    waiting_on: Literal["pause"] | None = Field(
        default=None,
        exclude_if=lambda v: v is None,
        description="`pause` while a `checking` story job waits on its paused story (a rating, "
        "reactivation or backfill check): its staged image and reservation are kept, and it "
        "revalidates at `next_check_at` (absent otherwise; FR-IMG-4).",
    )
    next_check_at: datetime | None = Field(
        default=None,
        exclude_if=lambda v: v is None,
        description="When a waiting job revalidates next (absent otherwise).",
    )

    @classmethod
    def of(cls, view: ImageJobView) -> ImageJobBody:
        ids = list(view.result_ids)
        return cls(
            job_id=view.job_id,
            kind="catalog" if view.kind == "catalog" else "story",
            status=view.status,
            status_reason=view.status_reason,
            asset_ids=ids if view.result_field == "asset_ids" else None,
            candidate_ids=ids if view.result_field == "candidate_ids" else None,
            attempts=view.attempts,
            created_at=view.created_at,
            updated_at=view.updated_at,
            policy_ids=None if view.policy_ids is None else list(view.policy_ids),
            status_detail=view.status_detail,
            waiting_on="pause" if view.waiting_on == "pause" else None,
            next_check_at=view.next_check_at,
        )


@router.get(
    "/{job_id}",
    response_model=ImageJobBody,
    operation_id="get_image_job",
    summary="Read an image job (long-polling with ?wait=)",
    description=(
        "An image job's state. "
        + STATUS_TEXT
        + f"\n\nWith `wait` ({WAIT_MIN_S} to {WAIT_MAX_S} seconds) the read is a long poll: it "
        "answers as soon as the status differs from `since_status` (default: the status when "
        "the read began), or when the wait ends, with the current state either way (200). A "
        "notification wakes it; it also looks once a second, so a missed one never makes it "
        "wait longer. 422 `validation_error` for a `wait` outside the range or an unknown "
        "`since_status`; 503 `too_many_waiters` when this process already holds 200 long "
        "polls; 404 `image_job_not_found`."
    ),
)
async def get_image_job(
    job_id: JobId,
    ctx: ContextDep,
    engine: EngineDep,
    wait: Annotated[int | None, Query(description="Seconds to wait, 1 to 30.")] = None,
    since_status: Annotated[
        str | None, Query(description="Answer once the status differs from this one.")
    ] = None,
) -> ImageJobBody:
    return ImageJobBody.of(
        await engine.get_image_job(ctx, job_id, wait=wait, since_status=since_status)
    )


@router.delete(
    "/{job_id}",
    response_model=ImageJobBody,
    operation_id="cancel_image_job",
    summary="Cancel an image job",
    description=(
        "Cancels a queued or running image job: it ends `cancelled`, a worker running it is "
        "fenced and promotes nothing, and its staged bytes are deleted. A job that already "
        "ended answers 200 with its state unchanged, so the cancel is idempotent by design and "
        "takes no `Idempotency-Key`. 404 `image_job_not_found`. " + STATUS_TEXT
    ),
    **route_flags(mutating=False),
)
async def cancel_image_job(job_id: JobId, ctx: ContextDep, engine: EngineDep) -> ImageJobBody:
    return ImageJobBody.of(await engine.cancel_image_job(ctx, job_id))
