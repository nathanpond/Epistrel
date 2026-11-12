"""Story bundles (FR-STORE-7, FR-ADMIN-4; #249): a story exported at one revision as a
checksummed, versioned archive.

**Classification** (``BUNDLE_TABLES``): every table with a ``story_id`` column is ``EXPORT``
(its rows go to ``records/<table>.jsonl``), ``REBUILD`` (a projection or index an import
rebuilds) or ``NEVER`` (deployment-level or transient work: jobs, the turn queue, timers,
idempotency results, check jobs, in-progress backfills and rewrite sessions, read-set indexes,
the export jobs themselves). ``erasure_ledger`` (keyed by ``story_ref``) is ``EXPORT`` by name.
A coverage test fails on a story table classified nowhere.

**The export** (``run_export``, the ``story_export`` job): one ``REPEATABLE READ`` read-only
transaction pinned to the story's revision (``exported_revision``) reads the story row, its
committed operations (in-flight ones are left out, with their events), its events in ``seq``
order and every ``EXPORT`` table's rows, and writes one gzip-compressed POSIX tar under
``EPISTREL_BUNDLE_DIR``: ``manifest.json`` first (format ``1.1.0``, the source story, the
revision, the Alembic head, when and by whom, and per file its path, SHA-256, size and record
count), then ``records/story.json``, ``events.jsonl`` and ``records/<table>.jsonl`` in a fixed
order. Lines are canonical JSON (sorted keys, UTF-8, ``\\n``), so two exports of one revision
differ only in the manifest's ``exported_at`` and ``exported_by``. Before the archive is kept,
every member is scanned for the deployment's secrets (service credentials and the config key,
raw, base64 and hex): a hit fails the job (``secret_detected``) and leaves no file.

Projections, indexes, vectors, deployment-level records, in-flight work and secrets are never in
a bundle.

#704 completes the format:

- **Catalog and media.** The catalog snapshots the story holds (``CATALOG``:
  ``character_snapshots``) go to ``catalog/<table>.jsonl``; every media asset the story's rows
  reference (``characters.images``, the snapshots, ``stories.scenario_snapshot``: the ids
  #105's ``MEDIA_REFERENCE_SOURCES`` names for a story, a video's poster with it) is one
  ``media.jsonl`` line and one ``media/<sha256>`` member (``blobs`` reads the bytes; a missing
  blob fails the export ``media_missing``).
- **Manifest.** It gains ``config_revision``, ``content_rating {id, name, version_id,
  version}`` and ``engine_profile {id, name, version, settings}``, and ``counts.media``.
- **Order.** ``manifest.json``, ``records/story.json``, ``media.jsonl``, ``catalog/*``,
  ``records/operations.jsonl``, ``events.jsonl``, the other ``records/*``, then
  ``media/<sha256>`` by checksum.
- **Validation.** Every event payload is validated against #45's registry
  (``payload_invalid``); every line validates against the bundle JSON Schema
  (``bundle_schemas``, published in the OpenAPI components).
- **Erasure race.** An erasure ledger entry for the story (or catalog-scope, for a subject
  the story references) committed while the export ran discards the output and restarts it, at
  most ``EXPORT_ATTEMPTS`` times (``export_failed``, ``erasure_race``).
- **Lifecycle.** An archive expires after ``bundle_export_ttl_hours``; ``expire_bundles``
  (``retention_sweep``) deletes the file and sets ``expired``; a story-scope erasure expires the
  story's stored archives (``StoryExportsTarget``, error ``erased``); deleting a story deletes
  its exports and files (``delete_story_bundles``).
- **Secrets.** The scan covers the model API keys and provider credentials the Engine passes,
  besides the service credentials and the config key.

#272 (format ``1.1.0``): the story's images (``records/story_images.jsonl``, ``EXPORT`` by
default) travel with their assets, deleted and retired ones included; a ``media.jsonl`` line
carries the asset's check fields (``check_status``, ``check_model``, ``check_endpoint``,
``policy_results``) and ``viewpoint_basis``. The image work and accounting tables
(``image_jobs``, ``image_reservations``, ``image_costs``) are ``NEVER``, as is every
deployment-level table that carries a ``story_id`` (``core.stories.STORY_ID_EXCEPTIONS``:
``audit_log``, ``certification_runs`` (#773) and the image accounting). A ``1.0.0`` bundle
imports through ``format.FORMAT_MIGRATIONS``' step: its media lines get the new fields' empty
values, and it imports with no story images.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json
import re
import tarfile
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel
from sqlalchemy import Table, select, text, update
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from epistrel.core import clock
from epistrel.core.enums import OperationStatus
from epistrel.core.errors import ExportExpired, ExportNotFound, ExportNotReady, StoryNotFound
from epistrel.core.ids import new_ulid
from epistrel.core.schema import metadata
from epistrel.core.tables import bundle_jobs, events, operations, stories
from epistrel.observability.logging import get_logger
from epistrel.testing import pauses

log = get_logger(__name__)

FORMAT_VERSION = "1.1.0"
TTL = timedelta(hours=24)
EXPORT_KIND = "story_export"
"""The job kind (#70)."""

Classification = Literal["EXPORT", "REBUILD", "NEVER"]

REBUILD: frozenset[str] = frozenset(
    {
        "story_heads",
        "lite_summary_segments",
        "relationships",
        "placement",
        "scene_state",
        "belief_state",
        "nested_belief_state",
        "state_values",
        "external_values",
        "subject_references",
        "transcript_messages",
        "story_texts",
    }
)
NEVER: frozenset[str] = frozenset(
    {
        "jobs",
        "image_jobs",
        "image_reservations",
        "image_costs",
        "turn_queue",
        "timers",
        "audit_log",
        "certification_runs",
        "idempotency_keys",
        "worker_heartbeats",
        "rating_check_jobs",
        "guardrail_check_results",
        "extraction_shadow",
        "escalations",
        "proposition_changes",
        "overflow_episodes",
        "backfill_chunks",
        "backfill_conflicts",
        "story_backfills",
        "rewrite_questions",
        "rewrite_sessions",
        "bundle_jobs",
    }
)
CATALOG: frozenset[str] = frozenset({"character_snapshots"})
"""#704: the catalog snapshots a story holds, written to ``catalog/<table>.jsonl``."""
SPECIAL: frozenset[str] = frozenset({"stories", "operations", "events"})
"""Written by the export itself (``records/story.json``, ``records/operations.jsonl``,
``events.jsonl``)."""


def story_tables() -> list[Table]:
    """Every table with a ``story_id`` column, by name."""
    return sorted(
        (t for t in metadata.tables.values() if "story_id" in t.c),
        key=lambda t: t.name,
    )


def classify(name: str) -> Classification | None:
    if name in REBUILD:
        return "REBUILD"
    if name in NEVER:
        return "NEVER"
    if name in SPECIAL or any(t.name == name for t in story_tables()):
        return "EXPORT"
    return None


def exported_tables() -> list[Table]:
    """The ``EXPORT`` tables written to ``records/`` (the special three apart), by name."""
    return [
        t
        for t in story_tables()
        if t.name not in REBUILD
        and t.name not in NEVER
        and t.name not in SPECIAL
        and t.name not in CATALOG
    ]


def catalog_tables() -> list[Table]:
    """#704: the ``CATALOG`` tables written to ``catalog/``, by name."""
    return [t for t in story_tables() if t.name in CATALOG]


COLUMNS_NOT_EXPORTED: dict[str, frozenset[str]] = {
    "memories": frozenset({"embedding", "embedding_model_version", "search"}),
    "facts": frozenset({"closed_by"}),
}
"""#704: vectors and generated columns never travel (an import re-embeds and regenerates).
#471: a fact's ``closed_by`` is the inverse of ``supersedes``, rebuilt by the import
(``canon.facts.rebuild_closers``, ADR-189)."""


def _exportable(table: Table, row: Any) -> dict[str, Any]:
    dropped = COLUMNS_NOT_EXPORTED.get(table.name, frozenset())
    return {k: v for k, v in dict(row._mapping).items() if k not in dropped}


class ExportJob(BaseModel):
    """``GET /admin/stories/exports/{job_id}``: an export's state."""

    job_id: str
    story_id: str
    status: str
    exported_revision: int | None
    manifest: dict[str, Any] | None
    size: int | None
    sha256: str | None
    expires_at: datetime | None
    error: dict[str, Any] | None


@dataclass(frozen=True, slots=True)
class ExportStarted:
    """``POST /admin/stories/{id}/export``'s answer (REST 202)."""

    job_id: str
    story_id: str
    status: str = "queued"
    timeline_revision: int = 0
    replayed: bool = False


@dataclass(frozen=True, slots=True)
class Bundle:
    """A ready export's archive: its bytes, file name and SHA-256."""

    data: bytes
    filename: str
    sha256: str


# -- encoding -------------------------------------------------------------------------------------


def _plain(value: Any) -> Any:
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, bytes | bytearray | memoryview):
        return base64.b64encode(bytes(value)).decode()
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, list | tuple | set | frozenset):
        return [_plain(v) for v in value]
    if hasattr(value, "value") and not isinstance(value, int | float | str | bool):
        return value.value
    return value


