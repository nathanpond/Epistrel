"""P-06: the vector-scale curve (docs/04 §11), the M4 part (NFR-SCALE-1; #153). This is
retrieval latency against memory count. It shows the HNSW index's headroom within the gated
range and publishes that range as the measured capacity. No capacity beyond it is claimed.

``test_p_06`` grows one story in one database through 1k → 10k → 50k → 100k memories. It starts
from the scale fixture at 1k memories (``scale.ScaleSpec.scaled``) and adds memories with
``scale.grow``, spread over the same turns. At each step the HNSW index is rebuilt the way a pin
builds it (``embeddings.retype``, ``maintenance_work_mem`` 1GB, no parallel workers). The step
records the build's elapsed time, the index's size and the retrieval latency of 200 queries
(P-01's templates, after 10 warm-up queries). Steps up to 50k memories are gated at NFR-PERF-1's
p95 ≤ 150 ms. The 100k step is measured and reported, never gated: NFR-SCALE-1's range ends at
~50k. The figures go to ``build/perf/p_06.json``, and from there to ``docs/capacity.md`` with
the server's settings. The test is ``perf_weekly``. ``test_p_06__smoke`` runs a 500 → 1k curve
in PR CI with no timing bound (#430, #432).

``test_p_06__retirement`` measures #147's forced story-scope retirement spike on the 50k
fixture (``perf_weekly``). This is the commit in which an overflow episode reaches its deadline.
It ranks every unpinned hot vector-bearing row of the story in Python (about 50k) and retires
one batch (``RETIREMENT_BATCH_SIZE``) inside the turn's transaction. The story's vector budget
is set 500 under its count, so one forced run has exactly one batch to retire. The episode's
deadline is set to the head, and ``on_turn_committed`` runs as the deadline turn's commit runs
it. Everything is rolled back, so the shared fixture is unchanged. The spike is reported beside
NFR-PERF-3's 250 ms turn-commit budget and P-03's 2 s single-commit bound. No requirement
budgets a single commit, so the spike is not gated (#147's carried obligation: measure and
report it, and file it if it breaks a stated budget). ``test_p_06__retirement_smoke`` runs the
same measurement on the 1k fixture in PR CI.
"""

from __future__ import annotations

import json
import time
from typing import Any

import pytest
from sqlalchemy import text

from epistrel.core.memory import records, retirement
from epistrel.core.profiles.effective import effective_configuration
from epistrel.testing.fixtures import scale
from tests.perf.scale_support import (
    ScaleRun,
    gate,
    host_info,
    measure_retrieval,
    percentiles,
    record,
    scale_database,
    server_info,
)

STEPS = (1_000, 10_000, 50_000, 100_000)
GATED_UP_TO = 50_000
"""NFR-SCALE-1's range: the curve is gated up to here, reported beyond."""
QUERIES = 200
WARMUP = 10
SMOKE_STEPS = (500, 1_000)
SMOKE_QUERIES = 20
BUDGET_MS = 150.0
"""NFR-PERF-1, at every gated step."""
COMMIT_BUDGET_MS = 250.0
"""NFR-PERF-3's turn-commit p95, which the spike is reported beside."""
STUCK_MS = 2_000.0
"""P-03's bound on any single commit."""


async def _curve(run: ScaleRun, steps: tuple[int, ...], queries: int) -> list[dict[str, Any]]:
    """Grow the run's story through ``steps``; at each, the index build and the retrieval."""
    texts = [q for _, q in scale.queries(run.fixture.spec, run.fixture.story, queries)]
    points: list[dict[str, Any]] = []
    for step in steps:
        index = await scale.grow(run.db, run.fixture, step)
        if index is None:  # the first step is the fixture as built
            index = await scale.rebuild_index(run.db, run.fixture.spec.dimension)
            run.fixture.index = index
        found = await measure_retrieval(run, texts, warmup=WARMUP)
        assert found.degraded == {}, found.degraded
        latency = percentiles(found.totals)
        points.append(
            {
                "memories": run.fixture.story.memories,
                "hnsw_build_s": index.build_s,
                "index_bytes": index.bytes,
                "vectors": index.vectors,
                "retrieval_ms": latency,
                "gated": step <= GATED_UP_TO,
                "within_budget": not gate(latency["p95"], BUDGET_MS),
            }
        )
    return points


