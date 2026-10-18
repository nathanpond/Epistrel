"""The scale fixtures P-01, P-02 and P-06 share (#153), built once per session and only when a
test asks for one: ``scale_50k`` (the default spec: 50k memories, 100k events) for the weekly
suite, ``scale_1k`` (the same proportions at 1k memories) for the PR smoke."""

from __future__ import annotations

import sys
from collections.abc import AsyncIterator

import pytest

from epistrel.testing.fixtures import scale
from tests.perf.scale_support import ScaleRun, scale_database

SMOKE_MEMORIES = 1_000


def _log(message: str) -> None:
    print(f"[scale fixture] {message}", file=sys.stderr, flush=True)


@pytest.fixture(scope="session")
async def scale_50k(pg_url: str) -> AsyncIterator[ScaleRun]:
    async with scale_database(pg_url, scale.ScaleSpec(), log=_log) as run:
        yield run


@pytest.fixture(scope="session")
async def scale_1k(pg_url: str) -> AsyncIterator[ScaleRun]:
    async with scale_database(pg_url, scale.ScaleSpec.scaled(SMOKE_MEMORIES)) as run:
        yield run
