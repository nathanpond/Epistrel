"""Alembic environment: async SQLAlchemy on psycopg 3, URL from Settings or ``-x url=``."""

from __future__ import annotations

import asyncio

from alembic import context
from sqlalchemy import Connection, pool
from sqlalchemy.ext.asyncio import create_async_engine

from epistrel.core.db import make_sync_engine, normalize_url
from epistrel.core.schema import AUTOGENERATE_OPTS, metadata

config = context.config
target_metadata = metadata


def resolve_url() -> str:
    """``-x url=<dsn>`` wins; then a programmatic ``sqlalchemy.url``; otherwise the environment."""
    x_args = context.get_x_argument(as_dictionary=True)
    if url := x_args.get("url"):
        return normalize_url(url)
    if url := config.get_main_option("sqlalchemy.url"):
        return normalize_url(url)
    from epistrel.config import load_settings
    from epistrel.core.db import database_url

    return database_url(load_settings())


def run_migrations_offline() -> None:
    context.configure(
        url=resolve_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        **AUTOGENERATE_OPTS,
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata, **AUTOGENERATE_OPTS)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations(url: str) -> None:
    engine = create_async_engine(url, poolclass=pool.NullPool)
    try:
        async with engine.connect() as connection:
            await connection.run_sync(do_run_migrations)
    finally:
        await engine.dispose()


def run_sync_migrations(url: str) -> None:
    """Used when an event loop is already running in this thread (e.g. from an async test)."""
    engine = make_sync_engine(url, poolclass=pool.NullPool)
    try:
        with engine.connect() as connection:
            do_run_migrations(connection)
    finally:
        engine.dispose()


def run_migrations_online() -> None:
    url = resolve_url()
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        asyncio.run(run_async_migrations(url))
    else:
        run_sync_migrations(url)


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
