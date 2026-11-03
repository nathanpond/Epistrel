"""Story import as a background job (FR-STORE-7, FR-ADMIN-4's restore; #250).

**The request** (``start_import``) checks up front, before anything is created:

- the upload's size against ``bundle_max_uncompressed_bytes`` (413 ``bundle_too_large``);
- the manifest, the archive's first member, readable within the limits and valid, and its
  format version (422 ``corrupt_bundle``, ``unsupported_format_version``);
- the rating choice: the ``content_rating_id`` named (it must exist and not be retired), else
  the one rating whose name matches the bundle's (NFKC, casefolded, not retired); none (or
  several) is 422 ``rating_required`` with ``bundle_rating_name`` (and ``candidates``);
- the profile: the ``engine_profile_id`` named (it must exist), else the bundle's own profile
  when this deployment has it (kept at the bundle's version when this deployment holds it, so
  the story runs as it ran), else the deployment's default; and, for any other configuration
  than the bundle's own, #251's impact report (``impact``): a report that warns, without
  ``ignore_warnings``, answers it (``ImportImpact``, ``applied: false``) and creates nothing.

It then stages the upload under ``<bundle dir>/imports/<job id>.epistrel.tgz`` (mode 0600) and,
in one transaction, creates the story row (``status = importing``: every read and write of it is
409 ``story_importing``, ``core.importing``), the ``bundle_jobs`` row (``kind = import``, the
choices and the limits frozen in ``request``) and the ``story_import`` job, and answers
``ImportStarted`` (REST 202, ``Location: /admin/stories/imports/{job_id}``).

**The job** (``run_import``) reads the staged archive under the frozen limits (counted while it
decompresses), checks every member (``core.bundles.importing``), writes the media blobs, then:

1. ``inserting``: the rows into the story, ids remapped (``importing.insert_bundle``), the
   ``import`` operation, the projections folded and the subject index rewritten;
2. #251's gates, in order (``IMPORT_GATES``): any raise of ``ImportRefused`` (or an erasure
   guard's refusal) is the import's refusal;
3. ``rebuilding``: the projections and the subject index again, from the post-gate records;
4. ``embedding``: the embed job queued (``after``; the import does not wait for vectors);
5. the story leaves ``importing`` (``active``, or ``paused`` when a gate started a backfill),
   the staged upload is deleted, the job is ``ready`` and one ``audit_log`` row is written.

**A refusal leaves nothing** (``refuse_and_clean``): #50's story-scoped delete removes every row
of the story, the story row included; the media assets the import created are retired (#105's
sweep removes their bytes when nothing else holds the checksum); the staged upload is deleted;
the ``bundle_jobs`` row is kept, ``refused`` (or ``failed`` once the job's attempts are spent),
with ``story_id`` null, its error and its report, so the operator can read why. A crashed
attempt is recovered by the job's lease: the next attempt deletes the partial story's rows and
starts again from the staged file (the remapped ids are recomputed the same).

Log lines carry ids and counts only (invariant 3).
"""

from __future__ import annotations

import asyncio
import hashlib
import unicodedata
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictStr
from sqlalchemy import func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from epistrel.core import audit, importing
from epistrel.core.bundles import importing as reader
from epistrel.core.enums import StoryStatus
from epistrel.core.errors import (
    BundleTooLarge,
    CorruptBundle,
    EpistrelError,
    ImportNotFound,
    InvalidRequest,
    ProfileDeleted,
    RatingRequired,
    UnsupportedFormatVersion,
)
from epistrel.core.ids import new_ulid
from epistrel.core.tables import bundle_jobs, stories
from epistrel.core.tables.audit_log import AuditKind
from epistrel.observability.logging import get_logger

log = get_logger(__name__)

