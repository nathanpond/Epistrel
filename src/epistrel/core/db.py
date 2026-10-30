"""Database engine construction."""

from __future__ import annotations

from sqlalchemy import Engine, create_engine
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from epistrel.config import DATABASE_URL_SCHEME, Settings

CONNECT_TIMEOUT_SECONDS = 2
"""libpq connect timeout; keeps `/health` and start-up failures fast when the database is away."""


def database_url(settings: Settings) -> str:
    """Return the configured DSN as a plain string (the only place the secret is unwrapped)."""
    return settings.database_url.get_secret_value()


def normalize_url(url: str) -> str:
    """Rewrite a bare ``postgresql://`` DSN to the ``postgresql+psycopg://`` form we use."""
    if url.startswith("postgresql://"):
        return DATABASE_URL_SCHEME + url.removeprefix("postgresql://")
    return url


def make_engine(
    settings: Settings,
    *,
    pool_size: int | None = None,
    connect_timeout: int = CONNECT_TIMEOUT_SECONDS,
) -> AsyncEngine:
    """Create a new async engine on psycopg 3. Each call is a fresh engine; callers dispose it.
    ``pool_size`` keeps that many connections open (SQLAlchemy's default otherwise), for a bulk
    writer whose concurrency is known: an overflow connection is closed when it is returned, so
    a writer busier than the pool opens a backend per transaction (P-04's seeders, #587)."""
    pool: dict[str, int] = {} if pool_size is None else {"pool_size": pool_size}
    return create_async_engine(
        database_url(settings),
        pool_pre_ping=True,
        connect_args={"connect_timeout": connect_timeout},
        **pool,
    )


def make_sync_engine(url: str, **kwargs: object) -> Engine:
    """Create a blocking engine on the same psycopg 3 dialect (Alembic, test fixtures)."""
    return create_engine(normalize_url(url), pool_pre_ping=True, **kwargs)
