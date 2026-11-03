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
``EPISTREL_BUNDLE_DIR``: ``manifest.json`` first (format ``1.0.0``, the source story, the
revision, the Alembic head, when and by whom, and per file its path, SHA-256, size and record
count), then ``records/story.json``, ``events.jsonl`` and ``records/<table>.jsonl`` in a fixed
order. Lines are canonical JSON (sorted keys, UTF-8, ``\\n``), so two exports of one revision
differ only in the manifest's ``exported_at`` and ``exported_by``. Before the archive is kept,
every member is scanned for the deployment's secrets (service credentials and the config key,
raw, base64 and hex): a hit fails the job (``secret_detected``) and leaves no file.

Projections, indexes, vectors, deployment-level records, in-flight work and secrets are never in
a bundle. Media, catalog snapshots outside the story's own tables, expiry and the erasure target
are not built yet (#704).
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json
import tarfile
from collections.abc import Iterable, Sequence
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

log = get_logger(__name__)

FORMAT_VERSION = "1.0.0"
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
        "turn_queue",
        "timers",
        "audit_log",
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
        if t.name not in REBUILD and t.name not in NEVER and t.name not in SPECIAL
    ]


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


def _lines(rows: Iterable[Any]) -> tuple[bytes, int]:
    body = bytearray()
    count = 0
    for row in rows:
        body += canonical(dict(row._mapping))
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


async def _snapshot(
    conn: AsyncConnection, story_id: str
) -> tuple[int, list[tuple[str, bytes]], dict[str, int]]:
    story = (await conn.execute(select(stories).where(stories.c.id == story_id))).one_or_none()
    if story is None:
        raise StoryNotFound(f"story {story_id} does not exist", {"story_id": story_id})
    revision = int(story.timeline_revision)
    members: list[tuple[str, bytes]] = [("records/story.json", canonical(dict(story._mapping)))]
    counts: dict[str, int] = {}
    o = operations.c
    committed = o.status != OperationStatus.IN_FLIGHT.value
    data, counts["operations"] = _lines(
        await conn.execute(
            select(operations).where(o.story_id == story_id, committed).order_by(o.operation_id)
        )
    )
    members.append(("records/operations.jsonl", data))
    kept = select(o.operation_id).where(o.story_id == story_id, committed)
    data, counts["events"] = _lines(
        await conn.execute(
            select(events)
            .where(events.c.story_id == story_id, events.c.operation_id.in_(kept))
            .order_by(events.c.seq)
        )
    )
    members.append(("events.jsonl", data))
    for table in exported_tables():
        key = table.c.story_id
        order = [c for c in table.primary_key.columns] or list(table.c)[:1]
        data, counts[table.name] = _lines(
            await conn.execute(select(table).where(key == story_id).order_by(*order))
        )
        members.append((f"records/{table.name}.jsonl", data))
    ledger = metadata.tables.get("erasure_ledger")
    if ledger is not None:
        data, counts["erasure_ledger"] = _lines(
            await conn.execute(
                select(ledger).where(ledger.c.story_ref == story_id).order_by(ledger.c.id)
            )
        )
        members.append(("records/erasure_ledger.jsonl", data))
    return revision, members, counts


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
) -> dict[str, Any]:
    return {
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
                "records": data.count(b"\n"),
            }
            for name, data in members
        ],
        "counts": counts,
    }


async def build(
    db: AsyncEngine, story_id: str, *, exported_by: str, secrets: Sequence[str] = ()
) -> tuple[bytes, dict[str, Any]]:
    """The archive of ``story_id`` at its current revision, and its manifest (one
    ``REPEATABLE READ`` snapshot); 404 for an unknown story."""
    async with db.connect() as conn, conn.begin():
        await conn.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"))
        revision, members, counts = await _snapshot(conn, story_id)
        schema_version = await _head(conn)
    found = manifest(
        story_id,
        revision,
        members,
        counts,
        schema_version=schema_version,
        exported_by=exported_by,
        exported_at=clock.now(),
    )
    ordered = [("manifest.json", canonical(found)), *members]
    leaked = scan(ordered, secrets)
    if leaked is not None:
        raise SecretDetected(leaked)
    return archive(ordered), found


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
    db: AsyncEngine, job_id: str, directory: Path, *, secrets: Sequence[str] = ()
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
        data, found = await build(
            db, str(row.story_id), exported_by=str(row.requested_by), secrets=secrets
        )
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
                expires_at=clock.now() + TTL,
            )
        )
    log.info("story exported", job_id=job_id, size=len(data))
    return {"ready": 1, "size": len(data)}


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
            await conn.execute(select(bundle_jobs).where(bundle_jobs.c.id == job_id))
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
    "EXPORT_KIND",
    "FORMAT_VERSION",
    "NEVER",
    "REBUILD",
    "SPECIAL",
    "Bundle",
    "ExportJob",
    "ExportStarted",
    "SecretDetected",
    "archive",
    "build",
    "bundle",
    "canonical",
    "classify",
    "exported_tables",
    "manifest",
    "read_archive",
    "run_export",
    "scan",
    "start",
    "status",
    "story_tables",
]
