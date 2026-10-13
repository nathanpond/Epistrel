"""Media assets: upload, the moderation hook, reads, retirement and reference discovery
(FR-MEDIA-1,2, FR-ADMIN-6, FR-API-6; docs/02 §4.10; #105).

**Upload** (``POST /media``; ``Engine.upload_media``) takes the asset's metadata (``MediaUpload``)
and its bytes:

1. The metadata is validated first: ``kind``, ``content_rating_id`` (omitted: 422
   ``validation_error``), optional ``alt_text`` (≤ 500), ``caption`` (≤ 2000),
   ``generation_metadata`` (FR-CAT-4's fields, stored exactly as sent) and ``owner``, and the
   ``depicted_subjects`` every upload must declare (FR-MEDIA-1, FR-IMG-9): an omitted list is 422
   ``subjects_required``, an explicitly empty one is accepted. Subjects are ``{kind, id}``
   (``character``, ``catalog_character`` or ``persona``), stored unverified (the subject may be
   created later), deduplicated, at most 50.
2. The bytes stream into the staging area (``core.media.store``) while they are hashed and
   counted: a request whose declared length exceeds ``media_max_bytes`` plus the metadata's
   allowance is 413 ``media_too_large`` before the file is read, and so is a file that grows
   past ``media_max_bytes``. The type is sniffed from the first bytes (``core.media.probe``):
   one the deployment does not allow (``media_allowed_types``), one Epistrel does not
   recognise, or one that disagrees with the declared ``kind`` or the file's own MIME type is
   415 ``media_type_not_allowed``.
3. The call then takes its ``Idempotency-Key`` (the request hash covers the metadata and the
   bytes' SHA-256), checks the rating (422 ``rating_retired`` for an unknown or retired one,
   #50), probes the dimensions or duration, and, when ``media_moderation_webhook_url`` is set,
   asks the moderation hook (``core.media.moderation``; fails closed). Nothing is stored before
   the hook accepts, and a refusal leaves no row and no blob.
4. One transaction, under the checksum's advisory lock, inserts the row (``live``, ``source =
   uploaded``, the rating's current version), indexes its subjects (``core.erasure.index``:
   the declared subjects and the catalog characters its caption, alt text and prompts name),
   records a committed ``control`` operation (``core.control``) and moves the staged bytes into
   place. Identical bytes share one blob.

**Reads.** ``get`` returns the metadata of a ``live`` or ``retired`` asset (a ``candidate`` is
private, FR-IMG-7: 404). ``open_content`` opens its bytes; with a valid signed URL
(``exp``/``sig``, ``core.media.signing``) it also serves an upload still waiting for the hook,
which is how the hook's ``content_url`` works before anything is committed. ``list_assets``
lists ``live`` assets (or ``retired`` ones when asked), newest first.

**Delete is retire** (FR-MEDIA-2): ``retire`` sets ``state = retired``. A retired asset leaves
listings and refuses new references (``assert_referenceable``, which #108, #115 and #119 call:
422 ``media_retired``), while the records that already reference it keep reading it. The media
sweep (``core.media.sweep``) purges it once nothing references it. **Reference discovery**
queries the records that can reference an asset (``MEDIA_REFERENCE_SOURCES``, which later
stories extend with ``register_reference_source``); nothing counts references.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import json
import os
import re
from collections.abc import AsyncIterable, AsyncIterator, Sequence
from dataclasses import dataclass, field
from datetime import datetime
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
from sqlalchemy import Integer, and_, cast, func, insert, select, tuple_, update
from sqlalchemy import text as sql_text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from epistrel.core import clock, control, ownership
from epistrel.core.config_changes import read_section
from epistrel.core.erasure.index import Declared, index_references
from epistrel.core.errors import (
    InvalidRequest,
    MediaNotFound,
    MediaRetired,
    MediaTooLarge,
    MediaTypeNotAllowed,
    SubjectsRequired,
    refuse_nul,
)
from epistrel.core.ids import ULID_PATTERN
from epistrel.core.media import moderation, probe
from epistrel.core.media.signing import Signer
from epistrel.core.media.store import LocalFilesystemStore
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

Ulid = Annotated[str, Field(pattern=ULID_PATTERN)]


# -- the request ----------------------------------------------------------------------------------


class DepictedSubject(BaseModel):
    """A subject an image or video depicts (FR-IMG-9): a story ``character``, a
    ``catalog_character`` or a ``persona``, by id. Stored as declared, unverified (the subject
    may be created later), and indexed so that erasure finds the asset (FR-ADMIN-6)."""

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
    prompt, the seed, the parameters (a JSON object of at most 8 KiB) and the ids of the
    reference images. Stored and returned exactly as sent: a key that was not sent is not
    returned."""

    model_config = ConfigDict(extra="forbid")

    adapter: str | None = Field(default=None, max_length=GENERATION_NAME_MAX)
    model: str | None = Field(default=None, max_length=GENERATION_NAME_MAX)
    prompt: str | None = Field(default=None, max_length=PROMPT_MAX)
    negative_prompt: str | None = Field(default=None, max_length=PROMPT_MAX)
    seed: int | None = Field(default=None, ge=0, le=SEED_MAX)
    parameters: Annotated[dict[str, Any] | None, AfterValidator(_parameters_bounded)] = None
    reference_ids: list[Ulid] | None = Field(default=None, max_length=REFERENCE_IDS_MAX)


