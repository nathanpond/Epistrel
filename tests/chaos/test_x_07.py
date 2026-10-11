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
import json
import os
import socket
import subprocess
import sys
import threading
import time
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
from sqlalchemy import delete, select, text, update
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.api import create_app
from epistrel.api.routes.health import check
from epistrel.config import Settings
from epistrel.core import clock
from epistrel.core.events.operation import Operation
from epistrel.core.tables import audit_log, idempotency_keys, jobs, recurring_tasks, timers
from epistrel.core.tables import worker_heartbeats as heartbeats
from epistrel.engine import TURN_QUEUE_HOOK, Engine
from epistrel.orchestrator.jobs import handlers, queue
from epistrel.orchestrator.jobs import timers as timer_api
from epistrel.orchestrator.jobs.handlers import retention, turn_queue
from epistrel.orchestrator.jobs.recurring import (
    NoPayload,
    RecurringTask,
    recording,
    register_recurring,
    unregister_recurring,
)
from epistrel.orchestrator.jobs.types import JobContext, JobHandler, JobOutput
from epistrel.orchestrator.jobs.worker import (
    _RECOVERY_HOOKS,
    Worker,
    WorkerConfig,
    recovery_pass,
)
from tests.auth_support import CREDENTIAL_A, auth_settings
from tests.conftest import REPO_ROOT
from tests.event_support import new_story, play_turn
from tests.integration import test_jobs as jt

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
    """Test handlers registered; jobs, timers and heartbeats empty; every seeded recurring row
    due far in the future. Everything is put back afterwards."""
    global PROBE
    PROBE = Probe()
    for handler in TEST_HANDLERS:
        handlers.register(handler)
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


class MovableClock:
    def __init__(self) -> None:
        self.offset = timedelta()

    def __call__(self) -> datetime:
        return datetime.now(UTC) + self.offset

    def advance(self, seconds: float) -> None:
        self.offset += timedelta(seconds=seconds)


@pytest.fixture
def moved() -> Iterator[MovableClock]:
    movable = MovableClock()
    clock.set_clock(movable)
    try:
        yield movable
    finally:
        clock.set_clock(None)


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
@pytest.mark.part("M2", "NFR-REL-5")
async def test_x_07__jobs(engine: AsyncEngine, registry: jt.Probe, moved: MovableClock) -> None:
    """Notifications suppressed, a job is still claimed within one poll interval. A worker
    killed mid-job leaves its lease; once it lapses the next worker's recovery pass reclaims it
    exactly once with fencing, and the job then finishes; a second recovery pass changes
    nothing. The lost claim's late commit is refused and writes nothing. Complement: the
    current claim's commit is accepted."""
    # 1. NOTIFY suppressed: polling alone claims the job within one interval.
    worker = Worker(engine, jt._config())
    running = asyncio.create_task(worker.run())
    await asyncio.sleep(0.1)  # the loop is idle and waiting for its next poll
    assert not worker.listening.is_set()
    started = time.monotonic()
    polled = await jt._enqueue(engine, "test_echo", {"n": 1})
    await jt._wait_for(lambda: jt._done(engine, polled), within=jt.POLL_S + 2.0)
    assert time.monotonic() - started <= jt.POLL_S + 0.5
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
@pytest.mark.part("M2", "NFR-REL-5")
async def test_x_07__timers(engine: AsyncEngine, quiet: Probe) -> None:
    """A due timer is fired by a polling worker with no notification, within one interval of
    falling due; ten due timers raced by four workers each fire exactly once; ``schedule`` moves
    a timer and ``cancel`` removes it; a timer of an unknown kind is deleted unfired.
    Complement: a timer not yet due is not fired."""
    worker = Worker(engine, _config())
    run = asyncio.create_task(worker.run())
    try:
        due_in = 0.5
        async with engine.begin() as conn:
            await timer_api.schedule(conn, TIMER, "polled", _now() + timedelta(seconds=due_in))
        started = time.monotonic()
        await _wait_for(lambda: _ran(quiet, "polled"), within=due_in + POLL_S + 2.0)
        assert time.monotonic() - started <= due_in + POLL_S + 1.0
    finally:
        worker.stop()
        await run
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
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _spawn(pg_url: str, port: int) -> subprocess.Popen[str]:
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "EPISTREL_DATABASE_URL": pg_url,
        "EPISTREL_SERVICE_CREDENTIALS": CREDENTIAL_A,
        "EPISTREL_MODEL_ADAPTER": "stub",
        "EPISTREL_WORKER_POLL_INTERVAL": "0.3",
        "EPISTREL_LOG_LEVEL": "WARNING",
        "EPISTREL_HOST": "127.0.0.1",
    }
    return subprocess.Popen(
        [sys.executable, "-m", "epistrel.cli", "worker", "--port", str(port)],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )


@pytest.mark.chaos
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
    ports = [_free_port(), _free_port()]
    procs = [_spawn(pg_url, port) for port in ports]
    try:

        async def all_alive() -> bool:
            for proc in procs:
                assert proc.poll() is None, proc.stderr.read() if proc.stderr else ""
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
        for proc in procs:
            proc.kill()
            proc.wait(timeout=10)
    assert await _scalar(engine, "SELECT count(*) FROM worker_heartbeats") == 4  # no clean stop
    await _execute(engine, "UPDATE worker_heartbeats SET at = at - interval '61 seconds'")
    api = auth_settings(pg_url)
    _, body = await _health(api, engine)
    assert body["status"] == "degraded"
    assert body["checks"]["workers"]["alive"] == 0
    assert body["checks"]["workers"]["last_heartbeat_s"] >= 61


# -- the deployment: compose, image, entrypoint -------------------------------------------------


@pytest.mark.chaos
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