def canonical(record: Any) -> bytes:
    """One canonical JSON line: sorted keys, UTF-8, no spaces, ``\\n``."""
    return (
        json.dumps(_plain(record), sort_keys=True, ensure_ascii=False, separators=(",", ":")) + "\n"
    ).encode()


def _lines(rows: Iterable[Any], table: Table | None = None) -> tuple[bytes, int]:
    body = bytearray()
    count = 0
    for row in rows:
        body += canonical(_exportable(table, row) if table is not None else dict(row._mapping))
        count += 1
    return bytes(body), count


def _secrets(values: Sequence[str]) -> list[bytes]:
    found: list[bytes] = []
    for value in values:
        raw = value.encode()
        if len(raw) < 8:
            continue
        found += [raw, base64.b64encode(raw), raw.hex().encode()]
    return found


def scan(members: Sequence[tuple[str, bytes]], secrets: Sequence[str]) -> str | None:
    """The first member holding a secret (raw, base64 or hex), or ``None``."""
    needles = _secrets(secrets)
    for name, data in members:
        if any(n in data for n in needles):
            return name
    return None


def archive(members: Sequence[tuple[str, bytes]]) -> bytes:
    """The gzip-compressed POSIX tar of ``members`` in order (fixed metadata: reproducible)."""
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w:gz", format=tarfile.PAX_FORMAT) as tar:
        for name, data in members:
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mtime = 0
            info.mode = 0o644
            info.type = tarfile.REGTYPE
            tar.addfile(info, io.BytesIO(data))
    return out.getvalue()