def _deduplicated(value: list[DepictedSubject] | None) -> list[DepictedSubject] | None:
    return None if value is None else list(dict.fromkeys(value))


def _subjects_required(schema: dict[str, Any]) -> None:
    """Document ``depicted_subjects`` as the required list it is (omitting it is refused with
    ``subjects_required`` rather than pydantic's ``missing``)."""
    schema.setdefault("required", []).append(DECLARED_FIELD)
    prop = schema["properties"][DECLARED_FIELD]
    options = [o for o in prop.pop("anyOf", []) if o.get("type") != "null"]
    if options:
        prop.update(options[0])
    prop.pop("default", None)
    schema["examples"] = [
        {
            "kind": "image",
            "content_rating_id": "00000000000000000000RAT001",
            "alt_text": "Mara at the lighthouse door",
            "caption": "Gull Rock, the night of the storm.",
            "depicted_subjects": [
                {"kind": "catalog_character", "id": "01J9Z8Q4N5V6W7X8Y9Z0A1B2C5"}
            ],
        }
    ]


class MediaUpload(BaseModel):
    """``POST /media``'s ``metadata`` part (FR-MEDIA-1): the asset's ``kind`` (which its bytes
    must be), its content rating, optional alt text, caption and generation metadata, the
    subjects it depicts (required: ``[]`` declares none) and, optionally, its ``owner`` (default:
    the requesting principal; never ``system``)."""

    model_config = ConfigDict(extra="forbid", json_schema_extra=_subjects_required)

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
    return require_subjects(request)


def require_subjects(request: MediaUpload) -> MediaUpload:
    """``request`` if it declared its subjects; 422 ``subjects_required`` otherwise."""
    if request.depicted_subjects is None:
        raise SubjectsRequired(
            "an upload must declare the subjects it depicts: `depicted_subjects` "
            "(an empty list declares none)",
            {"field": f"body.metadata.{DECLARED_FIELD}"},
        )
    return request


# -- results ---------------------------------------------------------------------------------------


class MediaAsset(BaseModel):
    """A media asset's metadata (FR-MEDIA-1): its owner, kind, MIME type (sniffed from the
    bytes), size, dimensions or duration (``null`` when no probe could read them), SHA-256
    checksum, content rating and the rating's version it was uploaded under, alt text, caption,
    generation metadata (as sent), source, state (``live``, or ``retired`` once deleted), the
    subjects it depicts, and who created it when."""

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


