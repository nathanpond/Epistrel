"""P-01: retrieval latency (docs/04 §11), the M4 part (NFR-PERF-1; #153). Viewpoint-scoped
retrieval of a story with ~50k memories completes within p95 ≤ 150 ms, model calls excluded.

The span is ``Engine.retrieve``'s pipeline (#141), as its own ``timings`` report it: resolution,
the occasion step, the vector arm (HNSW), the lexical and alias arms, fusion and ranking,
profiles and canon, and assembly. The query's embedding is taken out of every stage. Nothing is
retired in the fixture, so no query rehydrates.

``test_p_01`` is the measurement on the scale fixture (``epistrel.testing.fixtures.scale``:
50k memories, 100k events, 12 characters). It runs 500 queries drawn 40/40/20 from occasion,
free-text and proper-noun templates, omniscient at the head, one after another after 20
warm-up queries. It reports p50/p95/p99 by stage and the total's p95 against the budget
(``within_budget``). A miss is measured once more, and then it fails the test only on the
reference host (``EPISTREL_TEST_PERF_HOST_CLASS=reference``; ADR-190, #793). On any other host
it is recorded and reported, not gated. An error, a degraded or a mostly empty result fails on
either. The test is ``perf_weekly``: deselected unless ``-m`` names the marker, it runs in the
weekly ``perf.yml`` on the GitHub-hosted runner, labelled as such (host class ``other``). Its
figures go to ``build/perf/p_01.json`` and from there to ``docs/perf.md``, with the owner's
200 ms target for that runner (``P01_RUNNER_TARGET_MS``, #868) as a note row beside the budget,
reported against the last attempt's p95 and never gated. The same queries then run once more
as a character's message (#906): the viewpoint and the speaker are the cast's first character
after the player, as a render retrieves for it. That pass is recorded under ``character`` and
reported beside the budget, never gated; its errors, degradations and empty results fail as
the omniscient pass's do.

``test_p_01__smoke`` is PR CI's ``perf_smoke`` run. It runs the same plumbing on the 1k fixture
with no timing bound, because a timing bound in the PR gate fails under host load (#430,
#432). ``test_p_01__gate_bites`` is the complement: with the vector arm slowed by 200 ms, the
p95 is a miss, which fails on the reference host and is recorded and passes on any other.
Slowing only adds time, so the run cannot pass by luck on a busy host.
``test_p_01__own_graph`` (#866; ADR-187) runs one retrieval on a database of three 1k
stories and checks the vector arm's plan: an index scan of the story's own partition's HNSW
index, with no other story's partition in it.
``test_p_01__gap_marks`` (#888) pins the cost of the coverage-gap marks every retrieval
carries: they read no turn history when the story has no gap, and one row a run of the turns a
gap leaves, and they count in the retrieval's ``total``.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from epistrel.core.backfill import coverage
from epistrel.core.embeddings import HNSW_INDEX
from epistrel.core.memory.story_partitions import partition_name as memories_table_of
from epistrel.core.profiles.effective import effective_configuration
from epistrel.core.retrieval import coverage_marks, pipeline
from epistrel.core.retrieval.pipeline import STAGES
from epistrel.core.retrieval.search import EMBEDDING_UNAVAILABLE, vector_search
from epistrel.testing.fixtures import scale
from tests.perf.scale_support import (
    HOST_CLASS_VAR,
    HOST_CLASSES,
    OTHER,
    REFERENCE,
    VIEWPOINT,
    Retrievals,
    ScaleRun,
    Verdict,
    budget_verdict,
    gate,
    host_class,
    host_info,
    measure_retrieval,
    percentiles,
    record,
    server_info,
    speaker_of,
    stage_percentiles,
)
from tests.retrieval_support import index_names, statements

QUERIES = 500
WARMUP = 20
SMOKE_QUERIES = 50
SMOKE_WARMUP = 5
BUDGET_MS = 150.0
"""NFR-PERF-1."""
SLOWED_S = 0.200
P01_RUNNER_TARGET_MS = 200.0
"""#868: the owner's target for the total p95 on the GitHub-hosted runner (the run's last
attempt, ``attempt_p95_ms[-1]``). Reported beside NFR-PERF-1's budget as a note row in
docs/perf.md (``epistrel.tools.perf_docs``), never gated: the budget stays 150 ms (ADR-190)."""


def _queries(run: ScaleRun, n: int) -> list[str]:
    return [q for _, q in scale.queries(run.fixture.spec, run.fixture.story, n)]


async def _summary(run: ScaleRun, found: Retrievals, attempts: Sequence[float]) -> dict[str, Any]:
    return {
        "test": "P-01",
        "requirement": "NFR-PERF-1",
        "budget_p95_ms": BUDGET_MS,
        "runner_target_p95_ms": P01_RUNNER_TARGET_MS,
        "within_budget": budget_verdict(attempts[-1], BUDGET_MS, host_class()).within,
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


def _character_pass(run: ScaleRun, found: Retrievals, speaker: str) -> dict[str, Any]:
    """#906: the character pass's figures, beside the omniscient ones (reported, not gated)."""
    return {
        "viewpoint": f"character:{speaker}",
        "speaker": next(n for n, c in run.fixture.story.characters.items() if c == speaker),
        "queries": len(found.timings),
        "total_ms": percentiles(found.totals),
        "stages_ms": stage_percentiles(found.timings),
        "degraded": found.degraded,
        "empty_results": found.empty,
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
    measured once more first), gated on the reference host and reported elsewhere (ADR-190).

    Complement asserted: every query is answered with all its stages timed and the vector arm
    running (no degradation), so the figure is not that of a skipped arm."""
    queries = _queries(scale_50k, QUERIES)
    found = await measure_retrieval(scale_50k, queries, warmup=WARMUP)
    attempts = [percentiles(found.totals)["p95"]]
    if gate(attempts[0], BUDGET_MS):
        found = await measure_retrieval(scale_50k, queries)
        attempts.append(percentiles(found.totals)["p95"])
    summary = await _summary(scale_50k, found, attempts)
    speaker = speaker_of(scale_50k)
    as_character = await measure_retrieval(scale_50k, queries, warmup=WARMUP, character=speaker)
    summary["character"] = _character_pass(scale_50k, as_character, speaker)
    record("p_01", summary)
    _plumbing(found, QUERIES)
    _plumbing(as_character, QUERIES)
    assert not budget_verdict(attempts[-1], BUDGET_MS, host_class()).fail, summary


@pytest.mark.perf_smoke
@pytest.mark.part("M4", "NFR-PERF-1")
async def test_p_01__smoke(scale_1k: ScaleRun) -> None:
    """PR CI's subset: 50 retrievals over the 1k fixture through the same measurement and
    summary, omniscient and as a character's message (#906), with no timing bound.

    Complement asserted: the summary carries every stage's percentiles, the host it ran on and
    #868's runner target, and nearest-rank p50 ≤ p95 ≤ p99 ≤ max; the character pass is that
    character's (its viewpoint, not the player's) with every stage timed."""
    queries = _queries(scale_1k, SMOKE_QUERIES)
    found = await measure_retrieval(scale_1k, queries, warmup=SMOKE_WARMUP)
    _plumbing(found, SMOKE_QUERIES)
    speaker = speaker_of(scale_1k)
    as_character = await measure_retrieval(
        scale_1k, queries, warmup=SMOKE_WARMUP, character=speaker
    )
    _plumbing(as_character, SMOKE_QUERIES)
    summary = await _summary(scale_1k, found, [percentiles(found.totals)["p95"]])
    summary["character"] = _character_pass(scale_1k, as_character, speaker)
    record("p_01_smoke", summary)
    assert summary["character"]["viewpoint"] == f"character:{speaker}"
    assert speaker != scale_1k.fixture.story.player
    assert set(summary["character"]["stages_ms"]) == {*STAGES, "total"}
    assert set(summary["stages_ms"]) == {*STAGES, "total"}
    assert summary["runner_target_p95_ms"] == P01_RUNNER_TARGET_MS  # #868's note row
    total = summary["total_ms"]
    assert total["p50"] <= total["p95"] <= total["p99"] <= total["max"]
    assert summary["host"]["pgvector"]


@pytest.mark.perf_smoke
@pytest.mark.part("M4", "NFR-PERF-1")
async def test_p_01__gate_bites(scale_1k: ScaleRun, monkeypatch: pytest.MonkeyPatch) -> None:
    """With the vector arm slowed by 200 ms, the total's p95 is over 150 ms, and the extra
    time shows in the vector stage, not in another one. The host-aware verdict (ADR-190, #793)
    fails that miss on the reference host. On any other host it records the miss in the
    summary (``within_budget: false``) and passes.

    Complement asserted: a p95 at the budget is within it on both classes and one just over it
    is a miss; a missing measurement fails on both; a degraded or a mostly empty result fails
    the plumbing check on both; the host class defaults to ``other``, and an unknown one is an
    error."""

    async def slowed(*args: Any, **kwargs: Any) -> Any:
        await asyncio.sleep(SLOWED_S)
        return await vector_search(*args, **kwargs)

    monkeypatch.setattr(pipeline, "vector_search", slowed)
    found = await measure_retrieval(scale_1k, _queries(scale_1k, 10))
    p95 = percentiles(found.totals)["p95"]
    assert p95 >= SLOWED_S * 1000
    assert gate(p95, BUDGET_MS), p95
    assert min(t["vector"] for t in found.timings) >= SLOWED_S * 1000
    assert budget_verdict(p95, BUDGET_MS, REFERENCE) == Verdict(within=False, fail=True)
    assert budget_verdict(p95, BUDGET_MS, OTHER) == Verdict(within=False, fail=False)
    monkeypatch.setenv(HOST_CLASS_VAR, OTHER)
    summary = await _summary(scale_1k, found, [p95])
    assert summary["within_budget"] is False, summary
    assert summary["host"]["host_class"] == OTHER

    for cls in HOST_CLASSES:
        assert budget_verdict(BUDGET_MS, BUDGET_MS, cls) == Verdict(within=True, fail=False)
        assert not budget_verdict(BUDGET_MS + 0.1, BUDGET_MS, cls).within
        assert budget_verdict(None, BUDGET_MS, cls) == Verdict(within=False, fail=True)
        assert budget_verdict(float("nan"), BUDGET_MS, cls).fail
        monkeypatch.setenv(HOST_CLASS_VAR, cls)
        degraded = Retrievals(found.timings, {EMBEDDING_UNAVAILABLE: 1}, 0)
        with pytest.raises(AssertionError):
            _plumbing(degraded, len(found.timings))
        empty = Retrievals(found.timings, {}, len(found.timings))
        with pytest.raises(AssertionError):
            _plumbing(empty, len(found.timings))

    monkeypatch.delenv(HOST_CLASS_VAR)
    assert host_class() == OTHER
    monkeypatch.setenv(HOST_CLASS_VAR, "Reference")
    with pytest.raises(ValueError, match=HOST_CLASS_VAR):
        host_class()
    with pytest.raises(ValueError, match="host class"):
        budget_verdict(p95, BUDGET_MS, "fast")


async def _arm_plan(run: ScaleRun, settings: Sequence[str], sql: str, params: Any) -> str:
    """``EXPLAIN`` of the vector arm's statement under the settings the arm ran with (the
    pipeline's ``SET LOCAL hnsw.*`` and the arm's ``enable_sort = off``)."""
    async with run.db.connect() as conn, conn.begin() as tx:
        for setting in settings:
            await conn.execute(text(setting))
        await conn.execute(text("SET LOCAL enable_sort = off"))
        raw = await conn.get_raw_connection()
        cursor = raw.driver_connection.cursor()  # type: ignore[union-attr]
        await cursor.execute("EXPLAIN " + sql, params)
        found = await cursor.fetchall()
        await tx.rollback()
    return "\n".join(str(r[0]) for r in found)


@pytest.mark.perf_smoke
@pytest.mark.part("M4", "NFR-PERF-1")
async def test_p_01__own_graph(scale_1k_stories: ScaleRun) -> None:
    """On a database of three 1k stories, a retrieval's vector arm is an index scan of the
    HNSW index of the story's own memories table (#866; ADR-187): no other story's table, and
    no "Seq Scan", is in its plan.

    Complement asserted: the other stories hold vectors of the same space (their tables' HNSW
    indexes exist), so the plan leaves them out because it names its story, not because they
    are empty."""
    run = scale_1k_stories
    assert len(run.others) >= 2
    story = run.fixture.story.story_id
    with statements(run.db) as seen:
        await run.lib.retrieve(run.ctx, story, q=_queries(run, 1)[0], viewpoint=VIEWPOINT)
    arms = [(sql, p) for sql, p in seen.statements if "<=>" in sql]
    assert len(arms) == 1, [sql for sql, _ in arms]
    settings = [sql for sql, _ in seen.statements if sql.startswith("SET LOCAL hnsw.")]
    plan = await _arm_plan(run, settings, *arms[0])

    own = memories_table_of(story)
    async with run.db.connect() as conn:
        found = await conn.execute(
            text(
                "SELECT c.relname AS index, t.relname AS tbl FROM pg_index i "
                "JOIN pg_class c ON c.oid = i.indexrelid JOIN pg_class t ON t.oid = i.indrelid "
                "WHERE c.relname = ANY(:names)"
            ),
            {"names": sorted(await index_names(run.db, HNSW_INDEX))},
        )
        hnsw = {str(r.tbl): str(r.index) for r in found}
    others = [memories_table_of(o.story.story_id) for o in run.others]
    assert own in hnsw, hnsw
    assert all(o in hnsw for o in others), hnsw
    assert f"Index Scan using {hnsw[own]} on {own}" in plan, plan
    assert "Seq Scan" not in plan, plan
    assert not [o for o in others if o in plan], plan


async def _rows_read(conn: AsyncConnection, sql: str, params: Any, relation: str) -> int:
    """The rows ``EXPLAIN ANALYZE`` of ``sql`` reads from ``relation`` (its scan nodes')."""
    raw = await conn.get_raw_connection()
    cursor = raw.driver_connection.cursor()  # type: ignore[union-attr]
    await cursor.execute("EXPLAIN (ANALYZE, FORMAT JSON) " + sql, params)
    (plan,) = (await cursor.fetchone())[0]
    found, nodes = 0, [plan["Plan"]]
    while nodes:
        node = nodes.pop()
        if node.get("Relation Name") == relation:
            found += int(node["Actual Rows"]) * int(node.get("Actual Loops", 1))
        nodes += node.get("Plans", [])
    return found


@pytest.mark.perf_smoke
@pytest.mark.part("M4", "NFR-PERF-1")
async def test_p_01__gap_marks(scale_1k: ScaleRun, monkeypatch: pytest.MonkeyPatch) -> None:
    """#888: the coverage-gap marks of a retrieval (``coverage_marks.gaps``) cost what the
    story's coverage leaves uncovered, not its turn history. On the 1k fixture (500 turns), as
    built, there is no gap and no statement reads ``operations``. With the pipeline's coverage
    broken at turns 101-103, the one gap is found by a read of ``operations`` that answers one
    row and reads three. A retrieval's ``total`` includes the marks: slowed by 200 ms, the total
    is at least that.

    Complement asserted: the fixture's turns are all active (a read of every turn would answer
    500 rows), and the gap is found, so the bounded read is not an empty one."""
    run = scale_1k
    story = run.fixture.story
    window = (1, story.head)
    async with run.db.connect() as conn:
        settings = (await effective_configuration(conn, story.story_id, validate=False)).settings
        assert len(await coverage.active_turns(conn, story.story_id)) == story.head == 500
        with statements(run.db) as clean:
            assert await coverage_marks.gaps(conn, story.story_id, settings, window=window) == []
        assert clean.reading("operations") == []

        level = coverage.capabilities_of(settings)["pipeline"]
        await conn.execute(
            text("DELETE FROM capability_coverage WHERE story_id = :s AND capability = 'pipeline'"),
            {"s": story.story_id},
        )
        for low, high in ((1, 100), (104, None)):
            await coverage.add(
                conn, story.story_id, "pipeline", low, high, source=coverage.LIVE, level=level
            )
        with statements(run.db) as broken:
            found = await coverage_marks.gaps(conn, story.story_id, settings, window=window)
        assert [(g.capability, g.from_turn, g.to_turn) for g in found] == [("pipeline", 101, 103)]
        reads = broken.reading("operations")
        assert len(reads) == 1, reads
        assert broken.rows_reading("operations") == 1
        assert await _rows_read(conn, *reads[0], "operations") == 3
        await conn.rollback()

    async def slowed(*args: Any, **kwargs: Any) -> Any:
        await asyncio.sleep(SLOWED_S)
        return await marks(*args, **kwargs)

    marks = coverage_marks.gaps
    monkeypatch.setattr(coverage_marks, "gaps", slowed)
    result = await run.lib.retrieve(
        run.ctx, story.story_id, q=_queries(run, 1)[0], viewpoint=VIEWPOINT
    )
    assert result.timings["total"] >= SLOWED_S * 1000, result.timings
