"""``media_sweep``: the periodic media sweep (FR-MEDIA-2, NFR-REL-5; #105).

A recurring task every ``media_sweep_interval`` seconds (a deployment setting, default one hour;
``RecurringTask.interval_setting``). The job runs ``core.media.sweep.sweep`` over the blob store
of the process's ``Engine``, which ``Engine.start_workers`` binds here (``bind``); a process
with no store bound runs the job as a no-op (``{"store": false}``). Like the other sweeps it
records its outcome on its ``recurring_tasks`` row, runs once per due time (``max_attempts =
1``), writes no story state (no ``operations`` row), and stores counts only.
"""

from __future__ import annotations

from datetime import timedelta

from epistrel.core.media.store import LocalFilesystemStore
from epistrel.core.media.sweep import sweep
from epistrel.core.settings_schema import DEFAULT_MEDIA_SWEEP_INTERVAL_S, MEDIA_SWEEP_TASK
from epistrel.orchestrator.jobs.recurring import NoPayload, RecurringTask, recording
from epistrel.orchestrator.jobs.types import JobContext, JobHandler, JobOutput

MEDIA_SWEEP = MEDIA_SWEEP_TASK
INTERVAL_SETTING = "media_sweep_interval"

_bound: list[LocalFilesystemStore] = []


def bind(store: LocalFilesystemStore | None) -> None:
    """Make ``store`` (the process's ``Engine.media``) what the job sweeps; ``None`` unbinds."""
    _bound.clear()
    if store is not None:
        _bound.append(store)


def bound() -> LocalFilesystemStore | None:
    return _bound[0] if _bound else None


async def _run(ctx: JobContext) -> JobOutput:
    store = bound()
    if store is None:
        return JobOutput(result={"store": False})
    return JobOutput(result=await sweep(ctx.db, store))


MEDIA_SWEEP_HANDLER = JobHandler(
    kind=MEDIA_SWEEP,
    payload=NoPayload,
    run=recording(_run),
    max_attempts=1,
    records_operation=False,
)
MEDIA_SWEEP_TASK_DEFINITION = RecurringTask(
    MEDIA_SWEEP,
    timedelta(seconds=DEFAULT_MEDIA_SWEEP_INTERVAL_S),
    interval_setting=INTERVAL_SETTING,
)

__all__ = [
    "INTERVAL_SETTING",
    "MEDIA_SWEEP",
    "MEDIA_SWEEP_HANDLER",
    "MEDIA_SWEEP_TASK_DEFINITION",
    "bind",
    "bound",
]