# -- the export -----------------------------------------------------------------------------------


ULID_TEXT = re.compile(r"\b[0-9A-HJKMNP-TV-Z]{26}\b")


def _ulids(value: Any) -> set[str]:
    """Every ULID-shaped string inside a JSON value."""
    found: set[str] = set()
    if isinstance(value, str):
        found.update(ULID_TEXT.findall(value))
    elif isinstance(value, dict):
        for item in value.values():
            found |= _ulids(item)
    elif isinstance(value, list | tuple):
        for item in value:
            found |= _ulids(item)
    return found


@dataclass
class _Snapshot:
    revision: int
    story: bytes
    media_lines: list[dict[str, Any]]
    catalog: list[tuple[str, bytes]]
    operations: bytes
    events: bytes
    records: list[tuple[str, bytes]]
    counts: dict[str, int]
    extra: dict[str, Any]
    erasures: frozenset[str]

    def members(self) -> list[tuple[str, bytes]]:
        media = b"".join(canonical(line) for line in self.media_lines)
        return [
            ("records/story.json", self.story),
            ("media.jsonl", media),
            *self.catalog,
            ("records/operations.jsonl", self.operations),
            ("events.jsonl", self.events),
            *self.records,
        ]


MEDIA_FIELDS = (
    "id",
    "checksum",
    "kind",
    "mime_type",
    "size",
    "width",
    "height",
    "duration_ms",
    "alt_text",
    "caption",
    "generation_metadata",
    "source",
    "depicted_subjects",
    "content_rating_version_id",
    "poster_media_id",
    "viewpoint_basis",
    "check_status",
    "check_model",
    "check_endpoint",
    "policy_results",
)
MEDIA_FIELDS_1_1 = ("check_status", "check_model", "check_endpoint", "policy_results")
"""#272: the fields format ``1.1.0`` added to a ``media.jsonl`` line (``viewpoint_basis``,
#261, with them)."""


