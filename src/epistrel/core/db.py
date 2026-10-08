"""Database engine construction."""

from __future__ import annotations

from sqlalchemy import Engine, create_engine
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from epistrel.config import DATABASE_URL_SCHEME, Settings


def database_url(settings: Settings) -> str:
    """Return the configured DSN as a plain string (the only place the secret is unwrapped)."""
    return settings.database_url.get_secret_value()


def normalize_url(url: str) -> str:
    """Rewrite a bare ``postgresql://`` DSN to the ``postgresql+psycopg://`` form we use."""
    if url.startswith("postgresql://"):
        return DATABASE_URL_SCHEME + url.removeprefix("postgresql://")
    return url


def make_engine(settings: Settings) -> AsyncEngine:
    """Create a new async engine on psycopg 3. Each call is a fresh engine; callers dispose it."""
    return create_async_engine(database_url(settings), pool_pre_ping=True)


def make_sync_engine(url: str, **kwargs: object) -> Engine:
    """Create a blocking engine on the same psycopg 3 dialect (Alembic, test fixtures)."""
    return create_engine(normalize_url(url), pool_pre_ping=True, **kwargs)
