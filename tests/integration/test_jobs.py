"""Durable background jobs: claim, lease, fencing, the recovery pass and the input recheck (#70).

X-07's jobs half, ``test_x_07__jobs``, moved to ``tests/chaos/test_x_07.py`` with the rest of
X-07 (#71; a docs/04 part lives in one file) and uses this module's handlers. Test handlers
are registered per test through the handler registry; a worker is killed by cancelling its
task, and leases lapse by moving the injectable clock (``core.clock``), not by sleeping.
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
import time
from collections.abc import AsyncIterator, Iterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel, ConfigDict, ValidationError
from sqlalchemy import delete, select, text, update
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.api import create_app
from epistrel.config import Settings
from epistrel.core import clock
from epistrel.core.context import RequestContext
from epistrel.core.enums import OperationKind
from epistrel.core.errors import StoryBusy
from epistrel.core.events.operation import Operation
from epistrel.core.input_versions import (
    UnknownInput,
    check_key,
    input_key,
    read_version,
    stale_inputs,
)
from epistrel.core.stories import delete_story
from epistrel.core.tables import jobs, operations
from epistrel.engine import Engine
from epistrel.orchestrator.jobs import handlers, queue
from epistrel.orchestrator.jobs.handlers.rebuild import RebuildQueued
from epistrel.orchestrator.jobs.types import JobContext, JobHandler, JobOutput
from epistrel.orchestrator.jobs.worker import (
    Worker,
    WorkerConfig,
    recovery_pass,
    register_recovery_hook,
    unregister_recovery_hook,
)
from tests.auth_support import auth_settings, bearer
from tests.event_support import new_story, play_turn, rows, scalar
from tests.projection_support import truncate

POLL_S = 0.3
LEASE_S = 3.0


DERIVED_KIND = OperationKind.CONSOLIDATION.value


def _now() -> datetime:
    return clock.now()


class MovableClock:
    """The real time plus an offset the test moves (``clock.set_clock``)."""

    def __init__(self) -> None:
        self.offset = timedelta()

    def __call__(self) -> datetime:
        return datetime.now(UTC) + self.offset

    def advance(self, seconds: float) -> None:
        self.offset += timedelta(seconds=seconds)


# -- test handlers ---------------------------------------------------------------------------


class Numbered(BaseModel):
    model_config = ConfigDict(extra="forbid")

    n: int


class StoryJob(BaseModel):
    model_config = ConfigDict(extra="forbid")

    story_id: str


class Probe:
    """What the test handlers did, and the gate a blocking handler waits on."""

    def __init__(self) -> None:
        self.started: list[tuple[str, int]] = []
        self.gate = asyncio.Event()
        self.entered = asyncio.Event()
        self.declared: list[dict[str, Any]] = []


PROBE = Probe()


async def _echo(ctx: JobContext) -> JobOutput:
    PROBE.started.append((ctx.job.id, ctx.job.attempt))
    return JobOutput(result={"n": cast(Numbered, ctx.payload).n})


async def _block(ctx: JobContext) -> JobOutput:
    PROBE.started.append((ctx.job.id, ctx.job.attempt))
    PROBE.entered.set()
    await PROBE.gate.wait()
    return JobOutput(result={"n": cast(Numbered, ctx.payload).n})


async def _boom(ctx: JobContext) -> JobOutput:
    PROBE.started.append((ctx.job.id, ctx.job.attempt))
    raise RuntimeError("story text must never reach the job row")


async def _summarise(ctx: JobContext) -> JobOutput:
    """Declares every active turn of the story as an input, waits on the gate (the test
    rewinds the story meanwhile), then writes one derived segment through the last turn."""
    story_id = cast(StoryJob, ctx.payload).story_id
    PROBE.started.append((ctx.job.id, ctx.job.attempt))
    async with ctx.snapshot() as conn:
        turns = (
            await conn.execute(
                select(operations.c.operation_id, operations.c.narrative_turn)
                .where(
                    operations.c.story_id == story_id,
                    operations.c.kind == OperationKind.TURN.value,
                    operations.c.status == "committed",
                    operations.c.membership == "active",
                )
                .order_by(operations.c.narrative_turn)
            )
        ).all()
        for row in turns:
            await ctx.declare_input(conn, input_key(operations.name, row.operation_id))
    PROBE.declared.append(dict(ctx.input_versions))
    PROBE.entered.set()
    await PROBE.gate.wait()
    through = int(turns[-1].narrative_turn)

    def stage(op: Operation) -> None:
        op.append("lite_summary_segment", {"text": "derived", "through_turn": through})

    return JobOutput(stage=stage, result={"through_turn": through})


TEST_HANDLERS = (
    JobHandler(kind="test_echo", payload=Numbered, run=_echo),
    JobHandler(kind="test_block", payload=Numbered, run=_block),
    JobHandler(kind="test_boom", payload=Numbered, run=_boom),
    JobHandler(
        kind="test_summarise",
        payload=StoryJob,
        run=_summarise,
        operation_kind=OperationKind.CONSOLIDATION,
    ),
)


@pytest.fixture
async def registry(engine: AsyncEngine) -> AsyncIterator[Probe]:
    """Register the test handlers on an empty ``jobs`` table; leave both as they were."""
    global PROBE
    PROBE = Probe()
    for handler in TEST_HANDLERS:
        handlers.register(handler)
    async with engine.begin() as conn:
        await conn.execute(delete(jobs))
    try:
        yield PROBE
    finally:
        for handler in TEST_HANDLERS:
            handlers.unregister(handler.kind)
        async with engine.begin() as conn:
            await conn.execute(delete(jobs))


@pytest.fixture
def moved() -> Iterator[MovableClock]:
    movable = MovableClock()
    clock.set_clock(movable)
    try:
        yield movable
    finally:
        clock.set_clock(None)


async def _enqueue(engine: AsyncEngine, kind: str, payload: dict[str, Any], **kw: Any) -> str:
    async with engine.begin() as conn:
        return await queue.enqueue(conn, kind, payload, **kw)


async def _job(engine: AsyncEngine, job_id: str) -> Any:
    async with engine.connect() as conn:
        return (await conn.execute(select(jobs).where(jobs.c.id == job_id))).one_or_none()


async def _table(engine: AsyncEngine) -> list[tuple[Any, ...]]:
    async with engine.connect() as conn:
        return [tuple(r) for r in (await conn.execute(select(jobs).order_by(jobs.c.id))).all()]


async def _wait_for(probe: Any, within: float = 5.0) -> None:
    deadline = time.monotonic() + within
    while not await probe():
        assert time.monotonic() < deadline, "condition not reached in time"
        await asyncio.sleep(0.02)


def _config(**kw: Any) -> WorkerConfig:
    base = WorkerConfig(poll_interval=POLL_S, lease_s=LEASE_S, max_attempts=5, listen=False)
    return replace(base, concurrency=1, **kw)


async def _status(engine: AsyncEngine, job_id: str) -> str:
    row = await _job(engine, job_id)
    return str(row.status) if row is not None else "gone"


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


async def _done(engine: AsyncEngine, job_id: str) -> bool:
    return await _status(engine, job_id) == "done"


async def _segments(engine: AsyncEngine, story_id: str) -> list[str]:
    found = await rows(
        engine,
        "SELECT payload->>'text' AS segment FROM events "
        "WHERE story_id = :s AND type = 'lite_summary_segment' ORDER BY seq",
        s=story_id,
    )
    return [r.segment for r in found]


# -- complements -----------------------------------------------------------------------------


async def test_notify_wakes_a_worker_before_its_poll(engine: AsyncEngine, registry: Probe) -> None:
    """With LISTEN on and a 30 s poll, an enqueued job is claimed at once by the notification."""
    worker = Worker(engine, _config(listen=True, poll_interval=30.0))
    running = asyncio.create_task(worker.run())
    try:
        await asyncio.wait_for(worker.listening.wait(), 5.0)
        await asyncio.sleep(0.1)
        started = time.monotonic()
        job_id = await _enqueue(engine, "test_echo", {"n": 7})
        await _wait_for(lambda: _done(engine, job_id), within=5.0)
        assert time.monotonic() - started < 3.0
    finally:
        worker.stop()
        await running


async def test_failures_back_off_then_fail(
    engine: AsyncEngine, registry: Probe, moved: MovableClock
) -> None:
    """A failing job is retried with growing backoff and is ``failed`` after ``max_attempts``
    failures, storing the exception class only; a reclaim is not counted as a failure."""
    worker = Worker(engine, _config(max_attempts=3))
    job_id = await _enqueue(engine, "test_boom", {"n": 1})
    delays: list[float] = []
    for expected in ("queued", "queued", "failed"):
        claimed = await queue.claim(engine, "w:boom:0", lease_s=LEASE_S)
        assert claimed is not None
        failed_at = clock.now()
        assert await worker.execute(claimed) == expected
        row = await _job(engine, job_id)
        assert row.result == {"error": "RuntimeError"}
        if expected == "queued":
            delays.append((row.due_at - failed_at).total_seconds())
            assert await queue.claim(engine, "w:boom:0", lease_s=LEASE_S) is None  # not due
            moved.advance(400)
    assert 4.0 <= delays[0] <= 6.0
    assert 8.0 <= delays[1] <= 12.0
    row = await _job(engine, job_id)
    assert (row.status, row.failures, row.attempt) == ("failed", 3, 3)
    assert queue.backoff_s(10) <= queue.BACKOFF_CAP_S * 1.2

    reclaimed = await _enqueue(engine, "test_echo", {"n": 2})
    assert await queue.claim(engine, "w:lost:0", lease_s=LEASE_S) is not None
    moved.advance(LEASE_S + 1)
    assert await queue.reap(engine) == [reclaimed]
    row = await _job(engine, reclaimed)
    assert (row.status, row.failures, row.attempt) == ("queued", 0, 2)


async def test_enqueue_validates_and_coalesces(engine: AsyncEngine, registry: Probe) -> None:
    """``enqueue`` refuses an unknown kind and a bad payload before writing; a coalesce key
    held by a queued job returns that job with the earlier due time. A row of an unknown kind
    (a newer version wrote it) fails at claim."""
    async with engine.begin() as conn:
        with pytest.raises(queue.UnknownJobKind):
            await queue.enqueue(conn, "no_such_kind", {})
        with pytest.raises(ValidationError):
            await queue.enqueue(conn, "test_echo", {"n": "not a number"})
    assert await _table(engine) == []
    later = clock.now() + timedelta(hours=1)
    first = await _enqueue(engine, "test_echo", {"n": 1}, due_at=later, coalesce_key="k")
    second = await _enqueue(engine, "test_echo", {"n": 1}, coalesce_key="k")
    assert first == second
    row = await _job(engine, first)
    assert row.due_at < later
    other = await _enqueue(engine, "test_echo", {"n": 1}, coalesce_key="other")
    assert other != first

    async with engine.begin() as conn:
        await conn.execute(delete(jobs))
        await conn.execute(
            text(
                "INSERT INTO jobs (id, kind, payload, due_at) "
                "VALUES ('01JOBUNKNOWNKIND0000000000', 'from_the_future', '{}', now())"
            )
        )
    worker = Worker(engine, _config())
    assert await worker.run_one("w:u:0")
    row = await _job(engine, "01JOBUNKNOWNKIND0000000000")
    assert (row.status, row.result) == ("failed", {"error": "UnknownJobKind"})


async def test_fenced_heartbeat_cancels_the_handler(engine: AsyncEngine, registry: Probe) -> None:
    """The heartbeat renews the lease every third of it; once the claim is fenced (another
    worker took the job) it cancels the handler, and nothing is committed or failed."""
    worker = Worker(engine, _config(lease_s=0.6))
    job_id = await _enqueue(engine, "test_block", {"n": 3})
    claimed = await queue.claim(engine, "w:hb:0", lease_s=0.6)
    assert claimed is not None
    run = asyncio.create_task(worker.execute(claimed))
    await asyncio.wait_for(registry.entered.wait(), 5.0)
    first = (await _job(engine, job_id)).lease_until
    await asyncio.sleep(0.5)
    assert (await _job(engine, job_id)).lease_until > first  # renewed
    async with engine.begin() as conn:
        await conn.execute(update(jobs).where(jobs.c.id == job_id).values(attempt=5))
    assert await asyncio.wait_for(run, 2.0) is None
    row = await _job(engine, job_id)
    assert (row.status, row.failures, row.result) == ("running", 0, None)


async def test_runtime_cap_is_a_failed_attempt(engine: AsyncEngine, registry: Probe) -> None:
    """A handler running past the cap is cancelled and counts as a failed attempt."""
    worker = Worker(engine, _config(max_runtime_s=0.2))
    job_id = await _enqueue(engine, "test_block", {"n": 4})
    assert await worker.run_one("w:cap:0")
    row = await _job(engine, job_id)
    assert (row.status, row.failures, row.result) == ("queued", 1, {"error": "TimeoutError"})


async def test_recovery_hooks_run_in_order(engine: AsyncEngine, registry: Probe) -> None:
    """Registered hooks run after the reclaim, in registration order; a failing hook is
    reported and does not stop the others."""
    calls: list[str] = []

    async def first(conn: Any) -> None:
        calls.append("first")

    async def broken(conn: Any) -> None:
        raise RuntimeError("hook")

    async def last(conn: Any) -> None:
        calls.append("last")

    for name, hook in (("t-first", first), ("t-broken", broken), ("t-last", last)):
        register_recovery_hook(name, hook)
    try:
        report = await recovery_pass(engine)
    finally:
        for name in ("t-first", "t-broken", "t-last"):
            unregister_recovery_hook(name)
    assert calls == ["first", "last"]
    assert report.failed_hooks == ("t-broken",)
    assert report.hooks[-2:] == ("t-first", "t-last")


async def test_large_story_rebuilds_as_a_job(
    engine: AsyncEngine, registry: Probe, settings: Settings
) -> None:
    """Over ``rebuild_async_threshold`` events the rebuild is a ``rebuild_projections`` job
    (asked twice, one job); a worker folds it as a maintenance operation with no revision bump.
    Complement: at or under the threshold it is synchronous."""
    story = await new_story(engine)
    await play_turn(engine, story, "hello")
    revision = await scalar(engine, "SELECT timeline_revision FROM stories WHERE id = :s", s=story)
    events = await scalar(engine, "SELECT count(*) FROM events WHERE story_id = :s", s=story)
    small = Engine(settings.model_copy(update={"rebuild_async_threshold": events}), db=engine)
    synchronous = await small.rebuild_projections(RequestContext(principal="p-op"), story)
    assert not isinstance(synchronous, RebuildQueued)
    library = Engine(settings.model_copy(update={"rebuild_async_threshold": 1}), db=engine)
    queued = await library.rebuild_projections(RequestContext(principal="p-op"), story)
    assert isinstance(queued, RebuildQueued)
    again = await library.rebuild_projections(RequestContext(principal="p-op"), story)
    assert isinstance(again, RebuildQueued)
    assert again.job_id == queued.job_id
    assert await Worker(engine, _config()).run_one("w:rb:0")
    row = await _job(engine, queued.job_id)
    assert row.status == "done"
    assert row.result["counts"]["transcript_messages"] >= 2
    assert row.input_versions == {f"stories:{story}": revision}
    op = (
        await rows(
            engine,
            "SELECT kind, status FROM operations WHERE operation_id = :o",
            o=row.result["operation_id"],
        )
    )[0]
    assert (op.kind, op.status) == ("maintenance", "committed")
    after = await scalar(engine, "SELECT timeline_revision FROM stories WHERE id = :s", s=story)
    assert after == revision


def test_rebuild_route_answers_202(pg_url: str) -> None:
    """``POST /admin/stories/{id}/rebuild`` on a large story is 202 ``{job_id,
    timeline_revision}`` with the echoed key."""
    settings = auth_settings(pg_url, rebuild_async_threshold=1)

    async def setup() -> str:
        from epistrel.core.db import make_engine

        engine = make_engine(settings)
        try:
            story = await new_story(engine)
            await play_turn(engine, story, "hello")
            return story
        finally:
            await engine.dispose()

    story = asyncio.run(setup())
    with TestClient(create_app(settings)) as client:
        answer = client.post(
            f"/admin/stories/{story}/rebuild",
            headers={**bearer(), "X-Epistrel-Principal": "p-op"},
        )
    assert answer.status_code == 202
    body = answer.json()
    assert set(body) == {"job_id", "timeline_revision", "idempotency_key"}
    assert len(body["job_id"]) == 26
    assert body["timeline_revision"] == 2
    assert answer.headers["Idempotency-Key"] == body["idempotency_key"]

    async def cleanup() -> None:
        from epistrel.core.db import make_engine

        engine = make_engine(settings)
        try:
            async with engine.begin() as conn:
                await conn.execute(delete(jobs).where(jobs.c.id == body["job_id"]))
        finally:
            await engine.dispose()

    asyncio.run(cleanup())


async def test_delete_waits_for_a_running_job(
    engine: AsyncEngine, registry: Probe, moved: MovableClock
) -> None:
    """Deleting a story while one of its jobs holds a live lease is 423 ``job_running``; once
    the lease lapsed the delete goes through and takes the story's jobs with it."""
    story = await new_story(engine)
    job_id = await _enqueue(engine, "test_echo", {"n": 1}, story_id=story)
    queued_only = await _enqueue(engine, "test_echo", {"n": 2}, story_id=story)
    assert await queue.claim(engine, "w:del:0", lease_s=LEASE_S) is not None
    with pytest.raises(StoryBusy) as busy:
        await delete_story(engine, story, principal="p-op")
    assert busy.value.details["reason"] == "job_running"
    assert busy.value.details["job_id"] == job_id
    moved.advance(LEASE_S + 1)
    removed = await delete_story(engine, story, principal="p-op")
    assert removed["jobs"] == 2
    assert await _status(engine, job_id) == await _status(engine, queued_only) == "gone"


