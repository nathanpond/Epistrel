"""S-CONSOLIDATION-RACE, M2 part (docs/04 §7): a background summary job whose inputs a rewind
changed while it ran drops its output and runs again over the surviving turns (FR-CONC-8,
FR-STORE-24,27). Two slices in one file (the parts gate allows one file per test id):

- test_s_consolidation_race__jobs (#70): the generic half, with the test handler of
  tests/integration/test_jobs.py;
- test_s_consolidation_race (#72): the real ``lite_summary`` job over turns 701-900,
  truncated to 800 while it calls the model.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Iterator

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.core import clock, summary_port
from epistrel.core.input_versions import input_key
from epistrel.core.tables import operations
from epistrel.orchestrator.jobs import handlers, queue
from epistrel.orchestrator.jobs.handlers import lite_summary
from epistrel.orchestrator.jobs.worker import Worker
from epistrel.orchestrator.models.access import ModelAccessLayer
from tests.auth_support import auth_settings
from tests.event_support import execute_in_tx, new_story, play_turn, rows, scalar
from tests.integration import test_jobs as jt
from tests.integration.test_jobs import DERIVED_KIND, LEASE_S, MovableClock, Probe
from tests.projection_support import truncate
from tests.summary_support import claim_summary, run_due_jobs, segment_rows, worker

_config = jt._config
_enqueue = jt._enqueue
_job = jt._job
_now = jt._now
_segments = jt._segments

SETTINGS = {"lite_window_messages": 2, "lite_summary_chunk": 200}
"""Two verbatim rows (the head turn) and 100 turns per segment: the grid is 1-100, 101-200,
..., 701-800, 801-900."""


@pytest.fixture
async def registry(engine: AsyncEngine) -> AsyncIterator[Probe]:
    """``tests/integration/test_jobs.py``'s handlers on an empty ``jobs`` table (its own
    ``registry`` fixture, for the slice that moved here)."""
    jt.PROBE = jt.Probe()
    for handler in jt.TEST_HANDLERS:
        handlers.register(handler)
    await execute_in_tx(engine, ("DELETE FROM jobs", {}))
    try:
        yield jt.PROBE
    finally:
        for handler in jt.TEST_HANDLERS:
            handlers.unregister(handler.kind)
        await execute_in_tx(engine, ("DELETE FROM jobs", {}))


@pytest.fixture
def moved() -> Iterator[MovableClock]:
    movable = MovableClock()
    clock.set_clock(movable)
    try:
        yield movable
    finally:
        clock.set_clock(None)


@pytest.fixture
def models(pg_url: str, engine: AsyncEngine) -> ModelAccessLayer:
    layer = ModelAccessLayer.from_settings(auth_settings(pg_url, model_adapter="stub"))
    layer.bind(engine)
    return layer


async def _turns(engine: AsyncEngine, story: str, first: int, last: int) -> None:
    """Turns ``first``..``last``, written with the summary scheduler off (the test schedules)."""
    summary_port.set_scheduler(None)
    try:
        for n in range(first, last + 1):
            await play_turn(engine, story, f"line {n}")
    finally:
        summary_port.set_scheduler(lite_summary.schedule)


async def _positions(engine: AsyncEngine, story: str) -> dict[str, tuple[int, int]]:
    found = await rows(
        engine,
        "SELECT operation_id, narrative_turn, turn_variant FROM operations "
        "WHERE story_id = :s AND kind = 'turn' AND status = 'committed'",
        s=story,
    )
    return {r.operation_id: (r.narrative_turn, r.turn_variant) for r in found}


async def _turn_ops(engine: AsyncEngine, story: str, first: int, last: int) -> set[str]:
    found = await rows(
        engine,
        "SELECT operation_id FROM operations WHERE story_id = :s AND kind = 'turn' "
        "AND membership = 'active' AND narrative_turn BETWEEN :a AND :b",
        s=story,
        a=first,
        b=last,
    )
    return {r.operation_id for r in found}


def _declared_turns(versions: dict[str, object], turn_ops: set[str]) -> set[str]:
    keys = {input_key(operations.name, op) for op in turn_ops}
    return {key for key in versions if key in keys}


@pytest.mark.part("M2", "FR-CONC-8; FR-STORE-24,27")
async def test_s_consolidation_race(engine: AsyncEngine, models: ModelAccessLayer) -> None:
    """The summary job over turns 701-900 declares every one of those turns as an input. A
    truncation to 800 while it calls the model makes its commit drop the output (no segment,
    its operations abandoned) and puts the same job back, due now, with no failure counted.
    After one more turn it runs again over the surviving 701-800 window only and commits.
    Segment events, recorded between turns, never move any turn's narrative position: every
    segment is positioned at the last turn it covers.

    Complement asserted: the first run, over 1-700 with nothing rewound, commits."""
    story = await new_story(engine)
    await execute_in_tx(
        engine,
        (
            "UPDATE stories SET settings = settings || CAST(:patch AS jsonb) WHERE id = :s",
            {"s": story, "patch": json.dumps(SETTINGS)},
        ),
    )
    await _turns(engine, story, 1, 701)
    assert await lite_summary.schedule(engine, story)
    assert await run_due_jobs(engine, models, story) == [queue.CommitOutcome.COMMITTED]
    assert [(r.covers_from, r.covers_to) for r in await segment_rows(engine, story)] == [
        (100 * k + 1, 100 * (k + 1)) for k in range(7)
    ]

    await _turns(engine, story, 702, 901)
    before = await _positions(engine, story)
    assert await lite_summary.schedule(engine, story)
    job = await claim_summary(engine, story)
    assert job is not None
    latch = models.stub.hold("lite_summary")
    run = asyncio.create_task(worker(engine).execute(job))
    await asyncio.wait_for(latch.entered.wait(), 10)
    over = await _turn_ops(engine, story, 701, 900)
    assert len(over) == 200
    await truncate(engine, story, 800)  # turns 801-901 are discarded while it runs
    latch.release()
    assert await run is queue.CommitOutcome.DROPPED

    (row,) = await rows(engine, "SELECT * FROM jobs WHERE id = :j", j=job.id)
    assert (row.status, row.attempt, row.failures) == ("queued", job.attempt, 0)
    assert abs((row.due_at - _now()).total_seconds()) < 2
    assert _declared_turns(row.input_versions, over) == {
        input_key(operations.name, op) for op in over
    }
    assert len(await segment_rows(engine, story)) == 7  # nothing from the stale run
    dropped = await rows(
        engine,
        "SELECT status FROM operations WHERE story_id = :s AND kind = :k AND status <> 'committed'",
        s=story,
        k=DERIVED_KIND,
    )
    assert [r.status for r in dropped] == ["abandoned"] * 3  # the job's own and two segments

    await play_turn(engine, story, "line 801, again")  # the window moves past 800
    assert [r.id for r in await rows(engine, QUEUED, s=story)] == [job.id]  # the same job
    assert await run_due_jobs(engine, models, story) == [queue.CommitOutcome.COMMITTED]
    (done,) = await rows(engine, "SELECT * FROM jobs WHERE id = :j", j=job.id)
    surviving = await _turn_ops(engine, story, 701, 800)
    assert _declared_turns(done.input_versions, over) == {
        input_key(operations.name, op) for op in surviving
    }
    newest = (await segment_rows(engine, story))[-1]
    assert (newest.covers_from, newest.covers_to, newest.membership) == (701, 800, "active")
    window = await scalar(
        engine,
        "SELECT source_window FROM operations WHERE operation_id = :o",
        o=newest.operation_id,
    )
    assert window == {"from_turn": 701, "to_turn": 800}

    after = await _positions(engine, story)
    assert {op: after[op] for op in before} == before
    placed = await rows(
        engine,
        "SELECT e.narrative_turn, e.payload FROM events e WHERE e.story_id = :s "
        "AND e.type = 'lite_summary_segment' ORDER BY e.seq",
        s=story,
    )
    assert all(p.narrative_turn == p.payload["covers_to"] for p in placed)
    turns = await rows(
        engine,
        "SELECT narrative_turn FROM operations WHERE story_id = :s AND kind = 'turn' "
        "AND membership = 'active' ORDER BY narrative_turn",
        s=story,
    )
    assert [t.narrative_turn for t in turns] == list(range(1, 802))


QUEUED = "SELECT id FROM jobs WHERE story_id = :s AND kind = 'lite_summary' AND status = 'queued'"


@pytest.mark.part("M2", "FR-CONC-8")
async def test_s_consolidation_race__jobs(
    engine: AsyncEngine, registry: Probe, moved: MovableClock
) -> None:
    """S-CONSOLIDATION-RACE's jobs half (#72 runs it with the real summary job): a job whose
    inputs a rewind discarded while it ran has its output dropped (no derived row; its
    operation is abandoned) and is re-enqueued on the same row, due now, with no failure
    counted. Complement: rerun, it declares only the surviving turns and commits."""
    story = await new_story(engine)
    for said in ("one", "two", "three"):
        await play_turn(engine, story, said)
    stale = await _enqueue(engine, "test_summarise", {"story_id": story}, story_id=story)
    claimed = await queue.claim(engine, "w:stale:0", lease_s=LEASE_S)
    assert claimed is not None
    assert claimed.id == stale
    run = asyncio.create_task(Worker(engine, _config()).execute(claimed))
    await asyncio.wait_for(registry.entered.wait(), 5.0)
    assert len(registry.declared[-1]) == 3
    await truncate(engine, story, 1)  # turns 2 and 3 are discarded while the job runs
    registry.gate.set()
    assert await run is queue.CommitOutcome.DROPPED
    row = await _job(engine, stale)
    assert (row.status, row.attempt, row.failures, row.result) == ("queued", 1, 0, None)
    assert abs((row.due_at - _now()).total_seconds()) < 2
    assert await _segments(engine, story) == []  # nothing derived from the stale run
    abandoned = await rows(
        engine,
        "SELECT status FROM operations WHERE story_id = :s AND kind = :k",
        s=story,
        k=DERIVED_KIND,
    )
    assert [r.status for r in abandoned] == ["abandoned"]
    rerun = await queue.claim(engine, "w:stale:0", lease_s=LEASE_S)
    assert rerun is not None
    assert rerun.id == stale
    assert await Worker(engine, _config()).execute(rerun) is queue.CommitOutcome.COMMITTED
    assert len(registry.declared[-1]) == 1  # recomputed against the current timeline
    row = await _job(engine, stale)
    assert row.status == "done"
    assert row.result["through_turn"] == 1
    assert set(row.input_versions) == set(registry.declared[-1])
    assert await _segments(engine, story) == ["derived"]
