"""Certifications (FR-PROF-15..17, NFR-QUAL-8,11; #233).

- ``POST /engine-profiles/{id}/certification-runs`` and ``POST /stories/{id}/certification-runs``:
  estimate a certification run of a profile version or a story's effective configuration, or
  with ``confirm`` queue it (202 ``{run_id, job_id}``).
- ``GET /certification-runs/{id}``: a run's status, estimate and report summary.
- ``GET /certifications`` and ``GET /certifications/{id}``: the certification records.
- ``POST /admin/certifications/import``: import a hand run's record (``admin_router``).

Every handler makes one ``Engine`` call (``core.profiles.certification`` holds the rules, and
``orchestrator.jobs.handlers.certification_run`` the run).
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Body, Path, Query, Response
from fastapi.responses import JSONResponse
from pydantic import Field

from epistrel.api.deps import ContextDep, EngineDep, StoryId
from epistrel.api.idempotency import answered
from epistrel.api.responses import DeploymentMutationResponse
from epistrel.api.routes.engine_profiles import PROFILES_TAG
from epistrel.core.ids import ULID_PATTERN
from epistrel.core.profiles.certification import (
    Certification,
    CertificationImported,
    CertificationPage,
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


def _answer(
    found: CertificationRunAnswer, ctx: ContextDep, response: Response
) -> CertificationRunBody | JSONResponse:
    body = answered(CertificationRunBody.of(found), ctx, response, replayed=found.replayed)
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
    return _answer(found, ctx, response)


@router.post(
    "/stories/{story_id}/certification-runs",
    response_model=CertificationRunBody,
    responses={202: {"model": CertificationRunBody, "description": "The run is queued."}},
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
) -> CertificationRunBody | JSONResponse:
    found = await engine.request_certification_run(ctx, body, story_id=story_id)
    return _answer(found, ctx, response)


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
    response_model=CertificationPage,
    operation_id="list_certifications",
    summary="List certifications",
    description=(
        "Certification records (FR-PROF-15), newest first, stale ones included, narrowed by "
        "`fingerprint` and `profile_id` when given (both: ANDed). `cursor` continues a page "
        "from its `next_cursor`."
    ),
)
async def list_certifications(
    ctx: ContextDep,
    engine: EngineDep,
    fingerprint: Annotated[
        str | None, Query(pattern=FINGERPRINT_PATTERN, description="A configuration fingerprint.")
    ] = None,
    profile_id: Annotated[
        str | None, Query(pattern=ULID_PATTERN, description="A profile's id.")
    ] = None,
    limit: Annotated[int, Query(ge=1, le=200, description="At most this many.")] = 50,
    cursor: Annotated[
        str | None, Query(pattern=ULID_PATTERN, description="The previous page's next_cursor.")
    ] = None,
) -> CertificationPage:
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


__all__ = ["admin_router", "router"]
