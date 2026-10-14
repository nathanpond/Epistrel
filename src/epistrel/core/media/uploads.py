"""Resumable uploads: multipart sessions for large media (FR-MEDIA-3; #107).

An upload larger than ``media_multipart_threshold`` (default 8 MiB) cannot be sent in one
``POST /media`` (413 ``use_multipart``); it opens a **session** and sends its bytes in parts, so
an upload that breaks midway resumes instead of restarting. Smaller uploads may use a session
too.

- **Open** (``POST /media/uploads``; ``UploadOpen``): the asset's metadata as ``POST /media``
  takes it (``depicted_subjects`` required, a video's ``poster`` only as an asset id), plus the
  declared ``size``, ``mime_type`` and ``sha256``. Checked at once: the size against
  ``media_max_bytes`` (413), the type against ``media_allowed_types`` and ``kind`` (415), the
  rating (422), a named poster (422), and, for a video without one, that ffmpeg is there to
  extract it (422 ``poster_required``). The answer names the session (``upload_id``) and its
  ``part_size`` (8 MiB): parts are numbered from 1, each exactly ``part_size`` bytes but the
  last, which holds the rest.
- **Parts** (``PUT /media/uploads/{id}/parts/{n}``): a raw body with its ``Content-Length``
  (none: 411 ``length_required``), optionally ``X-Part-SHA256`` (checked: 422
  ``checksum_mismatch``). A number past the last part or a size that is not the part's is 422
  ``part_invalid``. A part sent again replaces the one received, so the request is naturally
  idempotent and keeps no ``Idempotency-Key``. Each part streams to the media root's session
  directory first; the local store keeps it there, the S3 store sends it to the bucket's
  multipart upload (``core.media.store``, ``core.media.s3``). Every part moves the expiry to
  ``media_upload_ttl`` (default 24 hours) after it.
- **Resume** (``GET /media/uploads/{id}``): the session's ``state``, its size, type, part size
  and count, the ``received_parts`` so far, its expiry and, once completed, its asset.
- **Complete** (``POST /media/uploads/{id}/complete``): under the session's row lock, so a
  second completion waits and then finds it closed (409 ``upload_closed``). Every part must be
  there (409 ``upload_incomplete``, ``details.missing``). The parts are assembled into the
  staging file of the new asset while they are hashed; a SHA-256 other than the declared one is
  422 ``checksum_mismatch`` with no asset. The assembled bytes then go through #105's own path:
  type sniffing (415), the rating, the probe, the moderation hook, the video's poster
  (``core.media.assets.prepare``), and one transaction that stores the asset, indexes it and
  closes the session as ``completed`` (``commit``). Answers 201 with the asset (``Location:
  /media/{id}``). Any refusal leaves the session ``open`` with its parts, for a retry (on S3 the
  completed multipart upload is re-opened from the assembled object).
- **Abort** (``DELETE /media/uploads/{id}``): an open session becomes ``aborted`` and its parts
  go; a closed one is left as it is (204 either way).

Session routes, like every route, are open to any authenticated caller: the session's owner
gates nothing (FR-AUTH-8). The media sweep (``sweep_sessions``, part of ``core.media.sweep``)
aborts open sessions past their expiry, deletes closed rows once the idempotency result window
(``idempotency_result_window``) has passed since they closed, and removes parts a store holds for
no open session once they are ``media_upload_ttl`` old.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
from collections.abc import AsyncIterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, BinaryIO

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import delete, insert, select, update
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from epistrel.core import clock, control
from epistrel.core.config_changes import read_section
from epistrel.core.erasure.index import drop_many, index_records
from epistrel.core.erasure.records import upload_session_record
from epistrel.core.errors import (
    ChecksumMismatch,
    InvalidRequest,
    LengthRequired,
    PartInvalid,
    UploadClosed,
    UploadIncomplete,
    UploadNotFound,
)
from epistrel.core.ids import new_ulid
from epistrel.core.media import probe
from epistrel.core.media.assets import (
    EXAMPLE_UPLOAD,
    MediaUpload,
    MediaWrite,
    Prepared,
    Staged,
    check_declared_type,
    check_supplied_poster,
    check_type,
    check_upload,
    commit,
    config_revision,
    discard,
    document_subjects_required,
    prepare,
    rating_version,
    read_asset,
    too_large,
)
from epistrel.core.media.poster import PosterExtractor, poster_required
from epistrel.core.media.signing import Signer
from epistrel.core.media.store import BlobStore
from epistrel.core.settings_schema import (
    MEDIA_MAX_BYTES_LIMIT,
    IdempotencySettings,
    MediaSettings,
)
from epistrel.core.tables.media import MediaKind, UploadState, media_upload_sessions
from epistrel.testing import pauses

logger = logging.getLogger(__name__)

PART_SIZE = 8 * 1024 * 1024
"""Every part's size but the last (S3's multipart floor is 5 MiB)."""
SHA256_PATTERN = r"^[0-9a-f]{64}$"
PART_HASH_HEADER = "X-Part-SHA256"
WRITE_CHUNK = 64 * 1024
POSTER_AT = "body"
"""Where a session's fields are refused: ``body.<field>``."""


