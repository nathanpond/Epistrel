"""``/media``: immutable media assets and their delivery (FR-MEDIA-1,2,3, FR-API-6; #105,
#107).

- ``POST /media``: a ``multipart/form-data`` upload, the ``metadata`` part (``MediaUpload``)
  first, then the ``file`` part, then, for a video, an optional ``poster_file`` part. 201 with
  the asset's metadata. Refusals: 422 ``subjects_required`` (no ``depicted_subjects``), 415
  ``media_type_not_allowed``, 413 ``media_too_large``, 413 ``use_multipart`` (larger than
  ``media_multipart_threshold``), 422 ``rating_retired``, a video's poster refusals, and with a
  moderation hook 422 ``moderation_rejected`` or 503 ``moderation_unavailable``; nothing is
  stored on any of them.
- ``GET /media``: ``live`` assets newest first (``state=retired``: the deleted ones), a page at
  a time, filtered by ``owner``, ``relation`` and ``kind`` only when asked.
- ``GET /media/{id}``: the metadata; ``GET /media/{id}/content`` (and ``HEAD``): the bytes, with
  the stored MIME type, ``ETag`` (the SHA-256), ``Cache-Control: immutable`` and
  ``Accept-Ranges: bytes``. One ``Range`` is answered 206 with ``Content-Range`` (a suffix
  range too, clamped to the content), one no byte of which exists 416 with ``Content-Range:
  bytes */<size>``; several ranges, another unit or a malformed header get the whole content
  (RFC 9110). ``If-None-Match`` matching the ``ETag`` is 304, before any range; ``If-Range``
  applies the range only when it names the current ``ETag``. A retired asset reads as any
  other, so what references it keeps showing it.
- ``GET /media/{id}/url``: a short-lived URL that serves the bytes without the credential: S3's
  presigned URL (the store serves them), or on the local store Epistrel's own signed
  ``/media/{id}/content?exp=&sig=`` under ``public_base_url``.
- ``DELETE /media/{id}``: retire (204), a video's poster with it. The asset leaves listings and
  refuses new references; the media sweep purges it once nothing references it.
- ``/media/uploads``: resumable uploads (``core.media.uploads``): ``POST`` opens a session,
  ``PUT …/parts/{n}`` sends a part (raw bytes with their ``Content-Length``), ``GET`` lists the
  parts received, ``POST …/complete`` creates the asset (201, ``Location: /media/{id}``) and
  ``DELETE`` aborts.
- ``PUT /media/{id}/owner``, ``POST``/``GET …/relations`` and ``DELETE
  …/relations/{relation}/{principal}``: the asset's owner and relations (``routes.ownership``,
  #104). Nothing gates on them (FR-AUTH-8).

Every handler makes one ``Engine`` call. Writes are deployment-scoped (idempotent under
``Idempotency-Key``, answering the deployment's unchanged ``config_revision``; a part is the
exception: sending it again replaces it, so it keeps no result). Every route needs the service
credential (invariant 4) but one: the content route is on its own router (``content_router``),
whose guard also accepts a valid signed URL (``exp`` and ``sig``; ``api.auth.SIGNED_ROUTES``),
so a player can fetch the bytes from a URL the consumer handed it.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Header, Path, Query, Request, Response
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
from epistrel.core.ids import ULID_PATTERN
from epistrel.core.media.assets import (
    DEFAULT_PAGE,
    MAX_PAGE,
    ByteRange,
    MediaAsset,
    MediaContent,
    MediaPage,
    MediaUrl,
    MediaWrite,
)
from epistrel.core.media.store import CACHE_CONTROL
from epistrel.core.media.uploads import (
    PART_HASH_HEADER,
    SHA256_PATTERN,
    PartStored,
    UploadOpen,
    UploadOpened,
    UploadSession,
)
from epistrel.core.tables.media import MediaKind, MediaState
from epistrel.core.tables.ownership import EntityType

router = APIRouter(prefix="/media", tags=["media"])
content_router = APIRouter(prefix="/media", tags=["media"])
"""The content route alone: included behind ``api.auth.SIGNED_DEPENDENCIES`` (#107)."""

SIG_MAX = 128
EXP_MAX = 2**53 - 1
TTL_MAX = 2**31 - 1
PART_MAX = 10_000
RANGE_PATTERN = re.compile(r"^\s*bytes\s*=\s*([0-9]{0,18})\s*-\s*([0-9]{0,18})\s*$", re.IGNORECASE)
EXPOSED_HEADERS = "Accept-Ranges, Content-Range, Content-Length, ETag"
"""What a browser's script may read of a cross-origin content answer (signed URLs, #107)."""

UploadId = Annotated[
    str,
    Path(
        pattern=ULID_PATTERN,
        description="The upload session's id (a ULID).",
        examples=["01J9Z8Q4N5V6W7X8Y9Z0A1B2C8"],
    ),
]


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


class MediaUrlBody(BaseModel):
    """A short-lived URL that serves the asset's bytes without the service credential, and
    when it stops working (FR-MEDIA-3)."""

    url: str
    expires_at: datetime

    @classmethod
    def of(cls, found: MediaUrl) -> MediaUrlBody:
        return cls(url=found.url, expires_at=found.expires_at)


class UploadOpenedBody(DeploymentMutationResponse):
    """An opened upload session: send its parts (numbered from 1, each ``part_size`` bytes but
    the last) before ``expires_at``, which every part moves on."""

    upload_id: str
    part_size: int
    part_count: int
    expires_at: datetime

    @classmethod
    def of(cls, opened: UploadOpened) -> UploadOpenedBody:
        return cls(
            upload_id=opened.upload_id,
            part_size=opened.part_size,
            part_count=opened.part_count,
            expires_at=opened.expires_at,
            config_revision=opened.config_revision,
        )


class PartStoredBody(DeploymentMutationResponse):
    """A part the session holds now, and every part it has received."""

    n: int
    size: int
    received_parts: list[int]

    @classmethod
    def of(cls, stored: PartStored) -> PartStoredBody:
        return cls(
            n=stored.n,
            size=stored.size,
            received_parts=list(stored.received_parts),
            config_revision=stored.config_revision,
        )


# -- uploads (declared before the ``/{media_id}`` routes, whose paths they would shadow) ----------


@router.post(
    "/uploads",
    status_code=http_status.HTTP_201_CREATED,
    response_model=UploadOpenedBody,
    operation_id="open_upload",
    summary="Open a resumable upload",
    description=(
        "For media larger than `media_multipart_threshold` (smaller ones may use it too): the "
        "asset's metadata, as `POST /media`'s `metadata` part takes it (a video's `poster` "
        "only as an asset id; without one, the poster is extracted from the first frame when "
        "the upload completes), plus the upload's `size`, `mime_type` and `sha256`. Answers "
        "the `upload_id` and the `part_size`: send parts 1 to `part_count` with `PUT "
        "/media/uploads/{upload_id}/parts/{n}`, each `part_size` bytes but the last, then "
        "`POST …/complete`. The session expires `media_upload_ttl` after its last part."
    ),
)
async def open_upload(
    body: UploadOpen, response: Response, ctx: ContextDep, engine: EngineDep
) -> UploadOpenedBody:
    opened = await engine.open_upload(ctx, body)
    return answered(UploadOpenedBody.of(opened), ctx, response, replayed=opened.replayed)


@router.get(
    "/uploads/{upload_id}",
    response_model=UploadSession,
    operation_id="get_upload",
    summary="Read an upload session",
    description=(
        "The session's `state`, its size, MIME type, part size and count, the "
        "`received_parts` so far (an interrupted upload resumes by sending the others), when it "
        "expires, and the asset its completion created (`media_id`)."
    ),
)
async def get_upload(upload_id: UploadId, ctx: ContextDep, engine: EngineDep) -> UploadSession:
    return await engine.get_upload(ctx, upload_id)


@router.put(
    "/uploads/{upload_id}/parts/{n}",
    response_model=PartStoredBody,
    operation_id="put_upload_part",
    summary="Send one part of an upload",
    description=(
        "The part's raw bytes (`application/octet-stream`) with their `Content-Length` (a "
        "chunked body is 411 `length_required`): exactly `part_size` bytes, or the rest for the "
        "last part (422 `part_invalid`, as is a number past the last part). `X-Part-SHA256`, "
        "when sent, must be the part's SHA-256 (422 `checksum_mismatch`). Sending a part again "
        "replaces it, so a retry needs no stored result: the `Idempotency-Key` is echoed, not "
        "kept. 409 `upload_closed` once the session is completed, aborted or expired."
    ),
)
async def put_upload_part(
    upload_id: UploadId,
    n: Annotated[int, Path(ge=1, le=PART_MAX, description="The part's number, from 1.")],
    request: Request,
    response: Response,
    ctx: ContextDep,
    engine: EngineDep,
    part_sha256: Annotated[
        str | None,
        Header(
            alias=PART_HASH_HEADER,
            pattern=SHA256_PATTERN,
            description="The part's SHA-256 (lowercase hex), checked when sent.",
        ),
    ] = None,
) -> PartStoredBody:
    stored = await engine.put_upload_part(
        ctx,
        upload_id,
        n,
        request.stream(),
        size=multipart.declared_length(request),
        sha256=part_sha256,
    )
    return answered(PartStoredBody.of(stored), ctx, response, replayed=False)


@router.post(
    "/uploads/{upload_id}/complete",
    status_code=http_status.HTTP_201_CREATED,
    response_model=MediaUploadedBody,
    operation_id="complete_upload",
    summary="Complete an upload",
    description=(
        "Assemble the parts and store the asset, through every check `POST /media` makes (the "
        "bytes' type, the rating, the moderation hook, a video's poster). Every part must be "
        "there (409 `upload_incomplete`, `details.missing`), and the assembled bytes must have "
        "the declared `sha256` (422 `checksum_mismatch`). A refusal leaves the session open "
        "with its parts. Answers 201 with the asset and `Location: /media/{id}`; a second "
        "completion is 409 `upload_closed`."
    ),
)
async def complete_upload(
    upload_id: UploadId, response: Response, ctx: ContextDep, engine: EngineDep
) -> MediaUploadedBody:
    write = await engine.complete_upload(ctx, upload_id)
    response.headers["Location"] = f"/media/{write.asset.id}"
    return answered(MediaUploadedBody.of(write), ctx, response, replayed=write.replayed)


@router.delete(
    "/uploads/{upload_id}",
    status_code=http_status.HTTP_204_NO_CONTENT,
    response_class=Response,
    operation_id="abort_upload",
    summary="Abort an upload",
    description=(
        "An open session is aborted and its parts removed; a completed or aborted one is left "
        "as it is (204 either way)."
    ),
)
async def abort_upload(upload_id: UploadId, ctx: ContextDep, engine: EngineDep) -> Response:
    aborted = await engine.abort_upload(ctx, upload_id)
    return no_content(ctx, replayed=aborted.replayed)


# -- assets ------------------------------------------------------------------------------------


@router.post(
    "",
    status_code=http_status.HTTP_201_CREATED,
    response_model=MediaUploadedBody,
    operation_id="upload_media",
    summary="Upload an image or video",
    description=(
        "A `multipart/form-data` body: the `metadata` part (JSON: `kind`, `content_rating_id`, "
        "`depicted_subjects`, optional `alt_text`, `caption`, `generation_metadata`, `owner`, "
        "and for a video `poster`), then the `file` part, then for a video an optional "
        "`poster_file` part (an image). The bytes' type is sniffed: it must be one of the "
        "deployment's `media_allowed_types` and agree with `kind` and the part's own "
        "`Content-Type` (else 415 `media_type_not_allowed`); larger than `media_max_bytes` is "
        "413 `media_too_large`, and a body larger than `media_multipart_threshold` is 413 "
        "`use_multipart` (open a resumable upload instead). `depicted_subjects` is required "
        "(`[]` declares none; omitted: 422 `subjects_required`). Every video gets a poster: the "
        "`poster` named (a live image), the `poster_file` sent, or a PNG taken from its first "
        "frame with ffmpeg (422 `poster_required` without ffmpeg, 422 "
        "`poster_extraction_failed` when it fails); a poster stored with the video has its "
        "rating, owner and subjects. When the deployment configures a moderation hook, the "
        "upload (and a poster stored with it) is sent to it before anything is stored; "
        "`reject` is 422 `moderation_rejected`, and a hook that times out, cannot be reached or "
        "answers anything else is 503 `moderation_unavailable` (it fails closed). Identical "
        "bytes are stored once. Answers 201 with the asset."
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
        poster_file=form.poster,
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
        "references it keeps showing it); a video names its poster (`poster_media_id`)."
    ),
)
async def get_media(media_id: MediaId, ctx: ContextDep, engine: EngineDep) -> MediaAsset:
    return await engine.get_media(ctx, media_id)


@router.get(
    "/{media_id}/url",
    response_model=MediaUrlBody,
    operation_id="get_media_url",
    summary="Get a short-lived URL for a media asset's bytes",
    description=(
        "A URL that serves the bytes without the service credential until `expires_at`: on an "
        "S3-compatible store the store's own presigned URL (the store serves the bytes and "
        "answers 403 after the expiry), on the local store Epistrel's signed "
        "`/media/{id}/content?exp=&sig=` under `public_base_url` (503 `config_missing` when it "
        "is unset; afterwards 403 `url_expired`). It works for `ttl` seconds, at most and by "
        "default the deployment's `media_signed_url_ttl` (a longer `ttl` is cut to it). Hand "
        "it only to whoever may see the asset: anyone holding it can fetch the bytes."
    ),
)
async def get_media_url(
    media_id: MediaId,
    ctx: ContextDep,
    engine: EngineDep,
    ttl: Annotated[
        int | None,
        Query(
            ge=1,
            le=TTL_MAX,
            description="Seconds the URL works (at most `media_signed_url_ttl`).",
        ),
    ] = None,
) -> MediaUrlBody:
    return MediaUrlBody.of(await engine.get_media_url(ctx, media_id, ttl=ttl))


@router.delete(
    "/{media_id}",
    status_code=http_status.HTTP_204_NO_CONTENT,
    response_class=Response,
    operation_id="delete_media",
    summary="Delete (retire) a media asset",
    description=(
        "Retire the asset (FR-MEDIA-2): it leaves listings and refuses new references (422 "
        "`media_retired`), while the catalog versions, drafts and stories that already reference "
        "it keep its content until the media sweep finds no reference and purges it. A video's "
        "poster is retired with it. Deleting a retired asset changes nothing (204). A retry "
        "under the same `Idempotency-Key` is a 204 with `Idempotent-Replay: true`."
    ),
)
async def delete_media(media_id: MediaId, ctx: ContextDep, engine: EngineDep) -> Response:
    retired = await engine.delete_media(ctx, media_id)
    return no_content(ctx, replayed=retired.replayed)


mount_ownership_routes(router, EntityType.MEDIA)


# -- the content route (credential or signed URL) -----------------------------------------------


def parse_range(value: str | None) -> ByteRange | None:
    """One ``bytes`` range of a ``Range`` header; ``None`` (the whole content) for no header,
    another unit, several ranges (the pattern takes one), or a malformed one (RFC 9110 §14.2)."""
    if value is None:
        return None
    matched = RANGE_PATTERN.fullmatch(value)
    if matched is None:
        return None
    first, last = matched.groups()
    if not first and not last:
        return None
    if not first:
        return ByteRange(suffix=int(last))
    if not last:
        return ByteRange(first=int(first))
    if int(last) < int(first):
        return None
    return ByteRange(first=int(first), last=int(last))


def entity_tags(value: str | None) -> list[str]:
    """The entity tags of an ``If-None-Match`` header (``*`` kept)."""
    if value is None:
        return []
    return [tag.strip() for tag in value.split(",") if tag.strip()]


def _content_headers(content: MediaContent) -> dict[str, str]:
    headers = {
        "Cache-Control": CACHE_CONTROL,
        "Accept-Ranges": "bytes",
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Expose-Headers": EXPOSED_HEADERS,
    }
    if content.etag is not None:
        headers["ETag"] = content.etag
    if not content.not_modified:
        headers["Content-Length"] = str(content.size)
    if content.start is not None and content.end is not None:
        headers["Content-Range"] = f"bytes {content.start}-{content.end}/{content.total}"
    return headers


def _status(content: MediaContent) -> int:
    if content.not_modified:
        return http_status.HTTP_304_NOT_MODIFIED
    if content.partial:
        return http_status.HTTP_206_PARTIAL_CONTENT
    return http_status.HTTP_200_OK


RANGE_DESCRIPTION = (
    "One byte range, `bytes=<first>-<last>`, `bytes=<first>-` or `bytes=-<suffix>`: answered "
    "206 with `Content-Range` (clamped to the content), or 416 when no byte of it exists. "
    "Several ranges, another unit or a malformed header get the whole content (200)."
)
_HEADERS_DOC: dict[str, Any] = {
    "ETag": {
        "description": "The quoted SHA-256 of the bytes (absent while staged).",
        "schema": {"type": "string"},
    },
    "Cache-Control": {"description": CACHE_CONTROL, "schema": {"type": "string"}},
    "Accept-Ranges": {"description": "`bytes`.", "schema": {"type": "string"}},
    "Access-Control-Allow-Origin": {"description": "`*`.", "schema": {"type": "string"}},
}


async def _content(
    media_id: str,
    ctx: ContextDep,
    engine: EngineDep,
    *,
    exp: int | None,
    sig: str | None,
    range_header: str | None,
    if_none_match: str | None,
    if_range: str | None,
    with_bytes: bool,
) -> Response:
    content = await engine.get_media_content(
        ctx,
        media_id,
        exp=exp,
        sig=sig,
        byte_range=parse_range(range_header),
        if_none_match=entity_tags(if_none_match),
        if_range=if_range.strip() if if_range is not None else None,
        with_bytes=with_bytes,
    )
    headers = _content_headers(content)
    if not with_bytes or content.not_modified:
        await content.aclose()
        return Response(
            status_code=_status(content),
            headers=headers,
            media_type=None if content.not_modified else content.mime_type,
        )
    return StreamingResponse(
        content.chunks(),
        status_code=_status(content),
        media_type=content.mime_type,
        headers=headers,
        background=BackgroundTask(content.aclose),
    )


ExpQuery = Annotated[
    int | None,
    Query(ge=0, le=EXP_MAX, description="A signed URL's expiry (Unix seconds)."),
]
SigQuery = Annotated[str | None, Query(max_length=SIG_MAX, description="A signed URL's signature.")]
RangeHeader = Annotated[str | None, Header(alias="Range", description=RANGE_DESCRIPTION)]
IfNoneMatchHeader = Annotated[
    str | None,
    Header(
        alias="If-None-Match",
        description="Entity tags (or `*`): when one is the asset's `ETag`, 304 with no body.",
    ),
]
IfRangeHeader = Annotated[
    str | None,
    Header(
        alias="If-Range",
        description="An entity tag: `Range` applies only when it is the asset's `ETag`.",
    ),
]


@content_router.get(
    "/{media_id}/content",
    response_class=Response,
    operation_id="get_media_content",
    summary="Read a media asset's bytes",
    description=(
        "The asset's bytes with its stored MIME type, `ETag` (its SHA-256), `Cache-Control: "
        "immutable` and `Accept-Ranges: bytes`; served for a `live` or `retired` asset, and "
        "with `HEAD` as headers only. One `Range` is 206 with `Content-Range`, so video can "
        "seek; one no byte of which exists is 416. `If-None-Match` matching the `ETag` is 304 "
        "(before any range); `If-Range` applies the range only when it names the `ETag`. This "
        "route takes the service credential or, instead, a valid signed URL from `GET "
        "/media/{id}/url` (`exp` and `sig`): an expired one is 403 `url_expired`, a forged one "
        "403 `signature_invalid`, neither credential nor signature 401. With the credential, a "
        "bad `exp`/`sig` is ignored. A signed URL (such as the moderation hook's "
        "`content_url`) also serves an upload still waiting for the hook."
    ),
    responses={
        http_status.HTTP_200_OK: {
            "description": "The asset's bytes.",
            "content": {"*/*": {"schema": {"type": "string", "format": "binary"}}},
            "headers": _HEADERS_DOC,
        },
        http_status.HTTP_206_PARTIAL_CONTENT: {
            "description": "The requested byte range.",
            "content": {"*/*": {"schema": {"type": "string", "format": "binary"}}},
            "headers": {
                **_HEADERS_DOC,
                "Content-Range": {
                    "description": "`bytes <first>-<last>/<size>`.",
                    "schema": {"type": "string"},
                },
            },
        },
        http_status.HTTP_304_NOT_MODIFIED: {
            "description": "`If-None-Match` named the asset's `ETag`: no body.",
            "headers": _HEADERS_DOC,
        },
    },
)
async def get_media_content(
    media_id: MediaId,
    ctx: ContextDep,
    engine: EngineDep,
    exp: ExpQuery = None,
    sig: SigQuery = None,
    range_header: RangeHeader = None,
    if_none_match: IfNoneMatchHeader = None,
    if_range: IfRangeHeader = None,
) -> Response:
    return await _content(
        media_id,
        ctx,
        engine,
        exp=exp,
        sig=sig,
        range_header=range_header,
        if_none_match=if_none_match,
        if_range=if_range,
        with_bytes=True,
    )


@content_router.head(
    "/{media_id}/content",
    response_class=Response,
    operation_id="get_media_content_head",
    include_in_schema=False,
)
async def head_media_content(
    media_id: MediaId,
    ctx: ContextDep,
    engine: EngineDep,
    exp: ExpQuery = None,
    sig: SigQuery = None,
    range_header: RangeHeader = None,
    if_none_match: IfNoneMatchHeader = None,
    if_range: IfRangeHeader = None,
) -> Response:
    """``GET``'s headers without the body (#107)."""
    return await _content(
        media_id,
        ctx,
        engine,
        exp=exp,
        sig=sig,
        range_header=range_header,
        if_none_match=if_none_match,
        if_range=if_range,
        with_bytes=False,
    )
