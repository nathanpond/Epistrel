"""Feedback export for evaluation (FR-FB-4, FR-ADMIN-8; #126): rated messages as labelled cases.

**Off by default.** The deployment setting ``feedback_export`` (``PUT /admin/settings``) switches
it on; while it is off, an export is 403 ``feedback_export_disabled`` and writes nothing (no
file, no log row, no idempotency key).

**An export** takes ``{from?, to?, filters?}``: the window on when a rating was first given
(``from`` inclusive, ``to`` exclusive) and the filters ``story_id``, ``tags`` (a rating carrying
any of them), ``min_rating``/``max_rating``, ``config_fingerprint`` and ``profile_version``. It
runs at once and writes one JSON Lines file (``application/x-ndjson``; one case per line, keys
sorted, every line ending in a newline; no cases: an empty file) with **one case per current
rating**:

- ``position`` (``{story_id, turn, variant}``) and ``message_id``;
- ``player_input`` (``{say, do}``, the input the message answered; null for an opening
  narration) and ``message_text``, as stored (an erased span reads ``[erased]``);
- ``rating``, ``tags`` and ``text``;
- ``config_fingerprint``, ``profile_version``, ``models`` and ``quality_status``: the context
  the message was produced under.

Ratings of every variant are exported (dormant and discarded ones too), and of archived stories;
a story the erasure reconciler holds blocked (``blocked_by_erasure``) is left out until it is
reconciled. No principal, credential, model API key or setting is in a case: only the fields
above (C-09).

**The file** is an object of the blob store (``exports/<export id>.jsonl``), so an S3 deployment
keeps it in its bucket. It is served by ``GET /admin/feedback/exports/{id}/content`` behind the
service credential only (no signed URL), and deleted by ``DELETE /admin/feedback/exports/{id}``
or by the media sweep seven days after it was written (``core.media.sweep.EXPORT_TTL``).

**Every export is recorded** in ``feedback_export_log`` (what: the window, the filters and the
case count; when; which principal; the file's key, size and SHA-256), a deployment-level record
that a story's deletion keeps. It is the only record: an export writes no operation or event.
The row stays when the file is deleted (``deleted_at``, ``deleted_by``).

**The erasure boundary** (FR-ADMIN-8). A file is **delivered** once it has been downloaded
(``downloaded_at``): from then on it is outside the erasure boundary, as the boundary's
"exports already delivered" says (``core.erasure.boundary.DELIVERED_EXPORTS``). Until then it is
in the deployment's keeping, and an erasure rewrites it (``ExportErasureTarget``, one of #124's
``ERASURE_TARGETS``): each case of the erased scope (the story, or every story for a catalog
subject) has the subject's spans in its texts replaced by ``[erased]``, and the row's size and
checksum follow.

The idempotent write is keyed per deployment (``Idempotency-Key``): a retry returns the stored
answer and writes no second file. Log lines and the ``epistrel_feedback_exports_total`` counter
carry counts only (invariant 3).
"""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import json
from datetime import datetime
from typing import Annotated, Any

from prometheus_client import Counter
from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, StrictInt, StringConstraints
from sqlalchemy import and_, insert, or_, select, update
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from epistrel.core import clock
from epistrel.core.catalog.search import Position, decode_cursor, encode_cursor
from epistrel.core.config_changes import read_section
from epistrel.core.erasure import ledger as erasure_ledger
from epistrel.core.erasure.boundary import DELIVERED_EXPORTS
from epistrel.core.erasure.erase import register_target
from epistrel.core.erasure.redact import Redactor
from epistrel.core.errors import (
    FeedbackExportDisabled,
    FeedbackExportNotFound,
    InvalidRequest,
    refuse_nul,
)
from epistrel.core.feedback_reporting import (
    check_window,
    feedback_settings,
    message_text_column,
    player_input_column,
    player_input_of,
    unblocked,
)
from epistrel.core.ids import ULID_PATTERN, new_ulid
from epistrel.core.media.store import EXPORTS_DIR, BlobStore
from epistrel.core.media.sweep import EXPORT_TTL
from epistrel.core.settings_schema import MediaSettings
from epistrel.core.tables import deployment_state, stories
from epistrel.core.tables.deployment import DEPLOYMENT_ROW_ID
from epistrel.core.tables.feedback import (
    RATING_MAX,
    RATING_MIN,
    feedback_export_log,
    message_feedback,
)
from epistrel.observability.logging import get_logger