async def _media(conn: AsyncConnection, story_row: Any, story_id: str) -> list[dict[str, Any]]:
    """#704: the media assets the story's rows reference (its characters' images, its catalog
    snapshots, its scenario snapshot, #261: its story images, deleted ones included) and their
    posters, as ``media.jsonl`` lines."""
    from epistrel.core.tables import characters, media_assets, story_images

    wanted = _ulids(story_row.scenario_snapshot)
    for (images,) in await conn.execute(
        select(characters.c.images).where(characters.c.story_id == story_id)
    ):
        wanted |= _ulids(images)
    snapshots = metadata.tables.get("character_snapshots")
    if snapshots is not None:
        for (content,) in await conn.execute(
            select(snapshots.c.content).where(snapshots.c.story_id == story_id)
        ):
            wanted |= _ulids(content)
    wanted |= {
        str(a)
        for a in await conn.scalars(
            select(story_images.c.asset_id).where(story_images.c.story_id == story_id)
        )
    }
    if not wanted:
        return []
    m = media_assets.c
    rows = list(await conn.execute(select(media_assets).where(m.id.in_(sorted(wanted)))))
    posters = {str(r.poster_media_id) for r in rows if r.poster_media_id is not None}
    known = {str(r.id) for r in rows}
    if posters - known:
        rows += list(
            await conn.execute(select(media_assets).where(m.id.in_(sorted(posters - known))))
        )
    lines = []
    for row in sorted(rows, key=lambda r: str(r.id)):
        mapping = dict(row._mapping)
        line = {f: mapping.get(f) for f in MEDIA_FIELDS}
        line["sha256"] = line.pop("checksum")
        lines.append(line)
    return lines


async def _story_erasures(conn: AsyncConnection, story_id: str) -> frozenset[str]:
    """#704: the erasure ledger entries that touch the story: its own (story scope) and the
    catalog-scope ones for a subject it references."""
    ledger = metadata.tables.get("erasure_ledger")
    if ledger is None:
        return frozenset()
    found = {
        str(i)
        for (i,) in await conn.execute(select(ledger.c.id).where(ledger.c.story_ref == story_id))
    }
    references = metadata.tables.get("subject_references")
    if references is not None and "subject_id" in ledger.c:
        found |= {
            str(i)
            for (i,) in await conn.execute(
                select(ledger.c.id).where(
                    ledger.c.story_ref.is_(None),
                    ledger.c.subject_id.in_(
                        select(references.c.subject_id).where(references.c.story_id == story_id)
                    ),
                )
            )
        }
    return frozenset(found)


def _validated_events(rows: Iterable[Any]) -> tuple[bytes, int]:
    """#704: ``events.jsonl``, each payload validated against #45's registry
    (``PayloadInvalid`` names the first that fails)."""
    from epistrel.core.events.registry import validate

    body = bytearray()
    count = 0
    for row in rows:
        mapping = dict(row._mapping)
        try:
            validate(str(mapping["type"]), dict(mapping["payload"] or {}))
        except Exception as exc:  # any validation error fails the export with the event's id
            raise PayloadInvalid(str(mapping["id"])) from exc
        body += canonical(mapping)
        count += 1
    return bytes(body), count


