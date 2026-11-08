"""Certifications (FR-PROF-15..17, NFR-QUAL-8,11; #233).

- ``POST /engine-profiles/{id}/certification-runs`` and ``POST /stories/{id}/certification-runs``:
  estimate a certification run of a profile version or a story's effective configuration, or
  with ``confirm`` queue it (202 ``{run_id, job_id}``).
- ``GET /certification-runs/{id}``: a run's status, estimate and report summary.
- ``GET /certifications`` and ``GET /certifications/{id}``: the certification records.
- ``POST /admin/certifications/import``: import a hand run's record (``admin_router``).
- ``POST /admin/certifications/{id}/invalidate``: declare a certification stale (#234).
- ``POST /admin/certifications/image-check`` and ``POST
  /admin/certifications/image-check/{id}/invalidate``: record and withdraw an image checker's
  certification for one policy (NFR-QUAL-12; #270, ADR-182); ``GET
  /certifications?subject=image_check`` lists them.

Every handler makes one ``Engine`` call (``core.profiles.certification`` holds the rules, and
``orchestrator.jobs.handlers.certification_run`` the run).
"""

from __future__ import annotations

from typing import Annotated, Any, Literal, cast

from fastapi import APIRouter, Body, Path, Query, Response
from fastapi.responses import JSONResponse
from pydantic import Field

from epistrel.api.deps import ContextDep, EngineDep, StoryId
from epistrel.api.idempotency import answered
from epistrel.api.responses import DeploymentMutationResponse, MutationResponse
from epistrel.api.routes.engine_profiles import PROFILES_TAG
from epistrel.core.ids import ULID_PATTERN
from epistrel.core.profiles.certification import (
    Certification,
    CertificationImported,
    CertificationInvalidated,
    CertificationPage,
    EvalRun,
    EvalRunPage,
    ImageCheckCertification,
    ImageCheckCertificationPage,
    ImageCheckCertificationRequest,
    ImageCheckInvalidated,
    ImageCheckRecorded,
    InvalidateRequest,
    RunEstimate,
)
from epistrel.core.profiles.fingerprint import FINGERPRINT_PATTERN
from epistrel.orchestrator.jobs.handlers.certification_run import (
    CertificationRun,
    CertificationRunAnswer,
    CertificationRunRequest,
)

router = APIRouter(tags=[PROFILES_TAG])
admin_router = APIRouter(prefix="/certifications")
evals_router = APIRouter(prefix="/evals")

_ULID_EXAMPLE = "01J9Z8Q4N5V6W7X8Y9Z0A1B2C3"
ProfileId = Annotated[
    str, Path(pattern=ULID_PATTERN, description="The profile's id.", examples=[_ULID_EXAMPLE])
]
RunId = Annotated[
    str,
    Path(pattern=ULID_PATTERN, description="The certification run's id.", examples=[_ULID_EXAMPLE]),
]
CertificationId = Annotated[
    str,
    Path(pattern=ULID_PATTERN, description="The certification's id.", examples=[_ULID_EXAMPLE]),
]

_RUN_RULES = (
    "Without `confirm`, the answer is the estimate (`model_calls`, `tokens`, `duration_s`; "
    "`cost` only when the model matrix prices tokens), the configuration's `fingerprint` and "
    "the `suites` the run would play, and nothing is written. With `confirm: true` a "
    "`certification_run` job is queued: 202 `{run_id, job_id}` with the estimate repeated "
    "(409 `certification_run_in_progress` while a run for the same fingerprint is queued or "
    "running). `suites` defaults to every suite the configuration enables; naming one it does "
    "not enable, or an unknown one, is 422 `validation_error`. `seeds` is 1-5 (default 3) and "
    "`sample` a per-suite case cap of at least 10 (under 300 cases a zero-tolerance floor is "
    "`insufficient_evidence`, NFR-QUAL-8). The run plays the suites on scratch stories with "
    "the deployment's models and records a certification when a floor passes; it never "
    "changes the configuration. The fingerprint is captured now: a later change does not "
    "affect the run. 503 `certification_unavailable` when the process has no eval harness."
)


