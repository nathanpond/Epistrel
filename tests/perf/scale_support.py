"""The scale fixture's databases and the measurement plumbing of P-01, P-02 and P-06 (#153).

``scale_database`` makes a database of its own on the session's server (the fixture pins an
embedding dimension, a schema change the shared database must never see). It restores the
fixture from the dump cache when ``EPISTREL_TEST_PERF_CACHE_DIR`` names one that holds this
spec's key (``epistrel.testing.fixtures.scale.cache_key``). Otherwise it migrates the database
and builds the fixture, then dumps it there for the next run when a cache directory is named.
It yields the fixture with the library Engine over that database, on the stub adapters with the
fixture's embedding dimension. The database is dropped afterwards.

``measure_retrieval`` runs queries through ``Engine.retrieve``. Its span is the pipeline's own
``timings``, with the query embedding taken out. ``summarise`` gives nearest-rank percentiles
(``epistrel.testing.perf.percentile``, #54's method). ``record`` writes one result file under
``build/perf/``. ``epistrel.tools.perf_docs`` turns those files into ``docs/perf.md`` and
``docs/capacity.md``, and every file carries the host it ran on (``host_info``).

Timing is asserted only by the ``perf_weekly`` tests (``gate``): a timing bound in a test
that PR CI runs fails under host load (#430, #432), so the ``perf_smoke`` tests check the
plumbing only.
"""

from __future__ import annotations

import json
import os
import platform
import secrets
import time
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from alembic import command
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.config import Settings
from epistrel.core.context import RequestContext
from epistrel.core.db import make_engine, make_sync_engine
from epistrel.engine import Engine
from epistrel.orchestrator.jobs.handlers import reembed as reembed_job
from epistrel.testing import perf
from epistrel.testing.fixtures import scale
from tests.auth_support import auth_settings, forget_ledger
from tests.conftest import alembic_config
from tests.model_support import config_key

CACHE_VAR = "EPISTREL_TEST_PERF_CACHE_DIR"
HOST_VAR = "EPISTREL_TEST_PERF_HOST"
"""The host label every result carries (``perf.yml`` sets ``github-runner``)."""
RESULT_DIR = Path("build/perf")
VIEWPOINT = "omniscient"


@dataclass
class ScaleRun:
    """A scale fixture's database: its URL and settings, the Engine over it, the fixture, and
    whether it came from the cache, with the seconds that took."""

    url: str
    settings: Settings
    db: AsyncEngine
    lib: Engine
    fixture: scale.ScaleFixture
    restored: bool
    prepare_s: float
    ctx: RequestContext = field(default_factory=lambda: RequestContext(principal=scale.PRINCIPAL))


def cache_dir() -> Path | None:
    named = os.environ.get(CACHE_VAR)
    return Path(named) if named else None


@asynccontextmanager
async def scale_database(
    pg_url: str, spec: scale.ScaleSpec, *, log: Any = None
) -> AsyncIterator[ScaleRun]:
    """The fixture for ``spec`` in a fresh database (see the module docstring)."""
    name = f"epistrel_scale_{secrets.token_hex(4)}"
    admin = make_sync_engine(pg_url, isolation_level="AUTOCOMMIT")
    drop = text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
    url = pg_url.rsplit("/", 1)[0] + f"/{name}"
    before = reembed_job.bound()
    db: AsyncEngine | None = None
    lib: Engine | None = None
    try:
        with admin.connect() as conn:
            conn.execute(drop)
            conn.execute(text(f'CREATE DATABASE "{name}"'))
        forget_ledger(url)
        started = time.perf_counter()
        directory = cache_dir()
        cached = scale.read_cached(directory, spec)
        settings = auth_settings(
            url,
            model_adapter="stub",
            stub_embedding_dimension=spec.dimension,
            config_key=config_key(),
        )
        if cached is not None and directory is not None:
            scale.restore(url, scale.cached_paths(directory, spec)[0])
            fixture = cached
        else:
            command.upgrade(alembic_config(url), "head")
            builder = make_engine(settings)
            try:
                fixture = await scale.build_scale_fixture(builder, spec, log=log)
            finally:
                await builder.dispose()
            if directory is not None:
                scale.write_cached(directory, url, fixture)
        prepare_s = time.perf_counter() - started
        db = make_engine(settings)
        lib = Engine(settings, db=db)
        yield ScaleRun(url, settings, db, lib, fixture, cached is not None, prepare_s)
    finally:
        reembed_job.bind(before)
        if lib is not None:
            await lib.drainer.stop()
            await lib.models.aclose()
        if db is not None:
            await db.dispose()
        with admin.connect() as conn:
            conn.execute(drop)
        admin.dispose()
        forget_ledger(url)