IMPORT_KIND = "story_import"
"""The #70 job kind."""
STAGED_DIR = "imports"
STAGES = (
    "queued",
    "reading",
    "inserting",
    "indexing",
    "erasure",
    "guardrails",
    "outbox",
    "profile",
    "rebuilding",
    "embedding",
    "cleaning",
)
"""An import's stages, in order (#250's ``stage``)."""
TERMINAL = ("ready", "refused", "failed")
REFUSALS: tuple[type[EpistrelError], ...] = (
    BundleTooLarge,
    CorruptBundle,
    UnsupportedFormatVersion,
)


class ImportMetadata(BaseModel):
    """The import request's ``metadata`` part (#250): who will own the story, its name, the
    rating and the profile to attach, and #251's impact choices."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        json_schema_extra={
            "examples": [
                {
                    "name": "The Lighthouse (restored)",
                    "content_rating_id": "teen",
                    "engine_profile_id": "standard",
                    "upgrade_mode": "backfill",
                    "ignore_warnings": False,
                }
            ]
        },
    )

    owner: StrictStr | None = Field(default=None, min_length=1, max_length=255)
    name: StrictStr | None = Field(default=None, min_length=1, max_length=200)
    content_rating_id: StrictStr | None = Field(default=None, min_length=1, max_length=64)
    engine_profile_id: StrictStr | None = Field(default=None, min_length=1, max_length=64)
    upgrade_mode: Literal["backfill", "forward_only"] | None = None
    ignore_warnings: StrictBool = False


@dataclass(frozen=True, slots=True)
class ImportStarted:
    """An accepted import (REST 202): its job, its story and where to read its state."""

    job_id: str
    story_id: str
    status: str = "importing"
    config_revision: int = 0
    replayed: bool = False

    @property
    def status_url(self) -> str:
        return f"/admin/stories/imports/{self.job_id}"


@dataclass(frozen=True, slots=True)
class ImportImpact:
    """#251: an import whose impact report warns, without ``ignore_warnings`` (REST 200
    ``{applied: false, impact}``): nothing was created."""

    impact: Any
    applied: bool = False
    config_revision: int = 0
    replayed: bool = False


class ImportJob(BaseModel):
    """``GET /admin/stories/imports/{job_id}``: the import's state."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    job_id: str
    status: Literal["queued", "running", "ready", "refused", "failed"]
    story_id: str | None
    stage: str | None = None
    progress: dict[str, int] = Field(default_factory=lambda: {"done": 0, "total": 0})
    error: dict[str, Any] | None = None
    report: dict[str, Any] | None = None


