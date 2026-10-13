"""Liveness and readiness probe: `GET /health` (NFR-REL-5, NFR-OPS-1; #71).

It reports whether the database answers and how the background work is doing: how many worker
loops sent a heartbeat within ``worker_heartbeat_timeout`` and how old the newest heartbeat is,
and the age in seconds of the oldest overdue timer or recurring task and of the oldest overdue
queued job (``null``: nothing overdue). Status:

- ``ok`` (200): the database answers, a worker is alive and nothing is overdue by more than
  ``timer_lag_alert``;
- ``degraded`` (200): the database answers but no worker is alive or something is overdue by
  more than ``timer_lag_alert``. An ``api``-only deployment without workers says so here instead
  of silently rotting; the container stays healthy so compose does not restart-loop it;
- ``unavailable`` (503): the database does not answer within ``CHECK_TIMEOUT_SECONDS``; the
  background fields are ``null``.

``content_logging`` is ``true`` while operator-only content debug logging is on in this process
(``EPISTREL_CONTENT_DEBUG_LOGGING``, NFR-SEC-4; #74), so turning it on is visible.

``checks.ffmpeg`` and ``checks.ffprobe`` say whether the tools that extract a video's poster and
probe a video answered ``-version`` (``ok`` or ``missing``; checked once, at start-up or at the
first probe, #107). Neither changes the status: without ffmpeg only a video uploaded without a
poster is refused (422 ``poster_required``).

Ages use the database's own ``now()``. An app that was never started (no engine) reports ``ok``
with the database ``skipped``.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Literal

from fastapi import APIRouter, Request, Response, status
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.core.config_changes import read_section
from epistrel.core.settings_schema import WorkerHealthSettings
from epistrel.observability.logging import content_logging_active
from epistrel.orchestrator.jobs import health as worker_health
from epistrel.orchestrator.jobs.recurring import registered_tasks

router = APIRouter(tags=["ops"])
log = logging.getLogger(__name__)

CHECK_TIMEOUT_SECONDS = 2.0
WARNING_INTERVAL_SECONDS = 60.0

_last_warning: float | None = None


class WorkersCheck(BaseModel):
    """Worker loops with a heartbeat within `worker_heartbeat_timeout`, and the newest
    heartbeat's age in seconds (`null`: none)."""

    alive: int | None = None
    last_heartbeat_s: float | None = None


class HealthChecks(BaseModel):
    """Each check: the database, the workers, the oldest overdue timer and job (seconds;
    `null`: nothing overdue), and whether ffmpeg and ffprobe (video posters and probes) are
    there (`missing` never changes the status)."""

    database: Literal["ok", "error", "skipped"]
    workers: WorkersCheck = WorkersCheck()
    oldest_overdue_timer_s: float | None = None
    oldest_overdue_job_s: float | None = None
    ffmpeg: Literal["ok", "missing"] = "missing"
    ffprobe: Literal["ok", "missing"] = "missing"


class HealthResponse(BaseModel):
    """`ok`, `degraded` (still 200) or `unavailable` (503), the checks behind it, and whether
    operator-only content debug logging is on in this process (`content_logging`, NFR-SEC-4)."""

    status: Literal["ok", "degraded", "unavailable"]
    checks: HealthChecks
    content_logging: bool = False


def _warn(what: str, exc: BaseException) -> None:
    """Log at most once a minute (a probe runs every few seconds)."""
    global _last_warning
    now = time.monotonic()
    if _last_warning is None or now - _last_warning >= WARNING_INTERVAL_SECONDS:
        _last_warning = now
        log.warning("%s health check failed: %s", what, type(exc).__name__)


async def check(engine: AsyncEngine) -> HealthResponse:
    """The whole probe under one ``CHECK_TIMEOUT_SECONDS`` budget."""
    try:
        async with asyncio.timeout(CHECK_TIMEOUT_SECONDS), engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
            try:
                settings = await read_section(conn, WorkerHealthSettings)
                snap = await worker_health.snapshot(conn, settings, registered_tasks())
            except Exception as exc:
                if isinstance(exc, TimeoutError | asyncio.CancelledError):
                    raise
                _warn("worker", exc)
                return HealthResponse(status="degraded", checks=HealthChecks(database="ok"))
    except Exception as exc:
        _warn("database", exc)
        return HealthResponse(status="unavailable", checks=HealthChecks(database="error"))
    return HealthResponse(
        status="degraded" if snap.degraded(settings) else "ok",
        checks=HealthChecks(
            database="ok",
            workers=WorkersCheck(alive=snap.alive, last_heartbeat_s=snap.last_heartbeat_s),
            oldest_overdue_timer_s=snap.oldest_overdue_timer_s,
            oldest_overdue_job_s=snap.oldest_overdue_job_s,
        ),
    )


@router.get(
    "/health",
    response_model=HealthResponse,
    operation_id="health",
    summary="Liveness and readiness",
    responses={status.HTTP_503_SERVICE_UNAVAILABLE: {"model": HealthResponse}},
)
async def health(request: Request, response: Response) -> HealthResponse:
    engine: AsyncEngine | None = getattr(request.app.state, "engine", None)
    if engine is None:
        result = HealthResponse(status="ok", checks=HealthChecks(database="skipped"))
    else:
        result = await check(engine)
    if result.status == "unavailable":
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    tools = await request.app.state.epistrel.posters.status()
    checks = result.checks.model_copy(update={"ffmpeg": tools.ffmpeg, "ffprobe": tools.ffprobe})
    return result.model_copy(update={"checks": checks, "content_logging": content_logging_active()})
