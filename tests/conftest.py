from __future__ import annotations

import os
import secrets
import time
import urllib.error
import urllib.request
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from hypothesis import settings as hypothesis_settings
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.config import Settings
from epistrel.core.db import make_engine, make_sync_engine, normalize_url
from tests.auth_support import ERASURE_LEDGER_DIR, auth_settings, note_session_database
from tests.perf.census import CENSUS, installed

pytest_plugins = ("pytester",)

# CI runs property tests derandomized: the same examples on every run, so a red build is
# reproducible. Locally Hypothesis explores freely. Tests may set their own example counts.
hypothesis_settings.register_profile("ci", derandomize=True, deadline=None, print_blob=True)
if os.environ.get("CI"):
    hypothesis_settings.load_profile("ci")

REPO_ROOT = Path(__file__).resolve().parent.parent
ALEMBIC_INI = REPO_ROOT / "alembic.ini"
INTEGRATION_DIR = Path(__file__).resolve().parent / "integration"
CHAOS_DIR = Path(__file__).resolve().parent / "chaos"
DETERMINISM_DIR = Path(__file__).resolve().parent / "determinism"
SMOKE_DIR = Path(__file__).resolve().parent / "smoke"
SCENARIO_DIR = Path(__file__).resolve().parent / "scenario"
P05_FILE = "test_p_05.py"
PG_IMAGE = "pgvector/pgvector:pg16"
MINIO_IMAGE = (
    "bitnamilegacy/minio:2025.7.23-debian-12-r5"
    "@sha256:6dabb4a2088c9a79908de3bc05f4586c23ad2182c8908e7e3acbf61c1467fb20"
)
"""The S3-compatible store the S3 adapter's tests run against (#107): MinIO
RELEASE.2025-07-23T15-54-02Z. Resolved 2026-10-12: ``minio/minio`` is no longer published on
Docker Hub, and MinIO's latest release (RELEASE.2025-10-15T17-29-55Z) has no image anywhere, so
the tag is Bitnami's frozen legacy build of the newest release that has one, pinned by digest.
Its ``minio`` binary runs directly (``MINIO_ENTRYPOINT``), without Bitnami's setup scripts, which
start and stop the server once before serving. MinIO is AGPL: a test service only, never a
dependency."""
MINIO_ENTRYPOINT = "/opt/bitnami/minio/bin/minio"
MINIO_ACCESS_KEY = "epistrel-test"
MINIO_SECRET_KEY = "epistrel-test-secret-0123"
MINIO_READY_S = 60.0
OVERRIDE_VAR = "EPISTREL_TEST_DATABASE_URL"
LIVE_MODEL_MARKER = "live_model"
PERF_LONG_MARKER = "perf_long"
SMOKE_MARKER = "smoke"
REDTEAM_LONG_MARKER = "redteam_long"
SOAK_MARKER = "soak"
PERF_WEEKLY_MARKER = "perf_weekly"
OPT_IN_MARKERS = (
    LIVE_MODEL_MARKER,
    PERF_LONG_MARKER,
    PERF_WEEKLY_MARKER,
    SMOKE_MARKER,
    REDTEAM_LONG_MARKER,
    SOAK_MARKER,
)
"""Markers whose tests run only when ``-m`` names them: a real endpoint (#54), minutes of
seeding and measuring (#76), a live compose stack (C-10, #77), a full red-team corpus (R-01's,
#145; nightly), a long-horizon soak (X-01's 20,000 turns, #147; weekly), or the weekly
performance suite on the scale fixture (#153)."""
DUMMY_DSN = "postgresql+psycopg://u:p@localhost:5432/epistrel_test"

