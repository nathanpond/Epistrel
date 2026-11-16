"""A case retries its own story's ``StoryBusy`` (#929) instead of failing the run.

A scratch story's own background work can hold its lock while a case's next step commits: a
commit that waits past the story-lock timeout is refused with ``StoryBusy`` (``reason``
``lock_timeout``, #472), and a second turn on a story whose turn is still running is refused
with ``reason`` ``in_flight`` (#63). Both mean "nothing was written; try again". The harness
used to count the case as a harness error, which failed CI's zero-leak job intermittently.

``BusyRetry`` retries the call that raised, never a step that committed (a refused commit wrote
nothing), with backoff that honours the error's ``Retry-After`` and is bounded: calls and sleeps
together stay within ``BOUND_S`` (30 s) per case. After the bound the last ``StoryBusy`` is
raised again, naming the retries, so the case is a **harness error** that says ``StoryBusy``:
not a model error, and not silently passed. ``queue_full`` (a full turn queue) is a
configuration problem, not contention, and is never retried. Any other error is never retried.

It covers every call a case makes on its ``Engine`` (``BusyEngine``, the proxy ``CaseContext``,
the case's scratch stories and a suite's setup and teardown are given) and the harness's own
commits (``scenes.episodes``). A ``lock_timeout`` logs, at warning, the backend then holding
the story's row, found by the row's ``xmax`` (``log_holders``: state, the first 80 characters
of the statement and the transaction's age; SQL, never content), so a run that meets one names
what held the row.
Backoff time is not case latency (``slept_ms``), and the number
of retries goes into the case's ``detail`` (``story_busy_retries``) when there were any. Once a
run's ``stop`` is set (Ctrl-C), a case waiting in backoff ends with ``Stopped``.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import inspect
import logging
import time
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any

from sqlalchemy import text

from epistrel.core.errors import StoryBusy

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine

    from epistrel.engine import Engine

BOUND_S = 30.0
"""The most a case spends retrying ``StoryBusy``: calls and backoff together."""
FIRST_WAIT_S = 0.25
MAX_WAIT_S = 5.0
log = logging.getLogger(__name__)
RETRIED = frozenset({"lock_timeout", "in_flight"})
"""The ``StoryBusy`` reasons that mean contention (``queue_full`` is not)."""


class Stopped(Exception):
    """The run was asked to stop (``SuiteRun.stop``): no new case started, the ones in flight
    finished and were journalled, and nothing was reported. Also raised by a case waiting out
    ``StoryBusy`` once the stop is set (``runner.Stopped`` is this class)."""


class BusyRetry:
    """One case's retry budget (module docstring)."""

    def __init__(
        self,
        *,
        bound_s: float | None = None,
        stop: asyncio.Event | None = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
        db: AsyncEngine | None = None,
    ) -> None:
        self.bound_s = BOUND_S if bound_s is None else bound_s
        self.stop = stop
        self._sleep = sleep
        self.db = db
        self.retries = 0
        self.slept_s = 0.0
        self._started: float | None = None

    @property
    def slept_ms(self) -> int:
        return round(self.slept_s * 1000)

    async def run[T](self, call: Callable[[], Awaitable[T]]) -> T:
        """``call()``, called again while it raises a retryable ``StoryBusy`` within the
        bound."""
        wait = FIRST_WAIT_S
        while True:
            if self._started is None:
                self._started = time.monotonic()
            try:
                return await call()
            except StoryBusy as exc:
                if exc.details.get("reason") not in RETRIED:
                    raise
                if exc.details.get("reason") == "lock_timeout" and self.db is not None:
                    await log_holders(self.db, exc)
                left = self.bound_s - (time.monotonic() - self._started)
                pause = min(max(float(exc.retry_after or 0), wait), MAX_WAIT_S)
                if left <= 0 or pause > left:
                    raise StoryBusy(
                        f"{exc} (still busy after {self.retries} retries over "
                        f"{self.bound_s:.0f} s)",
                        dict(exc.details),
                        retry_after=exc.retry_after,
                    ) from None
                await self._pause(pause)
                self.retries += 1
                wait = min(wait * 2, MAX_WAIT_S)

    async def _pause(self, seconds: float) -> None:
        started = time.monotonic()
        try:
            if self._sleep is not None:
                await self._sleep(seconds)
            elif self.stop is None:
                await asyncio.sleep(seconds)
            else:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self.stop.wait(), timeout=seconds)
            if self.stop is not None and self.stop.is_set():
                raise Stopped("stopped while a case waited for its story")
        finally:
            self.slept_s += time.monotonic() - started


