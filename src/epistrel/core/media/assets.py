"""Media assets: upload, the moderation hook, posters, reads, signed URLs, retirement and
reference discovery (FR-MEDIA-1,2,3, FR-ADMIN-6, FR-API-6; docs/02 §4.10; #105, #107).

**Upload** (``POST /media``; ``Engine.upload_media``) takes the asset's metadata (``MediaUpload``)
and its bytes:

1. The metadata is validated first: ``kind``, ``content_rating_id`` (omitted: 422
   ``validation_error``), optional ``alt_text`` (≤ 500), ``caption`` (≤ 2000),
   ``generation_metadata`` (FR-CAT-4's fields, stored exactly as sent, so a parameter number
   JSON cannot hold, such as ``1e400``, is 422), ``owner`` and, for a video, ``poster`` (an
   image asset to show for it), and the ``depicted_subjects`` every upload must declare
   (FR-MEDIA-1, FR-IMG-9): an omitted list is 422 ``subjects_required``, an explicitly empty
   one is accepted. Subjects are ``{kind, id}`` (``character``, ``catalog_character`` or
   ``persona``), stored as declared with no check that they exist (a subject may be created
   later), deduplicated, at most 50.
2. The bytes stream into the staging area (``core.media.store``) while they are hashed and
   counted: a request whose declared length exceeds ``media_max_bytes`` plus the metadata's
   allowance is 413 ``media_too_large`` before the file is read, and so is a file that grows
   past ``media_max_bytes``. A request larger than ``media_multipart_threshold`` (declared, or
   counted as it streams) is 413 ``use_multipart``: it must use a resumable session
   (``core.media.uploads``, #107). The type is sniffed from the first bytes
   (``core.media.probe``): one the deployment does not allow (``media_allowed_types``), one
   Epistrel does not recognise, or one that disagrees with the declared ``kind`` or the file's
   own MIME type is 415 ``media_type_not_allowed``. A video may carry a second file, its poster
   (``poster_file``), staged the same way as an image.
3. The call then takes its ``Idempotency-Key`` (the request hash covers the metadata and the
   bytes' SHA-256), checks the rating (422 ``rating_retired`` for an unknown or retired one,
   #50), probes the dimensions or duration, and, when ``media_moderation_webhook_url`` is set,
   asks the moderation hook (``core.media.moderation``; fails closed). A **video's poster**
   (FR-MEDIA-3, #107) is the ``poster`` named (a ``live`` image: 422 ``media_retired`` or
   ``poster_invalid`` otherwise), the ``poster_file`` sent, or a PNG ffmpeg takes from the
   video's first frame (``core.media.poster``; 422 ``poster_required`` without ffmpeg, 422
   ``poster_extraction_failed`` when it fails). A poster stored with its video inherits the
   video's owner, rating and subjects, is described ``Poster for <caption or id>``, and goes to
   the hook too; any refusal fails the whole upload. Nothing is stored before the hook accepts,
   and a refusal leaves no row and no blob.
4. One transaction, under the checksums' advisory locks, inserts the rows (``live``, ``source =
   uploaded``, or ``extracted`` for an extracted poster; the rating's current version; the
   video's ``poster_media_id``), indexes their subjects (``core.erasure.index``: the declared
   subjects and the catalog characters a caption, alt text or prompt names), records a
   committed ``control`` operation (``core.control``) and moves the staged bytes into place.
   Identical bytes share one blob. A resumable session's completion (``core.media.uploads``)
   runs steps 3 and 4 on its assembled bytes.

**Reads.** ``get`` returns the metadata of a ``live`` or ``retired`` asset (a ``candidate`` is
private, FR-IMG-7: 404). ``open_content`` opens its bytes, all of them or one byte range
(``ByteRange``; FR-MEDIA-3: a file seek locally, a ``Range`` request on S3; 416
``range_not_satisfiable`` when no byte of it exists), answers ``not_modified`` to a matching
``If-None-Match`` before any range, and applies a range only when ``If-Range`` names the current
entity tag (the checksum). With a valid signed URL (``exp``/``sig``, ``core.media.signing``) it
also serves an upload still waiting for the hook, which is how the hook's ``content_url`` works
before anything is committed. ``signed_url`` is ``GET /media/{id}/url``: a presigned URL the S3
store serves itself, or on the local store Epistrel's own signed content URL under
``public_base_url`` (503 ``config_missing`` without it), valid ``media_signed_url_ttl`` seconds
or less. ``list_assets`` lists ``live`` assets (or ``retired`` ones when asked), newest first.

**Delete is retire** (FR-MEDIA-2): ``retire`` sets ``state = retired``, and a video's poster is
retired with it. A retired asset leaves listings and refuses new references
(``assert_referenceable``, which #108, #115 and #119 call: 422 ``media_retired``), while the
records that already reference it keep reading it. The media sweep (``core.media.sweep``) purges
it once nothing references it. **Reference discovery** queries the records that can reference an
asset (``MEDIA_REFERENCE_SOURCES``, which later stories extend with
``register_reference_source``; a video's ``poster_media_id`` is one); nothing counts references.
``referenced`` answers for given assets, and ``unreferenced`` is the same test as an anti-join a
query filters by.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import json
import os
import re
from collections.abc import AsyncIterable, AsyncIterator, Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any, BinaryIO

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    ValidationError,
)
from sqlalchemy import (
    ColumnElement,
    Integer,
    and_,
    cast,
    func,
    insert,
    select,
    true,
    tuple_,
    update,
)
from sqlalchemy import text as sql_text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine
from sqlalchemy.sql.elements import TextClause

from epistrel.core import clock, control, ownership
from epistrel.core.config_changes import read_section
from epistrel.core.erasure import guard as erasure_guard
from epistrel.core.erasure.index import Declared, index_references
from epistrel.core.errors import (
    ConfigMissing,
    InvalidRequest,
    MediaNotFound,
    MediaRetired,
    MediaTooLarge,
    MediaTypeNotAllowed,
    PosterInvalid,
    RangeNotSatisfiable,
    SubjectsRequired,
    UseMultipart,
    refuse_nul,
)
from epistrel.core.ids import ULID_PATTERN, new_ulid
from epistrel.core.json_numbers import require_finite
from epistrel.core.media import moderation, probe
from epistrel.core.media.poster import POSTER_MIME_TYPE, PosterExtractor
from epistrel.core.media.signing import Signer
from epistrel.core.media.store import BlobStore, ByteStream, Window
from epistrel.core.ownership import Owner, RelationFilter
from epistrel.core.ratings import require_assignable
from epistrel.core.settings_schema import MediaSettings
from epistrel.core.tables import content_rating_versions, content_ratings, deployment_state
from epistrel.core.tables.deployment import DEPLOYMENT_ROW_ID
from epistrel.core.tables.media import (
    MediaKind,
    MediaSource,
    MediaState,
    SubjectKind,
    media_assets,
)
from epistrel.core.tables.ownership import EntityType
from epistrel.testing import pauses

RECORD_TABLE = media_assets.name
"""``subject_references.record_table`` of an asset's rows."""
ALT_TEXT_MAX = 500
CAPTION_MAX = 2_000
SUBJECTS_MAX = 50
GENERATION_NAME_MAX = 200
PROMPT_MAX = 4_000
PARAMETERS_MAX_BYTES = 8 * 1024
REFERENCE_IDS_MAX = 20
SEED_MAX = 2**64 - 1
METADATA_MAX_BYTES = 256 * 1024
"""The largest ``metadata`` part an upload may carry."""
ENVELOPE_MAX_BYTES = METADATA_MAX_BYTES + 16 * 1024
"""What a multipart request may carry besides the file: the metadata part and the framing. A
request whose declared length exceeds ``media_max_bytes`` plus this is 413 before it is read."""
CHUNK_BYTES = 64 * 1024
LOCK_CLASS = 0x4D454449
"""The first key of every checksum's ``pg_advisory_xact_lock(int, int)``. The two-key space
is separate from the one-key space the deployment lock (``DEPLOYMENT_LOCK_KEY``) lives in."""
DEFAULT_PAGE = 50
MAX_PAGE = 200
INDEXED_FIELDS = (
    "caption",
    "alt_text",
    "generation_metadata.prompt",
    "generation_metadata.negative_prompt",
)
"""The text fields whose subject names ``index_references`` records (FR-ADMIN-6)."""
DECLARED_FIELD = "depicted_subjects"
MIME_ALIASES = {"image/jpg": "image/jpeg", "image/pjpeg": "image/jpeg", "image/x-png": "image/png"}
"""Non-standard spellings clients send for a file's own MIME type."""
UNSPECIFIED_TYPE = "application/octet-stream"
"""A file part declared as this (or not at all) declares no type."""
POSTER_FIELD = "poster"
POSTER_FILE_FIELD = "poster_file"
POSTER_ALT_PREFIX = "Poster for "

