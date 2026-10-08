from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.config import Settings
from epistrel.core.db import CONNECT_TIMEOUT_SECONDS, make_engine, make_sync_engine, normalize_url


def test_normalize_url_adds_the_psycopg_driver() -> None:
    assert normalize_url("postgresql://u:p@h/db") == "postgresql+psycopg://u:p@h/db"


def test_normalize_url_leaves_a_driver_url_alone() -> None:
    assert normalize_url("postgresql+psycopg://u:p@h/db") == "postgresql+psycopg://u:p@h/db"


async def test_make_engine_builds_an_async_psycopg_engine(dummy_dsn: str) -> None:
    engine = make_engine(Settings(database_url=dummy_dsn))
    try:
        assert isinstance(engine, AsyncEngine)
        assert engine.url.drivername == "postgresql+psycopg"
        assert engine.url.database == "epistrel_test"
        assert engine.pool._pre_ping is True
    finally:
        await engine.dispose()


def test_make_sync_engine_uses_the_same_dialect(dummy_dsn: str) -> None:
    engine = make_sync_engine(dummy_dsn)
    try:
        assert engine.url.drivername == "postgresql+psycopg"
    finally:
        engine.dispose()


def test_connect_timeout_is_short() -> None:
    assert CONNECT_TIMEOUT_SECONDS <= 2