async def _snapshot(conn: AsyncConnection, story_id: str) -> _Snapshot:
    story = (await conn.execute(select(stories).where(stories.c.id == story_id))).one_or_none()
    if story is None:
        raise StoryNotFound(f"story {story_id} does not exist", {"story_id": story_id})
    revision = int(story.timeline_revision)
    counts: dict[str, int] = {}
    erasures = await _story_erasures(conn, story_id)
    o = operations.c
    committed = o.status != OperationStatus.IN_FLIGHT.value
    ops, counts["operations"] = _lines(
        await conn.execute(
            select(operations).where(o.story_id == story_id, committed).order_by(o.operation_id)
        )
    )
    kept = select(o.operation_id).where(o.story_id == story_id, committed)
    found_events, counts["events"] = _validated_events(
        await conn.execute(
            select(events)
            .where(events.c.story_id == story_id, events.c.operation_id.in_(kept))
            .order_by(events.c.seq)
        )
    )
    catalog: list[tuple[str, bytes]] = []
    for table in catalog_tables():
        order = [c for c in table.primary_key.columns] or list(table.c)[:1]
        data, counts[table.name] = _lines(
            await conn.execute(select(table).where(table.c.story_id == story_id).order_by(*order)),
            table,
        )
        catalog.append((f"catalog/{table.name}.jsonl", data))
    records: list[tuple[str, bytes]] = []
    for table in exported_tables():
        key = table.c.story_id
        order = [c for c in table.primary_key.columns] or list(table.c)[:1]
        data, counts[table.name] = _lines(
            await conn.execute(select(table).where(key == story_id).order_by(*order)), table
        )
        records.append((f"records/{table.name}.jsonl", data))
    ledger = metadata.tables.get("erasure_ledger")
    if ledger is not None:
        data, counts["erasure_ledger"] = _lines(
            await conn.execute(
                select(ledger).where(ledger.c.story_ref == story_id).order_by(ledger.c.id)
            )
        )
        records.append(("records/erasure_ledger.jsonl", data))
    media_lines = await _media(conn, story, story_id)
    counts["media"] = len(media_lines)
    return _Snapshot(
        revision=revision,
        story=canonical(dict(story._mapping)),
        media_lines=media_lines,
        catalog=catalog,
        operations=ops,
        events=found_events,
        records=records,
        counts=counts,
        extra=await _manifest_extra(conn, story),
        erasures=erasures,
    )


async def _manifest_extra(conn: AsyncConnection, story: Any) -> dict[str, Any]:
    """#704: the manifest's ``config_revision``, ``content_rating`` and ``engine_profile``."""
    from epistrel.core.tables import (
        content_rating_versions,
        content_ratings,
        engine_profile_versions,
        engine_profiles,
    )

    rating = (
        await conn.execute(
            select(
                content_ratings.c.id,
                content_ratings.c.name,
                content_rating_versions.c.version_id,
                content_rating_versions.c.version,
            )
            .join(
                content_rating_versions,
                content_rating_versions.c.version_id == content_ratings.c.current_version_id,
            )
            .where(content_ratings.c.id == story.content_rating_id)
        )
    ).one_or_none()
    profile = None
    if story.engine_profile_id is not None:
        runs_on = story.engine_profile_version_pin
        p, v = engine_profiles.c, engine_profile_versions.c
        if runs_on is None:
            runs_on = await conn.scalar(
                select(p.current_version).where(p.id == story.engine_profile_id)
            )
        found = (
            await conn.execute(
                select(p.id, p.name, v.version, v.settings)
                .join(engine_profile_versions, v.profile_id == p.id)
                .where(p.id == story.engine_profile_id, v.version == runs_on)
            )
        ).one_or_none()
        if found is not None:
            profile = {
                "id": str(found.id),
                "name": str(found.name),
                "version": int(found.version),
                "settings": dict(found.settings or {}),
            }
    return {
        "config_revision": int(getattr(story, "config_revision", 0) or 0),
        "content_rating": None
        if rating is None
        else {
            "id": str(rating.id),
            "name": str(rating.name),
            "version_id": str(rating.version_id),
            "version": int(rating.version),
        },
        "engine_profile": profile,
    }


async def _head(conn: AsyncConnection) -> str:
    found = await conn.scalar(text("SELECT version_num FROM alembic_version"))
    return str(found or "")


def manifest(
    story_id: str,
    revision: int,
    members: Sequence[tuple[str, bytes]],
    counts: dict[str, int],
    *,
    schema_version: str,
    exported_by: str,
    exported_at: datetime,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        **(extra or {}),
        "format_version": FORMAT_VERSION,
        "schema_version": schema_version,
        "source_story_id": story_id,
        "exported_revision": revision,
        "exported_at": exported_at.isoformat(),
        "exported_by": exported_by,
        "files": [
            {
                "path": name,
                "sha256": hashlib.sha256(data).hexdigest(),
                "size": len(data),
                "records": None if name.startswith("media/") else data.count(b"\n"),
            }
            for name, data in members
        ],
        "counts": counts,
    }


BlobReader = Callable[[str], bytes]
"""#704: reads a media blob's bytes by checksum (``FileNotFoundError`` when missing)."""