Ulid = Annotated[str, Field(pattern=ULID_PATTERN)]


# -- the request ----------------------------------------------------------------------------------


class DepictedSubject(BaseModel):
    """A subject an image or video depicts (FR-IMG-9): a story ``character``, a
    ``catalog_character`` or a ``persona``, by id. Stored as declared, with no check that the
    subject exists (it may be created later), and indexed so that erasure finds the asset
    (FR-ADMIN-6)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: SubjectKind
    id: Ulid


def _parameters_bounded(value: dict[str, Any] | None) -> dict[str, Any] | None:
    if value is not None:
        size = len(json.dumps(value, separators=(",", ":"), sort_keys=True).encode())
        if size > PARAMETERS_MAX_BYTES:
            raise ValueError(f"is {size} bytes as JSON; the limit is {PARAMETERS_MAX_BYTES}")
    return value


class GenerationMetadata(BaseModel):
    """How an image was generated (FR-CAT-4): the adapter, the model, the prompt and negative
    prompt, the seed, the parameters (a JSON object of at most 8 KiB, every number in it finite:
    ``1e400``, ``NaN`` or ``Infinity`` is 422) and the ids of the reference images. Stored and
    returned exactly as sent: a key that was not sent is not returned."""

    model_config = ConfigDict(extra="forbid")

    adapter: str | None = Field(default=None, max_length=GENERATION_NAME_MAX)
    model: str | None = Field(default=None, max_length=GENERATION_NAME_MAX)
    prompt: str | None = Field(default=None, max_length=PROMPT_MAX)
    negative_prompt: str | None = Field(default=None, max_length=PROMPT_MAX)
    seed: int | None = Field(default=None, ge=0, le=SEED_MAX)
    parameters: Annotated[
        dict[str, Any] | None,
        AfterValidator(require_finite),  # a JSON dump would store a non-finite number as null
        AfterValidator(_parameters_bounded),
    ] = None
    reference_ids: list[Ulid] | None = Field(default=None, max_length=REFERENCE_IDS_MAX)


def _deduplicated(value: list[DepictedSubject] | None) -> list[DepictedSubject] | None:
    return None if value is None else list(dict.fromkeys(value))


EXAMPLE_UPLOAD: dict[str, Any] = {
    "kind": "image",
    "content_rating_id": "00000000000000000000RAT001",
    "alt_text": "Mara at the lighthouse door",
    "caption": "Gull Rock, the night of the storm.",
    "depicted_subjects": [{"kind": "catalog_character", "id": "01J9Z8Q4N5V6W7X8Y9Z0A1B2C5"}],
}


def document_subjects_required(schema: dict[str, Any]) -> None:
    """Document ``depicted_subjects`` as the required list it is (omitting it is refused with
    ``subjects_required`` rather than pydantic's ``missing``)."""
    schema.setdefault("required", []).append(DECLARED_FIELD)
    prop = schema["properties"][DECLARED_FIELD]
    options = [o for o in prop.pop("anyOf", []) if o.get("type") != "null"]
    if options:
        prop.update(options[0])
    prop.pop("default", None)
    schema["examples"] = [dict(EXAMPLE_UPLOAD)]


class MediaUpload(BaseModel):
    """``POST /media``'s ``metadata`` part (FR-MEDIA-1): the asset's ``kind`` (which its bytes
    must be), its content rating, optional alt text, caption and generation metadata, the
    subjects it depicts (required: ``[]`` declares none), optionally its ``owner`` (default: the
    requesting principal; never ``system``) and, for a video, its ``poster`` (FR-MEDIA-3)."""

    model_config = ConfigDict(extra="forbid", json_schema_extra=document_subjects_required)

    kind: MediaKind = Field(description="`image` or `video`: what the bytes must be.")
    content_rating_id: str = Field(
        min_length=1,
        max_length=64,
        description="The asset's content rating (FR-RATE-1); an unknown or retired one is 422 "
        "`rating_retired`.",
    )
    alt_text: str | None = Field(default=None, max_length=ALT_TEXT_MAX)
    caption: str | None = Field(default=None, max_length=CAPTION_MAX)
    generation_metadata: GenerationMetadata | None = None
    depicted_subjects: Annotated[list[DepictedSubject] | None, AfterValidator(_deduplicated)] = (
        Field(
            default=None,
            max_length=SUBJECTS_MAX,
            description="Every subject the asset depicts (required; `[]` declares none). Omitted: "
            "422 `subjects_required`.",
        )
    )
    owner: Owner | None = Field(
        default=None,
        description="The asset's owner principal (default: the requesting principal; never "
        "`system`). Recorded, never interpreted.",
    )
    poster: Ulid | None = Field(
        default=None,
        description="A video only (FR-MEDIA-3): the id of a `live` image asset to show as its "
        "poster. Without one (and without a `poster_file` part), the poster is taken from the "
        "video's first frame with ffmpeg. On anything but a video it is 422.",
    )

    def stored_generation_metadata(self) -> dict[str, Any] | None:
        """The generation metadata as stored: exactly the keys that were sent."""
        if self.generation_metadata is None:
            return None
        stored: dict[str, Any] = self.generation_metadata.model_dump(
            mode="json", exclude_unset=True
        )
        return stored


_UPLOAD: TypeAdapter[MediaUpload] = TypeAdapter(MediaUpload)


def parse_metadata(raw: bytes) -> MediaUpload:
    """The ``metadata`` part of a REST upload, validated as JSON's own types (strict, like
    every JSON body, FR-API-4): 422 ``validation_error`` at ``body.metadata.<field>``, or
    ``subjects_required``."""
    try:
        document = json.loads(raw)
    except ValueError:
        raise InvalidRequest(
            "the metadata part is not valid JSON", {"field": "body.metadata"}
        ) from None
    refuse_nul(document, ("body", "metadata"))
    try:
        request = _UPLOAD.validate_json(raw, strict=True)
    except ValidationError as exc:
        raise InvalidRequest.from_errors(exc.errors(), prefix=("body", "metadata")) from None
    return check_upload(request)


def require_subjects(request: MediaUpload, *, at: str = "body.metadata") -> MediaUpload:
    """``request`` if it declared its subjects; 422 ``subjects_required`` otherwise."""
    if request.depicted_subjects is None:
        raise SubjectsRequired(
            "an upload must declare the subjects it depicts: `depicted_subjects` "
            "(an empty list declares none)",
            {"field": f"{at}.{DECLARED_FIELD}"},
        )
    return request


def check_upload(request: MediaUpload, *, at: str = "body.metadata") -> MediaUpload:
    """The checks pydantic leaves to the Engine: subjects declared (422 ``subjects_required``)
    and a ``poster`` only on a video (422 ``validation_error`` at ``<at>.poster``)."""
    request = require_subjects(request, at=at)
    if request.poster is not None and request.kind is not MediaKind.VIDEO:
        raise InvalidRequest("only a video has a poster", {"field": f"{at}.{POSTER_FIELD}"})
    return request


# -- results ---------------------------------------------------------------------------------------


class MediaAsset(BaseModel):
    """A media asset's metadata (FR-MEDIA-1): its owner, kind, MIME type (sniffed from the
    bytes), size, dimensions or duration (``null`` when no probe could read them), SHA-256
    checksum, content rating and the rating's version it was uploaded under, alt text, caption,
    generation metadata (as sent), source, state (``live``, or ``retired`` once deleted), the
    subjects it depicts, who created it when, and, for a video, its poster's id (FR-MEDIA-3;
    ``null`` for an image)."""

    id: str
    owner_principal: str
    kind: MediaKind
    mime_type: str
    size: int
    width: int | None
    height: int | None
    duration_ms: int | None
    checksum: str
    content_rating_id: str | None
    content_rating_version_id: str
    alt_text: str | None
    caption: str | None
    generation_metadata: dict[str, Any] | None
    source: MediaSource
    state: MediaState
    depicted_subjects: list[DepictedSubject]
    created_at: datetime
    created_by: str
    poster_media_id: str | None


@dataclass(frozen=True, slots=True)
class MediaWrite:
    """An uploaded asset and the deployment's ``config_revision`` (which media writes leave as
    it is)."""

    asset: MediaAsset
    config_revision: int
    replayed: bool = False
    """``True`` when an ``Idempotency-Key`` retry returned this stored result (#61)."""


@dataclass(frozen=True, slots=True)
class MediaRetirement:
    """A delete: the asset, and whether it was ``live`` until now (``changed``)."""

    id: str
    changed: bool
    config_revision: int
    replayed: bool = False
    """``True`` when an ``Idempotency-Key`` retry returned this stored result (#61)."""


@dataclass(frozen=True, slots=True)
class MediaPage:
    """A page of assets, newest first, and the cursor of the next page (``None``: the last)."""

    items: list[MediaAsset]
    next_cursor: str | None


@dataclass(frozen=True, slots=True)
class MediaUrl:
    """A short-lived URL that serves an asset's bytes without the service credential (S3's
    presigned URL, or Epistrel's signed content URL), and when it stops working."""

    url: str
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class ByteRange:
    """One requested byte range (RFC 9110 §14.1.2): ``first``-``last`` (``last`` omitted: to the
    end), or the final ``suffix`` bytes."""

    first: int | None = None
    last: int | None = None
    suffix: int | None = None

    def resolve(self, size: int) -> tuple[int, int] | None:
        """The inclusive ``(start, end)`` this range selects of ``size`` bytes, clamped to the
        content; ``None`` when no byte of it exists (416)."""
        if self.suffix is not None:
            if self.suffix <= 0 or size == 0:
                return None
            return max(0, size - self.suffix), size - 1
        first = self.first or 0
        if first >= size:
            return None
        last = size - 1 if self.last is None else min(self.last, size - 1)
        return first, last


@dataclass
class MediaContent:
    """An asset's bytes, opened: their MIME type, how many this answer carries (``size``), the
    whole content's size (``total``), the checksum (``None`` for an upload still waiting for the
    moderation hook) and, for a partial answer, the inclusive ``start``/``end`` of the range.
    ``not_modified`` answers a matching ``If-None-Match`` with no bytes; ``handle`` is ``None``
    then, and for a metadata-only read (``HEAD``). Read the bytes with ``chunks()`` or
    ``read()``; either closes the stream at the end, and ``aclose()`` closes it early."""

    media_id: str
    mime_type: str
    size: int
    checksum: str | None
    handle: ByteStream | None = field(default=None, repr=False)
    total: int = 0
    start: int | None = None
    end: int | None = None
    not_modified: bool = False
    _closed: bool = field(default=False, repr=False)

    @property
    def partial(self) -> bool:
        return self.start is not None

    @property
    def etag(self) -> str | None:
        return f'"{self.checksum}"' if self.checksum is not None else None

    async def chunks(self) -> AsyncIterator[bytes]:
        try:
            if self.handle is None:
                return
            while True:
                chunk = await asyncio.to_thread(self.handle.read, CHUNK_BYTES)
                if not chunk:
                    return
                yield chunk
        finally:
            await self.aclose()

    async def read(self) -> bytes:
        return b"".join([chunk async for chunk in self.chunks()])

    async def aclose(self) -> None:
        if self.handle is not None and not self._closed:
            self._closed = True
            await asyncio.to_thread(self.handle.close)


# -- staging --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Staged:
    """An upload's bytes in the staging area: their SHA-256, size and sniffed type."""

    asset_id: str
    path: Path
    sha256: str
    size: int
    mime_type: str
    kind: MediaKind


@dataclass
class UploadFile:
    """A second file an upload carries, a video's ``poster_file``: its bytes (or chunks) and
    the MIME type it declares."""

    content: bytes | AsyncIterable[bytes]
    content_type: str | None = None


PosterFileSource = UploadFile | Callable[[], Awaitable[UploadFile | None]] | None
"""A ``poster_file``: given, absent, or (REST) found only once the main file has been read."""


def _declared(content_type: str | None) -> str | None:
    """A file part's own MIME type without parameters, or ``None`` when it declares none."""
    if content_type is None:
        return None
    value = content_type.split(";", 1)[0].strip().lower()
    value = MIME_ALIASES.get(value, value)
    return None if value in ("", UNSPECIFIED_TYPE) else value


def _type_refused(
    reason: str, message: str, found: str | None, declared: str | None, allowed: list[str]
) -> MediaTypeNotAllowed:
    return MediaTypeNotAllowed(
        message, {"reason": reason, "mime_type": found, "declared": declared, "allowed": allowed}
    )


def check_type(
    head: bytes, settings: MediaSettings, kind: MediaKind, content_type: str | None
) -> str:
    """The sniffed MIME type of bytes starting ``head``; 415 ``media_type_not_allowed`` unless
    it is recognised, allowed, and agrees with ``kind`` and the declared type."""
    found = probe.sniff(head)
    declared = _declared(content_type)
    allowed = list(settings.media_allowed_types)
    if found is None:
        raise _type_refused(
            "unrecognized", "the file is not a type Epistrel recognises", found, declared, allowed
        )
    if found not in allowed:
        raise _type_refused(
            "not_allowed", f"{found} is not an allowed media type", found, declared, allowed
        )
    if declared is not None and declared != found:
        raise _type_refused(
            "declared_mismatch", f"the file is {found}, not {declared}", found, declared, allowed
        )
    if probe.kind_of(found) is not kind:
        raise _type_refused(
            "kind_mismatch", f"the file is {found}, not an {kind.value}", found, declared, allowed
        )
    return found


def check_declared_type(mime_type: str, settings: MediaSettings, kind: MediaKind) -> str:
    """A resumable session's declared MIME type, before any byte arrives: 415
    ``media_type_not_allowed`` unless it is allowed and agrees with ``kind``."""
    declared = _declared(mime_type) or mime_type
    allowed = list(settings.media_allowed_types)
    if declared not in allowed:
        raise _type_refused(
            "not_allowed", f"{declared} is not an allowed media type", None, declared, allowed
        )
    if probe.kind_of(declared) is not kind:
        raise _type_refused(
            "kind_mismatch", f"{declared} is not an {kind.value}", None, declared, allowed
        )
    return declared


def too_large(size: int, limit: int) -> MediaTooLarge:
    return MediaTooLarge(
        f"the upload is larger than the {limit}-byte limit", {"size": size, "limit": limit}
    )


def _use_multipart(size: int, threshold: int) -> UseMultipart:
    return UseMultipart(
        f"an upload larger than {threshold} bytes must use a resumable session "
        "(POST /media/uploads)",
        {"size": size, "threshold": threshold},
    )


def _finish(handle: BinaryIO) -> None:
    handle.flush()
    os.fsync(handle.fileno())
    handle.close()


def _abandon(handle: BinaryIO, path: Path) -> None:
    handle.close()
    path.unlink(missing_ok=True)


async def stage(
    store: BlobStore,
    asset_id: str,
    content: AsyncIterable[bytes],
    *,
    settings: MediaSettings,
    kind: MediaKind,
    content_type: str | None = None,
    body_size: int | None = None,
    threshold: int | None = None,
    counted: int = 0,
) -> Staged:
    """Stream ``content`` into the staging file of ``asset_id``, hashing and counting it, and
    check its type and size as it arrives (see the module docstring). ``body_size`` is the whole
    request's declared length (REST's ``Content-Length``). ``threshold`` is the direct upload's
    ``media_multipart_threshold``, over this file plus the ``counted`` bytes of the request's
    earlier files (``None``: no threshold). On any refusal or error the staging file is
    removed."""
    limit = settings.media_max_bytes
    if body_size is not None and body_size > limit + ENVELOPE_MAX_BYTES:
        raise too_large(body_size, limit)
    declared = body_size if body_size is not None else 0
    if threshold is not None and declared > threshold + ENVELOPE_MAX_BYTES:
        raise _use_multipart(declared, threshold)
    path = store.staging_path(asset_id)
    handle = await asyncio.to_thread(store.open_staging, asset_id)
    digest = hashlib.sha256()
    size = 0
    head = bytearray()
    found: str | None = None
    try:
        async for chunk in content:
            if not chunk:
                continue
            size += len(chunk)
            if size > limit:
                raise too_large(size, limit)
            if threshold is not None and counted + size > threshold:
                raise _use_multipart(counted + size, threshold)
            if found is None:
                head += chunk[: probe.SNIFF_BYTES - len(head)]
                if len(head) >= probe.SNIFF_BYTES:
                    found = check_type(bytes(head), settings, kind, content_type)
            digest.update(chunk)
            await asyncio.to_thread(handle.write, chunk)
        if found is None:
            found = check_type(bytes(head), settings, kind, content_type)
        await asyncio.to_thread(_finish, handle)
    except BaseException:
        await asyncio.to_thread(_abandon, handle, path)
        raise
    return Staged(asset_id, path, digest.hexdigest(), size, found, kind)


def _digest_file(path: Path) -> tuple[str, int, bytes]:
    """A staged file's SHA-256, size and first bytes."""
    digest = hashlib.sha256()
    size = 0
    head = b""
    with path.open("rb") as handle:
        while chunk := handle.read(CHUNK_BYTES):
            if not head:
                head = chunk[: probe.SNIFF_BYTES]
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size, head


async def discard(staged: Staged | None) -> None:
    """Remove the staged file if it is still there (a refusal, a replay, an error)."""
    if staged is not None:
        await asyncio.to_thread(staged.path.unlink, missing_ok=True)


async def content_of(data: bytes | AsyncIterable[bytes]) -> AsyncIterator[bytes]:
    """``data`` as chunks (a library caller may pass the bytes themselves)."""
    if isinstance(data, bytes | bytearray | memoryview):
        view = bytes(data)
        for start in range(0, len(view), CHUNK_BYTES):
            yield view[start : start + CHUNK_BYTES]
        return
    async for chunk in data:
        yield chunk


async def poster_file_of(source: PosterFileSource) -> UploadFile | None:
    """The ``poster_file`` a source gives (REST's is read only now, after the main file)."""
    if source is None or isinstance(source, UploadFile):
        return source
    return await source()


# -- shared reads --------------------------------------------------------------------------------


async def read_media_settings(db: AsyncEngine | AsyncConnection) -> MediaSettings:
    """The deployment's media settings, read now (defaults where unset or invalid)."""
    return await read_section(db, MediaSettings)


async def config_revision(conn: AsyncConnection) -> int:
    found = await conn.scalar(
        select(deployment_state.c.config_revision).where(deployment_state.c.id == DEPLOYMENT_ROW_ID)
    )
    return int(found or 0)


async def lock_checksum(conn: AsyncConnection, sha256: str) -> None:
    """Serialise every write of one blob (an upload's ``put``, the sweep's delete) in ``conn``'s
    transaction: ``pg_advisory_xact_lock(LOCK_CLASS, <the digest's first 32 bits>)``."""
    key = int(sha256[:8], 16) - 2**31
    await conn.execute(
        select(func.pg_advisory_xact_lock(cast(LOCK_CLASS, Integer), cast(key, Integer)))
    )


_COLUMNS = (
    media_assets.c.id,
    media_assets.c.owner_principal,
    media_assets.c.kind,
    media_assets.c.mime_type,
    media_assets.c.size,
    media_assets.c.width,
    media_assets.c.height,
    media_assets.c.duration_ms,
    media_assets.c.checksum,
    content_rating_versions.c.rating_id.label("content_rating_id"),
    media_assets.c.content_rating_version_id,
    media_assets.c.alt_text,
    media_assets.c.caption,
    media_assets.c.generation_metadata,
    media_assets.c.source,
    media_assets.c.state,
    media_assets.c.depicted_subjects,
    media_assets.c.created_at,
    media_assets.c.created_by,
    media_assets.c.poster_media_id,
)
_FROM = media_assets.outerjoin(
    content_rating_versions,
    content_rating_versions.c.version_id == media_assets.c.content_rating_version_id,
)


def _asset(row: Any) -> MediaAsset:
    return MediaAsset.model_validate(dict(row._mapping))


def _not_found(media_id: str) -> MediaNotFound:
    return MediaNotFound(f"media asset {media_id} does not exist", {"media_id": media_id})


async def read_asset(conn: AsyncConnection, media_id: str) -> MediaAsset:
    """The asset's metadata (``live`` or ``retired``); 404 otherwise."""
    row = (
        await conn.execute(
            select(*_COLUMNS).select_from(_FROM).where(media_assets.c.id == media_id)
        )
    ).one_or_none()
    if row is None or row.state == MediaState.CANDIDATE.value:
        raise _not_found(media_id)
    return _asset(row)


async def rating_version(conn: AsyncConnection, rating_id: str) -> str:
    """The current version of ``rating_id`` if it can be assigned (held ``FOR SHARE``); 422
    ``rating_retired`` (``reason`` unknown or retired) otherwise."""
    await require_assignable(conn, rating_id, field="metadata.content_rating_id")
    version: str = (
        await conn.execute(
            select(content_ratings.c.current_version_id).where(content_ratings.c.id == rating_id)
        )
    ).scalar_one()
    return version


async def check_supplied_poster(conn: AsyncConnection, media_id: str, *, field: str) -> None:
    """A ``poster`` an upload names: a ``live`` asset (held ``FOR SHARE``; 422
    ``media_retired`` otherwise) that is an image (422 ``poster_invalid``)."""
    await assert_referenceable(conn, [media_id], field=field)
    kind = await conn.scalar(select(media_assets.c.kind).where(media_assets.c.id == media_id))
    if kind != MediaKind.IMAGE.value:
        raise PosterInvalid(
            f"media asset {media_id} is not an image, so it cannot be a poster",
            {"media_id": media_id, "field": field, "reason": "not_image"},
        )


# -- upload ----------------------------------------------------------------------------------------


def _hook_payload(
    staged: Staged,
    request: MediaUpload,
    found: probe.Dimensions,
    *,
    owner: str,
    content_url: str,
) -> dict[str, Any]:
    return {
        "asset": {
            "id": staged.asset_id,
            "kind": staged.kind.value,
            "mime_type": staged.mime_type,
            "size": staged.size,
            "width": found.width,
            "height": found.height,
            "duration_ms": found.duration_ms,
            "content_rating_id": request.content_rating_id,
            "alt_text": request.alt_text,
            "caption": request.caption,
            "owner_principal": owner,
            "depicted_subjects": [
                s.model_dump(mode="json") for s in request.depicted_subjects or []
            ],
        },
        "sha256": staged.sha256,
        "content_url": content_url,
    }


def content_path(media_id: str) -> str:
    """The content route of an asset (what a signed URL signs)."""
    return f"/media/{media_id}/content"


def _indexed_fields(request: MediaUpload) -> dict[str, str | None]:
    """The text of each of ``INDEXED_FIELDS``."""
    metadata = request.generation_metadata
    found = {
        "caption": request.caption,
        "alt_text": request.alt_text,
        "generation_metadata.prompt": metadata.prompt if metadata is not None else None,
        "generation_metadata.negative_prompt": (
            metadata.negative_prompt if metadata is not None else None
        ),
    }
    return {path: found[path] for path in INDEXED_FIELDS}


def poster_request(video: MediaUpload, video_id: str, *, owner: str) -> MediaUpload:
    """What a poster stored with its video is: an image with the video's rating, subjects and
    owner, described ``Poster for <caption or id>``."""
    return MediaUpload(
        kind=MediaKind.IMAGE,
        content_rating_id=video.content_rating_id,
        alt_text=(POSTER_ALT_PREFIX + (video.caption or video_id))[:ALT_TEXT_MAX],
        depicted_subjects=list(video.depicted_subjects or []),
        owner=owner,
    )


@dataclass(frozen=True, slots=True)
class Probed:
    """A staged file and what its probe read."""

    staged: Staged
    found: probe.Dimensions


@dataclass(frozen=True, slots=True)
class Prepared:
    """A staged upload checked and accepted (its rating, its probe, the hook) and, for a
    video, its poster: an existing image (``poster_id``), or one stored with it (``poster``,
    ``poster_source``)."""

    request: MediaUpload
    owner: str
    main: Probed
    poster_id: str | None = None
    poster: Probed | None = None
    poster_source: MediaSource = MediaSource.UPLOADED
    poster_field: str = f"body.metadata.{POSTER_FIELD}"

    @property
    def extracted(self) -> Staged | None:
        """The poster ffmpeg made (its staging file is this module's to remove)."""
        if self.poster is not None and self.poster_source is MediaSource.EXTRACTED:
            return self.poster.staged
        return None


async def _dimensions(staged: Staged, posters: PosterExtractor) -> probe.Dimensions:
    if staged.kind is MediaKind.VIDEO:
        return await posters.probe_video(staged.path)
    return await probe.dimensions(staged.path, staged.kind)


async def _moderate(
    staged: Staged,
    request: MediaUpload,
    found: probe.Dimensions,
    *,
    owner: str,
    settings: MediaSettings,
    signer: Signer,
) -> None:
    hook = settings.media_moderation_webhook_url
    if hook is None:
        return
    path, _ = signer.sign_path(content_path(staged.asset_id))
    base = (settings.public_base_url or "").rstrip("/")
    payload = _hook_payload(staged, request, found, owner=owner, content_url=base + path)
    await moderation.moderate(
        hook, payload, signer=signer, timeout_ms=settings.media_moderation_timeout_ms
    )


async def extract_poster(store: BlobStore, posters: PosterExtractor, video: Staged) -> Staged:
    """The video's first frame as a staged PNG image (the poster asset's own id)."""
    poster_id = new_ulid()
    out = store.staging_path(poster_id)
    await posters.extract(video.path, out)
    try:
        sha256, size, head = await asyncio.to_thread(_digest_file, out)
        if probe.sniff(head) != POSTER_MIME_TYPE:
            raise ValueError("ffmpeg wrote no PNG")
    except BaseException:
        await asyncio.to_thread(out.unlink, missing_ok=True)
        raise
    return Staged(poster_id, out, sha256, size, POSTER_MIME_TYPE, MediaKind.IMAGE)


async def prepare(
    db: AsyncEngine,
    store: BlobStore,
    staged: Staged,
    request: MediaUpload,
    *,
    principal: str,
    settings: MediaSettings,
    signer: Signer,
    posters: PosterExtractor,
    poster_file: Staged | None = None,
    poster_field: str = f"body.metadata.{POSTER_FIELD}",
) -> Prepared:
    """Steps 3's checks of the module docstring, before anything is stored: the rating (and a
    named poster), the probe, the hook and, for a video, its poster (given, sent, or extracted
    and sent to the hook too). Nothing is written; a poster this call extracted is removed when
    it fails."""
    async with db.begin() as conn:
        await rating_version(conn, request.content_rating_id)
        if request.poster is not None:
            await check_supplied_poster(conn, request.poster, field=poster_field)
    owner = request.owner or principal
    found = await _dimensions(staged, posters)
    await _moderate(staged, request, found, owner=owner, settings=settings, signer=signer)
    main = Probed(staged, found)
    if staged.kind is not MediaKind.VIDEO:
        return Prepared(request, owner, main, poster_field=poster_field)
    if request.poster is not None:
        return Prepared(request, owner, main, poster_id=request.poster, poster_field=poster_field)
    poster_meta = poster_request(request, staged.asset_id, owner=owner)
    if poster_file is not None:
        poster_found = await probe.dimensions(poster_file.path, MediaKind.IMAGE)
        await _moderate(
            poster_file, poster_meta, poster_found, owner=owner, settings=settings, signer=signer
        )
        return Prepared(
            request,
            owner,
            main,
            poster=Probed(poster_file, poster_found),
            poster_field=poster_field,
        )
    extracted = await extract_poster(store, posters, staged)
    try:
        poster_found = await probe.dimensions(extracted.path, MediaKind.IMAGE)
        await _moderate(
            extracted, poster_meta, poster_found, owner=owner, settings=settings, signer=signer
        )
    except BaseException:
        await discard(extracted)
        raise
    return Prepared(
        request,
        owner,
        main,
        poster=Probed(extracted, poster_found),
        poster_source=MediaSource.EXTRACTED,
        poster_field=poster_field,
    )


async def _insert(
    conn: AsyncConnection,
    probed: Probed,
    request: MediaUpload,
    *,
    owner: str,
    version_id: str,
    source: MediaSource,
    principal: str,
    poster_media_id: str | None,
) -> None:
    staged, found = probed.staged, probed.found
    # No new medium depicts a subject a catalog-scope erasure removed (FR-ADMIN-9; #127).
    await erasure_guard.assert_not_erased(
        conn,
        [s.id for s in request.depicted_subjects or []],
        field=f"body.metadata.{DECLARED_FIELD}",
        path=__name__,
    )
    await conn.execute(
        insert(media_assets).values(
            id=staged.asset_id,
            owner_principal=owner,
            kind=staged.kind.value,
            mime_type=staged.mime_type,
            size=staged.size,
            width=found.width,
            height=found.height,
            duration_ms=found.duration_ms,
            checksum=staged.sha256,
            content_rating_version_id=version_id,
            alt_text=request.alt_text,
            caption=request.caption,
            generation_metadata=request.stored_generation_metadata(),
            source=source.value,
            state=MediaState.LIVE.value,
            depicted_subjects=[s.model_dump(mode="json") for s in request.depicted_subjects or []],
            created_at=clock.now(),
            created_by=principal,
            poster_media_id=poster_media_id,
        )
    )
    subjects: list[Declared] = [
        (subject.kind, subject.id, DECLARED_FIELD) for subject in request.depicted_subjects or []
    ]
    await index_references(
        conn,
        RECORD_TABLE,
        staged.asset_id,
        story_id=None,
        fields=_indexed_fields(request),
        subjects=subjects,
    )


async def commit(
    conn: AsyncConnection,
    store: BlobStore,
    prepared: Prepared,
    *,
    principal: str,
    assembled: str | None = None,
) -> str:
    """Step 4 of the module docstring in ``conn``'s transaction: the rows (the poster's first),
    their subject index, one ``control`` operation, then the bytes; the asset's id.
    ``assembled`` names the upload session whose assembled object the store copies (S3)."""
    request, main, poster = prepared.request, prepared.main, prepared.poster
    checksums = {main.staged.sha256} | ({poster.staged.sha256} if poster is not None else set())
    for checksum in sorted(checksums):
        await lock_checksum(conn, checksum)
    version_id = await rating_version(conn, request.content_rating_id)
    poster_id = prepared.poster_id
    if poster_id is not None:
        await check_supplied_poster(conn, poster_id, field=prepared.poster_field)
    if poster is not None:
        poster_id = poster.staged.asset_id
        await _insert(
            conn,
            poster,
            poster_request(request, main.staged.asset_id, owner=prepared.owner),
            owner=prepared.owner,
            version_id=version_id,
            source=prepared.poster_source,
            principal=principal,
            poster_media_id=None,
        )
    await _insert(
        conn,
        main,
        request,
        owner=prepared.owner,
        version_id=version_id,
        source=MediaSource.UPLOADED,
        principal=principal,
        poster_media_id=poster_id if main.staged.kind is MediaKind.VIDEO else None,
    )
    await control.record_control_operation(conn, actor=principal)
    # Last, so a refused statement above leaves the bytes staged (and then discarded).
    if poster is not None:
        await asyncio.to_thread(
            store.put,
            poster.staged.path,
            poster.staged.sha256,
            content_type=poster.staged.mime_type,
        )
    await asyncio.to_thread(
        store.put,
        main.staged.path,
        main.staged.sha256,
        content_type=main.staged.mime_type,
        assembled=assembled,
    )
    # Until this commits, the checksum's lock keeps the sweep from deleting the blob ``put``
    # may have kept rather than written (identical bytes a purge left behind).
    await pauses.pause(pauses.MEDIA_UPLOAD_BEFORE_COMMIT)
    return main.staged.asset_id


async def upload(
    db: AsyncEngine,
    store: BlobStore,
    staged: Staged,
    request: MediaUpload,
    *,
    principal: str,
    settings: MediaSettings,
    signer: Signer,
    posters: PosterExtractor,
    poster_file: Staged | None = None,
) -> MediaWrite:
    """Store a staged upload as a ``live`` asset (steps 3 and 4 of the module docstring):
    422 ``rating_retired``; 422 ``moderation_rejected``, 503 ``moderation_unavailable`` or 503
    ``config_key_missing`` from the hook; a video's poster refusals; nothing is stored on any
    refusal."""
    request = check_upload(request)
    prepared = await prepare(
        db,
        store,
        staged,
        request,
        principal=principal,
        settings=settings,
        signer=signer,
        posters=posters,
        poster_file=poster_file,
    )
    try:
        async with db.begin() as conn:
            media_id = await commit(conn, store, prepared, principal=principal)
            asset = await read_asset(conn, media_id)
            revision = await config_revision(conn)
    finally:
        await discard(prepared.extracted)
    return MediaWrite(asset=asset, config_revision=revision)


# -- reads -----------------------------------------------------------------------------------------


async def get(db: AsyncEngine, media_id: str) -> MediaAsset:
    """The asset's metadata (``live`` or ``retired``); 404 otherwise."""
    async with db.connect() as conn:
        return await read_asset(conn, media_id)


def _staged_head(path: Path) -> tuple[bytes, int]:
    with path.open("rb") as handle:
        head = handle.read(probe.SNIFF_BYTES)
        return head, os.fstat(handle.fileno()).st_size


def _etag_matches(candidates: Sequence[str], etag: str | None) -> bool:
    """``If-None-Match``'s weak comparison: ``*`` matches any current tag, else the opaque
    tags compare with any ``W/`` dropped."""
    if etag is None:
        return False
    bare = etag.removeprefix("W/")
    return any(value == "*" or value.removeprefix("W/") == bare for value in candidates)


def _open_window(
    opener: Callable[[], ByteStream],
    ranged: Callable[[int, int], ByteStream],
    window: tuple[int, int] | None,
) -> ByteStream:
    return opener() if window is None else ranged(window[0], window[1])


async def open_content(
    db: AsyncEngine,
    store: BlobStore,
    media_id: str,
    *,
    signed: bool = False,
    byte_range: ByteRange | None = None,
    if_none_match: Sequence[str] = (),
    if_range: str | None = None,
    with_bytes: bool = True,
) -> MediaContent:
    """The asset's bytes, opened (a ``live`` or ``retired`` asset), all of them or the one
    ``byte_range`` (applied only when ``if_range`` is absent or names the current entity tag;
    416 ``range_not_satisfiable`` when no byte of it exists). A matching ``if_none_match``
    answers ``not_modified`` first. With ``signed`` (the request carried a valid signed URL for
    this asset) an upload still waiting for the moderation hook is served from staging too.
    ``with_bytes`` false (``HEAD``) opens nothing. 404 otherwise."""
    async with db.connect() as conn:
        row = (
            await conn.execute(
                select(
                    media_assets.c.mime_type,
                    media_assets.c.size,
                    media_assets.c.checksum,
                    media_assets.c.state,
                ).where(media_assets.c.id == media_id)
            )
        ).one_or_none()
    staged_path: Path | None = None
    if row is not None and row.state != MediaState.CANDIDATE.value:
        mime_type, total, checksum = str(row.mime_type), int(row.size), str(row.checksum)
    elif row is None and signed:
        staged_path = store.staging_path(media_id)
        try:
            head, total = await asyncio.to_thread(_staged_head, staged_path)
        except FileNotFoundError:
            raise _not_found(media_id) from None
        mime_type, checksum = probe.sniff(head) or UNSPECIFIED_TYPE, None
    else:
        raise _not_found(media_id)
    content = MediaContent(media_id, mime_type, total, checksum, total=total)
    if if_none_match and _etag_matches(if_none_match, content.etag):
        content.not_modified = True
        content.size = 0
        return content
    window: tuple[int, int] | None = None
    if byte_range is not None and (if_range is None or if_range == content.etag):
        window = byte_range.resolve(total)
        if window is None:
            raise RangeNotSatisfiable(
                f"no byte of the requested range exists in {total} bytes",
                {"media_id": media_id, "size": total},
            )
        content.start, content.end = window
        content.size = window[1] - window[0] + 1
    if not with_bytes:
        return content
    if staged_path is not None:
        path = staged_path

        def local() -> ByteStream:
            return path.open("rb")

        def local_range(start: int, end: int) -> ByteStream:
            return Window(path.open("rb"), start, end - start + 1)

        opener, ranged = local, local_range
    else:
        assert checksum is not None  # noqa: S101 - a committed asset has its checksum
        digest = checksum

        def blob() -> ByteStream:
            return store.open(digest)

        def blob_range(start: int, end: int) -> ByteStream:
            return store.open_range(digest, start, end)

        opener, ranged = blob, blob_range
    try:
        content.handle = await asyncio.to_thread(_open_window, opener, ranged, window)
    except FileNotFoundError:
        raise MediaNotFound(
            f"the bytes of media asset {media_id} are missing",
            {"media_id": media_id, "reason": "bytes_missing"},
        ) from None
    return content


async def signed_url(
    db: AsyncEngine,
    store: BlobStore,
    media_id: str,
    *,
    ttl_s: int | None,
    settings: MediaSettings,
    signer: Signer,
) -> MediaUrl:
    """``GET /media/{id}/url`` (FR-MEDIA-3): a URL that serves the asset's bytes without the
    service credential for ``ttl_s`` seconds (the setting ``media_signed_url_ttl`` when
    ``None``, never longer): the store's own presigned URL (S3), or Epistrel's signed content
    URL (local: 503 ``config_key_missing`` or ``config_missing`` for ``public_base_url``).
    404 for an asset that is not ``live`` or ``retired``."""
    limit = settings.media_signed_url_ttl
    if ttl_s is not None and ttl_s < 1:
        raise InvalidRequest("ttl must be at least 1 second", {"field": "query.ttl"})
    seconds = limit if ttl_s is None else min(ttl_s, limit)
    async with db.connect() as conn:
        asset = await read_asset(conn, media_id)
    started = datetime.now(UTC)
    presigned = await asyncio.to_thread(store.presign, asset.checksum, seconds)
    if presigned is not None:
        return MediaUrl(url=presigned, expires_at=started + timedelta(seconds=seconds))
    signer.require("EPISTREL_CONFIG_KEY is not set, so no signed media URL can be made")
    base = settings.public_base_url
    if base is None:
        raise ConfigMissing(
            "public_base_url is not set, so a signed media URL has no base to start with",
            {"setting": "public_base_url"},
        )
    path, exp = signer.sign_path(content_path(media_id), ttl_s=seconds)
    return MediaUrl(url=base.rstrip("/") + path, expires_at=datetime.fromtimestamp(exp, UTC))


def _encode_cursor(asset: MediaAsset) -> str:
    raw = json.dumps([asset.created_at.isoformat(), asset.id]).encode()
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _decode_cursor(cursor: str) -> tuple[datetime, str]:
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        created_at, media_id = json.loads(raw)
        moment = datetime.fromisoformat(created_at)
        if not (isinstance(media_id, str) and re.fullmatch(ULID_PATTERN, media_id)):
            raise ValueError("not an id")
        if moment.tzinfo is None:
            raise ValueError("no time zone")
    except ValueError, TypeError, binascii.Error:
        raise InvalidRequest("not a media cursor", {"field": "query.cursor"}) from None
    return moment, media_id


async def list_assets(
    db: AsyncEngine,
    *,
    owner: str | None = None,
    relations: Sequence[RelationFilter] = (),
    kind: MediaKind | None = None,
    state: MediaState = MediaState.LIVE,
    limit: int = DEFAULT_PAGE,
    cursor: str | None = None,
) -> MediaPage:
    """A page of ``state`` assets (``live`` by default, ``retired`` when asked; never a
    ``candidate``), newest first, filtered by owner, relations and kind only when asked
    (FR-AUTH-8)."""
    if state is MediaState.CANDIDATE:
        raise InvalidRequest("candidates are not listed", {"field": "query.state"})
    if not 1 <= limit <= MAX_PAGE:
        raise InvalidRequest(f"limit must be 1 to {MAX_PAGE}", {"field": "query.limit"})
    where = [
        media_assets.c.state == state.value,
        *ownership.list_filters(
            EntityType.MEDIA,
            media_assets.c.id,
            media_assets.c.owner_principal,
            owner=owner,
            relations=relations,
        ),
    ]
    if kind is not None:
        where.append(media_assets.c.kind == kind.value)
    if cursor is not None:
        moment, after = _decode_cursor(cursor)
        where.append(tuple_(media_assets.c.created_at, media_assets.c.id) < tuple_(moment, after))
    query = (
        select(*_COLUMNS)
        .select_from(_FROM)
        .where(and_(*where))
        .order_by(media_assets.c.created_at.desc(), media_assets.c.id.desc())
        .limit(limit + 1)
    )
    async with db.connect() as conn:
        rows = (await conn.execute(query)).all()
    items = [_asset(row) for row in rows[:limit]]
    following = _encode_cursor(items[-1]) if len(rows) > limit and items else None
    return MediaPage(items=items, next_cursor=following)


# -- retire --------------------------------------------------------------------------------------


async def retire(db: AsyncEngine, media_id: str, *, principal: str) -> MediaRetirement:
    """Delete-as-retire (FR-MEDIA-2; see the module docstring): ``live`` becomes ``retired``,
    and so does a video's ``live`` poster, recorded by one ``control`` operation; a retired
    asset is left as it is (``changed`` false, nothing recorded); 404 for an unknown asset or a
    candidate."""
    async with db.begin() as conn:
        row = (
            await conn.execute(
                select(media_assets.c.state, media_assets.c.poster_media_id)
                .where(media_assets.c.id == media_id)
                .with_for_update()
            )
        ).one_or_none()
        if row is None or row.state == MediaState.CANDIDATE.value:
            raise _not_found(media_id)
        changed = row.state == MediaState.LIVE.value
        if changed:
            retired = [media_id]
            if row.poster_media_id is not None:
                retired.append(str(row.poster_media_id))
            await conn.execute(
                update(media_assets)
                .where(
                    media_assets.c.id.in_(retired),
                    media_assets.c.state == MediaState.LIVE.value,
                )
                .values(state=MediaState.RETIRED.value)
            )
            await control.record_control_operation(conn, actor=principal)
        revision = await config_revision(conn)
    return MediaRetirement(id=media_id, changed=changed, config_revision=revision)


async def assert_referenceable(
    conn: AsyncConnection, media_ids: Sequence[str], *, field: str = "media_id"
) -> None:
    """The check every write that adds a reference to an asset makes, in its own transaction
    (#107's posters, #108, #115, #119): each asset must be ``live``. Holds them ``FOR SHARE``,
    so none can be retired or purged before the write commits. 422 ``media_retired``
    (``reason`` ``retired``, ``unknown`` or ``candidate``) at ``field`` (``field[i]`` for
    several) otherwise."""
    wanted = list(dict.fromkeys(media_ids))
    if not wanted:
        return
    rows = await conn.execute(
        select(media_assets.c.id, media_assets.c.state)
        .where(media_assets.c.id.in_(wanted))
        .with_for_update(read=True)
    )
    states = {str(row.id): str(row.state) for row in rows}
    for index, media_id in enumerate(media_ids):
        state = states.get(media_id)
        if state == MediaState.LIVE.value:
            continue
        reason = "unknown" if state is None else state
        where = field if len(media_ids) == 1 else f"{field}[{index}]"
        raise MediaRetired(
            f"media asset {media_id} cannot be referenced ({reason})",
            {"media_id": media_id, "field": where, "reason": reason},
        )


# -- reference discovery --------------------------------------------------------------------

ANY_STRING_PATH = 'strict $.** ? (@.type() == "string")'
"""Every string anywhere in a document: an asset id wherever a later story puts it."""
_IDENTIFIER = re.compile(r"^[a-z_][a-z0-9_]*$")


@dataclass(frozen=True, slots=True)
class ReferenceSource:
    """A column whose values may name assets: a ``jsonb`` document whose media ids are the
    strings ``path`` (a SQL/JSON path) yields, or, with ``path`` ``None``, a column that holds
    one asset id itself (``media_assets.poster_media_id``)."""

    table: str
    """``schema.table``."""
    column: str
    path: str | None = ANY_STRING_PATH

    def __post_init__(self) -> None:
        parts = self.table.split(".")
        if len(parts) != 2 or not all(_IDENTIFIER.fullmatch(p) for p in (*parts, self.column)):
            raise ValueError(f"not a schema-qualified table and column: {self.table}.{self.column}")

    @property
    def quoted(self) -> str:
        schema, table = self.table.split(".")
        return f'"{schema}"."{table}"'


MEDIA_REFERENCE_SOURCES: list[ReferenceSource] = [
    ReferenceSource("catalog.character_catalog", "draft"),
    ReferenceSource("catalog.character_versions", "content"),
    ReferenceSource(f"public.{RECORD_TABLE}", "poster_media_id", None),
    ReferenceSource("catalog.scenario_catalog", "draft"),
    ReferenceSource("catalog.scenario_versions", "content"),
    ReferenceSource("public.characters", "images"),
    ReferenceSource("public.character_snapshots", "content"),
    ReferenceSource("public.stories", "scenario_snapshot"),
]
"""Where references to assets live (FR-MEDIA-2): catalog character and scenario drafts and
published versions (#108: a scenario's cover, gallery and videos), a video's poster (#107), the
images of a character added in a story (``characters.images``, #114: an asset a story shows
survives its retirement until the story is deleted), and a story's genesis snapshots (#119,
#316): the catalog character versions in ``character_snapshots.content`` and the scenario
version in ``stories.scenario_snapshot``, so an asset a story was created with survives its
catalog entry's deletion until the story is deleted. M8 (candidates, story images) registers
its own. A source whose table does not exist (yet) is
skipped; one whose column does not exist fails the query, so a source is registered only once
its column exists."""


def register_reference_source(source: ReferenceSource) -> None:
    """Add ``source`` to ``MEDIA_REFERENCE_SOURCES`` (once)."""
    if source not in MEDIA_REFERENCE_SOURCES:
        MEDIA_REFERENCE_SOURCES.append(source)


async def _present_sources(conn: AsyncConnection) -> list[ReferenceSource]:
    """The sources of ``MEDIA_REFERENCE_SOURCES`` whose table exists."""
    present: list[ReferenceSource] = []
    for source in MEDIA_REFERENCE_SOURCES:
        exists = await conn.scalar(
            sql_text("SELECT to_regclass(CAST(:t AS text)) IS NOT NULL"), {"t": source.table}
        )
        if exists:
            present.append(source)
    return present


def _values(source: ReferenceSource, path_param: str) -> tuple[str, str]:
    """``(FROM clause, value expression)`` that lists the asset ids ``source`` names: rows of
    ``t``, joined to the path's items for a document."""
    if source.path is None:
        return f"{source.quoted} AS t", f't."{source.column}"'
    return (
        f'{source.quoted} AS t CROSS JOIN LATERAL jsonb_path_query(t."{source.column}", '
        f"CAST(:{path_param} AS jsonpath)) AS v(item)",
        "v.item #>> '{}'",
    )


async def referenced(conn: AsyncConnection, media_ids: Sequence[str]) -> set[str]:
    """Which of ``media_ids`` some record of ``MEDIA_REFERENCE_SOURCES`` names, found by
    querying the records (never by a counter)."""
    wanted = list(dict.fromkeys(media_ids))
    found: set[str] = set()
    if not wanted:
        return found
    for source in await _present_sources(conn):
        source_from, value = _values(source, "path")
        params: dict[str, Any] = {"ids": wanted}
        if source.path is not None:
            params["path"] = source.path
        hits = await conn.scalars(
            sql_text(
                f"SELECT DISTINCT CAST({value} AS text) FROM {source_from} "  # noqa: S608
                f"WHERE CAST({value} AS text) = ANY(CAST(:ids AS text[]))"
            ),
            params,
        )
        found.update(str(hit) for hit in hits)
    return found


@dataclass(frozen=True, slots=True)
class MediaReference:
    """One record that names an asset (``referenced``'s finding, with who holds it): the
    owned entity the record belongs to (its type and id, as ``core.ownership`` names them) and
    that entity's owner. ``None`` for a record no owned entity holds."""

    media_id: str
    entity_type: str | None
    entity_id: str | None
    owner: str | None


REFERENCE_HOLDERS: dict[str, tuple[str, str, str]] = {
    "catalog.character_catalog": ("character", "t.id", "t.owner_principal"),
    "catalog.character_versions": (
        "character",
        "t.catalog_id",
        "(SELECT c.owner_principal FROM catalog.character_catalog AS c WHERE c.id = t.catalog_id)",
    ),
    "catalog.scenario_catalog": ("scenario", "t.id", "t.owner_principal"),
    "catalog.scenario_versions": (
        "scenario",
        "t.catalog_id",
        "(SELECT c.owner_principal FROM catalog.scenario_catalog AS c WHERE c.id = t.catalog_id)",
    ),
    f"public.{RECORD_TABLE}": ("media", "t.id", "t.owner_principal"),
    "public.characters": (
        "story",
        "t.story_id",
        "(SELECT s.owner_principal FROM public.stories AS s WHERE s.id = t.story_id)",
    ),
    "public.character_snapshots": (
        "story",
        "t.story_id",
        "(SELECT s.owner_principal FROM public.stories AS s WHERE s.id = t.story_id)",
    ),
    "public.stories": ("story", "t.id", "t.owner_principal"),
}
"""For each reference source's table: the owned entity a record belongs to and the SQL of its
id and owner (``t`` is the record). #117's offboarding reads them: media referenced by an entry
it transfers move with it, and media it would retire that another owner's entry references are
warned of."""


async def references(conn: AsyncConnection, media_ids: Sequence[str]) -> list[MediaReference]:
    """Every record of ``MEDIA_REFERENCE_SOURCES`` that names one of ``media_ids``, with the
    owned entity it belongs to and that entity's owner (``REFERENCE_HOLDERS``; ``None`` for a
    source no entry describes). Found by querying the records, like ``referenced``."""
    wanted = list(dict.fromkeys(media_ids))
    found: set[MediaReference] = set()
    if not wanted:
        return []
    for source in await _present_sources(conn):
        source_from, value = _values(source, "path")
        holder = REFERENCE_HOLDERS.get(source.table)
        entity_type, entity_id, owner = (
            (f"'{holder[0]}'", holder[1], holder[2]) if holder else ("NULL", "NULL", "NULL")
        )
        params: dict[str, Any] = {"ids": wanted}
        if source.path is not None:
            params["path"] = source.path
        rows = await conn.execute(
            sql_text(
                f"SELECT DISTINCT CAST({value} AS text) AS media_id, "  # noqa: S608
                f"CAST({entity_type} AS text) AS entity_type, "
                f"CAST({entity_id} AS text) AS entity_id, CAST({owner} AS text) AS owner "
                f"FROM {source_from} WHERE CAST({value} AS text) = ANY(CAST(:ids AS text[]))"
            ),
            params,
        )
        found.update(
            MediaReference(
                str(row.media_id),
                row.entity_type,
                None if row.entity_id is None else str(row.entity_id).strip(),
                row.owner,
            )
            for row in rows
        )
    return sorted(
        found, key=lambda r: (r.media_id, r.entity_type or "", r.entity_id or "", r.owner or "")
    )


async def unreferenced(conn: AsyncConnection) -> ColumnElement[bool]:
    """A condition on ``media_assets`` rows that holds when no record of
    ``MEDIA_REFERENCE_SOURCES`` names the asset: ``referenced`` as an anti-join, so a query can
    leave the referenced assets out before it limits its rows (the media sweep's batches).
    The sources whose table exists are read now; the condition can be reused for a run."""
    clauses: list[TextClause] = []
    for index, source in enumerate(await _present_sources(conn)):
        param = f"reference_path_{index}"
        source_from, value = _values(source, param)
        clause = sql_text(
            f"NOT EXISTS (SELECT 1 FROM {source_from} "  # noqa: S608
            f"WHERE CAST({value} AS text) = CAST({RECORD_TABLE}.id AS text))"
        )
        if source.path is not None:
            clause = clause.bindparams(**{param: source.path})
        clauses.append(clause)
    return and_(true(), *clauses)


__all__ = [
    "ANY_STRING_PATH",
    "ENVELOPE_MAX_BYTES",
    "INDEXED_FIELDS",
    "MEDIA_REFERENCE_SOURCES",
    "METADATA_MAX_BYTES",
    "POSTER_FILE_FIELD",
    "RECORD_TABLE",
    "REFERENCE_HOLDERS",
    "ByteRange",
    "DepictedSubject",
    "GenerationMetadata",
    "MediaAsset",
    "MediaContent",
    "MediaPage",
    "MediaReference",
    "MediaRetirement",
    "MediaUpload",
    "MediaUrl",
    "MediaWrite",
    "PosterFileSource",
    "Prepared",
    "Probed",
    "ReferenceSource",
    "Staged",
    "UploadFile",
    "assert_referenceable",
    "check_declared_type",
    "check_supplied_poster",
    "check_type",
    "check_upload",
    "commit",
    "content_of",
    "content_path",
    "discard",
    "document_subjects_required",
    "extract_poster",
    "get",
    "list_assets",
    "lock_checksum",
    "open_content",
    "parse_metadata",
    "poster_file_of",
    "poster_request",
    "prepare",
    "rating_version",
    "read_asset",
    "read_media_settings",
    "referenced",
    "references",
    "register_reference_source",
    "require_subjects",
    "retire",
    "signed_url",
    "stage",
    "too_large",
    "unreferenced",
    "upload",
]
