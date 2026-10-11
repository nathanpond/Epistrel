"""The worker: N loops that claim and run due jobs, woken by ``LISTEN`` and by polling, and the
start-up recovery pass (NFR-REL-5; #70).

**Wake-up.** One raw psycopg connection per process ``LISTEN``s on ``epistrel_jobs`` (when
``worker_listen_enabled``), reconnecting with a 1 to 30 s backoff; a notification wakes every loop.
Notifications only shorten latency: each loop also wakes every ``worker_poll_interval``, or
sooner when the earliest queued job falls due, so a lost notification delays a job by at most
one interval.

**A loop iteration** reaps lapsed leases (``queue.reap``), claims one due job (earliest
``due_at`` first) and runs it: the handler for its kind (an unknown kind or a payload that no
longer validates fails the job at once), with the lease renewed every third of
``job_lease_seconds``. A heartbeat that finds the claim fenced cancels the handler. A handler
running past ``MAX_RUNTIME_S`` is a failed attempt. A finished handler's output goes through
``queue.commit_job`` (fenced, input versions rechecked).

**Timers and recurring tasks (#71).** Every loop iteration also fires the due timers and
recurring tasks (``timers.fire_due``: each becomes a job, once across all workers), and the idle
sleep ends early when one falls due. A recurring kind's handler may set ``max_attempts`` (the
sweeps use 1: a failed run waits for its next interval).

**Heartbeats (#71).** While it runs, the worker upserts one ``worker_heartbeats`` row per loop
every ``poll_interval`` (``health.beat``) and deletes them when it stops, so ``/health`` in any
process sees how many workers are alive.

**Recovery pass.** ``recovery_pass()`` runs once when a worker starts: it reclaims every job
whose lease lapsed (each exactly once, with fencing), writes the registered recurring tasks
into rows (``timers.reconcile``), fires every overdue timer and recurring task (late, never
skipped; a task that missed several runs runs once), and then runs the registered recovery
hooks (``register_recovery_hook``, ``async (conn) -> None``, in registration order, each in its
own transaction; the Engine registers its turn-queue drain as ``turn_queue``). Running it again
changes nothing.

**Stopping.** ``stop()`` (SIGTERM/SIGINT under ``epistrel worker``) lets running jobs finish for
up to ``DRAIN_S`` seconds, then cancels them; a cancelled job keeps its lease, which lapses, and
the next recovery pass or reap reclaims it.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import socket
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime

import psycopg
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from epistrel.config import Settings
from epistrel.core import clock
from epistrel.orchestrator.jobs import handlers, health, queue, timers
from epistrel.orchestrator.jobs.types import ClaimedJob, JobContext, JobHandler, JobOutput

logger = logging.getLogger(__name__)

MAX_RUNTIME_S = 600.0
"""A handler running longer than this is a failed attempt (10 min)."""
DRAIN_S = 30.0
"""How long ``stop()`` lets running jobs finish before cancelling them."""
LISTEN_BACKOFF_MIN_S = 1.0
LISTEN_BACKOFF_MAX_S = 30.0

RecoveryHook = Callable[[AsyncConnection], Awaitable[None]]

_RECOVERY_HOOKS: dict[str, RecoveryHook] = {}


def register_recovery_hook(name: str, hook: RecoveryHook) -> None:
    """Run ``hook`` in every worker's start-up recovery pass (after the lease reclaim). Hooks
    run in registration order; registering a name again replaces its hook in place."""
    _RECOVERY_HOOKS[name] = hook


def unregister_recovery_hook(name: str) -> None:
    _RECOVERY_HOOKS.pop(name, None)


@dataclass(frozen=True, slots=True)
class RecoveryReport:
    reclaimed: tuple[str, ...]
    hooks: tuple[str, ...]
    failed_hooks: tuple[str, ...] = ()
    fired: tuple[str, ...] = ()


async def recovery_pass(db: AsyncEngine) -> RecoveryReport:
    """Reclaim every lapsed lease (fenced, each once), reconcile and fire every overdue timer
    and recurring task, and run the recovery hooks; idempotent."""
    reclaimed = await queue.reap(db)
    await timers.reconcile(db)
    due = await timers.fire_due(db)
    ran: list[str] = []
    failed: list[str] = []
    for name, hook in list(_RECOVERY_HOOKS.items()):
        try:
            async with db.begin() as conn:
                await hook(conn)
            ran.append(name)
        except Exception as exc:
            logger.warning("recovery hook %s failed: %s", name, type(exc).__name__)
            failed.append(name)
    return RecoveryReport(
        tuple(reclaimed), tuple(ran), tuple(failed), tuple(due.timers + due.recurring)
    )


@dataclass(frozen=True, slots=True)
class WorkerConfig:
    poll_interval: float = 5.0
    lease_s: float = 60.0
    max_attempts: int = 5
    listen: bool = True
    concurrency: int = 2
    max_runtime_s: float = MAX_RUNTIME_S
    drain_s: float = DRAIN_S
    mode: str = "worker"
    """The process mode the heartbeats name (``all`` or ``worker``)."""
    timers: bool = True
    """Fire due timers and recurring tasks in the loop (tests of the bare queue turn it off)."""

    @classmethod
    def from_settings(
        cls, settings: Settings, *, concurrency: int | None = None, mode: str | None = None
    ) -> WorkerConfig:
        return cls(
            poll_interval=settings.worker_poll_interval,
            lease_s=settings.job_lease_seconds,
            max_attempts=settings.job_max_attempts,
            listen=settings.worker_listen_enabled,
            concurrency=concurrency or settings.worker_concurrency,
            mode=mode or settings.mode,
        )


def listen_dsn(db: AsyncEngine) -> str:
    """The libpq DSN of ``db`` (the raw driver connection that LISTENs needs no dialect)."""
    return db.url.set(drivername="postgresql").render_as_string(hide_password=False)


@dataclass
class _Run:
    fenced: bool = False


@dataclass
class Worker:
    """``worker_concurrency`` loops over the job queue in this process; see the module
    docstring. ``run()`` until ``stop()``; tests drive ``run_one`` and ``recovery_pass``."""

    db: AsyncEngine
    config: WorkerConfig = field(default_factory=WorkerConfig)
    dsn: str | None = None
    listening: asyncio.Event = field(default_factory=asyncio.Event)
    _wakes: list[asyncio.Event] = field(default_factory=list)
    _stopping: bool = False
    _stopped: asyncio.Event = field(default_factory=asyncio.Event)
    _recovered: bool = False
    _started_at: datetime | None = None

    async def recovery_pass(self) -> RecoveryReport | None:
        """The start-up recovery pass, once per worker (``None`` when it already ran)."""
        if self._recovered:
            return None
        self._recovered = True
        return await recovery_pass(self.db)

    def worker_id(self, loop: int) -> str:
        return f"{socket.gethostname()}:{os.getpid()}:{loop}"

    def wake(self) -> None:
        for event in self._wakes:
            event.set()

    def stop(self) -> None:
        self._stopping = True
        self._stopped.set()
        self.wake()

    def worker_ids(self) -> list[str]:
        return [self.worker_id(index) for index in range(self.config.concurrency)]

    async def beat(self) -> None:
        """Upsert this process's heartbeats (one per loop) now."""
        if self._started_at is None:
            self._started_at = clock.now()
        await health.beat(
            self.db,
            self.worker_ids(),
            mode=self.config.mode,
            pid=os.getpid(),
            started_at=self._started_at,
        )

    async def _heartbeats(self) -> None:
        while not self._stopping:
            try:
                await self.beat()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("worker heartbeat failed: %s", type(exc).__name__)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stopped.wait(), self.config.poll_interval)

    async def run(self) -> None:
        """Recovery pass, then the loops (and the listener) until ``stop()``."""
        self._wakes = [asyncio.Event() for _ in range(self.config.concurrency)]
        beats = asyncio.create_task(self._heartbeats(), name="epistrel-jobs-heartbeat")
        try:
            await self.recovery_pass()
        except Exception as exc:
            logger.warning("worker recovery pass failed: %s", type(exc).__name__)
        listener = (
            asyncio.create_task(self._listen(), name="epistrel-jobs-listen")
            if self.config.listen
            else None
        )
        loops = [
            asyncio.create_task(self._loop(index), name=f"epistrel-jobs-{index}")
            for index in range(self.config.concurrency)
        ]
        try:
            await self._stopped.wait()
            _, pending = await asyncio.wait(loops, timeout=self.config.drain_s)
            for task in pending:
                task.cancel()
            await asyncio.gather(*loops, return_exceptions=True)
        finally:
            for task in loops:
                task.cancel()
            if listener is not None:
                listener.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await listener
            await asyncio.gather(*loops, return_exceptions=True)
            beats.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await beats
            try:
                await asyncio.shield(health.forget(self.db, self.worker_ids()))
            except Exception as exc:
                logger.warning("worker heartbeats not removed: %s", type(exc).__name__)

    # -- the loop ---------------------------------------------------------------------------

    async def _loop(self, index: int) -> None:
        worker_id = self.worker_id(index)
        wake = self._wakes[index]
        while not self._stopping:
            wake.clear()
            try:
                await queue.reap(self.db)
                if self.config.timers:
                    await timers.fire_due(self.db)
                if await self.run_one(worker_id):
                    continue
                timeout = await self._idle_timeout()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("worker loop %s: %s", index, type(exc).__name__)
                timeout = self.config.poll_interval
            if self._stopping:
                return
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(wake.wait(), timeout)

    async def _idle_timeout(self) -> float:
        """Sleep until the next poll, or until the earliest queued job is due if sooner."""
        timeout = self.config.poll_interval
        due = [await queue.next_due(self.db)]
        if self.config.timers:
            due.append(await timers.next_due(self.db))
        for moment in due:
            if moment is not None:
                timeout = max(0.0, min(timeout, (moment - clock.now()).total_seconds()))
        return timeout

    async def run_one(self, worker_id: str) -> bool:
        """Claim one due job and run it to its end; ``False`` when none was due."""
        job = await queue.claim(self.db, worker_id, lease_s=self.config.lease_s)
        if job is None:
            return False
        await self.execute(job)
        return True

    async def execute(self, job: ClaimedJob) -> queue.CommitOutcome | str | None:
        """Run a claimed job: its handler under heartbeat and the runtime cap, then the commit.
        Returns the commit outcome, the status a failure left (``queued``/``failed``), or
        ``None`` when the claim was fenced."""
        handler = handlers.lookup(job.kind)
        if handler is None:
            return await self._fail(job, queue.UnknownJobKind.__name__, terminal=True)
        try:
            payload = handler.payload.model_validate(job.payload)
        except ValidationError:
            return await self._fail(job, ValidationError.__name__, handler, terminal=True)
        ctx = JobContext(db=self.db, job=job, payload=payload)
        state = _Run()
        task = asyncio.create_task(handler.run(ctx))
        beat = asyncio.create_task(self._heartbeat(job, task, state))
        try:
            return await self._finish(job, handler, ctx, task, state)
        finally:
            beat.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await beat

    async def _finish(
        self,
        job: ClaimedJob,
        handler: JobHandler,
        ctx: JobContext,
        task: asyncio.Task[JobOutput],
        state: _Run,
    ) -> queue.CommitOutcome | str | None:
        try:
            async with asyncio.timeout(self.config.max_runtime_s):
                output = await task
        except TimeoutError:
            return await self._fail(job, TimeoutError.__name__, handler)
        except asyncio.CancelledError:
            current = asyncio.current_task()
            if state.fenced and (current is None or not current.cancelling()):
                logger.info("job %s (%s): fenced, handler cancelled", job.id, job.kind)
                return None
            raise
        except Exception as exc:
            return await self._fail(job, type(exc).__name__, handler)
        try:
            return await queue.commit_job(self.db, job, handler, output, ctx.input_versions)
        except Exception as exc:
            return await self._fail(job, type(exc).__name__, handler)

    async def _fail(
        self,
        job: ClaimedJob,
        error: str,
        handler: JobHandler | None = None,
        *,
        terminal: bool = False,
    ) -> str | None:
        limit = self.config.max_attempts
        if handler is not None and handler.max_attempts is not None:
            limit = handler.max_attempts
        return await queue.fail_job(self.db, job, error, max_attempts=limit, terminal=terminal)

    async def _heartbeat(self, job: ClaimedJob, task: asyncio.Task[JobOutput], state: _Run) -> None:
        interval = self.config.lease_s / 3
        while not task.done():
            await asyncio.sleep(interval)
            try:
                held = await queue.heartbeat(self.db, job, lease_s=self.config.lease_s)
            except Exception as exc:
                logger.warning("job %s heartbeat failed: %s", job.id, type(exc).__name__)
                continue
            if not held:
                state.fenced = True
                task.cancel()
                return

    # -- LISTEN -----------------------------------------------------------------------------

    async def _listen(self) -> None:
        dsn = self.dsn or listen_dsn(self.db)
        delay = LISTEN_BACKOFF_MIN_S
        while True:
            try:
                conn = await psycopg.AsyncConnection.connect(dsn, autocommit=True)
                async with conn:
                    await conn.execute("LISTEN epistrel_jobs")
                    delay = LISTEN_BACKOFF_MIN_S
                    self.listening.set()
                    self.wake()  # anything notified while not listening is looked at now
                    async for _ in conn.notifies():
                        self.wake()
            except asyncio.CancelledError:
                self.listening.clear()
                raise
            except Exception as exc:
                logger.warning("job LISTEN connection lost: %s", type(exc).__name__)
            self.listening.clear()
            await asyncio.sleep(delay)
            delay = min(delay * 2, LISTEN_BACKOFF_MAX_S)


__all__ = [
    "DRAIN_S",
    "MAX_RUNTIME_S",
    "RecoveryHook",
    "RecoveryReport",
    "Worker",
    "WorkerConfig",
    "listen_dsn",
    "recovery_pass",
    "register_recovery_hook",
    "unregister_recovery_hook",
]