def part_count(size: int, part_size: int = PART_SIZE) -> int:
    return max(1, -(-size // part_size))


def part_length(number: int, size: int, part_size: int = PART_SIZE) -> int:
    """The size part ``number`` must have: ``part_size``, or the rest for the last."""
    return min(part_size, size - (number - 1) * part_size)


# -- the request and the answers ------------------------------------------------------------------


def _open_schema(schema: dict[str, Any]) -> None:
    document_subjects_required(schema)
    schema["examples"] = [
        {
            **EXAMPLE_UPLOAD,
            "kind": "video",
            "alt_text": "The storm reaching Gull Rock",
            "size": 52_428_800,
            "mime_type": "video/mp4",
            "sha256": "9f2c" + "0" * 60,
        }
    ]


class UploadOpen(MediaUpload):
    """``POST /media/uploads``: the asset's metadata (as ``POST /media``'s ``metadata`` part),
    and the upload's declared ``size`` (bytes), ``mime_type`` and ``sha256`` (lowercase hex),
    which the assembled bytes must match."""

    model_config = ConfigDict(extra="forbid", json_schema_extra=_open_schema)

    size: int = Field(
        ge=1,
        le=MEDIA_MAX_BYTES_LIMIT,
        description="The whole upload's size in bytes (at most `media_max_bytes`).",
    )
    mime_type: str = Field(
        min_length=3,
        max_length=127,
        description="The bytes' MIME type: one of `media_allowed_types`, agreeing with `kind`.",
    )
    sha256: str = Field(
        pattern=SHA256_PATTERN,
        description="The SHA-256 of the whole upload (lowercase hex); completion checks it.",
    )

    def asset(self) -> MediaUpload:
        """The asset's metadata, as stored with the session (exactly the fields sent)."""
        return MediaUpload.model_validate(self.stored_metadata())

    def stored_metadata(self) -> dict[str, Any]:
        stored: dict[str, Any] = self.model_dump(
            mode="json", exclude_unset=True, exclude={"size", "mime_type", "sha256"}
        )
        return stored


class UploadSession(BaseModel):
    """A resumable upload session (``GET /media/uploads/{id}``): its state (``open``,
    ``completed`` or ``aborted``), the declared size and MIME type, the part size and count,
    the parts received so far (send the others to resume), when it expires (it is extended
    by every part), and the asset its completion created (``null`` until then)."""

    model_config = ConfigDict(title="UploadSession")

    id: str
    state: UploadState
    size: int
    mime_type: str
    part_size: int
    part_count: int
    received_parts: list[int]
    expires_at: datetime
    media_id: str | None


@dataclass(frozen=True, slots=True)
class UploadOpened:
    """An opened session: its id, part size and count, expiry, and the deployment's
    (unchanged) ``config_revision``."""

    upload_id: str
    part_size: int
    part_count: int
    expires_at: datetime
    config_revision: int
    replayed: bool = False
    """``True`` when an ``Idempotency-Key`` retry returned this stored result (#61)."""


@dataclass(frozen=True, slots=True)
class PartStored:
    """A part the session holds now: its number and size, every part received, and the
    deployment's (unchanged) ``config_revision``."""

    n: int
    size: int
    received_parts: list[int]
    config_revision: int


@dataclass(frozen=True, slots=True)
class UploadAborted:
    """An abort: the session, and whether it was ``open`` until now (``changed``)."""

    upload_id: str
    changed: bool
    config_revision: int
    replayed: bool = False
    """``True`` when an ``Idempotency-Key`` retry returned this stored result (#61)."""


# -- reads ---------------------------------------------------------------------------------------


def _view(row: Any) -> UploadSession:
    return UploadSession(
        id=str(row.id),
        state=UploadState(row.state),
        size=int(row.size),
        mime_type=str(row.mime_type),
        part_size=int(row.part_size),
        part_count=part_count(int(row.size), int(row.part_size)),
        received_parts=sorted(int(n) for n in row.received_parts),
        expires_at=row.expires_at,
        media_id=str(row.media_id) if row.media_id is not None else None,
    )


def _missing(upload_id: str) -> UploadNotFound:
    return UploadNotFound(f"upload session {upload_id} does not exist", {"upload_id": upload_id})


async def _row(conn: AsyncConnection, upload_id: str, *, lock: bool = False) -> Any:
    query = select(media_upload_sessions).where(media_upload_sessions.c.id == upload_id)
    if lock:
        query = query.with_for_update()
    row = (await conn.execute(query)).one_or_none()
    if row is None:
        raise _missing(upload_id)
    return row


def _require_open(row: Any, now: datetime) -> None:
    """409 ``upload_closed`` unless the session is ``open`` and not past its expiry."""
    if row.state != UploadState.OPEN.value:
        raise UploadClosed(
            f"upload session {row.id} is {row.state}",
            {"upload_id": str(row.id), "state": str(row.state)},
        )
    if row.expires_at <= now:
        raise UploadClosed(
            f"upload session {row.id} expired",
            {"upload_id": str(row.id), "state": "expired"},
        )


async def get_session(db: AsyncEngine, upload_id: str) -> UploadSession:
    """The session as ``GET /media/uploads/{id}`` answers it; 404 for an unknown one."""
    async with db.connect() as conn:
        return _view(await _row(conn, upload_id))


# -- open --------------------------------------------------------------------------------------


async def open_session(
    db: AsyncEngine,
    store: BlobStore,
    request: UploadOpen,
    *,
    principal: str,
    settings: MediaSettings,
    posters: PosterExtractor,
) -> UploadOpened:
    """Open a session (see the module docstring); nothing is stored on a refusal."""
    asset = check_upload(request.asset(), at=POSTER_AT)
    if request.size > settings.media_max_bytes:
        raise too_large(request.size, settings.media_max_bytes)
    mime_type = check_declared_type(request.mime_type, settings, asset.kind)
    async with db.begin() as conn:
        await rating_version(conn, asset.content_rating_id)
        if asset.poster is not None:
            await check_supplied_poster(conn, asset.poster, field=f"{POSTER_AT}.poster")
    if (
        asset.kind is MediaKind.VIDEO
        and asset.poster is None
        and (await posters.status()).ffmpeg != "ok"
    ):
        raise poster_required()
    upload_id = new_ulid()
    store_id = await asyncio.to_thread(store.begin_parts, upload_id, mime_type)
    now = clock.now()
    expires_at = now + timedelta(seconds=settings.media_upload_ttl)
    try:
        async with db.begin() as conn:
            await conn.execute(
                insert(media_upload_sessions).values(
                    id=upload_id,
                    owner_principal=asset.owner or principal,
                    created_by=principal,
                    size=request.size,
                    mime_type=mime_type,
                    metadata=request.stored_metadata(),
                    declared_sha256=request.sha256,
                    part_size=PART_SIZE,
                    received_parts=[],
                    state=UploadState.OPEN.value,
                    s3_upload_id=store_id,
                    media_id=None,
                    created_at=now,
                    updated_at=now,
                    expires_at=expires_at,
                    closed_at=None,
                )
            )
            # The session holds the asset's captions, prompts and subjects until the sweep
            # deletes it: indexed like the asset (FR-ADMIN-6, #334).
            stored = (
                await conn.execute(
                    select(media_upload_sessions).where(media_upload_sessions.c.id == upload_id)
                )
            ).one()
            await index_records(conn, [upload_session_record(stored)])
            await control.record_control_operation(conn, actor=principal)
            revision = await config_revision(conn)
    except BaseException:
        await _quietly(store.abort_parts, upload_id, store_id)
        raise
    return UploadOpened(
        upload_id=upload_id,
        part_size=PART_SIZE,
        part_count=part_count(request.size),
        expires_at=expires_at,
        config_revision=revision,
    )


async def _quietly(run: Any, *args: Any) -> None:
    """A store clean-up that must not hide the error being raised (logged, never content)."""
    try:
        await asyncio.to_thread(run, *args)
    except Exception as exc:
        logger.warning("media store clean-up failed: %s", type(exc).__name__)


# -- parts ---------------------------------------------------------------------------------------


def _part_invalid(number: int, reason: str, message: str, **sizes: int) -> PartInvalid:
    return PartInvalid(message, {"part": number, "reason": reason, **sizes})


async def put_part(
    db: AsyncEngine,
    store: BlobStore,
    upload_id: str,
    number: int,
    content: AsyncIterable[bytes],
    *,
    size: int | None,
    sha256: str | None,
    settings: MediaSettings,
) -> PartStored:
    """Store part ``number`` of a session (see the module docstring)."""
    if sha256 is not None and not re.fullmatch(SHA256_PATTERN, sha256):
        raise InvalidRequest(
            f"{PART_HASH_HEADER} must be a lowercase hex SHA-256",
            {"field": f"headers.{PART_HASH_HEADER}"},
        )
    if size is None:
        raise LengthRequired(
            "a part must declare its size (Content-Length); a chunked body is refused",
            {"upload_id": upload_id, "part": number},
        )
    async with db.connect() as conn:
        row = await _row(conn, upload_id)
    _require_open(row, clock.now())
    total, part_size = int(row.size), int(row.part_size)
    count = part_count(total, part_size)
    if not 1 <= number <= count:
        raise _part_invalid(
            number, "number", f"the session has parts 1 to {count}", part_count=count
        )
    expected = part_length(number, total, part_size)
    if size != expected:
        raise _part_invalid(
            number,
            "size",
            f"part {number} must be {expected} bytes",
            size=size,
            expected=expected,
        )
    staged = await asyncio.to_thread(store.part_staging, upload_id, number)
    try:
        received, digest = await _receive(staged, content, number, expected)
        if sha256 is not None and digest != sha256:
            raise ChecksumMismatch(
                f"part {number} is not the bytes {PART_HASH_HEADER} declares",
                {"part": number, "declared": sha256, "actual": digest},
            )
        now = clock.now()
        async with db.begin() as conn:
            row = await _row(conn, upload_id, lock=True)
            _require_open(row, now)
            await asyncio.to_thread(store.store_part, upload_id, number, staged, row.s3_upload_id)
            parts = sorted({int(n) for n in row.received_parts} | {number})
            await conn.execute(
                update(media_upload_sessions)
                .where(media_upload_sessions.c.id == upload_id)
                .values(
                    received_parts=parts,
                    updated_at=now,
                    expires_at=now + timedelta(seconds=settings.media_upload_ttl),
                )
            )
            revision = await config_revision(conn)
    finally:
        await asyncio.to_thread(staged.unlink, missing_ok=True)
    return PartStored(n=number, size=received, received_parts=parts, config_revision=revision)


def _write(handle: BinaryIO, data: bytes) -> None:
    handle.write(data)


async def _receive(
    staged: Any, content: AsyncIterable[bytes], number: int, expected: int
) -> tuple[int, str]:
    """Stream a part into ``staged``: its size and SHA-256; 422 ``part_invalid`` when the
    bytes are more or fewer than declared."""
    digest = hashlib.sha256()
    received = 0
    handle = await asyncio.to_thread(staged.open, "xb")
    try:
        async for chunk in content:
            if not chunk:
                continue
            received += len(chunk)
            if received > expected:
                raise _part_invalid(
                    number, "length", "the body is longer than its Content-Length", size=expected
                )
            digest.update(chunk)
            await asyncio.to_thread(_write, handle, chunk)
        await asyncio.to_thread(handle.flush)
        await asyncio.to_thread(os.fsync, handle.fileno())
    finally:
        await asyncio.to_thread(handle.close)
    if received != expected:
        raise _part_invalid(
            number,
            "length",
            "the body is shorter than its Content-Length",
            size=received,
            expected=expected,
        )
    return received, digest.hexdigest()


# -- complete ---------------------------------------------------------------------------------


class _Hashing:
    """A write target that hashes, counts and keeps the first bytes of what passes through."""

    def __init__(self, handle: BinaryIO) -> None:
        self.handle = handle
        self.digest = hashlib.sha256()
        self.size = 0
        self.head = b""

    def write(self, data: bytes) -> int:
        if len(self.head) < probe.SNIFF_BYTES:
            self.head += data[: probe.SNIFF_BYTES - len(self.head)]
        self.digest.update(data)
        self.size += len(data)
        return self.handle.write(data)


def _assemble_into(
    store: BlobStore, upload_id: str, count: int, store_id: str | None, handle: BinaryIO
) -> _Hashing:
    target = _Hashing(handle)
    try:
        store.assemble(upload_id, count, store_id, target)
        handle.flush()
        os.fsync(handle.fileno())
    finally:
        handle.close()
    return target


async def _assemble(store: BlobStore, row: Any, kind: MediaKind, settings: MediaSettings) -> Staged:
    """The session's parts, assembled into the staging file of a new asset and checked against
    the declared SHA-256 (422 ``checksum_mismatch``) and type (415)."""
    asset_id = new_ulid()
    path = store.staging_path(asset_id)
    handle = await asyncio.to_thread(store.open_staging, asset_id)
    total, part_size = int(row.size), int(row.part_size)
    try:
        target = await asyncio.to_thread(
            _assemble_into,
            store,
            str(row.id),
            part_count(total, part_size),
            row.s3_upload_id,
            handle,
        )
        actual = target.digest.hexdigest()
        if actual != row.declared_sha256 or target.size != total:
            raise ChecksumMismatch(
                "the assembled upload is not the bytes its sha256 declares; the session stays "
                "open (send the wrong parts again)",
                {"declared": str(row.declared_sha256), "actual": actual, "size": target.size},
            )
        found = check_type(target.head, settings, kind, str(row.mime_type))
    except BaseException:
        await asyncio.to_thread(path.unlink, missing_ok=True)
        raise
    return Staged(asset_id, path, actual, total, found, kind)


async def complete(
    db: AsyncEngine,
    store: BlobStore,
    upload_id: str,
    *,
    principal: str,
    settings: MediaSettings,
    signer: Signer,
    posters: PosterExtractor,
) -> MediaWrite:
    """Complete a session into a ``live`` asset (see the module docstring)."""
    staged: Staged | None = None
    prepared: Prepared | None = None
    assembling = False
    store_id: str | None = None
    total = part_size = 0
    try:
        async with db.begin() as conn:
            row = await _row(conn, upload_id, lock=True)
            now = clock.now()
            _require_open(row, now)
            store_id = row.s3_upload_id
            total, part_size = int(row.size), int(row.part_size)
            count = part_count(total, part_size)
            expected = set(range(1, count + 1))
            missing = sorted(expected - {int(n) for n in row.received_parts})
            if not missing:
                held = await asyncio.to_thread(store.stored_parts, upload_id, store_id)
                if held is not None:  # the parts the row counts must be in the store too
                    missing = sorted(
                        n for n in expected if held.get(n) != part_length(n, total, part_size)
                    )
            if missing:
                raise UploadIncomplete(
                    f"upload session {upload_id} lacks {len(missing)} part(s)",
                    {"upload_id": upload_id, "missing": missing},
                )
            await pauses.pause(pauses.MEDIA_COMPLETE_LOCKED)
            request = check_upload(MediaUpload.model_validate(row.metadata), at=POSTER_AT)
            if total > settings.media_max_bytes:
                raise too_large(total, settings.media_max_bytes)
            check_declared_type(str(row.mime_type), settings, request.kind)
            assembling = True
            staged = await _assemble(store, row, request.kind, settings)
            prepared = await prepare(
                db,
                store,
                staged,
                request,
                principal=principal,
                settings=settings,
                signer=signer,
                posters=posters,
                poster_field=f"{POSTER_AT}.poster",
            )
            media_id = await commit(conn, store, prepared, principal=principal, assembled=upload_id)
            await conn.execute(
                update(media_upload_sessions)
                .where(media_upload_sessions.c.id == upload_id)
                .values(
                    state=UploadState.COMPLETED.value,
                    media_id=media_id,
                    updated_at=now,
                    closed_at=now,
                )
            )
            asset = await read_asset(conn, media_id)
            revision = await config_revision(conn)
    except BaseException:
        if assembling:
            await _reopen(db, store, upload_id, store_id, part_size, total)
        raise
    finally:
        await discard(staged)
        if prepared is not None:
            await discard(prepared.extracted)
    await _quietly(store.finish_parts, upload_id, store_id)
    return MediaWrite(asset=asset, config_revision=revision)


async def _reopen(
    db: AsyncEngine,
    store: BlobStore,
    upload_id: str,
    store_id: str | None,
    part_size: int,
    total: int,
) -> None:
    """After a completion that failed past its assembly: the session stays open with its
    parts (S3: a new multipart upload from the assembled object, recorded on the row)."""
    try:
        reopened = await asyncio.to_thread(
            store.reopen_parts, upload_id, store_id, part_size, total
        )
    except Exception as exc:
        logger.warning("an upload session's parts were not re-opened: %s", type(exc).__name__)
        return
    if reopened != store_id:
        async with db.begin() as conn:
            await conn.execute(
                update(media_upload_sessions)
                .where(media_upload_sessions.c.id == upload_id)
                .values(s3_upload_id=reopened)
            )


# -- abort ---------------------------------------------------------------------------------------


async def abort(
    db: AsyncEngine, store: BlobStore, upload_id: str, *, principal: str
) -> UploadAborted:
    """Abort an open session (its parts go; recorded by a ``control`` operation); a closed one
    is left as it is (``changed`` false). 404 for an unknown one."""
    async with db.begin() as conn:
        row = await _row(conn, upload_id, lock=True)
        changed = row.state == UploadState.OPEN.value
        if changed:
            now = clock.now()
            await conn.execute(
                update(media_upload_sessions)
                .where(media_upload_sessions.c.id == upload_id)
                .values(state=UploadState.ABORTED.value, updated_at=now, closed_at=now)
            )
            await control.record_control_operation(conn, actor=principal)
        revision = await config_revision(conn)
    if changed:
        await _quietly(store.abort_parts, upload_id, row.s3_upload_id)
    return UploadAborted(upload_id=upload_id, changed=changed, config_revision=revision)


# -- the sweep ---------------------------------------------------------------------------------


async def sweep_sessions(
    db: AsyncEngine, store: BlobStore, *, now: datetime | None = None
) -> dict[str, int]:
    """The media sweep's session pass (see the module docstring): counts of the sessions it
    expired, the closed rows it deleted and the stray parts it removed."""
    moment = now or clock.now()
    async with db.connect() as conn:
        settings = await read_section(conn, MediaSettings)
        window = await read_section(conn, IdempotencySettings)
    async with db.begin() as conn:
        expired = (
            await conn.execute(
                update(media_upload_sessions)
                .where(
                    media_upload_sessions.c.id.in_(
                        select(media_upload_sessions.c.id)
                        .where(
                            media_upload_sessions.c.state == UploadState.OPEN.value,
                            media_upload_sessions.c.expires_at <= moment,
                        )
                        .with_for_update(skip_locked=True)
                        .scalar_subquery()
                    )
                )
                .values(state=UploadState.ABORTED.value, updated_at=moment, closed_at=moment)
                .returning(media_upload_sessions.c.id, media_upload_sessions.c.s3_upload_id)
            )
        ).all()
    for upload_id, store_id in expired:
        await _quietly(store.abort_parts, str(upload_id), store_id)
    cutoff = moment - timedelta(hours=window.idempotency_result_window)
    async with db.begin() as conn:
        purged = await conn.execute(
            delete(media_upload_sessions)
            .where(
                media_upload_sessions.c.state != UploadState.OPEN.value,
                media_upload_sessions.c.closed_at <= cutoff,
            )
            .returning(media_upload_sessions.c.id)
        )
        purged_ids = [str(found) for found in purged.scalars()]
        await drop_many(conn, media_upload_sessions.name, purged_ids)
    stray = 0
    try:
        stale = await asyncio.to_thread(store.stale_part_uploads, float(settings.media_upload_ttl))
    except Exception as exc:  # the store's listing failed: the next run tries again
        logger.warning("stray upload parts were not listed: %s", type(exc).__name__)
        stale = []
    if stale:
        async with db.connect() as conn:
            open_ids = set(
                await conn.scalars(
                    select(media_upload_sessions.c.id).where(
                        media_upload_sessions.c.id.in_([name for name, _ in stale]),
                        media_upload_sessions.c.state == UploadState.OPEN.value,
                    )
                )
            )
        for name, store_id in stale:
            if name not in open_ids:
                await _quietly(store.abort_parts, name, store_id)
                stray += 1
    return {
        "sessions_expired": len(expired),
        "sessions_purged": len(purged_ids),
        "parts_removed": stray,
    }


__all__ = [
    "PART_HASH_HEADER",
    "PART_SIZE",
    "PartStored",
    "UploadAborted",
    "UploadOpen",
    "UploadOpened",
    "UploadSession",
    "abort",
    "complete",
    "get_session",
    "open_session",
    "part_count",
    "part_length",
    "put_part",
    "sweep_sessions",
]
