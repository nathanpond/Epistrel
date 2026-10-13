"""The media sweep: purge what nothing references, then the bytes nothing names
(FR-MEDIA-2, NFR-REL-5; #105).

The ``media_sweep`` recurring job (``orchestrator.jobs.handlers.media_sweep``, every
``media_sweep_interval``, default 1 hour) runs ``sweep``:

1. **Assets.** A ``live`` or ``retired`` asset row is purged when no record of
   ``MEDIA_REFERENCE_SOURCES`` references it (a catalog draft or version now; story snapshots
   and live candidate records register theirs when they exist). References are found by
   querying those records, never by counting. A ``live`` asset younger than 24 hours is kept, so
   an upload is not purged before its uploader references it, and ``candidate`` rows are never
   touched here (only M8's candidate-expiry sweep removes them). Each purge is one transaction:
   the row is locked (skipped if a writer holds it), its references are checked again, its
   relations go with a ``deleted`` ownership event by ``system`` (``core.ownership``), its
   subject references go (``core.erasure.index``), then the row.
2. **Bytes.** A blob is deleted only when no asset row of any state still has its checksum,
   checked under the checksum's advisory lock (the one an upload holds from its insert to its
   commit): at once for a purged row's checksum, and after an hour for any other blob nothing
   names (left by an upload whose transaction failed after its bytes moved into place). Two
   assets with identical bytes share one blob, so purging one keeps the bytes.
3. **Staging.** Staged uploads older than an hour (a crashed upload's) are removed.

Each run handles at most ``MAX_BATCHES`` batches of ``BATCH`` rows or blobs; the next run takes
the rest. Every step is idempotent, so a run repeated after a crash changes nothing more. The
result and the log line carry counts only.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from itertools import islice
from typing import Any

from sqlalchemy import and_, delete, select, tuple_
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.core import clock, ownership
from epistrel.core.context import SYSTEM_PRINCIPAL
from epistrel.core.erasure.index import drop_references
from epistrel.core.media.assets import RECORD_TABLE, lock_checksum, referenced
from epistrel.core.media.store import LocalFilesystemStore
from epistrel.core.tables.media import MediaState, media_assets
from epistrel.core.tables.ownership import EntityType
from epistrel.testing import pauses

logger = logging.getLogger(__name__)

BATCH = 500
MAX_BATCHES = 20
LIVE_GRACE = timedelta(hours=24)
"""A ``live`` asset this young is never purged."""
ORPHAN_AGE_S = 3_600.0
"""A blob nothing names, or a staged upload, this old is removed."""
_SWEPT = (MediaState.LIVE.value, MediaState.RETIRED.value)


def _eligible(now: datetime) -> Any:
    """The rows step 1 may purge: retired ones, and live ones past the grace period."""
    return and_(
        media_assets.c.state.in_(_SWEPT),
        (media_assets.c.state == MediaState.RETIRED.value)
        | (media_assets.c.created_at <= now - LIVE_GRACE),
    )


async def collect_blob(db: AsyncEngine, store: LocalFilesystemStore, checksum: str) -> bool:
    """Delete blob ``checksum`` if no asset row of any state has it (under its lock)."""
    async with db.begin() as conn:
        await lock_checksum(conn, checksum)
        named = await conn.scalar(
            select(media_assets.c.id).where(media_assets.c.checksum == checksum).limit(1)
        )
        if named is not None:
            return False
        return await asyncio.to_thread(store.delete, checksum)


async def _purge(db: AsyncEngine, asset_id: str, now: datetime) -> str | None:
    """Purge one asset if it is still eligible and unreferenced; its checksum, or ``None``."""
    async with db.begin() as conn:
        row = (
            await conn.execute(
                select(media_assets.c.checksum)
                .where(media_assets.c.id == asset_id, _eligible(now))
                .with_for_update(skip_locked=True)
            )
        ).one_or_none()
        if row is None or await referenced(conn, [asset_id]):
            return None
        await ownership.drop_entity(conn, EntityType.MEDIA, asset_id, principal=SYSTEM_PRINCIPAL)
        await drop_references(conn, RECORD_TABLE, asset_id)
        await conn.execute(delete(media_assets).where(media_assets.c.id == asset_id))
        return str(row.checksum)


async def _purge_unreferenced(
    db: AsyncEngine, store: LocalFilesystemStore, now: datetime
) -> tuple[int, int]:
    purged = blobs = 0
    after: tuple[datetime, str] | None = None
    for _ in range(MAX_BATCHES):
        query = select(media_assets.c.id, media_assets.c.created_at).where(_eligible(now))
        if after is not None:
            query = query.where(tuple_(media_assets.c.created_at, media_assets.c.id) > after)
        query = query.order_by(media_assets.c.created_at, media_assets.c.id).limit(BATCH)
        async with db.connect() as conn:
            rows = (await conn.execute(query)).all()
            kept = await referenced(conn, [str(row.id) for row in rows])
        for row in rows:
            if str(row.id) in kept:
                continue
            await pauses.pause(pauses.MEDIA_SWEEP_BEFORE_PURGE)
            checksum = await _purge(db, str(row.id), now)
            if checksum is not None:
                purged += 1
                blobs += await collect_blob(db, store, checksum)
        if len(rows) < BATCH:
            break
        after = (rows[-1].created_at, str(rows[-1].id))
    return purged, blobs


async def _collect_orphans(db: AsyncEngine, store: LocalFilesystemStore) -> int:
    deleted = 0
    found = await asyncio.to_thread(
        lambda: list(islice(store.list_checksums(), BATCH * MAX_BATCHES))
    )
    for start in range(0, len(found), BATCH):
        batch = found[start : start + BATCH]
        async with db.connect() as conn:
            named = set(
                await conn.scalars(
                    select(media_assets.c.checksum).where(media_assets.c.checksum.in_(batch))
                )
            )
        for checksum in batch:
            if checksum in named:
                continue
            age = await asyncio.to_thread(store.age, checksum)
            if age is not None and age >= ORPHAN_AGE_S:
                deleted += await collect_blob(db, store, checksum)
    return deleted


def _clear_staging(store: LocalFilesystemStore) -> int:
    removed = 0
    for path, age in list(store.staged_files()):
        if age >= ORPHAN_AGE_S:
            path.unlink(missing_ok=True)
            removed += 1
    return removed


async def sweep(
    db: AsyncEngine, store: LocalFilesystemStore, *, now: datetime | None = None
) -> dict[str, int]:
    """One media sweep (see the module docstring); the counts of what it removed."""
    moment = now or clock.now()
    purged, blobs = await _purge_unreferenced(db, store, moment)
    blobs += await _collect_orphans(db, store)
    staged = await asyncio.to_thread(_clear_staging, store)
    logger.info(
        "media sweep: %d asset(s) purged, %d blob(s) deleted, %d staged upload(s) removed",
        purged,
        blobs,
        staged,
    )
    return {"assets_purged": purged, "blobs_deleted": blobs, "staged_removed": staged}


__all__ = ["BATCH", "LIVE_GRACE", "MAX_BATCHES", "ORPHAN_AGE_S", "collect_blob", "sweep"]