class CertificationRunBody(DeploymentMutationResponse):
    """A run request's answer: the estimate, fingerprint and suites, and with ``confirm`` the
    run and its job."""

    estimate: RunEstimate
    fingerprint: str
    suites: list[str]
    run_id: str | None = Field(default=None, exclude_if=lambda value: value is None)
    job_id: str | None = Field(default=None, exclude_if=lambda value: value is None)

    @classmethod
    def of(cls, found: CertificationRunAnswer) -> CertificationRunBody:
        return cls(
            estimate=found.estimate,
            fingerprint=found.fingerprint,
            suites=list(found.suites),
            run_id=found.run_id,
            job_id=found.job_id,
            config_revision=found.config_revision,
        )


class StoryCertificationRunBody(MutationResponse):
    """A story's run request's answer (#233): as ``CertificationRunBody``, with the story's
    ``timeline_revision`` (unchanged) beside the deployment's ``config_revision``."""

    estimate: RunEstimate
    fingerprint: str
    suites: list[str]
    config_revision: int
    run_id: str | None = Field(default=None, exclude_if=lambda value: value is None)
    job_id: str | None = Field(default=None, exclude_if=lambda value: value is None)

    @classmethod
    def of(cls, found: CertificationRunAnswer) -> StoryCertificationRunBody:
        return cls(
            estimate=found.estimate,
            fingerprint=found.fingerprint,
            suites=list(found.suites),
            run_id=found.run_id,
            job_id=found.job_id,
            config_revision=found.config_revision,
            timeline_revision=found.timeline_revision or 0,
        )


def _answer(
    found: CertificationRunAnswer, ctx: ContextDep, response: Response
) -> CertificationRunBody | StoryCertificationRunBody | JSONResponse:
    shown = (
        StoryCertificationRunBody.of(found)
        if found.timeline_revision is not None
        else CertificationRunBody.of(found)
    )
    body = answered(shown, ctx, response, replayed=found.replayed)
    if found.run_id is None:
        return body
    return JSONResponse(
        body.model_dump(mode="json"),
        status_code=202,
        headers=dict(response.headers),
    )


@router.post(
    "/engine-profiles/{profile_id}/certification-runs",
    response_model=CertificationRunBody,
    responses={202: {"model": CertificationRunBody, "description": "The run is queued."}},
    operation_id="request_certification_run_profile",
    summary="Certify an engine profile version",
    description=(
        "A certification run of the profile at its latest version (`version`: that one) under "
        "the deployment's models and no story overrides (FR-PROF-17). " + _RUN_RULES
    ),
)
async def request_certification_run_profile(
    profile_id: ProfileId,
    body: CertificationRunRequest,
    response: Response,
    ctx: ContextDep,
    engine: EngineDep,
) -> CertificationRunBody | JSONResponse:
    found = await engine.request_certification_run(ctx, body, profile_id=profile_id)
    return cast(CertificationRunBody | JSONResponse, _answer(found, ctx, response))


@router.post(
    "/stories/{story_id}/certification-runs",
    response_model=StoryCertificationRunBody,
    responses={202: {"model": StoryCertificationRunBody, "description": "The run is queued."}},
    operation_id="request_certification_run_story",
    summary="Certify a story's effective configuration",
    description=(
        "A certification run of the story's effective configuration: its profile version with "
        "its overrides, under the deployment's models (FR-PROF-17; `version` is ignored). "
        + _RUN_RULES
    ),
)
async def request_certification_run_story(
    story_id: StoryId,
    body: CertificationRunRequest,
    response: Response,
    ctx: ContextDep,
    engine: EngineDep,
) -> StoryCertificationRunBody | JSONResponse:
    found = await engine.request_certification_run(ctx, body, story_id=story_id)
    return cast(StoryCertificationRunBody | JSONResponse, _answer(found, ctx, response))