async def build(
    db: AsyncEngine,
    story_id: str,
    *,
    exported_by: str,
    secrets: Sequence[str] = (),
    blobs: BlobReader | None = None,
) -> tuple[bytes, dict[str, Any]]:
    """The archive of ``story_id`` at its current revision, and its manifest (one
    ``REPEATABLE READ`` snapshot); 404 for an unknown story. An erasure that touches the story
    while it runs raises ``ErasedMeanwhile`` (the caller restarts)."""
    async with db.connect() as conn, conn.begin():
        await conn.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"))
        snap = await _snapshot(conn, story_id)
        schema_version = await _head(conn)
    await pauses.pause(AFTER_SNAPSHOT)
    members = snap.members()
    for line in snap.media_lines:
        if blobs is None:
            raise MediaMissing(str(line["id"]))
        try:
            data = await asyncio.to_thread(blobs, str(line["sha256"]))
        except FileNotFoundError:
            raise MediaMissing(str(line["id"])) from None
        members.append((f"media/{line['sha256']}", data))
    by_name = dict(members)
    media = sorted(n for n in by_name if n.startswith("media/"))
    ordered_members = [(n, d) for n, d in members if not n.startswith("media/")] + [
        (n, by_name[n]) for n in media
    ]
    async with db.connect() as conn:
        if await _story_erasures(conn, story_id) != snap.erasures:
            raise ErasedMeanwhile(story_id)
    found = manifest(
        story_id,
        snap.revision,
        ordered_members,
        snap.counts,
        schema_version=schema_version,
        exported_by=exported_by,
        exported_at=clock.now(),
        extra=snap.extra,
    )
    ordered = [("manifest.json", canonical(found)), *ordered_members]
    await pauses.pause(BEFORE_SCAN)
    leaked = scan(ordered, secrets)
    if leaked is not None:
        raise SecretDetected(leaked)
    return archive(ordered), found


AFTER_SNAPSHOT = pauses.BUNDLE_EXPORT_AFTER_SNAPSHOT
BEFORE_SCAN = pauses.BUNDLE_EXPORT_BEFORE_SCAN
EXPORT_ATTEMPTS = 3
"""#704: an export restarted by an erasure race this many times fails ``export_failed``."""


class ErasedMeanwhile(Exception):
    """An erasure touching the story committed while it was exported (the export restarts)."""


class MediaMissing(Exception):
    """A referenced media asset's blob is missing (the export fails ``media_missing``)."""


class PayloadInvalid(Exception):
    """An event's payload fails its registry model (the export fails ``payload_invalid``)."""


class SecretDetected(Exception):
    """A member held a configured secret (the job fails ``secret_detected``)."""

    def __init__(self, member: str) -> None:
        super().__init__(member)
        self.member = member


# -- the job and its reads ------------------------------------------------------------------------


async def start(conn: AsyncConnection, story_id: str, *, principal: str) -> str:
    """Record a queued export of ``story_id`` (404 for an unknown story); its id."""
    if await conn.scalar(select(stories.c.id).where(stories.c.id == story_id)) is None:
        raise StoryNotFound(f"story {story_id} does not exist", {"story_id": story_id})
    job = new_ulid()
    await conn.execute(
        bundle_jobs.insert().values(id=job, story_id=story_id, requested_by=principal)
    )
    return job


async def run_export(
    db: AsyncEngine,
    job_id: str,
    directory: Path,
    *,
    secrets: Sequence[str] = (),
    blobs: BlobReader | None = None,
) -> dict[str, Any]:
    """The ``story_export`` job (see the module docstring); counts only."""
    async with db.begin() as conn:
        row = (
            await conn.execute(select(bundle_jobs).where(bundle_jobs.c.id == job_id))
        ).one_or_none()
        if row is None or str(row.status) not in ("queued", "running"):
            return {"skipped": 1}
        await conn.execute(
            update(bundle_jobs)
            .where(bundle_jobs.c.id == job_id)
            .values(status="running", attempts=bundle_jobs.c.attempts + 1)
        )
    try:
        data, found = await _build_retrying(db, job_id, row, secrets=secrets, blobs=blobs)
    except ErasedMeanwhile:
        await _fail(db, job_id, {"code": "export_failed", "reason": "erasure_race"})
        return {"failed": 1}
    except MediaMissing as exc:
        await _fail(
            db, job_id, {"code": "export_failed", "reason": "media_missing", "media_id": str(exc)}
        )
        return {"failed": 1}
    except PayloadInvalid as exc:
        await _fail(
            db, job_id, {"code": "export_failed", "reason": "payload_invalid", "event_id": str(exc)}
        )
        return {"failed": 1}
    except SecretDetected as exc:
        await _fail(db, job_id, {"code": "secret_detected", "member": exc.member})
        log.warning("story export refused: secret detected", job_id=job_id)
        return {"failed": 1}
    except StoryNotFound:
        await _fail(db, job_id, {"code": "story_not_found"})
        return {"failed": 1}
    final = directory / f"{job_id}.epistrel.tgz"
    await asyncio.to_thread(_write, directory, final, data)
    digest = hashlib.sha256(data).hexdigest()
    async with db.begin() as conn:
        await conn.execute(
            update(bundle_jobs)
            .where(bundle_jobs.c.id == job_id)
            .values(
                status="ready",
                exported_revision=int(found["exported_revision"]),
                path=str(final),
                size=len(data),
                sha256=digest,
                manifest=found,
                finished_at=clock.now(),
                expires_at=clock.now() + await _ttl(db),
            )
        )
    log.info("story exported", job_id=job_id, size=len(data))
    return {"ready": 1, "size": len(data)}


