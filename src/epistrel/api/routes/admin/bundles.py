"""Story exports (FR-STORE-7, FR-ADMIN-4; #249):

- ``POST /admin/stories/{story_id}/export``: queue an export (202, ``Location`` the job).
- ``GET /admin/stories/exports/{job_id}``: its state.
- ``GET /admin/stories/exports/{job_id}/bundle``: the archive, once ready.
- ``POST /admin/stories/import``: a bundle becomes a new story, through a background job (#250).
- ``GET /admin/stories/imports/{job_id}``: an import's state (#250).
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Path, Request, Response
from fastapi import status as http_status
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from epistrel.api.deps import ContextDep, EngineDep, StoryId
from epistrel.api.idempotency import answered
from epistrel.api.multipart import read_import
from epistrel.api.responses import DeploymentMutationResponse, MutationResponse
from epistrel.core.bundles import ExportJob, ExportStarted
from epistrel.core.bundles.import_job import ImportImpact, ImportJob, ImportMetadata, ImportStarted
from epistrel.core.errors import InvalidRequest
from epistrel.core.ids import ULID_PATTERN
from epistrel.core.profiles.impact import ImpactReport

router = APIRouter(prefix="/stories")

JobId = Annotated[str, Path(pattern=ULID_PATTERN, description="The export's id.")]
BUNDLE_MEDIA_TYPE = "application/gzip"


class ExportStartedBody(MutationResponse):
    """A queued export: its job id, the story and its status (``queued``)."""

    job_id: str
    story_id: str
    status: str

    @classmethod
    def of(cls, found: ExportStarted) -> ExportStartedBody:
        return cls(
            job_id=found.job_id,
            story_id=found.story_id,
            status=found.status,
            timeline_revision=found.timeline_revision,
        )


@router.get(
    "/exports/{job_id}",
    response_model=ExportJob,
    operation_id="export_status",
    summary="Read a story export",
    description=(
        "An export's state (#249): `status` (`queued`, `running`, `ready`, `failed`, "
        "`expired`), the revision it read (`exported_revision`), the archive's `manifest`, "
        "`size` and `sha256`, when it expires, and the `error` that ended a failed one "
        "(`secret_detected`: a configured secret was found in a member, and no file was kept). "
        "404 `job_not_found`."
    ),
)
async def export_status(job_id: JobId, ctx: ContextDep, engine: EngineDep) -> ExportJob:
    return await engine.export_status(ctx, job_id)


@router.get(
    "/exports/{job_id}/bundle",
    response_class=Response,
    operation_id="open_export_bundle",
    summary="Download a story export",
    description=(
        "The export's archive (#249): a gzip-compressed POSIX tar whose first member is "
        "`manifest.json` (format `1.1.0`, the source story, the revision, each file's SHA-256), "
        "then `records/story.json`, `events.jsonl` and `records/<table>.jsonl`. 409 "
        "`export_not_ready` before it is written; 410 `export_expired` once it expired."
    ),
    responses={
        http_status.HTTP_200_OK: {
            "description": "The archive.",
            "content": {BUNDLE_MEDIA_TYPE: {"schema": {"type": "string", "format": "binary"}}},
        }
    },
)
async def open_export_bundle(job_id: JobId, ctx: ContextDep, engine: EngineDep) -> Response:
    found = await engine.open_export_bundle(ctx, job_id)
    return Response(
        content=found.data,
        media_type=BUNDLE_MEDIA_TYPE,
        headers={
            "Content-Disposition": f'attachment; filename="{found.filename}"',
            "ETag": f'"{found.sha256}"',
            "Cache-Control": "private, no-store",
        },
    )


@router.post(
    "/{story_id}/export",
    status_code=http_status.HTTP_202_ACCEPTED,
    response_model=ExportStartedBody,
    operation_id="export_story",
    summary="Export a story as a bundle",
    description=(
        "FR-STORE-7, FR-ADMIN-4 (#249): a background job reads the story at one revision (one "
        "`REPEATABLE READ` snapshot: committed operations, every event in order, every "
        "durable story table; never projections, indexes, vectors, deployment-level records, "
        "in-flight work or secrets) and writes one checksummed archive. 202 with the job; poll "
        "`GET /admin/stories/exports/{job_id}`, then download its bundle. 404 for an unknown "
        "story."
    ),
)
async def export_story(
    story_id: StoryId, response: Response, ctx: ContextDep, engine: EngineDep
) -> ExportStartedBody | JSONResponse:
    found = await engine.export_story(ctx, story_id)
    shown = answered(ExportStartedBody.of(found), ctx, response, replayed=found.replayed)
    return JSONResponse(
        shown.model_dump(mode="json"),
        status_code=http_status.HTTP_202_ACCEPTED,
        headers={**dict(response.headers), "Location": f"/admin/stories/exports/{found.job_id}"},
    )


class ImportStartedBody(DeploymentMutationResponse):
    """An accepted import (#250): its job, the new story (``importing`` until the job ends)
    and its status."""

    job_id: str
    story_id: str
    status: str

    @classmethod
    def of(cls, found: ImportStarted) -> ImportStartedBody:
        return cls(
            job_id=found.job_id,
            story_id=found.story_id,
            status=found.status,
            config_revision=found.config_revision,
        )


class ImportImpactBody(DeploymentMutationResponse):
    """#251: an import whose impact report warns, without ``ignore_warnings``: nothing was
    created."""

    applied: bool = False
    impact: ImpactReport

    @classmethod
    def of(cls, found: ImportImpact) -> ImportImpactBody:
        return cls(applied=False, impact=found.impact, config_revision=found.config_revision)


def _metadata(raw: bytes) -> ImportMetadata:
    """The ``metadata`` part (422 ``validation_error`` at ``body.metadata.<field>``)."""
    try:
        return ImportMetadata.model_validate_json(raw or b"{}")
    except ValidationError as exc:
        errors = exc.errors()
        where = ".".join(str(p) for p in errors[0]["loc"]) if errors else ""
        raise InvalidRequest(
            "the import's metadata is invalid",
            {"field": "body.metadata" + (f".{where}" if where else "")},
        ) from exc


IMPORT_DESCRIPTION = (
    "FR-STORE-7, FR-ADMIN-4 (#250, #251): a `multipart/form-data` body: part `metadata` "
    "(JSON: `owner`, `name`, `content_rating_id`, `engine_profile_id`, `upgrade_mode`, "
    "`ignore_warnings`), then part `file`, the archive `GET /admin/stories/exports/{job_id}/"
    "bundle` gives. Checked up front, before anything is created: the upload's size "
    "(`bundle_max_uncompressed_bytes`), the manifest (the archive's first member), the format "
    "(`1.x`) and the rating (the one named, else the one whose name matches the bundle's: "
    "422 `rating_required` when none does); an impact report for the profile that warns, "
    "without `ignore_warnings`, is answered 200 `{applied: false, impact}` and nothing is "
    "created. Then 202: the story exists, `status: importing` (every route of it is 409 "
    "`story_importing` until its import ends), and a background job reads the archive under "
    "the limits (`bundle_max_entries`, `bundle_max_entry_bytes`, counted while it "
    "decompresses), checks every member against the manifest and its schema, inserts the rows "
    "with every id remapped, applies this deployment's erasure ledger, checks the whole active "
    "history against the rating's guardrails, parks every unsettled external effect "
    "(`imported_unsettled`), attaches the profile (a backfill when it gains data-producing "
    "capability, unless `upgrade_mode: forward_only`) and rebuilds the projections. Poll "
    "`GET /admin/stories/imports/{job_id}`. A refusal at any stage leaves no story. 413 "
    "`bundle_too_large`; 422 `corrupt_bundle` (`details.reason`), "
    "`unsupported_format_version`, `rating_required`, `validation_error`."
)


@router.post(
    "/import",
    status_code=http_status.HTTP_202_ACCEPTED,
    response_model=ImportStartedBody,
    operation_id="import_story",
    summary="Import a story bundle",
    description=IMPORT_DESCRIPTION,
    responses={
        http_status.HTTP_200_OK: {
            "model": ImportImpactBody,
            "description": "#251: the impact report warns; nothing was created.",
        }
    },
)
async def import_story(
    request: Request, response: Response, ctx: ContextDep, engine: EngineDep
) -> JSONResponse:
    from epistrel.core.config_changes import read_section
    from epistrel.core.settings_schema import RetentionSettings

    cap = (await read_section(engine.db, RetentionSettings)).bundle_max_uncompressed_bytes
    form = await read_import(request, cap=cap)
    found = await engine.import_story(ctx, form.data, _metadata(form.metadata))
    if isinstance(found, ImportImpact):
        shown_impact = answered(ImportImpactBody.of(found), ctx, response, replayed=found.replayed)
        return JSONResponse(
            shown_impact.model_dump(mode="json"),
            status_code=http_status.HTTP_200_OK,
            headers=dict(response.headers),
        )
    shown = answered(ImportStartedBody.of(found), ctx, response, replayed=found.replayed)
    return JSONResponse(
        shown.model_dump(mode="json"),
        status_code=http_status.HTTP_202_ACCEPTED,
        headers={**dict(response.headers), "Location": found.status_url},
    )


ImportJobId = Annotated[str, Path(pattern=ULID_PATTERN, description="The import's id.")]


@router.get(
    "/imports/{job_id}",
    response_model=ImportJob,
    operation_id="import_status",
    summary="Read a story import",
    description=(
        "An import's state (#250): `status` (`queued`, `running`, `ready`, `refused`, "
        "`failed`), the story (`null` once a refusal deleted it), the `stage` it is at "
        "(`reading`, `inserting`, `erasure`, `guardrails`, `outbox`, `profile`, `rebuilding`, "
        "`embedding`, `cleaning`) with its `progress`, and, once it ended, its `error` "
        "(`{code, message, details}`: `corrupt_bundle`, `bundle_too_large`, "
        "`unsupported_format_version`, `rating_check_failed`, `erased_subject_referenced`, "
        "`import_failed`) and `report` (`errors[]`, `warnings[]`, counts). 404 `job_not_found`."
    ),
)
async def import_status(job_id: ImportJobId, ctx: ContextDep, engine: EngineDep) -> ImportJob:
    return await engine.import_status(ctx, job_id)


__all__ = ["router"]