log = get_logger(__name__)

MEDIA_TYPE = "application/x-ndjson"
SETTING = "feedback_export"
EXPORTS_PATH = "/admin/feedback/exports"
DEFAULT_PAGE = 50
MAX_PAGE = 200
CURSOR_MAX = 512
TAG_MAX = 64
TAGS_MAX = 50
CASE_TEXTS = ("message_text", "text")
"""The case fields an erasure redacts, beside ``player_input.say`` and ``.do``."""
ERASURE_TARGET = "feedback_export_cases"
"""The name an erasure counts the export cases it redacted under (``records``)."""

EXPORTS = Counter(
    "epistrel_feedback_exports",
    "Feedback exports written (FR-FB-4).",
)


# -- the request ----------------------------------------------------------------------------------


def _rfc3339_only(value: Any) -> Any:
    """A window bound is an RFC 3339 date-time, as documented: pydantic would also take a
    number of seconds since the epoch."""
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            pass
    raise ValueError("must be an RFC 3339 date-time, e.g. 2026-10-11T12:00:00Z")


RfcDateTime = Annotated[datetime, BeforeValidator(_rfc3339_only)]


class FeedbackExportFilters(BaseModel):
    """Which current ratings an export takes (every one by default): of one ``story_id``,
    carrying any of ``tags``, rated from ``min_rating`` to ``max_rating``, of one
    ``config_fingerprint`` or ``profile_version`` (``<profile id>@<version>``)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    story_id: Annotated[str, StringConstraints(pattern=ULID_PATTERN)] | None = None
    tags: list[Annotated[str, StringConstraints(min_length=1, max_length=TAG_MAX)]] | None = Field(
        default=None, max_length=TAGS_MAX
    )
    min_rating: StrictInt | None = Field(default=None, ge=RATING_MIN, le=RATING_MAX)
    max_rating: StrictInt | None = Field(default=None, ge=RATING_MIN, le=RATING_MAX)
    config_fingerprint: str | None = Field(default=None, min_length=1, max_length=256)
    profile_version: str | None = Field(default=None, min_length=1, max_length=256)


class FeedbackExportRequest(BaseModel):
    """``POST /admin/feedback/export``: the window on when a rating was first given (``from``
    inclusive, ``to`` exclusive; none: all time) and the ``filters``."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        populate_by_name=True,
        json_schema_extra={
            "examples": [
                {
                    "from": "2026-10-01T00:00:00Z",
                    "to": "2026-10-08T00:00:00Z",
                    "filters": {"max_rating": 2, "tags": ["factual_error"]},
                }
            ]
        },
    )

    from_: RfcDateTime | None = Field(default=None, alias="from")
    to: RfcDateTime | None = None
    filters: FeedbackExportFilters = Field(default_factory=FeedbackExportFilters)


def check_request(request: FeedbackExportRequest) -> None:
    """422 ``validation_error`` for a window that ends before it starts (``body.from``) or a
    ``min_rating`` above ``max_rating`` (``body.filters.min_rating``); a NUL anywhere is 422."""
    refuse_nul({"body": request.model_dump(mode="json", by_alias=True)})
    check_window(request.from_, request.to, field="body.from")
    low, high = request.filters.min_rating, request.filters.max_rating
    if low is not None and high is not None and low > high:
        field = "body.filters.min_rating"
        message = "min_rating must not be above max_rating"
        raise InvalidRequest(
            message,
            {
                "field": field,
                "errors": [{"field": field, "message": message, "type": "value_error"}],
            },
        )


# -- the answers ----------------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True, slots=True)
class FeedbackExportWritten:
    """What an export wrote: its id, where its file is served (``content_url``: absolute under
    ``public_base_url`` when that is set, else the path), how many cases it holds, the
    deployment's ``config_revision``, and whether this answer is a stored one (an
    ``Idempotency-Key`` retry)."""

    export_id: str
    content_url: str
    case_count: int
    config_revision: int
    replayed: bool = False