async def _ttl(db: AsyncEngine) -> timedelta:
    """#704: ``bundle_export_ttl_hours``, the deployment's setting."""
    from epistrel.core.config_changes import read_section
    from epistrel.core.settings_schema import RetentionSettings

    return timedelta(hours=(await read_section(db, RetentionSettings)).bundle_export_ttl_hours)


async def _build_retrying(
    db: AsyncEngine,
    job_id: str,
    row: Any,
    *,
    secrets: Sequence[str],
    blobs: BlobReader | None,
) -> tuple[bytes, dict[str, Any]]:
    """``build``, restarted after an erasure race (#704) while attempts remain; each restart
    is counted on the job's ``attempts``."""
    attempt = 1
    while True:
        try:
            return await build(
                db,
                str(row.story_id),
                exported_by=str(row.requested_by),
                secrets=secrets,
                blobs=blobs,
            )
        except ErasedMeanwhile:
            if attempt >= EXPORT_ATTEMPTS:
                raise
            attempt += 1
            async with db.begin() as conn:
                await conn.execute(
                    update(bundle_jobs)
                    .where(bundle_jobs.c.id == job_id)
                    .values(attempts=bundle_jobs.c.attempts + 1)
                )
            log.info("story export restarted: an erasure landed", job_id=job_id)


async def expire_bundles(db: AsyncEngine, now: datetime | None = None) -> int:
    """#704: every ready export past its ``expires_at``: the file deleted, ``expired``; how
    many (``retention_sweep``)."""
    moment = now or clock.now()
    async with db.begin() as conn:
        rows = list(
            await conn.execute(
                select(bundle_jobs.c.id, bundle_jobs.c.path)
                .where(bundle_jobs.c.status == "ready", bundle_jobs.c.expires_at <= moment)
                .with_for_update(skip_locked=True)
            )
        )
        for row in rows:
            _unlink(row.path)
        if rows:
            await conn.execute(
                update(bundle_jobs)
                .where(bundle_jobs.c.id.in_([r.id for r in rows]))
                .values(status="expired")
            )
    return len(rows)


async def delete_story_bundles(conn: AsyncConnection, story_id: str) -> int:
    """#704: a story's exports and their files, deleted with the story. #250: the record of
    the import that made it is deployment-level and kept, no longer naming the story."""
    await conn.execute(
        update(bundle_jobs)
        .where(bundle_jobs.c.story_id == story_id, bundle_jobs.c.kind == "import")
        .values(story_id=None)
    )
    exports = (bundle_jobs.c.story_id == story_id) & (bundle_jobs.c.kind == "export")
    rows = list(await conn.execute(select(bundle_jobs.c.id, bundle_jobs.c.path).where(exports)))
    for row in rows:
        _unlink(row.path)
    if rows:
        await conn.execute(bundle_jobs.delete().where(exports))
    return len(rows)


def _unlink(path: Any) -> None:
    if path:
        Path(str(path)).unlink(missing_ok=True)


