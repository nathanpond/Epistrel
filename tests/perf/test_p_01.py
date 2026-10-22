"""P-01: retrieval latency (docs/04 §11), the M4 part (NFR-PERF-1; #153). Viewpoint-scoped
retrieval of a story with ~50k memories completes within p95 ≤ 150 ms, model calls excluded.

The span is ``Engine.retrieve``'s pipeline (#141), as its own ``timings`` report it: resolution,
the occasion step, the vector arm (HNSW), the lexical and alias arms, fusion and ranking,
profiles and canon, and assembly. The query's embedding is taken out of every stage. Nothing is
retired in the fixture, so no query rehydrates.

``test_p_01`` is the measurement on the scale fixture (``epistrel.testing.fixtures.scale``:
50k memories, 100k events, 12 characters). It runs 500 queries drawn 40/40/20 from occasion,
free-text and proper-noun templates, omniscient at the head, one after another after 20
warm-up queries. It reports p50/p95/p99 by stage and gates the total's p95. A miss is measured
once more before the gate fails. The test is ``perf_weekly``: deselected unless ``-m`` names the
marker, it runs in the weekly ``perf.yml`` on the GitHub-hosted runner, labelled as such. Its
figures go to ``build/perf/p_01.json`` and from there to ``docs/perf.md``.

``test_p_01__smoke`` is PR CI's ``perf_smoke`` run. It runs the same plumbing on the 1k fixture
with no timing bound, because a timing bound in the PR gate fails under host load (#430,
#432). ``test_p_01__gate_bites`` is the complement: with the vector arm slowed by 200 ms, the
gate refuses the run. Slowing only adds time, so the run cannot pass by luck on a busy host.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import Any

import pytest

from epistrel.core.retrieval import pipeline
from epistrel.core.retrieval.pipeline import STAGES
from epistrel.core.retrieval.search import vector_search
from epistrel.testing.fixtures import scale
from tests.perf.scale_support import (
    Retrievals,
    ScaleRun,
    gate,
    host_info,
    measure_retrieval,
    percentiles,
    record,
    server_info,
    stage_percentiles,
)

QUERIES = 500
WARMUP = 20
SMOKE_QUERIES = 50
SMOKE_WARMUP = 5
BUDGET_MS = 150.0
"""NFR-PERF-1."""
SLOWED_S = 0.200


def _queries(run: ScaleRun, n: int) -> list[str]:
    return [q for _, q in scale.queries(run.fixture.spec, run.fixture.story, n)]


async def _summary(run: ScaleRun, found: Retrievals, attempts: Sequence[float]) -> dict[str, Any]:
    return {
        "test": "P-01",
        "requirement": "NFR-PERF-1",
        "budget_p95_ms": BUDGET_MS,
        "queries": len(found.timings),
        "memories": run.fixture.story.memories,
        "events": run.fixture.story.events,
        "total_ms": percentiles(found.totals),
        "stages_ms": stage_percentiles(found.timings),
        "degraded": found.degraded,
        "empty_results": found.empty,
        "attempt_p95_ms": [round(a, 3) for a in attempts],
        "restored_from_cache": run.restored,
        "fixture_s": round(run.prepare_s, 1),
        "fixture_timings_s": run.fixture.timings,
        "host": host_info(await server_info(run.db)),
    }


def _plumbing(found: Retrievals, n: int) -> None:
    """Every query was answered with every stage timed, the vector arm ran, and most queries
    found something."""
    assert len(found.timings) == n
    assert all(set(t) == {*STAGES, "total"} for t in found.timings)
    assert found.degraded == {}, found.degraded
    assert found.empty < n // 2, found.empty


@pytest.mark.perf_weekly
@pytest.mark.part("M4", "NFR-PERF-1")
async def test_p_01(scale_50k: ScaleRun) -> None:
    """500 retrievals over the 50k-memory fixture: the total's p95 is within 150 ms (a miss is
    measured once more first).

    Complement asserted: every query is answered with all its stages timed and the vector arm
    running (no degradation), so the figure is not that of a skipped arm."""
    queries = _queries(scale_50k, QUERIES)
    found = await measure_retrieval(scale_50k, queries, warmup=WARMUP)
    attempts = [percentiles(found.totals)["p95"]]
    if gate(attempts[0], BUDGET_MS):
        found = await measure_retrieval(scale_50k, queries)
        attempts.append(percentiles(found.totals)["p95"])
    summary = await _summary(scale_50k, found, attempts)
    record("p_01", summary)
    _plumbing(found, QUERIES)
    assert gate(attempts[-1], BUDGET_MS) == [], summary


@pytest.mark.perf_smoke
@pytest.mark.part("M4", "NFR-PERF-1")
async def test_p_01__smoke(scale_1k: ScaleRun) -> None:
    """PR CI's subset: 50 retrievals over the 1k fixture through the same measurement and
    summary, with no timing bound.

    Complement asserted: the summary carries every stage's percentiles and the host it ran on,
    and nearest-rank p50 ≤ p95 ≤ p99 ≤ max."""
    found = await measure_retrieval(
        scale_1k, _queries(scale_1k, SMOKE_QUERIES), warmup=SMOKE_WARMUP
    )
    _plumbing(found, SMOKE_QUERIES)
    summary = await _summary(scale_1k, found, [percentiles(found.totals)["p95"]])
    record("p_01_smoke", summary)
    assert set(summary["stages_ms"]) == {*STAGES, "total"}
    total = summary["total_ms"]
    assert total["p50"] <= total["p95"] <= total["p99"] <= total["max"]
    assert summary["host"]["pgvector"]


@pytest.mark.perf_smoke
@pytest.mark.part("M4", "NFR-PERF-1")
async def test_p_01__gate_bites(scale_1k: ScaleRun, monkeypatch: pytest.MonkeyPatch) -> None:
    """With the vector arm slowed by 200 ms, P-01's gate refuses the run: the total's p95 is
    over 150 ms, and the extra time shows in the vector stage, not in another one.

    Complement asserted: the gate accepts a p95 at the budget and refuses one just over it."""

    async def slowed(*args: Any, **kwargs: Any) -> Any:
        await asyncio.sleep(SLOWED_S)
        return await vector_search(*args, **kwargs)

    monkeypatch.setattr(pipeline, "vector_search", slowed)
    found = await measure_retrieval(scale_1k, _queries(scale_1k, 10))
    p95 = percentiles(found.totals)["p95"]
    assert p95 >= SLOWED_S * 1000
    assert gate(p95, BUDGET_MS), p95
    assert min(t["vector"] for t in found.timings) >= SLOWED_S * 1000
    assert gate(BUDGET_MS, BUDGET_MS) == []
    assert gate(BUDGET_MS + 0.1, BUDGET_MS)
