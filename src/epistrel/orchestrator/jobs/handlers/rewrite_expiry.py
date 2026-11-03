"""``rewrite_session_expiry``: rewrite sessions idle past their ``expires_at`` expire
(FR-REWRITE-4; #223).

A recurring task every 10 minutes (fixed). One global sweep moves up to 500 idle sessions
(``awaiting_answers`` or ``awaiting_approval``) to ``expired``; an ``applying`` session never
expires. Like the other sweeps it records its outcome on its ``recurring_tasks`` row, runs once
per due time (``max_attempts = 1``), writes no story state and stores a count only.
"""

from __future__ import annotations

from datetime import timedelta

from epistrel.orchestrator.jobs.recurring import NoPayload, RecurringTask, recording
from epistrel.orchestrator.jobs.types import JobContext, JobHandler, JobOutput
from epistrel.orchestrator.rewrite.session import expire_idle

REWRITE_EXPIRY = "rewrite_session_expiry"
INTERVAL = timedelta(minutes=10)


async def _run(ctx: JobContext) -> JobOutput:
    return JobOutput(result={"expired": await expire_idle(ctx.db)})


REWRITE_EXPIRY_HANDLER = JobHandler(
    kind=REWRITE_EXPIRY,
    payload=NoPayload,
    run=recording(_run),
    max_attempts=1,
    records_operation=False,
)
REWRITE_EXPIRY_TASK = RecurringTask(REWRITE_EXPIRY, INTERVAL)

__all__ = ["INTERVAL", "REWRITE_EXPIRY", "REWRITE_EXPIRY_HANDLER", "REWRITE_EXPIRY_TASK"]