@dataclass
class MediaContent:
    """An asset's bytes, opened: their MIME type, size and checksum (``None`` for an upload
    still waiting for the moderation hook). Read them with ``chunks()`` or ``read()``; either
    closes the file at the end, and ``aclose()`` closes it early."""

    media_id: str
    mime_type: str
    size: int
    checksum: str | None
    handle: BinaryIO = field(repr=False)

    async def chunks(self) -> AsyncIterator[bytes]:
        try:
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
        if not self.handle.closed:
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


def _declared(content_type: str | None) -> str | None:
    """A file part's own MIME type without parameters, or ``None`` when it declares none."""
    if content_type is None:
        return None
    value = content_type.split(";", 1)[0].strip().lower()
    value = MIME_ALIASES.get(value, value)
    return None if value in ("", UNSPECIFIED_TYPE) else value


def check_type(
    head: bytes, settings: MediaSettings, kind: MediaKind, content_type: str | None
) -> str:
    """The sniffed MIME type of bytes starting ``head``; 415 ``media_type_not_allowed`` unless
    it is recognised, allowed, and agrees with ``kind`` and the declared type."""
    found = probe.sniff(head)
    declared = _declared(content_type)
    allowed = list(settings.media_allowed_types)

    def refused(reason: str, message: str) -> MediaTypeNotAllowed:
        return MediaTypeNotAllowed(
            message,
            {"reason": reason, "mime_type": found, "declared": declared, "allowed": allowed},
        )

    if found is None:
        raise refused("unrecognized", "the file is not a type Epistrel recognises")
    if found not in allowed:
        raise refused("not_allowed", f"{found} is not an allowed media type")
    if declared is not None and declared != found:
        raise refused("declared_mismatch", f"the file is {found}, not {declared}")
    if probe.kind_of(found) is not kind:
        raise refused("kind_mismatch", f"the file is {found}, not an {kind.value}")
    return found


def _too_large(size: int, limit: int) -> MediaTooLarge:
    return MediaTooLarge(
        f"the upload is larger than the {limit}-byte limit", {"size": size, "limit": limit}
    )


def _finish(handle: BinaryIO) -> None:
    handle.flush()
    os.fsync(handle.fileno())
    handle.close()


def _abandon(handle: BinaryIO, path: Path) -> None:
    handle.close()
    path.unlink(missing_ok=True)


async def stage(
    store: LocalFilesystemStore,
    asset_id: str,
    content: AsyncIterable[bytes],
    *,
    settings: MediaSettings,
    kind: MediaKind,
    content_type: str | None = None,
    body_size: int | None = None,
) -> Staged:
    """Stream ``content`` into the staging file of ``asset_id``, hashing and counting it, and
    check its type and size as it arrives (see the module docstring). ``body_size`` is the whole
    request's declared length (REST's ``Content-Length``). On any refusal or error the staging
    file is removed."""
    limit = settings.media_max_bytes
    if body_size is not None and body_size > limit + ENVELOPE_MAX_BYTES:
        raise _too_large(body_size, limit)
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
                raise _too_large(size, limit)
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


async def discard(staged: Staged) -> None:
    """Remove the staged file if it is still there (a refusal, a replay, an error)."""
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
)
_FROM = media_assets.outerjoin(
    content_rating_versions,
    content_rating_versions.c.version_id == media_assets.c.content_rating_version_id,
)


def _asset(row: Any) -> MediaAsset:
    return MediaAsset.model_validate(dict(row._mapping))


def _not_found(media_id: str) -> MediaNotFound:
    return MediaNotFound(f"media asset {media_id} does not exist", {"media_id": media_id})


async def _read(conn: AsyncConnection, media_id: str) -> MediaAsset:
    row = (
        await conn.execute(
            select(*_COLUMNS).select_from(_FROM).where(media_assets.c.id == media_id)
        )
    ).one_or_none()
    if row is None or row.state == MediaState.CANDIDATE.value:
        raise _not_found(media_id)
    return _asset(row)


async def _rating_version(conn: AsyncConnection, rating_id: str) -> str:
    """The current version of ``rating_id`` if it can be assigned (held ``FOR SHARE``); 422
    ``rating_retired`` (``reason`` unknown or retired) otherwise."""
    await require_assignable(conn, rating_id, field="metadata.content_rating_id")
    version: str = (
        await conn.execute(
            select(content_ratings.c.current_version_id).where(content_ratings.c.id == rating_id)
        )
    ).scalar_one()
    return version


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


