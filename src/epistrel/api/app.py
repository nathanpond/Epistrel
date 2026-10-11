"""FastAPI application factory."""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import APIRouter, FastAPI
from starlette.types import ASGIApp, Receive, Scope, Send

from epistrel import __version__
from epistrel.api import errors, routes
from epistrel.api.auth import PROTECTED_DEPENDENCIES, TRUST_BOUNDARY_STATEMENT
from epistrel.config import ConfigError, Settings, load_settings
from epistrel.core.db import make_engine
from epistrel.engine import Engine
from epistrel.orchestrator.models.config import config_key_problem


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Own the database engine for the life of the server process.

    Refuses to start when a model API key is stored but ``EPISTREL_CONFIG_KEY`` is unset
    (skipped under the stub adapter); ``epistrel serve`` runs the same check first and exits 78.
    """
    engine = make_engine(app.state.settings)
    problem = await config_key_problem(app.state.settings, engine)
    if problem is not None:
        await engine.dispose()
        raise ConfigError([problem])
    app.state.engine = engine
    app.state.epistrel.bind(engine)
    # Queued turns left by a stopped process are drained here (#63; #70's worker loop later).
    await app.state.epistrel.start_workers()
    try:
        yield
    finally:
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
    """Include ``router`` behind the service credential and the principal rule (invariant 4)."""
    app.include_router(router, dependencies=PROTECTED_DEPENDENCIES)


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the app. `settings` is loaded from the environment when not given.

    The engine is created by the lifespan, so an app that is never started (plain
    `TestClient(app)` without `with`) has no engine and `/health` reports `skipped`.
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
    app.add_middleware(ReceiptStamp)
    app.include_router(routes.public_router)
    include_protected(app, routes.router)
    return app
