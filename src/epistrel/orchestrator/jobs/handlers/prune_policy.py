"""``prune_run``: the daily automatic pruning (FR-STORE-32, NFR-REL-5; #248).

A recurring task, daily. Each run prunes, for every story whose ``prune_policy`` is set and that
is active, the dormant continuations and discarded tails older than its ``older_than_days``,
through #247's pruning with ``trigger: policy`` as ``system`` (opting into the policy is the
confirmation, so there is no report stage). A story with nothing eligible gets no operation. A
missed run runs once when the workers return (the recurring task's own catch-up), and a rerun
changes nothing further: a pruned target is no longer eligible. The result holds counts only.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.core import clock
from epistrel.orchestrator.jobs.recurring import NoPayload, RecurringTask, recording
from epistrel.orchestrator.jobs.types import JobContext, JobHandler, JobOutput

PRUNE_RUN = "prune_run"
INTERVAL = timedelta(days=1)


Runner = Callable[[AsyncEngine, datetime], Awaitable[dict[str, int]]]
_runner: list[Runner] = []


def bind(runner: Runner | None) -> None:
    """``core.prune.run_policy``, bound by the Engine at start-up (the Orchestrator never
    imports the feedback marking a pruning calls: ``feedback-is-flag-only``)."""
    _runner.clear()
    if runner is not None:
        _runner.append(runner)


async def _run(ctx: JobContext) -> JobOutput:
    if not _runner:
        return JobOutput(result={"stories": 0, "pruned": 0, "unbound": 1})
    return JobOutput(result=await _runner[0](ctx.db, clock.now()))


PRUNE_RUN_HANDLER = JobHandler(
    kind=PRUNE_RUN,
    payload=NoPayload,
    run=recording(_run),
    max_attempts=1,
    records_operation=False,
)
PRUNE_RUN_TASK = RecurringTask(PRUNE_RUN, INTERVAL)

__all__ = ["INTERVAL", "PRUNE_RUN", "PRUNE_RUN_HANDLER", "PRUNE_RUN_TASK", "bind"]