async def _curve_summary(run: ScaleRun, points: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "test": "P-06",
        "requirement": "NFR-SCALE-1",
        "budget_p95_ms": BUDGET_MS,
        "gated_up_to": GATED_UP_TO,
        "queries_per_step": QUERIES,
        "events": run.fixture.story.events,
        "maintenance_work_mem": scale.MAINTENANCE_WORK_MEM,
        "points": points,
        "host": host_info(await server_info(run.db)),
    }


@pytest.mark.perf_weekly
@pytest.mark.part("M4", "NFR-SCALE-1")
async def test_p_06(pg_url: str) -> None:
    """The curve at 1k, 10k, 50k and 100k memories: the index holds every vector at each step,
    and retrieval's p95 is within 150 ms at every step up to 50k (100k is reported only).

    Complement asserted: the index and the vector count grow with the memories, so each point
    measures the bigger set and not the first one again."""
    async with scale_database(pg_url, scale.ScaleSpec.scaled(STEPS[0])) as run:
        points = await _curve(run, STEPS, QUERIES)
        summary = await _curve_summary(run, points)
    record("p_06", summary)
    assert [p["memories"] for p in points] == list(STEPS)
    assert [p["vectors"] for p in points] == list(STEPS)
    sizes = [p["index_bytes"] for p in points]
    assert sizes == sorted(sizes), sizes
    assert len(set(sizes)) == len(sizes), sizes
    missed = [p for p in points if p["gated"] and not p["within_budget"]]
    assert missed == [], json.dumps(missed)


@pytest.mark.perf_smoke
@pytest.mark.part("M4", "NFR-SCALE-1")
async def test_p_06__smoke(pg_url: str) -> None:
    """PR CI's subset: a 500 → 1k curve through the same growth, index rebuild and summary,
    with no timing bound.

    Complement asserted: the second point's index holds twice the vectors and is larger than
    the first's."""
    async with scale_database(pg_url, scale.ScaleSpec.scaled(SMOKE_STEPS[0])) as run:
        points = await _curve(run, SMOKE_STEPS, SMOKE_QUERIES)
        summary = await _curve_summary(run, points)
    record("p_06_smoke", summary)
    first, second = points
    assert (first["vectors"], second["vectors"]) == SMOKE_STEPS
    assert second["index_bytes"] > first["index_bytes"]
    assert first["hnsw_build_s"] > 0
    assert second["hnsw_build_s"] > 0
    assert summary["host"]["maintenance_work_mem"]


