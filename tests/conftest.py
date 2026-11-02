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
from epistrel.core import ids
from epistrel.core.db import make_engine, make_sync_engine, normalize_url
from epistrel.core.enums import UNFINISHED_TURN_STAGES
from tests import takeback_support
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
PERF_CAPACITY_MARKER = "perf_capacity"
OPT_IN_MARKERS = (
    LIVE_MODEL_MARKER,
    PERF_LONG_MARKER,
    PERF_WEEKLY_MARKER,
    PERF_CAPACITY_MARKER,
    SMOKE_MARKER,
    REDTEAM_LONG_MARKER,
    SOAK_MARKER,
)
"""Markers whose tests run only when ``-m`` names them: a real endpoint (#54), minutes of
seeding and measuring (#76), a live compose stack (C-10, #77), a full red-team corpus (R-01's,
#145; nightly), a long-horizon soak (X-01's 20,000 turns, #147; weekly), the weekly
performance suite on the scale fixture (#153), or P-10's capacity exploration (by hand, #498)."""
DUMMY_DSN = "postgresql+psycopg://u:p@localhost:5432/epistrel_test"

NO_DOCKER_MESSAGE = (
    "Integration tests need PostgreSQL. Docker is not available to start "
    f"{PG_IMAGE!r}; either start Docker or point {OVERRIDE_VAR} at a reachable "
    "PostgreSQL 16 + pgvector server whose role can CREATE DATABASE."
)


def grouped_args(args: list[str], invocation_dir: Path) -> list[str]:
    """``args`` reordered so that the paths under one directory are contiguous (#677): each
    directory, and each path, keeps the order it first appears in, so already grouped
    arguments come back unchanged. Interleaved arguments (a conformance file, a root file,
    another conformance file) make pytest 9 collect the re-entered directory as a second
    ``Package`` node, and it matches a conftest's fixtures to the node they were parsed for, so
    the second node's tests lose them (``fixture 'surface' not found``)."""
    first: dict[Path, int] = {}
    keys: list[tuple[int, ...]] = []
    for arg in args:
        path = (invocation_dir / arg.split("::", 1)[0]).resolve()
        keys.append(tuple(first.setdefault(p, len(first)) for p in [*reversed(path.parents), path]))
    return [args[i] for i in sorted(range(len(args)), key=lambda i: (keys[i], i))]


