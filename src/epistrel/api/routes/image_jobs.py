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
    "and a catalog job `candidate_ids`, never both, `[]` until it succeeds. `attempts` lists "
    "its provider attempts by start (FR-IMG-11): each one's token, `outcome`, the outputs it "
    "`requested`, and once settled its `used`, `overage` and `extra_outputs`, with its `cost`. "
    "A worker that stalls past its lease is fenced: no attempt starts before the stalled "
    "call's horizon (`waiting_for_horizon`), and a late result is counted and costed, never "
    "shown (`late_discarded`, `late_after_horizon`, `late_duplicate`). A worker lost mid-send "
    "leaves an ambiguous send: an adapter whose provider deduplicates by key re-sends it "
    "(`resent`); otherwise it is held to its horizon and counted as presumed (`presumed`), "
    "corrected once if its result arrives (`late_after_presumed`). With an adapter that "
    "confirms completion, the job asks the provider until `confirm_deadline_at`, then falls "
    "back to retrying (`confirm_fallback`). A job that failed on a policy also answers "
    "`policy_ids`. A story job "
    "is revalidated against its story before its image is attached (FR-IMG-4): it ends "
    "`target_gone` when the story was deleted, or `stale` when its source changed "
    "(`status_reason` `target_inactive`, `amended`, `erased`, `policy_changed` or "
    "`paused_too_long`, with `status_detail`). While its story is paused it stays `checking` "
    "with `waiting_on: pause` and `next_check_at`. Until an "
    "image has passed every check it has no media asset, URL or history entry: nothing a "
    "consumer can reach."
)


class AttemptBody(BaseModel):
    """One provider attempt (FR-IMG-11; #267)."""

    attempt_token: str
    outcome: str | None = Field(
        description="Null while in flight; then `promoted`, `rejected`, `failed`, `discarded`, "
        "`settled`, `adopted`, `cancelled`, `released_at_horizon`, `abandoned_fallback`, "
        "`late_discarded`, `late_after_horizon`, `presumed` (an ambiguous send counted as if it "
        "returned what it requested), `resent` (an idempotent re-send returned its result) or "
        "`late_after_presumed` (the actual result corrected a presumption once)."
    )
    send_state: Literal["prepared", "sending", "accepted", "returned", "ambiguous"] = Field(
        description="Where the send is: `ambiguous` when a worker was lost after the request "
        "was recorded as sending but before the provider's identifier or result was "
        "(FR-IMG-11): the provider may have generated, and may bill, for it."
    )
    basis: Literal["actual", "presumed", "corrected"] = Field(
        description="How `used` is known: `actual` outputs, `presumed` (an ambiguous send's "
        "requested outputs) or `corrected` once by the actual result."
    )
    resend_of: str | None = Field(
        description="For an idempotent re-send, the attempt whose request and key it sent "
        "again; its outputs count on that attempt (null otherwise)."
    )
    requested: int = Field(description="The outputs the attempt reserved.")
    used: int | None = Field(description="Outputs counted as usage; null while held.")
    overage: int | None = Field(
        description="Outputs counted past the reservation; null while held."
    )
    extra_outputs: int | None = Field(
        description="Outputs beyond what was requested, discarded unchecked; null while held."
    )
    cost: dict[str, str] | Literal["unknown"] | None = Field(
        description="`{amount, currency}` in the provider's currency, `unknown` when the "
        "provider gave none (never 0), or null before the attempt returned."
    )


class ImageJobBody(BaseModel):
    """An image job's state."""

    job_id: str
    kind: Literal["catalog", "story"]
    status: str
    status_reason: str | None
    asset_ids: list[str] | None = Field(default=None, exclude_if=lambda v: v is None)
    candidate_ids: list[str] | None = Field(default=None, exclude_if=lambda v: v is None)
    attempts: list[AttemptBody]
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
    waiting_for_horizon: bool = Field(
        default=False,
        description="A worker stalled past its lease with a provider call in flight: no attempt "
        "starts before that call's horizon (its timeout plus `image_attempt_grace_s`).",
    )
    confirm_deadline_at: datetime | None = Field(
        default=None,
        description="With an adapter that confirms completion: when the job stops asking the "
        "provider about a stalled call and retries (null otherwise).",
    )
    confirm_fallback: bool = Field(
        default=False,
        description="The job fell back to retrying with no definitive answer about a stalled "
        "call (sticky).",
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
            attempts=[
                AttemptBody(
                    attempt_token=a.attempt_token,
                    outcome=a.outcome,
                    requested=a.requested,
                    used=a.used,
                    overage=a.overage,
                    extra_outputs=a.extra_outputs,
                    cost=a.cost,
                    send_state=a.send_state,
                    basis=a.basis,
                    resend_of=a.resend_of,
                )
                for a in view.attempts
            ],
            created_at=view.created_at,
            updated_at=view.updated_at,
            policy_ids=None if view.policy_ids is None else list(view.policy_ids),
            status_detail=view.status_detail,
            waiting_on="pause" if view.waiting_on == "pause" else None,
            next_check_at=view.next_check_at,
            waiting_for_horizon=view.waiting_for_horizon,
            confirm_deadline_at=view.confirm_deadline_at,
            confirm_fallback=view.confirm_fallback,
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
