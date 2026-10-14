"""Feedback export for evaluation (FR-FB-4, FR-ADMIN-8; #126), under ``/admin``:

- ``POST /admin/feedback/export``: export the current ratings as labelled cases (201), when the
  deployment's ``feedback_export`` is on (403 ``feedback_export_disabled`` otherwise);
- ``GET /admin/feedback/exports``: the export log, newest first;
- ``GET /admin/feedback/exports/{export_id}``: one export's record;
- ``GET /admin/feedback/exports/{export_id}/content``: its JSON Lines file, behind the service
  credential (no signed URL);
- ``DELETE /admin/feedback/exports/{export_id}``: delete its file (204; the record stays).

Every handler makes one ``Engine`` call. The delete is idempotent by itself and flagged
``mutating=False``: no ``Idempotency-Key``.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Path, Query, Response
from fastapi import status as http_status
from pydantic import Field

from epistrel.api.deps import ContextDep, EngineDep, route_flags
from epistrel.api.idempotency import answered
from epistrel.api.responses import DeploymentMutationResponse
from epistrel.core.erasure.boundary import DELIVERED_EXPORTS
from epistrel.core.feedback_export import (
    BOUNDARY_NOTE,
    CURSOR_MAX,
    DEFAULT_PAGE,
    MAX_PAGE,
    MEDIA_TYPE,
    FeedbackExport,
    FeedbackExportPage,
    FeedbackExportRequest,
    FeedbackExportWritten,
)
from epistrel.core.ids import ULID_PATTERN

router = APIRouter(prefix="/feedback")

ExportId = Annotated[
    str,
    Path(
        pattern=ULID_PATTERN,
        description="The export's id (a ULID).",
        examples=["01J9Z8Q4N5V6W7X8Y9Z0A1B2E7"],
    ),
]

CASES = (
    "one case per current rating: `position` (`{story_id, turn, variant}`), `message_id`, "
    "`player_input` (`{say, do}`, null for an opening narration), `message_text`, `rating`, "
    "`tags`, `text`, `config_fingerprint`, `profile_version`, `models` and `quality_status`"
)


class FeedbackExportWrittenBody(DeploymentMutationResponse):
    """What the export wrote: its id, where its file is served and how many cases it holds."""

    export_id: str
    content_url: str = Field(
        description="`GET /admin/feedback/exports/{id}/content`: absolute under "
        "`public_base_url` when that is set, else the path. It needs the service credential."
    )
    case_count: int

    @classmethod
    def of(cls, written: FeedbackExportWritten) -> FeedbackExportWrittenBody:
        return cls(
            export_id=written.export_id,
            content_url=written.content_url,
            case_count=written.case_count,
            config_revision=written.config_revision,
        )


@router.post(
    "/export",
    response_model=FeedbackExportWrittenBody,
    status_code=http_status.HTTP_201_CREATED,
    operation_id="export_feedback",
    summary="Export rated messages as labelled cases",
    description=(
        "Export the current ratings for the eval harness (FR-FB-4) as one JSON Lines file "
        f"(`{MEDIA_TYPE}`; keys sorted, one case per line, no cases: an empty file) with "
        f"{CASES}. Only while the deployment's `feedback_export` setting is on (off by "
        "default): otherwise 403 `feedback_export_disabled` and nothing is written. `from` "
        "(inclusive) and `to` (exclusive) bound when a rating was first given; `filters` "
        "narrow to a `story_id`, any of `tags`, `min_rating`..`max_rating`, a "
        "`config_fingerprint` or a `profile_version`. Every variant's ratings and archived "
        "stories' are exported; a story blocked by a pending erasure is left out. The file is "
        "kept in the blob store for seven days, and every export is recorded with the "
        "exporting principal (`GET /admin/feedback/exports`). A case holds no principal, "
        f"credential or setting. **Erasure boundary** ({DELIVERED_EXPORTS}): " + BOUNDARY_NOTE
    ),
)
async def export_feedback(
    body: FeedbackExportRequest, response: Response, ctx: ContextDep, engine: EngineDep
) -> FeedbackExportWrittenBody:
    written = await engine.export_feedback(ctx, body)
    return answered(FeedbackExportWrittenBody.of(written), ctx, response, replayed=written.replayed)


@router.get(
    "/exports",
    response_model=FeedbackExportPage,
    operation_id="list_feedback_exports",
    summary="List the feedback exports",
    description=(
        "The export log (FR-FB-4), newest first, `limit` at a time; `cursor` (a page's "
        "`next_cursor`) continues (422 `cursor_invalid` for one no list gave). Each record says "
        "who exported, when, what (window, filters, case count) and the file's state "
        "(downloaded, expires, deleted, redacted). A record outlives its file and its stories."
    ),
)
async def list_feedback_exports(
    ctx: ContextDep,
    engine: EngineDep,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE, description="Records per page.")] = DEFAULT_PAGE,
    cursor: Annotated[
        str | None,
        Query(max_length=CURSOR_MAX, description="The previous page's `next_cursor`."),
    ] = None,
) -> FeedbackExportPage:
    return await engine.list_feedback_exports(ctx, limit=limit, cursor=cursor)


@router.get(
    "/exports/{export_id}",
    response_model=FeedbackExport,
    operation_id="get_feedback_export",
    summary="Read a feedback export's record",
    description=(
        "One export's record: who exported, when, what, and its file's state and `content_url`. "
        "404 for an unknown export."
    ),
)
async def get_feedback_export(
    export_id: ExportId, ctx: ContextDep, engine: EngineDep
) -> FeedbackExport:
    return await engine.get_feedback_export(ctx, export_id)


@router.get(
    "/exports/{export_id}/content",
    response_class=Response,
    operation_id="get_feedback_export_content",
    summary="Download a feedback export",
    description=(
        f"The export's JSON Lines file (`{MEDIA_TYPE}`), behind the service credential only: "
        "there is no signed URL for it. The first download marks the export delivered "
        f"(`downloaded_at`). {BOUNDARY_NOTE} 404 for an unknown export, or `reason: deleted` "
        "once its file was deleted (by an operator, or seven days after it was written)."
    ),
    responses={
        http_status.HTTP_200_OK: {
            "description": "The export's cases, one JSON object per line.",
            "content": {MEDIA_TYPE: {"schema": {"type": "string"}}},
        }
    },
)
async def get_feedback_export_content(
    export_id: ExportId, ctx: ContextDep, engine: EngineDep
) -> Response:
    found = await engine.get_feedback_export_content(ctx, export_id)
    return Response(
        content=found.data,
        media_type=found.media_type,
        headers={"Cache-Control": "private, no-store"},
    )


@router.delete(
    "/exports/{export_id}",
    status_code=http_status.HTTP_204_NO_CONTENT,
    response_class=Response,
    operation_id="delete_feedback_export",
    summary="Delete a feedback export's file",
    description=(
        "Delete the export's file now (it would go seven days after it was written). Its record "
        "stays, with who deleted it and when: every export stays recorded. 204 also when the "
        "file was already deleted; 404 for an unknown export. The call takes no "
        "`Idempotency-Key`. A copy already downloaded is outside the erasure boundary and "
        "outside this deletion."
    ),
    **route_flags(mutating=False),
)
async def delete_feedback_export(
    export_id: ExportId, ctx: ContextDep, engine: EngineDep
) -> Response:
    await engine.delete_feedback_export(ctx, export_id)
    return Response(status_code=http_status.HTTP_204_NO_CONTENT)