async def upload(
    db: AsyncEngine,
    store: LocalFilesystemStore,
    staged: Staged,
    request: MediaUpload,
    *,
    principal: str,
    settings: MediaSettings,
    signer: Signer,
) -> MediaWrite:
    """Store a staged upload as a ``live`` asset (steps 3 and 4 of the module docstring):
    422 ``rating_retired``; 422 ``moderation_rejected``, 503 ``moderation_unavailable`` or 503
    ``config_key_missing`` from the hook; nothing is stored on any refusal."""
    request = require_subjects(request)
    async with db.begin() as conn:
        await _rating_version(conn, request.content_rating_id)
    found = await probe.dimensions(staged.path, staged.kind)
    owner = request.owner or principal
    hook = settings.media_moderation_webhook_url
    if hook is not None:
        path, _ = signer.sign_path(content_path(staged.asset_id))
        base = (settings.public_base_url or "").rstrip("/")
        payload = _hook_payload(staged, request, found, owner=owner, content_url=base + path)
        await moderation.moderate(
            hook, payload, signer=signer, timeout_ms=settings.media_moderation_timeout_ms
        )
    subjects: list[Declared] = [
        (subject.kind, subject.id, DECLARED_FIELD) for subject in request.depicted_subjects or []
    ]
    async with db.begin() as conn:
        await lock_checksum(conn, staged.sha256)
        version_id = await _rating_version(conn, request.content_rating_id)
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
                source=MediaSource.UPLOADED.value,
                state=MediaState.LIVE.value,
                depicted_subjects=[
                    s.model_dump(mode="json") for s in request.depicted_subjects or []
                ],
                created_at=clock.now(),
                created_by=principal,
            )
        )
        await index_references(
            conn,
            RECORD_TABLE,
            staged.asset_id,
            story_id=None,
            fields=_indexed_fields(request),
            subjects=subjects,
        )
        await control.record_control_operation(conn, actor=principal)
        # Last, so a refused statement above leaves the bytes staged (and then discarded).
        await asyncio.to_thread(store.put, staged.path, staged.sha256)
        asset = await _read(conn, staged.asset_id)
        revision = await config_revision(conn)
    return MediaWrite(asset=asset, config_revision=revision)


# -- reads -----------------------------------------------------------------------------------------


async def get(db: AsyncEngine, media_id: str) -> MediaAsset:
    """The asset's metadata (``live`` or ``retired``); 404 otherwise."""
    async with db.connect() as conn:
        return await _read(conn, media_id)


def _opened(handle: BinaryIO) -> tuple[bytes, int]:
    head = handle.read(probe.SNIFF_BYTES)
    handle.seek(0)
    return head, os.fstat(handle.fileno()).st_size