@router.get(
    "/certification-runs/{run_id}",
    response_model=CertificationRun,
    operation_id="get_certification_run",
    summary="Read a certification run",
    description=(
        "A certification run: what it certifies (fingerprint, profile version, story) and runs, "
        "its `status` (`queued`, `running`, `completed`, `failed`), its estimate, its `report` "
        "summary (per suite and per floor: passed, failed, insufficient) once it ran, and the "
        "`certification_id` it recorded (null when no floor passed or the run failed). 404 "
        "`certification_run_not_found`."
    ),
)
async def get_certification_run(
    run_id: RunId, ctx: ContextDep, engine: EngineDep
) -> CertificationRun:
    return await engine.get_certification_run(ctx, run_id)


@router.get(
    "/certifications",
    response_model=CertificationPage | ImageCheckCertificationPage,
    operation_id="list_certifications",
    summary="List certifications",
    description=(
        "Certification records (FR-PROF-15), newest first, stale ones included, narrowed by "
        "`fingerprint` and `profile_id` when given (both: ANDed). `cursor` continues a page "
        "from its `next_cursor`. With `subject=image_check` (#270): the image checkers' "
        "certifications instead (NFR-QUAL-12), narrowed by `checker_fingerprint` and "
        "`policy_id`, each with its evidence (`eval_run` or, for the minors policy only, "
        "`external_attestation`) and status."
    ),
)
async def list_certifications(
    ctx: ContextDep,
    engine: EngineDep,
    subject: Annotated[
        Literal["profile", "image_check"],
        Query(description="Which certifications: configurations' (default) or image checkers'."),
    ] = "profile",
    fingerprint: Annotated[
        str | None, Query(pattern=FINGERPRINT_PATTERN, description="A configuration fingerprint.")
    ] = None,
    profile_id: Annotated[
        str | None, Query(pattern=ULID_PATTERN, description="A profile's id.")
    ] = None,
    checker_fingerprint: Annotated[
        str | None,
        Query(pattern=r"^[0-9a-f]{12}$", description="An image checker's fingerprint."),
    ] = None,
    policy_id: Annotated[
        str | None, Query(min_length=1, max_length=200, description="An image-output policy.")
    ] = None,
    limit: Annotated[int, Query(ge=1, le=200, description="At most this many.")] = 50,
    cursor: Annotated[
        str | None, Query(pattern=ULID_PATTERN, description="The previous page's next_cursor.")
    ] = None,
) -> CertificationPage | ImageCheckCertificationPage:
    if subject == "image_check":
        return await engine.list_image_check_certifications(
            ctx,
            checker_fingerprint=checker_fingerprint,
            policy_id=policy_id,
            limit=limit,
            cursor=cursor,
        )
    return await engine.list_certifications(
        ctx, fingerprint=fingerprint, profile_id=profile_id, limit=limit, cursor=cursor
    )


@router.get(
    "/certifications/{certification_id}",
    response_model=Certification,
    operation_id="get_certification",
    summary="Read a certification",
    description=(
        "One certification record (FR-PROF-15): the fingerprint and models it certifies, the "
        "floors it passed, failed and could not decide (each with its rule, mean, standard "
        "deviation, sample size, seeds and bound), the intervention types NFR-QUAL-14's floor "
        "covers and lacks evidence for, whether its judge was calibrated, its model tier, host "
        "and report hash, how it was recorded and its status. 404 `certification_not_found`."
    ),
)
async def get_certification(
    certification_id: CertificationId, ctx: ContextDep, engine: EngineDep
) -> Certification:
    return await engine.get_certification(ctx, certification_id)


class CertificationImportedBody(DeploymentMutationResponse):
    """An imported record: its id, and whether a row was written (``false``: stored already)."""

    certification_id: str
    stored: bool

    @classmethod
    def of(cls, found: CertificationImported) -> CertificationImportedBody:
        return cls(
            certification_id=found.certification_id,
            stored=found.stored,
            config_revision=found.config_revision,
        )


