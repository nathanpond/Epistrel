"""FastAPI application factory."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import APIRouter, Depends, FastAPI
from starlette.types import ASGIApp, Receive, Scope, Send

from epistrel import __version__
from epistrel.api import errors, openapi, routes
from epistrel.api.auth import (
    PROTECTED_DEPENDENCIES,
    SIGNED_DEPENDENCIES,
    TRUST_BOUNDARY_STATEMENT,
)
from epistrel.api.validation import strict_json_body
from epistrel.config import ConfigError, Settings, load_settings
from epistrel.core.db import database_url, make_engine
from epistrel.core.media.store import MediaRootUnwritable
from epistrel.core.media.stores import store_kind_problem
from epistrel.engine import Engine
from epistrel.observability import metrics
from epistrel.observability.instrument import TelemetryMiddleware
from epistrel.orchestrator.models.config import config_key_problem


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Own the database engine for the life of the server process.

    Refuses to start when the media root cannot be written or the S3 bucket cannot be used
    (#105, #107), when the media are in another store than ``EPISTREL_MEDIA_STORE`` names
    (#107), or when a model API key is stored but ``EPISTREL_CONFIG_KEY`` is unset (skipped under
    the stub adapter); ``epistrel serve`` runs the same checks first and exits 78. ffmpeg and
    ffprobe are probed once here, for ``/health`` (#107).
    """
    try:
        await asyncio.to_thread(app.state.epistrel.prepare_media)
    except MediaRootUnwritable as exc:
        raise ConfigError([str(exc)]) from None
    engine = make_engine(app.state.settings)
    problem = await store_kind_problem(app.state.settings, engine)
    if problem is None:
        problem = await config_key_problem(app.state.settings, engine)
    if problem is not None:
        await engine.dispose()
        raise ConfigError([problem])
    await app.state.epistrel.posters.status()
    app.state.engine = engine
    app.state.epistrel.bind(engine)
    # ``all``/``worker``: the job worker runs here as an asyncio task (#71); ``api``: queued turns
    # left by a stopped process are drained once (#63).
    await app.state.epistrel.start_workers()
    # ``/metrics`` reads the job, timer and worker gauges from this database at scrape (#74).
    metrics.GAUGES.bind(database_url(app.state.settings))
    try:
        yield
    finally:
        metrics.GAUGES.unbind()
        await app.state.epistrel.stop_workers()
        app.state.epistrel.bind(None)
        await app.state.epistrel.models.aclose()
        await engine.dispose()
        app.state.engine = None


RECEIVED_AT = "received_at"
"""``scope["state"]`` key holding the request's arrival time (``time.monotonic()``, #69)."""


class ReceiptStamp:
    """The outermost ASGI middleware: stamps when each HTTP request arrived, before anything
    else runs, so a turn's deadline and ``elapsed_ms`` cover the whole request (#69)."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            scope.setdefault("state", {})[RECEIVED_AT] = time.monotonic()
        await self.app(scope, receive, send)


def include_protected(app: FastAPI, router: APIRouter) -> None:
    """Include ``router`` behind the service credential and the principal rule (invariant 4),
    then the strict body check (FR-API-4): a call is authenticated before its body is judged."""
    app.include_router(router, dependencies=[*PROTECTED_DEPENDENCIES, Depends(strict_json_body)])


def include_signed(app: FastAPI, router: APIRouter) -> None:
    """Include ``router`` (``routes.signed_router``: ``GET /media/{id}/content`` only) behind
    the credential-or-signed-URL guard (invariant 4's one conditional exemption, #107), the
    principal rule and the strict body check."""
    app.include_router(router, dependencies=[*SIGNED_DEPENDENCIES, Depends(strict_json_body)])


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the app. `settings` is loaded from the environment when not given.

    The engine is created by the lifespan, so an app that is never started (plain
    `TestClient(app)` without `with`) has no engine and `/health` reports `skipped`.

    ``settings.mode`` decides the routes: ``all`` and ``api`` serve every route (the media
    content route behind its credential-or-signed-URL guard, #107); ``worker`` serves only the
    public operational routes (``/health``, ``/metrics``), since it takes no API calls (#71).
    Process-wide logging and tracing are ``epistrel serve``'s (``observability.configure_process``),
    not the app's: an embedder keeps its own.
    """
    if settings is None:
        settings = load_settings()
    app = FastAPI(
        title="Epistrel Engine",
        version=__version__,
        description=TRUST_BOUNDARY_STATEMENT,
        lifespan=lifespan,
        # No OAuth flow exists; the redirect page would be a sixth unauthenticated route.
        swagger_ui_oauth2_redirect_url=None,
    )
    app.state.settings = settings
    app.state.engine = None
    app.state.epistrel = Engine.from_settings(settings)
    errors.install(app)
    # Added first, so it runs inside ``ReceiptStamp`` (which stays the outermost): one scope,
    # server span, latency sample and ``request`` line per request (#74).
    app.add_middleware(TelemetryMiddleware)
    app.add_middleware(ReceiptStamp)
    app.include_router(routes.public_router)
    if settings.mode != "worker":
        include_protected(app, routes.router)
        include_signed(app, routes.signed_router)
    openapi.install(app)
    return app
