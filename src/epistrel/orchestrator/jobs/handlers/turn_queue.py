"""``turn_queue_drain``: the per-story turn-queue safety net, every 30 s (FR-CONC-5,
NFR-REL-5; #71).

A turn queued by an API process is kicked in that process at once (#63); this task makes sure a
queue whose process died, or one queued while no drainer was running, is still drained by a
worker. The drainer belongs to the process's ``Engine``: ``Engine.start_workers`` binds it here
(``bind``), and the job kicks it for every story with queued turns. The same kick is
registered as the worker's ``turn_queue`` recovery hook. A process with no drainer bound runs
the job as a no-op (``{"drainer": false}``).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import timedelta

from epistrel.orchestrator.jobs.recurring import NoPayload, RecurringTask, recording
from epistrel.orchestrator.jobs.types import JobContext, JobHandler, JobOutput

TURN_QUEUE_DRAIN = "turn_queue_drain"
INTERVAL = timedelta(seconds=30)

Kick = Callable[[], Awaitable[None]]

_bound: list[Kick] = []


def bind(kick: Kick | None) -> None:
    """Make ``kick`` (the process's ``QueueDrainer.recover``) what the job runs; ``None``
    unbinds."""
    _bound.clear()
    if kick is not None:
        _bound.append(kick)


def bound() -> Kick | None:
    return _bound[0] if _bound else None


async def _run(ctx: JobContext) -> JobOutput:
    kick = bound()
    if kick is None:
        return JobOutput(result={"drainer": False})
    await kick()
    return JobOutput(result={"drainer": True})


TURN_QUEUE_HANDLER = JobHandler(
    kind=TURN_QUEUE_DRAIN,
    payload=NoPayload,
    run=recording(_run),
    max_attempts=1,
    records_operation=False,
)
TURN_QUEUE_TASK = RecurringTask(TURN_QUEUE_DRAIN, INTERVAL)

__all__ = ["INTERVAL", "TURN_QUEUE_DRAIN", "TURN_QUEUE_HANDLER", "TURN_QUEUE_TASK", "bind", "bound"]
