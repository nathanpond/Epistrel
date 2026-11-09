"""X-07: chaos — missed wake-ups and worker restarts, timed work and health (docs/04 §12;
NFR-REL-5, NFR-OPS-1,2; #71).

``test_x_07__jobs`` is the jobs half (#70: polling without notifications, a lapsed lease
reclaimed once with fencing), using ``tests/integration/test_jobs.py``'s handlers. The others
are #71's clauses: timers and recurring tasks fire once across racing workers and once after a
long outage, a killed sweep re-run changes nothing, ``/health`` is ``degraded`` without workers
(and in ``api`` mode), the worker runs as its own process, and the compose file starts workers
and no scheduler, cron or broker.

The seeded recurring rows are pushed out of the way and restored per test (``quiet``), so only
what a test sets up is due. Time moves with the injectable clock (``core.clock``) where the code
uses it; ``/health`` uses the database's ``now()``, so its tests age rows in SQL.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
from collections import deque
from collections.abc import AsyncIterator, Iterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, cast

import httpx2 as httpx
import pytest
import yaml
from pydantic import BaseModel, ConfigDict
from sqlalchemy import delete, insert, select, text, update
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.api import create_app
from epistrel.api.routes.health import check
from epistrel.config import Settings
from epistrel.core import clock, states
from epistrel.core import outbox as core_outbox
from epistrel.core.db import make_engine
from epistrel.core.deployment import apply_settings
from epistrel.core.enums import (
    OperationKind,
    Provenance,
    StateChangeStatus,
    StateContingency,
)
from epistrel.core.events.operation import Operation
from epistrel.core.ids import new_ulid
from epistrel.core.media import sweep as media_sweep
from epistrel.core.media.store import LocalFilesystemStore
from epistrel.core.profiles import certification
from epistrel.core.profiles.certification import (
    CertificationRecord,
    FloorResult,
    HarnessOutcome,
    HarnessRequest,
    RunEstimate,
)
from epistrel.core.seeds.profiles import FULL_PRESET_ID
from epistrel.core.settings_schema import CERTIFICATION_CANARY_TASK, DEFAULT_CANARY_INTERVAL_S
from epistrel.core.tables import audit_log, idempotency_keys, jobs, recurring_tasks, timers
from epistrel.core.tables import worker_heartbeats as heartbeats
from epistrel.engine import TURN_QUEUE_HOOK, Engine
from epistrel.orchestrator.jobs import handlers, queue
from epistrel.orchestrator.jobs import timers as timer_api
from epistrel.orchestrator.jobs.handlers import media_sweep as media_sweep_job
from epistrel.orchestrator.jobs.handlers import outbox as outbox_job
from epistrel.orchestrator.jobs.handlers import reservations, retention, turn_queue
from epistrel.orchestrator.jobs.recurring import (
    NoPayload,
    RecurringTask,
    recording,
    register_recurring,
    registered_tasks,
    unregister_recurring,
)
from epistrel.orchestrator.jobs.types import JobContext, JobHandler, JobOutput
from epistrel.orchestrator.jobs.worker import (
    _RECOVERY_HOOKS,
    Worker,
    WorkerConfig,
    recovery_pass,
)
from epistrel.orchestrator.providers.in_process import InProcessRegistry
from tests import canon_support as c
from tests import provider_support as ps
from tests import state_support as ss
from tests.auth_support import CREDENTIAL_A, ERASURE_LEDGER_DIR, MEDIA_ROOT, auth_settings
from tests.conformance.conftest import migrated_database
from tests.conftest import REPO_ROOT
from tests.event_support import new_story, play_turn
from tests.integration import test_jobs as jt
from tests.integration.test_media_assets import SWEEPER, put
from tests.loop_support import LoopLog
from tests.media_support import blob_path, png, scratch
from tests.memory_support import use_structured
from tests.respond_support import until
from tests.wall_clock_support import MovableClock, moving

POLL_S = 0.3
LEASE_S = 3.0
TICK = "test_tick"
FLAKY = "test_flaky"
TIMER = "test_timer"
INTERVAL = timedelta(minutes=1)


class Target(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target: str


class Probe:
    def __init__(self) -> None:
        self.ran: list[tuple[str, str]] = []


PROBE = Probe()


async def _timer(ctx: JobContext) -> JobOutput:
    PROBE.ran.append((TIMER, cast(Target, ctx.payload).target))
    return JobOutput(result={"ok": True})


async def _tick(ctx: JobContext) -> JobOutput:
    PROBE.ran.append((TICK, ctx.job.id))
    return JobOutput(result={})


async def _flaky(ctx: JobContext) -> JobOutput:
    PROBE.ran.append((FLAKY, ctx.job.id))
    raise RuntimeError("never logged as text")


TEST_HANDLERS = (
    JobHandler(kind=TIMER, payload=Target, run=_timer, records_operation=False),
    JobHandler(
        kind=TICK, payload=NoPayload, run=recording(_tick), max_attempts=1, records_operation=False
    ),
    JobHandler(
        kind=FLAKY,
        payload=NoPayload,
        run=recording(_flaky),
        max_attempts=1,
        records_operation=False,
    ),
)


async def _execute(engine: AsyncEngine, sql: str, **params: Any) -> None:
    async with engine.begin() as conn:
        await conn.execute(text(sql), params)


async def _scalar(engine: AsyncEngine, sql: str, **params: Any) -> Any:
    async with engine.connect() as conn:
        return await conn.scalar(text(sql), params)


async def _clean(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        for table in (jobs, timers, heartbeats):
            await conn.execute(delete(table))
        await conn.execute(
            delete(recurring_tasks).where(recurring_tasks.c.name.in_([TICK, FLAKY, "test_orphan"]))
        )


@pytest.fixture
async def quiet(engine: AsyncEngine) -> AsyncIterator[Probe]:
    """Test handlers registered; jobs, timers and heartbeats empty; every registered recurring
    task's row due far in the future. Everything is put back afterwards."""
    async with _quieted(engine) as probe:
        yield probe


@contextlib.asynccontextmanager
async def _quieted(engine: AsyncEngine) -> AsyncIterator[Probe]:
    """``quiet``'s body. It reconciles first, so a task registered since the rows were last
    written (every task, on a fresh database) has a row to push out; otherwise the test's own
    ``reconcile`` would create it due now (#318)."""
    global PROBE
    PROBE = Probe()
    for handler in TEST_HANDLERS:
        handlers.register(handler)
    await timer_api.reconcile(engine)
    async with engine.connect() as conn:
        saved = [dict(r._mapping) for r in (await conn.execute(select(recurring_tasks))).all()]
    await _clean(engine)
    await _execute(engine, "UPDATE recurring_tasks SET next_run_at = now() + interval '30 days'")
    try:
        yield PROBE
    finally:
        for handler in TEST_HANDLERS:
            handlers.unregister(handler.kind)
        for name in (TICK, FLAKY):
            unregister_recurring(name)
        await _clean(engine)
        async with engine.begin() as conn:
            for row in saved:
                await conn.execute(
                    update(recurring_tasks)
                    .where(recurring_tasks.c.name == row["name"])
                    .values(**{k: v for k, v in row.items() if k != "name"})
                )


def _now() -> datetime:
    return clock.now()


@pytest.fixture
def moved() -> Iterator[MovableClock]:
    with moving() as movable:
        yield movable


def _config(**kw: Any) -> WorkerConfig:
    base = WorkerConfig(poll_interval=POLL_S, lease_s=LEASE_S, max_attempts=5, listen=False)
    return replace(base, **{"concurrency": 1, **kw})


async def _wait_for(probe: Any, within: float = 5.0) -> None:
    deadline = time.monotonic() + within
    while not await probe():
        assert time.monotonic() < deadline, "condition not reached in time"
        await asyncio.sleep(0.02)


async def _jobs_of(engine: AsyncEngine, kind: str) -> list[Any]:
    async with engine.connect() as conn:
        return list(
            (await conn.execute(select(jobs).where(jobs.c.kind == kind).order_by(jobs.c.id))).all()
        )


async def _task(engine: AsyncEngine, name: str) -> Any:
    async with engine.connect() as conn:
        return (
            await conn.execute(select(recurring_tasks).where(recurring_tasks.c.name == name))
        ).one_or_none()


async def _drain(engine: AsyncEngine, worker: Worker) -> int:
    """Run every due job to its end; returns how many ran."""
    ran = 0
    while await worker.run_one("x07:drain:0"):
        ran += 1
    return ran


# -- X-07, jobs half (#70; moved from tests/integration/test_jobs.py) --------------------


@pytest.fixture
async def registry(engine: AsyncEngine) -> AsyncIterator[jt.Probe]:
    """``tests/integration/test_jobs.py``'s handlers on an empty ``jobs`` table (its own
    ``registry`` fixture, for the test that moved here), with every recurring row due far in
    the future so a recovery pass enqueues nothing of its own (a task registered since the
    rows were last reconciled, like #72's ``lite_summary``, would otherwise be created due
    now). Everything is put back afterwards."""
    jt.PROBE = jt.Probe()
    for handler in jt.TEST_HANDLERS:
        handlers.register(handler)
    await timer_api.reconcile(engine)
    async with engine.connect() as conn:
        saved = [dict(r._mapping) for r in (await conn.execute(select(recurring_tasks))).all()]
    await _execute(engine, "DELETE FROM jobs")
    await _execute(engine, "UPDATE recurring_tasks SET next_run_at = now() + interval '30 days'")
    try:
        yield jt.PROBE
    finally:
        for handler in jt.TEST_HANDLERS:
            handlers.unregister(handler.kind)
        await _execute(engine, "DELETE FROM jobs")
        async with engine.begin() as conn:
            for row in saved:
                await conn.execute(
                    update(recurring_tasks)
                    .where(recurring_tasks.c.name == row["name"])
                    .values(**{k: v for k, v in row.items() if k != "name"})
                )


