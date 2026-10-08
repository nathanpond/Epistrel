"""Liveness and readiness probe: `GET /health` reports whether the database answers."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Literal

from fastapi import APIRouter, Request, Response, status
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

router = APIRouter(tags=["ops"])
log = logging.getLogger(__name__)

DB_CHECK_TIMEOUT_SECONDS = 2.0
WARNING_INTERVAL_SECONDS = 60.0

_last_warning: float | None = None


class HealthResponse(BaseModel):
    status: Literal["ok", "unavailable"]
    checks: dict[str, str]


async def check_database(engine: AsyncEngine) -> bool:
    """Run `SELECT 1` on the shared pool with a hard timeout. Logs at most once a minute."""
    global _last_warning
    try:
        async with asyncio.timeout(DB_CHECK_TIMEOUT_SECONDS), engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception as exc:
        now = time.monotonic()
        if _last_warning is None or now - _last_warning >= WARNING_INTERVAL_SECONDS:
            _last_warning = now
            log.warning("database health check failed: %s", type(exc).__name__)
        return False
    return True


@router.get(
    "/health",
    response_model=HealthResponse,
    responses={status.HTTP_503_SERVICE_UNAVAILABLE: {"model": HealthResponse}},
)
async def health(request: Request, response: Response) -> HealthResponse:
    engine: AsyncEngine | None = getattr(request.app.state, "engine", None)
    if engine is None:
        return HealthResponse(status="ok", checks={"database": "skipped"})
    if await check_database(engine):
        return HealthResponse(status="ok", checks={"database": "ok"})
    response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return HealthResponse(status="unavailable", checks={"database": "error"})