NO_DOCKER_MESSAGE = (
    "Integration tests need PostgreSQL. Docker is not available to start "
    f"{PG_IMAGE!r}; either start Docker or point {OVERRIDE_VAR} at a reachable "
    "PostgreSQL 16 + pgvector server whose role can CREATE DATABASE."
)


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Everything under tests/integration/, tests/chaos/ and tests/determinism/ is an
    integration test, everything under tests/chaos/ a ``chaos`` test (#75), and everything
    under tests/smoke/ a ``smoke`` test (#77). ``live_model``, ``perf_long``, ``perf_weekly``,
    ``smoke``, ``redteam_long`` and ``soak`` tests run only when ``-m`` names their marker: they
    need a real endpoint (#54), minutes (#76), the 50k scale fixture (#153), a running compose
    stack (#77), a nightly corpus run (#145) or a weekly soak (#147). P-05
    runs last, over the turn census of everything before it (#69)."""
    for item in items:
        parents = Path(str(item.fspath)).parents
        if INTEGRATION_DIR in parents or CHAOS_DIR in parents or DETERMINISM_DIR in parents:
            item.add_marker(pytest.mark.integration)
        if CHAOS_DIR in parents:
            item.add_marker(pytest.mark.chaos)
        if SMOKE_DIR in parents:
            item.add_marker(pytest.mark.smoke)
    census = [item for item in items if Path(str(item.fspath)).name == P05_FILE]
    if census:
        items[:] = [item for item in items if item not in census] + census
    CENSUS.s_tests_collected = any(SCENARIO_DIR in Path(str(item.fspath)).parents for item in items)
    named = config.getoption("markexpr", "") or ""
    for marker in OPT_IN_MARKERS:
        if marker in named:
            continue
        opt_in = [item for item in items if item.get_closest_marker(marker)]
        if opt_in:
            config.hook.pytest_deselected(items=opt_in)
            items[:] = [item for item in items if not item.get_closest_marker(marker)]


@pytest.fixture(scope="session", autouse=True)
def _turn_census() -> Iterator[None]:
    """Record every Lite turn of the session for P-05 (``tests/perf/census.py``)."""
    with installed():
        yield


@pytest.fixture(autouse=True)
def _clean_epistrel_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Strip every EPISTREL_* variable so tests see only what they set (EPISTREL_TEST_* kept),
    except ``EPISTREL_ERASURE_LEDGER_DIR``: it points at this process's ledger directory, so an
    app built from bare ``Settings`` never writes an erasure ledger under the repository and
    always reads the one that matches the session database (#125)."""
    for name in list(os.environ):
        if name.upper().startswith("EPISTREL_") and not name.upper().startswith("EPISTREL_TEST_"):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("EPISTREL_ERASURE_LEDGER_DIR", str(ERASURE_LEDGER_DIR))


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
        note_session_database(url)
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
        note_session_database(url)
        yield url
    finally:
        container.stop()


@dataclass(frozen=True)
class S3Endpoint:
    """A running S3-compatible store: its URL and its access key pair."""

    url: str
    access_key: str
    secret_key: str


@pytest.fixture(scope="session")
def minio() -> Iterator[S3Endpoint]:
    """MinIO (``MINIO_IMAGE``) in a throwaway container for the session; a test fails, never
    skips, without Docker (#107)."""
    try:
        from testcontainers.core.container import DockerContainer

        container = DockerContainer(
            MINIO_IMAGE,
            command="server /tmp/data --address :9000",
            env={"MINIO_ROOT_USER": MINIO_ACCESS_KEY, "MINIO_ROOT_PASSWORD": MINIO_SECRET_KEY},
            ports=[9000],
            entrypoint=MINIO_ENTRYPOINT,
        )
        container.start()
    except Exception as exc:
        pytest.fail(
            f"The S3 adapter's tests need MinIO ({MINIO_IMAGE!r}), started through Docker.\n"
            f"Cause: {type(exc).__name__}: {exc}",
            pytrace=False,
        )
    try:
        url = f"http://{container.get_container_host_ip()}:{container.get_exposed_port(9000)}"
        deadline = time.monotonic() + MINIO_READY_S
        while True:
            try:
                with urllib.request.urlopen(f"{url}/minio/health/ready", timeout=2) as ready:  # noqa: S310 - the container's own URL
                    if ready.status == 200:
                        break
            except OSError, urllib.error.URLError:
                pass
            if time.monotonic() > deadline:
                pytest.fail(f"MinIO did not become ready within {MINIO_READY_S}s", pytrace=False)
            time.sleep(0.2)
        yield S3Endpoint(url, MINIO_ACCESS_KEY, MINIO_SECRET_KEY)
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


# -- embedding jobs on the shared database (#138) -------------------------------------------------

EMBEDDING_JOB_KINDS = ("embed", "reembed")
"""The jobs whose run pins the deployment's vector space (``core.embeddings.pin_first``): a
retype of ``memories.embedding`` and deployment-scope state. One left queued on the shared
session database could be claimed by a later test's worker and pin it, so no test may leave one
there (scratch databases are their tests' own)."""
_LEFTOVER = text(
    "SELECT count(*) FROM jobs WHERE kind = ANY(:kinds) AND status IN ('queued', 'running')"
)
_DRAIN = text("DELETE FROM jobs WHERE kind = ANY(:kinds) AND status IN ('queued', 'running')")


@pytest.fixture(scope="session")
def _shared_jobs_db(pg_url: str) -> Iterator[Any]:
    sync = make_sync_engine(pg_url)
    try:
        yield sync
    finally:
        sync.dispose()


@pytest.fixture(autouse=True)
def _no_embedding_jobs_left(request: pytest.FixtureRequest) -> Iterator[None]:
    """After every test that uses the shared database, fail it if an ``embed`` or ``reembed``
    job is left queued or running there (M3's rule: deployment-scope state is rolled back). A
    test that writes memories there drains its jobs with ``drained_embedding_jobs``. The
    database is requested before the test runs (only for a test that uses ``pg_url``, so a unit
    test starts no container): pytest refuses fixture requests during teardown."""
    sync = request.getfixturevalue("_shared_jobs_db") if "pg_url" in request.fixturenames else None
    yield
    if sync is None:
        return
    with sync.connect() as conn:
        left = int(conn.execute(_LEFTOVER, {"kinds": list(EMBEDDING_JOB_KINDS)}).scalar() or 0)
    if left:
        pytest.fail(
            f"{request.node.nodeid} left {left} embed/reembed job(s) queued on the shared "
            "database; use the drained_embedding_jobs fixture",
            pytrace=False,
        )


@pytest.fixture
def drained_embedding_jobs(_shared_jobs_db: Any) -> Iterator[None]:
    """Delete the ``embed``/``reembed`` jobs a test's memory writes enqueued on the shared
    database (its teardown runs before ``_no_embedding_jobs_left`` checks)."""
    yield
    with _shared_jobs_db.begin() as conn:
        conn.execute(_DRAIN, {"kinds": list(EMBEDDING_JOB_KINDS)})