# -- retrieval -------------------------------------------------------------------------------------


@dataclass
class Retrievals:
    """Each measured query's stage timings (ms) and the degradations any of them reported."""

    timings: list[Mapping[str, float]] = field(default_factory=list)
    degraded: dict[str, int] = field(default_factory=dict)
    empty: int = 0

    @property
    def totals(self) -> list[float]:
        return [t["total"] for t in self.timings]


async def measure_retrieval(
    run: ScaleRun, queries: Sequence[str], *, warmup: int = 0
) -> Retrievals:
    """``Engine.retrieve`` for each query at the head, omniscient, after ``warmup`` untimed
    ones (the first queries again)."""
    story = run.fixture.story.story_id
    for q in [*queries][:warmup]:
        await run.lib.retrieve(run.ctx, story, q=q, viewpoint=VIEWPOINT)
    found = Retrievals()
    for q in queries:
        result = await run.lib.retrieve(run.ctx, story, q=q, viewpoint=VIEWPOINT)
        found.timings.append(dict(result.timings))
        for reason in result.degradation:
            found.degraded[reason] = found.degraded.get(reason, 0) + 1
        if not result.memories and not result.episodes:
            found.empty += 1
    return found


def percentiles(samples: Sequence[float]) -> dict[str, float]:
    """Nearest-rank p50/p95/p99 and the maximum, in the samples' unit, rounded."""
    return {
        "p50": round(perf.percentile(samples, 50), 3),
        "p95": round(perf.percentile(samples, 95), 3),
        "p99": round(perf.percentile(samples, 99), 3),
        "max": round(max(samples), 3),
    }


def stage_percentiles(timings: Sequence[Mapping[str, float]]) -> dict[str, dict[str, float]]:
    stages = list(timings[0]) if timings else []
    return {stage: percentiles([t[stage] for t in timings]) for stage in stages}


# -- results ---------------------------------------------------------------------------------------


def host_info(server: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Where the measurement ran: the host label, the machine, and the runner image when
    GitHub's runner sets it."""
    try:
        memory = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
    except ValueError, OSError, AttributeError:
        memory = 0
    return {
        "host": os.environ.get(HOST_VAR, "local"),
        "platform": platform.platform(),
        "machine": platform.machine(),
        "vcpus": os.cpu_count() or 0,
        "ram_gb": round(memory / 2**30, 1),
        "runner_image": " ".join(
            v
            for v in (
                os.environ.get("ImageOS"),  # noqa: SIM112 - the runner's own spelling
                os.environ.get("ImageVersion"),  # noqa: SIM112
            )
            if v
        )
        or None,
        "sha": os.environ.get("GITHUB_SHA"),
        **dict(server or {}),
    }


async def server_info(db: AsyncEngine) -> dict[str, Any]:
    async with db.connect() as conn:
        version = await conn.scalar(text("SHOW server_version"))
        vector = await conn.scalar(
            text("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
        )
        buffers = await conn.scalar(text("SHOW shared_buffers"))
        maintenance = await conn.scalar(text("SHOW maintenance_work_mem"))
    return {
        "postgres": str(version),
        "pgvector": str(vector),
        "shared_buffers": str(buffers),
        "maintenance_work_mem": str(maintenance),
    }


def record(name: str, document: Mapping[str, Any]) -> Path:
    """Write ``document`` to ``build/perf/<name>.json`` and print it on one line."""
    out = RESULT_DIR / f"{name}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    body = {"measured_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), **document}
    out.write_text(json.dumps(body, indent=2, sort_keys=True) + "\n")
    print(f"{name}: {json.dumps(body, sort_keys=True)}")
    return out


def gate(measured_ms: float, budget_ms: float) -> list[str]:
    """The problems with a measured p95 against its budget (none: within it)."""
    if measured_ms <= budget_ms:
        return []
    return [f"p95 {measured_ms:.1f} ms is over the {budget_ms:.0f} ms budget"]


__all__ = [
    "CACHE_VAR",
    "HOST_VAR",
    "RESULT_DIR",
    "Retrievals",
    "ScaleRun",
    "gate",
    "host_info",
    "measure_retrieval",
    "percentiles",
    "record",
    "scale_database",
    "server_info",
    "stage_percentiles",
]