@pytest.mark.chaos
@pytest.mark.chaos_quick
@pytest.mark.part("M2", "NFR-REL-5")
async def test_x_07__jobs(engine: AsyncEngine, registry: jt.Probe, moved: MovableClock) -> None:
    """Notifications suppressed, a job is still claimed within one poll interval. A worker
    killed mid-job leaves its lease; once it lapses the next worker's recovery pass reclaims it
    exactly once with fencing, and the job then finishes; a second recovery pass changes
    nothing. The lost claim's late commit is refused and writes nothing. Complement: the
    current claim's commit is accepted."""
    # 1. NOTIFY suppressed: polling alone claims the job within one interval. The loop's own
    # record shows it, not elapsed time (#446): the first poll to start after the enqueue
    # claims it (at most one poll, under way at the enqueue, misses it), and no idle wait from
    # the one the loop was in at the enqueue up to the claim is longer than the interval.
    worker = Worker(engine, jt._config())
    with pytest.MonkeyPatch.context() as patch:
        log = LoopLog(worker, patch)
        running = asyncio.create_task(worker.run())
        await until(lambda: log.first("wait") is not None, "the loop is idle between polls")
        assert not worker.listening.is_set()
        mark = len(log.entries)
        polled = await jt._enqueue(engine, "test_echo", {"n": 1})
        await until(lambda: log.first("poll", True, after=mark) is not None, "a poll claims it")
        claim = log.first("poll", True, after=mark)
        assert claim is not None
        missed = [e for e in log.entries[mark:claim] if e[0] == "poll"]
        assert len(missed) <= 1, log.entries[mark : claim + 1]
        log.assert_waits_within_interval(POLL_S, log.last("wait", mark), claim, most=2)
        assert await jt._done(engine, polled)
        worker.stop()
        await running

    # 2. Kill a worker mid-job; the lease lapses; a new worker's recovery pass reclaims it once.
    killed = Worker(engine, jt._config())
    running = asyncio.create_task(killed.run())
    blocked = await jt._enqueue(engine, "test_block", {"n": 2})
    await asyncio.wait_for(registry.entered.wait(), 5.0)
    running.cancel()  # the kill
    with pytest.raises(asyncio.CancelledError):
        await running
    row = await jt._job(engine, blocked)
    assert (row.status, row.attempt) == ("running", 1)
    moved.advance(jt.LEASE_S + 1)
    registry.gate.set()
    revived = Worker(engine, jt._config())
    report = await revived.recovery_pass()
    assert report is not None
    assert report.reclaimed == (blocked,)
    assert await revived.recovery_pass() is None  # once per worker process
    again = await recovery_pass(engine)  # and idempotent when run again anyway
    assert again.reclaimed == ()
    assert await revived.run_one(revived.worker_id(0))
    row = await jt._job(engine, blocked)
    assert (row.status, row.attempt, row.failures) == ("done", 3, 0)
    assert row.result["n"] == 2
    assert [attempt for job, attempt in registry.started if job == blocked] == [1, 3]

    # 3. Fencing: a lost claim's late commit is refused; the current claim's is accepted.
    story = await new_story(engine)
    await play_turn(engine, story, "first")
    fenced = await jt._enqueue(engine, "test_summarise", {"story_id": story}, story_id=story)
    lost = await queue.claim(engine, "w:lost:0", lease_s=jt.LEASE_S)
    assert lost is not None
    assert lost.id == fenced

    def late(op: Operation) -> None:
        op.append("lite_summary_segment", {"text": "late", "covers_from": 1, "covers_to": 1})

    output = JobOutput(stage=late)
    moved.advance(jt.LEASE_S + 1)
    before = await jt._table(engine)
    assert (await recovery_pass(engine)).reclaimed == (fenced,)
    after = await jt._table(engine)
    assert (await recovery_pass(engine)).reclaimed == ()
    assert await jt._table(engine) == after != before  # the second pass changed nothing
    current = await queue.claim(engine, "w:new:0", lease_s=jt.LEASE_S)
    assert current is not None
    assert current.attempt == lost.attempt + 2
    summarise = handlers.lookup("test_summarise")
    assert summarise is not None
    refused = await queue.commit_job(engine, lost, summarise, output, {})
    assert refused is queue.CommitOutcome.REFUSED
    assert not await queue.heartbeat(engine, lost, lease_s=jt.LEASE_S)
    assert await jt._segments(engine, story) == []
    accepted = await queue.commit_job(engine, current, summarise, output, {})
    assert accepted is queue.CommitOutcome.COMMITTED
    assert await jt._segments(engine, story) == ["late"]


# -- timers ------------------------------------------------------------------------------------


@pytest.mark.chaos
@pytest.mark.chaos_quick
@pytest.mark.part("M2", "NFR-REL-5")
async def test_x_07__timers(engine: AsyncEngine, quiet: Probe) -> None:
    """A due timer is fired by a polling worker with no notification, within one interval of
    falling due; ten due timers raced by four workers each fire exactly once; ``schedule`` moves
    a timer and ``cancel`` removes it; a timer of an unknown kind is deleted unfired.
    Complement: a timer not yet due is not fired."""
    worker = Worker(engine, _config())
    with pytest.MonkeyPatch.context() as patch:
        log = LoopLog(worker, patch)
        run = asyncio.create_task(worker.run())
        try:
            # Past the recovery pass and into the loop, so a loop pass fires it (#446).
            await until(lambda: log.first("wait") is not None, "the loop is idle between polls")
            due = _now() + timedelta(seconds=0.5)
            async with engine.begin() as conn:
                await timer_api.schedule(conn, TIMER, "polled", due)
            await until(lambda: _ran(quiet, "polled"), "a polling worker fires the due timer")
        finally:
            worker.stop()
            await run
    # The loop's record, not elapsed time: the first pass that began once the timer was due
    # fired it (every earlier pass began before it was due), after at most one idle wait of
    # at most the interval since the pass before.
    passes = [i for i, e in enumerate(log.entries) if e[0] == "fire"]
    firing = [i for i in passes if TIMER in log.entries[i][2]]
    assert len(firing) == 1, log.entries
    (fired,) = firing
    earlier = [i for i in passes if i < fired]
    assert earlier, log.entries  # the recovery pass and a loop pass ran before it was scheduled
    assert all(log.entries[i][1] < due for i in earlier), log.entries
    log.assert_waits_within_interval(POLL_S, earlier[-1], fired, most=1)
    assert quiet.ran.count((TIMER, "polled")) == 1
    assert await _scalar(engine, "SELECT count(*) FROM timers") == 0

    # Two workers (here four firers) race for ten due timers: each fires once.
    now = _now()
    async with engine.begin() as conn:
        for n in range(10):
            await timer_api.schedule(conn, TIMER, f"race-{n}", now - timedelta(seconds=1))
        await timer_api.schedule(conn, TIMER, "later", now + timedelta(hours=1))
    results = await asyncio.gather(*(timer_api.fire_due(engine) for _ in range(4)))
    assert sorted(kind for r in results for kind in r.timers) == [TIMER] * 10
    fired_jobs = await _jobs_of(engine, TIMER)
    assert sorted(j.payload["target"] for j in fired_jobs if j.status == "queued") == sorted(
        f"race-{n}" for n in range(10)
    )
    assert await _scalar(engine, "SELECT target FROM timers") == "later"

    # schedule upserts (moves the due time); cancel removes; cancelling again finds nothing.
    async with engine.begin() as conn:
        first = await timer_api.schedule(conn, TIMER, "later", now + timedelta(hours=2))
        assert await _scalar(engine, "SELECT count(*) FROM timers") == 1
        assert first
        assert await timer_api.cancel(conn, TIMER, "later") is True
        assert await timer_api.cancel(conn, TIMER, "later") is False

    # Unknown kind, and a target its handler refuses: deleted, never enqueued.
    async with engine.begin() as conn:
        await timer_api.schedule(conn, "no_such_kind", "x", now - timedelta(seconds=1))
        await timer_api.schedule(conn, "rebuild_projections", "x", now - timedelta(seconds=1))
    dropped = await timer_api.fire_due(engine)
    assert sorted(dropped.dropped) == ["no_such_kind", "rebuild_projections"]
    assert dropped.timers == []
    assert await _scalar(engine, "SELECT count(*) FROM timers") == 0


async def _ran(probe: Probe, target: str) -> bool:
    return (TIMER, target) in probe.ran


# -- recurring tasks ---------------------------------------------------------------------------


@pytest.mark.chaos
@pytest.mark.chaos_quick
@pytest.mark.part("M2", "NFR-REL-5")
async def test_x_07__quiet(engine: AsyncEngine) -> None:
    """Regression (#318): on a database where no registered recurring task has a row yet (a
    fresh one), ``quiet`` still leaves none of them due, so a later ``reconcile`` and firing
    fire nothing. Complement: the rows exist again afterwards, as before."""
    registered = list(registered_tasks())
    assert registered  # the shipped tasks are registered
    async with engine.connect() as conn:
        before = [dict(r._mapping) for r in (await conn.execute(select(recurring_tasks))).all()]
    try:
        await _execute(
            engine, "DELETE FROM recurring_tasks WHERE name = ANY(:names)", names=registered
        )
        async with _quieted(engine):
            await timer_api.reconcile(engine)
            assert (await timer_api.fire_due(engine)).recurring == []
    finally:
        async with engine.begin() as conn:
            await conn.execute(delete(recurring_tasks))
            if before:
                await conn.execute(insert(recurring_tasks), before)
    async with engine.connect() as conn:
        names = set((await conn.scalars(select(recurring_tasks.c.name))).all())
    assert {row["name"] for row in before} == names