class StoryExportsTarget:
    """#704 (FR-ADMIN-8): the stored exports an erasure reaches. A story-scope erasure expires
    that story's stored, unexpired exports (file deleted, error ``erased``); a dry run counts
    them. A delivered archive is outside the boundary."""

    name = "story_exports"

    async def erase(
        self, conn: AsyncConnection, *, story_id: str | None, redactor: Any, write: bool
    ) -> int:
        if story_id is None:
            return 0
        rows = list(
            await conn.execute(
                select(bundle_jobs.c.id, bundle_jobs.c.path).where(
                    bundle_jobs.c.story_id == story_id,
                    bundle_jobs.c.status.in_(["queued", "running", "ready"]),
                )
            )
        )
        if write:
            for row in rows:
                _unlink(row.path)
            if rows:
                await conn.execute(
                    update(bundle_jobs)
                    .where(bundle_jobs.c.id.in_([r.id for r in rows]))
                    .values(status="expired", error={"code": "erased"})
                )
        return len(rows)


def _register() -> None:
    from epistrel.core.erasure.erase import register_target

    register_target(StoryExportsTarget())


_register()


def _write(directory: Path, final: Path, data: bytes) -> None:
    """Stage the archive, then rename it into place (never a partial file)."""
    directory.mkdir(parents=True, exist_ok=True)
    staged = directory / f".{final.name}.tmp"
    staged.write_bytes(data)
    staged.rename(final)


async def _fail(db: AsyncEngine, job_id: str, error: dict[str, Any]) -> None:
    async with db.begin() as conn:
        await conn.execute(
            update(bundle_jobs)
            .where(bundle_jobs.c.id == job_id)
            .values(status="failed", error=error, finished_at=clock.now())
        )


async def _row(db: AsyncEngine, job_id: str) -> Any:
    async with db.connect() as conn:
        row = (
            await conn.execute(
                select(bundle_jobs).where(
                    bundle_jobs.c.id == job_id, bundle_jobs.c.kind == "export"
                )
            )
        ).one_or_none()
    if row is None:
        raise ExportNotFound("no such export", {"job_id": job_id})
    return row


async def status(db: AsyncEngine, job_id: str) -> ExportJob:
    row = await _row(db, job_id)
    return ExportJob(
        job_id=str(row.id),
        story_id=str(row.story_id),
        status=str(row.status),
        exported_revision=row.exported_revision,
        manifest=row.manifest,
        size=row.size,
        sha256=row.sha256,
        expires_at=row.expires_at,
        error=row.error,
    )


async def bundle(db: AsyncEngine, job_id: str) -> Bundle:
    """A ready export's archive: 409 ``export_not_ready`` before, 410 ``export_expired``
    after it expired."""
    row = await _row(db, job_id)
    if str(row.status) == "expired" or (
        row.expires_at is not None and row.expires_at <= clock.now()
    ):
        raise ExportExpired("the export has expired", {"job_id": job_id})
    if str(row.status) != "ready" or not row.path:
        raise ExportNotReady("the export is not ready", {"job_id": job_id, "status": row.status})
    data = await asyncio.to_thread(Path(str(row.path)).read_bytes)
    revision = int(row.exported_revision or 0)
    return Bundle(data, f"{row.story_id}-r{revision}.epistrel.tgz", str(row.sha256))


def read_archive(data: bytes) -> list[tuple[str, bytes]]:
    """An archive's members in order (tests and #250's import)."""
    found: list[tuple[str, bytes]] = []
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        for member in tar.getmembers():
            if not member.isreg():
                raise ValueError(f"bundle member {member.name} is not a regular file")
            handle = tar.extractfile(member)
            found.append((member.name, b"" if handle is None else handle.read()))
    return found


__all__ = [
    "AFTER_SNAPSHOT",
    "BEFORE_SCAN",
    "CATALOG",
    "EXPORT_ATTEMPTS",
    "EXPORT_KIND",
    "FORMAT_VERSION",
    "NEVER",
    "REBUILD",
    "SPECIAL",
    "Bundle",
    "ErasedMeanwhile",
    "ExportJob",
    "ExportStarted",
    "MediaMissing",
    "PayloadInvalid",
    "SecretDetected",
    "StoryExportsTarget",
    "archive",
    "build",
    "bundle",
    "canonical",
    "catalog_tables",
    "classify",
    "delete_story_bundles",
    "expire_bundles",
    "exported_tables",
    "manifest",
    "read_archive",
    "run_export",
    "scan",
    "start",
    "status",
    "story_tables",
]