@admin_router.post(
    "/import",
    response_model=CertificationImportedBody,
    operation_id="import_certification",
    summary="Import a certification record",
    description=(
        "Store a certification record as `epistrel evals` writes it (a hand run's, #235): "
        "`schema_version` 1, the record's fields and its Markdown report in `report_md`. The "
        "record is validated (extra fields refused) and the report's SHA-256 (LF line ends, "
        "one trailing newline) must equal its `report_sha256`, else 422 "
        "`invalid_certification_record` and nothing is stored. It is stored by its "
        "`certification_id`: the same record again stores nothing (`stored: false`), and other "
        "content under that id is 409 `certification_record_conflict`. A record whose "
        "fingerprint matches no profile version is stored all the same."
    ),
)
async def import_certification(
    document: Annotated[dict[str, Any], Body(description="The certification record.")],
    response: Response,
    ctx: ContextDep,
    engine: EngineDep,
) -> CertificationImportedBody:
    found = await engine.import_certification(ctx, document)
    return answered(CertificationImportedBody.of(found), ctx, response, replayed=found.replayed)


class CertificationInvalidatedBody(DeploymentMutationResponse):
    """An invalidated certification: its id, its status (``stale``) and why."""

    certification_id: str
    status: str
    invalidated_reason: str

    @classmethod
    def of(cls, found: CertificationInvalidated) -> CertificationInvalidatedBody:
        return cls(
            certification_id=found.certification_id,
            status=found.status,
            invalidated_reason=found.invalidated_reason,
            config_revision=found.config_revision,
        )


@admin_router.post(
    "/{certification_id}/invalidate",
    response_model=CertificationInvalidatedBody,
    operation_id="invalidate_certification",
    summary="Declare a certification stale",
    description=(
        "Mark a `valid` certification `stale` (#234; ADR-107): the model changed under an "
        "unchanged identifier and reported version (`reason: drift`), or the operator withdraws "
        "it (`manual`). A `note` is required. Stories on its fingerprint report "
        "`{status: stale, certification_id, invalidated_reason}` from their next turn; the "
        "record is kept, and a later certification run restores `certified` with a new id. 404 "
        "`certification_not_found`; 409 `certification_not_valid` for one already stale or "
        "revoked."
    ),
)
async def invalidate_certification(
    certification_id: CertificationId,
    body: InvalidateRequest,
    response: Response,
    ctx: ContextDep,
    engine: EngineDep,
) -> CertificationInvalidatedBody:
    found = await engine.invalidate_certification(ctx, certification_id, body)
    return answered(CertificationInvalidatedBody.of(found), ctx, response, replayed=found.replayed)


class ImageCheckCertificationBody(DeploymentMutationResponse):
    """An image checker's certification record, as recorded or withdrawn (#270); ``stored``:
    whether this request wrote it (``false``: the same evidence was recorded before)."""

    certification: ImageCheckCertification
    stored: bool | None = Field(default=None, exclude_if=lambda value: value is None)
    changed: bool | None = Field(default=None, exclude_if=lambda value: value is None)

    @classmethod
    def recorded(cls, found: ImageCheckRecorded) -> ImageCheckCertificationBody:
        return cls(
            certification=found.certification,
            stored=found.stored,
            config_revision=found.config_revision,
        )

    @classmethod
    def invalidated(cls, found: ImageCheckInvalidated) -> ImageCheckCertificationBody:
        return cls(
            certification=found.certification,
            changed=found.changed,
            config_revision=found.config_revision,
        )


_IMAGE_CHECK_RULES = (
    "An image checker (its `checker_fingerprint`: 12 hex of its endpoint host, model, reported "
    "version and `image_check` template) is certified per image-output policy (NFR-QUAL-12, "
    "ADR-182). `evidence_kind: eval_run` takes one policy's row of a committed E-11 report "
    "(`report_path`, `report_sha256`, `checker_fingerprint`, `policy_id`, `policy_version`, "
    "`verdict`, `fn`, `fp`, `floors`, `uncertifiable`): it certifies only a report that passed "
    "both its false-negative and false-positive floors on reviewed labels (else 422 "
    "`invalid_evidence`), and the report is re-hashed when its path names a file here. "
    "`evidence_kind: external_attestation` takes the checker vendor's published evaluation "
    "(`title`, an https `url`, never fetched, `publisher`, a `publication_date` not in the "
    "future and the quoted `figure {metric, value 0-1, n?}`); it is accepted only for a policy "
    "flagged `external_evidence_only`, which only the seeded minors policy is (else 422 "
    "`attestation_not_allowed`), and an `eval_run` is refused for that policy (422 "
    "`eval_run_not_allowed`): Epistrel never measures its false negatives. The newest valid "
    "record for a checker and policy decides `certified` on the images checked afterwards; "
    "images checked before keep the flag they were checked with."
)


