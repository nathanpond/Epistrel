"""Helpers for the Lite rolling-summary tests (#72): run a story's due ``lite_summary`` jobs in
the test (no background worker), and read its segment rows and summary jobs."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.core import clock
from epistrel.orchestrator.jobs.handlers import lite_summary
from epistrel.orchestrator.jobs.types import ClaimedJob
from epistrel.orchestrator.jobs.worker import Worker, WorkerConfig
from epistrel.orchestrator.models.access import ModelAccessLayer
from tests.event_support import rows

LEASE_S = 60.0

_CLAIM = text(
    "UPDATE jobs SET status = 'running', lease_until = :lease, attempt = attempt + 1, "
    "worker_id = 'w:test:0', updated_at = :now "
    "WHERE id = (SELECT id FROM jobs WHERE kind = 'lite_summary' AND story_id = :s "
    "AND status = 'queued' AND due_at <= :now ORDER BY due_at, id LIMIT 1 "
    "FOR UPDATE SKIP LOCKED) RETURNING *"
)


async def claim_summary(engine: AsyncEngine, story_id: str) -> ClaimedJob | None:
    """Claim the story's due ``lite_summary`` job (the queue's own claim, scoped to it)."""
    now = clock.now()
    async with engine.begin() as conn:
        row = (
            await conn.execute(
                _CLAIM, {"s": story_id, "now": now, "lease": now + timedelta(seconds=LEASE_S)}
            )
        ).one_or_none()
    if row is None:
        return None
    return ClaimedJob(
        id=row.id,
        kind=row.kind,
        story_id=row.story_id,
        payload=dict(row.payload or {}),
        attempt=int(row.attempt),
        failures=int(row.failures),
        due_at=row.due_at,
        lease_until=row.lease_until,
        worker_id=row.worker_id,
        coalesce_key=row.coalesce_key,
    )


def worker(engine: AsyncEngine) -> Worker:
    return Worker(
        engine,
        WorkerConfig(poll_interval=0.3, lease_s=LEASE_S, listen=False, concurrency=1, timers=False),
    )


async def run_due_jobs(
    engine: AsyncEngine, models: ModelAccessLayer, story_id: str, *, limit: int = 10
) -> list[Any]:
    """Run the story's due ``lite_summary`` jobs one at a time through the worker's own
    ``execute`` (handler, then ``commit_job``) with ``models`` bound; the outcomes in order."""
    lite_summary.bind(models)
    outcomes: list[Any] = []
    for _ in range(limit):
        job = await claim_summary(engine, story_id)
        if job is None:
            break
        outcomes.append(await worker(engine).execute(job))
    return outcomes


async def segment_rows(engine: AsyncEngine, story_id: str) -> list[Any]:
    """The story's ``lite_summary_segments`` rows, by covered turns then level."""
    return await rows(
        engine,
        "SELECT segment_id, operation_id, covers_from, covers_to, level, turn_variant, text, "
        "membership::text AS membership, source_versions FROM lite_summary_segments "
        "WHERE story_id = :s ORDER BY covers_from, covers_to, level, seq",
        s=story_id,
    )


async def summary_jobs(engine: AsyncEngine, story_id: str) -> list[Any]:
    return await rows(
        engine,
        "SELECT id, status, result, coalesce_key FROM jobs "
        "WHERE story_id = :s AND kind = 'lite_summary' ORDER BY created_at, id",
        s=story_id,
    )


__all__ = [
    "claim_summary",
    "run_due_jobs",
    "segment_rows",
    "summary_jobs",
    "worker",
]