HOLDERS = text(
    "SELECT a.pid, a.state, a.wait_event_type, left(a.query, 80) AS query, "
    "round(extract(epoch FROM now() - a.xact_start)::numeric, 1) AS xact_s "
    "FROM pg_stat_activity a WHERE a.pid <> pg_backend_pid() "
    "AND a.backend_xid = (SELECT xmax FROM stories WHERE id = :story)"
)
"""#929: the backend whose transaction holds the story's row when a commit timed out on it (the
row's ``xmax`` is the locking transaction's id): its state, the first 80 characters of its
current or last statement (parameterized SQL, never story content) and its transaction's age.
A row locked by several transactions at once (a multixact) or already released matches none."""

TABLE_LOCKS = text(
    "SELECT a.pid, l.mode, l.granted, l.relation::regclass::text AS relation, a.state, "
    "left(a.query, 80) AS query, "
    "round(extract(epoch FROM now() - a.xact_start)::numeric, 1) AS xact_s "
    "FROM pg_locks l JOIN pg_stat_activity a ON a.pid = l.pid "
    "WHERE l.locktype = 'relation' AND a.pid <> pg_backend_pid() AND (NOT l.granted OR l.mode IN "
    "('ShareUpdateExclusiveLock', 'ShareLock', 'ShareRowExclusiveLock', 'ExclusiveLock', "
    "'AccessExclusiveLock'))"
)
"""#929: every table lock then waited for, or held in a mode that blocks writers or DDL. The
engine reports any lock wait past ``LOCK_TIMEOUT`` in a commit as the story being busy
(``operation.lock_timed_out``), so the lock a commit waited on need not be the story's row: a
partition attach or detach (``story_partitions``) takes table locks on ``memories`` and, through
its cloned foreign key, on ``operations``."""


async def log_holders(db: AsyncEngine, exc: StoryBusy) -> None:
    """Log what held the story's row when a case's commit was refused (#929), so a CI run that
    meets ``lock_timeout`` names the holder. Best effort: a failed look is logged as such."""
    try:
        async with db.connect() as conn:
            row = [
                dict(r._mapping)
                for r in await conn.execute(HOLDERS, {"story": exc.details.get("story_id")})
            ]
            tables = [dict(r._mapping) for r in await conn.execute(TABLE_LOCKS)]
    except Exception as failed:
        log.warning("evals: story busy (lock_timeout); holders unknown: %s", type(failed).__name__)
        return
    log.warning(
        "evals: story %s busy (lock_timeout); retrying. Holder of its row: %s. Table locks "
        "waited for or held in a blocking mode: %s",
        exc.details.get("story_id"),
        row or "none left (released, or a multixact)",
        tables or "none",
    )


CURRENT: ContextVar[BusyRetry | None] = ContextVar("BUSY_RETRY", default=None)


async def retrying[T](call: Callable[[], Awaitable[T]]) -> T:
    """``call()`` under the current case's ``BusyRetry`` (just ``call()`` outside a case)."""
    budget = CURRENT.get()
    if budget is None:
        return await call()
    return await budget.run(call)


class BusyEngine:
    """An ``Engine`` whose coroutine methods retry ``StoryBusy`` under the current case's
    budget; every other attribute is the engine's own (``db``, ``models``, ``settings``)."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    @property
    def unwrapped(self) -> Engine:
        return self._engine

    def __getattr__(self, name: str) -> Any:
        found = getattr(self._engine, name)
        if not inspect.iscoroutinefunction(found):
            return found

        @functools.wraps(found)
        async def retried(*args: Any, **kwargs: Any) -> Any:
            return await retrying(lambda: found(*args, **kwargs))

        return retried


def busy_engine(engine: Engine) -> Engine:
    """``engine`` behind a ``BusyEngine`` (typed as the engine it stands for)."""
    if isinstance(engine, BusyEngine):
        return engine
    proxy: Any = BusyEngine(engine)
    wrapped: Engine = proxy
    return wrapped


__all__ = [
    "BOUND_S",
    "CURRENT",
    "RETRIED",
    "BusyEngine",
    "BusyRetry",
    "Stopped",
    "busy_engine",
    "retrying",
]