def test_worker_command_refuses_to_start_without_configuration() -> None:
    """``epistrel worker`` fails fast (78) naming the missing setting; a zero concurrency is a
    usage error."""
    env = {"PATH": "/usr/bin:/bin"}
    proc = subprocess.run(
        [sys.executable, "-m", "epistrel.cli", "worker"],
        capture_output=True,
        text=True,
        timeout=20,
        env=env,
        check=False,
    )
    assert proc.returncode == 78
    assert "EPISTREL_DATABASE_URL" in proc.stderr
    assert "Traceback" not in proc.stderr
    usage = subprocess.run(
        [sys.executable, "-m", "epistrel.cli", "worker", "--concurrency", "0"],
        capture_output=True,
        text=True,
        timeout=20,
        env=env,
        check=False,
    )
    assert usage.returncode == 2


async def test_input_versions_name_what_changed(engine: AsyncEngine) -> None:
    """Every M2 input kind reads back a version; the recheck names exactly the inputs that
    changed or are gone, and an unknown table is refused. Complement: unchanged inputs pass."""
    story = await new_story(engine)
    turn = await play_turn(engine, story, "hello")
    keys = [
        input_key("stories", story),
        input_key("story_texts", story),
        input_key("operations", turn.operation_id),
        input_key("operations", "0" * 26),
    ]
    async with engine.connect() as conn:
        declared = {key: await read_version(conn, key) for key in keys}
        assert declared[keys[3]] is None
        assert await stale_inputs(conn, {k: v for k, v in declared.items() if v}) == ()
        with pytest.raises(UnknownInput):
            check_key("nowhere:1")
        with pytest.raises(UnknownInput):
            await read_version(conn, "stories")
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE story_texts SET custom_instructions = 'changed' WHERE story_id = :s"),
            {"s": story},
        )
    await play_turn(engine, story, "again")
    async with engine.connect() as conn:
        assert await stale_inputs(conn, declared) == tuple(sorted([keys[0], keys[1], keys[3]]))
