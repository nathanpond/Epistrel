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

**Recovery pass.** ``recovery_pass()`` runs once when a worker starts: it reclaims every job
whose lease lapsed (each exactly once, with fencing) and then runs the registered recovery
hooks (``register_recovery_hook``, ``async (conn) -> None``, in registration order, each in its
own transaction). Running it again changes nothing.

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

import psycopg
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from epistrel.config import Settings
from epistrel.core import clock
from epistrel.orchestrator.jobs import handlers, queue
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


async def recovery_pass(db: AsyncEngine) -> RecoveryReport:
    """Reclaim every lapsed lease (fenced, each once) and run the recovery hooks; idempotent."""
    reclaimed = await queue.reap(db)
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
    return RecoveryReport(tuple(reclaimed), tuple(ran), tuple(failed))


@dataclass(frozen=True, slots=True)
class WorkerConfig:
    poll_interval: float = 5.0
    lease_s: float = 60.0
    max_attempts: int = 5
    listen: bool = True
    concurrency: int = 2
    max_runtime_s: float = MAX_RUNTIME_S
    drain_s: float = DRAIN_S

    @classmethod
    def from_settings(cls, settings: Settings, *, concurrency: int | None = None) -> WorkerConfig:
        return cls(
            poll_interval=settings.worker_poll_interval,
            lease_s=settings.job_lease_seconds,
            max_attempts=settings.job_max_attempts,
            listen=settings.worker_listen_enabled,
            concurrency=concurrency or settings.worker_concurrency,
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

    async def run(self) -> None:
        """Recovery pass, then the loops (and the listener) until ``stop()``."""
        self._wakes = [asyncio.Event() for _ in range(self.config.concurrency)]
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

    # -- the loop ---------------------------------------------------------------------------

    async def _loop(self, index: int) -> None:
        worker_id = self.worker_id(index)
        wake = self._wakes[index]
        while not self._stopping:
            wake.clear()
            try:
                await queue.reap(self.db)
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
        due = await queue.next_due(self.db)
        if due is not None:
            timeout = max(0.0, min(timeout, (due - clock.now()).total_seconds()))
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
            return await self._fail(job, ValidationError.__name__, terminal=True)
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
            return await self._fail(job, TimeoutError.__name__)
        except asyncio.CancelledError:
            current = asyncio.current_task()
            if state.fenced and (current is None or not current.cancelling()):
                logger.info("job %s (%s): fenced, handler cancelled", job.id, job.kind)
                return None
            raise
        except Exception as exc:
            return await self._fail(job, type(exc).__name__)
        try:
            return await queue.commit_job(self.db, job, handler, output, ctx.input_versions)
        except Exception as exc:
            return await self._fail(job, type(exc).__name__)

    async def _fail(self, job: ClaimedJob, error: str, *, terminal: bool = False) -> str | None:
        return await queue.fail_job(
            self.db, job, error, max_attempts=self.config.max_attempts, terminal=terminal
        )

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
