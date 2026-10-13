"""``/media``: immutable media assets (FR-MEDIA-1,2, FR-API-6; #105).

- ``POST /media``: a ``multipart/form-data`` upload, the ``metadata`` part (``MediaUpload``)
  first, then the ``file`` part. 201 with the asset's metadata. Refusals: 422
  ``subjects_required`` (no ``depicted_subjects``), 415 ``media_type_not_allowed``, 413
  ``media_too_large``, 422 ``rating_retired``, and with a moderation hook 422
  ``moderation_rejected`` or 503 ``moderation_unavailable``; nothing is stored on any of them.
- ``GET /media``: ``live`` assets newest first (``state=retired``: the deleted ones), a page at
  a time, filtered by ``owner``, ``relation`` and ``kind`` only when asked.
- ``GET /media/{id}``: the metadata; ``GET /media/{id}/content``: the bytes, with the stored MIME
  type, ``ETag`` (the SHA-256) and ``Cache-Control: immutable`` (#107 adds ``Range``). A retired
  asset reads as any other, so what references it keeps showing it.
- ``DELETE /media/{id}``: retire (204). The asset leaves listings and refuses new references; the
  media sweep purges it once nothing references it.
- ``PUT /media/{id}/owner``, ``POST``/``GET …/relations`` and ``DELETE
  …/relations/{relation}/{principal}``: the asset's owner and relations (``routes.ownership``,
  #104). Nothing gates on them (FR-AUTH-8).

Every handler makes one ``Engine`` call. Writes are deployment-scoped (idempotent under
``Idempotency-Key``, answering the deployment's unchanged ``config_revision``). Like every route
but the five public ones, the content route needs the service credential (invariant 4); a
signed URL's ``exp`` and ``sig`` only add access to an upload still waiting for the moderation
hook (#107 makes a signed URL enough on its own).
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query, Request, Response
from fastapi import status as http_status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, WithJsonSchema
from starlette.background import BackgroundTask

from epistrel.api import multipart
from epistrel.api.deps import ContextDep, EngineDep, MediaId
from epistrel.api.idempotency import answered, no_content
from epistrel.api.responses import DeploymentMutationResponse
from epistrel.api.routes.ownership import mount_ownership_routes
from epistrel.api.routes.stories import (
    OWNER_FILTER_SCHEMA,
    RELATION_FILTERS_SCHEMA,
    RelationFilterParam,
)
from epistrel.core.context import PRINCIPAL_PATTERN
from epistrel.core.media.assets import DEFAULT_PAGE, MAX_PAGE, MediaAsset, MediaPage, MediaWrite
from epistrel.core.tables.media import MediaKind, MediaState
from epistrel.core.tables.ownership import EntityType

router = APIRouter(prefix="/media", tags=["media"])

CACHE_CONTROL = "private, max-age=31536000, immutable"
"""An asset's bytes never change; only a caller holding the credential (or, from #107, a signed
URL) reads them."""
SIG_MAX = 128
EXP_MAX = 2**53 - 1


class MediaUploadedBody(MediaAsset, DeploymentMutationResponse):
    """The asset an upload stored, with the deployment's ``config_revision`` (unchanged)."""

    model_config = ConfigDict(frozen=False)

    @classmethod
    def of(cls, write: MediaWrite) -> MediaUploadedBody:
        return cls(**write.asset.model_dump(), config_revision=write.config_revision)


class MediaListBody(BaseModel):
    """A page of media assets, newest first; ``next_cursor`` fetches the next page (``null``
    on the last)."""

    items: list[MediaAsset]
    next_cursor: str | None

    @classmethod
    def of(cls, page: MediaPage) -> MediaListBody:
        return cls(items=page.items, next_cursor=page.next_cursor)


@router.post(
    "",
    status_code=http_status.HTTP_201_CREATED,
    response_model=MediaUploadedBody,
    operation_id="upload_media",
    summary="Upload an image or video",
    description=(
        "A `multipart/form-data` body: the `metadata` part (JSON: `kind`, `content_rating_id`, "
        "`depicted_subjects`, optional `alt_text`, `caption`, `generation_metadata`, `owner`), "
        "then the `file` part. The bytes' type is sniffed: it must be one of the deployment's "
        "`media_allowed_types` and agree with `kind` and the part's own `Content-Type` (else 415 "
        "`media_type_not_allowed`); larger than `media_max_bytes` is 413 `media_too_large`. "
        "`depicted_subjects` is required (`[]` declares none; omitted: 422 "
        "`subjects_required`). When the deployment configures a moderation hook, the upload is "
        "sent to it before anything is stored; `reject` is 422 `moderation_rejected`, and a hook "
        "that times out, cannot be reached or answers anything else is 503 "
        "`moderation_unavailable` (it fails closed). Identical bytes are stored once. Answers "
        "201 with the asset."
    ),
)
async def upload_media(
    request: Request, response: Response, ctx: ContextDep, engine: EngineDep
) -> MediaUploadedBody:
    form = await multipart.read_upload(request)
    write = await engine.upload_media(
        ctx,
        form.metadata,
        form.chunks(),
        content_type=form.content_type,
        body_size=multipart.declared_length(request),
    )
    return answered(MediaUploadedBody.of(write), ctx, response, replayed=write.replayed)


