"""The recurring tasks this build owns, registered by name (NFR-REL-5; #71).

A recurring task is a ``recurring_tasks`` row plus a job handler of the same name: when the row
falls due a worker enqueues a job of that kind (``timers.fire_due``). M2 registers
``idempotency_purge`` (hourly), ``retention_sweep`` (daily) and ``turn_queue_drain`` (every 30 s)
in ``handlers``; later stories register theirs the same way (``register_recurring`` before any
worker starts). A worker's recovery pass reconciles the registry into rows (``timers.reconcile``):
a missing row is created due now and a changed interval is updated, and a row no task is
registered for is left alone, never fired, and logged.

``recording(name, run)`` wraps a task's handler so that the job records the run's outcome on
the row: ``last_status`` ``ok``, or ``failed`` with the exception class (never its message).
Recurring jobs run with ``max_attempts = 1``: a failed run waits for the next interval.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta

from pydantic import BaseModel, ConfigDict
from sqlalchemy import update

from epistrel.core.tables import recurring_tasks
from epistrel.orchestrator.jobs.types import JobContext, JobOutput, Run

logger = logging.getLogger(__name__)

OK, FAILED = "ok", "failed"


class NoPayload(BaseModel):
    """A recurring job's payload: nothing (the task's row says everything)."""

    model_config = ConfigDict(extra="forbid")


@dataclass(frozen=True, slots=True)
class RecurringTask:
    """A sweep: its ``name`` (the job kind it enqueues) and how often it runs."""

    name: str
    interval: timedelta


_TASKS: dict[str, RecurringTask] = {}


def register_recurring(task: RecurringTask) -> None:
    """Own ``task`` from now on (replacing a task of the same name)."""
    _TASKS[task.name] = task


def unregister_recurring(name: str) -> None:
    _TASKS.pop(name, None)


def registered_tasks() -> dict[str, RecurringTask]:
    """A copy of the registry, by name."""
    return dict(_TASKS)


async def record(ctx: JobContext, status: str, error: str | None = None) -> None:
    """Record the outcome of the run ``ctx`` is (its job kind is the task's name)."""
    async with ctx.db.begin() as conn:
        await conn.execute(
            update(recurring_tasks)
            .where(recurring_tasks.c.name == ctx.job.kind)
            .values(last_status=status, last_error=error)
        )


def recording(run: Run) -> Run:
    """``run``, recording ``last_status`` on the task's row when it ends."""

    async def wrapped(ctx: JobContext) -> JobOutput:
        try:
            output = await run(ctx)
        except Exception as exc:
            await record(ctx, FAILED, type(exc).__name__)
            raise
        await record(ctx, OK)
        return output

    return wrapped


__all__ = [
    "FAILED",
    "OK",
    "NoPayload",
    "RecurringTask",
    "record",
    "recording",
    "register_recurring",
    "registered_tasks",
    "unregister_recurring",
]
