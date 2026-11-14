"""Database engine construction."""

from __future__ import annotations

from sqlalchemy import Engine, create_engine, make_url
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from epistrel.config import DATABASE_URL_SCHEME, Settings

CONNECT_TIMEOUT_SECONDS = 2
"""libpq connect timeout; keeps `/health` and start-up failures fast when the database is away."""
SESSION_OPTIONS = "-c plan_cache_mode=force_custom_plan"
"""Every connection plans each statement with its parameters (#784, ADR-188). psycopg prepares
a statement it has run five times, and Postgres may then plan it generically, as it may the
cached plan of a foreign key's check: a generic plan of a statement on partitioned ``memories``
cannot prune to the story's partition when planned, and locks every partition and its indexes
when run (16 locks a story), which exhausts the shared lock table once a deployment holds
about a thousand stories."""


def session_options(url: str) -> str:
    """``SESSION_OPTIONS`` after any ``options`` the DSN already carries (libpq's
    ``options``: ``connect_args`` would replace them, not add to them)."""
    given = make_url(url).query.get("options")
    if isinstance(given, tuple):
        given = " ".join(given)
    return f"{given} {SESSION_OPTIONS}" if given else SESSION_OPTIONS


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
    url = database_url(settings)
    return create_async_engine(
        url,
        pool_pre_ping=True,
        connect_args={"connect_timeout": connect_timeout, "options": session_options(url)},
        **pool,
    )


def make_sync_engine(url: str, **kwargs: object) -> Engine:
    """Create a blocking engine on the same psycopg 3 dialect (Alembic, test fixtures)."""
    return create_engine(normalize_url(url), pool_pre_ping=True, **kwargs)