class ImportRefused(Exception):
    """A gate's refusal (#251): the import ends ``refused`` with ``code``, ``details`` and its
    report's ``errors``."""

    def __init__(
        self,
        code: str,
        message: str,
        details: Mapping[str, Any] | None = None,
        errors: Sequence[Mapping[str, Any]] = (),
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = dict(details or {})
        self.errors = [dict(e) for e in errors]


@dataclass(slots=True)
class ImportContext:
    """What a gate reads and writes (#251): the story, its job, the manifest, the choices, the
    id map, the report, the deployment's services, and the steps to run once the story is
    ready (``after_ready``, in the finishing transaction)."""

    db: AsyncEngine
    story_id: str
    job_id: str
    manifest: Mapping[str, Any]
    rating_id: str
    profile_id: str
    upgrade_mode: str | None
    ignore_warnings: bool
    principal: str
    inserted: reader.Inserted
    report: dict[str, Any]
    services: ImportServices
    after_ready: list[Callable[[AsyncConnection], Awaitable[None]]] = field(default_factory=list)

    async def stage(self, name: str, done: int = 0, total: int = 0) -> None:
        await set_stage(self.db, self.job_id, name, done, total)


Gate = Callable[[ImportContext], Awaitable[None]]
IMPORT_GATES: list[tuple[str, str, Gate]] = []
"""#251's gates, in order, as ``(name, stage, gate)``: registered by
``orchestrator.bundles.import_gates``."""


def register_gate(name: str, stage: str, gate: Gate) -> Gate:
    """Add (or replace) a gate; the order of first registration is the order they run in."""
    for n, (known, _, _) in enumerate(IMPORT_GATES):
        if known == name:
            IMPORT_GATES[n] = (name, stage, gate)
            return gate
    IMPORT_GATES.append((name, stage, gate))
    return gate


After = Callable[[AsyncConnection, str], Awaitable[None]]


@dataclass(slots=True)
class ImportServices:
    """What the Engine gives the job at start-up: the media store, the embed job, the bundle
    directory, and the services the gates use (``extra``: the erasure key, the summary
    compressor, the blob collector, the Model Access Layer)."""

    blobs: reader.BlobWriter | None = None
    after: After | None = None
    directory: Path = Path("var/bundles")
    extra: dict[str, Any] = field(default_factory=dict)


_services: list[ImportServices] = [ImportServices()]


def configure(services: ImportServices) -> None:
    _services[0] = services


def services() -> ImportServices:
    return _services[0]


# -- the request ------------------------------------------------------------------------------


def _folded(name: str) -> str:
    return unicodedata.normalize("NFKC", name).casefold()


async def resolve_rating(
    conn: AsyncConnection, chosen: str | None, manifest: Mapping[str, Any]
) -> str:
    """The rating the import attaches (see the module docstring)."""
    from epistrel.core.tables import content_ratings

    r = content_ratings.c
    if chosen is not None:
        found = (await conn.execute(select(r.id, r.retired_at).where(r.id == chosen))).one_or_none()
        if found is None or found.retired_at is not None:
            raise InvalidRequest(
                "no such content rating, or it is retired",
                {"field": "metadata.content_rating_id"},
            )
        return str(found.id)
    wanted = dict(manifest.get("content_rating") or {}).get("name")
    if not wanted:
        raise RatingRequired("name a content rating for the import", {"bundle_rating_name": None})
    matches = [
        str(row.id)
        for row in await conn.execute(
            select(r.id, r.name).where(r.retired_at.is_(None)).order_by(r.id)
        )
        if _folded(str(row.name)) == _folded(str(wanted))
    ]
    if len(matches) != 1:
        details: dict[str, Any] = {"bundle_rating_name": str(wanted)}
        if matches:
            details["candidates"] = matches
        raise RatingRequired(
            "no single rating of this deployment matches the bundle's; name one", details
        )
    return matches[0]


async def resolve_profile(
    conn: AsyncConnection, chosen: str | None, manifest: Mapping[str, Any] | None = None
) -> str:
    """The engine profile the import attaches: the one named (it must exist, not deleted),
    else the bundle's own profile when this deployment has it (not deleted), else the
    deployment's default."""
    from epistrel.core.profiles.store import default_profile
    from epistrel.core.tables import engine_profiles

    if chosen is None:
        own = dict((manifest or {}).get("engine_profile") or {}).get("id")
        if own:
            found = await conn.scalar(
                select(engine_profiles.c.id).where(
                    engine_profiles.c.id == str(own), engine_profiles.c.deleted_at.is_(None)
                )
            )
            if found is not None:
                return str(found)
        return await default_profile(conn)
    return await _named_profile(conn, chosen)


async def resolve_pin(
    conn: AsyncConnection, profile_id: str, chosen: str | None, manifest: Mapping[str, Any]
) -> int | None:
    """The version the imported story keeps: the bundle's own when the import attaches the
    bundle's own profile unnamed and this deployment holds that version (so the story runs as
    it ran), else none (the profile's latest)."""
    from epistrel.core.tables import engine_profile_versions

    own = dict(manifest.get("engine_profile") or {})
    if chosen is not None or str(own.get("id")) != profile_id or own.get("version") is None:
        return None
    v = engine_profile_versions.c
    found = await conn.scalar(
        select(v.version).where(v.profile_id == profile_id, v.version == int(own["version"]))
    )
    return None if found is None else int(found)


async def _named_profile(conn: AsyncConnection, chosen: str) -> str:
    from epistrel.core.tables import engine_profiles

    found = (
        await conn.execute(
            select(engine_profiles.c.id, engine_profiles.c.deleted_at).where(
                engine_profiles.c.id == chosen
            )
        )
    ).one_or_none()
    if found is None:
        raise InvalidRequest("no such engine profile", {"field": "metadata.engine_profile_id"})
    if found.deleted_at is not None:
        raise ProfileDeleted(
            "the engine profile is deleted", {"field": "metadata.engine_profile_id"}
        )
    return str(found.id)


Impact = Callable[[AsyncConnection, Mapping[str, Any], str, ImportMetadata], Awaitable[Any]]
"""#251's up-front impact report: ``(conn, manifest, profile_id, metadata) -> (report, warns)``."""


async def import_impact(
    conn: AsyncConnection,
    manifest: Mapping[str, Any],
    profile_id: str,
    metadata: ImportMetadata,
    facts: Any,
) -> tuple[Any, bool]:
    """#251 (FR-PROF-7,8): the impact report of attaching ``profile_id`` to a story with the
    bundle's history: the bundle's own profile settings (``manifest.engine_profile``) as the
    configuration before, the target profile's latest version as the one after, the history the
    bundle's transcript (``manifest.counts``); and whether it warns."""
    from epistrel.core.profiles import effective, impact
    from epistrel.core.tables import deployment_state
    from epistrel.core.tables.deployment import DEPLOYMENT_ROW_ID

    defaults = dict(
        await conn.scalar(
            select(deployment_state.c.profile_defaults).where(
                deployment_state.c.id == DEPLOYMENT_ROW_ID
            )
        )
        or {}
    )
    source = dict(manifest.get("engine_profile") or {})
    before = effective.Configuration(
        str(source.get("id") or profile_id),
        int(source.get("version") or 0),
        effective.merged_settings(defaults, dict(source.get("settings") or {}), {}),
    )
    target = await effective.assignable(conn, profile_id, field="metadata.engine_profile_id")
    after = effective.Configuration(
        profile_id, target.version, effective.merged_settings(defaults, target.document, {})
    )
    change = impact.delta(before, after, facts)
    counts = dict(manifest.get("counts") or {})
    messages = int(counts.get("transcript_messages") or counts.get("events") or 0)
    entry = impact.affected(
        str(manifest.get("source_story_id") or "bundle"),
        change,
        history=messages > 0,
        transcript=(messages, 0),
        mode=metadata.upgrade_mode,
    )
    report = impact.single_report(entry, change)
    return report, bool(report.warnings)


Enqueue = Callable[[AsyncConnection, str], Awaitable[str]]


async def start_import(
    db: AsyncEngine,
    data: bytes,
    metadata: ImportMetadata,
    *,
    principal: str,
    directory: Path,
    enqueue: Enqueue,
    impact: Impact | None = None,
) -> ImportStarted | ImportImpact:
    """Check the request and start the import (see the module docstring)."""
    from epistrel.core.config_changes import read_section
    from epistrel.core.settings_schema import RetentionSettings

    limits = reader.Limits.of(await read_section(db, RetentionSettings))
    if len(data) > limits.total:
        raise BundleTooLarge(
            "the upload is larger than the bundle limit",
            {"limit": "bundle_max_uncompressed_bytes", "value": limits.total},
        )
    manifest, story_row = await asyncio.to_thread(reader.read_head, data, limits)
    async with db.connect() as conn:
        rating = await resolve_rating(conn, metadata.content_rating_id, manifest)
        profile = await resolve_profile(conn, metadata.engine_profile_id, manifest)
        pin = await resolve_pin(conn, profile, metadata.engine_profile_id, manifest)
        if impact is not None and pin is None:
            report, warns = await impact(conn, manifest, profile, metadata)
            if warns and not metadata.ignore_warnings:
                return ImportImpact(report)
    job_id, story_id = new_ulid(), new_ulid()
    staged = directory / STAGED_DIR / f"{job_id}.epistrel.tgz"
    await asyncio.to_thread(_stage_file, staged, data)
    owner = metadata.owner or principal
    name = metadata.name or (story_row or {}).get("name") or None
    try:
        async with db.begin() as conn:
            await conn.execute(
                insert(stories).values(
                    id=story_id,
                    name=name,
                    owner_principal=owner,
                    status=StoryStatus.IMPORTING.value,
                    content_rating_id=rating,
                    engine_profile_id=profile,
                    engine_profile_version_pin=pin,
                )
            )
            await conn.execute(
                insert(bundle_jobs).values(
                    id=job_id,
                    story_id=story_id,
                    kind="import",
                    status="queued",
                    requested_by=principal,
                    path=str(staged),
                    size=len(data),
                    sha256=hashlib.sha256(data).hexdigest(),
                    manifest=dict(manifest),
                    stage="queued",
                    progress={"done": 0, "total": 0},
                    request={
                        **metadata.model_dump(mode="json"),
                        "owner": owner,
                        "content_rating_id": rating,
                        "engine_profile_id": profile,
                        "engine_profile_version_pin": pin,
                        "limits": limits.as_json(),
                    },
                )
            )
            queued = await enqueue(conn, job_id)
            await conn.execute(
                update(bundle_jobs).where(bundle_jobs.c.id == job_id).values(job_id=queued)
            )
    except BaseException:
        await asyncio.to_thread(_unlink, staged)
        raise
    log.info("story import started", job_id=job_id, story_id=story_id, size=len(data))
    return ImportStarted(job_id, story_id)


def _stage_file(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".part")
    temporary.write_bytes(data)
    temporary.chmod(0o600)
    temporary.replace(path)


def _unlink(path: Path | str | None) -> None:
    if path:
        Path(str(path)).unlink(missing_ok=True)


# -- the job ----------------------------------------------------------------------------------


async def set_stage(
    db: AsyncEngine, job_id: str, stage: str, done: int = 0, total: int = 0
) -> None:
    async with db.begin() as conn:
        await conn.execute(
            update(bundle_jobs)
            .where(bundle_jobs.c.id == job_id)
            .values(stage=stage, progress={"done": done, "total": total})
        )


async def _load(db: AsyncEngine, job_id: str) -> Any:
    async with db.connect() as conn:
        return (
            await conn.execute(
                select(bundle_jobs).where(
                    bundle_jobs.c.id == job_id, bundle_jobs.c.kind == "import"
                )
            )
        ).one_or_none()


async def _reset(conn: AsyncConnection, row: Any) -> None:
    """A resumed attempt (#250's crash recovery): the partial story's rows deleted and its row
    created again as the request made it."""
    from epistrel.core.stories import purge_story_rows

    story_id = str(row.story_id)
    request = dict(row.request or {})
    held = (
        await conn.execute(select(stories.c.name).where(stories.c.id == story_id))
    ).one_or_none()
    await purge_story_rows(conn, story_id)
    media = [str(i) for i in request.get("media_ids") or []]
    if media:
        from epistrel.core.tables import media_assets

        # The attempt recreates them under the same ids (``importing.fresh_id``).
        await conn.execute(media_assets.delete().where(media_assets.c.id.in_(media)))
    await conn.execute(
        insert(stories).values(
            id=story_id,
            name=None if held is None else held.name,
            owner_principal=str(request.get("owner")),
            status=StoryStatus.IMPORTING.value,
            content_rating_id=str(request["content_rating_id"]),
            engine_profile_id=str(request["engine_profile_id"]),
            engine_profile_version_pin=request.get("engine_profile_version_pin"),
        )
    )


async def _media_retired(conn: AsyncConnection, media_ids: Sequence[str]) -> None:
    """The media assets an import created, retired (#105's sweep removes their bytes when no
    other asset holds the checksum)."""
    from epistrel.core.tables import media_assets
    from epistrel.core.tables.media import MediaState

    if media_ids:
        await conn.execute(
            update(media_assets)
            .where(media_assets.c.id.in_(list(media_ids)))
            .values(state=MediaState.RETIRED.value)
        )


async def run_import(db: AsyncEngine, job_id: str) -> dict[str, Any]:
    """The ``story_import`` job (see the module docstring); counts only."""
    from epistrel.core.erasure.index import reindex_story
    from epistrel.core.projections.fold import replace_with_fold

    row = await _load(db, job_id)
    if row is None or str(row.status) in TERMINAL or row.story_id is None:
        return {"skipped": 1}
    story_id = str(row.story_id)
    request = dict(row.request or {})
    resumed = int(row.attempts or 0) > 0
    async with db.begin() as conn:
        await conn.execute(
            update(bundle_jobs)
            .where(bundle_jobs.c.id == job_id)
            .values(status="running", attempts=bundle_jobs.c.attempts + 1, stage="reading")
        )
        if resumed:
            await _reset(conn, row)
    the = services()
    limits = reader.Limits.from_json(request["limits"])
    report: dict[str, Any] = {"errors": [], "warnings": []}
    with importing.passing(story_id):
        try:
            data = await asyncio.to_thread(Path(str(row.path)).read_bytes)
            manifest, by_path = await asyncio.to_thread(reader.read_bundle, data, limits)
            media = await reader.stage_media(manifest, by_path, the.blobs)
            await set_stage(db, job_id, "inserting", 0, len(by_path))
            async with db.begin() as conn:
                inserted = await reader.insert_bundle(
                    conn,
                    story_id,
                    manifest,
                    by_path,
                    job_id=job_id,
                    principal=str(row.requested_by),
                    owner=str(request.get("owner") or row.requested_by),
                    media=media if the.blobs is not None else [],
                    bundle_sha256=str(row.sha256 or hashlib.sha256(data).hexdigest()),
                )
                await conn.execute(
                    update(bundle_jobs)
                    .where(bundle_jobs.c.id == job_id)
                    .values(request={**request, "media_ids": inserted.media_ids})
                )
            if media and the.blobs is None:
                inserted.warnings.append({"code": "media_not_imported", "count": len(media)})
            report["warnings"] = list(inserted.warnings)
            report["counts"] = dict(inserted.counts)
            context = ImportContext(
                db=db,
                story_id=story_id,
                job_id=job_id,
                manifest=manifest,
                rating_id=str(request["content_rating_id"]),
                profile_id=str(request["engine_profile_id"]),
                upgrade_mode=request.get("upgrade_mode"),
                ignore_warnings=bool(request.get("ignore_warnings")),
                principal=str(row.requested_by),
                inserted=inserted,
                report=report,
                services=the,
            )
            for _, stage, gate in list(IMPORT_GATES):
                await set_stage(db, job_id, stage)
                await gate(context)
        except ImportRefused as refused:
            report["errors"] = [*report["errors"], *refused.errors]
            error = {"code": refused.code, "message": refused.message, "details": refused.details}
            await refuse_and_clean(db, job_id, error, report)
            return {"refused": 1}
        except REFUSALS as refused:
            report["errors"] = [*report["errors"], {"code": refused.code, **refused.details}]
            error = {"code": refused.code, "message": str(refused), "details": refused.details}
            await refuse_and_clean(db, job_id, error, report)
            return {"refused": 1}
        except EpistrelError as refused:
            if refused.code not in ("erased_subject_referenced", "erasure_key_missing"):
                raise
            # #251: the erasure fence could not clear the imported story.
            error = {"code": refused.code, "message": str(refused), "details": refused.details}
            await refuse_and_clean(db, job_id, error, report)
            return {"refused": 1}
        await set_stage(db, job_id, "rebuilding")
        async with db.begin() as conn:
            await replace_with_fold(conn, story_id)
            await reindex_story(conn, story_id)
        await set_stage(db, job_id, "embedding")
        async with db.begin() as conn:
            if the.after is not None:
                await the.after(conn, story_id)
            for step in context.after_ready:
                await step(conn)
            await conn.execute(
                update(stories)
                .where(stories.c.id == story_id, stories.c.status == StoryStatus.IMPORTING.value)
                .values(status=StoryStatus.ACTIVE.value)
            )
            await conn.execute(
                update(bundle_jobs)
                .where(bundle_jobs.c.id == job_id)
                .values(
                    status="ready",
                    stage=None,
                    report=report,
                    finished_at=func.transaction_timestamp(),
                    path=None,
                )
            )
            await audit.record(
                conn,
                AuditKind.STORY_IMPORT,
                principal=str(row.requested_by),
                story_id=story_id,
                details={
                    "job_id": job_id,
                    "status": "ready",
                    "bundle_sha256": row.sha256,
                    "source_story_id": inserted.source_story_id,
                },
            )
    await asyncio.to_thread(_unlink, row.path)
    log.info("story imported", job_id=job_id, story_id=story_id, tables=len(inserted.counts))
    return {"ready": 1, "tables": len(inserted.counts)}


async def refuse_and_clean(
    db: AsyncEngine,
    job_id: str,
    error: Mapping[str, Any],
    report: Mapping[str, Any] | None = None,
    *,
    status: Literal["refused", "failed"] = "refused",
) -> None:
    """#250's one cleanup path (see the module docstring)."""
    from epistrel.core.stories import purge_story_rows

    row = await _load(db, job_id)
    if row is None:
        return
    await set_stage(db, job_id, "cleaning")
    request = dict(row.request or {})
    async with db.begin() as conn:
        await conn.execute(
            update(bundle_jobs).where(bundle_jobs.c.id == job_id).values(story_id=None)
        )
        await _media_retired(conn, request.get("media_ids") or [])
        if row.story_id is not None:
            exists = await conn.scalar(select(stories.c.id).where(stories.c.id == row.story_id))
            if exists is not None:
                await purge_story_rows(conn, str(row.story_id))
        await conn.execute(
            update(bundle_jobs)
            .where(bundle_jobs.c.id == job_id)
            .values(
                status=status,
                stage=None,
                error=dict(error),
                report=dict(report or {}),
                finished_at=func.transaction_timestamp(),
                path=None,
            )
        )
        await audit.record(
            conn,
            AuditKind.STORY_IMPORT,
            principal=str(row.requested_by),
            details={
                "job_id": job_id,
                "status": status,
                "bundle_sha256": row.sha256,
                "code": str(error.get("code")),
            },
        )
    await asyncio.to_thread(_unlink, row.path)
    log.info("story import refused", job_id=job_id, status=status, code=str(error.get("code")))


async def status(db: AsyncEngine, job_id: str) -> ImportJob:
    """The import's state (404 ``job_not_found`` for an unknown id, an export's included)."""
    row = await _load(db, job_id)
    if row is None:
        raise ImportNotFound("no such import", {"job_id": job_id})
    progress = dict(row.progress or {})
    return ImportJob(
        job_id=str(row.id),
        status=str(row.status),
        story_id=None if row.story_id is None else str(row.story_id),
        stage=row.stage,
        progress={
            "done": int(progress.get("done", 0)),
            "total": int(progress.get("total", 0)),
        },
        error=row.error,
        report=row.report,
    )


__all__ = [
    "IMPORT_GATES",
    "IMPORT_KIND",
    "STAGES",
    "ImportContext",
    "ImportImpact",
    "ImportJob",
    "ImportMetadata",
    "ImportRefused",
    "ImportServices",
    "ImportStarted",
    "configure",
    "import_impact",
    "refuse_and_clean",
    "register_gate",
    "resolve_profile",
    "resolve_rating",
    "run_import",
    "services",
    "set_stage",
    "start_import",
    "status",
]
