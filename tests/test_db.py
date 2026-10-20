from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import psycopg
from sqlalchemy.exc import InterfaceError, OperationalError
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.api.errors import connection_lost, database_error_handler
from epistrel.core.db import CONNECT_TIMEOUT_SECONDS, make_engine, make_sync_engine, normalize_url
from tests.auth_support import auth_settings


def test_normalize_url_adds_the_psycopg_driver() -> None:
    assert normalize_url("postgresql://u:p@h/db") == "postgresql+psycopg://u:p@h/db"


def test_normalize_url_leaves_a_driver_url_alone() -> None:
    assert normalize_url("postgresql+psycopg://u:p@h/db") == "postgresql+psycopg://u:p@h/db"


async def test_make_engine_builds_an_async_psycopg_engine(dummy_dsn: str) -> None:
    engine = make_engine(auth_settings(dummy_dsn))
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


class _Pool:
    def __init__(self) -> None:
        self.disposed = 0

    async def dispose(self) -> None:
        self.disposed += 1


def _request(pool: _Pool) -> Any:
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(engine=pool)))


async def test_a_lost_connection_disposes_the_pool_and_a_lock_timeout_does_not() -> None:
    """NFR-REL-3 (#75, #452): a lost connection (no SQLSTATE, class 08, a server shutdown, an
    interface error) is 503 ``database_unavailable`` and the pool is disposed. A lock or
    statement timeout the server sent on a live connection is the same 503, but the pool is
    kept: disposing it under load drops every healthy connection and makes each request
    connect again."""
    lost = [
        OperationalError("SELECT 1", {}, psycopg.OperationalError("connection timeout expired")),
        OperationalError("SELECT 1", {}, psycopg.errors.AdminShutdown("terminating")),
        OperationalError("SELECT 1", {}, psycopg.errors.ConnectionFailure("gone")),
        InterfaceError("SELECT 1", {}, psycopg.InterfaceError("closed")),
    ]
    alive = [
        OperationalError("SELECT 1", {}, psycopg.errors.LockNotAvailable("lock timeout")),
        OperationalError("SELECT 1", {}, psycopg.errors.QueryCanceled("statement timeout")),
    ]
    for exc, disposed in [(e, 1) for e in lost] + [(e, 0) for e in alive]:
        pool = _Pool()
        answer = await database_error_handler(_request(pool), exc)
        assert answer.status_code == 503
        assert json.loads(bytes(answer.body))["error"]["code"] == "database_unavailable"
        assert pool.disposed == disposed, exc
        assert connection_lost(exc) is bool(disposed)