class FeedbackExportWindow(BaseModel):
    """The window an export was asked for: ratings first given from ``from`` (inclusive) to
    ``to`` (exclusive); null is unbounded."""

    model_config = ConfigDict(frozen=True, populate_by_name=True)

    from_: datetime | None = Field(default=None, alias="from")
    to: datetime | None = None


class FeedbackExport(BaseModel):
    """One export's record (FR-FB-4): who exported (``principal``), when (``at``), what (the
    ``window``, the ``filters``, ``case_count``), its file (``bytes``, ``sha256``,
    ``content_url``) and its state: ``downloaded_at`` (the first download: from then on it is
    delivered, outside the erasure boundary), ``expires_at`` (when the media sweep deletes the
    file), ``deleted_at``/``deleted_by`` (when and by whom it was deleted; ``system`` for the
    sweep) and ``redacted_at`` (when an erasure rewrote it before its download)."""

    model_config = ConfigDict(frozen=True)

    export_id: str
    principal: str
    at: datetime
    window: FeedbackExportWindow
    filters: dict[str, Any]
    case_count: int
    bytes: int
    sha256: str
    content_url: str
    downloaded_at: datetime | None
    expires_at: datetime
    deleted_at: datetime | None
    deleted_by: str | None
    redacted_at: datetime | None


class FeedbackExportPage(BaseModel):
    """A page of the export log, newest first, and the cursor of the next page (null: the
    last)."""

    model_config = ConfigDict(frozen=True)

    items: list[FeedbackExport]
    next_cursor: str | None


@dataclasses.dataclass(frozen=True, slots=True)
class FeedbackExportContent:
    """An export's file: its bytes and media type (``application/x-ndjson``)."""

    export_id: str
    data: bytes
    media_type: str = MEDIA_TYPE
    sha256: str = ""


@dataclasses.dataclass(frozen=True, slots=True)
class FeedbackExportDeleted:
    """What a deletion did: whether the export still had a file to delete (REST answers 204
    either way)."""

    export_id: str
    deleted: bool


# -- writing --------------------------------------------------------------------------------------


def blob_key(export_id: str) -> str:
    return f"{EXPORTS_DIR}/{export_id}.jsonl"


def content_path(export_id: str) -> str:
    return f"{EXPORTS_PATH}/{export_id}/content"


async def _content_url(conn: AsyncConnection, export_id: str) -> str:
    base = (await read_section(conn, MediaSettings)).public_base_url
    path = content_path(export_id)
    return f"{str(base).rstrip('/')}{path}" if base is not None else path


async def _config_revision(conn: AsyncConnection) -> int:
    found = await conn.scalar(
        select(deployment_state.c.config_revision).where(deployment_state.c.id == DEPLOYMENT_ROW_ID)
    )
    return int(found or 0)


async def require_enabled(db: AsyncEngine) -> None:
    """403 ``feedback_export_disabled`` unless the deployment's ``feedback_export`` is on."""
    async with db.connect() as conn:
        settings = await feedback_settings(conn)
    if not settings.feedback_export:
        raise FeedbackExportDisabled(
            "feedback export is off: an operator switches it on with the deployment setting "
            "feedback_export",
            {"setting": SETTING},
        )


def _where(request: FeedbackExportRequest) -> list[Any]:
    f = message_feedback.c
    filters = request.filters
    where: list[Any] = [unblocked()]
    if request.from_ is not None:
        where.append(f.created_at >= request.from_)
    if request.to is not None:
        where.append(f.created_at < request.to)
    if filters.story_id is not None:
        where.append(f.story_id == filters.story_id)
    if filters.tags:
        where.append(f.tags.overlap(sorted({tag.strip().lower() for tag in filters.tags})))
    if filters.min_rating is not None:
        where.append(f.rating >= filters.min_rating)
    if filters.max_rating is not None:
        where.append(f.rating <= filters.max_rating)
    if filters.config_fingerprint is not None:
        where.append(f.config_fingerprint == filters.config_fingerprint)
    if filters.profile_version is not None:
        where.append(f.profile_version == filters.profile_version)
    return where


