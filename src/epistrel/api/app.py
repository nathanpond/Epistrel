"""FastAPI application factory."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from epistrel import __version__
from epistrel.api import routes
from epistrel.config import Settings, load_settings
from epistrel.core.db import make_engine


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Own the database engine for the life of the server process."""
    engine = make_engine(app.state.settings)
    app.state.engine = engine
    try:
        yield
    finally:
        await engine.dispose()
        app.state.engine = None


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the app. `settings` is loaded from the environment when not given.

    The engine is created by the lifespan, so an app that is never started (plain
    `TestClient(app)` without `with`) has no engine and `/health` reports `skipped`.
    """
    if settings is None:
        settings = load_settings()
    app = FastAPI(title="Epistrel Engine", version=__version__, lifespan=lifespan)
    app.state.settings = settings
    app.state.engine = None
    app.include_router(routes.router)
    return app
