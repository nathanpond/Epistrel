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
4. **Upload sessions** (#107; ``core.media.uploads.sweep_sessions``). Open sessions past their
   expiry (``media_upload_ttl`` after their last part) are aborted and their parts removed;
   completed and aborted sessions are deleted once the idempotency result window has passed
   since they closed; and parts a store holds for no open session are removed once they are
   ``media_upload_ttl`` old (a crash between a session's opening and its row).

5. **Feedback exports** (#126). An export's file (``exports/<id>.jsonl``) is deleted
   ``EXPORT_TTL`` (seven days) after it was written, and its ``feedback_export_log`` row is
   marked ``deleted_at``/``deleted_by = system``; the row stays, as the record of the export.

Each run handles at most ``MAX_BATCHES`` batches of ``BATCH`` rows or blobs, and the next run
goes on from there, so the sweep eventually reaches every row and every blob however many there
are:

- Step 1's batches hold only assets nothing references (``assets.unreferenced``, an anti-join
  over the sources, applied before the batch's limit). A referenced asset never takes a place in
  a batch, so each run purges up to the limit and the next run purges the next ones. Only an
  asset a writer holds, or one a reference reached since its batch was read, stays for a later
  run.
- Step 2 scans the blobs in checksum order from the store's sweep cursor (where the previous
  run stopped) and leaves the cursor where it stopped; a run that reaches the last blob resets
  it, so the next run starts again at the first.

Every step is idempotent, so a run repeated after a crash changes nothing more. The result and
the log line carry counts only.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from itertools import islice
from typing import Any

from sqlalchemy import and_, delete, select, tuple_, update
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.core import clock, ownership
from epistrel.core.context import SYSTEM_PRINCIPAL
from epistrel.core.erasure.index import drop_references
from epistrel.core.media.assets import RECORD_TABLE, lock_checksum, referenced, unreferenced
from epistrel.core.media.store import BlobStore
from epistrel.core.media.uploads import sweep_sessions
from epistrel.core.tables.feedback import feedback_export_log
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
EXPORT_TTL = timedelta(days=7)
"""A feedback export's file is deleted this long after it was written (#126)."""


def _eligible(now: datetime) -> Any:
    """The rows step 1 may purge: retired ones, and live ones past the grace period."""
    return and_(
        media_assets.c.state.in_(_SWEPT),
        (media_assets.c.state == MediaState.RETIRED.value)
        | (media_assets.c.created_at <= now - LIVE_GRACE),
    )


async def collect_blob(db: AsyncEngine, store: BlobStore, checksum: str) -> bool:
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


async def _purge_unreferenced(db: AsyncEngine, store: BlobStore, now: datetime) -> tuple[int, int]:
    purged = blobs = 0
    after: tuple[datetime, str] | None = None
    async with db.connect() as conn:
        free = await unreferenced(conn)
    for _ in range(MAX_BATCHES):
        query = select(media_assets.c.id, media_assets.c.created_at).where(_eligible(now), free)
        if after is not None:
            query = query.where(tuple_(media_assets.c.created_at, media_assets.c.id) > after)
        query = query.order_by(media_assets.c.created_at, media_assets.c.id).limit(BATCH)
        async with db.connect() as conn:
            rows = (await conn.execute(query)).all()
        for row in rows:
            await pauses.pause(pauses.MEDIA_SWEEP_BEFORE_PURGE)
            checksum = await _purge(db, str(row.id), now)
            if checksum is not None:
                purged += 1
                blobs += await collect_blob(db, store, checksum)
        if len(rows) < BATCH:
            break
        after = (rows[-1].created_at, str(rows[-1].id))
    return purged, blobs


async def _collect_orphans(db: AsyncEngine, store: BlobStore) -> int:
    deleted = 0
    limit = BATCH * MAX_BATCHES
    resume = await asyncio.to_thread(store.sweep_cursor)
    listed = await asyncio.to_thread(
        lambda: list(islice(store.list_checksums(after=resume), limit + 1))
    )
    found, more = listed[:limit], len(listed) > limit
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
    # The next run goes on after the last blob this one examined, or starts again at the first
    # once this one reached the last.
    await asyncio.to_thread(store.set_sweep_cursor, found[-1] if more else None)
    return deleted


async def expire_exports(db: AsyncEngine, store: BlobStore, now: datetime) -> int:
    """Delete the files of the feedback exports written ``EXPORT_TTL`` ago or earlier, each
    under its row's lock (an export another transaction holds, a download marking it or an
    erasure rewriting it, waits for the next run), marking the row; how many."""
    expired = 0
    for _ in range(MAX_BATCHES):
        async with db.begin() as conn:
            rows = (
                await conn.execute(
                    select(feedback_export_log.c.id, feedback_export_log.c.blob_key)
                    .where(
                        feedback_export_log.c.deleted_at.is_(None),
                        feedback_export_log.c.at <= now - EXPORT_TTL,
                    )
                    .order_by(feedback_export_log.c.at, feedback_export_log.c.id)
                    .limit(BATCH)
                    .with_for_update(skip_locked=True)
                )
            ).all()
            for row in rows:
                await asyncio.to_thread(store.delete_object, str(row.blob_key))
            if rows:
                await conn.execute(
                    update(feedback_export_log)
                    .where(feedback_export_log.c.id.in_([str(r.id) for r in rows]))
                    .values(deleted_at=now, deleted_by=SYSTEM_PRINCIPAL)
                )
        expired += len(rows)
        if len(rows) < BATCH:
            break
    return expired


def _clear_staging(store: BlobStore) -> int:
    removed = 0
    for path, age in list(store.staged_files()):
        if age >= ORPHAN_AGE_S:
            path.unlink(missing_ok=True)
            removed += 1
    return removed


async def sweep(
    db: AsyncEngine, store: BlobStore, *, now: datetime | None = None
) -> dict[str, int]:
    """One media sweep (see the module docstring); the counts of what it removed."""
    moment = now or clock.now()
    purged, blobs = await _purge_unreferenced(db, store, moment)
    blobs += await _collect_orphans(db, store)
    staged = await asyncio.to_thread(_clear_staging, store)
    sessions = await sweep_sessions(db, store, now=moment)
    exports = await expire_exports(db, store, moment)
    logger.info(
        "media sweep: %d asset(s) purged, %d blob(s) deleted, %d staged upload(s) removed, "
        "%d upload session(s) expired, %d closed session(s) deleted, %d stray part upload(s) "
        "removed, %d feedback export(s) expired",
        purged,
        blobs,
        staged,
        sessions["sessions_expired"],
        sessions["sessions_purged"],
        sessions["parts_removed"],
        exports,
    )
    return {
        "assets_purged": purged,
        "blobs_deleted": blobs,
        "staged_removed": staged,
        **sessions,
        "exports_expired": exports,
    }


__all__ = [
    "BATCH",
    "EXPORT_TTL",
    "LIVE_GRACE",
    "MAX_BATCHES",
    "ORPHAN_AGE_S",
    "collect_blob",
    "expire_exports",
    "sweep",
]
