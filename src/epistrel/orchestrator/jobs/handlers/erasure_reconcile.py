"""``erasure_reconcile_retry``: the erasure reconciler's safety net, every 5 minutes
(FR-ADMIN-7; #125).

At start-up the Engine compares the erasure ledger's file with the database and reapplies what a
restored backup lacked, within a 10-minute budget (``core.erasure.ledger``). An entry still
pending after it (its reapply failed or ran out of time) keeps its story blocked, and this task
runs the reconciler again until the entry is applied: no attempt count ends it. The run belongs
to the process's ``Engine``: ``Engine.start_workers`` binds it here (``bind``). A process with no
Engine bound runs the job as a no-op (``{"reconciler": false}``).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import Any

from epistrel.orchestrator.jobs.recurring import NoPayload, RecurringTask, recording
from epistrel.orchestrator.jobs.types import JobContext, JobHandler, JobOutput

ERASURE_RECONCILE_RETRY = "erasure_reconcile_retry"
INTERVAL = timedelta(minutes=5)

Run = Callable[[], Awaitable[Any]]

_bound: list[Run] = []


def bind(run: Run | None) -> None:
    """Make ``run`` (the process's ``Engine._reconcile_run``) what the job runs; ``None``
    unbinds."""
    _bound.clear()
    if run is not None:
        _bound.append(run)


def bound() -> Run | None:
    return _bound[0] if _bound else None


async def _run(ctx: JobContext) -> JobOutput:
    run = bound()
    if run is None:
        return JobOutput(result={"reconciler": False})
    report = await run()
    return JobOutput(
        result={
            "reconciler": True,
            "ran": bool(getattr(report, "ran", False)),
            "settled": len(getattr(report, "settled", ())),
            "failed": len(getattr(report, "failed", ())),
        }
    )


ERASURE_RECONCILE_HANDLER = JobHandler(
    kind=ERASURE_RECONCILE_RETRY,
    payload=NoPayload,
    run=recording(_run),
    max_attempts=1,
    records_operation=False,
)
ERASURE_RECONCILE_TASK = RecurringTask(ERASURE_RECONCILE_RETRY, INTERVAL)

__all__ = [
    "ERASURE_RECONCILE_HANDLER",
    "ERASURE_RECONCILE_RETRY",
    "ERASURE_RECONCILE_TASK",
    "INTERVAL",
    "bind",
    "bound",
]