@router.get(
    "",
    response_model=MediaListBody,
    operation_id="list_media",
    summary="List media assets",
    description=(
        "`live` assets, newest first (`state=retired` lists the deleted ones), `limit` at a "
        "time; `cursor` (a page's `next_cursor`) continues. `owner`, `relation` "
        "(`name:principal`, or `name:` for any principal; repeatable, all must hold) and `kind` "
        "narrow the list only when a request asks: they gate nothing (FR-AUTH-8)."
    ),
)
async def list_media(
    ctx: ContextDep,
    engine: EngineDep,
    owner: Annotated[
        str | None,
        Query(pattern=PRINCIPAL_PATTERN, description="Only the assets this principal owns."),
        WithJsonSchema(OWNER_FILTER_SCHEMA),
    ] = None,
    relation: Annotated[
        list[RelationFilterParam] | None,
        Query(
            description="Only the assets with this relation: `name:principal`, or `name:` for "
            "any principal (repeatable; all must hold)."
        ),
        WithJsonSchema(RELATION_FILTERS_SCHEMA),
    ] = None,
    kind: MediaKind | None = None,
    state: Annotated[
        MediaState,
        Query(description="`live` (the default) or `retired`; candidates are never listed."),
        WithJsonSchema({"type": "string", "enum": ["live", "retired"], "default": "live"}),
    ] = MediaState.LIVE,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE)] = DEFAULT_PAGE,
    cursor: Annotated[
        str | None, Query(max_length=200, description="The previous page's `next_cursor`.")
    ] = None,
) -> MediaListBody:
    page = await engine.list_media(
        ctx,
        owner=owner,
        relation=relation or (),
        kind=kind,
        state=state,
        limit=limit,
        cursor=cursor,
    )
    return MediaListBody.of(page)


@router.get(
    "/{media_id}",
    response_model=MediaAsset,
    operation_id="get_media",
    summary="Read a media asset's metadata",
    description=(
        "The asset's metadata, `live` or `retired` (a deleted asset still reads, so whatever "
        "references it keeps showing it)."
    ),
)
async def get_media(media_id: MediaId, ctx: ContextDep, engine: EngineDep) -> MediaAsset:
    return await engine.get_media(ctx, media_id)


@router.get(
    "/{media_id}/content",
    response_class=Response,
    operation_id="get_media_content",
    summary="Read a media asset's bytes",
    description=(
        "The asset's bytes with its stored MIME type, `ETag` (its SHA-256) and `Cache-Control: "
        "immutable`; served for a `live` or `retired` asset. `exp` and `sig` are a signed URL's "
        "(the moderation hook's `content_url`): with them, an upload still waiting for the hook "
        "is served too. Like every route but the public five, this one requires the service "
        "credential."
    ),
    responses={
        http_status.HTTP_200_OK: {
            "description": "The asset's bytes.",
            "content": {"*/*": {"schema": {"type": "string", "format": "binary"}}},
            "headers": {
                "ETag": {
                    "description": "The quoted SHA-256 of the bytes (absent while staged).",
                    "schema": {"type": "string"},
                },
                "Cache-Control": {"description": CACHE_CONTROL, "schema": {"type": "string"}},
            },
        }
    },
)
async def get_media_content(
    media_id: MediaId,
    ctx: ContextDep,
    engine: EngineDep,
    exp: Annotated[
        int | None, Query(ge=0, le=EXP_MAX, description="A signed URL's expiry (Unix seconds).")
    ] = None,
    sig: Annotated[
        str | None, Query(max_length=SIG_MAX, description="A signed URL's signature.")
    ] = None,
) -> Response:
    content = await engine.get_media_content(ctx, media_id, exp=exp, sig=sig)
    headers = {"Content-Length": str(content.size), "Cache-Control": CACHE_CONTROL}
    if content.checksum is not None:
        headers["ETag"] = f'"{content.checksum}"'
    return StreamingResponse(
        content.chunks(),
        media_type=content.mime_type,
        headers=headers,
        background=BackgroundTask(content.aclose),
    )


@router.delete(
    "/{media_id}",
    status_code=http_status.HTTP_204_NO_CONTENT,
    response_class=Response,
    operation_id="delete_media",
    summary="Delete (retire) a media asset",
    description=(
        "Retire the asset (FR-MEDIA-2): it leaves listings and refuses new references (422 "
        "`media_retired`), while the catalog versions, drafts and stories that already reference "
        "it keep its content until the media sweep finds no reference and purges it. Deleting a "
        "retired asset changes nothing (204). A retry under the same `Idempotency-Key` is a 204 "
        "with `Idempotent-Replay: true`."
    ),
)
async def delete_media(media_id: MediaId, ctx: ContextDep, engine: EngineDep) -> Response:
    retired = await engine.delete_media(ctx, media_id)
    return no_content(ctx, replayed=retired.replayed)


mount_ownership_routes(router, EntityType.MEDIA)