@admin_router.post(
    "/image-check",
    response_model=ImageCheckCertificationBody,
    status_code=201,
    responses={200: {"model": ImageCheckCertificationBody, "description": "Recorded before."}},
    operation_id="record_image_check_certification",
    summary="Record an image checker's certification",
    description=(
        "Record an image checker's certification for one policy: 201 with the record, or 200 "
        "with the stored one when the same evidence (checker, policy, kind and evidence, never "
        "who or when) was recorded before, whatever its status. " + _IMAGE_CHECK_RULES
    ),
)
async def record_image_check_certification(
    body: ImageCheckCertificationRequest,
    response: Response,
    ctx: ContextDep,
    engine: EngineDep,
) -> ImageCheckCertificationBody:
    found = await engine.record_image_check_certification(ctx, body)
    response.status_code = 201 if found.stored else 200
    return answered(
        ImageCheckCertificationBody.recorded(found), ctx, response, replayed=found.replayed
    )


@admin_router.post(
    "/image-check/{certification_id}/invalidate",
    response_model=ImageCheckCertificationBody,
    operation_id="invalidate_image_check_certification",
    summary="Withdraw an image checker's certification",
    description=(
        "Mark an image checker's certification `stale` (`reason: drift` or `manual`, a `note` "
        "required and never stored), with `invalidated_at`, `invalidated_by` and "
        "`invalidated_reason`. The policy then reads `certified: false` for that checker on "
        "images checked afterwards, unless another valid record certifies it. One already "
        "stale answers unchanged (`changed: false`). 404 `certification_not_found`."
    ),
)
async def invalidate_image_check_certification(
    certification_id: CertificationId,
    body: InvalidateRequest,
    response: Response,
    ctx: ContextDep,
    engine: EngineDep,
) -> ImageCheckCertificationBody:
    found = await engine.invalidate_image_check_certification(ctx, certification_id, body)
    return answered(
        ImageCheckCertificationBody.invalidated(found), ctx, response, replayed=found.replayed
    )


@evals_router.get(
    "/runs",
    response_model=EvalRunPage,
    operation_id="list_eval_runs",
    summary="List eval runs",
    description=(
        "Eval runs, newest first (#532): the reports committed under `docs/evals` and every "
        "completed certification run's report (#233), the same sources certifications are "
        "recorded from. Each run carries its date, model, version and fingerprint, preset, host "
        "kind, verdicts per suite and per NFR-QUAL requirement, and (in a report written since "
        "#532) each metric's value, floor, sample size and verdict, its seeds and sample size. "
        "`cursor` continues a page from its `next_cursor`."
    ),
)
async def list_eval_runs(
    ctx: ContextDep,
    engine: EngineDep,
    limit: Annotated[int, Query(ge=1, le=200, description="At most this many.")] = 50,
    cursor: Annotated[
        str | None, Query(max_length=200, description="The previous page's next_cursor.")
    ] = None,
) -> EvalRunPage:
    return await engine.list_eval_runs(ctx, limit=limit, cursor=cursor)


@evals_router.get(
    "/runs/{run_id}",
    response_model=EvalRun,
    operation_id="get_eval_run",
    summary="Read an eval run",
    description="One eval run (#532), as the list serves it. 404 `eval_run_not_found`.",
)
async def get_eval_run(
    run_id: Annotated[str, Path(max_length=200, description="The run's id.")],
    ctx: ContextDep,
    engine: EngineDep,
) -> EvalRun:
    return await engine.get_eval_run(ctx, run_id)


__all__ = ["admin_router", "evals_router", "router"]