@pytest.mark.chaos
@pytest.mark.chaos_quick
@pytest.mark.part("M2", "NFR-REL-5")
async def test_x_07__recurring(engine: AsyncEngine, quiet: Probe, moved: MovableClock) -> None:
    """A task that missed three intervals runs once, and its next run is one interval after
    it ran; workers racing for it fire it once; ``last_status`` records ``ok``, or ``failed``
    with the exception class after one attempt (the failed task waits for its next interval);
    a row no task is registered for is never fired. Complement: once it ran, the task is not
    due again until the interval has passed."""
    register_recurring(RecurringTask(TICK, INTERVAL))
    register_recurring(RecurringTask(FLAKY, INTERVAL))
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO recurring_tasks (name, interval, next_run_at) "
                "VALUES ('test_orphan', interval '1 minute', now() - interval '1 day')"
            )
        )
    assert await timer_api.reconcile(engine) == ["test_orphan"]
    missed = _now() - 3 * INTERVAL - timedelta(seconds=5)
    async with engine.begin() as conn:
        await conn.execute(
            update(recurring_tasks)
            .where(recurring_tasks.c.name.in_([TICK, FLAKY]))
            .values(next_run_at=missed)
        )

    results = await asyncio.gather(*(timer_api.fire_due(engine) for _ in range(4)))
    fired = sorted(name for r in results for name in r.recurring)
    assert fired == [FLAKY, TICK]  # each once, the orphan never
    again = await timer_api.fire_due(engine)
    assert again.recurring == []
    tick = await _task(engine, TICK)
    assert tick.next_run_at - tick.last_run_at == INTERVAL
    assert tick.next_run_at > _now()

    worker = Worker(engine, _config(timers=False))
    assert await _drain(engine, worker) == 2
    assert [k for k, _ in quiet.ran].count(TICK) == 1
    assert [k for k, _ in quiet.ran].count(FLAKY) == 1
    tick, flaky = await _task(engine, TICK), await _task(engine, FLAKY)
    assert (tick.last_status, tick.last_error) == ("ok", None)
    assert (flaky.last_status, flaky.last_error) == ("failed", "RuntimeError")
    (flaky_job,) = await _jobs_of(engine, FLAKY)
    assert (flaky_job.status, flaky_job.failures) == ("failed", 1)
    orphan = await _task(engine, "test_orphan")
    assert orphan.last_run_at is None

    # The next interval: both run again, once each.
    moved.advance(INTERVAL.total_seconds() + 1)
    assert sorted((await timer_api.fire_due(engine)).recurring) == [FLAKY, TICK]
    assert await _drain(engine, worker) == 2
    assert [k for k, _ in quiet.ran].count(TICK) == 2

    # Reconcile writes a changed interval and leaves next_run_at alone.
    before = await _task(engine, TICK)
    register_recurring(RecurringTask(TICK, timedelta(minutes=5)))
    await timer_api.reconcile(engine)
    after = await _task(engine, TICK)
    assert (after.interval, after.next_run_at) == (timedelta(minutes=5), before.next_run_at)


# -- restart: lapsed lease and result window, recovery once --------------------------------------


@pytest.mark.chaos
@pytest.mark.chaos_quick
@pytest.mark.part("M2", "NFR-REL-5")
async def test_x_07__restart(engine: AsyncEngine, quiet: Probe, moved: MovableClock) -> None:
    """Every worker is killed while a job holds a lease; while none runs, the job's lease and
    an idempotency result's window lapse and the hourly purge misses three runs. The next
    worker's recovery pass reclaims the job (fenced) and fires the purge, each exactly once;
    the purge then drops the result and keeps the tombstone. A second recovery pass changes
    nothing. Complement: a result whose window is still open is kept."""
    job_id = await _enqueue(engine, TIMER, {"target": "held"})
    claim = await queue.claim(engine, "x07:dead:0", lease_s=LEASE_S)
    assert claim is not None
    assert claim.id == job_id
    now = _now()
    keys = {"expired": now + timedelta(minutes=30), "fresh": now + timedelta(hours=26)}
    async with engine.begin() as conn:
        for key, expires in keys.items():
            await conn.execute(
                idempotency_keys.insert().values(
                    scope="deployment",
                    scope_id="deployment",
                    key=f"x07-{key}-{job_id}",
                    request_hash="h",
                    status="done",
                    result={"status": 200, "body": {}},
                    owner_token="t",
                    lease_until=now,
                    expires_at=expires,
                )
            )
        await conn.execute(
            update(recurring_tasks)
            .where(recurring_tasks.c.name == retention.IDEMPOTENCY_PURGE)
            .values(next_run_at=now + timedelta(minutes=1))
        )
    # The outage: four hours pass with no worker.
    moved.advance(4 * 3600)

    worker = Worker(engine, _config())
    report = await worker.recovery_pass()
    assert report is not None
    assert report.reclaimed == (job_id,)
    assert retention.IDEMPOTENCY_PURGE in report.fired
    assert report.fired.count(retention.IDEMPOTENCY_PURGE) == 1
    assert await worker.recovery_pass() is None  # once per worker
    second = await recovery_pass(engine)  # and a second pass by anyone changes nothing
    assert (second.reclaimed, second.fired) == ((), ())

    assert await _drain(engine, worker) == 2
    (purge,) = await _jobs_of(engine, retention.IDEMPOTENCY_PURGE)
    assert purge.status == "done"
    assert purge.result["results_purged"] >= 1
    (held,) = await _jobs_of(engine, TIMER)
    assert (held.status, held.attempt) == ("done", claim.attempt + 2)
    async with engine.connect() as conn:
        left = {
            r.key: r.result
            for r in (
                await conn.execute(
                    select(idempotency_keys).where(idempotency_keys.c.key.like(f"x07-%-{job_id}"))
                )
            ).all()
        }
    assert left == {
        f"x07-expired-{job_id}": None,
        f"x07-fresh-{job_id}": {"status": 200, "body": {}},
    }
    async with engine.begin() as conn:
        await conn.execute(delete(idempotency_keys).where(idempotency_keys.c.key.in_(list(left))))


async def _enqueue(engine: AsyncEngine, kind: str, payload: dict[str, Any]) -> str:
    async with engine.begin() as conn:
        return await queue.enqueue(conn, kind, payload)


# -- a sweep killed mid-run, then run again ----------------------------------------------------


