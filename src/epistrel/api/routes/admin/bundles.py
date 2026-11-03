"""Story exports (FR-STORE-7, FR-ADMIN-4; #249):

- ``POST /admin/stories/{story_id}/export``: queue an export (202, ``Location`` the job).
- ``GET /admin/stories/exports/{job_id}``: its state.
- ``GET /admin/stories/exports/{job_id}/bundle``: the archive, once ready.
- ``POST /admin/stories/import``: a bundle (the request body) becomes a new story (#250).
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Path, Request, Response
from fastapi import status as http_status
from fastapi.responses import JSONResponse
from pydantic import Field

from epistrel.api.deps import ContextDep, EngineDep, StoryId
from epistrel.api.idempotency import answered
from epistrel.api.responses import DeploymentMutationResponse, MutationResponse
from epistrel.core.bundles import ExportJob, ExportStarted
from epistrel.core.bundles.importing import Imported, ImportRequest
from epistrel.core.errors import BundleTooLarge
from epistrel.core.ids import ULID_PATTERN

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
        "`manifest.json` (format `1.0.0`, the source story, the revision, each file's SHA-256), "
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


class ImportedBody(DeploymentMutationResponse):
    """An imported story: its new id, the bundle's source story and revision, and the rows
    written per table."""

    story_id: str
    source_story_id: str
    exported_revision: int
    counts: dict[str, int]
    operation_id: str | None = Field(
        default=None,
        description="#705: the story's `import` operation (its `turn_report.imported_from`: "
        "the source story, the exported revision, the format version and the bundle's "
        "SHA-256).",
    )
    warnings: list[dict[str, Any]] = Field(
        default_factory=list,
        description="#705: what a bundle of a newer format MINOR carried that this deployment "
        "ignored (`unknown_member`, `unknown_fields`), or `media_not_imported`.",
    )

    @classmethod
    def of(cls, found: Imported) -> ImportedBody:
        return cls(
            story_id=found.story_id,
            source_story_id=found.source_story_id,
            exported_revision=found.exported_revision,
            counts=found.counts,
            config_revision=found.config_revision,
            operation_id=found.operation_id,
            warnings=list(found.warnings),
        )


MAX_COMPRESSED_BYTES = 256 * 1024 * 1024


async def _archive(request: Request) -> bytes:
    body = bytearray()
    async for piece in request.stream():
        body += piece
        if len(body) > MAX_COMPRESSED_BYTES:
            raise BundleTooLarge(
                "the bundle is too large",
                {"limit": "bundle_max_bytes", "value": MAX_COMPRESSED_BYTES},
            )
    return bytes(body)


@router.post(
    "/import",
    status_code=http_status.HTTP_201_CREATED,
    response_model=ImportedBody,
    operation_id="import_story",
    summary="Import a story bundle",
    description=(
        "FR-STORE-7, FR-ADMIN-4 (#250): the raw request body is a bundle, the gzip-compressed "
        "archive `GET /admin/stories/exports/{job_id}/bundle` gives. It is read under limits, "
        "checked against its manifest (format `1.x`, every member's SHA-256), every id it owns "
        "remapped, every line checked against its schema (`components.schemas` "
        "`StoryBundle*`) and every event payload against its type, the members in the export's "
        "order, and written as a new story in one transaction, owned by `owner` (default: "
        "the caller) under `engine_profile_id` (default: its bundle profile when this "
        "deployment has it, else the default profile); projections and the subject index are "
        "rebuilt, its media written as new assets of the owner, its revision the exported one "
        "plus one (an `import` operation records `imported_from`), and the embed job queued. "
        "201 with the new story. 413 `bundle_too_large`; 422 `corrupt_bundle` "
        "(`details.reason`) or `unsupported_format_version`. A refusal writes nothing."
    ),
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                "application/octet-stream": {"schema": {"type": "string", "format": "binary"}}
            },
        }
    },
)
async def import_story(
    response: Response,
    ctx: ContextDep,
    engine: EngineDep,
    choices: Annotated[ImportRequest, Depends()],
    data: Annotated[bytes, Depends(_archive)],
) -> ImportedBody:
    found = await engine.import_story(ctx, data, choices)
    return answered(ImportedBody.of(found), ctx, response, replayed=found.replayed)


__all__ = ["router"]
