from __future__ import annotations

import os
import secrets
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from hypothesis import settings as hypothesis_settings
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.config import Settings
from epistrel.core.db import make_engine, make_sync_engine, normalize_url
from tests.auth_support import auth_settings

pytest_plugins = ("pytester",)

# CI runs property tests derandomized: the same examples on every run, so a red build is
# reproducible. Locally Hypothesis explores freely. Tests may set their own example counts.
hypothesis_settings.register_profile("ci", derandomize=True, deadline=None, print_blob=True)
if os.environ.get("CI"):
    hypothesis_settings.load_profile("ci")

REPO_ROOT = Path(__file__).resolve().parent.parent
ALEMBIC_INI = REPO_ROOT / "alembic.ini"
INTEGRATION_DIR = Path(__file__).resolve().parent / "integration"
PG_IMAGE = "pgvector/pgvector:pg16"
OVERRIDE_VAR = "EPISTREL_TEST_DATABASE_URL"
LIVE_MODEL_MARKER = "live_model"
DUMMY_DSN = "postgresql+psycopg://u:p@localhost:5432/epistrel_test"

NO_DOCKER_MESSAGE = (
    "Integration tests need PostgreSQL. Docker is not available to start "
    f"{PG_IMAGE!r}; either start Docker or point {OVERRIDE_VAR} at a reachable "
    "PostgreSQL 16 + pgvector server whose role can CREATE DATABASE."
)


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Everything under tests/integration/ is an integration test. ``live_model`` tests run
    only when ``-m`` names that marker: they need a real endpoint (#54)."""
    for item in items:
        if INTEGRATION_DIR in Path(str(item.fspath)).parents:
            item.add_marker(pytest.mark.integration)
    if LIVE_MODEL_MARKER in (config.getoption("markexpr", "") or ""):
        return
    live = [item for item in items if item.get_closest_marker(LIVE_MODEL_MARKER)]
    if live:
        config.hook.pytest_deselected(items=live)
        items[:] = [item for item in items if not item.get_closest_marker(LIVE_MODEL_MARKER)]


@pytest.fixture(autouse=True)
def _clean_epistrel_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Strip every EPISTREL_* variable so tests see only what they set (EPISTREL_TEST_* kept)."""
    for name in list(os.environ):
        if name.upper().startswith("EPISTREL_") and not name.upper().startswith("EPISTREL_TEST_"):
            monkeypatch.delenv(name, raising=False)


@pytest.fixture
def dummy_dsn() -> str:
    return DUMMY_DSN


def alembic_config(url: str) -> Config:
    """Alembic Config pointed at the repo's alembic.ini with the URL supplied programmatically."""
    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    return cfg


@pytest.fixture(scope="session")
def pg_url() -> Iterator[str]:
    """A PostgreSQL 16 + pgvector database migrated to head.

    Uses EPISTREL_TEST_DATABASE_URL when set; otherwise starts a throwaway container.
    Shared for the whole session: tests that mutate schema use a scratch database (see
    ``scratch_database``), never this one.
    """
    override = os.environ.get(OVERRIDE_VAR)
    if override:
        url = normalize_url(override)
        command.upgrade(alembic_config(url), "head")
        yield url
        return

    try:
        from testcontainers.community.postgres import PostgresContainer

        container = PostgresContainer(PG_IMAGE, driver="psycopg")
        container.start()
    except Exception as exc:
        pytest.fail(f"{NO_DOCKER_MESSAGE}\nCause: {type(exc).__name__}: {exc}", pytrace=False)
    try:
        url = container.get_connection_url()
        command.upgrade(alembic_config(url), "head")
        yield url
    finally:
        container.stop()


@pytest.fixture
def scratch_database(pg_url: str) -> Iterator[str]:
    """A fresh, empty database on the same server, dropped afterwards."""
    name = f"epistrel_mig_{secrets.token_hex(4)}"
    admin = make_sync_engine(pg_url, isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as conn:
            try:
                conn.execute(text(f'CREATE DATABASE "{name}"'))
            except Exception as exc:
                pytest.fail(
                    f"Could not CREATE DATABASE on {OVERRIDE_VAR or 'the test server'}; the role "
                    f"needs CREATEDB for migration up/down tests.\nCause: {exc}",
                    pytrace=False,
                )
        yield pg_url.rsplit("/", 1)[0] + f"/{name}"
        with admin.connect() as conn:
            conn.execute(
                text(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = :name AND pid <> pg_backend_pid()"
                ),
                {"name": name},
            )
            conn.execute(text(f'DROP DATABASE IF EXISTS "{name}"'))
    finally:
        admin.dispose()


@pytest.fixture
def settings(pg_url: str) -> Settings:
    return auth_settings(pg_url)


@pytest.fixture
async def engine(settings: Settings) -> AsyncIterator[AsyncEngine]:
    engine = make_engine(settings)
    try:
        yield engine
    finally:
        await engine.dispose()
