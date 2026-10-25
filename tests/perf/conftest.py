"""The scale fixtures P-01, P-02 and P-06 share (#153), built once per session and only when a
test asks for one: ``scale_50k`` (the default spec: 50k memories, 100k events) for the weekly
suite, ``scale_1k`` (the same proportions at 1k memories) for the PR smoke.

``scale_50k_knowledge`` and ``scale_1k_knowledge`` (#182) add per-character beliefs and
exposures to them (``epistrel.testing.fixtures.knowledge``, no memory made private, so P-01's
reads are unchanged) for P-02's ``could_have_known`` span, once per session."""

from __future__ import annotations

import sys
from collections.abc import AsyncIterator

import pytest

from epistrel.testing.fixtures import knowledge, scale
from tests.perf.scale_support import KNOWLEDGE, ScaleRun, scale_database

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


@pytest.fixture(scope="session")
async def scale_50k_knowledge(scale_50k: ScaleRun) -> ScaleRun:
    fixture = scale_50k.fixture
    await knowledge.apply(scale_50k.db, fixture.spec, fixture.story, KNOWLEDGE)
    return scale_50k


@pytest.fixture(scope="session")
async def scale_1k_knowledge(scale_1k: ScaleRun) -> ScaleRun:
    fixture = scale_1k.fixture
    await knowledge.apply(scale_1k.db, fixture.spec, fixture.story, KNOWLEDGE)
    return scale_1k