async def _forced_run(run: ScaleRun) -> dict[str, Any]:
    """#147's forced story-scope run in a deadline turn's commit, rolled back (see the module
    docstring): its milliseconds, the ranking's alone, and what it retired."""
    story = run.fixture.story.story_id
    async with run.db.connect() as conn:
        tx = await conn.begin()
        try:
            exact = await retirement.exact_counts(conn, story)
            batch = retirement.RETIREMENT_BATCH_SIZE
            budget = max(1, exact.story_vectors - batch)
            await conn.execute(
                text(
                    "INSERT INTO story_profile_overrides (story_id, settings, revision) "
                    "VALUES (:s, CAST(:o AS jsonb), 1) ON CONFLICT (story_id) DO UPDATE "
                    "SET settings = excluded.settings"
                ),
                {
                    "s": story,
                    "o": json.dumps({"memory": {"hot_set": {"vectors_per_story": budget}}}),
                },
            )
            await retirement.reconcile(conn, story)
            opened = await conn.execute(
                text(
                    "UPDATE overflow_episodes SET deadline_turn = opened_turn "
                    "WHERE story_id = :s AND closed_turn IS NULL AND scope = 'story' "
                    "RETURNING id"
                ),
                {"s": story},
            )
            episodes = [str(r.id) for r in opened]
            head = await retirement.head_turn(conn, story)
            settings = await effective_configuration(conn, story, validate=False)
            half_life = await records.half_life_turns(conn)
            budgets = retirement.Budgets.of(settings.settings)
            started = time.perf_counter()
            candidates = await retirement.ranked(
                conn,
                story,
                retirement.STORY,
                story,
                budgets,
                now=head,
                half_life=half_life,
            )
            ranking_ms = (time.perf_counter() - started) * 1000.0
            started = time.perf_counter()
            await retirement.on_turn_committed(conn, story, head, ())
            forced_ms = (time.perf_counter() - started) * 1000.0
            retired = await conn.execute(
                text(
                    "SELECT retired_reason, count(*) AS n FROM memories WHERE story_id = :s "
                    "AND retrieval_tier = 'retired' GROUP BY retired_reason"
                ),
                {"s": story},
            )
            by_reason = {str(r.retired_reason): int(r.n) for r in retired}
        finally:
            await tx.rollback()
    return {
        "test": "P-06 retirement spike",
        "requirement": "NFR-SCALE-1",
        "obligation": "#147",
        "vectors_ranked": len(candidates),
        "story_vectors": exact.story_vectors,
        "budget_set": budget,
        "episodes": len(episodes),
        "ranking_ms": round(ranking_ms, 3),
        "forced_run_ms": round(forced_ms, 3),
        "retired": by_reason,
        "commit_budget_p95_ms": COMMIT_BUDGET_MS,
        "single_commit_bound_ms": STUCK_MS,
        "over_commit_budget": forced_ms > COMMIT_BUDGET_MS,
        "over_single_commit_bound": forced_ms > STUCK_MS,
        "host": host_info(await server_info(run.db)),
    }


def _forced_as_designed(found: dict[str, Any], vectors: int) -> None:
    """One story-scope episode was forced and retired exactly one batch, after ranking every
    counted vector."""
    assert found["episodes"] == 1, found
    assert found["vectors_ranked"] == found["story_vectors"] >= vectors // 2, found
    assert sum(found["retired"].values()) == retirement.RETIREMENT_BATCH_SIZE, found


@pytest.mark.perf_weekly
@pytest.mark.part("M4", "NFR-SCALE-1")
async def test_p_06__retirement(scale_50k: ScaleRun) -> None:
    """#147's forced story-scope retirement on the 50k fixture: measured, reported beside the
    turn-commit budget, and rolled back.

    Complement asserted: the run ranked all of the story's ~50k counted vectors and retired
    exactly one batch, so the figure is the spike's worst case for one run, and the fixture is
    unchanged afterwards (nothing retired)."""
    found = await _forced_run(scale_50k)
    record("p_06_retirement", found)
    _forced_as_designed(found, scale_50k.fixture.story.memories)
    async with scale_50k.db.connect() as conn:
        left = await conn.scalar(
            text("SELECT count(*) FROM memories WHERE retrieval_tier = 'retired'")
        )
    assert left == 0


@pytest.mark.perf_smoke
@pytest.mark.part("M4", "NFR-SCALE-1")
async def test_p_06__retirement_smoke(scale_1k: ScaleRun) -> None:
    """PR CI's subset: the same forced run on the 1k fixture, with no timing bound.

    Complement asserted: as the weekly run, one episode, one batch retired after ranking every
    counted vector, and nothing left retired after the rollback."""
    found = await _forced_run(scale_1k)
    record("p_06_retirement_smoke", found)
    _forced_as_designed(found, scale_1k.fixture.story.memories)
    async with scale_1k.db.connect() as conn:
        left = await conn.scalar(
            text("SELECT count(*) FROM memories WHERE retrieval_tier = 'retired'")
        )
    assert left == 0
