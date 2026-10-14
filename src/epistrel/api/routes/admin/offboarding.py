"""``/admin/offboarding``: offboard a principal, type by type (FR-AUTH-7, FR-API-6; #117).

- ``POST /admin/offboarding``: ``{principal, actions, transfer_to?, dry_run?,
  ignore_warnings?}``. A dry run, or a request whose plan warns without ``ignore_warnings``,
  answers 200 ``{applied: false, counts, warnings}`` and changes nothing. Otherwise the
  offboarding is enqueued as a background job: 202 ``{applied: true, job_id, counts,
  warnings}`` with ``Location: /admin/offboarding/{job_id}``, stored under the request's
  ``Idempotency-Key``.
- ``GET /admin/offboarding/{job_id}``: the job's status, progress and request, and once it
  ends its report.

Every handler makes one ``Engine`` call. The write is deployment-scoped and answers the
deployment's unchanged ``config_revision``.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

from epistrel.api.deps import ContextDep, EngineDep, JobId
from epistrel.api.idempotency import answered
from epistrel.api.responses import DeploymentMutationResponse
from epistrel.core.offboarding import (
    OffboardingPlan,
    OffboardingQueued,
    OffboardingRequest,
    OffboardingView,
    OffboardingWarning,
)

OFFBOARDING_PATH = "/admin/offboarding/{}"

router = APIRouter(prefix="/offboarding")


class OffboardingWarningBody(BaseModel):
    """Something the offboarding would do that touches other principals: ``shared_story`` (a
    story to be deleted that others are related to), ``profile_in_use`` (a profile to be
    deleted that other owners' stories use), ``media_referenced`` (an asset to be retired that
    other owners' records reference) or ``default_profile`` (the deployment's default, which is
    never deleted), with the entity and the other principals."""

    model_config = ConfigDict(extra="forbid")

    kind: str
    entity_type: str
    entity_id: str
    principals: list[str]

    @classmethod
    def of(cls, warning: OffboardingWarning) -> OffboardingWarningBody:
        return cls(
            kind=warning.kind,
            entity_type=warning.entity_type,
            entity_id=warning.entity_id,
            principals=list(warning.principals),
        )


class OffboardingPlanBody(DeploymentMutationResponse):
    """A dry run, or a request its warnings held: nothing was written (``applied`` false). The
    counts per type (``delete``, ``transfer`` and ``keep``; for relations ``remove`` and
    ``keep``; a character's companion goes with it, and media moved by a transferred entry
    count as ``transfer``) and the warnings. Send it again with ``ignore_warnings`` to
    proceed."""

    applied: bool
    counts: dict[str, dict[str, int]]
    warnings: list[OffboardingWarningBody]

    @classmethod
    def of(cls, plan: OffboardingPlan) -> OffboardingPlanBody:
        return cls(
            applied=plan.applied,
            counts=plan.counts,
            warnings=[OffboardingWarningBody.of(w) for w in plan.warnings],
            config_revision=plan.config_revision,
        )


class OffboardingQueuedBody(DeploymentMutationResponse):
    """The offboarding a worker carries out (202): its job (read it at the ``Location``), and
    the counts and warnings it was confirmed over."""

    applied: bool
    job_id: str
    counts: dict[str, dict[str, int]]
    warnings: list[OffboardingWarningBody]

    @classmethod
    def of(cls, queued: OffboardingQueued) -> OffboardingQueuedBody:
        return cls(
            applied=queued.applied,
            job_id=queued.job_id,
            counts=queued.counts,
            warnings=[OffboardingWarningBody.of(w) for w in queued.warnings],
            config_revision=queued.config_revision,
        )


@router.post(
    "",
    response_model=OffboardingPlanBody,
    responses={
        202: {
            "model": OffboardingQueuedBody,
            "description": "Enqueued: a worker carries it out (read it at the `Location`).",
        }
    },
    operation_id="offboard_principal",
    summary="Offboard a principal",
    description=(
        "Apply an action to everything the principal owns, chosen per type (FR-AUTH-7): "
        "`delete`, `transfer` (to `transfer_to`) or `keep` for `stories`, `characters`, "
        "`scenarios`, `personas`, `media` and `engine_profiles`, and `remove` or `keep` for "
        "the `relations` naming it on entities it does not own. A type not named is `keep`. A "
        "character's companion scenario follows the character's action. Media a transferred "
        "catalog entry references move with it, whatever the media action. Deletion uses each "
        "type's own delete: a story is deleted for every participant, catalog and persona "
        "deletion never alters stories, and media and profiles are retired. The ids are taken "
        "now, so entities created for the principal later are not included. The plan warns of "
        "stories to be deleted that others are related to, profiles to be deleted that other "
        "owners' stories use, media to be retired that other owners' records reference, and "
        "the default profile, which is never deleted. A `dry_run`, or a plan with warnings "
        "and no `ignore_warnings`, answers 200 `{applied: false, counts, warnings}` and writes "
        "nothing. Otherwise a background job carries it out, one entity at a time, resumable "
        "and safe to repeat: 202 `{job_id, counts, warnings}` with `Location`, and a retry "
        "under the same `Idempotency-Key` answers the same job. 422 `principal_reserved` for "
        "`system`, `transfer_target_required` for a `transfer` without `transfer_to`, "
        "`transfer_target_invalid` for a `transfer_to` that is the principal or `system`; 409 "
        "`offboarding_in_progress` while the principal has one queued or running. Offboarding "
        "is not erasure: the principal stays recorded on events in stories it did not own."
    ),
)
async def offboard_principal(
    body: OffboardingRequest, response: Response, ctx: ContextDep, engine: EngineDep
) -> OffboardingPlanBody | JSONResponse:
    result = await engine.offboard_principal(ctx, body)
    if isinstance(result, OffboardingQueued):
        queued = answered(OffboardingQueuedBody.of(result), ctx, response, replayed=result.replayed)
        headers: dict[str, Any] = dict(response.headers)
        headers["Location"] = OFFBOARDING_PATH.format(result.job_id)
        return JSONResponse(queued.model_dump(mode="json"), status_code=202, headers=headers)
    return answered(OffboardingPlanBody.of(result), ctx, response, replayed=result.replayed)


@router.get(
    "/{job_id}",
    response_model=OffboardingView,
    operation_id="get_offboarding",
    summary="Read an offboarding's progress and report",
    description=(
        "The offboarding's `status` (`queued`, `running`, `done` or `failed`), `progress` "
        "(`{done, total}` of the entities taken), the entity in hand (`current`), the per-type "
        "`counts` and the `request`. Once it ends, the `report` lists, by type, every entity "
        "`deleted`, `transferred` (`{id, to}`; a companion with `companion_of`, media moved by "
        "a transferred entry with `reason: referenced`), `removed` (relations), `kept` and "
        "`skipped` (`{id, reason}`: `gone`, `not_owned`, `name_taken`, `default_profile` or "
        "`busy`), and `remaining`: the live entities still owned by or related to the "
        "principal. A failed job answers its partial report and `error`. The report is kept "
        "`offboarding_report_retention_days` (90) after the job ends."
    ),
)
async def get_offboarding(job_id: JobId, ctx: ContextDep, engine: EngineDep) -> OffboardingView:
    return await engine.get_offboarding(ctx, job_id)


__all__ = [
    "OffboardingPlanBody",
    "OffboardingQueuedBody",
    "OffboardingWarningBody",
    "router",
]