@pytest.mark.chaos
@pytest.mark.chaos_quick
@pytest.mark.part("M2", "NFR-REL-5")
async def test_x_07__sweep_rerun(
    engine: AsyncEngine, quiet: Probe, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A retention sweep is killed in the middle of its second chunk: the first chunk's deletes
    stand, the second rolls back. Run again, it deletes the rest, leaving exactly what one
    uninterrupted run leaves; a third run changes nothing. Complement: records inside the
    window are kept by every run."""
    monkeypatch.setattr(retention, "CHUNK", 10)
    old, fresh = await _audit_rows(engine, old=25, fresh=5)
    calls = 0
    blocked = asyncio.Event()
    original = retention._allow_audit_expiry

    async def slow(conn: Any) -> None:
        nonlocal calls
        calls += 1
        await original(conn)
        # Chunk 1 is the (empty) audit events, 2 and 3 the audit_log chunks.
        if calls == 3:
            blocked.set()
            await asyncio.Event().wait()  # killed here, inside audit_log's second chunk

    monkeypatch.setattr(retention, "_allow_audit_expiry", slow)
    sweep = asyncio.create_task(retention.sweep_retention(engine))
    await asyncio.wait_for(blocked.wait(), 5)
    sweep.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await sweep
    assert len(await _present(engine, old)) == 15  # one chunk of 10 committed
    monkeypatch.setattr(retention, "_allow_audit_expiry", original)

    first = await retention.sweep_retention(engine)
    assert first["audit_log"] == 15
    assert await _present(engine, old) == set()
    assert await _present(engine, fresh) == fresh
    again = await retention.sweep_retention(engine)
    assert again["audit_log"] == 0
    assert await _present(engine, fresh) == fresh
    async with engine.begin() as conn:
        await conn.execute(text("SELECT set_config('epistrel.allow_audit_expiry', 'on', true)"))
        await conn.execute(delete(audit_log).where(audit_log.c.id.in_(list(fresh))))


async def _audit_rows(engine: AsyncEngine, *, old: int, fresh: int) -> tuple[set[str], set[str]]:
    from epistrel.core.ids import new_ulid

    made: dict[str, set[str]] = {"old": set(), "fresh": set()}
    async with engine.begin() as conn:
        for label, count, age in (("old", old, 91), ("fresh", fresh, 89)):
            for _ in range(count):
                row_id = new_ulid()
                await conn.execute(
                    audit_log.insert().values(
                        id=row_id,
                        at=datetime.now(UTC) - timedelta(days=age),
                        principal="p-x07",
                        kind="story_deleted",
                    )
                )
                made[label].add(row_id)
    return made["old"], made["fresh"]


async def _present(engine: AsyncEngine, ids: set[str]) -> set[str]:
    async with engine.connect() as conn:
        return set(
            (await conn.scalars(select(audit_log.c.id).where(audit_log.c.id.in_(list(ids))))).all()
        )


# -- health --------------------------------------------------------------------------------------


async def _health(settings: Settings, engine: AsyncEngine) -> tuple[int, dict[str, Any]]:
    app = create_app(settings)
    app.state.engine = engine
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/health")
    return response.status_code, response.json()


@pytest.mark.chaos
@pytest.mark.chaos_quick
@pytest.mark.part("M2", "NFR-REL-5; NFR-OPS-1")
async def test_x_07__health(engine: AsyncEngine, settings: Settings, quiet: Probe) -> None:
    """With no worker, ``/health`` is ``degraded`` (200) with no heartbeat and the oldest
    overdue timer's age; an ``api``-mode deployment reports the same. A live worker with
    nothing overdue is ``ok``; a heartbeat older than ``worker_heartbeat_timeout`` is not
    alive; an overdue recurring task or queued job past ``timer_lag_alert`` is ``degraded``,
    and a running (leased) job is not overdue. Complement: overdue work younger than the lag
    keeps it ``ok``."""
    assert settings.mode == "api"
    async with engine.begin() as conn:
        await timer_api.schedule(conn, TIMER, "overdue", _now())
    await _execute(engine, "UPDATE timers SET due_at = now() - interval '400 seconds'")
    status, body = await _health(settings, engine)
    assert status == 200
    assert body["status"] == "degraded"
    assert body["checks"]["database"] == "ok"
    assert body["checks"]["workers"] == {"alive": 0, "last_heartbeat_s": None}
    assert body["checks"]["oldest_overdue_timer_s"] >= 400
    assert body["checks"]["oldest_overdue_job_s"] is None

    # A live worker, nothing overdue: ok.
    await _execute(engine, "DELETE FROM timers")
    worker = Worker(engine, _config(concurrency=2, mode="all"))
    await worker.beat()
    status, body = await _health(settings, engine)
    assert (status, body["status"]) == (200, "ok")
    assert body["checks"]["workers"]["alive"] == 2
    assert 0 <= body["checks"]["workers"]["last_heartbeat_s"] < 5

    # Overdue but within the lag: still ok (the complement of the lag rule).
    await _execute(
        engine,
        "UPDATE recurring_tasks SET next_run_at = now() - interval '100 seconds' "
        "WHERE name = 'retention_sweep'",
    )
    _, body = await _health(settings, engine)
    assert body["status"] == "ok"
    assert 100 <= body["checks"]["oldest_overdue_timer_s"] < 200
    # A registered recurring task overdue past the lag: degraded.
    await _execute(
        engine,
        "UPDATE recurring_tasks SET next_run_at = now() - interval '400 seconds' "
        "WHERE name = 'retention_sweep'",
    )
    _, body = await _health(settings, engine)
    assert body["status"] == "degraded"
    await _execute(
        engine,
        "UPDATE recurring_tasks SET next_run_at = now() + interval '1 day' "
        "WHERE name = 'retention_sweep'",
    )

    # A queued job overdue past the lag: degraded; once a worker holds it, it is not overdue.
    job_id = await _enqueue(engine, TIMER, {"target": "late"})
    await _execute(
        engine, "UPDATE jobs SET due_at = now() - interval '400 seconds' WHERE id = :j", j=job_id
    )
    _, body = await _health(settings, engine)
    assert body["status"] == "degraded"
    assert body["checks"]["oldest_overdue_job_s"] >= 400
    assert await queue.claim(engine, "x07:h:0", lease_s=60) is not None
    _, body = await _health(settings, engine)
    assert (body["status"], body["checks"]["oldest_overdue_job_s"]) == ("ok", None)

    # The heartbeats lapse: no worker is alive any more.
    await _execute(engine, "UPDATE worker_heartbeats SET at = now() - interval '61 seconds'")
    _, body = await _health(settings, engine)
    assert body["status"] == "degraded"
    assert body["checks"]["workers"]["alive"] == 0
    assert body["checks"]["workers"]["last_heartbeat_s"] >= 61

    # Pure probe (no HTTP): the same answer.
    assert (await check(engine)).status == "degraded"


@pytest.mark.chaos
@pytest.mark.chaos_quick
@pytest.mark.part("M2", "NFR-REL-5; NFR-OPS-1")
async def test_x_07__all_mode(pg_url: str, engine: AsyncEngine, quiet: Probe) -> None:
    """``all`` mode runs the worker in the server process: once started, ``/health`` counts its
    ``worker_concurrency`` loops alive and the turn-queue drain is registered as its recovery
    hook and bound to the job; on shutdown it removes its heartbeats. ``worker`` mode serves
    ``/health`` and none of the API routes. Complement: ``api`` mode starts no worker."""
    settings = auth_settings(pg_url, mode="all", worker_poll_interval=0.2, worker_concurrency=3)
    app = create_app(settings)
    transport = httpx.ASGITransport(app=app)
    lib: Engine = app.state.epistrel
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=transport, base_url="http://test") as client,
    ):
        assert lib.worker is not None
        assert turn_queue.bound() == lib.drainer.recover

        async def alive() -> bool:
            return bool((await client.get("/health")).json()["checks"]["workers"]["alive"] == 3)

        await _wait_for(alive, within=5)
        response = await client.get("/health")
        assert response.json()["status"] == "ok"
        assert (await client.get("/stories")).status_code != 404  # the API is served too
    assert lib.worker is None
    assert turn_queue.bound() is None
    assert await _scalar(engine, "SELECT count(*) FROM worker_heartbeats") == 0

    worker_app = create_app(auth_settings(pg_url, mode="worker"))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=worker_app), base_url="http://test"
    ) as client:
        assert (await client.get("/health")).status_code == 200
        assert (await client.get("/stories", headers=_bearer())).status_code == 404

    api = create_app(auth_settings(pg_url))
    async with api.router.lifespan_context(api):
        assert api.state.epistrel.worker is None


def _bearer() -> dict[str, str]:
    return {"Authorization": f"Bearer {CREDENTIAL_A}"}


@pytest.mark.chaos
@pytest.mark.chaos_quick
@pytest.mark.part("M2", "NFR-REL-5")
async def test_x_07__turn_queue(engine: AsyncEngine, settings: Settings, quiet: Probe) -> None:
    """A worker-mode process drains turn queues: its recovery pass runs the Engine's drain
    (the ``turn_queue`` hook), and every ``turn_queue_drain`` run kicks it again. Complement: a
    process with no drainer bound runs the task as a no-op."""
    await _execute(
        engine,
        "UPDATE recurring_tasks SET next_run_at = now() - interval '1 minute' "
        "WHERE name = 'turn_queue_drain'",
    )
    lib = Engine(replace_mode(settings, "worker"), db=engine)
    kicks = 0

    async def counted() -> None:
        nonlocal kicks
        kicks += 1

    lib.drainer.recover = counted  # type: ignore[method-assign]
    await lib.start_workers()
    try:
        assert lib.worker is not None
        await _wait_for(lambda: _at_least(lambda: kicks, 1))
        await _wait_for(lambda: _task_ran(engine, turn_queue.TURN_QUEUE_DRAIN), within=5)
        await _wait_for(lambda: _at_least(lambda: kicks, 2))
    finally:
        await lib.stop_workers()
    assert TURN_QUEUE_HOOK not in _RECOVERY_HOOKS
    assert turn_queue.bound() is None
    async with engine.begin() as conn:
        await queue.enqueue(conn, turn_queue.TURN_QUEUE_DRAIN, {})
    worker = Worker(engine, _config(timers=False))
    assert await _drain(engine, worker) >= 1
    unbound = [j for j in await _jobs_of(engine, turn_queue.TURN_QUEUE_DRAIN) if j.result]
    assert {"drainer": False} in [j.result for j in unbound]


def replace_mode(settings: Settings, mode: str) -> Settings:
    return settings.model_copy(update={"mode": mode, "worker_poll_interval": 0.2})


async def _at_least(value: Any, n: int) -> bool:
    return bool(value() >= n)


async def _task_ran(engine: AsyncEngine, name: str) -> bool:
    task = await _task(engine, name)
    return task is not None and task.last_status == "ok"


# -- the worker as its own process ---------------------------------------------------------------


def _free_port() -> int:
    """A port free a moment ago: ``_workers`` starts a worker on it again elsewhere if someone
    took it before the worker bound it (#468)."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


BOUND = re.compile(r"Uvicorn running on http://127\.0\.0\.1:(\d+)")
"""uvicorn's INFO line once it bound its port (the CLI takes no port 0 to report one)."""
IN_USE = "address already in use"
"""uvicorn's ERROR line when its port was taken: it exits."""
WORKER_ATTEMPTS = 3
WORKER_START_S = 30.0


class _Worker:
    """An ``epistrel worker`` process on ``port``; a thread drains its output (its JSON log lines
    on stdout at INFO, and stderr), keeping the last lines and noting whether it bound its port
    or found it taken."""

    def __init__(self, pg_url: str, port: int) -> None:
        self.port = port
        self.proc = _spawn(pg_url, port)
        self.lines: deque[str] = deque(maxlen=200)
        self.bound = threading.Event()
        self.in_use = False
        self.drain = threading.Thread(target=self._drain, daemon=True)
        self.drain.start()

    def _drain(self) -> None:
        assert self.proc.stdout is not None
        for line in self.proc.stdout:
            self.lines.append(line)
            found = BOUND.search(line)
            if found and int(found.group(1)) == self.port:
                self.bound.set()
            elif IN_USE in line.lower():
                self.in_use = True

    def output(self) -> str:
        return "".join(self.lines)


async def _workers(pg_url: str, n: int) -> list[_Worker]:
    """``n`` workers started together, each on a port it bound itself: one whose port was taken
    between ``_free_port`` and its bind is started again on a fresh port (``WORKER_ATTEMPTS`` at
    most, #468). Until a worker has bound, nothing talks to its port."""
    workers = [_Worker(pg_url, _free_port()) for _ in range(n)]
    started: list[_Worker] = []
    try:
        for slot in range(n):
            for attempt in range(1, WORKER_ATTEMPTS + 1):
                worker = workers[slot]
                deadline = time.monotonic() + WORKER_START_S
                while not worker.bound.is_set() and worker.proc.poll() is None:
                    assert time.monotonic() < deadline, f"worker not bound: {worker.output()}"
                    await asyncio.sleep(0.05)
                if worker.bound.is_set():
                    break
                worker.proc.wait(timeout=10)
                worker.drain.join(timeout=5)  # it has read the last lines
                assert worker.in_use, worker.output()
                assert attempt < WORKER_ATTEMPTS, worker.output()
                workers[slot] = _Worker(pg_url, _free_port())
            started.append(workers[slot])
    except BaseException:
        for worker in workers:
            worker.proc.kill()
            worker.proc.wait(timeout=10)
        raise
    return started


def _spawn(pg_url: str, port: int) -> subprocess.Popen[str]:
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "EPISTREL_DATABASE_URL": pg_url,
        "EPISTREL_SERVICE_CREDENTIALS": CREDENTIAL_A,
        "EPISTREL_MODEL_ADAPTER": "stub",
        "EPISTREL_WORKER_POLL_INTERVAL": "0.3",
        # INFO: uvicorn's line once it bound its port (``BOUND``).
        "EPISTREL_LOG_LEVEL": "INFO",
        "EPISTREL_HOST": "127.0.0.1",
        # The start-up check needs a writable media root (#105); never one under the repository.
        "EPISTREL_MEDIA_ROOT": str(MEDIA_ROOT),
        # The session's ledger, which matches ``pg_url``; the default ``var/erasure-ledger``
        # under the repository outlives the session, and the start-up reconcile would load an
        # earlier session's erasures into this database as pending entries (#125).
        "EPISTREL_ERASURE_LEDGER_DIR": str(ERASURE_LEDGER_DIR),
    }
    return subprocess.Popen(
        [sys.executable, "-m", "epistrel.cli", "worker", "--port", str(port)],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )


@pytest.mark.chaos
@pytest.mark.chaos_quick
@pytest.mark.part("M2", "NFR-REL-5; NFR-OPS-1")
async def test_x_07__processes(pg_url: str, engine: AsyncEngine, quiet: Probe) -> None:
    """Two ``epistrel worker`` processes start together after the hourly purge missed three
    runs: it runs once between them. Each serves ``/health`` and nothing else. Killed (SIGKILL,
    no clean stop), their heartbeats go stale and an ``api`` process reports ``degraded`` with
    no worker alive. Complement: while they ran, every loop was alive."""
    await _execute(
        engine,
        "UPDATE recurring_tasks SET next_run_at = now() - interval '3 hours 5 minutes' "
        "WHERE name = 'idempotency_purge'",
    )
    workers = await _workers(pg_url, 2)
    ports = [w.port for w in workers]
    try:

        async def all_alive() -> bool:
            for worker in workers:
                assert worker.proc.poll() is None, worker.output()
            return bool(await _scalar(engine, "SELECT count(*) FROM worker_heartbeats") == 4)

        await _wait_for(all_alive, within=30)

        async def purged() -> bool:
            rows = await _jobs_of(engine, retention.IDEMPOTENCY_PURGE)
            return bool(rows) and all(r.status == "done" for r in rows)

        await _wait_for(purged, within=10)
        await asyncio.sleep(1.0)  # both had several more ticks to fire it again
        assert len(await _jobs_of(engine, retention.IDEMPOTENCY_PURGE)) == 1
        task = await _task(engine, retention.IDEMPOTENCY_PURGE)
        assert task.last_status == "ok"
        assert timedelta(minutes=59) < task.next_run_at - task.last_run_at <= timedelta(hours=1)
        async with httpx.AsyncClient() as client:
            health = await client.get(f"http://127.0.0.1:{ports[0]}/health")
            assert health.status_code == 200
            assert health.json()["checks"]["workers"]["alive"] == 4
            routes = await client.get(f"http://127.0.0.1:{ports[0]}/stories", headers=_bearer())
            assert routes.status_code == 404
    finally:
        for worker in workers:
            worker.proc.kill()
            worker.proc.wait(timeout=10)
    assert await _scalar(engine, "SELECT count(*) FROM worker_heartbeats") == 4  # no clean stop
    await _execute(engine, "UPDATE worker_heartbeats SET at = at - interval '61 seconds'")
    api = auth_settings(pg_url)
    _, body = await _health(api, engine)
    assert body["status"] == "degraded"
    assert body["checks"]["workers"]["alive"] == 0
    assert body["checks"]["workers"]["last_heartbeat_s"] >= 61


@pytest.mark.chaos
@pytest.mark.chaos_quick
@pytest.mark.part("M2", "NFR-REL-5; NFR-OPS-1")
async def test_x_07__processes_port_taken(
    pg_url: str, engine: AsyncEngine, quiet: Probe, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A worker whose port another process took before it bound it (#468) exits with uvicorn's
    ``address already in use``; ``_workers`` starts it again on a fresh port, and only that one
    runs: its heartbeats are the only ones (the first stopped cleanly), and nothing talked to
    the taken port."""
    with socket.socket() as other:
        other.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        other.bind(("127.0.0.1", 0))
        other.listen()
        other.setblocking(False)
        taken = int(other.getsockname()[1])
        picked: list[int] = []
        fresh = _free_port

        def first_taken() -> int:
            picked.append(taken if not picked else fresh())
            return picked[-1]

        monkeypatch.setattr(sys.modules[__name__], "_free_port", first_taken)
        (worker,) = await _workers(pg_url, 1)
        try:
            assert len(picked) == 2
            assert worker.port == picked[1] != taken

            async def alive() -> bool:
                assert worker.proc.poll() is None, worker.output()
                return bool(await _scalar(engine, "SELECT count(*) FROM worker_heartbeats") == 2)

            await _wait_for(alive, within=30)
            async with httpx.AsyncClient() as client:
                health = await client.get(f"http://127.0.0.1:{worker.port}/health")
                assert health.status_code == 200
                assert health.json()["checks"]["workers"]["alive"] == 2
        finally:
            worker.proc.kill()
            worker.proc.wait(timeout=10)
        with pytest.raises(BlockingIOError):
            other.accept()


# -- the deployment: compose, image, entrypoint -------------------------------------------------


@pytest.mark.chaos
@pytest.mark.chaos_quick
@pytest.mark.part("M2", "NFR-OPS-1,2")
def test_x_07__compose(tmp_path: Path) -> None:
    """The default compose file starts the engine in ``all`` mode (the workers run in it) and
    contains no scheduler, cron, broker or cache service: its services are exactly ``engine``
    and ``db``. The image defaults to ``all``; its HEALTHCHECK exits 0 on ``degraded`` (200) and
    fails on ``unavailable`` (503); the entrypoint migrates in ``all``/``api`` mode and never in
    ``worker`` mode."""
    compose = yaml.safe_load((REPO_ROOT / "docker-compose.yml").read_text())
    assert set(compose["services"]) == {"engine", "db"}
    assert compose["services"]["engine"]["command"] == ["serve", "--mode", "all"]
    images = " ".join(str(s.get("image", "")) for s in compose["services"].values())
    for forbidden in ("redis", "rabbit", "kafka", "nats", "memcache", "cron", "celery"):
        assert forbidden not in images
    dockerfile = (REPO_ROOT / "Dockerfile").read_text()
    assert 'CMD ["serve", "--mode", "all"]' in dockerfile

    probe = _healthcheck_command(dockerfile)
    for code, status, healthy in ((200, "degraded", True), (200, "ok", True), (503, "x", False)):
        with _serving(code, {"status": "unavailable" if code == 503 else status}) as port:
            result = subprocess.run(
                [sys.executable, "-c", probe],
                env={"EPISTREL_PORT": str(port), "PATH": os.environ.get("PATH", "")},
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )
        assert (result.returncode == 0) is healthy, (status, result.stderr)
        if healthy:
            assert result.stdout.strip() == status

    for args, env_mode, migrates in (
        ([], None, True),
        (["serve", "--mode", "api"], None, True),
        (["serve", "--mode", "all"], None, True),
        (["serve", "--mode=worker"], None, False),
        (["serve", "--mode", "worker"], None, False),
        (["serve"], "worker", False),
        (["worker"], None, False),
    ):
        log = _run_entrypoint(tmp_path, args, env_mode)
        assert ("alembic upgrade head" in log) is migrates, (args, env_mode, log)
        assert "epistrel " in log


def _healthcheck_command(dockerfile: str) -> str:
    joined = dockerfile.replace("\\\n", " ")
    line = next(line for line in joined.splitlines() if line.startswith("HEALTHCHECK"))
    command = json.loads(line[line.index("CMD") + 3 :].strip())
    assert command[:2] == ["python", "-c"]
    return str(command[2])


@contextlib.contextmanager
def _serving(code: int, body: dict[str, Any]) -> Iterator[int]:
    payload = json.dumps(body).encode()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args: Any) -> None:
            return

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield int(server.server_address[1])
    finally:
        server.shutdown()
        server.server_close()


def _run_entrypoint(tmp_path: Path, args: list[str], env_mode: str | None) -> str:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    log = tmp_path / "calls.log"
    log.write_text("")
    for name in ("alembic", "epistrel"):
        stub = bin_dir / name
        stub.write_text(f'#!/bin/sh\necho "{name} $*" >> "{log}"\n')
        stub.chmod(0o755)
    script = (REPO_ROOT / "docker" / "entrypoint.sh").read_text().replace("cd /app", "cd .")
    entry = tmp_path / "entrypoint.sh"
    entry.write_text(script)
    env = {"PATH": f"{bin_dir}:/usr/bin:/bin"}
    if env_mode is not None:
        env["EPISTREL_MODE"] = env_mode
    subprocess.run(["/bin/sh", str(entry), *args], env=env, cwd=tmp_path, check=True, timeout=20)
    return log.read_text()


# -- the media sweep (M3) ---------------------------------------------------------------------


async def _retired_assets(space: Any, count: int) -> dict[str, str]:
    """``count`` retired assets nothing references (unique bytes each): id -> checksum."""
    found: dict[str, str] = {}
    for n in range(count):
        media_id = await put(space, png(nonce=f"x07-media-{n}"))
        found[media_id] = (await space.lib.get_media(SWEEPER, media_id)).checksum
        await space.lib.delete_media(SWEEPER, media_id)
    return found


async def _media_ids(db: AsyncEngine) -> set[str]:
    async with db.connect() as conn:
        return {str(i) for i in await conn.scalars(text("SELECT id FROM media_assets"))}


@pytest.mark.chaos
@pytest.mark.chaos_quick
@pytest.mark.part("M3", "NFR-REL-5")
async def test_x_07__media_sweep(
    pg_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The media sweep recovers the same way (#343): killed while purging its second asset, the
    first purge (row and bytes) stands and the rest stay; run again it purges the rest, each
    once, leaving what one uninterrupted run leaves, and a third run changes nothing. Missed
    for three intervals while no worker ran, workers racing for it fire it once, and one
    worker runs it once.

    Complements asserted: the killed run purged exactly one asset; the rerun deletes each
    remaining blob once; after the race exactly one ``media_sweep`` job exists."""
    async with scratch(pg_url, tmp_path / "media") as space:
        assets = await _retired_assets(space, 3)
        calls = 0
        blocked = asyncio.Event()
        original = media_sweep._purge

        async def killed(db: AsyncEngine, asset_id: str, now: datetime) -> str | None:
            nonlocal calls
            calls += 1
            if calls == 2:
                blocked.set()
                await asyncio.Event().wait()  # killed here, before the second purge
            return await original(db, asset_id, now)

        monkeypatch.setattr(media_sweep, "_purge", killed)
        run = asyncio.create_task(media_sweep.sweep(space.db, space.lib.media))
        await asyncio.wait_for(blocked.wait(), 10)
        run.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await run
        left = await _media_ids(space.db)
        assert len(left) == 2
        (gone,) = set(assets) - left
        assert not blob_path(space.root, assets[gone]).exists()
        assert all(blob_path(space.root, assets[m]).exists() for m in left)
        monkeypatch.setattr(media_sweep, "_purge", original)

        again = await media_sweep.sweep(space.db, space.lib.media)
        assert (again["assets_purged"], again["blobs_deleted"]) == (2, 2)
        assert await _media_ids(space.db) == set()
        assert not any(blob_path(space.root, c).exists() for c in assets.values())
        third = await media_sweep.sweep(space.db, space.lib.media)
        assert (third["assets_purged"], third["blobs_deleted"]) == (0, 0)

        # Missed for three intervals while no worker ran: racing workers fire it once.
        late = await _retired_assets(space, 1)
        interval = timedelta(seconds=3600)
        await timer_api.reconcile(space.db)  # the registered tasks' rows, as a worker's start
        async with space.db.begin() as conn:
            await conn.execute(
                update(recurring_tasks).values(next_run_at=_now() + timedelta(days=30))
            )
            await conn.execute(
                update(recurring_tasks)
                .where(recurring_tasks.c.name == media_sweep_job.MEDIA_SWEEP)
                .values(next_run_at=_now() - 3 * interval - timedelta(seconds=5))
            )
        results = await asyncio.gather(*(timer_api.fire_due(space.db) for _ in range(4)))
        fired = [name for r in results for name in r.recurring]
        assert fired == [media_sweep_job.MEDIA_SWEEP]
        assert len(await _jobs_of(space.db, media_sweep_job.MEDIA_SWEEP)) == 1
        media_sweep_job.bind(space.lib.media)
        try:
            worker = Worker(space.db, _config(timers=False))
            assert await _drain(space.db, worker) == 1
        finally:
            media_sweep_job.bind(None)
        assert await _media_ids(space.db) == set()
        assert not blob_path(space.root, next(iter(late.values()))).exists()
        task = await _task(space.db, media_sweep_job.MEDIA_SWEEP)
        assert task.last_status == "ok"
        assert task.next_run_at > _now()


# -- the outbox's claim leases (#210) ------------------------------------------------------------


async def _outbox_turn(engine: AsyncEngine, story: str, names: list[str]) -> list[str]:
    """One turn writing an ``intended`` outbox row per provider ``names``; their ids."""
    ids = [new_ulid() for _ in names]
    async with Operation(engine, story_id=story, kind=OperationKind.TURN, principal="p-test") as op:
        op.append("player_input", {"text": "pay"}, provenance=Provenance.PLAYER)
        op.append("message_rendered", {"text": "the coins change hands"})

        async def write(conn: Any) -> None:
            await core_outbox.write_intended(
                conn,
                op,
                [
                    core_outbox.Intended(
                        id=row_id,
                        provider_name=name,
                        value_name="gold",
                        scope_key=None,
                        effect={"kind": "change", "state": "gold", "delta": -5, "reason": "dues"},
                        beat=2,
                    )
                    for row_id, name in zip(ids, names, strict=True)
                ],
            )
            for row_id in ids:
                await outbox_job.enqueue(conn, row_id, story)

        op.add_commit_step(write)
        await op.commit()
    return ids


async def _outbox_state(engine: AsyncEngine, outbox_id: str) -> tuple[str, int, list[str]]:
    """A row's status and attempts, and the kinds of its outcome records in order."""
    async with engine.connect() as conn:
        row = (
            await conn.execute(
                text("SELECT status, attempts FROM external_effect_outbox WHERE id = :i"),
                {"i": outbox_id},
            )
        ).one()
        kinds = list(
            await conn.scalars(
                text(
                    "SELECT kind FROM outbox_outcomes WHERE outbox_id = :i ORDER BY received_at, id"
                ),
                {"i": outbox_id},
            )
        )
    return str(row.status), int(row.attempts), kinds


@pytest.mark.chaos
@pytest.mark.chaos_quick
@pytest.mark.part("M6", "NFR-REL-5")
async def test_x_07__m6_outbox(engine: AsyncEngine, quiet: Probe, moved: MovableClock) -> None:
    """The outbox's claim leases (#210, FR-EXT-17,18): a turn's two outbox rows are claimed and
    the worker dies before it sends either. While no worker runs their leases lapse; racing
    workers fire each lease timer once, and the next worker runs them: the provider that
    deduplicates gets the row again under the same key (confirmed), the one that does not has
    its row moved to ``unknown`` with one outcome record, never sent. Restarts and later passes
    change nothing. Complement: the confirmed row is never re-sent."""
    dedupe, plain = ps.unique_name("dedupe"), ps.unique_name("plain")
    await ps.register(engine, ps.write_capable_spec(dedupe, dedupes=True))
    await ps.register(engine, ps.write_capable_spec(plain, dedupes=False))
    story = await new_story(engine)
    registry = InProcessRegistry()
    fakes = {name: ps.ScriptedProvider(name) for name in (dedupe, plain)}
    for name, fake in fakes.items():
        registry.register(name, read=fake.read, write=fake.write)
        await ps.bind(engine, story, name, {"player": "p-1"})
    outbox_job.bind(registry)
    try:
        resent, lapsed = await _outbox_turn(engine, story, [dedupe, plain])
        assert {j.status for j in await ps.outbox_jobs(engine, story)} == {"queued"}
        # The worker claims both rows and dies before sending (its dispatch jobs end).
        for row_id in (resent, lapsed):
            assert isinstance(await outbox_job.claim(engine, row_id), outbox_job.Claimed)
        async with engine.begin() as conn:
            await conn.execute(delete(jobs).where(jobs.c.story_id == story))
        assert await _outbox_state(engine, lapsed) == ("claimed", 1, [])

        # The outage outlasts the leases; racing workers fire each lease timer once.
        moved.advance(outbox_job.CLAIM_LEASE.total_seconds() + 5)
        results = await asyncio.gather(*(timer_api.fire_due(engine) for _ in range(4)))
        fired = sorted(kind for r in results for kind in r.timers)
        assert fired == [outbox_job.OUTBOX_LEASE, outbox_job.OUTBOX_LEASE]
        worker = Worker(engine, _config(timers=False))
        assert await worker.recovery_pass() is not None
        # The two lease jobs, and (#217) the late settlement the confirmation enqueued.
        assert await _drain(engine, worker) == 3
        assert fakes[dedupe].idempotency_keys == [resent]
        assert fakes[plain].idempotency_keys == []
        assert await _outbox_state(engine, resent) == ("confirmed", 2, ["confirmed"])
        assert await _outbox_state(engine, lapsed) == ("unknown", 1, ["unknown"])
        listed = (await core_outbox.listings(engine, story)).listings
        assert [(e.outbox_id, e.reason) for e in listed] == [(lapsed, "unknown")]

        # Restarts and later passes change nothing; the confirmed row is never re-sent.
        for _ in range(2):
            moved.advance(outbox_job.CLAIM_LEASE.total_seconds() + 5)
            restarted = Worker(engine, _config(timers=False))
            await restarted.recovery_pass()
            await timer_api.fire_due(engine)
            await _drain(engine, restarted)
            await outbox_job.expire(engine, lapsed)
            await outbox_job.expire(engine, resent)
        assert fakes[dedupe].idempotency_keys == [resent]
        assert fakes[plain].idempotency_keys == []
        assert await _outbox_state(engine, resent) == ("confirmed", 2, ["confirmed"])
        assert await _outbox_state(engine, lapsed) == ("unknown", 1, ["unknown"])
    finally:
        outbox_job.bind(None)
        await ps.drop_outbox_jobs(engine, story)


# -- State reservation expiry (#216) -------------------------------------------------------------


async def _holds(engine: AsyncEngine) -> tuple[str, str, str]:
    """A story whose Cal has 10 gold, two of it held by one turn for two external payouts
    (``held`` reservations), each with a ``reservation_expiry`` timer due in two hours; answers
    the story and the two held changes' ids."""
    world = await c.world(engine)
    story, cal = world.story_id, world.characters["Cal"]
    await use_structured(engine, story)
    await ss.set_level(engine, story, "on")
    await ss.define_all(engine, story, ss.gold())
    await ss.change(engine, story, ss.set_to("gold", 10, scope_key=cal))

    def hold() -> Any:
        return ss.delta(
            "gold",
            -1,
            scope_key=cal,
            contingency=StateContingency.RESERVATION,
            status=StateChangeStatus.HELD,
            prerequisite_effect_ids=(new_ulid(),),
        )

    async def write(conn: Any, op: Operation) -> list[str]:
        found = [await states.apply_change(conn, op, hold()) for _ in range(2)]
        ids = [f.change_id for f in found if isinstance(f, states.Applied)]
        due = clock.now() + timedelta(hours=2)
        for change_id in ids:
            await timer_api.schedule(
                conn, reservations.RESERVATION_EXPIRY, change_id, due, story_id=story
            )
        return ids

    _, (first, second) = await c.run(engine, story, write)
    return story, first, second


async def _change_status(engine: AsyncEngine, change_id: str) -> str:
    return str(await _scalar(engine, "SELECT status FROM state_changes WHERE id = :i", i=change_id))


@pytest.mark.chaos
@pytest.mark.chaos_quick
@pytest.mark.part("M6", "NFR-REL-5")
async def test_x_07__m6_reservation(
    engine: AsyncEngine, quiet: Probe, moved: MovableClock, states_delivered: None
) -> None:
    """State reservation expiry (#216, FR-STATE-14): two holds' expiry timers come due while no
    worker runs; racing workers fire each timer once, and the next worker's recovery pass and
    drain release the hold exactly once. Restarts, later passes and a repeated release change
    nothing. Complement: a hold converted before its expiry is not released by its timer, and a
    timer not yet due fires nothing."""
    story, released, converted = await _holds(engine)
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE state_changes SET status = 'converted' WHERE id = :i"), {"i": converted}
        )
    # Not due yet: nothing fires.
    assert (await timer_api.fire_due(engine)).timers == []

    # The outage outlasts the expiry; racing workers fire each timer once.
    moved.advance(timedelta(hours=2, minutes=1).total_seconds())
    results = await asyncio.gather(*(timer_api.fire_due(engine) for _ in range(4)))
    fired = sorted(kind for r in results for kind in r.timers)
    assert fired == [reservations.RESERVATION_EXPIRY] * 2
    worker = Worker(engine, _config(timers=False))
    assert await worker.recovery_pass() is not None
    assert await _drain(engine, worker) == 2
    outcomes = sorted(
        j.result["outcome"] for j in await _jobs_of(engine, reservations.RESERVATION_EXPIRY)
    )
    assert outcomes == [reservations.RELEASED, reservations.SKIPPED]
    assert await _change_status(engine, released) == "released"
    assert await _change_status(engine, converted) == "converted"

    # Restarts and later passes change nothing.
    for _ in range(2):
        moved.advance(timedelta(hours=3).total_seconds())
        restarted = Worker(engine, _config(timers=False))
        await restarted.recovery_pass()
        await timer_api.fire_due(engine)
        assert await _drain(engine, restarted) == 0
        assert await reservations.expire(engine, released) == reservations.SKIPPED
    assert await _change_status(engine, released) == "released"
    assert len(await _jobs_of(engine, reservations.RESERVATION_EXPIRY)) == 2
    async with engine.begin() as conn:
        await conn.execute(delete(jobs).where(jobs.c.story_id == story))


# -- X-07 M7: the certification canary (#234) ---------------------------------------------------

CANARY_FLOOR = "smoke.turn_completed"


class _HoldingHarness:
    """A harness on which every canary holds its floor (no model is called)."""

    def __init__(self) -> None:
        self.runs: list[str] = []

    def suites(self) -> list[str]:
        return ["smoke"]

    def applicable(self, settings: Any) -> list[str]:
        return ["smoke"]

    def estimate(self, suites: Any, seeds: int, sample: int | None) -> RunEstimate:
        return RunEstimate(model_calls=0, tokens=0, duration_s=0)

    def committed_runs(self) -> list[dict[str, Any]]:
        return []

    def suites_of(self, metric_ids: Any) -> list[str]:
        return ["smoke"]

    async def run(self, request: HarnessRequest) -> HarnessOutcome:
        self.runs.append(request.run_id)
        floor = {"metric_id": CANARY_FLOOR, "rule": "rate", "n": 10, "seeds": 2, "verdict": "pass"}
        return HarnessOutcome(summary={"floors": {"passed": [floor]}}, report_md="")


def _reference_record() -> CertificationRecord:
    report = "# reference\n"
    return CertificationRecord(
        schema_version=1,
        certification_id=new_ulid(),
        fingerprint="sha256:" + "c" * 64,
        profile_id=FULL_PRESET_ID,
        profile_version=1,
        floors_passed=[
            FloorResult(metric_id=CANARY_FLOOR, rule="rate", n=10, seeds=2, verdict="pass")
        ],
        seed_count=2,
        model_tier="reference",
        report_sha256=hashlib.sha256(report.encode()).hexdigest(),
        run_at=datetime.now(UTC),
    )


@pytest.mark.chaos
@pytest.mark.part("M7", "NFR-REL-5")
async def test_x_07__m7_canary(pg_url: str, moved: MovableClock) -> None:
    """With the canary on and a reference certification in use, no worker runs past three of
    its due times: ``/health`` is ``degraded`` for the overdue task. Workers return and race
    for the due row: the canary fans out once (one canary run for the certification, not
    three), late and never skipped, and the run holds. Complement: the next due time is one
    interval after it ran, so firing again at once queues nothing."""
    with migrated_database(pg_url, "epistrel_x07_canary") as url:
        settings = auth_settings(url)
        db = make_engine(settings)
        harness = _HoldingHarness()
        certification.install_harness(harness)
        try:
            async with db.begin() as conn:
                await certification.store_record(conn, _reference_record(), via="import")
            await apply_settings(db, {"certification_canary_enabled": True}, principal="p-op")
            await timer_api.reconcile(db)
            await _execute(db, "UPDATE recurring_tasks SET next_run_at = now() + interval '1 day'")
            interval = timedelta(seconds=DEFAULT_CANARY_INTERVAL_S)
            await _execute(
                db,
                "UPDATE recurring_tasks SET next_run_at = now() - :late WHERE name = :name",
                late=3 * interval + timedelta(seconds=5),
                name=CERTIFICATION_CANARY_TASK,
            )
            status, body = await _health(settings, db)
            assert (status, body["status"]) == (200, "degraded")
            assert body["checks"]["oldest_overdue_timer_s"] >= 3 * interval.total_seconds()

            results = await asyncio.gather(*(timer_api.fire_due(db) for _ in range(3)))
            fired = [name for r in results for name in r.recurring]
            assert fired == [CERTIFICATION_CANARY_TASK]
            worker = Worker(db, _config(timers=False))
            assert await _drain(db, worker) == 2  # the fan-out, then its one canary run
            async with db.connect() as conn:
                runs = (
                    await conn.execute(
                        text("SELECT kind, outcome, requested_by FROM certification_runs")
                    )
                ).all()
            assert [tuple(r) for r in runs] == [("canary", "held", "system:canary")]
            assert len(harness.runs) == 1
            task = await _task(db, CERTIFICATION_CANARY_TASK)
            assert task.last_status == "ok"
            assert task.next_run_at - task.last_run_at == interval
            assert (await timer_api.fire_due(db)).recurring == []
        finally:
            certification.install_harness(None)
            await db.dispose()


@pytest.mark.chaos
@pytest.mark.chaos_quick
@pytest.mark.part("M7", "NFR-REL-5")
async def test_x_07__m7_prune(pg_url: str, moved: MovableClock) -> None:
    """#703: with a story opted into a one-day ``prune_policy`` and an aged discarded tail, no
    worker runs past three of ``prune_run``'s due times. Workers return and race for the due
    row: the run fires once and prunes the tail once (one ``pruning`` event), late and never
    skipped. Complement: firing again at once queues nothing, and a rerun of the policy (a
    worker killed mid-run and run again) prunes nothing further."""
    from epistrel.core import prune as core_prune
    from epistrel.core import timeline
    from epistrel.orchestrator.jobs.handlers import prune_policy

    with migrated_database(pg_url, "epistrel_x07_prune") as url:
        db = make_engine(auth_settings(url))
        prune_policy.bind(core_prune.run_policy)
        try:
            story = await new_story(db)
            for n in range(3):
                await play_turn(db, story, f"turn {n}")
            await timeline.truncate(
                db, story, timeline.TruncateRequest(to_turn=1), principal="p-test"
            )
            await _execute(
                db,
                "UPDATE operations SET membership_tx = membership_tx - interval '3 days' "
                "WHERE story_id = :s AND membership = 'discarded'",
                s=story,
            )
            await _execute(
                db,
                "UPDATE stories SET settings = settings || "
                '\'{"prune_policy": {"older_than_days": 1}}\'::jsonb WHERE id = :s',
                s=story,
            )
            await timer_api.reconcile(db)
            await _execute(db, "UPDATE recurring_tasks SET next_run_at = now() + interval '1 day'")
            await _execute(
                db,
                "UPDATE recurring_tasks SET next_run_at = now() - :late WHERE name = :name",
                late=3 * prune_policy.INTERVAL + timedelta(seconds=5),
                name=prune_policy.PRUNE_RUN,
            )
            results = await asyncio.gather(*(timer_api.fire_due(db) for _ in range(3)))
            assert [n for r in results for n in r.recurring] == [prune_policy.PRUNE_RUN]
            worker = Worker(db, _config(timers=False))
            assert await _drain(db, worker) == 1
            prunings = await _scalar(
                db, "SELECT count(*) FROM events WHERE story_id = :s AND type = 'pruning'", s=story
            )
            assert prunings == 1
            assert (await timer_api.fire_due(db)).recurring == []
            again = await core_prune.run_policy(db, clock.now())
            assert again == {"stories": 1, "pruned": 0}
            assert (
                await _scalar(
                    db,
                    "SELECT count(*) FROM events WHERE story_id = :s AND type = 'pruning'",
                    s=story,
                )
                == 1
            )
        finally:
            prune_policy.bind(None)
            await db.dispose()


# -- image staging (M8, #256) -----------------------------------------------------------------


@pytest.mark.chaos
@pytest.mark.chaos_quick
@pytest.mark.part("M8", "NFR-REL-5")
async def test_x_07__m8_staging(
    pg_url: str, engine: AsyncEngine, quiet: Probe, tmp_path: Path
) -> None:
    """#256: workers killed after an image job staged its output and before the check
    (``images.after_stage``), and the job then ended by recovery without its after-commit
    delete, leave the staging directory. While no worker runs it ages past ``staging_ttl`` and
    the 10-minute ``image_staging_sweep`` falls overdue; restarting runs the overdue sweep once,
    which deletes the directory. Running the sweep again changes nothing.

    Complements asserted: before the restart the directory is still there, and exactly one
    ``image_staging_sweep`` job ran."""
    from epistrel.orchestrator.images import jobs as image_jobs
    from epistrel.orchestrator.jobs.handlers import image_job
    from tests.image_job_support import (
        backdate,
        configure_child_adapter,
        crashed_child,
        end_by_recovery,
        image_engine,
        image_settings_for,
        insert,
        staged,
    )

    ttl = 3600
    settings = image_settings_for(pg_url, tmp_path)
    async with image_engine(settings, engine) as lib:
        await configure_child_adapter(lib, settings)
        job_id = await insert(lib)
        child = await crashed_child(pg_url, "images.after_stage", job_id)
        try:
            store = LocalFilesystemStore(Path(child.media_root))
            job_dir = store.image_staging_dir(job_id)
            assert [p.name for p in staged(store, job_id)] == ["0"]
            await end_by_recovery(engine, job_id)
            backdate(job_dir, ttl + 60)
            await _execute(
                engine,
                "UPDATE recurring_tasks SET next_run_at = now() - interval '35 minutes' "
                "WHERE name = :name",
                name=image_job.IMAGE_STAGING_SWEEP,
            )
            assert job_dir.exists()
            child.crash = None
            await child.start()

            async def swept() -> bool:
                task = await _task(engine, image_job.IMAGE_STAGING_SWEEP)
                return not job_dir.exists() and task.last_status == "ok"

            await _wait_for(swept, within=30)
            assert len(await _jobs_of(engine, image_job.IMAGE_STAGING_SWEEP)) == 1
            task = await _task(engine, image_job.IMAGE_STAGING_SWEEP)
            assert task.next_run_at > _now()
            again = await image_jobs.sweep_staging(engine, store, ttl_s=ttl)
            assert again == {"removed": 0, "skipped_running": 0}
        finally:
            await child.__aexit__(None, None, None)


# -- image candidates (M8, #260) ----------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.part("M8", "NFR-REL-5")
async def test_x_07__m8_candidate_ttl(quiet: Probe, pg_url: str, engine: AsyncEngine) -> None:
    """#260: with no worker running, a catalog candidate passes its ``expires_at`` and the
    hourly ``image_candidate_expiry`` falls overdue; starting a worker runs the overdue expiry
    once, which deletes the candidate row, its asset row and its bytes. Running it again
    changes nothing.

    Complements asserted: before the start the candidate, its asset and its bytes are still
    there, and exactly one ``image_candidate_expiry`` job ran."""
    from epistrel.core.images import candidates
    from epistrel.core.images.store import insert_generated_asset
    from epistrel.core.seeds.ratings import EVERYONE_ID
    from epistrel.core.tables import content_ratings
    from epistrel.core.tables.media import MediaState
    from epistrel.orchestrator.jobs.handlers import image_candidate_expiry
    from epistrel.testing import faults
    from epistrel.testing.images import png
    from tests.auth_support import CREDENTIAL_A
    from tests.model_support import CONFIG_KEY

    name = image_candidate_expiry.IMAGE_CANDIDATE_EXPIRY
    child = faults.EngineProcess(pg_url, CREDENTIAL_A, config_key=CONFIG_KEY, mode="all")
    store = LocalFilesystemStore(Path(child.media_root))
    store.prepare()
    data = png(83, 41, shade=7)
    checksum = hashlib.sha256(data).hexdigest()
    staged = Path(child.media_root) / "x07-candidate.png"
    staged.write_bytes(data)
    store.put(staged, checksum, content_type="image/png")
    entry_id = new_ulid()
    async with engine.begin() as conn:
        version = await conn.scalar(
            select(content_ratings.c.current_version_id).where(content_ratings.c.id == EVERYONE_ID)
        )
        asset_id = await insert_generated_asset(
            conn,
            checksum=checksum,
            mime_type="image/png",
            size=len(data),
            width=83,
            height=41,
            owner="p-x07",
            content_rating_version_id=str(version),
            generation_metadata={},
            state=MediaState.CANDIDATE,
            created_by="p-x07",
        )
        [candidate_id] = await candidates.insert(
            conn,
            entry_kind="character",
            entry_id=entry_id,
            job_id=new_ulid(),
            slot="primary",
            draft_revision=1,
            fingerprint="v1:" + "0" * 64,
            asset_ids=[asset_id],
            ttl_s=3_600,
        )
    try:
        await _execute(
            engine,
            "UPDATE catalog.image_candidates SET expires_at = now() - interval '3 hours' "
            "WHERE id = :c",
            c=candidate_id,
        )
        await _execute(
            engine,
            "UPDATE recurring_tasks SET next_run_at = now() - interval '2 hours' WHERE name = :n",
            n=name,
        )
        async with engine.connect() as conn:
            assert await candidates.count_for_entry(conn, "character", entry_id) == 1
        assert store.exists(checksum)
        async with child:

            async def expired() -> bool:
                task = await _task(engine, name)
                return task.last_status == "ok" and not store.exists(checksum)

            await _wait_for(expired, within=30)
        assert len(await _jobs_of(engine, name)) == 1
        async with engine.connect() as conn:
            assert await candidates.count_for_entry(conn, "character", entry_id) == 0
            assert (
                await conn.scalar(
                    text("SELECT count(*) FROM media_assets WHERE id = :a"), {"a": asset_id}
                )
                == 0
            )
        again = await image_candidate_expiry.expire(engine, store)
        assert again == {"expired": 0, "blobs_deleted": 0}
    finally:
        await _execute(
            engine, "DELETE FROM catalog.image_candidates WHERE entry_id = :e", e=entry_id
        )
        await _execute(engine, "DELETE FROM media_assets WHERE id = :a", a=asset_id)


# -- image attempt horizons (M8, #267) ----------------------------------------------------------


@pytest.mark.chaos
@pytest.mark.chaos_quick
@pytest.mark.part("M8", "NFR-REL-5")
async def test_x_07__m8_attempts_hook(
    engine: AsyncEngine, settings: Settings, quiet: Probe
) -> None:
    """#268, #804: a worker-mode process registers the ``image_attempts`` recovery hook, so its
    start-up pass runs ``attempts.recover_attempts``, and stopping the workers unregisters it.

    Complement asserted: before the start and after the stop, no ``image_attempts`` hook is
    registered."""
    from epistrel.engine import IMAGE_ATTEMPTS_HOOK, _recover_image_attempts

    assert IMAGE_ATTEMPTS_HOOK not in _RECOVERY_HOOKS
    lib = Engine(replace_mode(settings, "worker"), db=engine)
    await lib.start_workers()
    try:
        assert _RECOVERY_HOOKS.get(IMAGE_ATTEMPTS_HOOK) is _recover_image_attempts
    finally:
        await lib.stop_workers()
    assert IMAGE_ATTEMPTS_HOOK not in _RECOVERY_HOOKS


@pytest.mark.integration
@pytest.mark.part("M8", "NFR-REL-5")
async def test_x_07__m8_horizon(pg_url: str, tmp_path: Path) -> None:
    """#267: a confirming adapter's call stalls, the worker loses its lease, and the reaper
    fences the attempt and schedules its ``image_attempt_horizon`` timer. With no worker running
    the clock moves past the horizon and past ``image_confirm_wait_max_s``. The restart's
    recovery pass fires the overdue timer, and the drained jobs release the reservation once and
    run the overdue fallback once: one ``cancel``, ``confirm_fallback`` set, the record
    ``abandoned_fallback``, one retry. A second recovery pass and drain change nothing.

    Complements asserted: before the restart the record is still held and nothing was retried,
    and the counters reconcile after each pass."""
    from epistrel.core.tables.images import ImageJobKind
    from epistrel.observability.metrics import IMAGE_ATTEMPT_OUTCOMES
    from epistrel.orchestrator.images import attempts
    from epistrel.orchestrator.images import jobs as image_jobs
    from epistrel.testing.images import ScriptedChecks, installed_checks, registered_target
    from tests.image_job_support import configure, prepared
    from tests.image_limits_support import CatalogTarget, assert_clean, records, space
    from tests.image_support import Outputs, Stall, StatusSeq, StubImageGenerator, registered
    from tests.image_support import catalog_origin as origin_of

    def releases() -> float:
        return float(IMAGE_ATTEMPT_OUTCOMES.labels(outcome="released_at_horizon")._value.get())

    async def fenced_record() -> Any:
        found = [r for r in await records(sp.db, job_id) if r.attempt_token == old]
        assert len(found) == 1
        return found[0]

    async with space(pg_url, tmp_path, "x07_horizon") as sp:
        gate = asyncio.Event()
        stub = StubImageGenerator(
            submission="async",
            confirms_completion=True,
            script=[Stall(gate, StatusSeq(["running"], Outputs(2))), Outputs(2)],
        )
        with (
            registered("stub_async", stub),
            registered_target(CatalogTarget(), kind=ImageJobKind.CATALOG),
            installed_checks(ScriptedChecks(fail_closed=False)),
        ):
            await configure(sp.lib, sp.settings, "stub_async")
            job_id = await sp.insert(
                prepared(kind=ImageJobKind.CATALOG, origin=origin_of(new_ulid()), n=2)
            )
            stalled = asyncio.create_task(sp.run(job_id))
            try:
                await _wait_for(lambda: _sent_one(stub), within=10)
                old = await _scalar(
                    sp.db, "SELECT attempt_token::text FROM image_jobs WHERE id = :j", j=job_id
                )

                async def accepted() -> bool:
                    return bool((await fenced_record()).send_state.value == "accepted")

                await _wait_for(accepted, within=10)
                await _execute(
                    sp.db,
                    "UPDATE jobs SET status = 'running', lease_until = now() - interval '1 second'"
                    " WHERE kind = 'image_job' AND payload->>'image_job_id' = :j",
                    j=job_id,
                )
                assert len(await queue.reap(sp.db)) == 1
                # Workers down for three hours: past the horizon and past confirm_wait_max.
                for sql in (
                    "UPDATE image_reservations SET horizon_at = now() - interval '3 hours' "
                    "WHERE job_id = :j",
                    "UPDATE timers SET due_at = now() - interval '3 hours' WHERE target LIKE :p",
                    "UPDATE jobs SET due_at = now() - interval '3 hours' "
                    "WHERE payload->>'image_job_id' = :j",
                ):
                    await _execute(sp.db, sql, j=job_id, p=f"{job_id}:%")
                assert (await fenced_record()).state.value == "held"
                assert len(stub.requests) == 1
                await assert_clean(sp.db)

                released_before = releases()
                image_jobs.bind(sp.lib.image_runtime())
                worker = Worker(sp.db, _config())
                try:
                    first = await recovery_pass(sp.db)
                    assert attempts.HORIZON_TIMER in first.fired
                    await _drain(sp.db, worker)
                    record = await fenced_record()
                    assert (record.state.value, record.used) == ("released", 0)
                    assert stub.cancelled == ["stub-1"]
                    assert len(stub.requests) == 2, "one retry, after the fallback"
                    row = await _scalar(
                        sp.db,
                        "SELECT confirm_fallback FROM image_jobs WHERE id = :j",
                        j=job_id,
                    )
                    assert row is True
                    outcome = await _scalar(
                        sp.db,
                        "SELECT outcome FROM image_reservations "
                        "WHERE job_id = :j AND attempt_token = :t",
                        j=job_id,
                        t=old,
                    )
                    assert outcome == "abandoned_fallback"
                    # Released once: by the job's own run or by the timer, whichever ran first.
                    assert releases() == released_before + 1
                    horizons = await _jobs_of(sp.db, attempts.HORIZON_TIMER)
                    assert len(horizons) == 1
                    assert horizons[0].result["outcome"] in ("released", "skipped")
                    await assert_clean(sp.db)

                    second = await recovery_pass(sp.db)
                    assert attempts.HORIZON_TIMER not in second.fired
                    await _drain(sp.db, worker)
                    assert stub.cancelled == ["stub-1"]
                    assert len(stub.requests) == 2
                    assert len(await _jobs_of(sp.db, attempts.HORIZON_TIMER)) == 1
                    assert releases() == released_before + 1
                    status = await _scalar(
                        sp.db, "SELECT status FROM image_jobs WHERE id = :j", j=job_id
                    )
                    assert status == "candidates_ready"
                    await assert_clean(sp.db)
                finally:
                    image_jobs.bind(None)
            finally:
                stalled.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await stalled


async def _sent_one(stub: Any) -> bool:
    return len(stub.requests) >= 1