@pytest.hookimpl(tryfirst=True)
def pytest_collection(session: pytest.Session) -> None:
    """Group the command-line paths by directory before collection (``grouped_args``)."""
    config = session.config
    if not config.option.pyargs:
        config.args[:] = grouped_args(config.args, config.invocation_params.dir)


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Everything under tests/integration/, tests/chaos/ and tests/determinism/ is an
    integration test, everything under tests/chaos/ a ``chaos`` test (#75), and everything
    under tests/smoke/ a ``smoke`` test (#77). ``live_model``, ``perf_long``, ``perf_weekly``,
    ``perf_capacity``, ``smoke``, ``redteam_long`` and ``soak`` tests run only when ``-m`` names
    their marker: they need a real endpoint (#54), minutes (#76), the 50k scale fixture (#153),
    hours of growth to millions of memories (P-10, #498), a running compose stack (#77), a
    nightly corpus run (#145) or a weekly soak (#147). P-05
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
    named = config.getoption("markexpr", "") or ""
    for marker in OPT_IN_MARKERS:
        if marker in named:
            continue
        opt_in = [item for item in items if item.get_closest_marker(marker)]
        if opt_in:
            config.hook.pytest_deselected(items=opt_in)
            items[:] = [item for item in items if not item.get_closest_marker(marker)]


def pytest_runtest_logreport(report: pytest.TestReport) -> None:
    """Record each test whose call phase passed, for P-05's census (#653)."""
    if report.when == "call" and report.passed:
        CENSUS.passed.add(report.nodeid)


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
def frozen_ids(monkeypatch: pytest.MonkeyPatch) -> int:
    """Every ``new_ulid``/``random_ulid`` from here on carries this millisecond (#477), so
    ids that would have sorted at random sort only by ``new_ulid``'s monotonic sequence. The
    sequence starts afresh and is put back afterwards."""
    millis = ids._millis()
    monkeypatch.setattr(ids, "_millis", lambda: millis)
    monkeypatch.setattr(ids, "_last", 0)
    return millis


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


@pytest.fixture
def withheld_module(monkeypatch: pytest.MonkeyPatch) -> Any:
    """One module read as undelivered for the test (``profile_support.WITHHELD``): every module
    is delivered since #203, so a test of the refusal of an undelivered module withholds one."""
    from tests import profile_support

    return profile_support.withhold(monkeypatch)


_STATES_OFF = text(
    "UPDATE story_profile_overrides SET settings = settings - 'states', revision = revision + 1 "
    "WHERE settings -> 'states' IS NOT NULL"
)


@pytest.fixture
async def states_delivered(
    monkeypatch: pytest.MonkeyPatch, engine: AsyncEngine
) -> AsyncIterator[None]:
    """The ``states`` module delivered for the test (#185; #203 delivers it for real), and no
    story left on the shared database with a ``states`` override afterwards: once the patch
    is gone, such a story's configuration names an undelivered level, which a later
    deployment-wide validation (``PATCH /admin/settings``) refuses."""
    from tests import state_support

    state_support.deliver(monkeypatch)
    try:
        yield
    finally:
        async with engine.begin() as conn:
            await conn.execute(_STATES_OFF)


@pytest.fixture
async def clock_delivered(
    monkeypatch: pytest.MonkeyPatch, engine: AsyncEngine
) -> AsyncIterator[None]:
    """The ``clock`` module delivered for the test (#193; #203 delivers it for real), and no
    story left on the shared database with a ``clock`` override afterwards (as
    ``states_delivered``)."""
    from tests import clock_support

    clock_support.deliver(monkeypatch)
    try:
        yield
    finally:
        async with engine.begin() as conn:
            await conn.execute(clock_support.CLOCK_OFF)


@pytest.fixture
async def stops_delivered(
    monkeypatch: pytest.MonkeyPatch, engine: AsyncEngine
) -> AsyncIterator[None]:
    """The ``clock`` module and its ``scheduled_stops`` flag delivered for the test (#196; #203
    delivers them for real), and no story left with a ``clock`` override afterwards (as
    ``clock_delivered``)."""
    from tests import clock_support

    clock_support.deliver_stops(monkeypatch)
    try:
        yield
    finally:
        async with engine.begin() as conn:
            await conn.execute(clock_support.CLOCK_OFF)


@pytest.fixture
async def time_claims_delivered(
    monkeypatch: pytest.MonkeyPatch, engine: AsyncEngine
) -> AsyncIterator[None]:
    """The ``clock`` module with its ``scheduled_stops`` and ``time_claims`` flags delivered for
    the test (#200; #203 delivers them for real), and no story left with a ``clock`` override
    afterwards (as ``clock_delivered``)."""
    from tests import clock_support

    clock_support.deliver_time_claims(monkeypatch)
    try:
        yield
    finally:
        async with engine.begin() as conn:
            await conn.execute(clock_support.CLOCK_OFF)


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
_PINNED = text(
    "SELECT (SELECT embedding_dimension FROM deployment_state LIMIT 1) AS dimension, "
    "(SELECT format_type(atttypid, atttypmod) FROM pg_attribute "
    "WHERE attrelid = 'memories'::regclass AND attname = 'embedding') AS column_type"
)
"""The shared database's vector space: unpinned is no dimension and a ``vector`` column with
none (#473: a pin left there retypes ``memories.embedding`` for every later test)."""
_UNPIN = (
    "UPDATE deployment_state SET embedding_model_version = NULL, embedding_dimension = NULL, "
    "embedding_pinned_at = NULL, reembed_state = '{}'::jsonb",
    "DROP INDEX IF EXISTS ix_memories_embedding_hnsw",
    "UPDATE memories SET embedding = NULL, embedding_model_version = NULL",
    "ALTER TABLE memories ALTER COLUMN embedding TYPE vector",
    "UPDATE stories SET embedding_model_version = NULL",
)
"""Put a pinned shared database back to unpinned (``tests.embedding_support.RESET``'s vector
space half), so only the test that pinned it fails, not every test after it."""
_STALLED = text(
    "SELECT operation_id FROM operations WHERE status = 'committed' "
    "AND turn_stage::text = ANY(:stages) ORDER BY operation_id"
)
"""Staged turns left unfinished on the shared database (``UNFINISHED_TURN_STAGES``, #212).
Every worker's recovery pass, a later test's or a child engine's (``EngineProcess``), resumes
them (#213, ``staged.resume_stalled``): a resume that is not that test's delays its worker's
start (``Worker.listening``) and commits a turn under its harness (X-04's paused commit)."""
_ABANDON = (
    "UPDATE stories SET in_flight_turn = NULL, in_flight_lease_until = NULL "
    "WHERE in_flight_turn = ANY(:ops)",
    "UPDATE operations SET turn_stage = 'abandoned' WHERE operation_id = ANY(:ops)",
)
"""Take the unfinished staged turns a test left out of every later recovery pass's reach."""


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
    test that writes memories there drains its jobs with ``drained_embedding_jobs``. It fails
    too if the test left the shared vector space pinned (``_PINNED``; #473), after unpinning
    it (``_UNPIN``) so the tests after it run unpinned; and if it left a staged turn unfinished
    there (``_STALLED``), after abandoning it (``_ABANDON``) so no later worker resumes it. The
    database is requested before the test runs (only for a test that uses ``pg_url``, so a unit
    test starts no container): pytest refuses fixture requests during teardown."""
    sync = request.getfixturevalue("_shared_jobs_db") if "pg_url" in request.fixturenames else None
    yield
    if sync is None:
        return
    with sync.connect() as conn:
        left = int(conn.execute(_LEFTOVER, {"kinds": list(EMBEDDING_JOB_KINDS)}).scalar() or 0)
        pinned = conn.execute(_PINNED).one()
        stalled = [
            str(r.operation_id)
            for r in conn.execute(_STALLED, {"stages": [s.value for s in UNFINISHED_TURN_STAGES]})
        ]
    if stalled:
        with sync.begin() as conn:
            for statement in _ABANDON:
                conn.execute(text(statement), {"ops": stalled})
    if left:
        pytest.fail(
            f"{request.node.nodeid} left {left} embed/reembed job(s) queued on the shared "
            "database; use the drained_embedding_jobs fixture",
            pytrace=False,
        )
    if pinned.dimension is not None or pinned.column_type != "vector":
        with sync.begin() as conn:
            for statement in _UNPIN:
                conn.execute(text(statement))
        pytest.fail(
            f"{request.node.nodeid} pinned the shared database's vector space (dimension "
            f"{pinned.dimension}, memories.embedding {pinned.column_type}); a test that pins "
            "runs on a database of its own (tests.conformance.conftest.migrated_database)",
            pytrace=False,
        )
    if stalled:
        pytest.fail(
            f"{request.node.nodeid} left {len(stalled)} staged turn(s) unfinished on the "
            f"shared database ({', '.join(stalled)}), which a later worker's recovery pass "
            "would resume (#213): finish them (staged.resume) or use a database of its own",
            pytrace=False,
        )


@pytest.fixture(autouse=True)
def _m5_leftovers_taken_back(request: pytest.FixtureRequest) -> Iterator[None]:
    """After every test, take back the story overrides and profiles the M5 helpers recorded
    (``tests.takeback_support``, #517) on the shared database, so a later ``profile_defaults``
    write does not validate them. The database is requested before the test runs, as for
    ``_no_embedding_jobs_left``; a test without it only has its record cleared."""
    sync = request.getfixturevalue("_shared_jobs_db") if "pg_url" in request.fixturenames else None
    takeback_support.clear()
    yield
    if sync is None:
        takeback_support.clear()
        return
    with sync.begin() as conn:
        takeback_support.take_back(conn)


@pytest.fixture
def drained_embedding_jobs(_shared_jobs_db: Any) -> Iterator[None]:
    """Delete the ``embed``/``reembed`` jobs a test's memory writes enqueued on the shared
    database (its teardown runs before ``_no_embedding_jobs_left`` checks)."""
    yield
    with _shared_jobs_db.begin() as conn:
        conn.execute(_DRAIN, {"kinds": list(EMBEDDING_JOB_KINDS)})
