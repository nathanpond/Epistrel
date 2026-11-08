"""Helpers for the image limit tests (#262): a scratch database per test (limits, the time zone
and the counters are deployment-wide, so they never reach the shared database), the REST client
and Engine on it, and reads of the reservation records and counters."""

from __future__ import annotations

import secrets
from collections.abc import AsyncIterator, Iterator, Mapping
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx2 as httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.config import Settings
from epistrel.core import clock
from epistrel.core.db import make_engine
from epistrel.core.deployment import apply_settings
from epistrel.core.images import limits
from epistrel.core.tables.images import ImageJobStatus
from epistrel.engine import Engine
from epistrel.orchestrator.images import jobs as image_jobs
from epistrel.orchestrator.images.jobs import PreparedImageJob
from epistrel.testing.images import TestImageTarget
from tests.conformance.conftest import migrated_database
from tests.image_job_support import configure, image_settings_for
from tests.respond_support import respond_client

ADAPTER = "stub_sync"
OPERATOR = "op-limits"
POOL = 40
"""Connections the scratch engine keeps: every concurrent request holds one at its barrier."""


@dataclass
class Space:
    client: httpx.AsyncClient
    lib: Engine
    settings: Settings
    db: AsyncEngine

    async def configure(self) -> None:
        await configure(self.lib, self.settings, ADAPTER)

    async def set(self, **changes: Any) -> None:
        await apply_settings(self.db, changes, principal=OPERATOR)

    async def insert(self, job: PreparedImageJob) -> str:
        async with self.db.begin() as conn:
            return (await image_jobs.insert_image_job(conn, job, None)).job_id

    async def run(self, job_id: str, failures: int = 0) -> dict[str, Any]:
        return await image_jobs.run_image_job(self.lib.image_runtime(), job_id, failures)


@asynccontextmanager
async def space(pg_url: str, tmp_path: Path, name: str) -> AsyncIterator[Space]:
    """The REST app and its Engine on a fresh migrated database of their own."""
    with migrated_database(pg_url, f"epistrel_{name}_{secrets.token_hex(3)}") as url:
        settings = image_settings_for(url, tmp_path)
        db = make_engine(settings, pool_size=POOL)
        try:
            async with respond_client(settings, db) as (client, lib):
                lib.prepare_media()
                try:
                    yield Space(client, lib, settings, db)
                finally:
                    await lib.image_waiters.aclose()
                    await lib.models.aclose()
        finally:
            await db.dispose()


class CatalogTarget(TestImageTarget):
    """The test target as a catalog job's: its outputs are candidates."""

    success_status = ImageJobStatus.CANDIDATES_READY

    async def result_ids(self, conn: Any, job: Any) -> tuple[str, list[str]]:
        return "candidate_ids", list(job.asset_ids)


async def counters(db: AsyncEngine) -> dict[tuple[str, str, str], dict[str, int]]:
    """Every counter row: ``{(scope, scope_key, period): {limit_value, reserved, used, overage,
    extra_outputs}}``."""
    async with db.connect() as conn:
        rows = await conn.execute(
            text(
                "SELECT scope, scope_key, period::text AS period, limit_value, reserved, used, "
                "overage, extra_outputs FROM image_limit_counters"
            )
        )
        return {
            (r.scope, r.scope_key, r.period): {
                "limit_value": r.limit_value,
                "reserved": r.reserved,
                "used": r.used,
                "overage": r.overage,
                "extra_outputs": r.extra_outputs,
            }
            for r in rows
        }


async def deployment(db: AsyncEngine, period: str) -> dict[str, int]:
    return (await counters(db))[("deployment", "deployment", period)]


async def records(db: AsyncEngine, job_id: str) -> list[limits.ReservationRecord]:
    async with db.connect() as conn:
        return await limits.records(conn, job_id)


async def all_records(db: AsyncEngine) -> list[Mapping[str, Any]]:
    async with db.connect() as conn:
        rows = await conn.execute(
            text(
                "SELECT job_id, attempt_token, story_id, entry_kind, entry_id, counter_keys, "
                "requested, used, overage, extra_outputs, basis, state FROM image_reservations "
                "ORDER BY created_at, attempt_token"
            )
        )
        return [dict(r._mapping) for r in rows]


async def assert_clean(db: AsyncEngine) -> None:
    """The stored counters equal their records' sums and no held record is stuck."""
    report = await limits.reconcile(db)
    assert report.clean, report.as_json()


def today() -> str:
    return clock.now().date().isoformat()


@contextmanager
def at(moment: datetime) -> Iterator[None]:
    with clock.frozen_at(moment):
        yield


__all__ = [
    "ADAPTER",
    "OPERATOR",
    "CatalogTarget",
    "Space",
    "all_records",
    "assert_clean",
    "at",
    "counters",
    "deployment",
    "records",
    "space",
    "today",
]
