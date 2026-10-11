"""Worker heartbeats and the background-work part of ``GET /health`` (NFR-REL-5; #71).

Every worker loop has a ``worker_heartbeats`` row (``host:pid:loop``) that its process upserts
every poll tick (``beat``) and deletes when it stops (``forget``); the writer also prunes rows
older than ``PRUNE_AFTER`` (a process that died). Heartbeats are rows, not memory, so an ``api``
process with no workers of its own can still report them.

``snapshot`` reads, in one statement and with the database's own ``now()``: how many workers
sent a heartbeat within ``worker_heartbeat_timeout``, the age of the newest heartbeat, the age
of the oldest overdue timer or registered recurring task, and the age of the oldest overdue
queued job (a leased, running job is not overdue). ``degraded`` is: no live worker, or an
overdue timer, task or job older than ``timer_lag_alert``.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy import Float, bindparam, delete, func, text
from sqlalchemy.dialects.postgresql import ARRAY, insert
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine
from sqlalchemy.types import Text

from epistrel.core.settings_schema import WorkerHealthSettings
from epistrel.core.tables import worker_heartbeats

PRUNE_AFTER = timedelta(minutes=10)
"""Heartbeat rows older than this are deleted by the next writer (their process is gone)."""


async def beat(
    db: AsyncEngine, worker_ids: Iterable[str], *, mode: str, pid: int, started_at: datetime
) -> None:
    """Upsert one heartbeat per worker loop, stamped with the database's ``now()``, and prune
    rows older than ``PRUNE_AFTER``."""
    rows = [
        {"worker_id": worker_id, "mode": mode, "pid": pid, "started_at": started_at}
        for worker_id in worker_ids
    ]
    if not rows:
        return
    statement = insert(worker_heartbeats).values([{**row, "at": func.now()} for row in rows])
    async with db.begin() as conn:
        await conn.execute(
            statement.on_conflict_do_update(
                index_elements=[worker_heartbeats.c.worker_id],
                set_={"at": func.now(), "mode": statement.excluded.mode},
            )
        )
        await conn.execute(
            delete(worker_heartbeats).where(worker_heartbeats.c.at < func.now() - PRUNE_AFTER)
        )


async def forget(db: AsyncEngine, worker_ids: Iterable[str]) -> None:
    """Delete these loops' heartbeats (a clean stop)."""
    ids = list(worker_ids)
    if not ids:
        return
    async with db.begin() as conn:
        await conn.execute(delete(worker_heartbeats).where(worker_heartbeats.c.worker_id.in_(ids)))


@dataclass(frozen=True, slots=True)
class Snapshot:
    alive: int
    last_heartbeat_s: float | None
    oldest_overdue_timer_s: float | None
    oldest_overdue_job_s: float | None

    def degraded(self, settings: WorkerHealthSettings) -> bool:
        lag = settings.timer_lag_alert
        overdue = [
            age
            for age in (self.oldest_overdue_timer_s, self.oldest_overdue_job_s)
            if age is not None
        ]
        return self.alive == 0 or any(age > lag for age in overdue)


SNAPSHOT_SQL = text(
    """
    SELECT
      (SELECT count(*) FROM worker_heartbeats
        WHERE at > now() - make_interval(secs => :timeout)) AS alive,
      (SELECT extract(epoch FROM now() - max(at)) FROM worker_heartbeats) AS last_heartbeat_s,
      (SELECT extract(epoch FROM now() - min(due)) FROM (
          SELECT due_at AS due FROM timers
           WHERE due_at < now() AND (lease_until IS NULL OR lease_until < now())
          UNION ALL
          SELECT next_run_at FROM recurring_tasks
           WHERE next_run_at < now() AND name = ANY(:names)
        ) AS overdue) AS oldest_overdue_timer_s,
      (SELECT extract(epoch FROM now() - min(due_at)) FROM jobs
        WHERE status = 'queued' AND due_at < now()) AS oldest_overdue_job_s
    """
).bindparams(
    bindparam("timeout", type_=Float()),
    bindparam("names", type_=ARRAY(Text())),
)


def _seconds(value: Decimal | float | None) -> float | None:
    return None if value is None else round(float(value), 3)


async def snapshot(
    conn: AsyncConnection, settings: WorkerHealthSettings, recurring_names: Iterable[str]
) -> Snapshot:
    row = (
        await conn.execute(
            SNAPSHOT_SQL,
            {"timeout": settings.worker_heartbeat_timeout, "names": list(recurring_names)},
        )
    ).one()
    return Snapshot(
        alive=int(row.alive),
        last_heartbeat_s=_seconds(row.last_heartbeat_s),
        oldest_overdue_timer_s=_seconds(row.oldest_overdue_timer_s),
        oldest_overdue_job_s=_seconds(row.oldest_overdue_job_s),
    )


__all__ = ["PRUNE_AFTER", "SNAPSHOT_SQL", "Snapshot", "beat", "forget", "snapshot"]