async def open_content(
    db: AsyncEngine, store: LocalFilesystemStore, media_id: str, *, signed: bool = False
) -> MediaContent:
    """The asset's bytes, opened (a ``live`` or ``retired`` asset). With ``signed`` (the
    request carried a valid signed URL for this asset) an upload still waiting for the
    moderation hook is served from staging too. 404 otherwise."""
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
    if row is not None and row.state != MediaState.CANDIDATE.value:
        try:
            handle = await asyncio.to_thread(store.open, row.checksum)
        except FileNotFoundError:
            raise MediaNotFound(
                f"the bytes of media asset {media_id} are missing",
                {"media_id": media_id, "reason": "bytes_missing"},
            ) from None
        return MediaContent(media_id, row.mime_type, int(row.size), row.checksum, handle)
    if row is None and signed:
        try:
            handle = await asyncio.to_thread(store.staging_path(media_id).open, "rb")
        except FileNotFoundError:
            raise _not_found(media_id) from None
        head, size = await asyncio.to_thread(_opened, handle)
        return MediaContent(
            media_id, probe.sniff(head) or "application/octet-stream", size, None, handle
        )
    raise _not_found(media_id)


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
    recorded by a ``control`` operation; a retired asset is left as it is (``changed`` false,
    nothing recorded); 404 for an unknown asset or a candidate."""
    async with db.begin() as conn:
        state = await conn.scalar(
            select(media_assets.c.state).where(media_assets.c.id == media_id).with_for_update()
        )
        if state is None or state == MediaState.CANDIDATE.value:
            raise _not_found(media_id)
        changed = state == MediaState.LIVE.value
        if changed:
            await conn.execute(
                update(media_assets)
                .where(media_assets.c.id == media_id)
                .values(state=MediaState.RETIRED.value)
            )
            await control.record_control_operation(conn, actor=principal)
        revision = await config_revision(conn)
    return MediaRetirement(id=media_id, changed=changed, config_revision=revision)


async def assert_referenceable(
    conn: AsyncConnection, media_ids: Sequence[str], *, field: str = "media_id"
) -> None:
    """The check every write that adds a reference to an asset makes, in its own transaction
    (#108, #115, #119): each asset must be ``live``. Holds them ``FOR SHARE``, so none can be
    retired or purged before the write commits. 422 ``media_retired`` (``reason`` ``retired``,
    ``unknown`` or ``candidate``) at ``field`` (``field[i]`` for several) otherwise."""
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
    """A ``jsonb`` column whose documents may name assets: the media ids are the strings
    ``path`` (a SQL/JSON path) yields."""

    table: str
    """``schema.table``."""
    column: str
    path: str = ANY_STRING_PATH

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
]
"""Where references to assets live (FR-MEDIA-2): catalog drafts and published versions now;
#108 (scenarios), #119 (story snapshots) and M8 (candidates, story images) register theirs.
A source whose table does not exist (yet) is skipped."""


def register_reference_source(source: ReferenceSource) -> None:
    """Add ``source`` to ``MEDIA_REFERENCE_SOURCES`` (once)."""
    if source not in MEDIA_REFERENCE_SOURCES:
        MEDIA_REFERENCE_SOURCES.append(source)


async def referenced(conn: AsyncConnection, media_ids: Sequence[str]) -> set[str]:
    """Which of ``media_ids`` some record of ``MEDIA_REFERENCE_SOURCES`` names, found by
    querying the records (never by a counter)."""
    wanted = list(dict.fromkeys(media_ids))
    found: set[str] = set()
    if not wanted:
        return found
    for source in MEDIA_REFERENCE_SOURCES:
        exists = await conn.scalar(
            sql_text("SELECT to_regclass(CAST(:t AS text)) IS NOT NULL"), {"t": source.table}
        )
        if not exists:
            continue
        hits = await conn.scalars(
            sql_text(
                f"SELECT DISTINCT v.item #>> '{{}}' FROM {source.quoted} AS t "  # noqa: S608
                f'CROSS JOIN LATERAL jsonb_path_query(t."{source.column}", '
                "CAST(:path AS jsonpath)) AS v(item) "
                "WHERE v.item #>> '{}' = ANY(CAST(:ids AS text[]))"
            ),
            {"path": source.path, "ids": wanted},
        )
        found.update(str(hit) for hit in hits)
    return found


__all__ = [
    "ANY_STRING_PATH",
    "ENVELOPE_MAX_BYTES",
    "INDEXED_FIELDS",
    "MEDIA_REFERENCE_SOURCES",
    "METADATA_MAX_BYTES",
    "RECORD_TABLE",
    "DepictedSubject",
    "GenerationMetadata",
    "MediaAsset",
    "MediaContent",
    "MediaPage",
    "MediaRetirement",
    "MediaUpload",
    "MediaWrite",
    "ReferenceSource",
    "Staged",
    "assert_referenceable",
    "check_type",
    "content_of",
    "content_path",
    "discard",
    "get",
    "list_assets",
    "lock_checksum",
    "open_content",
    "parse_metadata",
    "read_media_settings",
    "referenced",
    "register_reference_source",
    "require_subjects",
    "retire",
    "stage",
    "upload",
]