def _case(row: Any) -> dict[str, Any]:
    player_input = player_input_of(row.player_input)
    return {
        "position": {"story_id": row.story_id, "turn": row.turn, "variant": row.variant},
        "message_id": row.message_id,
        "player_input": None if player_input is None else player_input.model_dump(mode="json"),
        "message_text": row.message_text,
        "rating": row.rating,
        "tags": list(row.tags),
        "text": row.text,
        "config_fingerprint": row.config_fingerprint,
        "profile_version": row.profile_version,
        "models": row.models,
        "quality_status": row.quality_status,
    }


def encode_cases(cases: list[dict[str, Any]]) -> bytes:
    """The file: one case per line, keys sorted, each line ending in a newline."""
    return b"".join(
        json.dumps(case, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode() + b"\n"
        for case in cases
    )


def decode_cases(data: bytes) -> list[dict[str, Any]]:
    return [json.loads(line) for line in data.splitlines() if line.strip()]


async def export(
    db: AsyncEngine, store: BlobStore, request: FeedbackExportRequest, *, principal: str
) -> FeedbackExportWritten:
    """Write the export (see the module docstring), record it and answer where it is. The
    caller has checked that exports are on (``require_enabled``)."""
    export_id = new_ulid()
    key = blob_key(export_id)
    f = message_feedback.c
    async with db.begin() as conn:
        found = await conn.execute(
            select(
                message_feedback,
                message_text_column().label("message_text"),
                player_input_column().label("player_input"),
            )
            .where(*_where(request))
            .order_by(f.created_at, f.id)
        )
        cases = [_case(row) for row in found]
        data = encode_cases(cases)
        await asyncio.to_thread(store.put_object, key, data, content_type=MEDIA_TYPE)
        await conn.execute(
            insert(feedback_export_log).values(
                id=export_id,
                principal=principal,
                at=clock.now(),
                window_from=request.from_,
                window_to=request.to,
                filters=request.filters.model_dump(mode="json", exclude_none=True),
                case_count=len(cases),
                blob_key=key,
                bytes=len(data),
                sha256=hashlib.sha256(data).hexdigest(),
            )
        )
        content_url = await _content_url(conn, export_id)
        revision = await _config_revision(conn)
    EXPORTS.inc()
    log.info("feedback exported", export_id=export_id, cases=len(cases))
    return FeedbackExportWritten(
        export_id=export_id,
        content_url=content_url,
        case_count=len(cases),
        config_revision=revision,
    )


# -- reading --------------------------------------------------------------------------------------


async def _row(conn: AsyncConnection, export_id: str, *, lock: bool = False) -> Any:
    query = select(feedback_export_log).where(feedback_export_log.c.id == export_id)
    if lock:
        query = query.with_for_update()
    row = (await conn.execute(query)).one_or_none()
    if row is None:
        raise FeedbackExportNotFound(
            f"feedback export {export_id} does not exist", {"export_id": export_id}
        )
    return row


def _record(row: Any, content_url: str) -> FeedbackExport:
    return FeedbackExport(
        export_id=row.id,
        principal=row.principal,
        at=row.at,
        window=FeedbackExportWindow(from_=row.window_from, to=row.window_to),
        filters=dict(row.filters or {}),
        case_count=row.case_count,
        bytes=row.bytes,
        sha256=row.sha256,
        content_url=content_url,
        downloaded_at=row.downloaded_at,
        expires_at=row.at + EXPORT_TTL,
        deleted_at=row.deleted_at,
        deleted_by=row.deleted_by,
        redacted_at=row.redacted_at,
    )


async def read(db: AsyncEngine, export_id: str) -> FeedbackExport:
    """The export's record; 404 for an unknown export."""
    async with db.connect() as conn:
        row = await _row(conn, export_id)
        return _record(row, await _content_url(conn, export_id))


async def list_exports(
    db: AsyncEngine, *, limit: int = DEFAULT_PAGE, cursor: str | None = None
) -> FeedbackExportPage:
    """A page of the export log, newest first (422 ``cursor_invalid`` for a cursor no list
    gave)."""
    after = decode_cursor(cursor, SETTING, ranked=False) if cursor is not None else None
    log_c = feedback_export_log.c
    query = select(feedback_export_log)
    if after is not None:
        query = query.where(
            or_(
                log_c.at < after.updated_at, and_(log_c.at == after.updated_at, log_c.id < after.id)
            )
        )
    async with db.connect() as conn:
        rows = list(
            await conn.execute(query.order_by(log_c.at.desc(), log_c.id.desc()).limit(limit + 1))
        )
        page = rows[:limit]
        items = [_record(row, await _content_url(conn, str(row.id))) for row in page]
    next_cursor = (
        encode_cursor(Position(None, page[-1].at, str(page[-1].id)), SETTING)
        if len(rows) > limit and page
        else None
    )
    return FeedbackExportPage(items=items, next_cursor=next_cursor)


def _read_object(store: BlobStore, key: str) -> bytes:
    stream = store.open_object(key)
    try:
        return stream.read()
    finally:
        stream.close()


def _gone(export_id: str) -> FeedbackExportNotFound:
    return FeedbackExportNotFound(
        f"feedback export {export_id}'s file was deleted; its record stays",
        {"export_id": export_id, "reason": "deleted"},
    )


async def _refuse_blocked_cases(conn: AsyncConnection, data: bytes) -> None:
    """503 ``erasure_pending`` when a case belongs to a story the erasure reconciler holds
    blocked (#370): its cases may still name the subject, and delivering the file would put it
    outside the reapply's reach for good."""
    named = {
        position["story_id"]
        for case in decode_cases(data)
        if isinstance(position := case.get("position"), dict)
        and isinstance(position.get("story_id"), str)
    }
    if not named:
        return
    found = await conn.execute(
        select(stories.c.id, stories.c.status).where(stories.c.id.in_(sorted(named)))
    )
    for story_id, status in found:
        await erasure_ledger.refuse_blocked(conn, str(story_id), status)


async def content(db: AsyncEngine, store: BlobStore, export_id: str) -> FeedbackExportContent:
    """The export's file, delivered: the first download marks ``downloaded_at``, after which it
    is outside the erasure boundary. 404 for an unknown export, or one whose file was deleted
    (``reason`` ``deleted``). An export not yet delivered with a case of a story the erasure
    reconciler holds blocked is 503 ``erasure_pending`` and stays undelivered (#370); the
    caller compares the ledger first."""
    async with db.begin() as conn:
        row = await _row(conn, export_id, lock=True)
        if row.deleted_at is not None:
            raise _gone(export_id)
        try:
            data = await asyncio.to_thread(_read_object, store, str(row.blob_key))
        except FileNotFoundError:
            raise _gone(export_id) from None
        if row.downloaded_at is None:
            await _refuse_blocked_cases(conn, data)
            await conn.execute(
                update(feedback_export_log)
                .where(feedback_export_log.c.id == export_id)
                .values(downloaded_at=clock.now())
            )
    log.info("feedback export downloaded", export_id=export_id)
    return FeedbackExportContent(
        export_id=export_id, data=data, sha256=hashlib.sha256(data).hexdigest()
    )


async def delete(
    db: AsyncEngine, store: BlobStore, export_id: str, *, principal: str
) -> FeedbackExportDeleted:
    """Delete the export's file; its record stays, marked. Deleting it again changes nothing.
    404 for an unknown export."""
    async with db.begin() as conn:
        row = await _row(conn, export_id, lock=True)
        if row.deleted_at is not None:
            return FeedbackExportDeleted(export_id=export_id, deleted=False)
        await asyncio.to_thread(store.delete_object, str(row.blob_key))
        await conn.execute(
            update(feedback_export_log)
            .where(feedback_export_log.c.id == export_id)
            .values(deleted_at=clock.now(), deleted_by=principal)
        )
    log.info("feedback export deleted", export_id=export_id)
    return FeedbackExportDeleted(export_id=export_id, deleted=True)


# -- erasure (#124's ERASURE_TARGETS) -------------------------------------------------------------


def _redact_case(case: dict[str, Any], redactor: Redactor, export_id: str, index: int) -> bool:
    changed = False
    at = f"cases[{index}]"
    for name in CASE_TEXTS:
        value = case.get(name)
        if isinstance(value, str) and value:
            new = redactor.text(value, (feedback_export_log.name, export_id, f"{at}.{name}"))
            if new != value:
                case[name] = new
                changed = True
    said = case.get("player_input")
    if isinstance(said, dict):
        for name in ("say", "do"):
            value = said.get(name)
            if isinstance(value, str) and value:
                new = redactor.text(
                    value, (feedback_export_log.name, export_id, f"{at}.player_input.{name}")
                )
                if new != value:
                    said[name] = new
                    changed = True
    return changed


class ExportErasureTarget:
    """The exports not yet delivered take part in an erasure (FR-ADMIN-6,8): see the module
    docstring. ``store`` is the blob store of the process's Engine (``bind``)."""

    name = ERASURE_TARGET

    def __init__(self) -> None:
        self.store: BlobStore | None = None

    async def erase(
        self,
        conn: AsyncConnection,
        *,
        story_id: str | None,
        redactor: Redactor,
        write: bool,
    ) -> int:
        store = self.store
        if store is None:
            return 0
        query = (
            select(feedback_export_log)
            .where(
                feedback_export_log.c.downloaded_at.is_(None),
                feedback_export_log.c.deleted_at.is_(None),
            )
            .order_by(feedback_export_log.c.at, feedback_export_log.c.id)
        )
        # A real run holds the rows, so a download or the sweep waits for the rewrite; a dry
        # run reads in a read-only transaction and only counts.
        rows = (await conn.execute(query.with_for_update() if write else query)).all()
        changed = 0
        for row in rows:
            try:
                data = await asyncio.to_thread(_read_object, store, str(row.blob_key))
            except FileNotFoundError:
                continue
            cases = decode_cases(data)
            touched = 0
            for index, case in enumerate(cases):
                position = case.get("position")
                in_scope = story_id is None or (
                    isinstance(position, dict) and position.get("story_id") == story_id
                )
                if in_scope and _redact_case(case, redactor, str(row.id), index):
                    touched += 1
            if not touched:
                continue
            changed += touched
            if write:
                rewritten = encode_cases(cases)
                await asyncio.to_thread(
                    store.put_object, str(row.blob_key), rewritten, content_type=MEDIA_TYPE
                )
                await conn.execute(
                    update(feedback_export_log)
                    .where(feedback_export_log.c.id == row.id)
                    .values(
                        bytes=len(rewritten),
                        sha256=hashlib.sha256(rewritten).hexdigest(),
                        redacted_at=clock.now(),
                    )
                )
        return changed


ERASURE = ExportErasureTarget()
register_target(ERASURE)


def bind(store: BlobStore | None) -> None:
    """Make ``store`` (the process's ``Engine.media``) the one an erasure rewrites exports in."""
    ERASURE.store = store


BOUNDARY_NOTE = (
    f"Delivered exports are outside the erasure boundary (FR-ADMIN-8: {DELIVERED_EXPORTS}): an "
    "export's file counts as delivered once it has been downloaded, and an erasure cannot reach "
    "a copy outside the deployment. Until its first download, an erasure redacts the subject in "
    "the file's cases."
)
"""The sentence the export routes and the docs quote (``GET /admin/erasure/boundary``)."""


__all__ = [
    "BOUNDARY_NOTE",
    "ERASURE",
    "MEDIA_TYPE",
    "ExportErasureTarget",
    "FeedbackExport",
    "FeedbackExportContent",
    "FeedbackExportDeleted",
    "FeedbackExportFilters",
    "FeedbackExportPage",
    "FeedbackExportRequest",
    "FeedbackExportWindow",
    "FeedbackExportWritten",
    "bind",
    "check_request",
    "content",
    "decode_cases",
    "delete",
    "encode_cases",
    "export",
    "list_exports",
    "read",
    "require_enabled",
]
