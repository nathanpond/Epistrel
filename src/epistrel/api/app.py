"""FastAPI application factory."""

from __future__ import annotations

from fastapi import FastAPI

from epistrel import __version__
from epistrel.api import routes
from epistrel.config import Settings, load_settings


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the app. `settings` is loaded from the environment when not given."""
    if settings is None:
        settings = load_settings()
    app = FastAPI(title="Epistrel Engine", version=__version__)
    app.state.settings = settings
    app.include_router(routes.router)
    return app
